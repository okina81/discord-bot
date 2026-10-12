"""Discordのチャット履歴から本人の話し方を分析し、音声通話Botの指示文に使える形にまとめるスクリプト。

使い方:
    python tools/analyze_style.py
    python tools/analyze_style.py --user-id 512510702129512469 --limit 20000

- .env の DISCORD_TOKEN と GEMINI_API_KEY を使う。Botが入っている全サーバーのテキストチャンネルを読む。
- 既定では cogs/voice.py の NICKNAMES で「いまきた」になっている人の発言を集める。
- 集めた発言は style_data/messages.txt、分析結果は style_data/style_report.md に保存する。
  style_data/ は本人の発言そのものを含むため、gitには入れない(.gitignore 済み)。
- 分析結果を確認し、本人らしいと思う部分を cogs/voice.py の PERSONA / SYSTEM_INSTRUCTION に反映する。
"""
import argparse
import asyncio
import collections
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord  # noqa: E402
from config import TOKEN, gemini_client  # noqa: E402
from cogs.voice import NICKNAMES  # noqa: E402

OUT_DIR = "style_data"
ANALYZE_MODEL = "gemini-2.5-flash-lite"
MAX_PROMPT_CHARS = 30000
_URL = re.compile(r"https?://\S+")
_MENTION = re.compile(r"<[@#:][^>]+>")


def clean(text: str) -> str:
    """URLやメンションなど、話し方と関係ない部分を落とす。"""
    return re.sub(r"\s+", " ", _MENTION.sub("", _URL.sub("", text))).strip()


async def collect(user_ids: set[int], limit: int) -> list[str]:
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    messages: list[tuple] = []

    @client.event
    async def on_ready():
        try:
            for guild in client.guilds:
                for ch in guild.text_channels:
                    if not ch.permissions_for(guild.me).read_message_history:
                        continue
                    count = 0
                    try:
                        async for msg in ch.history(limit=limit):
                            if msg.author.id in user_ids and (text := clean(msg.content)):
                                messages.append((msg.created_at, text))
                                count += 1
                    except discord.Forbidden:
                        continue
                    print(f"  #{ch.name}: {count}件")
        finally:
            await client.close()

    await client.start(TOKEN)
    messages.sort()
    return [text for _, text in messages]


def stats(messages: list[str]) -> str:
    lengths = sorted(len(m) for m in messages)
    endings = collections.Counter(re.sub(r"[\s。、!！?？]+$", "", m)[-2:] for m in messages if len(m) >= 2)
    laughs = sum(1 for m in messages if re.search(r"[wｗ草]$|笑", m))
    lines = [
        f"- 発言数: {len(messages)}",
        f"- 1発言の長さ: 中央値 {lengths[len(lengths) // 2]}文字",
        f"- 笑い(w・草・笑)を含む発言: {laughs * 100 // len(messages)}%",
        "- よく使う語尾(末尾2文字): " + "、".join(f"「{e}」{n}回" for e, n in endings.most_common(15)),
    ]
    return "\n".join(lines)


def analyze(messages: list[str]) -> str:
    # 新しい発言を優先して、プロンプトに収まる分だけ渡す
    sample, size = [], 0
    for m in reversed(messages):
        if size + len(m) > MAX_PROMPT_CHARS:
            break
        sample.append(m)
        size += len(m) + 1
    sample.reverse()
    prompt = (
        "以下は、ある20代の男性(神戸出身)がDiscordのチャットに書いた発言です。"
        "この人になりきって音声通話で話すAIへの指示を作るため、話し方の特徴を分析してください。\n\n"
        "次の見出しごとに、箇条書きで具体的に書いてください。実際の発言から言い回しを引用してください。\n"
        "## 語尾・方言\n## 口癖・よく使う言葉\n## 相づち・リアクション\n## 笑い方・ツッコミ方\n"
        "## テンション・ノリ\n## 返事の長さ・会話の進め方\n## 使わない言葉遣い(標準語・敬語など)\n"
        "## この人らしい発言の例(実際の発言から10個)\n\n"
        "発言:\n" + "\n".join(sample)
    )
    response = gemini_client.models.generate_content(model=ANALYZE_MODEL, contents=prompt)
    return response.text.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user-id", type=int, action="append", help="分析する人のユーザーID(複数可)")
    parser.add_argument("--limit", type=int, default=10000, help="1チャンネルあたりに読むメッセージ数の上限")
    args = parser.parse_args()

    if not TOKEN or gemini_client is None:
        sys.exit("DISCORD_TOKEN と GEMINI_API_KEY を .env に設定してください")
    user_ids = set(args.user_id or (uid for uid, name in NICKNAMES.items() if name == "いまきた"))

    print("チャット履歴を集めています...")
    messages = asyncio.run(collect(user_ids, args.limit))
    if len(messages) < 50:
        sys.exit(f"発言が{len(messages)}件しか見つかりませんでした。分析には50件以上必要です。")
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "messages.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(messages))

    print(f"{len(messages)}件の発言を分析しています...")
    report = f"# 話し方の分析\n\n## 集計\n{stats(messages)}\n\n{analyze(messages)}\n"
    path = os.path.join(OUT_DIR, "style_report.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"分析結果を {path} に保存しました。")


if __name__ == "__main__":
    main()
