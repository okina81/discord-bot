import random
import time
import aiohttp
import discord
from discord.ext import commands


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
    async def ping(self, ctx):
        msg = await ctx.send("🌐 通信速度を測定中... しばらく待ってね（10〜20秒かかるよ）")
        try:
            embed = await build_ping_embed()
            await msg.delete()
            await ctx.send(embed=embed)
        except Exception as e:
            await msg.edit(content=f"❌ 測定に失敗しました: {e}")

    @commands.command()
    async def usage(self, ctx):
        embed = discord.Embed(title="🤖 Botの使い方", color=discord.Color.blurple())
        embed.add_field(
            name="🎮 ゲーム募集",
            value="`募集` `募` `ぼ` を含む発言をすると参加者を募る投票を自動で作成\n✅ 参加する　🕐 後から参加（時間をDMで聞いて自動タイマー）　❌ 参加できない",
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
            name="🐾 パルワールド図鑑",
            value="`!pal モコロン` でパルの属性・弱点・作業適性・ステータスを表示\n英語名（`!pal Lamball`）でもOK",
            inline=False,
        )
        embed.add_field(
            name="🥚 パル交配計算",
            value="`!palbreed モコロン キツネビ` で生まれるパルを計算\n`!palparent アヌビス` で逆にそのパルが生まれる親の組み合わせを検索",
            inline=False,
        )
        embed.add_field(
            name="🛠️ パル作業適性検索",
            value="`!palwork 採掘 3` で採掘Lv3以上のパルをランキング表示\n拠点のパル選びに（火起こし・水やり・種まき・発電・手作業・採取・伐採・採掘・製薬・冷却・運搬・牧場）",
            inline=False,
        )
        embed.add_field(
            name="🔰 パル属性相性",
            value="`!paltype` で属性相性表を表示\n`!paltype 炎` で炎属性の有利・弱点と強いパルを表示",
            inline=False,
        )
        embed.add_field(
            name="🖥️ パルワールドサーバー",
            value="`!palserver` でみんなが遊んでいる専用サーバーの接続先を表示\n"
                  "`PALWORLD_API_URL` `PALWORLD_API_PASSWORD` を設定すると\n"
                  "参加人数・サーバーFPS・稼働時間・参加者リストも表示",
            inline=False,
        )
        embed.add_field(
            name="📰 フェイクニュース",
            value="`!news` でサーバーメンバーが登場するフィクションのゲームニュースを生成",
            inline=False,
        )
        embed.add_field(
            name="🎮 コントロールパネル",
            value="`!panel` でボタン式メニューを表示\nランクマップ・サーバー状態・Apex統計・回線速度・フェイクニュース・パル図鑑・パル交配・作業適性・属性相性・パルサーバーをワンタップで操作",
            inline=False,
        )
        embed.set_footer(text="このチャンネル専用Bot")
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Utils(bot))
