"""PACHI TOOL が分析対象にするスマスロ機種の判定。"""
from __future__ import annotations

import re
import unicodedata


# 媒体によって「L」「スマスロ」が省略される代表的な機種。
# 新しい省略表記が見つかった場合はここへ追加する。
_BARE_SMARTSLOT_TOKENS = (
    "いざ!番長",
    "いざ！番長",
    "真打 吉宗",
    "真打吉宗",
    "からくりサーカス",
    "かぐや様は告らせたい",
    "モンキーターンV",
    "モンキーターンⅤ",
    "ゴッドイーター リザレクション",
    "ゴッドイーターリザレクション",
)


def is_smartslot_machine(machine_name: str) -> bool:
    """公開サイト上の機種名がスマスロ対象なら True を返す。"""
    if not machine_name:
        return False
    name = re.sub(r"[\s　]+", "", unicodedata.normalize("NFKC", machine_name))
    if not name or name.startswith("_") or "ジャグラー" in name:
        return False
    if "スマスロ" in name:
        return True
    # 型式名の先頭 L / LB はスマスロの表示として各データ媒体で使われる。
    if re.match(r"^L(?:B|パチスロ|スロット|[ァ-ヶ一-龠A-Za-z0-9])", name):
        return True
    return any(token.replace(" ", "") in name for token in _BARE_SMARTSLOT_TOKENS)


def is_juggler_machine(machine_name: str) -> bool:
    """公開サイト上の機種名がジャグラーシリーズなら True を返す。"""
    if not machine_name:
        return False
    name = re.sub(r"[\s　]+", "", unicodedata.normalize("NFKC", machine_name))
    return bool(name and not name.startswith("_") and "ジャグラー" in name)


def is_supported_analysis_machine(machine_name: str) -> bool:
    """独立分析モードのいずれかで使用する機種かを返す。"""
    return is_smartslot_machine(machine_name) or is_juggler_machine(machine_name)


def normalize_machine_key(machine_name: str) -> str:
    """媒体ごとの接頭辞・空白・記号差を除いた保守的な照合キー。"""
    name = unicodedata.normalize("NFKC", machine_name or "").lower()
    name = re.sub(r"[\s　・･~〜～:：!！?？_\-‐()（）\[\]【】]", "", name)
    previous = None
    while name and name != previous:
        previous = name
        name = re.sub(r"^(?:スマスロ|パチスロ|lb?|l)", "", name)
    return "".join(character for character in name if character.isalnum())


def machine_names_match(left: str, right: str) -> bool:
    """同一機種と判断できる表記だけを、短すぎる部分一致を避けて照合する。"""
    left_key = normalize_machine_key(left)
    right_key = normalize_machine_key(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    return min(len(left_key), len(right_key)) >= 5 and (
        left_key in right_key or right_key in left_key
    )
