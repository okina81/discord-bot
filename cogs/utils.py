import re
import random
import asyncio
import time
import aiohttp
import discord
from discord.ext import commands
from playwright.async_api import async_playwright
from helpers import parse_duration, format_duration

MAC_CATEGORIES = {
    "🍔 バーガー": "https://www.mcdonalds.co.jp/menu/burger/",
    "🍟 サイドメニュー": "https://www.mcdonalds.co.jp/menu/side/",
    "🥤 ドリンク": "https://www.mcdonalds.co.jp/menu/drink/",
    "🍦 スイーツ": "https://www.mcdonalds.co.jp/menu/dessert/",
}
MAC_EXCLUDE = ["特殊立地", "アレルギー", "栄養", "ソース", "シロップ", "コーヒーフレッシュ",
               "シュガー", "リキッドレモン", "バターパット", "焙煎", "シーズニング"]


async def fetch_mac_menu():
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


async def build_mac_embed():
    menu = await fetch_mac_menu()
    if not menu:
        return None
    embed = discord.Embed(title="🍟 マックのおすすめメニュー（公式より）", color=discord.Color.red())
    total = 0
    for cat, items in menu.items():
        if items:
            picks = random.sample(items, min(2, len(items)))
            embed.add_field(name=cat, value="\n".join(f"・{i}" for i in picks), inline=False)
            total += len(items)
    embed.set_footer(text=f"全{total}種類の中からランダム2選！ | mcdonalds.co.jp")
    return embed


async def build_ping_embed():
    async with aiohttp.ClientSession() as session:
        t0 = time.monotonic()
        async with session.get("https://speed.cloudflare.com/__down?bytes=0") as r:
            await r.read()
        ping_ms = (time.monotonic() - t0) * 1000
        t0 = time.monotonic()
        async with session.get("https://speed.cloudflare.com/__down?bytes=25000000") as r:
            dl_data = await r.read()
        download = len(dl_data) * 8 / (time.monotonic() - t0) / 1_000_000
        payload = b"x" * 10_000_000
        t0 = time.monotonic()
        async with session.post("https://speed.cloudflare.com/__up", data=payload) as r:
            await r.read()
        upload = len(payload) * 8 / (time.monotonic() - t0) / 1_000_000
    if download >= 500:
        comment = random.choice([
            "⚡ 化け物回線すぎて草", "⚡ 何に使うんその速度",
            "⚡ お前のうち通信会社か？", "⚡ もはや回線じゃなくて光そのもの",
        ])
    elif download >= 100:
        comment = random.choice([
            "🚀 はや！光回線の申し子か", "🚀 こんな速度出る？天才か",
            "🚀 どんな回線やねん", "🚀 Botもびびってる",
        ])
    elif download >= 30:
        comment = random.choice([
            "🟢 普通に快適やん", "🟢 まあ文句なし",
            "🟢 ゲームも動画も余裕やな", "🟢 悪くないやん", "🟢 これで不満なら欲張りすぎ",
        ])
    elif download >= 10:
        comment = random.choice([
            "🟡 まあギリ許せる速度", "🟡 ラグったらルーター叩け",
            "🟡 動画たまに止まりそう", "🟡 ゲームはちょっと不安やな", "🟡 Wi-Fi近づけたら？",
        ])
    else:
        comment = random.choice([
            "🔴 回線ゴミすぎｗ Wi-Fi近づけろ", "🔴 それ回線？砂時計？",
            "🔴 ダイヤルアップかよ", "🔴 光回線解約したん？", "🔴 ポケットWi-Fiの電波1本やろこれ",
        ])
    embed = discord.Embed(title="🌐 通信速度テスト結果", color=discord.Color.blue())
    embed.add_field(name="📥 ダウンロード", value=f"{download:.1f} Mbps", inline=True)
    embed.add_field(name="📤 アップロード", value=f"{upload:.1f} Mbps",   inline=True)
    embed.add_field(name="🏓 Ping",         value=f"{ping_ms:.1f} ms",    inline=True)
    embed.add_field(name="一言",             value=comment,                inline=False)
    embed.set_footer(text="※ Botが動いているマシンの回線速度です")
    return embed


