import asyncio
import audioop
import logging
import threading
import time

import davey
import discord
from discord.ext import commands, voice_recv
from discord.ext.voice_recv import opus as voice_recv_opus
from google.genai import types
from config import GEMINI_API_KEY, gemini_client

log = logging.getLogger(__name__)


def _log_future_error(future: "asyncio.Future"):
    """run_coroutine_threadsafeの戻り値は誰も見ないと例外が握りつぶされるため、ログに残す。"""
    exc = future.exception() if not future.cancelled() else None
    if exc:
        log.exception("voice input pipeline error", exc_info=exc)


# discord-ext-voice-recvは1パケットのOpusデコードに失敗すると例外が
# PacketRouterのスレッドまで伝播し、その場でリスニング全体を停止してしまう
# (voice_recv/router.py の run() が例外を捕捉した後 stop_listening() を呼ぶ)。
# 壊れたパケット1つで通話全体が無音になるのを防ぐため、デコード失敗時は
# そのパケットだけを無音として捨てて処理を継続させる。
_original_decode_packet = voice_recv_opus.PacketDecoder._decode_packet


def _resilient_decode_packet(self, packet):
    try:
        return _original_decode_packet(self, packet)
    except discord.opus.OpusError:
        log.warning("voice recv: dropped a corrupted opus packet (ssrc=%s)", self.ssrc)
        return packet, b""


voice_recv_opus.PacketDecoder._decode_packet = _resilient_decode_packet


# Discordは2026年3月からボイスのE2E暗号化(DAVE)を必須化しており、受信したOpusフレームは
# トランスポート層の復号後もDAVEで暗号化されたまま(末尾が0xFAFA)。discord.py 2.7は送信側の
# DAVE暗号化に対応しているが、discord-ext-voice-recv 0.5.2は受信フレームのDAVE復号をしない
# ため、暗号文がそのままOpusデコーダーに渡され大半のパケットが"corrupted stream"になる。
# ジッターバッファに積む前に、接続中のDAVEセッションで復号しておく。
DAVE_FRAME_MARKER = b"\xfa\xfa"
_original_push_packet = voice_recv_opus.PacketDecoder.push_packet


def _dave_decrypting_push_packet(self, packet):
    data = getattr(packet, "decrypted_data", None)
    if data and data.endswith(DAVE_FRAME_MARKER):
        vc = self.sink.voice_client
        session = getattr(getattr(vc, "_connection", None), "dave_session", None)
        user_id = vc._get_id_from_ssrc(self.ssrc) if vc else None
        if session is None or not session.ready or user_id is None:
            return
        try:
            packet.decrypted_data = session.decrypt(user_id, davey.MediaType.audio, data)
        except Exception as e:
            log.debug("voice recv: DAVE decrypt failed (ssrc=%s): %r", self.ssrc, e)
            return
    _original_push_packet(self, packet)


voice_recv_opus.PacketDecoder.push_packet = _dave_decrypting_push_packet

LIVE_MODEL = "gemini-3.8-live"
VOICE_NAME = "Puck"  # 明るい・アップビートな男性声
IDLE_TIMEOUT_SECONDS = 300
IDLE_CHECK_INTERVAL = 30

DISCORD_RATE = 48000
GEMINI_IN_RATE = 16000
GEMINI_OUT_RATE = 24000
FRAME_MS = 20
DISCORD_FRAME_BYTES = int(DISCORD_RATE * 2 * 2 * FRAME_MS / 1000)  # 48kHz stereo 16bit, 20ms

SYSTEM_INSTRUCTION = (
    "あなたはDiscordのボイスチャンネルに参加している、関西弁で話す明るい青年です。"
    "常にノリの良い関西弁(大阪弁)で、テンション高めにカジュアルに、簡潔に会話してください。"
    "標準語や丁寧語には絶対に戻らないでください。"
)
GREETING_PROMPT = "今ボイスチャンネルに参加したところです。みんなに一言だけ短く挨拶してください。"


