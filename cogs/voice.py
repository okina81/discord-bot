import asyncio
import audioop
import collections
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

# パイプライン各段の通過件数。定期的に1行のヘルスログとして出し、どこで止まったかを切り分ける。
# 複数スレッドから加算されるが、診断用なので多少の数え漏れは許容する。
STATS: collections.Counter = collections.Counter()
_last_dave_error_log = 0.0


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
        STATS["opus_corrupt"] += 1
        log.debug("voice recv: dropped a corrupted opus packet (ssrc=%s)", self.ssrc)
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
    global _last_dave_error_log
    data = getattr(packet, "decrypted_data", None)
    if data and data.endswith(DAVE_FRAME_MARKER):
        vc = self.sink.voice_client
        session = getattr(getattr(vc, "_connection", None), "dave_session", None)
        user_id = vc._get_id_from_ssrc(self.ssrc) if vc else None
        if session is None or not session.ready:
            STATS["dave_drop_not_ready"] += 1
            return
        if user_id is None:
            STATS["dave_drop_no_user"] += 1
            return
        try:
            packet.decrypted_data = session.decrypt(user_id, davey.MediaType.audio, data)
        except Exception as e:
            STATS["dave_drop_error"] += 1
            now = time.monotonic()
            if now - _last_dave_error_log > 30:
                _last_dave_error_log = now
                log.warning("voice recv: DAVE decrypt failed (ssrc=%s user=%s): %r", self.ssrc, user_id, e)
            return
        STATS["dave_decrypted"] += 1
    _original_push_packet(self, packet)


voice_recv_opus.PacketDecoder.push_packet = _dave_decrypting_push_packet

LIVE_MODEL = "gemini-3.8-live"
VOICE_NAME = "Puck"  # 明るい・アップビートな男性声
IDLE_TIMEOUT_SECONDS = 300
IDLE_CHECK_INTERVAL = 30
RECONNECT_GRACE_SECONDS = 15
INPUT_PAUSE_SECONDS = 0.5
RECONNECT_WINDOW_SECONDS = 60
MAX_RECONNECTS_PER_WINDOW = 3
RESTART_MAX_WAIT_SECONDS = 30
HEALTH_LOG_INTERVAL = 30

DISCORD_RATE = 48000
GEMINI_IN_RATE = 16000
GEMINI_OUT_RATE = 24000
FRAME_MS = 20
DISCORD_FRAME_BYTES = int(DISCORD_RATE * 2 * 2 * FRAME_MS / 1000)  # 48kHz stereo 16bit, 20ms
TRAILING_SILENCE_SECONDS = 1.0
TRAILING_SILENCE = b"\x00" * int(GEMINI_IN_RATE * 2 * TRAILING_SILENCE_SECONDS)  # 16kHz mono 16bit

