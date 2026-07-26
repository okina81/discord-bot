import asyncio
import base64
import difflib
import json
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands
from config import (
    PALWORLD_API_PASSWORD, PALWORLD_API_URL, PALWORLD_API_USER, PALWORLD_SERVER_ADDRESS,
)

DATA_PATH = Path(__file__).parent.parent / "data" / "palworld.json"

ELEMENTS = {
    "Normal":        ("無属性", "⚪"),
    "Fire":          ("炎属性", "🔥"),
    "Water":         ("水属性", "💧"),
    "Leaf":          ("草属性", "🌿"),
    "Electricity":   ("雷属性", "⚡"),
    "Ice":           ("氷属性", "❄️"),
    "Earth":         ("地属性", "🪨"),
    "Dark":          ("闇属性", "🌑"),
    "Dragon":        ("竜属性", "🐉"),
}

# 「その属性が弱点とする属性」= その属性のパルに大ダメージを与えられる属性
WEAK_TO = {
    "Normal":      "Dark",
    "Fire":        "Water",
    "Water":       "Electricity",
    "Leaf":        "Fire",
    "Electricity": "Earth",
    "Ice":         "Fire",
    "Earth":       "Leaf",
    "Dark":        "Dragon",
    "Dragon":      "Ice",
}

WORKS = {
    "EmitFlame":           ("火起こし", "🔥"),
    "Watering":            ("水やり",   "💧"),
    "Seeding":             ("種まき",   "🌱"),
    "GenerateElectricity": ("発電",     "⚡"),
    "Handcraft":           ("手作業",   "🔨"),
    "Collection":          ("採取",     "🧺"),
    "Deforest":            ("伐採",     "🪓"),
    "Mining":              ("採掘",     "⛏️"),
    "ProductMedicine":     ("製薬",     "💊"),
    "Cool":                ("冷却",     "🧊"),
    "Transport":           ("運搬",     "📦"),
    "MonsterFarm":         ("牧場",     "🐄"),
}

WORK_ALIASES = {
    "火起こし": "EmitFlame", "火起し": "EmitFlame", "たき火": "EmitFlame", "焚火": "EmitFlame",
    "水やり": "Watering", "水撒き": "Watering",
    "種まき": "Seeding", "植え付け": "Seeding", "植付": "Seeding",
    "発電": "GenerateElectricity",
    "手作業": "Handcraft",
    "採取": "Collection",
    "伐採": "Deforest",
    "採掘": "Mining",
    "製薬": "ProductMedicine",
    "冷却": "Cool", "クーラー": "Cool",
    "運搬": "Transport",
    "牧場": "MonsterFarm",
}

SIZE_LABEL = {"XS": "極小", "S": "小", "M": "中", "L": "大", "XL": "特大"}


def normalize(text: str) -> str:
    """検索用にひらがなをカタカナへ、英字を小文字へ揃える。"""
    text = text.strip().lower()
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in text)


class PalDex:
    """パルのデータと交配計算をまとめたもの。"""

    def __init__(self, data: dict):
        self.version = data["version"]
        self.pals = data["pals"]
        self.breedable = data["breedable"]
        for name, pal in self.pals.items():
            pal["name"] = name

        self.specials = {}
        for parent_a, parent_b, child, gender in data["specials"]:
            self.specials.setdefault(tuple(sorted((parent_a, parent_b))), []).append((child, gender))

        # 交配ランクの平均値 → 生まれるパル、を先に全部計算しておく（逆引きを一瞬で返すため）
        pool = [self.pals[n] for n in self.breedable]
        max_rank = max(p["rank"] for p in self.pals.values())
        self.child_by_rank = [
            min(pool, key=lambda p: (abs(p["rank"] - target), -p["rank"], p["idx"]))["name"]
            for target in range(max_rank + 1)
        ]

        self.search_index = []
        for name, pal in self.pals.items():
            for key in (pal["ja"], pal["en"], name):
                self.search_index.append((normalize(key), name))

    def find(self, query: str) -> list[dict]:
        """名前（日本語・英語・部分一致）からパルを探す。"""
        query = normalize(query)
        if not query:
            return []
        for kind in (lambda k: k == query, lambda k: k.startswith(query), lambda k: query in k):
            hits = list(dict.fromkeys(name for key, name in self.search_index if kind(key)))
            if hits:
                return [self.pals[n] for n in hits]
        # 打ち間違い・うろ覚え用（例: ジェットラン → ジェッドラン）
        close = difflib.get_close_matches(query, [key for key, _ in self.search_index], n=3, cutoff=0.7)
        hits = list(dict.fromkeys(name for key, name in self.search_index if key in close))
        return [self.pals[n] for n in hits]

    def breed(self, parent_a: dict, parent_b: dict) -> list[tuple[dict, str | None]]:
        """親2匹から生まれる子を返す。性別で結果が変わる場合は複数返る。"""
        special = self.specials.get(tuple(sorted((parent_a["name"], parent_b["name"]))))
        if special:
            return [(self.pals[child], gender) for child, gender in special]
        target = (parent_a["rank"] + parent_b["rank"] + 1) // 2
        return [(self.pals[self.child_by_rank[target]], None)]

    def parents_of(self, child: dict) -> list[tuple[dict, dict]]:
        """その子が生まれる親の組み合わせを全部探す。"""
        names = list(self.pals)
        pairs = []
        for i, name_a in enumerate(names):
            for name_b in names[i:]:
                for born, _ in self.breed(self.pals[name_a], self.pals[name_b]):
                    if born["name"] == child["name"]:
                        pairs.append((self.pals[name_a], self.pals[name_b]))
                        break
        return pairs


