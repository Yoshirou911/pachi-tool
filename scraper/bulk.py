"""
パチ7から複数機種を一括スクレイピングする。

ベガスベガス大東店でよく見かける機種を DAITO_MACHINES にプリセット。
パチ7 ID は https://pachiseven.jp/machines/{id}/cutout/4 のページから確認。

使い方:
    python -m scraper.bulk              # プリセット全機種
    python -m scraper.bulk --ids 6548 6692 4421   # IDを指定
    python -m scraper.bulk --list       # プリセット一覧を表示
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from scraper.pachiseven import save, scrape_machine, INTERVAL

# ベガスベガス大東店で頻繁に登場する機種のパチ7 ID
# ID は pachiseven.jp/machines/{id}/cutout/4 から確認
DAITO_MACHINES: dict[str, int] = {
    "ゴーゴージャグラー":          4421,   # 確認済み
    "パチスロ甲鉄城のカバネリ":    6548,   # 確認済み
    "スマスロ北斗の拳":            6692,   # 確認済み
    "スマスロディスクアップ2":     7120,   # 要確認
    "スマスロ戦国乙女4":           7215,   # 要確認
    "バジリスク絆2天膳":           7001,   # 要確認
    "スマスロ攻殻機動隊":          7300,   # 要確認
    "スマスロこのすば":            7350,   # 要確認
    "スマスロSBJ":                 6900,   # 要確認
    "まどか☆マギカ前後篇":        5800,   # 要確認
    "スマスロ不二子TYPE-B":        7100,   # 要確認
    "バジリスク絆2":               5932,   # 要確認
    "ハナハナホウオウ天翔":        5100,   # 要確認
    "アイムジャグラーEX":          4200,   # 要確認
    "マイジャグラー5":             6100,   # 要確認
}


def run_bulk(machine_ids: list[int] | None = None, dry_run: bool = False) -> None:
    if machine_ids is None:
        targets = list(DAITO_MACHINES.values())
        print(f"プリセット {len(targets)} 機種をスクレイピングします")
    else:
        targets = machine_ids

    ok, skip, fail = 0, 0, 0
    for mid in targets:
        print(f"  [{ok+skip+fail+1}/{len(targets)}] id={mid} ...", end=" ", flush=True)
        if dry_run:
            print("(dry-run skip)")
            continue
        data = scrape_machine(mid)
        if data is None:
            skip += 1
            continue
        try:
            out = save(data)
            print(f"→ {out.name} ({len(data['elements'])} 要素)")
            ok += 1
        except Exception as e:
            print(f"[保存エラー] {e}")
            fail += 1
        time.sleep(INTERVAL)

    print(f"\n完了: 保存={ok} / スキップ={skip} / エラー={fail}")


def show_list() -> None:
    print("プリセット機種一覧 (大東店向け):")
    for name, mid in DAITO_MACHINES.items():
        print(f"  {mid:6d}  {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="パチ7 一括スクレイピング")
    parser.add_argument("--ids", nargs="*", type=int, help="機種IDを指定 (未指定=プリセット全機種)")
    parser.add_argument("--list", "-l", action="store_true", help="プリセット一覧を表示")
    parser.add_argument("--dry-run", action="store_true", help="実際にはスクレイピングせずID一覧のみ表示")
    args = parser.parse_args()

    if args.list:
        show_list()
        return
    run_bulk(args.ids, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
