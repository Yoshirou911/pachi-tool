"""公開元ごとの店舗名表記を、アプリ内の正式名称へ安全に統一する。"""
from __future__ import annotations

import re
import unicodedata


_CANONICAL_ALIASES: dict[str, tuple[str, ...]] = {
    "キコーナ四條畷店": ("キコーナ四條畷店", "キコーナ四条畷店"),
    "ひま・わり四條畷店": ("ひま・わり四條畷店", "ひまわり四條畷店", "ひま・わり四条畷店"),
    "キコーナ野崎店": ("キコーナ野崎店",),
    "ニコニコ住道店": ("ニコニコ住道店",),
    "キコーナ大東店": ("キコーナ大東店",),
    "マルハン大東店": ("マルハン大東店",),
    "スーパーコスモプレミアム大東店": (
        "スーパーコスモプレミアム大東店",
        "SUPER COSMO PREMIUM 大東店",
        "SUPER COSMO PREMIUM大東店",
    ),
    "ベガスベガス大東店": ("ベガスベガス大東店", "VEGAS VEGAS 大東店"),
    "ラッシュMATSUMOTO#59": ("ラッシュMATSUMOTO#59", "ラッシュ松本#59"),
}


def normalize_hall_key(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    normalized = normalized.replace("matsumoto", "松本")
    return re.sub(r"[^0-9a-zぁ-んァ-ヶ一-龠]", "", normalized)


_ALIAS_TO_CANONICAL = {
    normalize_hall_key(alias): canonical
    for canonical, aliases in _CANONICAL_ALIASES.items()
    for alias in aliases
}


def canonical_hall_name(value: str | None) -> str:
    """確認済みの別表記だけを正式名へ統一し、未知の名称は変更しない。"""
    raw = unicodedata.normalize("NFKC", value or "").strip()
    return _ALIAS_TO_CANONICAL.get(normalize_hall_key(raw), raw)


def hall_names_match(left: str | None, right: str | None) -> bool:
    return bool(left and right and canonical_hall_name(left) == canonical_hall_name(right))


def known_hall_aliases(canonical: str) -> tuple[str, ...]:
    name = canonical_hall_name(canonical)
    return _CANONICAL_ALIASES.get(name, (name,))
