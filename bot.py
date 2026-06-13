import os
import re
import json
import random
import asyncio
import datetime
import time
from pathlib import Path
import aiohttp
import discord
from google import genai as google_genai
from discord.ext import commands
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()
TOKEN          = os.getenv("DISCORD_TOKEN")
APEX_API_KEY   = os.getenv("APEX_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
_gemini_client = google_genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

RECRUIT_KEYWORDS = ["募集", "募", "ぼ"]
TARGET_USER_IDS = [512510702129512469, 1133749381250695269]
ALLOWED_CHANNEL_IDS = [1509231531326181406]

JST = datetime.timezone(datetime.timedelta(hours=9))

# 募集投票のメッセージID → {"channel_id": int, "start_at": datetime | None}
recruit_polls: dict[int, dict] = {}

# サーバー統計
STATS_FILE = Path("stats.json")

def load_stats() -> dict:
    if STATS_FILE.exists():
        with open(STATS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"messages": {}, "emojis": {}}

def save_stats() -> None:
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(server_stats, f, ensure_ascii=False, indent=2)

server_stats = load_stats()

UNICODE_EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001FAFF]|[\U00002600-\U000027BF]|[\U0001F000-\U0001F02F]"
)
CUSTOM_EMOJI_RE  = re.compile(r"<a?:(\w+):\d+>")


# ポケモン名キャッシュ
pokemon_cache = {}   # {"レントラー": 405, ...}
pokemon_pattern = None  # 検索用コンパイル済み正規表現

STAT_NAMES = {
    "hp": "HP",
    "attack": "こうげき",
    "defense": "ぼうぎょ",
    "special-attack": "とくこう",
    "special-defense": "とくぼう",
    "speed": "すばやさ",
}


async def build_pokemon_cache():
    """PokeAPI GraphQLで全ポケモンの日本語名→IDキャッシュを構築する"""
    global pokemon_cache, pokemon_pattern
    graphql_url = "https://beta.pokeapi.co/graphql/v1beta"
    query = """{ pokemon_v2_pokemonspeciesname(where: {language_id: {_in: [1, 11]}}) { name pokemon_species_id } }"""
    async with aiohttp.ClientSession() as session:
        async with session.post(graphql_url, json={"query": query}) as resp:
            data = await resp.json()
    for item in data["data"]["pokemon_v2_pokemonspeciesname"]:
        pokemon_cache[item["name"]] = item["pokemon_species_id"]
    names_sorted = sorted(pokemon_cache.keys(), key=len, reverse=True)
    pokemon_pattern = re.compile("|".join(re.escape(n) for n in names_sorted))
    print(f"ポケモンキャッシュ構築完了: {len(pokemon_cache)}種")


async def get_pokemon_stats(pokemon_id):
    """ポケモンIDから種族値と日本語名を取得する"""
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://pokeapi.co/api/v2/pokemon/{pokemon_id}/") as resp:
            data = await resp.json()
        async with session.get(f"https://pokeapi.co/api/v2/pokemon-species/{pokemon_id}/") as resp:
            species = await resp.json()
    ja_name = next(
        (n["name"] for n in species["names"] if n["language"]["name"] == "ja"),
        data["name"]
    )
    stats = {STAT_NAMES[s["stat"]["name"]]: s["base_stat"]
             for s in data["stats"] if s["stat"]["name"] in STAT_NAMES}
    return ja_name, data["id"], stats


async def notify_game_start(poll_msg_id, channel_id, seconds, time_str):
    """指定秒数後に、募集メッセージの ✅ リアクションを押した全員をメンションする"""
    await asyncio.sleep(seconds)
    channel = bot.get_channel(channel_id)
    try:
        poll_msg = await channel.fetch_message(poll_msg_id)
    except discord.NotFound:
        return
    mentions = []
    for reaction in poll_msg.reactions:
        if str(reaction.emoji) == "✅":
            async for user in reaction.users():
                if not user.bot:
                    mentions.append(user.mention)
    mention_str = " ".join(mentions) if mentions else ""
    await channel.send(f"⏰ **{time_str}** になったぞ！ゲーム開始の時間だ！\n{mention_str}")


@bot.event
async def on_ready():
    print(f"{bot.user} としてログインしました")
    await build_pokemon_cache()


