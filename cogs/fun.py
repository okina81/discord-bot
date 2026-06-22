import random
import asyncio
from urllib.parse import quote
import discord
from discord.ext import commands
from config import TARGET_USER_IDS, GEMINI_API_KEY, gemini_client
from cogs.apex import APEX_LEGENDS

APEX_MAPS_JA = [
    "ワールズエッジ", "ストームポイント", "ブロークンムーン",
    "キングスキャニオン", "オリンパス",
]

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

JP_TRENDS = [
    # 2013
    "激おこぷんぷん丸", "倍返しだ!", "今でしょ!", "じぇじぇじぇ",
    "お・も・て・な・し", "マカンコウサッポウ", "アベノミクス",
    # 2014
    "壁ドン", "ダメよ〜ダメダメ", "ありのままで", "妖怪ウォッチ",
    "レリゴー", "STAP細胞はあります", "バケツチャレンジ",
    # 2015
    "安心してください、穿いてますよ", "ラッスンゴレライ", "五郎丸ポーズ",
    "爆買い", "ドローン", "エンブレム問題",
    # 2016
    "PPAP", "ポケモンGO", "神ってる", "ゲス不倫",
    "センテンススプリング", "シン・ゴジラ", "君の名は。",
    # 2017
    "5000兆円欲しい!", "このハゲーーー!", "忖度", "インスタ映え",
    "けものフレンズ", "すごーい!君は○○なフレンズなんだね!",
    "35億", "プレミアムフライデー",
    # 2018
    "大迫半端ないって", "クッパ姫", "そだねー", "eスポーツ",
    "バーチャルYouTuber", "キズナアイ", "もぐもぐタイム",
    "ボーッと生きてんじゃねーよ!", "TikTok", "グリッドマン",
    # 2019
    "令和", "タピオカ", "上級国民", "NHKをぶっ壊す",
    "闇営業", "ONE TEAM", "計画通り", "ハンドスピナー",
    "笑ってはいけない", "100日後に死ぬワニ",
    # 2020
    "あつまれどうぶつの森", "鬼滅の刃", "全集中の呼吸",
    "三密", "密です", "アマビエ", "ソーシャルディスタンス",
    "鬼滅の刃 無限列車編", "Zoom映え", "アベノマスク",
    # 2021
    "うっせぇわ", "親ガチャ", "ゴン攻め", "黙食",
    "GetWild退勤", "ピクトグラム", "リアル二刀流",
    "ウマ娘", "Z世代", "推し活", "人流",
    # 2022
    "おじさん構文", "ちいかわ", "知らんけど", "それってあなたの感想ですよね",
    "ぼっち・ざ・ろっく!", "きつねダンス", "村神様",
    "スパイファミリー", "ヌン活", "タイパ", "悪い顔",
    # 2023
    "推しの子", "蛙化現象", "ひき肉です", "生成AI", "アレ(A.R.E.)",
    "なぁぜなぁぜ", "かわちい", "ChatGPT", "闇バイト",
    "スイカゲーム", "新しい学校のリーダーズ", "アイドル(YOASOBI)",
    # 2024
    "葬送のフリーレン", "ふてにゃん", "でこぴん", "界隈",
    "Bling-Bang-Bang-Born", "猫ミーム", "はいよろこんで",
    "名探偵コナン 100万ドルの五稜星", "裏金問題",
    "50-50", "ふてほど", "もうええでしょう", "BeReal",
    # 2025
    "石丸構文", "マイナ保険証", "ルックバック", "学マス",
    "令和ロマン", "チームラボ", "ジャンボリミッキー",
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
    if not gemini_client:
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
            gemini_client.models.generate_content,
            model="gemini-2.5-flash-lite",
            contents=prompt,
        )
        return resp.text.strip()
    except Exception:
        return None


class Fun(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def roast(self, ctx, member: discord.Member):
        roast_text = random.choice(ROASTS)
        await ctx.send(f"🔥 {member.mention}　→　{roast_text}")

    @commands.command()
    async def roulette(self, ctx, member: discord.Member = None):
        target = member or ctx.author
        msg = await ctx.send("🎰 ルーレット回転中...")
        await asyncio.sleep(2)
        if target.id in TARGET_USER_IDS:
            result = "🍺 次の集まりで全員分おごり確定！"
        else:
            result = random.choice(ROULETTES)
        await msg.edit(content=f"🎰 **結果発表！** {target.mention}\n{result}")

    @commands.command()
    async def team(self, ctx, *members: discord.Member):
        if len(members) < 2:
            await ctx.send("❌ 2人以上メンションしてね！例: `!team @A @B @C @D @E @F`")
            return
        shuffled = list(members)
        random.shuffle(shuffled)
        mid = (len(shuffled) + 1) // 2
        team_a = shuffled[:mid]
        team_b = shuffled[mid:]
        embed = discord.Embed(title="⚔️ チーム分け結果", color=discord.Color.blue())
        embed.add_field(name="🔴 チームA", value="\n".join(f"・{m.display_name}" for m in team_a), inline=True)
        embed.add_field(name="🔵 チームB", value="\n".join(f"・{m.display_name}" for m in team_b), inline=True)
        if len(members) % 2 != 0:
            embed.set_footer(text="人数が奇数のためチームAに1人多く振り分けました")
        await ctx.send(embed=embed)

    @commands.command()
    async def trend(self, ctx):
        name = random.choice(JP_TRENDS)
        url = f"https://dic.pixiv.net/a/{quote(name, safe='')}"
        embed = discord.Embed(title=name, url=url, color=discord.Color.blue())
        if gemini_client:
            try:
                resp = await asyncio.to_thread(
                    gemini_client.models.generate_content,
                    model="gemini-2.5-flash-lite",
                    contents=(
                        f"「{name}」という日本のネット流行語・トレンドについて、"
                        "以下の形式で書いてください。\n\n"
                        "1行目: 当時の空気感が伝わるような懐かしくて面白い解説（2〜3文）。"
                        "「あの頃みんな○○してたよね」「TLが○○で埋め尽くされてた」のような、"
                        "当時を知ってる人が思わず「あったあったw」となるノリで書くこと。\n"
                        "2行目: 空行\n"
                        "3行目: 🗓️ 流行った時期（例: 2016年頃）"
                    ),
                )
                embed.description = resp.text.strip()
            except Exception:
                pass
        embed.set_footer(text="出典: ピクシブ百科事典")
        await ctx.send(embed=embed)

    @commands.command()
    async def news(self, ctx):
        members = [m for m in ctx.guild.members if not m.bot]
        if len(members) < 2:
            await ctx.send("❌ メンバーが足りないよ！")
            return
        msg = await ctx.send("📰 ニュースを生成中...")
        ai_text = None
        if GEMINI_API_KEY:
            history = await scan_messages(ctx.guild)
            names = [m.display_name for m in members]
            ai_text = await generate_ai_news(history, names)
        if ai_text:
            embed = discord.Embed(
                title="📰 速報 — 今北Bot通信社",
                description=ai_text,
                color=discord.Color.yellow(),
            )
            embed.set_footer(text="※ AIがチャット履歴を学習して生成したフィクションです")
        else:
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


async def setup(bot):
    await bot.add_cog(Fun(bot))