paldex = PalDex(json.loads(DATA_PATH.read_text(encoding="utf-8")))


def element_text(pal: dict) -> str:
    if not pal["elements"]:
        return "不明"
    return " / ".join(f"{ELEMENTS[e][1]} {ELEMENTS[e][0]}" for e in pal["elements"] if e in ELEMENTS)


def weakness_text(pal: dict) -> str:
    weak = {WEAK_TO[e] for e in pal["elements"] if e in WEAK_TO}
    if not weak:
        return "なし"
    return " / ".join(f"{ELEMENTS[e][1]} {ELEMENTS[e][0]}" for e in sorted(weak))


def work_text(pal: dict) -> str:
    if not pal["work"]:
        return "なし（戦闘・騎乗向き）"
    items = sorted(pal["work"].items(), key=lambda kv: (-kv[1], kv[0]))
    return "　".join(f"{WORKS[k][1]}{WORKS[k][0]} **{v}**" for k, v in items if k in WORKS)


def pal_label(pal: dict) -> str:
    return f"{pal['ja']}（{pal['en']}）"


def build_pal_embed(pal: dict) -> discord.Embed:
    color = discord.Color.teal() if not pal["variant"] else discord.Color.purple()
    embed = discord.Embed(title=f"🐾 {pal_label(pal)}", color=color)
    embed.add_field(name="属性",     value=element_text(pal), inline=True)
    embed.add_field(name="弱点",     value=weakness_text(pal), inline=True)
    embed.add_field(name="レア度",   value=f"{'★' * min(pal['rarity'], 10)}（{pal['rarity']}）", inline=True)
    embed.add_field(name="作業適性", value=work_text(pal), inline=False)
    embed.add_field(
        name="ステータス",
        value=f"HP **{pal['hp']}**　攻撃 **{pal['atk']}**　防御 **{pal['def']}**",
        inline=True,
    )
    embed.add_field(
        name="その他",
        value=f"サイズ {SIZE_LABEL.get(pal['size'], pal['size'])}　"
              f"食事量 {pal['food']}　{'🌙 夜行性' if pal['nocturnal'] else '☀️ 昼行性'}",
        inline=True,
    )
    embed.set_footer(text=f"交配ランク {pal['rank']} | パルデータ {paldex.version}")
    return embed


def build_breed_embed(parent_a: dict, parent_b: dict) -> discord.Embed:
    results = paldex.breed(parent_a, parent_b)
    embed = discord.Embed(
        title="🥚 交配結果",
        description=f"**{pal_label(parent_a)}** ＋ **{pal_label(parent_b)}**",
        color=discord.Color.gold(),
    )
    for child, gender in results:
        label = "生まれるパル"
        if gender == "MALE":
            label = f"生まれるパル（{parent_a['ja']}が♂・{parent_b['ja']}が♀のとき）"
        elif gender == "FEMALE":
            label = f"生まれるパル（{parent_a['ja']}が♀・{parent_b['ja']}が♂のとき）"
        embed.add_field(
            name=label,
            value=f"🐾 **{pal_label(child)}**\n{element_text(child)}\n{work_text(child)}",
            inline=False,
        )
    if paldex.specials.get(tuple(sorted((parent_a["name"], parent_b["name"])))):
        embed.set_footer(text="この組み合わせは専用レシピだよ！")
    else:
        embed.set_footer(text=f"交配ランク {parent_a['rank']} と {parent_b['rank']} の平均から算出")
    return embed


