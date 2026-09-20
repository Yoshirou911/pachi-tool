"""店舗ごとに確認済みの公開取得経路を一覧化する。"""
from __future__ import annotations

from hall.names import canonical_hall_name


def collection_source_plan(hall_name: str) -> dict:
    """推測URLを作らず、コードで確認済みの取得経路だけを返す。"""
    name = canonical_hall_name(hall_name)

    # 遅延 import にして、分析APIの読み込み時に収集器の通信処理を起動しない。
    from scraper.anoslot_public import HALL_IDS as ANOSLOT_IDS
    from scraper.dmm_snapshot import HALL_URLS as DMM_URLS
    from scraper.events import SLOMAP_HALL_URLS
    from scraper.minrepo_archive import KNOWN_SEEDS
    from scraper.pachireview import HALL_SOURCES as PACHIREVIEW_URLS
    from scraper.pworld_snapshot import HALL_URLS as PWORLD_URLS

    sources = []

    def add(
        key: str,
        label: str,
        category: str,
        supported: bool,
        source_url: str = "",
        *,
        automatic: bool = True,
        note: str = "",
    ) -> None:
        sources.append({
            "key": key,
            "label": label,
            "category": category,
            "supported": bool(supported),
            "automatic": bool(automatic),
            "source_url": source_url if supported else "",
            "note": note,
        })

    anoslot_id = ANOSLOT_IDS.get(name)
    add(
        "anoslot",
        "公開機種別実績",
        "performance",
        anoslot_id is not None,
        f"https://anoslot.moe/stores/{anoslot_id}" if anoslot_id is not None else "",
        note="公開ページに差枚がある期間だけ自動取得",
    )
    add(
        "minrepo_archive",
        "過去差枚アーカイブ",
        "performance",
        name in KNOWN_SEEDS,
        KNOWN_SEEDS.get(name, ""),
        note="確認済み起点URLから前日・翌日だけをたどる",
    )
    add(
        "pachireview",
        "公開月別・日別実績",
        "performance",
        name in PACHIREVIEW_URLS,
        PACHIREVIEW_URLS.get(name, ""),
        note="掲載済みの日別ページだけを取得",
    )
    add(
        "pworld",
        "現在の設置機種",
        "installation",
        name in PWORLD_URLS,
        PWORLD_URLS.get(name, ""),
        note="店舗公開ページの設置機種を日次確認",
    )
    add(
        "dmm",
        "設置台数・フロアマップ",
        "layout",
        name in DMM_URLS,
        DMM_URLS.get(name, ""),
        note="確認済み店舗ページだけを対象",
    )
    add(
        "slomap_events",
        "イベント予定",
        "events",
        name in SLOMAP_HALL_URLS,
        SLOMAP_HALL_URLS.get(name, ""),
        note="公開JSON-LDと公式告知を候補として取得し品質確認",
    )
    add(
        "manual_seat",
        "現地・CSV台番号実績",
        "manual",
        True,
        automatic=False,
        note="公開差枚がない店舗を現地入力またはCSVで補完",
    )

    automatic = [source for source in sources if source["automatic"] and source["supported"]]
    return {
        "hall_name": name,
        "sources": sources,
        "summary": {
            "automatic_source_count": len(automatic),
            "performance_source_count": sum(
                source["supported"] and source["category"] == "performance"
                for source in sources
            ),
            "installation_supported": any(
                source["supported"] and source["category"] == "installation"
                for source in sources
            ),
            "event_supported": any(
                source["supported"] and source["category"] == "events"
                for source in sources
            ),
            "layout_supported": any(
                source["supported"] and source["category"] == "layout"
                for source in sources
            ),
            "manual_fallback": True,
        },
    }
