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

# 四條畷駅を起点に、徒歩圏と野崎・住道・大東方面までを日常の比較対象にする。
SHIJONAWATE_AREA_HALLS = frozenset({
    "キコーナ四條畷店",
    "ひま・わり四條畷店",
    "キコーナ野崎店",
    "ニコニコ住道店",
    "キコーナ大東店",
    "マルハン大東店",
    "スーパーコスモプレミアム大東店",
    "ベガスベガス大東店",
})

REGION_META = {
    "all": {"label": "全地域", "center": (35.45, 136.75)},
    "shijonawate": {"label": "四條畷駅周辺", "center": (34.733, 135.639)},
    "matsumoto_shiojiri": {"label": "松本・塩尻", "center": (36.18, 137.95)},
    "nagano": {"label": "長野県", "center": (36.18, 137.95)},
    "osaka": {"label": "大阪府", "center": (34.724, 135.631)},
}


def region_matches(hall_name: str, prefecture: str | None, region: str) -> bool:
    if region == "all":
        return True
    if region == "shijonawate":
        return hall_name in SHIJONAWATE_AREA_HALLS
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
