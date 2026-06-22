import datetime
import json
import random
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands, tasks
from config import APEX_API_KEY, APEX_NEWS_CHANNEL_ID, JST

APEX_LEGENDS = {
    "バンガロール":     "煙幕張って逃げるだけの消極的戦士",
    "ブラッドハウンド": "スキャンしか能がない犬",
    "コースティック":   "ガスおじさん。チームにも毒を撒く",
    "クリプト":         "ドローン飛ばして本体は物陰でしゃがんでる",
    "ヒューズ":         "グレネード投げすぎて味方を巻き込む豪州人",
    "ジブラルタル":     "デカすぎて当たり判定がバグってる",
    "ホライゾン":       "重力で遊んでる場合じゃない",
    "ライフライン":     "蘇生ドローンが来ない絶望感の権化",
    "ローバ":           "ショップ開いてる間に全員死んでる",
    "マッドマギー":     "何がしたいのか誰もわかってない",
    "ミラージュ":       "幻惑されるやつ現代に0人",
    "ニューキャッスル": "盾持ちながらなぜか最前線に突っ込む",
    "オクタン":         "スティムして特攻して最初に死ぬ担当",
    "パスファインダー": "フックで壁に引っかかって迷子",
    "ランパート":       "タレット設置して自分は動かない置物",
    "レヴナント":       "シャドウフォームで無駄に粘って結局死ぬ",
    "シア":             "ハートビートセンサーのせいで長年嫌われてた",
    "ヴァルキリー":     "飛んで逃げるだけのチキン野郎",
    "ワットソン":       "フェンス張り忘れて終わる人",
    "レイス":           "ボイドに逃げ込んでドン勝狙いのチキン",
    "アッシュ":         "かっこいい見た目に反してスキルが地味",
    "バリスティック":   "おじいちゃんがなぜか最前線に",
    "コンジット":       "シールド回復しか能がない（でもブス）",
    "オルター":         "使ってる人を見たことがない幻の存在",
    "ヴァンテージ":     "スコープ覗いて芋ってるだけのスナイパー",
    "カタリスト":       "フェロフルードで通路塞いで満足してる",
    "アクセル":         "速すぎて自分でも何やってるかわかってない",
}

APEX_MAP_JA = {
    "World's Edge":  "ワールズエッジ",
    "Storm Point":   "ストームポイント",
    "Broken Moon":   "ブロークンムーン",
    "Kings Canyon":  "キングスキャニオン",
    "Olympus":       "オリンパス",
}

RANK_EMOJI = {
    "Rookie":        "🔰",
    "Bronze":        "🥉",
    "Silver":        "🩶",
    "Gold":          "🥇",
    "Platinum":      "🩵",
    "Diamond":       "💎",
    "Master":        "🟣",
    "Apex Predator": "🔴",
}

PLATFORM_ALIAS = {"pc": "PC", "ps4": "PS4", "ps": "PS4", "xbox": "X1", "x1": "X1"}

PC_SERVICES = {
    "EA_novafusion": "🎮 ゲームサーバー",
    "Origin_login":  "🔑 ログイン",
    "EA_accounts":   "👤 アカウント",
}

AWS_REGION_LABEL = {
    "EU-West":      "eu-central-1 (フランクフルト)",
    "EU-East":      "eu-central-1 (フランクフルト)",
    "US-West":      "us-west-2 (オレゴン)",
    "US-Central":   "us-east-2 (オハイオ)",
    "US-East":      "us-east-1 (バージニア)",
    "SouthAmerica": "sa-east-1 (サンパウロ)",
    "Asia":         "ap-northeast-1 (東京)",
    "MiddleEast":   "me-south-1 (バーレーン)",
    "Oceania":      "ap-southeast-2 (シドニー)",
    "Asia-HK":      "ap-east-1 (香港)",
}


