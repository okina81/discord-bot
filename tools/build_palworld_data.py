"""パルワールドのパルデータ（data/palworld.json）を生成するスクリプト。

ゲームのアップデートでパルが増えたときに再実行する:

    python tools/build_palworld_data.py

データ元（どちらもゲーム本体の .pak から抽出されたもの）:
  - tylercamp/palcalc      … 日本語名・交配ランク・ステータス・交配結果テーブル
  - oMaN-Rod/palworld-save-pal … 属性・作業適性（採油を含む）

交配は「両親の交配ランクの平均に最も近いパルが生まれる」という計算式で求められるが、
一部の組み合わせは専用の結果が決まっている（アヌビス・ジェットランなど）。
このスクリプトは palcalc の全 44,851 通りの交配結果テーブルと計算式を突き合わせ、
計算式で再現できない組み合わせだけを specials として書き出す。
そのため生成された JSON は全組み合わせを完全に再現できる。
"""

import json
import urllib.request
from collections import Counter
from pathlib import Path

PALCALC_DB = "https://raw.githubusercontent.com/tylercamp/palcalc/main/PalCalc.Model/db.json"
PALCALC_BREEDING = "https://raw.githubusercontent.com/tylercamp/palcalc/main/PalCalc.Model/breeding.json"
SAVE_PAL_PALS = "https://raw.githubusercontent.com/oMaN-Rod/palworld-save-pal/main/data/json/pals.json"

OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "palworld.json"

# 交配結果の候補にならないパル（亜種・伝説・イベント限定）を弾くためのしきい値。
# 通常交配で生まれるパルは数百通りの組み合わせから生まれるが、
# 専用の組み合わせでしか生まれないパルは 1〜2 通りしかない。
GENERIC_POOL_MIN_PAIRS = 4


def fetch_json(url):
    print(f"取得中: {url}")
    with urllib.request.urlopen(url, timeout=120) as resp:
        return json.load(resp)


def breeding_target(rank_a, rank_b):
    return (rank_a + rank_b + 1) // 2


def pick_child(pool, target):
    """交配ランクが target に最も近いパルを選ぶ（同着ならランクが高いほう）。"""
    return min(pool, key=lambda p: (abs(p["rank"] - target), -p["rank"], p["idx"]))


def main():
    db = fetch_json(PALCALC_DB)
    breeding = fetch_json(PALCALC_BREEDING)["Breeding"]
    save_pal = fetch_json(SAVE_PAL_PALS)
    extra = {k.lower(): v for k, v in save_pal.items() if v.get("is_pal")}

    pals = {}
    for p in db["Pals"]:
        name = p["InternalName"]
        ex = extra.get(name.lower(), {})
        pals[name] = {
            "ja": p["LocalizedNames"]["ja"],
            "en": p["Name"],
            "idx": p["InternalIndex"],
            "rank": p["BreedingPower"],
            "elements": ex.get("element_types", []),
            "work": {k: v for k, v in sorted(ex.get("work_suitability", {}).items()) if v > 0},
            "rarity": p["Rarity"],
            "size": p["Size"],
            "nocturnal": p["Nocturnal"],
            "hp": p["Hp"],
            "atk": p["Attack"],
            "def": p["Defense"],
            "food": p["FoodAmount"],
            "variant": p["Id"]["IsVariant"],
        }

    # 交配結果テーブル（親2匹は順不同なのでソートしたタプルをキーにする）
    table = {}
    gendered = []
    for entry in breeding:
        pair = tuple(sorted((entry["Parent1InternalName"], entry["Parent2InternalName"])))
        if entry["Parent1Gender"] == "WILDCARD":
            table[pair] = entry["ChildInternalName"]
        else:
            gendered.append(entry)

    counts = Counter(table.values())
    pool_names = sorted(n for n, c in counts.items() if c >= GENERIC_POOL_MIN_PAIRS)
    pool = [dict(pals[n], name=n) for n in pool_names]

    specials = []
    for pair, child in sorted(table.items()):
        target = breeding_target(pals[pair[0]]["rank"], pals[pair[1]]["rank"])
        if pick_child(pool, target)["name"] != child:
            specials.append([pair[0], pair[1], child, None])
    for entry in gendered:
        # 親の性別で結果が変わる組み合わせ（性別は親1のもの）
        specials.append([
            entry["Parent1InternalName"],
            entry["Parent2InternalName"],
            entry["ChildInternalName"],
            entry["Parent1Gender"],
        ])

    data = {
        "version": db["Version"],
        "sources": [PALCALC_DB, PALCALC_BREEDING, SAVE_PAL_PALS],
        "pals": pals,
        "breedable": pool_names,
        "specials": specials,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"書き出し完了: {OUT_PATH}（パル{len(pals)}種 / 通常交配{len(pool_names)}種 / 専用組み合わせ{len(specials)}件）")

    # 生成したデータで全組み合わせを再現できるか検証する
    special_map = {}
    for a, b, child, gender in specials:
        special_map[(tuple(sorted((a, b))), gender)] = child
    ng = 0
    for pair, child in table.items():
        got = special_map.get((pair, None))
        if got is None:
            got = pick_child(pool, breeding_target(pals[pair[0]]["rank"], pals[pair[1]]["rank"]))["name"]
        if got != child:
            ng += 1
    for entry in gendered:
        pair = tuple(sorted((entry["Parent1InternalName"], entry["Parent2InternalName"])))
        if special_map.get((pair, entry["Parent1Gender"])) != entry["ChildInternalName"]:
            ng += 1
    total = len(table) + len(gendered)
    print(f"検証: {total - ng}/{total} 一致" + ("" if ng == 0 else f" ← {ng}件が不一致！"))


if __name__ == "__main__":
    main()