@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id:
        return
    if payload.message_id not in recruit_polls:
        return
    if str(payload.emoji) != "🕐":
        return

    guild  = bot.get_guild(payload.guild_id)
    member = guild.get_member(payload.user_id)
    if member is None:
        return

    poll_data       = recruit_polls[payload.message_id]
    recruit_channel = bot.get_channel(poll_data["channel_id"])
    start_at        = poll_data["start_at"]  # datetime | None

    # DMで参加予定時刻を確認
    if start_at:
        time_str_base = f"{start_at.hour}時" + (f"{start_at.minute}分" if start_at.minute else "")
        dm_hint = (
            f"🕐 **{time_str_base}から**のゲームに何分後に参加できる？\n"
            f"例: `10m`（{time_str_base}の10分後）　`1h`（1時間後）　`15時30分から`（時刻で直接指定）"
        )
    else:
        dm_hint = (
            "🕐 何分後（何時間後）に参加できる？\n"
            "例: `10m`（10分後）　`1h30m`（1時間30分後）　`15時から`　`15時30分から`"
        )

    try:
        await member.send(dm_hint)
    except discord.Forbidden:
        await recruit_channel.send(
            f"{member.mention} DMが送れなかったよ。何分後・何時から参加できるか教えて！"
        )
        return

    def is_reply(m):
        return m.author.id == payload.user_id and isinstance(m.channel, discord.DMChannel)

    try:
        reply = await bot.wait_for("message", check=is_reply, timeout=60)
    except asyncio.TimeoutError:
        await member.send("⌛ タイムアウトしたよ。もう一度 🕐 を押してね。")
        return

    text = reply.content.strip()

    # まず「〇分後」形式で解析
    duration_sec = parse_duration(text)
    if duration_sec is not None:
        if start_at:
            # 開始時刻 + 指定時間 を通知タイミングにする
            notify_at  = start_at + datetime.timedelta(seconds=duration_sec)
            seconds    = max(0, int((notify_at - datetime.datetime.now(JST)).total_seconds()))
            notify_str = f"{notify_at.hour}時" + (f"{notify_at.minute}分" if notify_at.minute else "")
            confirm    = f"⏱️ わかった！{notify_str}（{time_str_base}の{format_duration(duration_sec)}後）に教えるね。"
            notify     = f"⏰ {member.mention} {notify_str}になったぞ！そろそろゲーム参加できる？"
        else:
            if duration_sec > 3 * 3600:
                await member.send("❌ タイマーは最大3時間までだよ！")
                return
            seconds = duration_sec
            confirm = f"⏱️ わかった！{format_duration(seconds)}後に教えるね。"
            notify  = f"⏰ {member.mention} {format_duration(seconds)}経ったぞ！そろそろゲーム参加できる？"
    else:
        # 「〇時から」形式で解析
        result = parse_clock_time(text)
        if result is None:
            await member.send(
                "❌ 形式が合ってないよ！\n"
                "例: `10m` `1h30m` `15時から` `15時30分から`"
            )
            return
        seconds, time_str = result
        if seconds > 12 * 3600:
            await member.send("❌ 12時間以上先の時刻は指定できないよ！")
            return
        confirm = f"⏱️ わかった！{time_str}になったら教えるね。"
        notify  = f"⏰ {member.mention} {time_str}になったぞ！そろそろゲーム参加できる？"

    await member.send(confirm)
    await asyncio.sleep(seconds)
    await recruit_channel.send(notify)


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # 統計集計（サーバー内のみ）
    if message.guild:
        uid = str(message.author.id)
        server_stats["messages"][uid] = server_stats["messages"].get(uid, 0) + 1
        for emoji in UNICODE_EMOJI_RE.findall(message.content):
            server_stats["emojis"][emoji] = server_stats["emojis"].get(emoji, 0) + 1
        for name in CUSTOM_EMOJI_RE.findall(message.content):
            key = f":{name}:"
            server_stats["emojis"][key] = server_stats["emojis"].get(key, 0) + 1
        save_stats()

    if "x.com/" in message.content or "twitter.com/" in message.content:
        if random.random() < 1/3:
            await message.reply("おま、X依存症かよw")

    if message.channel.id not in ALLOWED_CHANNEL_IDS:
        await bot.process_commands(message)
        return

    if any(kw in message.content for kw in RECRUIT_KEYWORDS):
        time_result = extract_clock_time(message.content)
        if time_result:
            _, time_str, start_at = time_result
            description = (
                f"**{message.author.display_name}** さんが "
                f"**{time_str}から** 一緒にゲームをする人を募集しています！"
            )
        else:
            start_at    = None
            description = f"**{message.author.display_name}** さんが一緒にゲームをする人を募集しています！"

        embed = discord.Embed(
            title="🎮 ゲーム募集",
            description=description,
            color=discord.Color.green(),
        )
        embed.set_footer(text="✅ 参加する　🕐 後から参加　❌ 参加できない")

        recruit_channel = bot.get_channel(1134854694645276703)
        poll = await recruit_channel.send(embed=embed)
        await poll.add_reaction("✅")
        await poll.add_reaction("🕐")
        await poll.add_reaction("❌")
        recruit_polls[poll.id] = {"channel_id": recruit_channel.id, "start_at": start_at}

        if time_result:
            seconds_until, time_str, _ = time_result
            asyncio.create_task(
                notify_game_start(poll.id, recruit_channel.id, seconds_until, time_str)
            )

    if message.author.id in TARGET_USER_IDS:
        if random.random() < 0.03:
            await message.reply("だぼが")

    if bot.user in message.mentions:
        await message.reply(random.choice(["マギーちゃんのビラビラおまんまん", "うおwww", "う、うおwww", "I love 高木", "いとこは禁句な？", "かわいいだけじゃ、ダメですか？", "いぇーーーーーい！！！"]))

    if "コンジット" in message.content:
        await message.reply("コンジットはブス")

    if "パチンコで負け" in message.content:
        await message.reply("…いや待って聞いて、確かに数字だけ見たらマイナスに見えるかもしれんけど、まず交通費と昼飯代除いたら実質もっと少ないし、そもそもあの日の大当たり映像今でも鮮明に覚えてるし精神的な充実度で言ったら絶対プラスなんよ…それにパチ屋で培った確率論の知識とか台の見極め方とか、そういうスキルって金に換算したらめちゃくちゃ価値あると思うし…あの日あの台さえ引き続けてなければ今頃全然違う結果になってたし、俺の立ち回り自体は間違ってないんよ、ただ台が悪かっただけで…あと出玉で飯食えた日も何回かあったし、そういうの全部トータルで計算したらほぼトントンやと思うし…たぶん…そもそも俺はパチンコに課金してるんじゃなくてエンタメに投資してる感覚やから、映画とかライブとか行くのと本質的には変わらんくて、むしろ安い日とかも普通にあるし…まだ終わってへんし、確率って長期で収束するもんやから今はまだ収束途中なだけで、あと少し続けたら絶対取り返せるし…ボソボソ")

    if pokemon_pattern:
        match = pokemon_pattern.search(message.content)
        if match:
            pokemon_name = match.group()
            pokemon_id = pokemon_cache[pokemon_name]
            try:
                ja_name, dex_id, stats = await get_pokemon_stats(pokemon_id)
                total = sum(stats.values())
                embed = discord.Embed(
                    title=f"📊 {ja_name}（#{dex_id}）の種族値",
                    color=discord.Color.gold(),
                )
                stats_text = "\n".join(f"{k}　{v}" for k, v in stats.items())
                embed.description = f"```{stats_text}\n─────────\n合計　　{total}```"
                embed.set_footer(text="出典: PokeAPI")
                await message.channel.send(embed=embed)
            except Exception:
                pass

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


# ─── パネル用ヘルパー ────────────────────────────────────────────

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


async def build_rankmap_embed():
    url = f"https://api.mozambiquehe.re/maprotation?auth={APEX_API_KEY}&version=2"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json(content_type=None)
    ranked  = data.get("ranked", {})
    current = ranked.get("current", {})
    nxt     = ranked.get("next", {})
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


PC_SERVICES = {
    "EA_novafusion": "🎮 ゲームサーバー",
    "Origin_login":  "🔑 ログイン",
    "EA_accounts":   "👤 アカウント",
}

# 2025年4月 AWS 移行後のリージョン対応表
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
        icon   = "🟢" if all_up else ("🟡" if any_up else "🔴")
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
    g        = data.get("global", {})
    rank     = g.get("rank", {})
    realtime = data.get("realtime", {})
    rank_name  = rank.get("rankName", "不明")
    rank_div   = rank.get("rankDiv", 0)
    rank_rp    = rank.get("rankScore", 0)
    rank_emoji = RANK_EMOJI.get(rank_name, "🎮")
    div_label  = ["IV", "III", "II", "I"][rank_div - 1] if 1 <= rank_div <= 4 else ""
    rank_str   = f"{rank_name} {div_label}".strip() if div_label else rank_name
    level      = g.get("level", "?")
    level_pct  = g.get("toNextLevelPercent", 0)
    if realtime.get("isInGame"):
        state = "🟢 ゲーム中"
    elif realtime.get("isOnline"):
        state = "🟡 オンライン"
    else:
        state = "⚫ オフライン"
    legend = realtime.get("selectedLegend", "")
    embed  = discord.Embed(title=f"🎮 {g.get('name', username)} の統計", color=discord.Color.dark_red())
    embed.add_field(name="📊 レベル",           value=f"Lv.{level}（{level_pct}%）", inline=True)
    embed.add_field(name=f"{rank_emoji} ランク", value=f"{rank_str}\n{rank_rp:,} RP",  inline=True)
    embed.add_field(name="🔵 状態",              value=state,                           inline=True)
    if legend:
        embed.add_field(name="🦸 選択レジェンド", value=legend, inline=True)
    embed.set_footer(text=f"Platform: {platform} | 出典: Apex Legends Status API")
    return embed


