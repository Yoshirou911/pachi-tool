"""店舗分析で共有する地域定義。"""
from __future__ import annotations


MATSUMOTO_SHIOJIRI_HALLS = frozenset({
    "ラッシュMATSUMOTO#59",
    "チャンピオンOZ",
    "マルハン松本店",
    "チャンピオンANNEX",
    "KEIZ松本店",
    "ABC松本白板店",
    "No.1松本筑摩店",
    "EX松本店",
    "APULO塩尻北インター店",
    "APULO811",
    "キング塩尻店",
    "キング会館ネクスト塩尻店",
})

REGION_META = {
    "all": {"label": "全地域", "center": (35.45, 136.75)},
    "matsumoto_shiojiri": {"label": "松本・塩尻", "center": (36.18, 137.95)},
    "nagano": {"label": "長野県", "center": (36.18, 137.95)},
    "osaka": {"label": "大阪府", "center": (34.724, 135.631)},
}


def region_matches(hall_name: str, prefecture: str | None, region: str) -> bool:
    if region == "all":
        return True
    if region == "matsumoto_shiojiri":
        return hall_name in MATSUMOTO_SHIOJIRI_HALLS
    if region == "nagano":
        return prefecture == "長野県"
    if region == "osaka":
        return prefecture == "大阪府"
    return False


def region_label(region: str) -> str:
    return str(REGION_META.get(region, REGION_META["all"])["label"])


def region_center(region: str) -> tuple[float, float]:
    return tuple(REGION_META.get(region, REGION_META["all"])["center"])
