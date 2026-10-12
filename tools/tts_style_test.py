"""クローン音声の読み上げに付ける演技指示(cogs/voice.py の TTS_STYLE)を試し聞きで選ぶスクリプト。

使い方:
    python tools/tts_style_test.py
    python tools/tts_style_test.py --style "眠そうに、ぼそぼそと" --style "早口でまくしたてるように"

- .env の GEMINI_API_KEY と GEMINI_VOICE_ID を使う。
- 指示なしと、各指示で同じ文を読み上げ、voice_samples/out/style/ に保存する。
  ファイル名の番号は、最後に表示される一覧の番号と対応する。
- 本人に近く、しかも指示の文まで読み上げていないものを選び、その指示を TTS_STYLE に入れる。
- TTSは1分あたりのリクエスト数に上限がある(無料枠は10回)。指示を増やしすぎると途中で429になる。
"""
import argparse
import io
import os
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.genai import types  # noqa: E402
from config import GEMINI_VOICE_ID, gemini_client  # noqa: E402
from cogs.voice import GEMINI_OUT_RATE, TTS_MODEL, tts_prompt  # noqa: E402

OUT_DIR = os.path.join("voice_samples", "out", "style")
TEST_TEXT = "いやそれはないやろ。昨日パチンコ行ってんけど、もうボロ負けやったわ。ほんで今日はモンハンしよんねん。"
DEFAULT_STYLES = [
    "神戸弁を話す20代の男性が、友達と通話でだらだら喋るように、自然な抑揚で読んで",
    "神戸弁を話す20代の男性が、友達と通話で笑いながら、少し早口で読んで",
]


def synthesize(text: str) -> bytes:
    response = gemini_client.models.generate_content(
        model=TTS_MODEL,
        contents=text,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(voice=GEMINI_VOICE_ID)),
        ),
    )
    return response.candidates[0].content.parts[0].inline_data.data


def save(path: str, pcm: bytes):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(GEMINI_OUT_RATE)
        w.writeframes(pcm)
    with open(path, "wb") as f:
        f.write(buf.getvalue())


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--style", action="append", help="試す演技指示(複数可。省略時は既定の候補を試す)")
    parser.add_argument("--text", default=TEST_TEXT, help="読み上げる文")
    args = parser.parse_args()

    if gemini_client is None or not GEMINI_VOICE_ID:
        sys.exit("GEMINI_API_KEY と GEMINI_VOICE_ID を .env に設定してください")
    os.makedirs(OUT_DIR, exist_ok=True)

    styles = [""] + (args.style or DEFAULT_STYLES)
    for i, style in enumerate(styles):
        path = os.path.join(OUT_DIR, f"{i:02d}.wav")
        save(path, synthesize(tts_prompt(args.text, style)))
        print(f"{i:02d}: {style or '(指示なし)'}  -> {path}")
    print()
    print("聞き比べて、本人に近いものの指示を cogs/voice.py の TTS_STYLE に入れてください。")
    print("指示の文まで読み上げているものは使えません。")


if __name__ == "__main__":
    main()
