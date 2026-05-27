import os
import random
import discord
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


MAC_MENU = {
    "🍔 バーガー": [
        "ビッグマック",
        "クォーターパウンダーチーズ",
        "てりやきマックバーガー",
        "フィレオフィッシュ",
        "チキンタツタ",
        "ダブルチーズバーガー",
    ],
    "🍟 サイド": [
        "マックフライポテト（L）",
        "チキンナゲット（15ピース）",
        "マックシェイク",
        "コーン",
        "サラダ",
    ],
    "🥤 ドリンク": [
        "コカ・コーラ",
        "マックフィズ オレンジ",
        "カフェラテ",
        "マックシェイク チョコレート",
        "お茶",
    ],
    "🍦 デザート": [
        "マックサンデー チョコレート",
        "アップルパイ",
        "ソフトツイストコーン",
        "マックフルーリー オレオ",
    ],
}


@bot.command()
async def mac(ctx):
    embed = discord.Embed(
        title="🍟 マックのおすすめメニュー",
        color=discord.Color.red(),
    )
    for category, items in MAC_MENU.items():
        pick = random.sample(items, min(2, len(items)))
        embed.add_field(name=category, value="\n".join(f"・{item}" for item in pick), inline=False)
    embed.set_footer(text="今日のおすすめはこれだ！")
    await ctx.send(embed=embed)


@bot.command()
async def hello(ctx):
    await ctx.send(f"こんにちは、{ctx.author.name}さん！")


@bot.command()
async def ping(ctx):
    await ctx.send(f"Pong! レイテンシ: {round(bot.latency * 1000)}ms")


bot.run(TOKEN)