def build_parents_embed(child: dict) -> discord.Embed:
    pairs = paldex.parents_of(child)
    embed = discord.Embed(
        title=f"🔍 {pal_label(child)} が生まれる組み合わせ",
        color=discord.Color.gold(),
    )
    if not pairs:
        embed.description = "交配では生まれないパルだよ（野生で捕まえよう）"
        return embed
    # 捕まえやすい（レア度の低い）親のペアを優先して表示する
    pairs.sort(key=lambda pair: (max(p["rarity"] for p in pair), sum(p["rarity"] for p in pair), pair[0]["ja"]))
    lines = [f"・{a['ja']} ＋ {b['ja']}" for a, b in pairs[:12]]
    embed.description = "\n".join(lines)
    if len(pairs) > 12:
        embed.description += f"\n…ほか {len(pairs) - 12} 通り"
    embed.set_footer(text=f"全 {len(pairs)} 通り | レア度の低い親から順に表示")
    return embed


def build_work_embed(work_key: str, min_level: int) -> discord.Embed:
    label, icon = WORKS[work_key]
    ranked = [p for p in paldex.pals.values() if p["work"].get(work_key, 0) >= min_level]
    ranked.sort(key=lambda p: (-p["work"][work_key], p["rarity"], p["ja"]))
    embed = discord.Embed(
        title=f"{icon} {label} が得意なパル（Lv{min_level}以上）",
        color=discord.Color.teal(),
    )
    if not ranked:
        embed.description = f"Lv{min_level}以上の{label}要員はいないよ！"
        return embed
    lines = [
        f"**Lv{p['work'][work_key]}** {p['ja']}　{element_text(p)}"
        for p in ranked[:15]
    ]
    embed.description = "\n".join(lines)
    if len(ranked) > 15:
        embed.description += f"\n…ほか {len(ranked) - 15} 種"
    embed.set_footer(text=f"該当 {len(ranked)} 種 | パルデータ {paldex.version}")
    return embed


def build_type_embed(element: str | None) -> discord.Embed:
    if element is None:
        embed = discord.Embed(title="🔰 属性相性表", color=discord.Color.teal())
        for key, (name, icon) in ELEMENTS.items():
            strong = [ELEMENTS[e][0] for e, w in WEAK_TO.items() if w == key]
            embed.add_field(
                name=f"{icon} {name}",
                value=f"有利: {'・'.join(strong) or 'なし'}\n弱点: {ELEMENTS[WEAK_TO[key]][0]}",
                inline=True,
            )
        embed.set_footer(text="「有利」= その属性のパルに大ダメージを与えられる相手")
        return embed

    name, icon = ELEMENTS[element]
    strong = [ELEMENTS[e][0] for e, w in WEAK_TO.items() if w == element]
    embed = discord.Embed(title=f"{icon} {name}", color=discord.Color.teal())
    embed.add_field(name="有利（攻めるとき）", value="・".join(strong) or "なし", inline=False)
    embed.add_field(name="弱点（守るとき）", value=ELEMENTS[WEAK_TO[element]][0], inline=False)
    best = sorted(
        (p for p in paldex.pals.values() if element in p["elements"]),
        key=lambda p: (-p["atk"], p["ja"]),
    )[:8]
    embed.add_field(name="攻撃力の高いパル", value="\n".join(f"・{p['ja']}（攻撃 {p['atk']}）" for p in best), inline=False)
    return embed


DEFAULT_REST_PORT = 8212


def api_base_url() -> str | None:
    """REST APIのURL。未設定なら接続先のホスト名＋既定ポート(8212)から組み立てる。"""
    if PALWORLD_API_URL:
        url = PALWORLD_API_URL.strip()
        if "://" in url:  # 完全なURLならそのまま使う（リバースプロキシ経由なども想定）
            return url.rstrip("/")
        host = url
    elif PALWORLD_SERVER_ADDRESS:
        host = PALWORLD_SERVER_ADDRESS.split(":")[0]  # ゲーム用ポートはREST APIでは使わない
    else:
        return None
    if ":" not in host:
        host = f"{host}:{DEFAULT_REST_PORT}"
    return f"http://{host}"


