"""
パチスロ設定推測 CLI ツール。

スマホ/PCの端末で直接使える対話型コンソール。
APIサーバなしにローカルで設定推測・EV計算を完結させる。

使い方:
    python -m core.cli                       # 対話モード
    python -m core.cli --machine ゴーゴージャグラー --games 3000  # ワンショット
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.bayes_engine import MachineProfile, Observation, SettingEstimator
from value.ev import compute_ev, format_ev_report

MACHINES_DIR = ROOT / "data" / "machines"

# ANSI カラー（Windows でも ANSI_VT100 が有効なら使える）
_USE_COLOR = sys.stdout.isatty() and os.name != 'nt' or os.environ.get('FORCE_COLOR')

def _c(text, code): return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text
def red(t): return _c(t, '31')
def yellow(t): return _c(t, '33')
def green(t): return _c(t, '32')
def blue(t): return _c(t, '34')
def magenta(t): return _c(t, '35')
def cyan(t): return _c(t, '36')
def bold(t): return _c(t, '1')

SETTING_COLORS = {
    '1': red, '2': lambda t: _c(t, '91'),
    '3': yellow, '4': green, '5': blue, '6': magenta,
}


def list_machines() -> list[str]:
    return sorted(p.stem for p in MACHINES_DIR.glob("*.json") if p.stem)


def load_profile(machine_name: str) -> MachineProfile | None:
    path = MACHINES_DIR / f"{machine_name}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return MachineProfile.from_dict(data)
    except Exception as e:
        print(red(f"エラー: {e}"))
        return None


def render_posterior(profile: MachineProfile, posterior: dict[str, float]) -> None:
    estimator = SettingEstimator(profile)
    expected = estimator.expected_setting(posterior)
    high_prob = estimator.high_setting_prob(posterior)

    width = 30
    print()
    print(bold("  ── 設定推測結果 ──"))
    print()
    for s in profile.settings:
        p = posterior.get(s, 0)
        filled = int(p * width)
        bar = "█" * filled + "░" * (width - filled)
        color = SETTING_COLORS.get(s, lambda t: t)
        pct_str = f"{p*100:5.1f}%"
        print(f"  {color(bold('設定' + s))}  {color(bar)}  {bold(pct_str)}")

    print()
    print(f"  期待設定値: {bold(f'{expected:.2f}')}")
    print(f"  高設定(4以上)確率: {bold(f'{high_prob*100:.1f}%')}")


def render_ev(profile: MachineProfile, posterior: dict[str, float]) -> None:
    result = compute_ev(posterior, machine_name=profile.machine_name)
    print()
    print(bold("  ── 期待値 / 撤退判断 ──"))
    ev_str = f"{result.ev_pct:.1f}%"
    ev_colored = green(bold(ev_str)) if result.ev >= 1.0 else yellow(bold(ev_str)) if result.ev >= 0.98 else red(bold(ev_str))
    print(f"  期待値: {ev_colored}  ({result.kw_source})")
    if result.should_retreat:
        print(f"  {red(bold('⚠ 撤退推奨'))}  {red(result.retreat_reason)}")
    else:
        print(f"  {green('✓ 続行可')}")


def interactive_mode() -> None:
    print(bold(cyan("=" * 46)))
    print(bold(cyan("  🎰 pachi-tool  設定推測CLI")))
    print(bold(cyan("=" * 46)))
    print()

    machines = list_machines()
    if not machines:
        print(red("機種データがありません。先に scraper を実行してください。"))
        return

    # 機種選択
    print(bold("利用可能な機種:"))
    for i, m in enumerate(machines, 1):
        print(f"  {i:2d}. {m}")
    print()

    while True:
        raw = input("機種番号または名前を入力 > ").strip()
        if not raw:
            continue
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(machines):
                machine_name = machines[idx]
                break
            print(red("番号が範囲外です"))
        elif raw in machines:
            machine_name = raw
            break
        else:
            # 部分一致
            matched = [m for m in machines if raw in m]
            if len(matched) == 1:
                machine_name = matched[0]
                break
            elif matched:
                print(f"候補: {', '.join(matched)}")
            else:
                print(red("機種が見つかりません"))

    profile = load_profile(machine_name)
    if not profile:
        return

    print(f"\n  機種: {bold(profile.machine_name)}")
    print(f"  設定: {', '.join(profile.settings)}")
    print(f"  要素数: {len(profile.elements)}")

    estimator = SettingEstimator(profile)

    print()
    print(bold("=" * 40))
    print(bold("  リアルタイム推測モード"))
    print("  カウントを入力するたびに推測を更新します")
    print("  終了: q または Ctrl+C")
    print(bold("=" * 40))

    games = 0
    counts: dict[str, int] = {}

    while True:
        print()

        # 総G数
        raw = input(f"  総ゲーム数 [{games or '未入力'}]: ").strip()
        if raw.lower() == 'q':
            break
        if raw.isdigit():
            games = int(raw)

        # 各カウント
        print(f"  要素カウント (Enterでスキップ):")
        for el in profile.elements:
            cur = counts.get(el.name, 0)
            raw = input(f"    {el.name} [{cur}]: ").strip()
            if raw.lower() == 'q':
                return
            if raw.isdigit():
                counts[el.name] = int(raw)
            elif raw == '':
                pass  # 前の値を保持

        if games == 0:
            print(yellow("  総ゲーム数を入力してください"))
            continue

        obs = Observation(total_games=games, counts={k: v for k, v in counts.items() if v > 0})
        posterior = estimator.estimate(obs)

        render_posterior(profile, posterior)
        render_ev(profile, posterior)

        print()
        again = input("  続けて更新しますか? [Y/n]: ").strip().lower()
        if again in ('n', 'q'):
            break


def oneshot_mode(args) -> None:
    profile = load_profile(args.machine)
    if not profile:
        print(red(f"機種 '{args.machine}' が見つかりません"))
        print("利用可能:", ", ".join(list_machines()))
        sys.exit(1)

    counts: dict[str, int] = {}
    for kv in (args.counts or []):
        if '=' in kv:
            k, v = kv.split('=', 1)
            counts[k.strip()] = int(v.strip())

    obs = Observation(total_games=args.games, counts=counts)
    estimator = SettingEstimator(profile)
    posterior = estimator.estimate(obs)

    print(bold(f"\n{profile.machine_name}  G数={args.games:,}"))
    if counts:
        print("カウント: " + ", ".join(f"{k}={v}" for k, v in counts.items()))

    render_posterior(profile, posterior)
    render_ev(profile, posterior)
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="pachi-tool 設定推測CLI")
    parser.add_argument("--machine", "-m", help="機種名")
    parser.add_argument("--games", "-g", type=int, default=0, help="総ゲーム数")
    parser.add_argument("--counts", "-c", nargs="*", help="カウント (要素名=回数 形式)")
    parser.add_argument("--list", "-l", action="store_true", help="機種一覧を表示")
    args = parser.parse_args()

    if args.list:
        for m in list_machines():
            print(m)
        return

    if args.machine:
        oneshot_mode(args)
    else:
        try:
            interactive_mode()
        except (KeyboardInterrupt, EOFError):
            print("\n終了")


if __name__ == "__main__":
    main()
