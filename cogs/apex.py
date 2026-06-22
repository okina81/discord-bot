import random
import aiohttp
import discord
from discord.ext import commands
from config import APEX_API_KEY

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


class Apex(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

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
