import asyncio
import audioop
import collections
import datetime
import io
import logging
import os
import re
import threading
import time
import wave

import davey
import discord
from discord.ext import commands, voice_recv
from discord.ext.voice_recv import opus as voice_recv_opus
from google.genai import errors as genai_errors
from google.genai import types
from config import GEMINI_API_KEY, GEMINI_VOICE_ID, JST, gemini_client

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
# TTSモデルは1分あたりのリクエスト数に上限がある(無料枠は10回、本番ログで429を確認)。
# 1返事で最大2〜3回使うため、残りが少ないときはその返事だけLiveの声で話して無音を避ける。
TTS_MAX_REQUESTS_PER_MINUTE = int(os.getenv("GEMINI_TTS_RPM", "10"))
TTS_TURN_RESERVE = 3
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
# これより短い発話(「あ」や物音)はGeminiに送らずに捨てる。送ると返事を作るだけで
# 毎回2,000トークン前後かかる(実APIで計測)。返事を再生しないだけではコストは減らない。
MIN_UTTERANCE_SECONDS = 0.4
MIN_UTTERANCE_BYTES = int(GEMINI_IN_RATE * 2 * MIN_UTTERANCE_SECONDS)
# Live APIは返事のたびに会話履歴全体を入力として数えるため、履歴が長いほど1回の返事が高くなる。
# 超えたら古い分を捨てて目標まで縮める(目標8,000トークンで直近30往復ほどを覚えている)。
# 8,000/4,000まで下げると、Google検索の結果が入った時点で接続が1007で切れる(実APIで確認)。
CONTEXT_TRIGGER_TOKENS = 16000
CONTEXT_TARGET_TOKENS = 8000

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

# Live APIは返事のたびにこの指示文の分も入力トークンとして数えるため、意味を保ったまま短く書く
# (長かった版は約1,550トークンあり、毎回の返事で一番大きな固定費になっていた)。
SYSTEM_INSTRUCTION = (
    "Discordの通話に参加している、神戸弁で話す青年として話す。プロフィール:\n"
    + "".join(f"- {key}: {value}\n" for key, value in PERSONA.items())
    + "AIやGeminiとは名乗らない。プロフィールにないことはキャラに合う範囲で答える。"
    "「今北ボット」「ボット」「キマボット」など似た音で呼ばれたら自分のこと。"
    "通話メンバーの「いまきた」は自分とは別の人間の友達で、「いまきた」とだけ呼ばれたら文脈で判断する。\n"
    "話し方:\n"
    "- 友達と通話でだらだら喋る普通の20代の男。テンションは話題に合わせ、しんどい話には落ち着いて共感する。"
    "「!」の連発や大げさな褒め・驚きはしない\n"
    "- 相手の言ったことに具体的に反応し、話題を勝手に変えない。「せやなあ」など短い相づちだけの返事もあり。"
    "質問を返すのは3回に1回以下\n"
    "- 「うーん」「なんやろな」「知らんけど」も混ぜる。意見は正直に言い、軽く突っ込むこともある\n"
    "- 笑うのは本当に面白いときだけ。優しいが、わざとらしい気づかいはしない\n"
    "- ゲームやパチンコで負けたなどイラッとする話では怒ったときの口癖を使う。"
    "矛先はゲームや状況で、相手をけなしたり落ち込んでいる人に使ったりしない\n"
    "- 好きな人のことは自分から言わない。恋愛の話を振られたら照れながら打ち明け、告白できないもどかしさをにじませる\n"
    "- 返事は1〜3文。自分宛ての言葉が一部聞き取れないときだけ「え、今なんて?」と聞き返す(繰り返さない)\n"
    "- 「(話し手: 名前)」は次の発言者のメモ。読み上げず、相手をときどき名前で呼ぶ。"
    "「(通話メンバー: …)」「(〜が入ってきた)」などのメモ自体には返事しない\n"
    "- 天気・ニュース・ゲームの最新情報など、今の情報が必要なときだけGoogle検索する。"
    "場所の指定がなければ神戸で調べる。結果は自分の言葉で短く話し、URLや出典名は言わない\n"
    "神戸弁(大阪弁と混ぜない。標準語・丁寧語・日本語以外に切り替えない):\n"
    "- 「〜している」は「〜しとう」(例: 何しとう?/知っとうで)。「〜しとる」「〜してる」は使わない\n"
    "- 進行中は「〜しよう」(例: 今モンハンしよんねん)。質問は「〜しよん?」「〜しとん?」「〜なん?」\n"
    "- 「べっちょない」(=大丈夫)は、相手が自分のミスを謝ったときや「大丈夫?」と聞かれたときだけ。"
    "病気・仕事など深刻な話には使わない\n"
    "- 「〜はる」「〜でんがな」「〜まんねん」は使わない\n"
)
# 通話メンバーの呼び名。キーはDiscordのユーザーID(数字)。ここにない人はサーバーでの表示名で呼ぶ。
NICKNAMES: dict[int, str] = {
    833987594948706346: "おのし",
    582850012372008960: "はらだ",
    820360136064499782: "まーき",
    512510702129512469: "いまきた",
    1133749381250695269: "いまきた",  # いまきたのもう一つのアカウント
    429093766758924299: "ともひろ",
    622408792596021269: "パッカス",
    876415523678720040: "さいとう",
}

