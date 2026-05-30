import os
import re
import random
import asyncio
import datetime
import aiohttp
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
ALLOWED_CHANNEL_IDS = [1509231531326181406]

# 募集投票のメッセージID → {"channel_id": int, "start_at": datetime | None}
recruit_polls: dict[int, dict] = {}

# 原神 特産品辞書 {特産品名: (地域, 場所)}
GENSHIN_TOKUSANHIN = {
    # ── モンド ──
    "セシリアの花":           ("🏔️ モンド", "スタースネイルの丘（モンド西側の崖）"),
    "ダンデライオンの種":     ("🏔️ モンド", "モンド城周辺・風の通り道"),
    "カラスの眼":             ("🏔️ モンド", "嵐の丘・モンド近郊"),
    "風車アスター":         ("🏔️ モンド", "風の通り道・キャロメイ農場周辺"),
    "フィラネモキノコ":       ("🏔️ モンド", "モンドシティの建物の壁・屋根の上"),
    "ヴァレベリー":           ("🏔️ モンド", "嵐の丘・スタースネイルの丘北部"),
    # ── 璃月 ──
    "琉璃百合":               ("🏮 璃月", "璃月港・仙境台周辺"),
    "絹の花":                 ("🏮 璃月", "璃月港・仙境台"),
    "夜泊石":                 ("🏮 璃月", "辰砂の荒地・嶃岩の野の洞窟内"),
    "青心":                   ("🏮 璃月", "玉京台・天頂付近の高峰"),
    "紫羅蘭":                 ("🏮 璃月", "辰砂の荒地・岩の海の崖"),
    "コアラピス":             ("🏮 璃月", "辰砂の荒地・嶃岩の野"),
    "ジュエユンチリ":         ("🏮 璃月", "玉京台周辺"),
    "狼牙草":                 ("🏮 璃月", "石門付近・岩の海"),
    # ── 稲妻 ──
    "桜のまどい":             ("⛩️ 稲妻", "鳴神大社周辺・神聖な桜の木の近く"),
    "海霊芝":                 ("⛩️ 稲妻", "稲妻各地の海岸沿い"),
    "珊瑚真珠草":             ("⛩️ 稲妻", "シメナワの伝説・壁奥島の海沿い"),
    "天雲草の実":             ("⛩️ 稲妻", "鳴神島・雷嵐の目周辺"),
    "筑紫の草木実":           ("⛩️ 稲妻", "筑紫島各地"),
    "夜来香":                 ("⛩️ 稲妻", "渡鳥の島・鎧島"),
    "伝説の霊光":             ("⛩️ 稲妻", "エンカノミヤ"),
    # ── スメール ──
    "パドイサンジの花":       ("🌿 スメール", "スメール雨林各地"),
    "ニルヴァーナの蓮":       ("🌿 スメール", "清浄の境内・川や池の水辺"),
    "カルパラタの蓮":         ("🌿 スメール", "スメール城周辺・高地"),
    "ヘナの実":               ("🌿 スメール", "砂漠地帯・アル＝アヒマル"),
    "スカラベ":               ("🌿 スメール", "アル＝アヒマル砂漠・砂の中"),
    "哀悼の花":               ("🌿 スメール", "枯れた砂漠・廃墟周辺"),
    "砂脂の幼虫":             ("🌿 スメール", "砂漠地帯の地面"),
    "ルッカサヴァキノコ":     ("🌿 スメール", "チェルビの森・雨林地帯"),
    # ── フォンテーヌ ──
    "ロマリタイムの花":       ("💧 フォンテーヌ", "フォンテーヌ各地の草原・水辺"),
    "ベリルの巻貝":           ("💧 フォンテーヌ", "フォンテーヌ海岸・水中"),
    "幻日のサンゴ":           ("💧 フォンテーヌ", "フォンテーヌ水中・海底"),
    "ルミトワール":           ("💧 フォンテーヌ", "エレシュナイル・地下水域"),
    "驚奇の蘭":               ("💧 フォンテーヌ", "フォンテーヌ草原地帯"),
    "眠れる幼虫":             ("💧 フォンテーヌ", "メロピド要塞周辺・水辺"),
    # ── ナタ ──
    "ナタの草花":             ("🔥 ナタ", "ナタ草原・サウリア平原"),
    "溶岩水晶":               ("🔥 ナタ", "ナタ火山地帯・溶岩地帯"),
    "凝結した炎の蝶":         ("🔥 ナタ", "ナタ各地の炎の生息地"),
    "霜峰の氷蘚":             ("🔥 ナタ", "霜峰の境・氷雪地帯"),
    "影纏いの葦":             ("🔥 ナタ", "ナタ湿地帯・水辺"),
}

