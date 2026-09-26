import asyncio
import audioop
import logging
import threading
import time

import discord
from discord.ext import commands, voice_recv
from google.genai import types
from config import GEMINI_API_KEY, gemini_client

log = logging.getLogger(__name__)


def _log_future_error(future: "asyncio.Future"):
    """run_coroutine_threadsafeの戻り値は誰も見ないと例外が握りつぶされるため、ログに残す。"""
    exc = future.exception() if not future.cancelled() else None
    if exc:
        log.exception("voice input pipeline error", exc_info=exc)

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


class GeminiOutputSource(discord.AudioSource):
    """Geminiの応答音声(24kHz mono)をDiscord用48kHzステレオへ変換して供給する。

    read()はDiscord側の再生スレッドから20ms周期で呼ばれ、push()は
    非同期タスク側から呼ばれるため、バッファはthreading.Lockで保護する。
    """

    def __init__(self):
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._rate_state = None

    def push(self, pcm_24k_mono: bytes):
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

    def start(self):
        loop = asyncio.get_running_loop()
        sink = GeminiInputSink(loop, self._on_input_pcm)
        self.voice_client.listen(sink)
        self.voice_client.play(self.output)
        self._task = asyncio.create_task(self._run())

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
        while True:
            pcm = await self._send_queue.get()
            await session.send_realtime_input(
                audio=types.Blob(data=pcm, mime_type=f"audio/pcm;rate={GEMINI_IN_RATE}")
            )

    async def _recv_loop(self, session):
        async for response in session.receive():
            sc = response.server_content
            if not sc:
                continue
            if sc.interrupted:
                self.output.clear()
            if sc.model_turn:
                for part in sc.model_turn.parts:
                    if part.inline_data and part.inline_data.data:
                        self.output.push(part.inline_data.data)

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