def build_stats_embed(guild: discord.Guild):
    embed   = discord.Embed(title="📊 サーバー統計", color=discord.Color.purple())
    medals  = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    messages = server_stats.get("messages", {})
    if messages:
        top_users = sorted(messages.items(), key=lambda x: x[1], reverse=True)[:5]
        ranking   = ""
        for i, (uid, count) in enumerate(top_users):
            member  = guild.get_member(int(uid))
            name    = member.display_name if member else "退出済みユーザー"
            ranking += f"{medals[i]} {name}　**{count}**回\n"
        embed.add_field(name="💬 発言数ランキング", value=ranking, inline=False)
    else:
        embed.add_field(name="💬 発言数ランキング", value="まだデータなし", inline=False)
    emojis = server_stats.get("emojis", {})
    if emojis:
        top_emojis = sorted(emojis.items(), key=lambda x: x[1], reverse=True)[:5]
        emoji_text = "\n".join(f"{medals[i]} {e}　**{c}**回" for i, (e, c) in enumerate(top_emojis))
        embed.add_field(name="🎭 よく使う絵文字 TOP5", value=emoji_text, inline=False)
    else:
        embed.add_field(name="🎭 よく使う絵文字 TOP5", value="まだデータなし", inline=False)
    embed.set_footer(text=f"総発言数: {sum(messages.values())}回 ｜ Bot起動後からの集計")
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
        payload  = b"x" * 10_000_000
        t0 = time.monotonic()
        async with session.post("https://speed.cloudflare.com/__up", data=payload) as r:
            await r.read()
        upload = len(payload) * 8 / (time.monotonic() - t0) / 1_000_000
    if download >= 500:
        comment = random.choice([
            "⚡ 化け物回線すぎて草",
            "⚡ 何に使うんその速度",
            "⚡ お前のうち通信会社か？",
            "⚡ もはや回線じゃなくて光そのもの",
        ])
    elif download >= 100:
        comment = random.choice([
            "🚀 はや！光回線の申し子か",
            "🚀 こんな速度出る？天才か",
            "🚀 どんな回線やねん",
            "🚀 Botもびびってる",
        ])
    elif download >= 30:
        comment = random.choice([
            "🟢 普通に快適やん",
            "🟢 まあ文句なし",
            "🟢 ゲームも動画も余裕やな",
            "🟢 悪くないやん",
            "🟢 これで不満なら欲張りすぎ",
        ])
    elif download >= 10:
        comment = random.choice([
            "🟡 まあギリ許せる速度",
            "🟡 ラグったらルーター叩け",
            "🟡 動画たまに止まりそう",
            "🟡 ゲームはちょっと不安やな",
            "🟡 Wi-Fi近づけたら？",
        ])
    else:
        comment = random.choice([
            "🔴 回線ゴミすぎｗ Wi-Fi近づけろ",
            "🔴 それ回線？砂時計？",
            "🔴 ダイヤルアップかよ",
            "🔴 光回線解約したん？",
            "🔴 ポケットWi-Fiの電波1本やろこれ",
        ])
    embed = discord.Embed(title="🌐 通信速度テスト結果", color=discord.Color.blue())
    embed.add_field(name="📥 ダウンロード", value=f"{download:.1f} Mbps", inline=True)
    embed.add_field(name="📤 アップロード", value=f"{upload:.1f} Mbps",   inline=True)
    embed.add_field(name="🏓 Ping",         value=f"{ping_ms:.1f} ms",    inline=True)
    embed.add_field(name="一言",             value=comment,                inline=False)
    embed.set_footer(text="※ Botが動いているマシンの回線速度です")
    return embed


# ─── パネル UI ───────────────────────────────────────────────────

class TeamSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="チーム分けするメンバーを選択（2人以上）",
        min_values=2,
        max_values=10,
    )
    async def select_members(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        members = select.values
        shuffled = list(members)
        random.shuffle(shuffled)
        mid    = (len(shuffled) + 1) // 2
        team_a = shuffled[:mid]
        team_b = shuffled[mid:]
        embed  = discord.Embed(title="⚔️ チーム分け結果", color=discord.Color.blue())
        embed.add_field(name="🔴 チームA", value="\n".join(f"・{m.display_name}" for m in team_a), inline=True)
        embed.add_field(name="🔵 チームB", value="\n".join(f"・{m.display_name}" for m in team_b), inline=True)
        if len(members) % 2 != 0:
            embed.set_footer(text="人数が奇数のためチームAに1人多く振り分けました")
        await interaction.response.send_message(embed=embed)


class ApexStatsModal(discord.ui.Modal, title="👤 Apex プレイヤー統計"):
    username = discord.ui.TextInput(label="EA名", placeholder="プレイヤー名を入力", required=True)
    platform = discord.ui.TextInput(label="プラットフォーム（PC / PS4 / X1）", default="PC", required=False)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        plat  = self.platform.value.strip() or "PC"
        embed = await build_apexstats_embed(self.username.value.strip(), plat)
        if embed is None:
            await interaction.followup.send("❌ プレイヤーが見つからなかったよ（EA名とプラットフォームを確認してね）")
        else:
            await interaction.followup.send(embed=embed)


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=600)

    # ── Row 0: Apex系 ──────────────────────────────────────
    @discord.ui.button(label="🗺️ ランクマップ", style=discord.ButtonStyle.primary, row=0)
    async def btn_rankmap(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=True)
        try:
            await interaction.followup.send(embed=await build_rankmap_embed())
        except Exception as e:
            await interaction.followup.send(f"❌ 取得失敗: {e}")

    @discord.ui.button(label="🖥️ サーバー状態", style=discord.ButtonStyle.primary, row=0)
    async def btn_apexstatus(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=True)
        try:
            await interaction.followup.send(embed=await build_apexstatus_embed())
        except Exception as e:
            await interaction.followup.send(f"❌ 取得失敗: {e}")

    @discord.ui.button(label="🎯 レジェンド", style=discord.ButtonStyle.secondary, row=0)
    async def btn_apex(self, interaction: discord.Interaction, button: discord.ui.Button):
        legend, catchphrase = random.choice(list(APEX_LEGENDS.items()))
        await interaction.response.send_message(f"🎯 今日のレジェンドは **{legend}** だ！\n> {catchphrase}")

    @discord.ui.button(label="👤 Apex統計", style=discord.ButtonStyle.primary, row=0)
    async def btn_apexstats(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ApexStatsModal())

    # ── Row 1: 遊び系 ──────────────────────────────────────
    @discord.ui.button(label="⚔️ チーム分け", style=discord.ButtonStyle.primary, row=1)
    async def btn_team(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "チーム分けするメンバーを選んでね！", view=TeamSelectView(), ephemeral=True
        )

    @discord.ui.button(label="🎰 ルーレット", style=discord.ButtonStyle.danger, row=1)
    async def btn_roulette(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=True)
        await asyncio.sleep(2)
        result = "🍺 次の集まりで全員分おごり確定！" if interaction.user.id in TARGET_USER_IDS else random.choice(ROULETTES)
        await interaction.followup.send(f"🎰 **結果発表！** {interaction.user.mention}\n{result}")

    @discord.ui.button(label="🍟 マック", style=discord.ButtonStyle.success, row=1)
    async def btn_mac(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=True)
        try:
            embed = await build_mac_embed()
            if embed:
                await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send("❌ メニューの取得に失敗したよ")
        except Exception as e:
            await interaction.followup.send(f"❌ エラー: {e}")

    # ── Row 2: 情報系 ──────────────────────────────────────
    @discord.ui.button(label="📊 サーバー統計", style=discord.ButtonStyle.secondary, row=2)
    async def btn_stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=build_stats_embed(interaction.guild))

    @discord.ui.button(label="🌐 回線速度", style=discord.ButtonStyle.secondary, row=2)
    async def btn_ping(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=True)
        try:
            await interaction.followup.send(embed=await build_ping_embed())
        except Exception as e:
            await interaction.followup.send(f"❌ 測定失敗: {e}")

    @discord.ui.button(label="📰 フェイクニュース", style=discord.ButtonStyle.secondary, row=2)
    async def btn_news(self, interaction: discord.Interaction, button: discord.ui.Button):
        members = [m for m in interaction.guild.members if not m.bot]
        if len(members) < 2:
            await interaction.response.send_message("❌ メンバーが足りないよ！")
            return
        await interaction.response.defer(thinking=True)
        ai_text = None
        if GEMINI_API_KEY:
            history = await scan_messages(interaction.guild)
            names   = [m.display_name for m in members]
            ai_text = await generate_ai_news(history, names)
        if ai_text:
            embed = discord.Embed(title="📰 速報 — 今北Bot通信社", description=ai_text, color=discord.Color.yellow())
            embed.set_footer(text="※ AIがチャット履歴を学習して生成したフィクションです")
        else:
            count = random.randint(2, 3)
            items = random.sample(NEWS_TEMPLATES, min(count, len(NEWS_TEMPLATES)))
            icons = ["📰", "⚡", "🔥", "💥", "🚨"]
            lines = [f"{icons[i % len(icons)]} {fill_template(t, members)}" for i, t in enumerate(items)]
            embed = discord.Embed(title="📰 速報 — 今北Bot通信社", description="\n\n".join(lines), color=discord.Color.yellow())
            embed.set_footer(text="※ この記事はフィクションです")
        await interaction.followup.send(embed=embed)


