"""録音した音声から、音声通話Bot用のクローン音声をGeminiに登録するスクリプト。

使い方:
    python tools/create_voice.py --consent voice_samples/consent.m4a voice_samples/01.m4a voice_samples/02.m4a ...
    python tools/create_voice.py --consent voice_samples/consent.m4a   (サンプル省略時は同意文の録音を声の見本に使う)

- サンプル音声は複数ファイル指定でき、先頭から順に合計30秒以内になるまでつなげて使う(10秒以上推奨)。
- --consent には、同じ本人が同意文を読み上げた録音を指定する(Gemini APIの必須要件)。
    私はこの音声の所有者であり、Googleがこの音声を使用して音声合成モデルを作成することを承認します。
  Google側で同意文とサンプルの話者が同一か判定され、別人と判定されると登録できない。
  別の日や別のマイクで録ったサンプルだと同一人物でも弾かれることがあったため、その場合はサンプルを省略する。
- 形式はffmpegが読めるもの(wav/m4a/mp3など)なら何でもよい。24kHz mono 16bit WAVに変換して送る。
- 登録に成功すると声のIDが表示される。サーバーの環境変数 GEMINI_VOICE_ID に設定すると通話Botがその声で話す。
- 確認用に voice_samples/out/tts_test.wav へ、通話Botと同じTTSモデルでその声を使った試し聞き音声を保存する。
"""
import argparse
import base64
import io
import os
import subprocess
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.genai import types  # noqa: E402
from config import gemini_client  # noqa: E402
from cogs.voice import TTS_MODEL  # noqa: E402

RATE = 24000
MAX_SAMPLE_SECONDS = 30
MIN_SAMPLE_SECONDS = 10
GAP = b"\x00" * int(RATE * 2 * 0.3)
OUT_DIR = os.path.join("voice_samples", "out")
TEST_TEXT = "どうも、今北ボットです! 今日もモンハン一狩り行こうや。オムライス食べてから、な!"


def to_pcm(path: str) -> bytes:
    """ffmpegで24kHz mono 16bit PCMに変換し、前後の無音を落とす。"""
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(RATE), "-f", "s16le",
         "-af", "silenceremove=start_periods=1:start_threshold=-45dB,areverse,"
                "silenceremove=start_periods=1:start_threshold=-45dB,areverse",
         "-"],
        capture_output=True, check=True,
    )
    return result.stdout


def to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def seconds(pcm: bytes) -> float:
    return len(pcm) / (RATE * 2)


def build_sample(paths: list[str]) -> bytes:
    sample = b""
    for path in paths:
        pcm = to_pcm(path)
        joined = sample + GAP + pcm if sample else pcm
        if seconds(joined) > MAX_SAMPLE_SECONDS:
            if not sample:  # 1ファイル目から30秒を超える場合は切り詰める
                sample = pcm[:RATE * 2 * MAX_SAMPLE_SECONDS]
            break
        sample = joined
        print(f"  + {path} ({seconds(pcm):.1f}s)")
    return sample


def save(name: str, pcm: bytes):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, name)
    with open(path, "wb") as f:
        f.write(to_wav(pcm))
    print(f"  saved {path} ({seconds(pcm):.1f}s)")


def test_tts(voice_id: str):
    response = gemini_client.models.generate_content(
        model=TTS_MODEL,
        contents=TEST_TEXT,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(voice=voice_id)),
        ),
    )
    save("tts_test.wav", response.candidates[0].content.parts[0].inline_data.data)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("samples", nargs="*", help="声のサンプル音声(複数可、先頭から30秒以内を使用。省略時は同意文の録音を使う)")
    parser.add_argument("--consent", required=True, help="同意文を読み上げた音声")
    parser.add_argument("--name", default="今北ボット", help="登録する声の表示名")
    parser.add_argument("--check", action="store_true", help="変換と長さの確認だけ行い、登録はしない")
    args = parser.parse_args()

    consent = to_pcm(args.consent)
    if args.samples:
        print("サンプル音声を変換中...")
        sample = build_sample(args.samples)
        if seconds(sample) < MIN_SAMPLE_SECONDS:
            sys.exit(f"サンプルが {seconds(sample):.1f}秒しかありません。{MIN_SAMPLE_SECONDS}秒以上になるようファイルを追加してください。")
    else:
        print("サンプル省略のため、同意文の録音を声の見本に使います。")
        sample = consent
    print(f"サンプル {seconds(sample):.1f}秒 / 同意音声 {seconds(consent):.1f}秒")
    save("sample_used.wav", sample)
    if args.check:
        print("--check のため登録はせずに終了します。sample_used.wav を聞いて問題なければ --check を外して実行してください。")
        return

    if gemini_client is None:
        sys.exit("GEMINI_API_KEY が設定されていません (.env を確認してください)")
    print("Geminiに声を登録中...")
    voice = gemini_client.voices.create(
        store=True,
        voice={
            "type": "replicated",
            "model": TTS_MODEL,
            "display_name": args.name,
            "language_code": "ja-JP",
            "replicated": {
                "source_audio": {"mime_type": "audio/wav", "data": base64.b64encode(to_wav(sample)).decode()},
                "consent_audio": {"mime_type": "audio/wav", "data": base64.b64encode(to_wav(consent)).decode()},
            },
        },
    )
    print(f"登録しました: id={voice.id} (有効期限 {voice.expire_time})")

    print("試し聞き用の音声を作成中...")
    test_tts(voice.id)

    print()
    print(f"サーバーの環境変数に GEMINI_VOICE_ID={voice.id} を設定すると、通話Botがこの声で話します。")
    print(f"{OUT_DIR}/tts_test.wav を聞いて、クローンした声になっているか確認してください。")


if __name__ == "__main__":
    main()
