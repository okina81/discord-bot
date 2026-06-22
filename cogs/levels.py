import json
import time
import random
from pathlib import Path
import discord
from discord.ext import commands

LEVELS_FILE = Path("levels.json")
XP_MIN, XP_MAX = 15, 25
XP_COOLDOWN = 60


def xp_for_level(level: int) -> int:
    return 5 * level ** 2 + 50 * level + 100


def load_levels() -> dict:
    if LEVELS_FILE.exists():
        with open(LEVELS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_levels(data: dict) -> None:
    with open(LEVELS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class Levels(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.data = load_levels()

    def _save(self):
        save_levels(self.data)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return
        uid = str(message.author.id)
        now_ts = time.time()
        user_lv = self.data.setdefault(uid, {"xp": 0, "level": 0, "last_xp_time": 0})
        if now_ts - user_lv["last_xp_time"] < XP_COOLDOWN:
            return
        gained = random.randint(XP_MIN, XP_MAX)
        user_lv["xp"] += gained
        user_lv["last_xp_time"] = now_ts
        old_level = user_lv["level"]
        while user_lv["xp"] >= xp_for_level(user_lv["level"]):
            user_lv["xp"] -= xp_for_level(user_lv["level"])
            user_lv["level"] += 1
        if user_lv["level"] > old_level:
            await message.channel.send(
                f"🎉 {message.author.mention} が **レベル {user_lv['level']}** に上がった！"
            )
        self._save()

    @commands.command()
    async def rank(self, ctx, member: discord.Member = None):
        target = member or ctx.author
        uid = str(target.id)
        user_lv = self.data.get(uid, {"xp": 0, "level": 0})
        level = user_lv["level"]
        xp = user_lv["xp"]
        needed = xp_for_level(level)
        pct = xp / needed if needed > 0 else 0
        bar_len = 20
        filled = int(bar_len * pct)
        bar = "█" * filled + "░" * (bar_len - filled)
        total_xp = sum(xp_for_level(i) for i in range(level)) + xp
        all_users = sorted(self.data.items(), key=lambda x: (x[1]["level"], x[1]["xp"]), reverse=True)
        rank_pos = next((i + 1 for i, (u, _) in enumerate(all_users) if u == uid), len(all_users) + 1)
        embed = discord.Embed(color=discord.Color.green())
        embed.set_author(name=f"{target.display_name} のランク", icon_url=target.display_avatar.url)
        embed.add_field(name="レベル", value=f"**{level}**", inline=True)
        embed.add_field(name="順位", value=f"**#{rank_pos}**", inline=True)
        embed.add_field(name="総XP", value=f"**{total_xp:,}**", inline=True)
        embed.add_field(name=f"進捗　{xp:,} / {needed:,} XP", value=f"`{bar}` {pct:.0%}", inline=False)
        await ctx.send(embed=embed)

    @commands.command()
    async def leaderboard(self, ctx):
        if not self.data:
            await ctx.send("❌ まだ誰もレベルデータがないよ！")
            return
        sorted_users = sorted(self.data.items(), key=lambda x: (x[1]["level"], x[1]["xp"]), reverse=True)[:10]
        medals = ["🥇", "🥈", "🥉"] + [f"**{i}.**" for i in range(4, 11)]
        lines = []
        for i, (uid, data) in enumerate(sorted_users):
            member = ctx.guild.get_member(int(uid))
            name = member.display_name if member else "退出済みユーザー"
            total_xp = sum(xp_for_level(j) for j in range(data["level"])) + data["xp"]
            lines.append(f"{medals[i]}　{name}　—　Lv.**{data['level']}**　({total_xp:,} XP)")
        embed = discord.Embed(
            title="🏆 レベルランキング TOP10",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="メッセージを送るとXPが貯まるよ！")
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Levels(bot))