@bot.command()
async def panel(ctx):
    embed = discord.Embed(
        title="🎮 Bot コントロールパネル",
        description="ボタンを押して機能を使ってね！\n（パネルは10分間有効）",
        color=discord.Color.blurple(),
    )
    await ctx.send(embed=embed, view=PanelView())


# ─────────────────────────────────────────────────────────────────

@bot.command()
async def mac(ctx):
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



@bot.command()
async def stats(ctx):
    await ctx.send(embed=build_stats_embed(ctx.guild))


@bot.command()
async def usage(ctx):
    embed = discord.Embed(
        title="🤖 Botの使い方",
        color=discord.Color.blurple(),
    )
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
        name="🤖 Bot自己紹介",
        value="`!who` でBotのプロフィールを表示",
        inline=False,
    )
    embed.add_field(
        name="🎮 Undertaleバトル",
        value="`!battle` でランダムな敵とバトル開始　`!battle sans` でサンズ戦\nFIGHT/ACT/ITEM/MERCY＋ドッジ回避あり　`!endbattle` で強制終了",
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


@bot.command()
async def apex(ctx):
    legend, catchphrase = random.choice(list(APEX_LEGENDS.items()))
    await ctx.send(f"🎯 今日のレジェンドは **{legend}** だ！\n> {catchphrase}")


APEX_MAP_JA = {
    "World's Edge":  "ワールズエッジ",
    "Storm Point":   "ストームポイント",
    "Broken Moon":   "ブロークンムーン",
    "Kings Canyon":  "キングスキャニオン",
    "Olympus":       "オリンパス",
}


@bot.command()
async def rankmap(ctx):
    if not APEX_API_KEY:
        await ctx.send("❌ APEX_API_KEY が設定されていないよ！")
        return
    try:
        await ctx.send(embed=await build_rankmap_embed())
    except Exception as e:
        await ctx.send(f"❌ 取得に失敗したよ: {e}")


RANK_EMOJI = {
    "Rookie":         "🔰",
    "Bronze":         "🥉",
    "Silver":         "🩶",
    "Gold":           "🥇",
    "Platinum":       "🩵",
    "Diamond":        "💎",
    "Master":         "🟣",
    "Apex Predator":  "🔴",
}

PLATFORM_ALIAS = {"pc": "PC", "ps4": "PS4", "ps": "PS4", "xbox": "X1", "x1": "X1"}


@bot.command()
async def apexstats(ctx, username: str, platform: str = "PC"):
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


@bot.command()
async def apexstatus(ctx):
    if not APEX_API_KEY:
        await ctx.send("❌ APEX_API_KEY が設定されていないよ！")
        return
    try:
        await ctx.send(embed=await build_apexstatus_embed())
    except Exception as e:
        await ctx.send(f"❌ 取得に失敗したよ: {e}")


ROASTS = [
    "こいつの存在意義、マジで誰か教えてくれ",
    "生まれてきた理由を今すぐ神に問い合わせた方がいい",
    "こいつがいると空気の密度が下がる気がする",
    "人生の方向性、完全に迷子やん",
    "こいつの将来を占ったら「霧」って出た",
    "話す内容が毎回5秒で忘れられるレベル",
    "このサーバーの平均IQを下げてる最有力候補",
    "存在がノイズ",
    "こいつのポジション、空気でよくない？",
    "生きてるだけで偉いと思ってそう（褒めてない）",
]

ROULETTES = [
    "🍺 次の集まりで全員分おごり確定！",
    "💪 その場で腕立て20回！",
    "🎤 一発ギャグを披露しろ！",
    "🍜 明日のランチは一人で吉野家",
    "📵 24時間スマホ禁止！",
    "🐔 次のゲームでチキンプレイ縛り",
    "💸 500円募金しろ",
    "🎵 一曲フルで熱唱しろ",
    "🧹 次の集まりの片付けは全部お前",
    "👶 今日一日「〜だにょ」口調で話せ",
    "🤐 1時間無言縛り",
    "🙇 全員に土下座しろ",
]


@bot.command()
async def team(ctx, *members: discord.Member):
    if len(members) < 2:
        await ctx.send("❌ 2人以上メンションしてね！例: `!team @A @B @C @D @E @F`")
        return

    shuffled = list(members)
    random.shuffle(shuffled)
    mid    = (len(shuffled) + 1) // 2
    team_a = shuffled[:mid]
    team_b = shuffled[mid:]

    embed = discord.Embed(title="⚔️ チーム分け結果", color=discord.Color.blue())
    embed.add_field(
        name="🔴 チームA",
        value="\n".join(f"・{m.display_name}" for m in team_a),
        inline=True,
    )
    embed.add_field(
        name="🔵 チームB",
        value="\n".join(f"・{m.display_name}" for m in team_b),
        inline=True,
    )
    if len(members) % 2 != 0:
        embed.set_footer(text="人数が奇数のためチームAに1人多く振り分けました")
    await ctx.send(embed=embed)


APEX_MAPS_JA = [
    "ワールズエッジ", "ストームポイント", "ブロークンムーン",
    "キングスキャニオン", "オリンパス",
]

NEWS_TEMPLATES = [
    "{user}、ランクマ中に突然「もうやめた」と宣言。その後{num}分で復帰し何事もなかったように続行。",
    "{user1}と{user2}、{map}にて同士討ち。両者ともラグを主張し和解の見込みなし。",
    "{user}のK/Dが{kd}を記録。本人は「キャリーしてた」とコメント。",
    "{user}、{legend}に乗り換えを表明。{num}試合で即撤回。",
    "深夜{time}時、{user}が突然VCに現れる。目的不明。",
    "{user}、マスターを目指すと宣言。現在ゴールド{div}。",
    "{user}、{legend}の使い方を「完全に理解した」と豪語。次の試合で開幕死。",
    "{user1}が{user2}のプレイを「下手すぎる」と批評。自身のスタッツは言及せず。",
    "{user}、今シーズンのランクマをお休みすると発表。理由は「メンタル」。",
    "{user}、フルパで参加。チームのドン勝率が{num}割低下したと関係者が語る。",
    "{user1}、{user2}に「ちゃんとやれ」と激怒。自身は先落ちしていた模様。",
    "{user}がVCで「俺のせいじゃない」と{num}回発言。新記録を更新。",
    "{user}、新シーズンから本気を出すと宣言。{num}シーズン連続の発言となる。",
    "{user}、{legend}のウルトを誤発動。味方{num}人を道連れに。",
    "{user1}と{user2}がデュオランク挑戦を表明。{num}試合でスタート地点に戻る。",
    "{user}、「今日は調子いい」と発言した直後に{num}連敗。",
    "{user}、{map}の地形を「覚えた」と自信満々に発言。即迷子になる。",
    "{user}が「絶対キャリーする」と宣言してから{num}時間が経過。続報なし。",
    "{user}、ドン勝の瞬間に回線が切れる。{num}回目。",
    "{user1}が{user2}を蘇生するもその直後に自分が落ちる。",
    "{user}、キャラ変更を検討中と発言。結局{legend}に戻る。",
    "{user}、「次で絶対上がる」と宣言して{num}時間が経過。ランクは変わらず。",
    "{user}がAPEXを「引退する」と発言。翌日ログインを確認。",
    "{user1}と{user2}、声が被りまくるもお互いに謝らず。",
    "{user}、{map}で芋り続けて7位。本人は「戦略」と主張。",
]


def fill_template(template: str, members: list) -> str:
    used: list = []

    def pick():
        pool = [m for m in members if m not in used] or members
        m = random.choice(pool)
        used.append(m)
        return m.display_name

    result = template
    for tag in ["{user1}", "{user2}", "{user}"]:
        if tag in result:
            result = result.replace(tag, pick())
    result = result.replace("{map}",    random.choice(APEX_MAPS_JA))
    result = result.replace("{legend}", random.choice(list(APEX_LEGENDS.keys())))
    result = result.replace("{num}",    str(random.randint(2, 9)))
    result = result.replace("{time}",   str(random.randint(1, 5)))
    result = result.replace("{kd}",     f"{random.uniform(0.1, 1.5):.2f}")
    result = result.replace("{div}",    str(random.randint(1, 4)))
    return result


async def scan_messages(guild: discord.Guild, limit: int = 100) -> str:
    """サーバー内の全テキストチャンネルの直近メッセージを時系列テキストで返す"""
    lines = []
    for ch in guild.text_channels:
        if not ch.permissions_for(guild.me).read_message_history:
            continue
        try:
            async for msg in ch.history(limit=limit):
                if msg.author.bot or not msg.content.strip():
                    continue
                lines.append(f"{msg.author.display_name}: {msg.content}")
        except discord.Forbidden:
            continue
    return "\n".join(reversed(lines))


async def generate_ai_news(history: str, member_names: list[str]) -> str | None:
    """Gemini でメッセージ履歴から内輪ネタニュースを生成する"""
    if not _gemini_client:
        return None
    try:
        prompt = f"""あなたは風刺新聞「今北Bot通信社」の記者です。
日本の風刺サイト「虚構新聞」のスタイルで、以下のDiscordチャット履歴を元に
フィクションのニュース記事を3本書いてください。

メンバー: {', '.join(member_names)}

チャット履歴:
{history[:4000]}

【虚構新聞のスタイル】
・荒唐無稽な出来事を、真面目な報道文体で淡々と伝える
・「〜と明らかになった」「〜との情報が入った」「〜と関係者は語った」「〜と結論付けた」
  などの硬い表現を使う
・見出し形式で1文。主語（人名）＋出来事＋結果のシンプルな構造
・チャットから実際の口癖・出来事を拾い、それを大げさに報道する
・絵文字は使わず、新聞らしい文体を守る

【出力例（虚構新聞スタイル）】
田中氏、「ランクやめる」宣言から2分53秒で復帰　本人は「気が変わった」と説明
鈴木・佐藤両氏、同一の敵に個別突撃し同時撃沈　チーム連携の定義に疑問符
山田氏の「次は本気を出す」発言、今期18回目を更新　周囲はノーコメント

【出力形式】
見出し3本を空行で区切って出力。前置き・説明・コメント等は一切不要。"""
        resp = await asyncio.to_thread(
            _gemini_client.models.generate_content,
            model="gemini-2.5-flash-lite",
            contents=prompt,
        )
        return resp.text.strip()
    except Exception:
        return None


@bot.command()
async def news(ctx):
    members = [m for m in ctx.guild.members if not m.bot]
    if len(members) < 2:
        await ctx.send("❌ メンバーが足りないよ！")
        return

    msg = await ctx.send("📰 ニュースを生成中...")

    ai_text = None
    if GEMINI_API_KEY:
        history  = await scan_messages(ctx.guild)
        names    = [m.display_name for m in members]
        ai_text  = await generate_ai_news(history, names)

    if ai_text:
        embed = discord.Embed(
            title="📰 速報 — 今北Bot通信社",
            description=ai_text,
            color=discord.Color.yellow(),
        )
        embed.set_footer(text="※ AIがチャット履歴を学習して生成したフィクションです")
    else:
        # フォールバック: テンプレート
        count = random.randint(2, 3)
        items = random.sample(NEWS_TEMPLATES, min(count, len(NEWS_TEMPLATES)))
        icons = ["📰", "⚡", "🔥", "💥", "🚨"]
        lines = [f"{icons[i % len(icons)]} {fill_template(t, members)}" for i, t in enumerate(items)]
        embed = discord.Embed(
            title="📰 速報 — 今北Bot通信社",
            description="\n\n".join(lines),
            color=discord.Color.yellow(),
        )
        embed.set_footer(text="※ この記事はフィクションです")

    await msg.delete()
    await ctx.send(embed=embed)


@bot.command()
async def roast(ctx, member: discord.Member):
    roast_text = random.choice(ROASTS)
    await ctx.send(f"🔥 {member.mention}　→　{roast_text}")


@bot.command()
async def roulette(ctx, member: discord.Member = None):
    target = member or ctx.author
    msg = await ctx.send("🎰 ルーレット回転中...")
    await asyncio.sleep(2)
    if target.id in TARGET_USER_IDS:
        result = "🍺 次の集まりで全員分おごり確定！"
    else:
        result = random.choice(ROULETTES)
    await msg.edit(content=f"🎰 **結果発表！** {target.mention}\n{result}")


@bot.command()
async def ping(ctx):
    msg = await ctx.send("🌐 通信速度を測定中... しばらく待ってね（10〜20秒かかるよ）")
    try:
        embed = await build_ping_embed()
        await msg.delete()
        await ctx.send(embed=embed)
    except Exception as e:
        await msg.edit(content=f"❌ 測定に失敗しました: {e}")


def parse_duration(text):
    """'1h30m20s' などの文字列を秒数に変換する"""
    match = re.fullmatch(r'(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?', text.strip())
    if not match or not any(match.groups()):
        return None
    hours   = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    total   = hours * 3600 + minutes * 60 + seconds
    return total if total > 0 else None


def format_duration(seconds):
    """秒数を '1時間30分20秒' 形式の文字列に変換する"""
    h, rem = divmod(seconds, 3600)
    m, s   = divmod(rem, 60)
    parts  = []
    if h: parts.append(f"{h}時間")
    if m: parts.append(f"{m}分")
    if s: parts.append(f"{s}秒")
    return "".join(parts)


def parse_clock_time(text):
    """'15時' '15時30分' '15時から' '15:30' などを受け取り、(秒数, 表示文字列) を返す。
    解析できない場合は None を返す。"""
    text = text.strip()
    # 〇時 / 〇時〇分 / 〇時から / 〇時〇分から
    m = re.fullmatch(r'(\d{1,2})時(?:(\d{1,2})分)?(?:から)?', text)
    if not m:
        # HH:MM または HH:MM から
        m2 = re.fullmatch(r'(\d{1,2}):(\d{2})(?:から)?', text)
        if not m2:
            return None
        hour, minute = int(m2.group(1)), int(m2.group(2))
    else:
        hour   = int(m.group(1))
        minute = int(m.group(2) or 0)

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    now    = datetime.datetime.now(JST)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)

    seconds  = int((target - now).total_seconds())
    time_str = f"{hour}時" + (f"{minute}分" if minute else "")
    return seconds, time_str