GENSHIN_PATTERN = re.compile("|".join(re.escape(k) for k in sorted(GENSHIN_TOKUSANHIN.keys(), key=len, reverse=True)))

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
            seconds    = max(0, int((notify_at - datetime.datetime.now()).total_seconds()))
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

    match_g = GENSHIN_PATTERN.search(message.content)
    if match_g:
        name = match_g.group()
        region, location = GENSHIN_TOKUSANHIN[name]
        embed = discord.Embed(
            title=f"🗺️ {name} の採取場所",
            color=discord.Color.orange(),
        )
        embed.add_field(name="地域", value=region, inline=True)
        embed.add_field(name="場所", value=location, inline=False)
        embed.set_footer(text="地図ボタンから現地確認できます")
        view = discord.ui.View()
        map_url = f"https://act.hoyolab.com/ys/app/interactive-map/#/map/2?lang=ja-jp"
        view.add_item(discord.ui.Button(label="🗺️ HoYoLABマップで見る", url=map_url, style=discord.ButtonStyle.link))
        await message.channel.send(embed=embed, view=view)

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
        name="🏓 ping確認",
        value="`!ping` でBotの応答速度を確認\nping値によって煽りや褒めが変わる",
        inline=False,
    )
    embed.add_field(
        name="📊 ポケモン種族値",
        value="ポケモンの名前を含む発言をすると\n自動で種族値を表示",
        inline=False,
    )
    embed.add_field(
        name="🗺️ 原神 特産品の場所",
        value="原神の特産品名を含む発言をすると\n採取場所とマップへのリンクを表示",
        inline=False,
    )
    embed.add_field(
        name="⏱️ タイマー",
        value="`!timer 10m` `!timer 1h30m` `!timer 30s`\n`!timer 15時から` `!timer 15時30分から` など\n時間になったらメンションで呼び出す",
        inline=False,
    )
    embed.add_field(
        name="🎯 Apexレジェンド",
        value="`!apex` でランダムにレジェンドを1人選出\nディスりキャッチコピー付き",
        inline=False,
    )
    embed.add_field(
        name="🔥 煽り",
        value="`!roast @ユーザー` で指定した人を煽る",
        inline=False,
    )
    embed.add_field(
        name="🎰 罰ゲームルーレット",
        value="`!roulette` でランダムに罰ゲームを決定\n※特定ユーザーは必ずおごり確定",
        inline=False,
    )
    embed.add_field(
        name="🤖 Bot自己紹介",
        value="`!who` でBotのプロフィールを表示",
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
async def roast(ctx, member: discord.Member):
    roast_text = random.choice(ROASTS)
    await ctx.send(f"🔥 {member.mention}　→　{roast_text}")


@bot.command()
async def roulette(ctx):
    msg = await ctx.send("🎰 ルーレット回転中...")
    await asyncio.sleep(2)
    if ctx.author.id in TARGET_USER_IDS:
        result = "🍺 次の集まりで全員分おごり確定！"
    else:
        result = random.choice(ROULETTES)
    await msg.edit(content=f"🎰 **結果発表！** {ctx.author.mention}\n{result}")


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

    now    = datetime.datetime.now()
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
    now    = datetime.datetime.now()
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
