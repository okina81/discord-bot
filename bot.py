import os
import random
import discord
from discord.ext import commands
from dotenv import load_dotenv
from playwright.async_api import async_playwright

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
        if random.random() < 0.03:
            await message.reply("だぼが")

    if bot.user in message.mentions:
        await message.reply(random.choice(["マギーちゃんのビラビラおまんまん", "うおwww", "う、うおwww", "I love 高木", "いとこは禁句な？", "かわいいだけじゃ、ダメですか？", "いぇーーーーーい！！！"]))

    if "x.com/" in message.content or "twitter.com/" in message.content:
        if random.random() < 1/3:
            await message.reply("おま、X依存症かよw")

    if "コンジット" in message.content:
        await message.reply("コンジットはブス")

    if "パチンコで負け" in message.content:
        await message.reply("…いや待って聞いて、確かに数字だけ見たらマイナスに見えるかもしれんけど、まず交通費と昼飯代除いたら実質もっと少ないし、そもそもあの日の大当たり映像今でも鮮明に覚えてるし精神的な充実度で言ったら絶対プラスなんよ…それにパチ屋で培った確率論の知識とか台の見極め方とか、そういうスキルって金に換算したらめちゃくちゃ価値あると思うし…あの日あの台さえ引き続けてなければ今頃全然違う結果になってたし、俺の立ち回り自体は間違ってないんよ、ただ台が悪かっただけで…あと出玉で飯食えた日も何回かあったし、そういうの全部トータルで計算したらほぼトントンやと思うし…たぶん…そもそも俺はパチンコに課金してるんじゃなくてエンタメに投資してる感覚やから、映画とかライブとか行くのと本質的には変わらんくて、むしろ安い日とかも普通にあるし…まだ終わってへんし、確率って長期で収束するもんやから今はまだ収束途中なだけで、あと少し続けたら絶対取り返せるし…ボソボソ")

    await bot.process_commands(message)


MAC_CATEGORIES = {
    "🍔 バーガー": "https://www.mcdonalds.co.jp/menu/burger/",
    "🍟 サイドメニュー": "https://www.mcdonalds.co.jp/menu/side/",
    "🥤 ドリンク": "https://www.mcdonalds.co.jp/menu/drink/",
    "🍦 スイーツ": "https://www.mcdonalds.co.jp/menu/dessert/",
}
MAC_EXCLUDE = ["特殊立地", "アレルギー", "栄養", "ソース", "シロップ", "コーヒーフレッシュ",
               "シュガー", "リキッドレモン", "バターパット", "焙煎", "シーズニング"]


async def fetch_mac_menu():
    """Playwrightでマクドナルド公式サイトからカテゴリ別メニューを取得する"""
    result = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        for cat, url in MAC_CATEGORIES.items():
            await page.goto(url, wait_until="networkidle")
            items = await page.eval_on_selector_all(
                "strong",
                "els => els.map(el => el.innerText.trim()).filter(t => t.length > 2)"
            )
            filtered = [i for i in items if not any(ex in i for ex in MAC_EXCLUDE)]
            result[cat] = list(dict.fromkeys(filtered))
        await browser.close()
    return result


@bot.command()
async def mac(ctx):
    msg = await ctx.send("🍟 マックのメニューを取得中...")
    try:
        menu = await fetch_mac_menu()
        if not menu:
            await msg.edit(content="メニューの取得に失敗しました。時間をおいて試してみてください。")
            return

        embed = discord.Embed(
            title="🍟 マックのおすすめメニュー（公式より）",
            color=discord.Color.red(),
        )
        total = 0
        for cat, items in menu.items():
            if items:
                picks = random.sample(items, min(2, len(items)))
                embed.add_field(name=cat, value="\n".join(f"・{i}" for i in picks), inline=False)
                total += len(items)
        embed.set_footer(text=f"全{total}種類の中からランダム2選！ | mcdonalds.co.jp")
        await msg.delete()
        await ctx.send(embed=embed)

    except Exception as e:
        await msg.edit(content=f"エラーが発生しました: {e}")



@bot.command()
async def hello(ctx):
    await ctx.send(f"こんにちは、{ctx.author.name}さん！")


@bot.command()
async def ping(ctx):
    latency = round(bot.latency * 1000)

    if latency < 100:
        status = random.choice([
            "🟢 はや！天才か？",
            "🟢 回線強者すぎる",
            "🟢 光回線の申し子",
            "🟢 はやすぎて草",
        ])
        color = discord.Color.green()
    elif latency < 200:
        status = random.choice([
            "🟡 まあ許してやる",
            "🟡 普通やな",
            "🟡 可もなく不可もなく",
        ])
        color = discord.Color.yellow()
    else:
        status = random.choice([
            "🔴 回線ゴミすぎｗ",
            "🔴 Wi-Fiルーターぶん投げろ",
            "🔴 それ回線？砂時計？",
            "🔴 光回線解約したん？",
        ])
        color = discord.Color.red()

    embed = discord.Embed(title="🏓 Pong!", color=color)
    embed.add_field(name="レイテンシ", value=f"{latency}ms", inline=True)
    embed.add_field(name="状態", value=status, inline=True)
    await ctx.send(embed=embed)


bot.run(TOKEN)