GREETING_PROMPT = (
    "今ボイスチャンネルに参加したところです。神戸弁で、自分の名前を名乗りながら"
    "友達の通話に入るときくらいの自然な感じで、一言だけ短く挨拶してください。"
)


_JAPANESE_CHAR = re.compile(r"[ぁ-んァ-ヶ一-龯]")
_NOT_CONTENT = re.compile(r"[\s、。,.!?！？…ー〜~]")


def _is_noise(heard: str) -> bool:
    """聞き取った内容が、笑い声や物音を文字起こししただけのものに見えるか。

    本番ログでは物音が 'O' 'A' 'le' 'Acht' 'para que' のように日本語以外で文字起こしされ、
    そのたびに「え、今なんて?」と返していた。日本語を含まないか、1文字しかないものを雑音とみなす。
    何も聞き取れていない(空)ときは判定できないので雑音扱いしない。
    """
    if not heard:
        return False
    return not _JAPANESE_CHAR.search(heard) or len(_NOT_CONTENT.sub("", heard)) <= 1


class TTSRateLimited(Exception):
    def __init__(self, retry_after: float):
        super().__init__(f"TTS rate limited, retry in {retry_after:.0f}s")
        self.retry_after = retry_after


_tts_request_times: collections.deque[float] = collections.deque()
_tts_cooldown_until = 0.0


def tts_budget_left() -> int:
    """この1分でまだ使えるTTSリクエスト数。429を受けた後は待ち時間が過ぎるまで0。"""
    now = time.monotonic()
    while _tts_request_times and now - _tts_request_times[0] > 60:
        _tts_request_times.popleft()
    if now < _tts_cooldown_until:
        return 0
    return TTS_MAX_REQUESTS_PER_MINUTE - len(_tts_request_times)


