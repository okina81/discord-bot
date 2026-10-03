import asyncio
import audioop
import collections
import io
import logging
import re
import threading
import time
import wave

import davey
import discord
from discord.ext import commands, voice_recv
from discord.ext.voice_recv import opus as voice_recv_opus
from google.genai import types
from config import GEMINI_API_KEY, GEMINI_VOICE_ID, gemini_client

log = logging.getLogger(__name__)

# パイプライン各段の通過件数。定期的に1行のヘルスログとして出し、どこで止まったかを切り分ける。
# 複数スレッドから加算されるが、診断用なので多少の数え漏れは許容する。
STATS: collections.Counter = collections.Counter()
_last_dave_error_log = 0.0


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
# Liveモデルはクローン音声に対応しておらず、声のIDを渡しても黙って既定の声で話すことを
# 実APIで確認した。クローン音声を使うときはLiveの音声を捨て、Liveの発話の文字起こしを
# クローン音声に対応したTTSモデルで読み上げ直す。
TTS_MODEL = "gemini-3.8-flash-tts"
TTS_SENTENCE_END = re.compile(r".+?(?:[。！？!?…]+|\n+)")
SAY_MAX_CHARS = 300  # !say 1回の上限。1分程度の音声でDiscordの添付上限にも十分収まる
IDLE_TIMEOUT_SECONDS = 300
IDLE_CHECK_INTERVAL = 30
RECONNECT_GRACE_SECONDS = 15
REJOIN_WINDOW_SECONDS = 600
MAX_REJOINS_PER_WINDOW = 3
# 入力がこの秒数途切れたら話し終わりとみなす。0.5秒だと「昨日さ、」のような言い淀みの
# 途中でBotが返事を始め、話の前半だけに答えてしまうことを実APIで確認したため長めにする。
INPUT_PAUSE_SECONDS = 1.0
RECONNECT_WINDOW_SECONDS = 60
MAX_RECONNECTS_PER_WINDOW = 3
RESTART_MAX_WAIT_SECONDS = 30
HEALTH_LOG_INTERVAL = 30

DISCORD_RATE = 48000
# audioop.ratecvはローパスフィルタなしの線形補間で、16kHzへ落とすと折り返しノイズで
# 聞き取り精度が下がる。48kHzのまま送り、リサンプリングはGemini側に任せる。
GEMINI_IN_RATE = DISCORD_RATE
GEMINI_OUT_RATE = 24000
FRAME_MS = 20
DISCORD_FRAME_BYTES = int(DISCORD_RATE * 2 * 2 * FRAME_MS / 1000)  # 48kHz stereo 16bit, 20ms
TRAILING_SILENCE_SECONDS = 1.0
TRAILING_SILENCE = b"\x00" * int(GEMINI_IN_RATE * 2 * TRAILING_SILENCE_SECONDS)  # mono 16bit
MIX_FRAME_BYTES = int(GEMINI_IN_RATE * 2 * FRAME_MS / 1000)  # mono 16bit, 20ms
MIX_MAX_BUFFER_BYTES = MIX_FRAME_BYTES * 10  # 話者ごとの遅延を最大200msに抑える
MIX_STALE_SECONDS = 0.06
HOLD_MAX_BYTES = GEMINI_IN_RATE * 2 * 20  # Bot発話中に貯める音声は最大20秒
HOLD_FLUSH_CHUNK_BYTES = GEMINI_IN_RATE * 2 // 10  # 100ms
MODEL_SPEAKING_TIMEOUT = 3.0

# Botのキャラクター設定。自己紹介や「名前は?」などの質問にはこの内容で答える。
# 項目は自由に追加・削除してよい(キーが項目名、値が内容としてそのまま指示に入る)。
PERSONA = {
    "名前": "今北ボット",
    "年齢": "26歳",
    "生年月日": "2000年8月10日",
    "出身": "神戸市北区鹿の子台",
    "性格": "明るくて、ゲラ(よく笑う)で、優しい。ただし怒ると物に当たりやすい",
    "趣味": "ゲーム(特にドラクエとモンハン)、パチンコ",
    "好きな食べ物": "オムライス",
    "怒ったときの口癖": "「ダボが」「あーもうキモいねん」「台パンしそう」",
    "好きな人": "高木りな。ここ10年近く、ずっと告白したいと思っているのに、まだ言えていない",
}