def server_configured() -> bool:
    return bool(PALWORLD_API_PASSWORD and api_base_url())


async def fetch_server_json(endpoint: str) -> dict:
    token = base64.b64encode(f"{PALWORLD_API_USER}:{PALWORLD_API_PASSWORD}".encode()).decode()
    headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{api_base_url()}/v1/api/{endpoint}",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)


def server_error_hint(error: Exception) -> str:
    """繋がらなかった理由を、次に何をすればいいか分かる形で伝える。"""
    if isinstance(error, aiohttp.ClientResponseError) and error.status in (401, 403):
        return "🔑 管理者パスワードが違うみたい。`PALWORLD_API_PASSWORD` を確認してね。"
    if isinstance(error, (aiohttp.ClientConnectorError, asyncio.TimeoutError)):
        return (
            f"🔌 `{api_base_url()}` に繋がらなかったよ。\n"
            "サーバー側で `RESTAPIEnabled=True` にして、RESTAPIのポートを開放してね。"
        )
    return f"❌ サーバー情報の取得に失敗したよ: {error}"


def format_uptime(seconds: int) -> str:
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}日{hours}時間{minutes}分"
    if hours:
        return f"{hours}時間{minutes}分"
    return f"{minutes}分"


async def build_server_embed() -> discord.Embed:
    info = await fetch_server_json("info")
    metrics = await fetch_server_json("metrics")
    players = (await fetch_server_json("players")).get("players", [])

    online = metrics.get("currentplayernum", len(players))
    capacity = metrics.get("maxplayernum", "?")
    fps = metrics.get("serverfps", 0)
    icon = "🟢" if fps >= 30 else ("🟡" if fps >= 15 else "🔴")

    embed = discord.Embed(
        title=f"🖥️ {info.get('servername', 'パルワールドサーバー')}",
        description=info.get("description") or None,
        color=discord.Color.teal(),
    )
    embed.add_field(name="👥 参加者", value=f"{online} / {capacity} 人", inline=True)
    embed.add_field(name=f"{icon} サーバーFPS", value=f"{fps}", inline=True)
    embed.add_field(name="⏱️ 稼働時間", value=format_uptime(metrics.get("uptime", 0)), inline=True)
    if players:
        lines = [
            f"・{p.get('name', '???')}　Lv.{p.get('level', '?')}　Ping {round(p.get('ping', 0))}ms"
            for p in players[:20]
        ]
        embed.add_field(name="🎮 参加中のプレイヤー", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="🎮 参加中のプレイヤー", value="いま誰もいないよ！", inline=False)
    if PALWORLD_SERVER_ADDRESS:
        embed.add_field(name="🔗 接続先", value=f"`{PALWORLD_SERVER_ADDRESS}`", inline=False)
    embed.set_footer(text=f"{info.get('version', '')} | Palworld REST API")
    return embed


def build_server_address_embed() -> discord.Embed:
    """REST APIの設定が無いときに、接続先だけを案内する。"""
    embed = discord.Embed(
        title="🖥️ パルワールド専用サーバー",
        description=f"**`{PALWORLD_SERVER_ADDRESS}`**",
        color=discord.Color.teal(),
    )
    embed.add_field(
        name="参加方法",
        value="タイトル画面 →「マルチプレイに参加（専用サーバー）」→\n"
              "右下の「参加するサーバーのIPを入力」に上のアドレスを貼り付け",
        inline=False,
    )
    embed.set_footer(text="参加人数やFPSも出したい場合は PALWORLD_API_PASSWORD を設定してね")
    return embed


def resolve_pal(ctx_query: str) -> tuple[dict | None, str | None]:
    """1件に絞り込めたパルを返す。絞り込めなければエラーメッセージを返す。"""
    hits = paldex.find(ctx_query)
    if not hits:
        return None, f"❌ 「{ctx_query}」というパルは見つからなかったよ！日本語名か英語名で入力してね。"
    if len(hits) > 1:
        names = "　".join(p["ja"] for p in hits[:10])
        more = f"　…ほか{len(hits) - 10}種" if len(hits) > 10 else ""
        return None, f"🔍 候補が {len(hits)} 種あるよ！もう少し詳しく入力してね。\n{names}{more}"
    return hits[0], None