def extract_clock_time(text):
    """メッセージ全体から '〇時' '〇時〇分から' パターンを探して
    (秒数, 表示文字列, target_datetime) を返す。見つからない場合は None を返す。"""
    m = re.search(r'(\d{1,2})時(?:(\d{1,2})分)?(?:から)?', text)
    if not m:
        return None
    hour   = int(m.group(1))
    minute = int(m.group(2) or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    now    = datetime.datetime.now(JST)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    seconds  = int((target - now).total_seconds())
    time_str = f"{hour}時" + (f"{minute}分" if minute else "")
    return seconds, time_str, target


@bot.command()
async def timer(ctx, duration: str):
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


@bot.command()
async def who(ctx):
    embed = discord.Embed(
        title="🤖 自己紹介",
        color=discord.Color.og_blurple(),
    )
    embed.add_field(name="名前", value="今北尚杜", inline=True)
    embed.add_field(name="住所", value="神戸市", inline=True)
    embed.add_field(name="職業", value="職なし子供部屋おじさんだにょ", inline=False)
    embed.add_field(name="見た目", value="台パンチビ眼鏡", inline=False)
    embed.add_field(name="役職", value="このサーバーの奴隷です", inline=False)
    embed.add_field(name="特技", value="破壊と近親相姦", inline=False)
    await ctx.send(embed=embed)


@bot.command()
async def hello(ctx):
    await ctx.send(f"こんにちは、{ctx.author.name}さん！")


# ── Undertale風バトル ────────────────────────────────────────────

UT_ENEMIES = [
    {
        "name": "フロギット",
        "hp": 12, "atk": 3,
        "flavor": "なんかぴょんぴょん跳んでいる。",
        "acts": {
            "なでる": (True,  "* フロギットはうっとりしている。"),
            "みる":   (False, "* ATK 3  DEF 0\n  かわいいカエル。なぜここにいるのか。"),
        },
        "attack_lines": [
            "* フロギットが体当たりしてきた！",
            "* フロギットがぴょんと跳びかかった！",
        ],
        "spare_line": "* フロギットは跳んで逃げた。よかった。",
    },
    {
        "name": "Napstablook",
        "hp": 1, "atk": 2,
        "flavor": "ゆうれいのDJです。",
        "acts": {
            "なぐさめる": (True,  "* Napstablookは少し元気になった。"),
            "きく":       (True,  "* 音楽に耳を傾けた。悪くない。"),
            "みる":       (False, "* ATK 2  DEF 0\n  ゆうれい。"),
        },
        "attack_lines": [
            "* Napstablookの音波攻撃！",
            "* なんとなく攻撃してきた。",
        ],
        "spare_line": "* Napstablookはゆっくり消えていった。\n  あ、ありがとう...",
    },
    {
        "name": "トリエル",
        "hp": 20, "atk": 4,
        "flavor": "おかあさんみたいな人。",
        "acts": {
            "はなす": (True,  "* トリエルはやさしく微笑んだ。"),
            "ほめる": (True,  "* トリエルは照れくさそうにした。"),
            "みる":   (False, "* ATK 4  DEF 4\n  ゴートマム。パイが得意。"),
        },
        "attack_lines": [
            "* トリエルが炎魔法を放った！",
            "* トリエルが魔法陣を展開した！",
        ],
        "spare_line": "* トリエルは優しく笑って手を振った。\n  「気をつけてね、我が子よ。」",
    },
    {
        "name": "サンズ",
        "hp": 1, "atk": 12,
        "flavor": "zzz...",
        "acts": {
            "みる":       (False, "* ATK ？？？  DEF ？？？\n  「いや、だいじょうぶ。」"),
            "うったえる": (False, "* サンズは聞いていない。\n  「よく　しゃべるな、おまえ。」"),
            "あくしゅ":   (False, "* サンズは手を差し伸べた。\n  ……冷たかった。"),
        },
        "attack_lines": [
            "* サンズが骨を投げてきた！",
            "* 骨が追ってくる！　KARMA！",
            "* 「bad time だね。」",
            "* GASTERBLASTERが発射された！",
            "* 「おまえは、ほんとうに　あきらめないんだな。」",
            "* 重力が反転した！",
            "* 「そろそろ　あきらめてくれよ。」",
        ],
        "attack_lines_wave2": [
            "* まだ続くぞ！",
            "* 骨が追い打ちをかける！",
            "* もう一発GASTERBLASTERだ！",
            "* 「ほら、まだだ。」",
            "* 反転した骨が来る！",
        ],
        "spare_line": "* サンズはニッと笑った。\n  「まあ、こうなることは　わかってたけど。」\n  そして消えた。",
        "special": True,
    },
]

active_battles: dict[int, dict] = {}


def ut_hp_bar(cur: int, max_: int, width: int = 10) -> str:
    filled = max(0, round(cur / max_ * width))
    return "█" * filled + "░" * (width - filled)


def build_ut_embed(state: dict) -> discord.Embed:
    enemy   = state["enemy"]
    e_hp    = state["enemy_hp"]
    e_max   = enemy["hp"]
    p_hp    = state["player_hp"]
    p_max   = state["player_max_hp"]
    love    = state["love"]
    is_sans = enemy.get("special", False)

    color = discord.Color.from_rgb(0, 0, 0) if is_sans else discord.Color.dark_red()
    embed = discord.Embed(color=color)

    if is_sans:
        tired = state.get("sans_tired", 0)
        tired_labels = [
            "zZZ...",
            "少し汗をかいている...",
            "かなり疲れてきた...",
            "💤 眠そうだ...  **今がチャンス！**",
        ]
        tired_bar = "■" * tired + "□" * (3 - tired)
        embed.add_field(
            name="💀 サンズ",
            value=f"HP: `？？？`\n疲労: `{tired_bar}` {tired_labels[tired]}",
            inline=False,
        )
    else:
        embed.add_field(
            name=f"👾 {enemy['name']}",
            value=f"`{ut_hp_bar(e_hp, e_max)}` {e_hp}/{e_max} HP",
            inline=False,
        )

    embed.add_field(
        name="💬",
        value=state.get("msg_text", f"* {enemy['name']}があらわれた！\n  {enemy['flavor']}"),
        inline=False,
    )
    hearts = "❤️" if p_hp > p_max * 0.5 else ("🧡" if p_hp > p_max * 0.2 else "💛")
    embed.add_field(
        name=f"{hearts} {state['player'].display_name}",
        value=(
            f"`{ut_hp_bar(p_hp, p_max)}` {p_hp}/{p_max} HP"
            + (f"\n💛 LOVE {love}/2" if love > 0 else "")
            + f"\n🍫 スパイシードッグ ×{state['items']}"
        ),
        inline=False,
    )
    return embed


class UtActView(discord.ui.View):
    def __init__(self, state: dict):
        super().__init__(timeout=30)
        self.state = state
        for act_name in state["enemy"]["acts"]:
            btn = discord.ui.Button(label=act_name, style=discord.ButtonStyle.primary)
            btn.callback = self._make_cb(act_name)
            self.add_item(btn)
        back = discord.ui.Button(label="← もどる", style=discord.ButtonStyle.secondary)
        back.callback = self._back
        self.add_item(back)

    def _make_cb(self, act_name: str):
        async def cb(interaction: discord.Interaction):
            if interaction.user.id != self.state["player"].id:
                await interaction.response.send_message("あなたのバトルじゃないよ！", ephemeral=True)
                return
            raises_love, text = self.state["enemy"]["acts"][act_name]
            self.state["msg_text"] = text
            if raises_love:
                self.state["love"] = min(2, self.state["love"] + 1)
            self.stop()
            await _ut_enemy_attack(interaction, self.state)
        return cb

    async def _back(self, interaction: discord.Interaction, button):
        if interaction.user.id != self.state["player"].id:
            await interaction.response.send_message("あなたのバトルじゃないよ！", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(embed=build_ut_embed(self.state), view=UtMainView(self.state))


class UtDodgeView(discord.ui.View):
    def __init__(self, state: dict, safe: str, wave: int = 1):
        super().__init__(timeout=4)
        self.state  = state
        self.safe   = safe
        self.wave   = wave
        self.picked: str | None = None
        for d in ["⬆️", "⬇️", "⬅️", "➡️"]:
            btn = discord.ui.Button(label=d, style=discord.ButtonStyle.secondary)
            btn.callback = self._make_cb(d)
            self.add_item(btn)

    def _make_cb(self, direction: str):
        async def cb(interaction: discord.Interaction):
            if interaction.user.id != self.state["player"].id:
                await interaction.response.send_message("あなたのバトルじゃないよ！", ephemeral=True)
                return
            self.picked = direction
            self.stop()
            await self._resolve(interaction)
        return cb

    async def on_timeout(self):
        await self._resolve(None)

    async def _resolve(self, interaction):
        state  = self.state
        enemy  = state["enemy"]
        dodged = self.picked == self.safe

        # サンズ第1波回避 → 第2波へ
        if dodged and enemy.get("special") and self.wave == 1:
            safe2  = random.choice(["⬆️", "⬇️", "⬅️", "➡️"])
            atk2   = random.choice(enemy.get("attack_lines_wave2", enemy["attack_lines"]))
            wave2  = UtDodgeView(state, safe2, wave=2)
            embed  = build_ut_embed(state)
            embed.set_field_at(1, name="💬", value=f"* 第1波を回避！\n{atk2}\n\n**4秒で回避！**", inline=False)
            if interaction:
                await interaction.response.edit_message(embed=embed, view=wave2)
                state["battle_msg"] = await interaction.original_response()
            else:
                try:
                    await state["battle_msg"].edit(embed=embed, view=wave2)
                except Exception:
                    pass
            return

        if dodged:
            state["msg_text"] = "* うまく回避した！"
        else:
            dmg = enemy["atk"]
            if enemy.get("special"):
                karma_msgs = [
                    f"* KARMA！　{dmg}ダメージ！\n  「あきらめろよ。」",
                    f"* 食らった！　{dmg}ダメージ！\n  「ははっ。」",
                    f"* {dmg}ダメージ！\n  「まだまだ。」",
                ]
                state["msg_text"] = random.choice(karma_msgs)
            elif self.picked is None:
                state["msg_text"] = f"* 回避できなかった！　{dmg}ダメージ！"
            else:
                state["msg_text"] = f"* 避け損ねた！　{dmg}ダメージ！"
            state["player_hp"] = max(0, state["player_hp"] - dmg)

        embed = build_ut_embed(state)

        if state["player_hp"] <= 0:
            if enemy.get("special"):
                embed.description = "**★ GAME OVER ★**\n* 「おつかれ。でも、わかってたろ？」"
            else:
                embed.description = "**★ GAME OVER ★**\n* でも、あきらめないで。"
            active_battles.pop(state["player"].id, None)
            next_view = None
        else:
            next_view = UtMainView(state)

        if interaction:
            await interaction.response.edit_message(embed=embed, view=next_view)
        else:
            try:
                await state["battle_msg"].edit(embed=embed, view=next_view)
            except Exception:
                pass


class UtMainView(discord.ui.View):
    def __init__(self, state: dict):
        super().__init__(timeout=60)
        self.state = state

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.state["player"].id:
            await interaction.response.send_message("あなたのバトルじゃないよ！", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="⚔️ FIGHT", style=discord.ButtonStyle.danger)
    async def fight(self, interaction: discord.Interaction, button):
        state = self.state
        enemy = state["enemy"]

        if enemy.get("special"):
            fight_count = state.get("fight_count", 0) + 1
            state["fight_count"] = fight_count
            state["sans_tired"] = min(3, fight_count // 3)

            if state["sans_tired"] >= 3:
                embed = build_ut_embed(state)
                embed.description = "* サンズを倒した。\n  「ふん…　まあ、よくやった…　ほんとうに…。」\n  zzz..."
                active_battles.pop(state["player"].id, None)
                await interaction.response.edit_message(embed=embed, view=None)
            else:
                dodge_msgs = [
                    "* サンズは避けた。\n  「そうくると思ってた。」",
                    "* ミスした！　サンズはにやりとした。",
                    "* サンズは骨でガードした。",
                    "* 攻撃が当たらない！\n  「heh。」",
                    "* サンズが姿を消した。\n  「速すぎたか？」",
                    "* 「まだそれくらいじゃ　当たらないよ。」",
                ]
                state["msg_text"] = random.choice(dodge_msgs)
                await _ut_enemy_attack(interaction, state)
        else:
            atk = random.randint(3, 8)
            state["enemy_hp"] = max(0, state["enemy_hp"] - atk)
            if state["enemy_hp"] <= 0:
                embed = build_ut_embed(state)
                embed.description = f"* {enemy['name']}を倒した。\n  ...なにかが、かわった。"
                active_battles.pop(state["player"].id, None)
                await interaction.response.edit_message(embed=embed, view=None)
                return
            state["msg_text"] = f"* {atk}のダメージを与えた！"
            await _ut_enemy_attack(interaction, state)

    @discord.ui.button(label="✨ ACT", style=discord.ButtonStyle.primary)
    async def act(self, interaction: discord.Interaction, button):
        embed = build_ut_embed(self.state)
        embed.set_field_at(1, name="💬", value="* なにをする？", inline=False)
        await interaction.response.edit_message(embed=embed, view=UtActView(self.state))

    @discord.ui.button(label="🍫 ITEM", style=discord.ButtonStyle.success)
    async def item(self, interaction: discord.Interaction, button):
        state = self.state
        if state["items"] <= 0:
            state["msg_text"] = "* アイテムがない！"
        else:
            heal = 10
            state["player_hp"] = min(state["player_max_hp"], state["player_hp"] + heal)
            state["items"] -= 1
            state["msg_text"] = f"* スパイシードッグを食べた。HP +{heal}！"
        await _ut_enemy_attack(interaction, state)

    @discord.ui.button(label="🏳️ MERCY", style=discord.ButtonStyle.secondary)
    async def mercy(self, interaction: discord.Interaction, button):
        state = self.state
        enemy = state["enemy"]

        if enemy.get("special"):
            mercy_count = state.get("mercy_count", 0) + 1
            state["mercy_count"] = mercy_count
            mercy_msgs = [
                "* サンズはMERCYを受け入れない。\n  「悪いけど、そうはいかない。」",
                "* また？\n  「…まじめに　きいてるのか？」",
                "* 「ははっ。　おまえ、　あきらめないんだな。」",
                "* 「わかった。おれも　もう　すこし　がんばるよ。」",
                "* サンズはあくびをした。\n  「もう　おわりにしようぜ。」",
            ]
            state["msg_text"] = mercy_msgs[min(mercy_count - 1, len(mercy_msgs) - 1)]
            await _ut_enemy_attack(interaction, state)
        elif state["love"] >= 2:
            embed = build_ut_embed(state)
            embed.description = enemy["spare_line"]
            active_battles.pop(state["player"].id, None)
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            remaining = 2 - state["love"]
            state["msg_text"] = f"* まだ仲直りする気になっていない。\n  （ACTであと{remaining}回仲良くなろう）"
            await _ut_enemy_attack(interaction, state)


async def _ut_enemy_attack(interaction: discord.Interaction, state: dict):
    enemy    = state["enemy"]
    safe     = random.choice(["⬆️", "⬇️", "⬅️", "➡️"])
    atk_line = random.choice(enemy["attack_lines"])

    if enemy.get("special"):
        dodge_view = UtDodgeView(state, safe, wave=1)
    else:
        dodge_view = UtDodgeView(state, safe)

    embed = build_ut_embed(state)
    embed.set_field_at(1, name="💬", value=f"{atk_line}\n\n**4秒で回避方向を選べ！**", inline=False)
    await interaction.response.edit_message(embed=embed, view=dodge_view)
    state["battle_msg"] = await interaction.original_response()


@bot.command()
async def battle(ctx, target: str = None):
    """Undertale風バトルを開始する（!battle sans でサンズ戦）"""
    uid = ctx.author.id
    if uid in active_battles:
        await ctx.send("❌ すでにバトル中だよ！`!endbattle` で終了できる。")
        return

    if target and target.lower() in ("sans", "サンズ"):
        enemy = next(e for e in UT_ENEMIES if e.get("special"))
    else:
        enemy = random.choice(UT_ENEMIES)

    is_sans      = enemy.get("special", False)
    player_max   = 35 if is_sans else 20
    items        = 3  if is_sans else 2
    sans_intro   = "* 「よお。\n  いい　bad time　してるか？」"
    state = {
        "player":        ctx.author,
        "player_hp":     player_max,
        "player_max_hp": player_max,
        "items":         items,
        "enemy":         enemy,
        "enemy_hp":      enemy["hp"],
        "love":          0,
        "msg_text":      sans_intro if is_sans else f"* {enemy['name']}があらわれた！\n  {enemy['flavor']}",
        "battle_msg":    None,
    }
    active_battles[uid] = state

    embed = build_ut_embed(state)
    msg = await ctx.send(embed=embed, view=UtMainView(state))
    state["battle_msg"] = msg


@bot.command()
async def endbattle(ctx):
    if ctx.author.id in active_battles:
        active_battles.pop(ctx.author.id)
        await ctx.send("* バトルを強制終了した。")
    else:
        await ctx.send("バトル中じゃないよ！")


# ─── 絵文字作成 ──────────────────────────────────────────────────

@bot.command()
async def emoji(ctx, name: str = None, url: str = None):
    """!emoji <名前> [画像URL] — サーバーにカスタム絵文字を追加する"""
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

    # 名前のバリデーション（英数字とアンダーバーのみ、2文字以上）
    if not re.fullmatch(r"[a-zA-Z0-9_]{2,32}", name):
        await ctx.send("❌ 絵文字の名前は英数字・アンダーバーのみ、2〜32文字で指定してね。")
        return

    # 画像ソースの決定（URL優先、なければ添付ファイル）
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
        await msg.edit(content=f"✅ 絵文字 {new_emoji} `:{ name}:` を追加したよ！")
    except discord.HTTPException as e:
        if e.code == 30008:
            await msg.edit(content="❌ サーバーの絵文字スロットが満杯だよ！")
        elif e.code == 50138:
            await msg.edit(content="❌ 画像サイズが大きすぎるよ（256KB以下にしてね）")
        else:
            await msg.edit(content=f"❌ 絵文字の作成に失敗したよ: {e.text}")
    except Exception as e:
        await msg.edit(content=f"❌ エラーが発生したよ: {e}")


# ─────────────────────────────────────────────────────────────────

bot.run(TOKEN)
