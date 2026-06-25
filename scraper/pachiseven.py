"""
パチ7 (pachiseven.jp) から設定差データをスクレイピングして
data/machines/{機種名}.json に保存するスクリプト。

設定差ページ: https://pachiseven.jp/machines/{id}/cutout/4

使い方:
  python -m scraper.pachiseven              # 全機種（時間かかる）
  python -m scraper.pachiseven 6548 6692    # IDを指定して個別取得
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://pachiseven.jp"
SETTINGS = ["1", "2", "3", "4", "5", "6"]
OUTPUT_DIR = Path(__file__).parent.parent / "data" / "machines"
INTERVAL = 2.0  # リクエスト間隔（秒）—— サーバ負荷対策


def _fetch(url: str) -> BeautifulSoup:
    resp = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; pachi-tool/1.0)"},
        timeout=15,
    )
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return BeautifulSoup(resp.text, "lxml")


def _parse_prob(text: str) -> float | None:
    """'1/273.1'、'12.5%'、または '0.00366' を確率(float)に変換。変換不可なら None。"""
    text = text.strip().replace(",", "")
    # 1/x 形式
    m = re.match(r"^1/(\d+\.?\d*)$", text)
    if m:
        denom = float(m.group(1))
        return 1.0 / denom if denom > 0 else None
    # xx.x% 形式
    m = re.match(r"^(\d+\.?\d*)%$", text)
    if m:
        v = float(m.group(1)) / 100.0
        return v if 0.0 < v < 1.0 else None
    # 直接確率
    try:
        v = float(text)
        return v if 0.0 < v < 1.0 else None
    except ValueError:
        return None


def _find_header_and_data(rows: list) -> tuple[list[str], int] | None:
    """
    テーブルの行リストから「設定」が先頭にある行を探し、
    (ヘッダーセル一覧, データ開始行インデックス) を返す。
    見つからなければ None。
    タイトル行が先頭にある複合テーブルにも対応する。
    """
    for i, row in enumerate(rows[:3]):  # 先頭3行まで探す
        cells = [c.get_text(strip=True) for c in row.find_all(["th", "td"])]
        if cells and cells[0] == "設定":
            return cells, i + 1
    return None


def _parse_setting_tables(soup: BeautifulSoup) -> list[dict]:
    """
    ページ内の全テーブルを走査し、設定1〜6データを抽出する。

    対応する2種類のテーブル形式:
      Format A — 左列が設定番号、上行が要素名（タイトル行あり複合テーブルも対応）
        例: 設定 | ベル確率 | ボーナス合算
             1  | 1/7.3   | 1/273.1

      Format B — 上行が設定番号、左列が要素名
        例: 要素     | 設定1 | 設定2 ... 設定6
            ベル確率 | 1/7.3 | 1/7.2 ... 1/6.9
    """
    elements_map: dict[str, dict[str, float]] = {}

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue

        first_headers = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]
        if not first_headers:
            continue

        # --- Format A: どこかの行の先頭が "設定" ---
        result = _find_header_and_data(rows)
        if result:
            headers, data_start = result
            element_names = headers[1:]
            for row in rows[data_start:]:
                cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
                if not cells:
                    continue
                # "1"〜"6" または "設定1"〜"設定6" 両方に対応
                m = re.match(r"^(?:設定)?(\d+)$", cells[0])
                if not m or m.group(1) not in SETTINGS:
                    continue
                s = m.group(1)
                for i, name in enumerate(element_names):
                    if not name or i + 1 >= len(cells):
                        continue
                    p = _parse_prob(cells[i + 1])
                    if p is not None:
                        elements_map.setdefault(name, {})[s] = p

        # --- Format B: 上行に設定1〜設定6 ---
        elif any(f"設定{s}" in first_headers for s in SETTINGS):
            setting_cols: dict[str, int] = {}
            for s in SETTINGS:
                for j, h in enumerate(first_headers):
                    if f"設定{s}" in h:
                        setting_cols[s] = j
                        break
            for row in rows[1:]:
                cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
                if not cells:
                    continue
                name = cells[0]
                if not name:
                    continue
                for s, col_i in setting_cols.items():
                    if col_i >= len(cells):
                        continue
                    p = _parse_prob(cells[col_i])
                    if p is not None:
                        elements_map.setdefault(name, {})[s] = p

    # 設定1と設定6が揃っているものを返す（一部機種は設定3等が非公開）
    return [
        {"name": name, "p": probs}
        for name, probs in elements_map.items()
        if "1" in probs and "6" in probs
    ]


def scrape_machine(machine_id: int) -> dict | None:
    """パチ7から1機種分の設定差データを取得。失敗・データなし時は None。"""
    url = f"{BASE_URL}/machines/{machine_id}/cutout/4"
    try:
        soup = _fetch(url)
    except requests.HTTPError as e:
        print(f"  [skip] id={machine_id}: HTTP {e.response.status_code}")
        return None

    # title タグから機種名を取得（h1 はロゴ画像のみ）
    title_tag = soup.find("title")
    if not title_tag:
        return None
    # "機種名 | 設定判別..." → "機種名"
    machine_name = re.split(r"\s*[｜│|]\s*", title_tag.get_text(strip=True))[0].strip()
    if not machine_name:
        return None

    elements = _parse_setting_tables(soup)
    if not elements:
        print(f"  [skip] id={machine_id} '{machine_name}': 設定差テーブルなし")
        return None

    return {
        "machine_name": machine_name,
        "settings": SETTINGS,
        "elements": elements,
    }


def scrape_machine_list() -> list[int]:
    """スロット機種一覧ページを走査して機種IDのリストを返す。"""
    ids: list[int] = []
    page = 1
    while True:
        url = f"{BASE_URL}/machines?type=slot&page={page}"
        try:
            soup = _fetch(url)
        except Exception as e:
            print(f"  一覧取得エラー (page={page}): {e}")
            break

        found = False
        for a in soup.select("a[href*='/machines/']"):
            m = re.search(r"/machines/(\d+)(?:/|$)", a.get("href", ""))
            if m:
                mid = int(m.group(1))
                if mid not in ids:
                    ids.append(mid)
                    found = True

        if not found:
            break
        print(f"  一覧 page={page}: 累計 {len(ids)} 機種")
        page += 1
        time.sleep(INTERVAL)

    return ids


def _normalize(data: dict) -> dict:
    """
    bayes_engine が要求する「全要素に全設定が揃っている」状態に正規化する。

    全要素に共通する設定番号だけを machine の settings にセットし、
    その settings が揃っていない要素は除外する。
    例: 北斗の拳は設定3未公開 → settings=["1","2","4","5","6"] で保存。
    """
    elements = data.get("elements", [])
    if not elements:
        return data

    # 各要素が持つ設定番号の積集合
    setting_sets = [set((el.get("p") or el.get("one_over") or {}).keys()) for el in elements]
    common = set.intersection(*setting_sets)

    # 元の settings 宣言と掛け合わせ、数値順にソート
    declared = set(data.get("settings", SETTINGS))
    final = sorted(common & declared, key=int)

    if not final:
        return data

    filtered = [
        el for el in elements
        if all(s in (el.get("p") or el.get("one_over") or {}) for s in final)
    ]

    return {**data, "settings": final, "elements": filtered}


def save(data: dict) -> Path:
    data = _normalize(data)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r'[\\/:*?"<>|]', "_", data["machine_name"])
    out = OUTPUT_DIR / f"{safe_name}.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def run(machine_ids: list[int] | None = None) -> None:
    if machine_ids is None:
        print("機種一覧を取得中...")
        machine_ids = scrape_machine_list()
        print(f"{len(machine_ids)} 機種を取得")

    ok = 0
    for mid in machine_ids:
        print(f"  scraping id={mid} ...", end=" ", flush=True)
        data = scrape_machine(mid)
        if data is None:
            continue
        out = save(data)
        print(f"-> {out.name} ({len(data['elements'])} 要素)")
        ok += 1
        time.sleep(INTERVAL)

    print(f"\n完了: {ok}/{len(machine_ids)} 機種を保存")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        run([int(x) for x in sys.argv[1:]])
    else:
        run()