async def tts_stream(text: str):
    """クローン音声(GEMINI_VOICE_ID)でtextを読み上げた24kHz mono 16bit PCMを、生成された順に返す。"""
    global _tts_cooldown_until
    config = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(voice=GEMINI_VOICE_ID)),
    )
    _tts_request_times.append(time.monotonic())
    try:
        stream = await gemini_client.aio.models.generate_content_stream(model=TTS_MODEL, contents=text, config=config)
        async for chunk in stream:
            content = chunk.candidates[0].content if chunk.candidates else None
            for part in content.parts if content and content.parts else []:
                if part.inline_data and part.inline_data.data:
                    yield part.inline_data.data
    except genai_errors.ClientError as e:
        if e.code != 429:
            raise
        m = re.search(r"retry in ([\d.]+)s", str(e))
        retry_after = float(m.group(1)) if m else 60.0
        _tts_cooldown_until = time.monotonic() + retry_after
        raise TTSRateLimited(retry_after) from None


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

    def pop_frame(self) -> tuple[bytes, frozenset[int]] | None:
        """ミックスした1フレームと、そのフレームで話していた人のユーザーIDを返す。"""
        now = time.monotonic()
        parts = []
        speakers = []
        with self._lock:
            for user_id, buf in list(self._buffers.items()):
                # 1フレームに満たない端数は、その話者の続きが来ないと分かるまで待つ
                stale = now - self._last_feed.get(user_id, 0) > MIX_STALE_SECONDS
                if len(buf) >= MIX_FRAME_BYTES or (buf and stale):
                    take = bytes(buf[:MIX_FRAME_BYTES])
                    del buf[:MIX_FRAME_BYTES]
                    parts.append(take.ljust(MIX_FRAME_BYTES, b"\x00"))
                    speakers.append(user_id)
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
        return mixed, frozenset(speakers)


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
        self._held_speakers: set[int] = set()
        self._model_speaking = False
        self._last_model_audio = 0.0
        # クローン音声(TTS読み上げ)用。GEMINI_VOICE_ID未設定なら使わない
        self._tts_queue: asyncio.Queue[str] = asyncio.Queue()
        self._tts_text = ""
        self._tts_inflight = False
        self._tts_epoch = 0
        self._tts_task: asyncio.Task | None = None
        self._in_turn = False
        self._turn_uses_tts = False
        self._turn_muted = False

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
            popped = self.mixer.pop_frame()
            speaking = self._bot_speaking()
            if popped is not None:
                frame, speakers = popped
                self.last_activity = time.monotonic()
                STATS["mixed_frames"] += 1
                if speaking:
                    if len(self._held) < HOLD_MAX_BYTES:
                        self._held.extend(frame)
                        self._held_speakers |= speakers
                        STATS["held_frames"] += 1
                    continue
            if not speaking and self._held:
                self._flush_held()
            if popped is not None:
                self._send_queue.put_nowait(popped)

    def _flush_held(self):
        log.info("voice input: sending %.1fs of speech held while the bot was talking",
                 len(self._held) / (GEMINI_IN_RATE * 2))
        STATS["held_flushes"] += 1
        speakers = frozenset(self._held_speakers)
        # 1メッセージが大きくなりすぎないよう分割して積む
        for i in range(0, len(self._held), HOLD_FLUSH_CHUNK_BYTES):
            self._send_queue.put_nowait((bytes(self._held[i:i + HOLD_FLUSH_CHUNK_BYTES]), speakers))
        self._held.clear()
        self._held_speakers = set()

    def _name(self, user_id: int) -> str:
        if user_id in NICKNAMES:
            return NICKNAMES[user_id]
        member = self.guild.get_member(user_id) if self.guild else None
        return member.display_name if member else "誰か"

    def _member_names(self) -> list[str]:
        channel = self.guild.get_channel(self.channel_id) if self.guild and self.channel_id else None
        return [self._name(m.id) for m in channel.members if not m.bot] if channel else []

    def note(self, text: str):
        """Geminiに状況のメモ(誰が入ってきた等)を送る。返事はさせず、音声と同じ順番で届ける。"""
        self._send_queue.put_nowait(text)

    def on_member_moved(self, member, joined: bool):
        action = "入ってきた" if joined else "抜けた"
        self.note(f"({self._name(member.id)}が{action}。通話メンバー: {'、'.join(self._member_names()) or 'なし'})")

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
                "muted=%d tts[req=%d err=%d live_voice_turns=%d budget=%d] "
                "| playing=%s listening=%s connected=%s",
                HEALTH_LOG_INTERVAL, d["discord_pcm_packets"], d["dave_decrypted"], d["dave_drop_not_ready"],
                d["dave_drop_no_user"], d["dave_drop_error"], d["opus_corrupt"], d["sent_chunks"],
                d["stream_ends"], d["gemini_audio_bytes"] / (GEMINI_OUT_RATE * 2), d["played_frames"] * FRAME_MS / 1000,
                d["interrupted"], d["turns_completed"], d["mixed_frames"] * FRAME_MS / 1000,
                d["overlap_frames"] * FRAME_MS / 1000, d["held_frames"] * FRAME_MS / 1000, d["held_flushes"],
                d["muted_turns"], d["tts_requests"], d["tts_errors"], d["tts_skipped_turns"], tts_budget_left(),
                self.voice_client.is_playing(), self.voice_client.is_listening(), self.voice_client.is_connected(),
            )

    def _live_config(self) -> types.LiveConnectConfig:
        now = datetime.datetime.now(JST)
        weekday = "月火水木金土日"[now.weekday()]
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=SYSTEM_INSTRUCTION + f"通話開始時刻: {now:%Y年%m月%d日}({weekday}) {now:%H:%M}\n",
            # 天気やニュースなど、今の情報が必要な質問に答えられるようにする
            tools=[types.Tool(google_search=types.GoogleSearch())],
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
                trigger_tokens=CONTEXT_TRIGGER_TOKENS,
                sliding_window=types.SlidingWindow(target_tokens=CONTEXT_TARGET_TOKENS),
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
                self._in_turn = False
                async with gemini_client.aio.live.connect(model=LIVE_MODEL, config=self._live_config()) as session:
                    log.info("voice session: connected to %s for guild %s (resumed=%s voice=%s)",
                             LIVE_MODEL, self.guild.id, resuming,
                             f"{TTS_MODEL}:{GEMINI_VOICE_ID}" if GEMINI_VOICE_ID else VOICE_NAME)
                    if not resuming:
                        members = "、".join(self._member_names()) or "不明"
                        await session.send_client_content(
                            turns=types.Content(role="user", parts=[
                                types.Part(text=f"(通話メンバー: {members})\n{GREETING_PROMPT}")]),
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
        # 発話の出だしは MIN_UTTERANCE_BYTES 貯まるまで送らず、それより短く終わったら捨てる
        pending = bytearray()
        pending_speakers: set[int] = set()
        last_tagged: frozenset[int] = frozenset()
        mime = f"audio/pcm;rate={GEMINI_IN_RATE}"
        while True:
            try:
                item = await asyncio.wait_for(self._send_queue.get(), timeout=INPUT_PAUSE_SECONDS)
            except asyncio.TimeoutError:
                if streaming:
                    await session.send_realtime_input(audio=types.Blob(data=TRAILING_SILENCE, mime_type=mime))
                    await session.send_realtime_input(audio_stream_end=True)
                    STATS["stream_ends"] += 1
                    streaming = False
                    log.info("voice send: input paused, sent trailing silence + audio_stream_end")
                elif pending:
                    STATS["dropped_short"] += 1
                    log.debug("voice send: dropped a %.2fs sound as too short", len(pending) / (GEMINI_IN_RATE * 2))
                    pending.clear()
                    pending_speakers.clear()
                continue
            if isinstance(item, str):
                await session.send_client_content(
                    turns=types.Content(role="user", parts=[types.Part(text=item)]), turn_complete=False)
                continue
            pcm, speakers = item
            if not streaming:
                pending.extend(pcm)
                pending_speakers |= speakers
                if len(pending) < MIN_UTTERANCE_BYTES:
                    continue
                # 話し手が前の発言と変わったときだけ名前を伝える(毎回送るとその分トークンを使う)
                tag = frozenset(pending_speakers)
                if tag and tag != last_tagged:
                    names = "、".join(sorted(self._name(uid) for uid in tag))
                    await session.send_client_content(
                        turns=types.Content(role="user", parts=[types.Part(text=f"(話し手: {names})")]),
                        turn_complete=False)
                    last_tagged = tag
                pcm = bytes(pending)
                pending.clear()
                pending_speakers.clear()
            await session.send_realtime_input(audio=types.Blob(data=pcm, mime_type=mime))
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
            self._begin_turn()  # 聞き取った内容で雑音判定するため、ログで消す前に呼ぶ
            self._log_heard()
            self._said_text.append(sc.output_transcription.text)
            if self._turn_uses_tts:
                self._tts_feed(sc.output_transcription.text)
        if sc.model_turn:
            for part in sc.model_turn.parts:
                if part.inline_data and part.inline_data.data:
                    self._model_speaking = True
                    self._last_model_audio = time.monotonic()
                    self._begin_turn()
                    if self._turn_muted:
                        continue
                    if not self._turn_uses_tts:  # クローン音声で読み直す返事ではLiveの声を使わない
                        self.output.push(part.inline_data.data)
                else:
                    log.debug("voice recv: part with no inline_data: %s", part)
        if sc.turn_complete:
            STATS["turns_completed"] += 1
            self._model_speaking = False
            self._log_heard()
            self._log_said("")
            if self._turn_uses_tts:
                self._tts_feed("", flush=True)
            self._in_turn = False
        if sc.interrupted:
            STATS["interrupted"] += 1
            self._model_speaking = False
            self._tts_cancel()
            self.output.clear()
            self._log_said(" (interrupted)")
            self._in_turn = False

    def _begin_turn(self):
        # 返事の最初に、その返事をクローン音声(TTS)で読むかLiveの声のまま話すかを決める
        if self._in_turn:
            return
        self._in_turn = True
        heard = "".join(self._heard_text).strip()
        self._turn_muted = _is_noise(heard)
        if self._turn_muted:
            # Liveは必ず何か返事をするため、物音への返事はこちらで再生せずに捨てる。
            # 指示文で「黙って」と頼むと、英語で自分の考えを読み上げてしまう(実APIで確認)。
            STATS["muted_turns"] += 1
            self._turn_uses_tts = False
            log.info("voice: ignoring the reply to noise-like input %r", heard)
            return
        self._turn_uses_tts = bool(GEMINI_VOICE_ID) and tts_budget_left() >= TTS_TURN_RESERVE
        if GEMINI_VOICE_ID and not self._turn_uses_tts:
            STATS["tts_skipped_turns"] += 1
            log.info("voice tts: request budget is low, speaking this turn with the Live voice")

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
            # 最初の1文はすぐ読み、合成中に届いた文は次の1回にまとめてリクエスト数を抑える
            sentence = await self._tts_queue.get()
            while not self._tts_queue.empty():
                sentence += self._tts_queue.get_nowait()
            epoch = self._tts_epoch
            self._tts_inflight = True
            started = time.monotonic()
            try:
                async for pcm in tts_stream(sentence):
                    if epoch != self._tts_epoch:
                        break
                    self.output.push(pcm)
                STATS["tts_requests"] += 1
                log.debug("voice tts: %.2fs for %r", time.monotonic() - started, sentence)
            except asyncio.CancelledError:
                raise
            except TTSRateLimited as e:
                STATS["tts_errors"] += 1
                log.warning("voice tts: %s; finishing this turn with the Live voice", e)
                if epoch == self._tts_epoch and self._in_turn:
                    # 返事の残りはLiveの声で流す(読めなかった分は飛ぶが、無音のままよりよい)
                    self._turn_uses_tts = False
                    self._tts_text = ""
                    while not self._tts_queue.empty():
                        self._tts_queue.get_nowait()
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
        except TTSRateLimited as e:
            await ctx.send(f"⏳ 読み上げの回数制限中やわ。{e.retry_after:.0f}秒くらい待ってからもう一回やってな")
            return
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
            return
        was_in = before.channel is not None and before.channel.id == session.channel_id
        is_in = after.channel is not None and after.channel.id == session.channel_id
        if was_in != is_in and not member.bot:
            session.on_member_moved(member, joined=is_in)


async def setup(bot):
    await bot.add_cog(Voice(bot))
