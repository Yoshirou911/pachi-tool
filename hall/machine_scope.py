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
