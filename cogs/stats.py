import re
import json
from pathlib import Path
import discord
from discord.ext import commands

STATS_FILE = Path("stats.json")

UNICODE_EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001FAFF]|[\U00002600-\U000027BF]|[\U0001F000-\U0001F02F]"
)
CUSTOM_EMOJI_RE = re.compile(r"<a?:(\w+):\d+>")


def load_stats() -> dict:
    if STATS_FILE.exists():
        with open(STATS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"messages": {}, "emojis": {}}


def save_stats(data: dict) -> None:
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def build_stats_embed(guild: discord.Guild, data: dict) -> discord.Embed:
    embed = discord.Embed(title="📊 サーバー統計", color=discord.Color.purple())
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    messages = data.get("messages", {})
    if messages:
        top_users = sorted(messages.items(), key=lambda x: x[1], reverse=True)[:5]
        ranking = ""
        for i, (uid, count) in enumerate(top_users):
            member = guild.get_member(int(uid))
            name = member.display_name if member else "退出済みユーザー"
            ranking += f"{medals[i]} {name}　**{count}**回\n"
        embed.add_field(name="💬 発言数ランキング", value=ranking, inline=False)
    else:
        embed.add_field(name="💬 発言数ランキング", value="まだデータなし", inline=False)
    emojis = data.get("emojis", {})
    if emojis:
        top_emojis = sorted(emojis.items(), key=lambda x: x[1], reverse=True)[:5]
        emoji_text = "\n".join(f"{medals[i]} {e}　**{c}**回" for i, (e, c) in enumerate(top_emojis))
        embed.add_field(name="🎭 よく使う絵文字 TOP5", value=emoji_text, inline=False)
    else:
        embed.add_field(name="🎭 よく使う絵文字 TOP5", value="まだデータなし", inline=False)
    embed.set_footer(text=f"総発言数: {sum(messages.values())}回 ｜ Bot起動後からの集計")
    return embed


class Stats(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.data = load_stats()

    def _save(self):
        save_stats(self.data)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return
        uid = str(message.author.id)
        self.data["messages"][uid] = self.data["messages"].get(uid, 0) + 1
        for emoji in UNICODE_EMOJI_RE.findall(message.content):
            self.data["emojis"][emoji] = self.data["emojis"].get(emoji, 0) + 1
        for name in CUSTOM_EMOJI_RE.findall(message.content):
            key = f":{name}:"
            self.data["emojis"][key] = self.data["emojis"].get(key, 0) + 1
        self._save()

    @commands.command()
    async def stats(self, ctx):
        await ctx.send(embed=build_stats_embed(ctx.guild, self.data))


async def setup(bot):
    await bot.add_cog(Stats(bot))