class PalDexModal(discord.ui.Modal, title="🐾 パル図鑑"):
    query = discord.ui.TextInput(label="パルの名前", placeholder="例: モコロン / Lamball", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        pal, error = resolve_pal(self.query.value)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        await interaction.response.send_message(embed=build_pal_embed(pal))


class PalBreedModal(discord.ui.Modal, title="🥚 パル交配計算"):
    parent_a = discord.ui.TextInput(label="親①", placeholder="例: モコロン", required=True)
    parent_b = discord.ui.TextInput(label="親②", placeholder="例: キツネビ", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        pal_a, error_a = resolve_pal(self.parent_a.value)
        pal_b, error_b = resolve_pal(self.parent_b.value)
        if error_a or error_b:
            await interaction.response.send_message(error_a or error_b, ephemeral=True)
            return
        await interaction.response.send_message(embed=build_breed_embed(pal_a, pal_b))


class PalWorkSelect(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.select(
        placeholder="作業を選んでね",
        options=[discord.SelectOption(label=label, value=key, emoji=icon) for key, (label, icon) in WORKS.items()],
    )
    async def select_work(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.send_message(embed=build_work_embed(select.values[0], 1))


class Palworld(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def pal(self, ctx, *, name: str = None):
        if name is None:
            await ctx.send("🐾 使い方: `!pal <パルの名前>`　例: `!pal モコロン` `!pal Lamball`")
            return
        pal, error = resolve_pal(name)
        if error:
            await ctx.send(error)
            return
        await ctx.send(embed=build_pal_embed(pal))

    @commands.command()
    async def palbreed(self, ctx, *, names: str = None):
        parts = [n for n in (names or "").replace("＋", " ").replace("+", " ").replace("×", " ").split() if n]
        if len(parts) != 2:
            await ctx.send("🥚 使い方: `!palbreed <親①> <親②>`　例: `!palbreed モコロン キツネビ`")
            return
        pal_a, error_a = resolve_pal(parts[0])
        pal_b, error_b = resolve_pal(parts[1])
        if error_a or error_b:
            await ctx.send(error_a or error_b)
            return
        await ctx.send(embed=build_breed_embed(pal_a, pal_b))

    @commands.command()
    async def palparent(self, ctx, *, name: str = None):
        if name is None:
            await ctx.send("🔍 使い方: `!palparent <ほしいパル>`　例: `!palparent アヌビス`")
            return
        pal, error = resolve_pal(name)
        if error:
            await ctx.send(error)
            return
        await ctx.send(embed=build_parents_embed(pal))

    @commands.command()
    async def palwork(self, ctx, work: str = None, level: int = 1):
        if work is None:
            await ctx.send(
                "🛠️ 使い方: `!palwork <作業> [レベル]`　例: `!palwork 採掘 3`\n"
                f"作業: {'・'.join(label for label, _ in WORKS.values())}"
            )
            return
        work_key = WORK_ALIASES.get(work.strip())
        if work_key is None:
            await ctx.send(
                f"❌ 「{work}」という作業はないよ！\n"
                f"作業: {'・'.join(label for label, _ in WORKS.values())}"
            )
            return
        await ctx.send(embed=build_work_embed(work_key, max(1, min(level, 5))))

    @commands.command()
    async def paltype(self, ctx, element: str = None):
        if element is None:
            await ctx.send(embed=build_type_embed(None))
            return
        query = element.strip().rstrip("属性")
        key = next(
            (k for k, (name, _) in ELEMENTS.items()
             if query and (query in name or k.lower().startswith(query.lower()))),
            None,
        )
        if key is None:
            await ctx.send(f"❌ 「{element}」という属性はないよ！\n属性: {'・'.join(name for name, _ in ELEMENTS.values())}")
            return
        await ctx.send(embed=build_type_embed(key))

    @commands.command()
    async def palserver(self, ctx):
        if not server_configured():
            await ctx.send(embed=build_server_address_embed())
            return
        msg = await ctx.send("🖥️ サーバーに接続中...")
        try:
            embed = await build_server_embed()
        except Exception as e:
            await msg.edit(content=server_error_hint(e), embed=build_server_address_embed())
            return
        await msg.delete()
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Palworld(bot))
