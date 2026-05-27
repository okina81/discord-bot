import os
import random
import aiohttp
import discord
from bs4 import BeautifulSoup
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

RECRUIT_KEYWORDS = ["募集", "募", "ぼ"]
TARGET_USER_IDS = [512510702129512469, 1133749381250695269]


@bot.event
async def on_ready():
    print(f"{bot.user} としてログインしました")


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if any(kw in message.content for kw in RECRUIT_KEYWORDS):
        embed = discord.Embed(
            title="🎮 ゲーム募集",
            description=f"**{message.author.display_name}** さんが一緒にゲームをする人を募集しています！\n\n参加できる人は ✅ を押してください！",
            color=discord.Color.green(),
        )
        embed.set_footer(text="❌ = 参加できない")

        poll = await message.channel.send(embed=embed)
        await poll.add_reaction("✅")
        await poll.add_reaction("❌")

    if message.author.id in TARGET_USER_IDS:
        if random.random() < 0.1:
            await message.reply("だぼが")

    if bot.user in message.mentions:
        await message.reply("マギーちゃんのビラビラおまんまん")

    await bot.process_commands(message)


MAC_URL = "https://www.mcdonalds.co.jp/menu/"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"}


async def fetch_mac_menu():
    """マクドナルド公式サイトからメニューを取得する"""
    async with aiohttp.ClientSession() as session:
        async with session.get(MAC_URL, headers=HEADERS) as resp:
            html = await resp.text()

    soup = BeautifulSoup(html, "html.parser")
    items = []
    for a in soup.find_all("a", href=True):
        if "/products/" in a["href"]:
            strong = a.find("strong")
            if strong and strong.text.strip():
                name = strong.text.strip()
                if name not in items:
                    items.append(name)
    return items


@bot.command()
async def mac(ctx):
    await ctx.send("🍟 マックのメニューを取得中...")
    try:
        items = await fetch_mac_menu()
        if not items:
            await ctx.send("メニューの取得に失敗しました。時間をおいて試してみてください。")
            return

        picks = random.sample(items, min(5, len(items)))
        embed = discord.Embed(
            title="🍟 マックのおすすめメニュー（公式より）",
            description="\n".join(f"・{item}" for item in picks),
            color=discord.Color.red(),
        )
        embed.set_footer(text=f"全{len(items)}種類の中からランダム5選！ | mcdonalds.co.jp")
        await ctx.send(embed=embed)

    except Exception as e:
        await ctx.send(f"エラーが発生しました: {e}")


@bot.command()
async def hello(ctx):
    await ctx.send(f"こんにちは、{ctx.author.name}さん！")


@bot.command()
async def ping(ctx):
    latency = round(bot.latency * 1000)

    if latency < 100:
        status = "🟢 快適！サクサクだ！"
        color = discord.Color.green()
    elif latency < 200:
        status = "🟡 まあまあかな"
        color = discord.Color.yellow()
    else:
        status = "🔴 重い…つらい…"
        color = discord.Color.red()

    embed = discord.Embed(title="🏓 Pong!", color=color)
    embed.add_field(name="レイテンシ", value=f"{latency}ms", inline=True)
    embed.add_field(name="状態", value=status, inline=True)
    await ctx.send(embed=embed)


bot.run(TOKEN)
