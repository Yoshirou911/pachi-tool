"""
ちょんぼりすた (chonborista.com) から設定差データをスクレイピングして
data/machines/{機種名}.json に保存するスクリプト。

機種ページ例: https://chonborista.com/slot/sammy-slot/255633/

注意: 記事ごとにテーブル構造が異なるためベストエフォート。
      データが取れない機種は skip される。

使い方:
  python -m scraper.chonborista                                   # 全機種
  python -m scraper.chonborista https://chonborista.com/slot/...  # URL指定
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://chonborista.com"
SETTINGS = ["1", "2", "3", "4", "5", "6"]
OUTPUT_DIR = Path(__file__).parent.parent / "data" / "machines"
INTERVAL = 2.0


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
    text = text.strip().replace(",", "")
    m = re.match(r"^1/(\d+\.?\d*)$", text)
    if m:
        denom = float(m.group(1))
        return 1.0 / denom if denom > 0 else None
    m = re.match(r"^(\d+\.?\d*)%$", text)
    if m:
        v = float(m.group(1)) / 100.0
        return v if 0.0 < v < 1.0 else None
    try:
        v = float(text)
        return v if 0.0 < v < 1.0 else None
    except ValueError:
        return None


def _find_header_and_data(rows: list) -> tuple[list[str], int] | None:
    for i, row in enumerate(rows[:3]):
        cells = [c.get_text(strip=True) for c in row.find_all(["th", "td"])]
        if cells and cells[0] == "設定":
            return cells, i + 1
    return None


def _parse_setting_tables(soup: BeautifulSoup) -> list[dict]:
    """
    記事内テーブルを走査して設定1〜6データを抽出する（ベストエフォート）。
    """
    elements_map: dict[str, dict[str, float]] = {}

    # 記事本文のみを対象（ヘッダー・フッターのノイズを除外）
    content = soup.find("article") or soup.find("main") or soup

    for table in content.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        first_headers = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]
        if not first_headers:
            continue

        # Format A: どこかの行の先頭が "設定"
        result = _find_header_and_data(rows)
        if result:
            headers, data_start = result
            element_names = headers[1:]
            for row in rows[data_start:]:
                cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
                if not cells:
                    continue
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

        # Format B: 上行に設定1〜設定6
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


def _extract_machine_name(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    if not h1:
        return "不明"
    raw = h1.get_text(strip=True)
    # 【機種名】〇〇 形式から抽出
    m = re.search(r"【(.+?)】", raw)
    if m:
        return m.group(1)
    # 「機種名 スロット〜」形式から前半だけ取る
    return re.split(r"\s+(スロット|解析|天井|設定|打ち方|評価|まとめ|判別|スペック)", raw)[0].strip()


def scrape_machine(url: str) -> dict | None:
    """ちょんぼりすたの機種ページから設定差データを取得。"""
    try:
        soup = _fetch(url)
    except requests.HTTPError as e:
        print(f"  [skip] {url}: HTTP {e.response.status_code}")
        return None

    machine_name = _extract_machine_name(soup)
    elements = _parse_setting_tables(soup)

    if not elements:
        print(f"  [skip] '{machine_name}': 設定差テーブルなし")
        return None

    return {
        "machine_name": machine_name,
        "settings": SETTINGS,
        "elements": elements,
    }


def scrape_machine_list() -> list[str]:
    """スロット一覧ページを走査して機種URLリストを返す。"""
    urls: list[str] = []
    page = 1
    while True:
        list_url = f"{BASE_URL}/slot/page/{page}/"
        try:
            soup = _fetch(list_url)
        except Exception as e:
            print(f"  一覧取得エラー (page={page}): {e}")
            break

        found = False
        for a in soup.find_all("a", href=re.compile(r"/slot/[^/]+/\d+/")):
            href = a["href"]
            if not href.startswith("http"):
                href = BASE_URL + href
            if href not in urls:
                urls.append(href)
                found = True

        if not found:
            break
        print(f"  一覧 page={page}: 累計 {len(urls)} 機種")
        page += 1
        time.sleep(INTERVAL)

    return urls


def _normalize(data: dict) -> dict:
    """全要素に共通する設定番号だけを残して bayes_engine に渡せる形に整える。"""
    elements = data.get("elements", [])
    if not elements:
        return data
    setting_sets = [set((el.get("p") or el.get("one_over") or {}).keys()) for el in elements]
    common = set.intersection(*setting_sets)
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


def run(urls: list[str] | None = None) -> None:
    if urls is None:
        print("機種一覧を取得中...")
        urls = scrape_machine_list()
        print(f"{len(urls)} 機種を取得")

    ok = 0
    for url in urls:
        print(f"  scraping {url} ...", end=" ", flush=True)
        data = scrape_machine(url)
        if data is None:
            continue
        out = save(data)
        print(f"-> {out.name} ({len(data['elements'])} 要素)")
        ok += 1
        time.sleep(INTERVAL)

    print(f"\n完了: {ok}/{len(urls)} 機種を保存")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        run(sys.argv[1:])
    else:
        run()