async def build_rankmap_embed():
    url = f"https://api.mozambiquehe.re/maprotation?auth={APEX_API_KEY}&version=2"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json(content_type=None)
    ranked = data.get("ranked", {})
    current = ranked.get("current", {})
    nxt = ranked.get("next", {})
    cur_map = APEX_MAP_JA.get(current.get("map", "不明"), current.get("map", "不明"))
    nxt_map = APEX_MAP_JA.get(nxt.get("map", "不明"), nxt.get("map", "不明"))
    rem_sec = current.get("remainingSecs", 0)
    rem_h, rem_m = divmod(rem_sec // 60, 60)
    remain_str = f"{rem_h}時間{rem_m}分" if rem_h else f"{rem_m}分"
    embed = discord.Embed(title="🗺️ Apex ランクマッチ 現在のマップ", color=discord.Color.dark_red())
    embed.add_field(name="🔴 現在",      value=f"**{cur_map}**", inline=True)
    embed.add_field(name="⏱️ 残り時間", value=remain_str,        inline=True)
    embed.add_field(name="⏭️ 次",       value=nxt_map,           inline=True)
    embed.set_footer(text="出典: Apex Legends Status API")
    return embed


async def build_apexstatus_embed():
    url = f"https://api.mozambiquehe.re/servers?auth={APEX_API_KEY}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json(content_type=None)
    embed = discord.Embed(title="🖥️ Apex Legends サーバー状態（PC）", color=discord.Color.dark_red())
    for key, label in PC_SERVICES.items():
        regions = data.get(key, {})
        if not isinstance(regions, dict):
            continue
        lines = [
            f"{'🟢' if v.get('Status') == 'UP' else '🔴'} {AWS_REGION_LABEL.get(k, k)}　{v.get('ResponseTime', '?')}ms"
            for k, v in regions.items() if isinstance(v, dict)
        ]
        all_up = all(v.get("Status") == "UP" for v in regions.values() if isinstance(v, dict))
        any_up = any(v.get("Status") == "UP" for v in regions.values() if isinstance(v, dict))
        icon = "🟢" if all_up else ("🟡" if any_up else "🔴")
        embed.add_field(name=f"{icon} {label}", value="\n".join(lines) or "データなし", inline=False)
    embed.set_footer(text="出典: Apex Legends Status API | AWS移行 2025年4月")
    return embed


async def build_apexstats_embed(username: str, platform: str = "PC"):
    platform = PLATFORM_ALIAS.get(platform.lower(), "PC")
    url = f"https://api.mozambiquehe.re/bridge?auth={APEX_API_KEY}&player={username}&platform={platform}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json(content_type=None)
    if "Error" in data:
        return None
    g = data.get("global", {})
    rank = g.get("rank", {})
    realtime = data.get("realtime", {})
    rank_name = rank.get("rankName", "不明")
    rank_div = rank.get("rankDiv", 0)
    rank_rp = rank.get("rankScore", 0)
    rank_emoji = RANK_EMOJI.get(rank_name, "🎮")
    div_label = ["IV", "III", "II", "I"][rank_div - 1] if 1 <= rank_div <= 4 else ""
    rank_str = f"{rank_name} {div_label}".strip() if div_label else rank_name
    level = g.get("level", "?")
    level_pct = g.get("toNextLevelPercent", 0)
    if realtime.get("isInGame"):
        state = "🟢 ゲーム中"
    elif realtime.get("isOnline"):
        state = "🟡 オンライン"
    else:
        state = "⚫ オフライン"
    legend = realtime.get("selectedLegend", "")
    embed = discord.Embed(title=f"🎮 {g.get('name', username)} の統計", color=discord.Color.dark_red())
    embed.add_field(name="📊 レベル",           value=f"Lv.{level}（{level_pct}%）", inline=True)
    embed.add_field(name=f"{rank_emoji} ランク", value=f"{rank_str}\n{rank_rp:,} RP",  inline=True)
    embed.add_field(name="🔵 状態",              value=state,                           inline=True)
    if legend:
        embed.add_field(name="🦸 選択レジェンド", value=legend, inline=True)
    embed.set_footer(text=f"Platform: {platform} | 出典: Apex Legends Status API")
    return embed


POSTED_NEWS_FILE = Path(__file__).parent.parent / "apex_posted_news.json"


def load_posted_links() -> set[str]:
    if POSTED_NEWS_FILE.exists():
        return set(json.loads(POSTED_NEWS_FILE.read_text(encoding="utf-8")))
    return set()


def save_posted_links(links: set[str]):
    POSTED_NEWS_FILE.write_text(
        json.dumps(sorted(links)[-200:], ensure_ascii=False), encoding="utf-8"
    )


async def fetch_apex_news() -> list[dict]:
    url = f"https://api.mozambiquehe.re/news?auth={APEX_API_KEY}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.json(content_type=None)


class Apex(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.posted_links = load_posted_links()
        if APEX_API_KEY and APEX_NEWS_CHANNEL_ID:
            self.check_apex_news.start()

    def cog_unload(self):
        self.check_apex_news.cancel()

    @tasks.loop(time=datetime.time(hour=9, minute=0, tzinfo=JST))
    async def check_apex_news(self):
        try:
            news_list = await fetch_apex_news()
        except Exception:
            return
        channel = self.bot.get_channel(APEX_NEWS_CHANNEL_ID)
        if not channel:
            return
        new_articles = [
            n for n in news_list
            if n.get("link") and n["link"] not in self.posted_links
        ]
        for article in reversed(new_articles):
            embed = discord.Embed(
                title=article.get("title", "Apex Legends News"),
                url=article.get("link"),
                description=article.get("short_desc", ""),
                color=discord.Color.dark_red(),
            )
            if article.get("img"):
                embed.set_image(url=article["img"])
            embed.set_footer(text="Apex Legends | EA")
            await channel.send(embed=embed)
            self.posted_links.add(article["link"])
        if new_articles:
            save_posted_links(self.posted_links)

    @check_apex_news.before_loop
    async def before_check_apex_news(self):
        await self.bot.wait_until_ready()

    @commands.command()
    async def apex(self, ctx):
        legend, catchphrase = random.choice(list(APEX_LEGENDS.items()))
        await ctx.send(f"🎯 今日のレジェンドは **{legend}** だ！\n> {catchphrase}")

    @commands.command()
    async def rankmap(self, ctx):
        if not APEX_API_KEY:
            await ctx.send("❌ APEX_API_KEY が設定されていないよ！")
            return
        try:
            await ctx.send(embed=await build_rankmap_embed())
        except Exception as e:
            await ctx.send(f"❌ 取得に失敗したよ: {e}")

    @commands.command()
    async def apexstats(self, ctx, username: str, platform: str = "PC"):
        if not APEX_API_KEY:
            await ctx.send("❌ APEX_API_KEY が設定されていないよ！")
            return
        try:
            embed = await build_apexstats_embed(username, platform)
            if embed is None:
                await ctx.send("❌ プレイヤーが見つからなかったよ（EA名とプラットフォームを確認してね）")
            else:
                await ctx.send(embed=embed)
        except Exception as e:
            await ctx.send(f"❌ 取得に失敗したよ: {e}")

    @commands.command()
    async def apexstatus(self, ctx):
        if not APEX_API_KEY:
            await ctx.send("❌ APEX_API_KEY が設定されていないよ！")
            return
        try:
            await ctx.send(embed=await build_apexstatus_embed())
        except Exception as e:
            await ctx.send(f"❌ 取得に失敗したよ: {e}")


async def setup(bot):
    await bot.add_cog(Apex(bot))