SYSTEM_INSTRUCTION = (
    "あなたはDiscordのボイスチャンネルに参加している、関西弁で話す明るい青年です。"
    "必ず日本語(関西弁)だけで話してください。相手が英語など日本語以外の言語で話しかけてきても、"
    "返事は必ず日本語にし、日本語以外の言語に切り替えないでください。"
    "常にノリの良い関西弁(大阪弁)で、テンション高めにカジュアルに、簡潔に会話してください。"
    "標準語や丁寧語には絶対に戻らないでください。"
)
GREETING_PROMPT = "今ボイスチャンネルに参加したところです。関西弁で、みんなに一言だけ短く挨拶してください。"


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
        STATS["gemini_audio_bytes"] += len(pcm_24k_mono)
        log.debug("voice output: received %d bytes of audio from Gemini", len(pcm_24k_mono))
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
                STATS["played_frames"] += 1
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
        STATS["discord_pcm_packets"] += 1
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
        self._resume_handle: str | None = None
        self._health_task: asyncio.Task | None = None

    def start(self):
        self._loop = asyncio.get_running_loop()
        self._start_listening()
        self._start_playing()
        self._task = asyncio.create_task(self._run())
        self._health_task = asyncio.create_task(self._health_log())

    def _start_listening(self):
        sink = GeminiInputSink(self._loop, self._on_input_pcm)
        self.voice_client.listen(sink, after=self._on_listen_stopped)

    def _start_playing(self):
        self.voice_client.play(self.output, after=self._on_play_stopped)

    # 受信(voice_recvのリーダー)も再生(discord.pyのAudioPlayer)も、内部で例外が起きると
    # スレッドごと終了し、以後は二度と動かない。どちらも止まれば通話は無反応になるため、
    # 停止を検知したら自動で立ち上げ直す。コールバックは各スレッドから呼ばれる。
    def _on_listen_stopped(self, error: Exception | None):
        if self._closed:
            return
        log.warning("voice listen: stopped unexpectedly (%r), restarting", error)
        asyncio.run_coroutine_threadsafe(
            self._restart("listen", self.voice_client.is_listening, self._start_listening), self._loop
        )

    def _on_play_stopped(self, error: Exception | None):
        if self._closed:
            return
        log.warning("voice play: stopped unexpectedly (%r), restarting", error)
        asyncio.run_coroutine_threadsafe(
            self._restart("play", self.voice_client.is_playing, self._start_playing), self._loop
        )

    async def _restart(self, name: str, is_running, start):
        # Discordとの再接続中はlisten()/play()が「未接続」で失敗するため、接続が戻るまで待つ。
        for _ in range(RESTART_MAX_WAIT_SECONDS):
            await asyncio.sleep(1)
            if self._closed or is_running():
                return
            if not self.voice_client.is_connected():
                continue
            try:
                start()
                log.info("voice %s: restarted successfully", name)
                return
            except Exception:
                log.exception("voice %s: failed to restart", name)
        log.error("voice %s: gave up restarting after %ds", name, RESTART_MAX_WAIT_SECONDS)

    async def _health_log(self):
        last = collections.Counter(STATS)
        while True:
            await asyncio.sleep(HEALTH_LOG_INTERVAL)
            d = STATS - last
            last = collections.Counter(STATS)
            log.info(
                "voice health (last %ds): discord_pcm=%d dave_ok=%d dave_drop[not_ready=%d no_user=%d error=%d] "
                "opus_corrupt=%d sent_chunks=%d stream_end=%d gemini_audio=%.1fs played=%.1fs "
                "interrupted=%d turns=%d | playing=%s listening=%s connected=%s",
                HEALTH_LOG_INTERVAL, d["discord_pcm_packets"], d["dave_decrypted"], d["dave_drop_not_ready"],
                d["dave_drop_no_user"], d["dave_drop_error"], d["opus_corrupt"], d["sent_chunks"],
                d["stream_ends"], d["gemini_audio_bytes"] / (GEMINI_OUT_RATE * 2), d["played_frames"] * FRAME_MS / 1000,
                d["interrupted"], d["turns_completed"],
                self.voice_client.is_playing(), self.voice_client.is_listening(), self.voice_client.is_connected(),
            )

    async def _on_input_pcm(self, pcm: bytes):
        self.last_activity = time.monotonic()
        await self._send_queue.put(pcm)

    def _live_config(self) -> types.LiveConnectConfig:
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=SYSTEM_INSTRUCTION,
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE_NAME)
                )
            ),
            # 圧縮なしの音声セッションは最大15分で打ち切られるため、古い履歴を圧縮して延命する。
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
            ),
            # 1本のWebSocket接続は約10分で切られる。再開ハンドルで文脈を保ったまま繋ぎ直す。
            session_resumption=types.SessionResumptionConfig(handle=self._resume_handle),
        )

    async def _run(self):
        try:
            reconnect_times: list[float] = []
            while True:
                resuming = self._resume_handle is not None
                async with gemini_client.aio.live.connect(model=LIVE_MODEL, config=self._live_config()) as session:
                    log.info("voice session: connected to %s for guild %s (resumed=%s)",
                             LIVE_MODEL, self.guild.id, resuming)
                    if not resuming:
                        await session.send_client_content(
                            turns=types.Content(role="user", parts=[types.Part(text=GREETING_PROMPT)]),
                            turn_complete=True,
                        )
                    ended_by_idle = await self._run_connection(session)
                if ended_by_idle:
                    return

                now = time.monotonic()
                reconnect_times = [t for t in reconnect_times if now - t < RECONNECT_WINDOW_SECONDS]
                reconnect_times.append(now)
                if self._resume_handle is None or len(reconnect_times) > MAX_RECONNECTS_PER_WINDOW:
                    raise RuntimeError("Geminiとの接続が切れて再接続できなかった")
                log.info("voice session: connection ended, reconnecting with resumption handle")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("voice session: fatal error")
            await self._safe_send(f"❌ ボイスセッションでエラーが発生したよ: {e}")
        finally:
            await self.cog.leave(self.guild.id)

    async def _run_connection(self, session) -> bool:
        """1本の接続を動かす。アイドルで終了したらTrue、接続が切れたらFalseを返す。"""
        send_task = asyncio.create_task(self._send_loop(session))
        recv_task = asyncio.create_task(self._recv_loop(session))
        idle_task = asyncio.create_task(self._idle_watch())
        tasks = [send_task, recv_task, idle_task]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if idle_task in done:
            return True
        for t in done:
            if t.exception():
                log.warning("voice session: connection ended: %r", t.exception())
        return False

    async def _send_loop(self, session):
        # Discordのクライアントは無音時にパケット自体を送らないため、Geminiの発話検知は
        # 「話し終わり」を判定できず応答を生成しない (audio_stream_end だけでは確定しない
        # ことを実APIで確認済み)。入力が途切れたら無音を送ってから audio_stream_end を送る。
        sent_count = 0
        streaming = False
        while True:
            try:
                pcm = await asyncio.wait_for(self._send_queue.get(), timeout=INPUT_PAUSE_SECONDS)
            except asyncio.TimeoutError:
                if streaming:
                    await session.send_realtime_input(
                        audio=types.Blob(data=TRAILING_SILENCE, mime_type=f"audio/pcm;rate={GEMINI_IN_RATE}")
                    )
                    await session.send_realtime_input(audio_stream_end=True)
                    STATS["stream_ends"] += 1
                    streaming = False
                    log.info("voice send: input paused, sent trailing silence + audio_stream_end")
                continue
            await session.send_realtime_input(
                audio=types.Blob(data=pcm, mime_type=f"audio/pcm;rate={GEMINI_IN_RATE}")
            )
            streaming = True
            STATS["sent_chunks"] += 1
            sent_count += 1
            if sent_count == 1 or sent_count % 50 == 0:
                log.debug("voice send: %d chunks sent to Gemini so far", sent_count)

    async def _recv_loop(self, session):
        # session.receive()は1ターン分(turn_complete)を返すと終了するため、ターンごとに回し直す。
        # 1件も受信できなかった場合は接続が閉じたとみなして抜け、空回りを防ぐ。
        while True:
            received = False
            async for response in session.receive():
                received = True
                self._handle_response(response)
            if not received:
                raise ConnectionError("Gemini Live session closed")

    def _handle_response(self, response):
        update = response.session_resumption_update
        if update and update.resumable and update.new_handle:
            self._resume_handle = update.new_handle
        if response.go_away:
            log.info("voice session: server sent GoAway (time_left=%s)", response.go_away.time_left)
        sc = response.server_content
        log.debug(
            "voice recv: setup_complete=%s server_content=%s interrupted=%s model_turn=%s",
            response.setup_complete is not None,
            sc is not None,
            getattr(sc, "interrupted", None),
            getattr(sc, "model_turn", None) is not None,
        )
        if not sc:
            return
        if sc.turn_complete:
            STATS["turns_completed"] += 1
        if sc.interrupted:
            STATS["interrupted"] += 1
            self.output.clear()
        if sc.model_turn:
            for part in sc.model_turn.parts:
                if part.inline_data and part.inline_data.data:
                    self.output.push(part.inline_data.data)
                else:
                    log.debug("voice recv: part with no inline_data: %s", part)

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
        if self._health_task:
            self._health_task.cancel()
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
                session = self.sessions.get(member.guild.id)
                if session is None:
                    return
                # discord.pyはボイスWSが4006等で切れると、一度channel=Noneで抜けてから
                # 自動で入り直す。ここで即leave()すると再接続を潰してしまうため、
                # 猶予を置いてから本当に切断されたままかを確認する。
                log.info("voice: bot left channel, waiting %ds for auto-reconnect", RECONNECT_GRACE_SECONDS)
                await asyncio.sleep(RECONNECT_GRACE_SECONDS)
                if self.sessions.get(member.guild.id) is session and not session.voice_client.is_connected():
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