class GeminiOutputSource(discord.AudioSource):
    """Geminiの応答音声(24kHz mono)をDiscord用48kHzステレオへ変換して供給する。

    read()はDiscord側の再生スレッドから20ms周期で呼ばれ、push()は
    非同期タスク側から呼ばれるため、バッファはthreading.Lockで保護する。
    """

    def __init__(self):
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._rate_state = None
        self._read_logged = False

    def push(self, pcm_24k_mono: bytes):
        log.info("voice output: received %d bytes of audio from Gemini", len(pcm_24k_mono))
        converted, self._rate_state = audioop.ratecv(
            pcm_24k_mono, 2, 1, GEMINI_OUT_RATE, DISCORD_RATE, self._rate_state
        )
        stereo = audioop.tostereo(converted, 2, 1, 1)
        with self._lock:
            self._buffer.extend(stereo)

    def clear(self):
        with self._lock:
            self._buffer.clear()

    def read(self) -> bytes:
        if not self._read_logged:
            self._read_logged = True
            log.info("voice output: Discord player started pulling frames")
        with self._lock:
            if len(self._buffer) >= DISCORD_FRAME_BYTES:
                chunk = bytes(self._buffer[:DISCORD_FRAME_BYTES])
                del self._buffer[:DISCORD_FRAME_BYTES]
                return chunk
        return b"\x00" * DISCORD_FRAME_BYTES

    def is_opus(self) -> bool:
        return False


