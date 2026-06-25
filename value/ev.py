"""
期待値・撤退判断モジュール。

EV = Σ posterior[s] × kw[s]   (kw は機械割、1.00 = 等価)

撤退推奨基準: EV < RETREAT_THRESHOLD (デフォルト 0.98)
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

MACHINES_DIR = Path(__file__).parent.parent / "data" / "machines"

RETREAT_THRESHOLD = 0.98
HIGH_SETTING_THRESHOLD = 4

# 機種データにkwがない場合のデフォルト（6段階設定の典型値）
_DEFAULT_KW: dict[str, float] = {
    "1": 0.970, "2": 0.985, "3": 0.995,
    "4": 1.010, "5": 1.040, "6": 1.080,
}


@dataclass
class EVResult:
    ev: float                        # 期待値（1.00 = 等価）
    ev_pct: float                    # EV パーセント表示
    expected_setting: float          # 期待設定値
    high_setting_prob: float         # 設定{threshold}以上の確率
    should_retreat: bool             # 撤退推奨フラグ
    retreat_reason: str              # 撤退理由テキスト
    ev_by_setting: dict[str, float]  # 設定ごとの機械割
    posterior: dict[str, float]      # 正規化済み事後分布
    kw_source: str                   # "machine_data" | "default"


def load_machine_kw(machine_name: str) -> tuple[dict[str, float], str]:
    path = MACHINES_DIR / f"{machine_name}.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            kw = data.get("machine_kw")
            if kw and isinstance(kw, dict):
                return {str(k): float(v) for k, v in kw.items()}, "machine_data"
        except (json.JSONDecodeError, ValueError, KeyError):
            pass
    return dict(_DEFAULT_KW), "default"


def compute_ev(
    posterior: dict[str, float],
    machine_name: str = "",
    machine_kw: Optional[dict[str, float]] = None,
    retreat_threshold: float = RETREAT_THRESHOLD,
    high_threshold: int = HIGH_SETTING_THRESHOLD,
) -> EVResult:
    """
    Args:
        posterior: {"1": 0.10, "2": 0.15, ...}  (正規化前でも可)
        machine_name: 機種名（machine_kwを自動ロードするために使用）
        machine_kw: 外部から渡す機械割（渡した場合はmachine_nameより優先）
        retreat_threshold: この機械割を下回ったら撤退推奨
        high_threshold: 「高設定」の最低設定番号
    """
    kw_source = "override"
    if machine_kw is None:
        machine_kw, kw_source = load_machine_kw(machine_name)

    total = sum(posterior.values())
    if total <= 0:
        raise ValueError("posterior の合計が 0 以下です")

    settings = sorted(posterior.keys(), key=lambda s: int(s))
    norm = {s: posterior[s] / total for s in settings}

    ev = 0.0
    expected_setting = 0.0
    high_prob = 0.0
    ev_by_setting: dict[str, float] = {}

    for s in settings:
        p = norm[s]
        kw = machine_kw.get(s, _DEFAULT_KW.get(s, 1.0))
        ev_by_setting[s] = kw
        ev += p * kw
        expected_setting += p * int(s)
        if int(s) >= high_threshold:
            high_prob += p

    should_retreat = ev < retreat_threshold
    if should_retreat:
        retreat_reason = (
            f"期待値 {ev*100:.1f}% < 撤退基準 {retreat_threshold*100:.0f}%"
        )
    elif high_prob < 0.20:
        should_retreat = True
        retreat_reason = f"高設定(設定{high_threshold}以上)確率 {high_prob*100:.0f}% < 20%"
    else:
        retreat_reason = ""

    return EVResult(
        ev=round(ev, 4),
        ev_pct=round(ev * 100, 2),
        expected_setting=round(expected_setting, 2),
        high_setting_prob=round(high_prob, 4),
        should_retreat=should_retreat,
        retreat_reason=retreat_reason,
        ev_by_setting=ev_by_setting,
        posterior=norm,
        kw_source=kw_source,
    )


def format_ev_report(result: EVResult) -> str:
    lines = [
        f"期待値       : {result.ev_pct:.1f}%",
        f"期待設定     : {result.expected_setting:.2f}",
        f"高設定({HIGH_SETTING_THRESHOLD}以上): {result.high_setting_prob*100:.1f}%",
        f"撤退推奨     : {'⚠ YES  ' + result.retreat_reason if result.should_retreat else '✓ NO'}",
        f"機械割ソース : {result.kw_source}",
        "",
        "  設定  機械割    確率",
        "  " + "─" * 32,
    ]
    for s in sorted(result.posterior.keys(), key=int):
        p = result.posterior[s]
        kw = result.ev_by_setting.get(s, 0)
        bar = "█" * int(p * 25)
        lines.append(f"  設定{s}  {kw*100:5.1f}%  {p*100:5.1f}%  {bar}")
    return "\n".join(lines)


if __name__ == "__main__":
    post = {"1": 0.05, "2": 0.10, "3": 0.15, "4": 0.30, "5": 0.25, "6": 0.15}
    r = compute_ev(post, machine_name="ゴーゴージャグラー")
    print(format_ev_report(r))