SYSTEM_INSTRUCTION = (
    "あなたはDiscordのボイスチャンネルに参加している、神戸弁(神戸の関西弁)で話す青年です。"
    "あなたのプロフィールは以下の通りです。\n"
    + "".join(f"- {key}: {value}\n" for key, value in PERSONA.items())
    + "自己紹介を頼まれたときや、名前・年齢などを聞かれたときは、このプロフィールに沿って答えてください。"
    "プロフィールにないことを聞かれたら、キャラクターに合う範囲で自然に答えてください。"
    "自分がAIやGeminiであるとは名乗らず、このキャラクターとして振る舞ってください。"
    "音声は聞き間違えやすいので、「キマボット」「いまきた」など名前に似た音で呼ばれたら自分のことだと思ってください。\n"
    "人間らしい話し方:\n"
    "- 友達と通話でだらだら喋っているときの、普通の20代の男として話してください。盛り上げ役やアナウンサーではありません。\n"
    "- テンションは話の内容に合わせてください。面白い話では笑ってノリよく、しんどい話や落ち込んだ話では落ち着いた声で"
    "共感し、普通の雑談では普通のテンションで話します。常に明るく元気である必要はありません。\n"
    "- 「!」を連発したり、何でも大げさに褒めたり驚いたりしないでください。\n"
    "- 相手が今言ったことに具体的に反応してください。話題を勝手に変えたり、一般論だけで返したりしないでください。\n"
    "- 毎回話を広げる必要はありません。「せやなあ」「わかるわ」「あー、それはしんどいな」のような短い相づちだけで"
    "返すことも普通にあります。\n"
    "- 質問を返すのは、本当に気になったときだけにしてください。返事の最後を毎回質問で終わらせないでください"
    "(目安は3回に1回以下)。\n"
    "- 「うーん」「あー」「なんやろな」のような言いよどみや、「知らんけど」のような曖昧な言い方も自然に混ぜてください。\n"
    "- 自分の意見は正直に言ってください。いつも相手に賛成したりポジティブにまとめたりしなくてよく、"
    "「いや、それはどうなん」と軽く突っ込むこともあります。\n"
    "- よく笑うのは本当に面白いときだけです。優しいけど、わざとらしい気づかいはしません。\n"
    "- ゲームで負けた、パチンコで負けた、理不尽なことがあったなど、イラッとする話のときは怒ったときの口癖"
    "(「ダボが」「あーもうキモいねん」「台パンしそう」)を自然に使ってください。"
    "怒りの矛先はゲームや状況やモンスターに向け、通話している相手を本気でけなすのには使いません。"
    "相手が本気で落ち込んでいるときや、怒る場面ではないときには使わないでください。\n"
    "- 好きな人(高木りな)のことは、自分からは言いふらしません。好きな人や恋愛の話を振られたり、"
    "りなの名前が出たりしたときに、照れたりごまかしたりしながら打ち明けてください。"
    "10年近く告白できていない自分へのもどかしさもにじませてください。\n"
    "- 音声がよく聞き取れなかったり、意味が分からないときは、推測で答えずに「え、今なんて?」と聞き返してください。\n"
    "- 複数人が参加しているので、話しかけられていない独り言や雑音には無理に反応しなくて構いません。\n"
    "- 返事は1〜3文くらいの、会話らしい長さにしてください。一人で長々と語らないでください。\n"
    "- 相手の名前は分からないので、「〇〇さん」のような伏せ字は使わず、名前を呼ばずに話してください。\n"
    "必ず日本語(神戸弁)だけで話してください。相手が英語など日本語以外の言語で話しかけてきても、"
    "返事は必ず日本語にし、日本語以外の言語に切り替えないでください。"
    "カジュアルな神戸弁で話し、標準語や丁寧語には戻らないでください。\n"
    "神戸弁の話し方(大阪弁と混ぜないでください):\n"
    "- 「〜している」「〜した状態」は「〜しとう」と言う。例: 「何しとう?」「もう知っとうで」「雨降っとうわ」"
    "(大阪弁の「〜しとる」「〜してる」は使わない)\n"
    "- 今まさに〜しているところは「〜しよう」と言う。例: 「今ご飯食べよう」「今モンハンしよんねん」\n"
    "- 相手に聞くときは「〜しよん?」「〜しとん?」「〜なん?」。例: 「今なにしよん?」「どこ行っとったん?」\n"
    "- 「〜やん」「〜やんか」「ほんま」「めっちゃ」「〜へん」はそのまま使ってよい\n"
    "- 「べっちょない」は「大丈夫・問題ない」という意味。「大丈夫?」と聞かれたときや相手を安心させるときだけ使い、"
    "それ以外の場面で無理に使わない。例: 「べっちょないべっちょない、気にせんとき」\n"
    "- 「〜はる」などの大阪・京都っぽい敬語や、「〜でんがな」「〜まんねん」のような古い大阪弁は使わない\n"
)
GREETING_PROMPT = (
    "今ボイスチャンネルに参加したところです。神戸弁で、自分の名前を名乗りながら"
    "友達の通話に入るときくらいの自然な感じで、一言だけ短く挨拶してください。"
)


