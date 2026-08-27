"""停止中の長野データを保持しつつ、既定収集対象から外すことを検証する。"""
import json

from api.scheduler import _DEFAULT_HALLS
from config import ROOT
from scraper.pworld_snapshot import HALL_URLS
from scraper.minrepo_archive import KNOWN_SEEDS


MATSUMOTO_HALLS = {
    "ラッシュMATSUMOTO#59", "チャンピオンOZ", "マルハン松本店",
    "チャンピオンANNEX", "KEIZ松本店", "ABC松本白板店",
    "No.1松本筑摩店", "EX松本店",
}
SHIOJIRI_HALLS = {
    "APULO塩尻北インター店", "APULO811", "キング塩尻店",
    "キング会館ネクスト塩尻店",
}


def test_nagano_halls_are_not_in_default_collection_scope():
    configs = {row["hall_name"]: row for row in _DEFAULT_HALLS}
    expected = MATSUMOTO_HALLS | SHIOJIRI_HALLS
    assert expected.isdisjoint(configs)
    assert {row["prefecture"] for row in configs.values()} == {"大阪府"}
    # 取得済みデータを後日再利用できるよう座標マスタは保持する。
    coordinates = json.loads((ROOT / "data" / "hall_coords.json").read_text(encoding="utf-8"))
    assert expected <= set(coordinates)


def test_current_pworld_matsumoto_sources_are_registered():
    expected = MATSUMOTO_HALLS - {"EX松本店"}
    assert expected <= set(HALL_URLS)
    assert all(HALL_URLS[name].startswith("https://") for name in expected)


def test_nagano_archive_seeds_are_registered():
    assert {"マルハン松本店", "KEIZ松本店", "ABC松本白板店",
            "APULO塩尻北インター店", "キング塩尻店",
            "ラッシュMATSUMOTO#59"} <= set(KNOWN_SEEDS)