class Utils(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def mac(self, ctx):
        msg = await ctx.send("🍟 マックのメニューを取得中...")
        try:
            embed = await build_mac_embed()
            if embed:
                await msg.delete()
                await ctx.send(embed=embed)
            else:
                await msg.edit(content="メニューの取得に失敗しました。時間をおいて試してみてください。")
        except Exception as e:
            await msg.edit(content=f"エラーが発生しました: {e}")

    @commands.command()
    async def ping(self, ctx):
        msg = await ctx.send("🌐 通信速度を測定中... しばらく待ってね（10〜20秒かかるよ）")
        try:
            embed = await build_ping_embed()
            await msg.delete()
            await ctx.send(embed=embed)
        except Exception as e:
            await msg.edit(content=f"❌ 測定に失敗しました: {e}")

    @commands.command()
    async def timer(self, ctx, duration: str):
        seconds = parse_duration(duration)
        if seconds is None:
            await ctx.send("⏱️ 時間の形式が違うよ！例: `!timer 10m` `!timer 1h30m` `!timer 30s`")
            return
        if seconds > 3 * 3600:
            await ctx.send("⏱️ タイマーは最大3時間までだよ！")
            return
        label = format_duration(seconds)
        await ctx.send(f"⏱️ {label}後に {ctx.author.mention} を呼び出すよ！")
        await asyncio.sleep(seconds)
        await ctx.send(f"⏰ {ctx.author.mention} タイムアップ！{label}経ったぞ、そろそろゲームやろ！")

    @commands.command()
    async def who(self, ctx):
        embed = discord.Embed(title="🤖 自己紹介", color=discord.Color.og_blurple())
        embed.add_field(name="名前", value="今北尚杜", inline=True)
        embed.add_field(name="住所", value="神戸市", inline=True)
        embed.add_field(name="職業", value="職なし子供部屋おじさんだにょ", inline=False)
        embed.add_field(name="見た目", value="台パンチビ眼鏡", inline=False)
        embed.add_field(name="役職", value="このサーバーの奴隷です", inline=False)
        embed.add_field(name="特技", value="破壊と近親相姦", inline=False)
        await ctx.send(embed=embed)

    @commands.command()
    async def hello(self, ctx):
        await ctx.send(f"こんにちは、{ctx.author.name}さん！")

    @commands.command()
    async def emoji(self, ctx, name: str = None, url: str = None):
        if not ctx.guild.me.guild_permissions.manage_expressions:
            await ctx.send("❌ Botに「絵文字の管理」権限がないよ！")
            return
        if name is None:
            await ctx.send(
                "❌ 使い方: `!emoji <名前> [画像URL]`\n"
                "画像URLを省略した場合は画像ファイルを添付してね。\n"
                "例: `!emoji kawaii https://example.com/image.png`"
            )
            return
        if not re.fullmatch(r"[a-zA-Z0-9_]{2,32}", name):
            await ctx.send("❌ 絵文字の名前は英数字・アンダーバーのみ、2〜32文字で指定してね。")
            return
        image_url = url
        if image_url is None:
            if ctx.message.attachments:
                image_url = ctx.message.attachments[0].url
            else:
                await ctx.send("❌ 画像URLか画像ファイルを添付してね。")
                return
        msg = await ctx.send("🎨 絵文字を作成中...")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(image_url) as resp:
                    if resp.status != 200:
                        await msg.edit(content=f"❌ 画像の取得に失敗したよ（HTTP {resp.status}）")
                        return
                    image_data = await resp.read()
            new_emoji = await ctx.guild.create_custom_emoji(name=name, image=image_data)
            await msg.edit(content=f"✅ 絵文字 {new_emoji} `:{name}:` を追加したよ！")
        except discord.HTTPException as e:
            if e.code == 30008:
                await msg.edit(content="❌ サーバーの絵文字スロットが満杯だよ！")
            elif e.code == 50138:
                await msg.edit(content="❌ 画像サイズが大きすぎるよ（256KB以下にしてね）")
            else:
                await msg.edit(content=f"❌ 絵文字の作成に失敗したよ: {e.text}")
        except Exception as e:
            await msg.edit(content=f"❌ エラーが発生したよ: {e}")

    @commands.command()
    async def usage(self, ctx):
        embed = discord.Embed(title="🤖 Botの使い方", color=discord.Color.blurple())
        embed.add_field(
            name="🎮 ゲーム募集",
            value="`募集` `募` `ぼ` を含む発言をすると参加者を募る投票を自動で作成\n✅ 参加する　🕐 後から参加（時間をDMで聞いて自動タイマー）　❌ 参加できない",
            inline=False,
        )
        embed.add_field(
            name="🍟 マックのおすすめ",
            value="`!mac` で公式サイトから\nカテゴリ別おすすめメニューをランダム表示",
            inline=False,
        )
        embed.add_field(
            name="🌐 通信速度テスト",
            value="`!ping` でダウンロード・アップロード速度と Ping を測定\n※ Botが動いているマシンの回線速度",
            inline=False,
        )
        embed.add_field(
            name="📊 ポケモン種族値",
            value="ポケモンの名前を含む発言をすると\n自動で種族値を表示",
            inline=False,
        )
        embed.add_field(
            name="⏱️ タイマー",
            value="`!timer 10m` `!timer 1h30m` `!timer 30s` など\n指定した時間後にメンションで呼び出す（最大3時間）",
            inline=False,
        )
        embed.add_field(
            name="🎯 Apexレジェンド",
            value="`!apex` でランダムにレジェンドを1人選出\nディスりキャッチコピー付き",
            inline=False,
        )
        embed.add_field(
            name="🗺️ Apex ランクマップ",
            value="`!rankmap` で現在のランクマッチのマップ・残り時間・次のマップを表示",
            inline=False,
        )
        embed.add_field(
            name="👤 Apex プレイヤー統計",
            value="`!apexstats EA名` でレベル・ランク・オンライン状態を表示\n例: `!apexstats PlayerName` `!apexstats PlayerName PS4`",
            inline=False,
        )
        embed.add_field(
            name="🖥️ Apex サーバー状態",
            value="`!apexstatus` でサーバーの稼働状況をリージョン別に表示",
            inline=False,
        )
        embed.add_field(
            name="⚔️ チーム分け",
            value="`!team @A @B @C @D ...` でメンションした人をランダムに2チームへ振り分け",
            inline=False,
        )
        embed.add_field(
            name="📈 トレンド",
            value="`!trend` でネットで流行った言葉・トレンドをランダムに表示＆解説",
            inline=False,
        )
        embed.add_field(
            name="🔥 煽り",
            value="`!roast @ユーザー` で指定した人を煽る",
            inline=False,
        )
        embed.add_field(
            name="🎰 罰ゲームルーレット",
            value="`!roulette` でランダムに罰ゲームを決定\n`!roulette @ユーザー` で指定したユーザーにルーレットを実行",
            inline=False,
        )
        embed.add_field(
            name="📊 サーバー統計",
            value="`!stats` で発言数ランキングと絵文字TOP5を表示",
            inline=False,
        )
        embed.add_field(
            name="📈 レベルシステム",
            value="`!rank` で自分のレベル・XP・順位を表示\n`!rank @ユーザー` で他の人のランクも確認可能\n`!leaderboard` でレベルランキングTOP10\nメッセージを送るとXPが貯まる（60秒クールダウン）",
            inline=False,
        )
        embed.add_field(
            name="🤖 Bot自己紹介",
            value="`!who` でBotのプロフィールを表示",
            inline=False,
        )
        embed.add_field(
            name="📰 フェイクニュース",
            value="`!news` でサーバーメンバーが登場するフィクションのゲームニュースを生成",
            inline=False,
        )
        embed.add_field(
            name="🎨 絵文字作成",
            value="`!emoji <名前> [画像URL]` でサーバーにカスタム絵文字を追加\n画像URLを省略した場合は画像ファイルを添付してね\n例: `!emoji kawaii https://example.com/image.png`",
            inline=False,
        )
        embed.add_field(
            name="🎮 コントロールパネル",
            value="`!panel` でボタン式メニューを表示\nランクマップ・サーバー状態・Apex統計・チーム分け・ルーレット・マック・サーバー統計・回線速度・フェイクニュースをワンタップで操作",
            inline=False,
        )
        embed.set_footer(text="このチャンネル専用Bot")
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Utils(bot))