class GeminiInputSink(voice_recv.AudioSink):
    """Discordの各話者の音声(48kHz stereo)をGemini向け16kHz monoへ変換して渡す。

    write()はdiscord-ext-voice-recvのソケット受信スレッドから呼ばれるため、
    asyncio.run_coroutine_threadsafeでイベントループ側へ橋渡しする。
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, on_pcm):
        super().__init__()
        self._loop = loop
        self._on_pcm = on_pcm
        self._rate_states = {}

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data: voice_recv.VoiceData):
        if user is None or getattr(user, "bot", False):
            return
        pcm = data.pcm
        if not pcm:
            return
        if user.id not in self._rate_states:
            log.info("voice input: first packet received from %s (%d bytes)", user, len(pcm))
        mono = audioop.tomono(pcm, 2, 0.5, 0.5)
        state = self._rate_states.get(user.id)
        converted, state = audioop.ratecv(mono, 2, 1, DISCORD_RATE, GEMINI_IN_RATE, state)
        self._rate_states[user.id] = state
        future = asyncio.run_coroutine_threadsafe(self._on_pcm(converted), self._loop)
        future.add_done_callback(_log_future_error)

    def cleanup(self):
        self._rate_states.clear()


class VoiceSession:
    def __init__(self, cog: "Voice", guild: discord.Guild,
                 voice_client: voice_recv.VoiceRecvClient, text_channel):
        self.cog = cog
        self.guild = guild
        self.voice_client = voice_client
        self.text_channel = text_channel
        self.output = GeminiOutputSource()
        self.last_activity = time.monotonic()
        self._send_queue: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self):
        self._loop = asyncio.get_running_loop()
        self._start_listening()
        self.voice_client.play(self.output)
        self._task = asyncio.create_task(self._run())

    def _start_listening(self):
        sink = GeminiInputSink(self._loop, self._on_input_pcm)
        self.voice_client.listen(sink, after=self._on_listen_stopped)

    def _on_listen_stopped(self, error: Exception | None):
        # discord-ext-voice-recvのリーダースレッドから呼ばれるコールバック。
        # 通常の切断(close()経由)ならself._closedが立っているので何もしない。
        # 想定外にリスニングが停止した場合は音声入力が完全に無音になってしまうため、
        # 自動で再度listen()し直す。
        if self._closed:
            return
        if error:
            log.warning("voice listen: stopped unexpectedly (%r), restarting", error)
        else:
            log.warning("voice listen: stopped unexpectedly, restarting")
        asyncio.run_coroutine_threadsafe(self._restart_listening(), self._loop)

    async def _restart_listening(self):
        if self._closed:
            return
        try:
            self._start_listening()
            log.info("voice listen: restarted successfully")
        except Exception:
            log.exception("voice listen: failed to restart")

    async def _on_input_pcm(self, pcm: bytes):
        self.last_activity = time.monotonic()
        await self._send_queue.put(pcm)

    async def _run(self):
        try:
            config = types.LiveConnectConfig(
                response_modalities=["AUDIO"],
                system_instruction=SYSTEM_INSTRUCTION,
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE_NAME)
                    )
                ),
            )
            async with gemini_client.aio.live.connect(model=LIVE_MODEL, config=config) as session:
                log.info("voice session: connected to %s for guild %s", LIVE_MODEL, self.guild.id)
                await session.send_client_content(
                    turns=types.Content(role="user", parts=[types.Part(text=GREETING_PROMPT)]),
                    turn_complete=True,
                )
                tasks = [
                    asyncio.create_task(self._send_loop(session)),
                    asyncio.create_task(self._recv_loop(session)),
                    asyncio.create_task(self._idle_watch()),
                ]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for t in tasks:
                        t.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                for t in done:
                    exc = t.exception()
                    if exc:
                        raise exc
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._safe_send(f"❌ ボイスセッションでエラーが発生したよ: {e}")
        finally:
            await self.cog.leave(self.guild.id)

    async def _send_loop(self, session):
        sent_count = 0
        while True:
            pcm = await self._send_queue.get()
            await session.send_realtime_input(
                audio=types.Blob(data=pcm, mime_type=f"audio/pcm;rate={GEMINI_IN_RATE}")
            )
            sent_count += 1
            if sent_count == 1 or sent_count % 50 == 0:
                log.info("voice send: %d chunks sent to Gemini so far", sent_count)

    async def _recv_loop(self, session):
        async for response in session.receive():
            sc = response.server_content
            log.info(
                "voice recv: setup_complete=%s server_content=%s interrupted=%s model_turn=%s",
                response.setup_complete is not None,
                sc is not None,
                getattr(sc, "interrupted", None),
                getattr(sc, "model_turn", None) is not None,
            )
            if not sc:
                continue
            if sc.interrupted:
                self.output.clear()
            if sc.model_turn:
                for part in sc.model_turn.parts:
                    if part.inline_data and part.inline_data.data:
                        self.output.push(part.inline_data.data)
                    else:
                        log.info("voice recv: part with no inline_data: %s", part)

    async def _idle_watch(self):
        while True:
            await asyncio.sleep(IDLE_CHECK_INTERVAL)
            if time.monotonic() - self.last_activity > IDLE_TIMEOUT_SECONDS:
                await self._safe_send("🔇 しばらく静かだったから通話から抜けるね")
                return

    async def _safe_send(self, message: str):
        try:
            await self.text_channel.send(message)
        except Exception:
            pass

    async def close(self, notify_message: str | None = None):
        if self._closed:
            return
        self._closed = True
        if notify_message:
            await self._safe_send(notify_message)
        current = asyncio.current_task()
        if self._task and self._task is not current:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            self.voice_client.stop()
        except Exception:
            pass
        try:
            await self.voice_client.disconnect(force=True)
        except Exception:
            pass


class Voice(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.sessions: dict[int, VoiceSession] = {}

    async def leave(self, guild_id: int, notify_message: str | None = None):
        session = self.sessions.pop(guild_id, None)
        if session:
            await session.close(notify_message)

    @commands.command(name="voice")
    @commands.is_owner()
    async def voice_cmd(self, ctx, action: str = "join"):
        action = action.lower()
        if action not in ("join", "leave"):
            await ctx.send("❌ 使い方: `!voice join` / `!voice leave`")
            return

        if action == "leave":
            if ctx.guild.id not in self.sessions:
                await ctx.send("❌ 通話に参加してないよ！")
                return
            await self.leave(ctx.guild.id)
            await ctx.send("👋 通話から抜けたよ")
            return

        if not GEMINI_API_KEY:
            await ctx.send("❌ GEMINI_API_KEY が設定されていないよ！")
            return
        if ctx.guild.id in self.sessions:
            await ctx.send("❌ もう通話に参加してるよ！")
            return
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("❌ 先にボイスチャンネルに入ってね！")
            return

        channel = ctx.author.voice.channel
        try:
            vc = await channel.connect(cls=voice_recv.VoiceRecvClient)
        except Exception as e:
            await ctx.send(f"❌ 接続に失敗したよ: {e}")
            return

        session = VoiceSession(self, ctx.guild, vc, ctx.channel)
        self.sessions[ctx.guild.id] = session
        try:
            session.start()
        except Exception as e:
            self.sessions.pop(ctx.guild.id, None)
            await vc.disconnect(force=True)
            await ctx.send(f"❌ 開始に失敗したよ: {type(e).__name__}: {e}")
            return
        await ctx.send(f"🎙️ **{channel.name}** に参加したよ！話しかけてね")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.id == self.bot.user.id:
            if after.channel is None:
                await self.leave(member.guild.id)
            return

        session = self.sessions.get(member.guild.id)
        if session is None:
            return
        channel = session.voice_client.channel
        if channel and all(m.bot for m in channel.members):
            await self.leave(member.guild.id, "🚪 誰もいなくなったから通話から抜けるね")


async def setup(bot):
    await bot.add_cog(Voice(bot))