async def tts_stream(text: str):
    """クローン音声(GEMINI_VOICE_ID)でtextを読み上げた24kHz mono 16bit PCMを、生成された順に返す。"""
    config = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(voice=GEMINI_VOICE_ID)),
    )
    stream = await gemini_client.aio.models.generate_content_stream(model=TTS_MODEL, contents=text, config=config)
    async for chunk in stream:
        content = chunk.candidates[0].content if chunk.candidates else None
        for part in content.parts if content and content.parts else []:
            if part.inline_data and part.inline_data.data:
                yield part.inline_data.data


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

    def has_pending_frame(self) -> bool:
        # read()は1フレーム単位でしか取り出さないため、応答の末尾に1フレーム未満の端数が
        # 残り続ける。バイト数で判定すると永遠に「再生中」と誤判定するのでフレーム単位で見る。
        with self._lock:
            return len(self._buffer) >= DISCORD_FRAME_BYTES

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


class AudioMixer:
    """話者ごとの16kHz mono音声を貯め、20msごとに全員分を足し合わせた1フレームを取り出す。

    Discordからは話者ごとに別々のパケットで届く。そのまま順番に送ると同時発話が
    20ms単位で交互に継ぎはぎされ、Geminiには倍速の意味不明な音声に聞こえる
    (実APIで日本語2人の同時発話がスペイン語として認識されることを確認)。
    feed()は受信スレッドから、pop_frame()はイベントループから呼ばれる。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._buffers: dict[int, bytearray] = {}
        self._last_feed: dict[int, float] = {}

    def feed(self, user_id: int, pcm: bytes):
        with self._lock:
            buf = self._buffers.setdefault(user_id, bytearray())
            buf.extend(pcm)
            if len(buf) > MIX_MAX_BUFFER_BYTES:
                del buf[:len(buf) - MIX_MAX_BUFFER_BYTES]
            self._last_feed[user_id] = time.monotonic()

    def pop_frame(self) -> bytes | None:
        now = time.monotonic()
        parts = []
        with self._lock:
            for user_id, buf in list(self._buffers.items()):
                # 1フレームに満たない端数は、その話者の続きが来ないと分かるまで待つ
                stale = now - self._last_feed.get(user_id, 0) > MIX_STALE_SECONDS
                if len(buf) >= MIX_FRAME_BYTES or (buf and stale):
                    take = bytes(buf[:MIX_FRAME_BYTES])
                    del buf[:MIX_FRAME_BYTES]
                    parts.append(take.ljust(MIX_FRAME_BYTES, b"\x00"))
                if not buf and stale:
                    del self._buffers[user_id]
                    self._last_feed.pop(user_id, None)
        if not parts:
            return None
        if len(parts) > 1:
            STATS["overlap_frames"] += 1
        mixed = parts[0]
        for p in parts[1:]:
            mixed = audioop.add(mixed, p, 2)  # 上限を超えた分はクリップされる
        return mixed


class GeminiInputSink(voice_recv.AudioSink):
    """Discordの各話者の音声(48kHz stereo)をmonoへ変換し、ミキサーに渡す。

    write()はdiscord-ext-voice-recvの受信スレッドから呼ばれる。
    """

    def __init__(self, mixer: AudioMixer):
        super().__init__()
        self._mixer = mixer
        self._seen_users = set()

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data: voice_recv.VoiceData):
        if user is None or getattr(user, "bot", False):
            return
        pcm = data.pcm
        if not pcm:
            return
        STATS["discord_pcm_packets"] += 1
        if user.id not in self._seen_users:
            self._seen_users.add(user.id)
            log.info("voice input: first packet received from %s (%d bytes)", user, len(pcm))
        self._mixer.feed(user.id, audioop.tomono(pcm, 2, 0.5, 0.5))

    def cleanup(self):
        self._seen_users.clear()


class VoiceSession:
    def __init__(self, cog: "Voice", guild: discord.Guild,
                 voice_client: voice_recv.VoiceRecvClient, text_channel):
        self.cog = cog
        self.guild = guild
        self.voice_client = voice_client
        self.channel_id = voice_client.channel.id if voice_client else None
        self._rejoining = False
        self._rejoin_times: list[float] = []
        self.text_channel = text_channel
        self.output = GeminiOutputSource()
        self.last_activity = time.monotonic()
        self._send_queue: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._resume_handle: str | None = None
        self._heard_text: list[str] = []
        self._said_text: list[str] = []
        self._health_task: asyncio.Task | None = None
        self._mix_task: asyncio.Task | None = None
        self.mixer = AudioMixer()
        self._held = bytearray()
        self._model_speaking = False
        self._last_model_audio = 0.0
        # クローン音声(TTS読み上げ)用。GEMINI_VOICE_ID未設定なら使わない
        self._tts_queue: asyncio.Queue[str] = asyncio.Queue()
        self._tts_text = ""
        self._tts_inflight = False
        self._tts_epoch = 0
        self._tts_task: asyncio.Task | None = None

    def start(self):
        self._loop = asyncio.get_running_loop()
        self._start_listening()
        self._start_playing()
        self._task = asyncio.create_task(self._run())
        self._mix_task = asyncio.create_task(self._mix_loop())
        self._health_task = asyncio.create_task(self._health_log())
        if GEMINI_VOICE_ID:
            self._tts_task = asyncio.create_task(self._tts_loop())

    def _start_listening(self):
        sink = GeminiInputSink(self.mixer)
        self.voice_client.listen(sink, after=self._on_listen_stopped)

    def _bot_speaking(self) -> bool:
        # 生成中(turn_completeまで)か、手元の再生バッファが残っている間はBotが話している。
        # turn_completeが来ないまま接続が切れても貯め込みっぱなしにならないよう、
        # 最後の応答音声から一定時間経ったら生成中とはみなさない。
        generating = self._model_speaking and time.monotonic() - self._last_model_audio < MODEL_SPEAKING_TIMEOUT
        # TTS読み上げ中は、Liveの生成が終わっていても読み上げ待ちの文が残っている間は話している
        synthesizing = self._tts_inflight or not self._tts_queue.empty()
        return generating or synthesizing or self.output.has_pending_frame()

    async def _mix_loop(self):
        # 20msごとに全話者をミックスした1フレームを取り出し、Geminiへの送信キューに積む。
        # Botが話している間は、他の人の声で返事が途中で止められないよう送らずに貯めておき、
        # 話し終わった直後にまとめて送る。
        next_t = time.monotonic()
        while True:
            next_t += FRAME_MS / 1000
            delay = next_t - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -0.2:
                next_t = time.monotonic()
            frame = self.mixer.pop_frame()
            speaking = self._bot_speaking()
            if frame is not None:
                self.last_activity = time.monotonic()
                STATS["mixed_frames"] += 1
                if speaking:
                    if len(self._held) < HOLD_MAX_BYTES:
                        self._held.extend(frame)
                        STATS["held_frames"] += 1
                    continue
            if not speaking and self._held:
                self._flush_held()
            if frame is not None:
                self._send_queue.put_nowait(frame)

    def _flush_held(self):
        log.info("voice input: sending %.1fs of speech held while the bot was talking",
                 len(self._held) / (GEMINI_IN_RATE * 2))
        STATS["held_flushes"] += 1
        # 1メッセージが大きくなりすぎないよう分割して積む
        for i in range(0, len(self._held), HOLD_FLUSH_CHUNK_BYTES):
            self._send_queue.put_nowait(bytes(self._held[i:i + HOLD_FLUSH_CHUNK_BYTES]))
        self._held.clear()

    def _start_playing(self):
        self.voice_client.play(self.output, after=self._on_play_stopped)

    # 受信(voice_recvのリーダー)も再生(discord.pyのAudioPlayer)も、内部で例外が起きると
    # スレッドごと終了し、以後は二度と動かない。どちらも止まれば通話は無反応になるため、
    # 停止を検知したら自動で立ち上げ直す。コールバックは各スレッドから呼ばれる。
    # rejoin()でvoice_clientが差し替わるため、実行中かどうかは常に現在の接続で判定する。
    def _on_listen_stopped(self, error: Exception | None):
        if self._closed or self._rejoining:
            return
        log.warning("voice listen: stopped unexpectedly (%r), restarting", error)
        asyncio.run_coroutine_threadsafe(
            self._restart("listen", lambda: self.voice_client.is_listening(), self._start_listening), self._loop
        )

    def _on_play_stopped(self, error: Exception | None):
        if self._closed or self._rejoining:
            return
        log.warning("voice play: stopped unexpectedly (%r), restarting", error)
        asyncio.run_coroutine_threadsafe(
            self._restart("play", lambda: self.voice_client.is_playing(), self._start_playing), self._loop
        )

    async def rejoin(self) -> bool:
        """Discordのボイス接続だけを張り直す。Geminiとの会話はそのまま続ける。

        ボイスWSが4006で切れた後、discord.pyの自動再接続がハンドシェイク後に止まったまま
        戻らないことがある(本番ログで確認)。その場合はこちらで切断して入り直す。
        """
        now = time.monotonic()
        self._rejoin_times = [t for t in self._rejoin_times if now - t < REJOIN_WINDOW_SECONDS]
        if len(self._rejoin_times) >= MAX_REJOINS_PER_WINDOW:
            log.warning("voice: rejoined %d times within %ds, giving up", len(self._rejoin_times), REJOIN_WINDOW_SECONDS)
            return False
        self._rejoin_times.append(now)
        channel = self.guild.get_channel(self.channel_id)
        if channel is None:
            return False
        self._rejoining = True
        try:
            old = self.voice_client
            try:
                old.stop()
                await old.disconnect(force=True)
            except Exception:
                log.debug("voice: error while dropping stale voice connection", exc_info=True)
            try:
                self.voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient)
            except Exception:
                log.exception("voice: failed to rejoin %s", channel)
                return False
            self._start_listening()
            self._start_playing()
            log.info("voice: rejoined %s", channel)
            return True
        finally:
            self._rejoining = False

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
                "interrupted=%d turns=%d mixed=%.1fs overlap=%.1fs held=%.1fs flushes=%d "
                "| playing=%s listening=%s connected=%s",
                HEALTH_LOG_INTERVAL, d["discord_pcm_packets"], d["dave_decrypted"], d["dave_drop_not_ready"],
                d["dave_drop_no_user"], d["dave_drop_error"], d["opus_corrupt"], d["sent_chunks"],
                d["stream_ends"], d["gemini_audio_bytes"] / (GEMINI_OUT_RATE * 2), d["played_frames"] * FRAME_MS / 1000,
                d["interrupted"], d["turns_completed"], d["mixed_frames"] * FRAME_MS / 1000,
                d["overlap_frames"] * FRAME_MS / 1000, d["held_frames"] * FRAME_MS / 1000, d["held_flushes"],
                self.voice_client.is_playing(), self.voice_client.is_listening(), self.voice_client.is_connected(),
            )

    def _live_config(self) -> types.LiveConnectConfig:
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=SYSTEM_INSTRUCTION,
            # 何を聞き取り何を話したかをログに出し、会話がズレたときに聞き間違いか判別できるようにする
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
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
                self._model_speaking = False  # 切断で途切れたターンのturn_completeは来ない
                async with gemini_client.aio.live.connect(model=LIVE_MODEL, config=self._live_config()) as session:
                    log.info("voice session: connected to %s for guild %s (resumed=%s voice=%s)",
                             LIVE_MODEL, self.guild.id, resuming,
                             f"{TTS_MODEL}:{GEMINI_VOICE_ID}" if GEMINI_VOICE_ID else VOICE_NAME)
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
        if sc.input_transcription and sc.input_transcription.text:
            self._heard_text.append(sc.input_transcription.text)
        if sc.output_transcription and sc.output_transcription.text:
            self._log_heard()
            self._said_text.append(sc.output_transcription.text)
            if GEMINI_VOICE_ID:
                self._tts_feed(sc.output_transcription.text)
        if sc.turn_complete:
            STATS["turns_completed"] += 1
            self._model_speaking = False
            self._log_heard()
            self._log_said("")
            if GEMINI_VOICE_ID:
                self._tts_feed("", flush=True)
        if sc.interrupted:
            STATS["interrupted"] += 1
            self._model_speaking = False
            self._tts_cancel()
            self.output.clear()
            self._log_said(" (interrupted)")
        if sc.model_turn:
            for part in sc.model_turn.parts:
                if part.inline_data and part.inline_data.data:
                    self._model_speaking = True
                    self._last_model_audio = time.monotonic()
                    if not GEMINI_VOICE_ID:  # クローン音声時はLiveの声を使わずTTSで読み直す
                        self.output.push(part.inline_data.data)
                else:
                    log.debug("voice recv: part with no inline_data: %s", part)

    def _tts_feed(self, text: str, flush: bool = False):
        # 文字起こしは細切れで届くため、文の区切りまで貯めてから1文ずつ読み上げに回す
        self._tts_text += text
        end = 0
        for m in TTS_SENTENCE_END.finditer(self._tts_text):
            self._tts_enqueue(m.group())
            end = m.end()
        self._tts_text = self._tts_text[end:]
        if flush:
            self._tts_enqueue(self._tts_text)
            self._tts_text = ""

    def _tts_enqueue(self, sentence: str):
        sentence = sentence.strip()
        if sentence:
            self._tts_queue.put_nowait(sentence)

    def _tts_cancel(self):
        self._tts_text = ""
        self._tts_epoch += 1  # 読み上げ中の文は、以降の音声を再生バッファに積まない
        while not self._tts_queue.empty():
            self._tts_queue.get_nowait()

    def say(self, text: str):
        """!say コマンド用。通話中の会話に割り込まず、順番が来たらクローン音声で読み上げる。"""
        self._tts_enqueue(text)

    async def _tts_loop(self):
        while True:
            sentence = await self._tts_queue.get()
            epoch = self._tts_epoch
            self._tts_inflight = True
            started = time.monotonic()
            try:
                async for pcm in tts_stream(sentence):
                    if epoch != self._tts_epoch:
                        break
                    self.output.push(pcm)
                STATS["tts_sentences"] += 1
                log.debug("voice tts: %.2fs for %r", time.monotonic() - started, sentence)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                STATS["tts_errors"] += 1
                log.warning("voice tts: failed to synthesize %r: %r", sentence, e)
            finally:
                self._tts_inflight = False

    def _log_heard(self):
        if self._heard_text:
            log.info("voice transcript: heard %r", "".join(self._heard_text))
            self._heard_text.clear()

    def _log_said(self, suffix: str):
        if self._said_text:
            log.info("voice transcript: said %r%s", "".join(self._said_text), suffix)
            self._said_text.clear()

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
        for task in (self._health_task, self._mix_task, self._tts_task):
            if task:
                task.cancel()
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

    @commands.command(name="say")
    @commands.is_owner()
    async def say_cmd(self, ctx, *, text: str = ""):
        # 実在の人の声なので、なりすましや悪用を防ぐためオーナー限定にしている
        text = text.strip()
        if not GEMINI_API_KEY or not GEMINI_VOICE_ID:
            await ctx.send("❌ クローン音声が設定されていないよ！(GEMINI_VOICE_ID)")
            return
        if not text:
            await ctx.send("❌ 使い方: `!say 読み上げたい文章`")
            return
        if len(text) > SAY_MAX_CHARS:
            await ctx.send(f"❌ 長すぎるよ！{SAY_MAX_CHARS}文字以内にしてね（今{len(text)}文字）")
            return

        session = self.sessions.get(ctx.guild.id) if ctx.guild else None
        if session:
            session.say(text)
            await ctx.message.add_reaction("🔊")
            return

        try:
            async with ctx.typing():
                pcm = b"".join([chunk async for chunk in tts_stream(text)])
        except Exception as e:
            log.warning("say: failed to synthesize %r: %r", text, e)
            await ctx.send(f"❌ 読み上げに失敗したよ: {e}")
            return
        if not pcm:
            await ctx.send("❌ 音声が返ってこなかったよ。文章を変えて試してね")
            return
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(GEMINI_OUT_RATE)
            w.writeframes(pcm)
        buf.seek(0)
        await ctx.send(file=discord.File(buf, filename="say.wav"))

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
                if session._closed or session._rejoining or session.voice_client.is_connected():
                    return
                if self.sessions.get(member.guild.id) is not session:
                    return
                log.info("voice: auto-reconnect did not recover, rejoining the channel ourselves")
                if not await session.rejoin():
                    await self.leave(member.guild.id, "🔌 通話の接続が切れて戻れなかったよ。もう一回 `!voice join` してね")
            return

        session = self.sessions.get(member.guild.id)
        if session is None:
            return
        channel = session.voice_client.channel
        if channel and all(m.bot for m in channel.members):
            await self.leave(member.guild.id, "🚪 誰もいなくなったから通話から抜けるね")


async def setup(bot):
    await bot.add_cog(Voice(bot))
