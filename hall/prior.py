"""
店傾向分析 + 事前分布生成モジュール。

[精度向上: v2]
- アナスロ台別データ(hall_reports.db)が存在する場合、機種ごとの
  実差枚分布から動的に事前分布を生成する。
- セッション履歴には時系列減衰重み（最近のデータを優先）を適用。
- ベガスベガス大東店の固有データは引き続きフォールバックとして使用。

Dirichlet スムージングで過学習を防ぐ。
"""
from __future__ import annotations

import datetime
import math
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Optional

try:
    from config import HALL_REPORTS_DB
except ImportError:
    HALL_REPORTS_DB = Path(__file__).parent.parent / "data" / "hall_reports.db"

# ---------------------------------------------------------------------------
# ベガスベガス大東店 固有分析データ（min-repo.com 12/15〜6/25 期間集計）
# ---------------------------------------------------------------------------

DAITO_MACHINE_SCORES: dict[str, tuple[int, int]] = {
    "ディスクアップULTRA": (45, 23),
    "ディスクアップ2":     (35, 17),
    "戦国乙女4":           (34, 14),
    "不二子BT":            (32, 14),
    "絆2天膳":             (28, 14),
    "攻殻機動隊":          (28, 12),
    "このすば":            (26, 12),
    "SBJ":                 (25, 11),
    "まどかマギカ":        (24,  9),
    "虚構推理L":           (15,  5),
}

# 曜日別平均スコア（0=月〜6=日）
DAITO_WEEKDAY_AVG: dict[int, float] = {
    0: 1.93, 1: 1.67, 2: 1.90, 3: 1.58, 4: 1.71, 5: 1.87, 6: 2.02,
}

# 特定日補正（日付の下1桁）
DAITO_DIGIT_ADJ: dict[str, float] = {
    "5": +0.03,
    "8": -0.22,
}

SMOOTHING = 3.0
_WEEKDAY_NAMES = ["月", "火", "水", "木", "金", "土", "日"]

# 時系列減衰: 1日ごとに何%減衰するか
RECENCY_DECAY = 0.97


def compute_prior(
    hall_name: str,
    machine_name: str = "",
    weekday: Optional[int] = None,
    is_event_day: bool = False,
    day_of_month: Optional[int] = None,
    settings: Optional[list[str]] = None,
    seat_number: Optional[int] = None,
) -> dict[str, float]:
    """
    指定条件に合った事前分布を生成して返す。

    優先順位:
      1. セッション履歴（時系列重み付き）
      2. アナスロ台別データの差枚分布から設定推定
      3. ベガスベガス大東店 固有データ（大東店のみ）
      4. 一様事前

    Args:
        hall_name:      ホール名
        machine_name:   機種名（機種スコアでシフト）
        weekday:        0=月 ... 6=日
        is_event_day:   イベント日フラグ
        day_of_month:   その日の「日」（1〜31）
        settings:       設定ラベルリスト

    Returns:
        {"1": 0.15, "2": 0.20, ...} のような正規化済み事前分布。
    """
    from records.models import list_sessions  # 循環import 回避

    if settings is None:
        settings = ["1", "2", "3", "4", "5", "6"]
    n = len(settings)

    # ── Step1: セッション履歴（時系列減衰重み付き）──────────────────────
    # 台番が指定された場合: 同台番のセッションを3倍重視
    sessions = list_sessions(hall_name=hall_name, machine_name=machine_name or None)
    accumulated: dict[str, float] = defaultdict(float)
    total_weight = 0.0
    today = datetime.date.today()

    for s in sessions:
        if s.posterior is None:
            continue
        try:
            d = datetime.date.fromisoformat(s.date)
            # 時系列減衰: 直近ほど重み大
            age_days = max(0, (today - d).days)
            decay = RECENCY_DECAY ** age_days
            weight = decay
            # 同曜日を追加重視
            if weekday is not None and d.weekday() == weekday:
                weight *= 2.0
            # 同下1桁の日付
            if day_of_month is not None:
                if str(d.day % 10) == str(day_of_month % 10):
                    weight *= 1.5
            # 同台番なら大幅追加重視（実際にその台で打った記録）
            if seat_number is not None and s.seat_number == seat_number:
                weight *= 3.0
        except ValueError:
            weight = 0.5

        for setting, prob in s.posterior.items():
            if setting in settings:
                accumulated[setting] += prob * weight
        total_weight += weight

    # ── Step2: アナスロ差枚データから機種別設定分布を推定 ───────────────
    anaslo_weights: dict[str, float] | None = _estimate_prior_from_anaslo(
        hall_name, machine_name, settings, weekday, seat_number=seat_number
    )

    # ── Step3: 一様事前 + Dirichlet スムージング ────────────────────────
    weights: dict[str, float] = {}
    total_acc = sum(accumulated.values())

    for s in settings:
        hist_part = (accumulated.get(s, 0) / total_acc) if total_acc > 0 else (1.0 / n)
        weights[s] = hist_part * total_weight + SMOOTHING / n

    # アナスロ由来の事前をブレンド（セッション履歴が少ない場合に効く）
    if anaslo_weights:
        blend = 1.0 / (1.0 + total_weight / 5.0)  # 履歴が多いほど薄まる
        for s in settings:
            weights[s] = weights[s] * (1 - blend) + anaslo_weights.get(s, 1.0/n) * blend

    # ── Step4: ベガスベガス大東店 固有調整 ─────────────────────────────
    if _is_daito(hall_name):
        weights = _apply_daito_adjustments(
            weights, settings, machine_name, weekday, is_event_day, day_of_month
        )

    # ── Step5: 正規化 ────────────────────────────────────────────────────
    total = sum(weights.values())
    return {s: weights[s] / total for s in settings}


# 機種名の表記ゆれ正規化マップ（DB名 → JSON名）
_MACHINE_NAME_ALIASES: dict[str, str] = {
    "マイジャグラーV":                  "マイジャグラー5",
    "マイジャグラー5":                  "マイジャグラー5",
    "マイジャグラーIV":                 "マイジャグラーIV",
    "マイジャグラー4":                  "マイジャグラー4",
    "ゴーゴージャグラー2":              "ゴーゴージャグラー2",
    "ゴーゴージャグラーⅡ":             "ゴーゴージャグラー2",
    "アイムジャグラーEX-TP":            "アイムジャグラーEX Anniversary",
    "アイムジャグラーEX Anniversary":   "アイムジャグラーEX Anniversary",
    "アイムジャグラーEX-AE":            "アイムジャグラーEX Anniversary",
    "ウルトラミラクルジャグラー":        "ウルトラミラクルジャグラー",
    "スマスロ サンダーV":               "スマスロ サンダーV",
    "スマスロサンダーV":                "スマスロ サンダーV",
    "スマスロ ハナビ":                  "スマスロ ハナビ",
    "スマスロハナビ":                   "スマスロ ハナビ",
    "バジリスク絆2 BLACK EDITION":      "スマスロバジリスク絆3",
    "スマスロ北斗の拳":                 "スマスロ北斗の拳",
    "スマスロ まどかマギカ2":           "スマスロまどかマギカ2",
    "スマスロまどかマギカ2":            "スマスロまどかマギカ2",
    "バイオハザード RE:3":              "バイオハザード RE:3",
    "バイオハザードRE3":               "バイオハザード RE:3",
    "バイオRE:3":                      "バイオハザード RE:3",
    "ソードアートオンラインII":          "ソードアート・オンライン II",
    "SAO II":                           "ソードアート・オンライン II",
    "ネオアイムジャグラーEX":           "ネオアイムジャグラーEX",
    "キングハナハナ-30":                "キングハナハナ-30",
    "キングハナハナ30":                 "キングハナハナ-30",
    "ニューキングハナハナV-30":         "ニューキングハナハナV-30",
    "スーパーミラクルジャグラー":        "スーパーミラクルジャグラー",
    "ハッピージャグラーVIII":           "ハッピージャグラーVIII",
    "ハッピージャグラー8":              "ハッピージャグラーVIII",
    "ファンキージャグラー2":            "ファンキージャグラー2",
    "スマスロ北斗の拳ZERO":             "スマスロ北斗の拳ZERO",
    "北斗の拳ZERO":                     "スマスロ北斗の拳ZERO",
    "スマスロガンダムSEED":             "スマスロ ガンダムSEED",
    "ガンダムSEED":                     "スマスロ ガンダムSEED",
    "バーサスライジング":                "バーサスライジング",
    "スーパーミラクルエース2":           "スマスロ スーパーミラクルエース2",
    "鬼武者":                           "スマスロ鬼武者",
    "スマスロ 鬼武者":                  "スマスロ鬼武者",
    "Re:ゼロ":                          "スマスロRe:ゼロから始める異世界生活",
    "リゼロ":                           "スマスロRe:ゼロから始める異世界生活",
    "スマスロリゼロ":                    "スマスロRe:ゼロから始める異世界生活",
    "Reゼロ":                           "スマスロRe:ゼロから始める異世界生活",
    "大海物語5":                        "スマスロ大海物語5",
    "スマスロ 大海物語5":               "スマスロ大海物語5",
    "ひぐらし":                         "スマスロひぐらしのなく頃に",
    "ひぐらしのなく頃に":               "スマスロひぐらしのなく頃に",
    "OVERLORD":                         "スマスロOVERLORD絶対支配者光臨",
    "オーバーロード":                    "スマスロOVERLORD絶対支配者光臨",
    "スマスロオーバーロード":             "スマスロOVERLORD絶対支配者光臨",
    "エウレカセブン3":                  "Lエウレカセブン3",
    "Lエウレカ3":                       "Lエウレカセブン3",
    "ALFA-X":                           "スマスロ ALFA-X",
    "スマスロALFA-X":                   "スマスロ ALFA-X",
    "パパラッチ":                        "スマスロ パパラッチ",
    "スマスロパパラッチ":                "スマスロ パパラッチ",
}


def _load_machine_theory(machine_name: str) -> Optional[dict]:
    """機種JSONの理論確率を読み込む。エイリアスマップ→完全一致→ファジーマッチの順で検索。"""
    import json
    try:
        machines_dir = Path(__file__).parent.parent / "data" / "machines"
        lookup_name = _MACHINE_NAME_ALIASES.get(machine_name, machine_name)
        all_files = list(machines_dir.glob("*.json"))
        all_data = []
        for f in all_files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                all_data.append(data)
            except Exception:
                continue

        # 1. エイリアス/完全一致
        for data in all_data:
            if data.get("machine_name") in (machine_name, lookup_name):
                return data

        # 2. ファジーマッチ: 名前の主要部分が含まれるか（前4文字以上が一致）
        def _normalize(s: str) -> str:
            return s.replace(" ","").replace("　","").replace("・","").replace("：","").replace(":","")

        norm_name = _normalize(machine_name)
        norm_lookup = _normalize(lookup_name)
        best = None
        best_score = 0
        for data in all_data:
            jname = _normalize(data.get("machine_name", ""))
            # 両方向で部分一致チェック
            score = 0
            if norm_name in jname or jname in norm_name:
                score = min(len(norm_name), len(jname))
            elif norm_lookup in jname or jname in norm_lookup:
                score = min(len(norm_lookup), len(jname))
            if score >= 4 and score > best_score:
                best = data
                best_score = score
        return best
    except Exception:
        return None


def _estimate_prior_from_anaslo(
    hall_name: str,
    machine_name: str,
    settings: list[str],
    weekday: Optional[int],
    seat_number: Optional[int] = None,
) -> Optional[dict[str, float]]:
    """
    アナスロ差枚データ + BB/RB実測確率から設定分布を推定する。

    アルゴリズム v2:
    1. 差枚の加重平均 → ガウス尤度で設定推定
    2. BB確率・RB確率の加重平均 → 理論値と照合してガウス尤度追加
    3. 両者の対数尤度を加算してブレンド
    """
    if not machine_name:
        return None
    try:
        conn = sqlite3.connect(HALL_REPORTS_DB)
        sql_dow = str((weekday + 1) % 7) if weekday is not None else None

        rows = conn.execute(
            """SELECT diff_coins, bb_prob, rb_prob, games, report_date, ev_pct, seat_number
               FROM hall_day_seat
               WHERE hall_name=? AND machine_name=? AND games > 0
                 AND (bb_prob IS NOT NULL OR ev_pct IS NOT NULL)
               ORDER BY report_date DESC
               LIMIT 200""",
            (hall_name, machine_name)
        ).fetchall()

        # 同機種内のBB確率z-score（台番特定時）
        seat_bb_z: Optional[float] = None
        if seat_number is not None:
            all_bbs_by_seat: dict[int, list[float]] = {}
            for rr in rows:
                s_num = rr[6]
                bb_v = rr[1]
                if bb_v and bb_v > 0 and s_num is not None:
                    all_bbs_by_seat.setdefault(int(s_num), []).append(float(bb_v))
            if len(all_bbs_by_seat) >= 2:
                seat_means = {k: sum(v)/len(v) for k, v in all_bbs_by_seat.items() if len(v) >= 2}
                if seat_number in seat_means and len(seat_means) >= 2:
                    all_m = list(seat_means.values())
                    global_mean = sum(all_m) / len(all_m)
                    global_std = math.sqrt(sum((x - global_mean)**2 for x in all_m) / len(all_m))
                    if global_std > 0:
                        seat_bb_z = (seat_means[seat_number] - global_mean) / global_std

        conn.close()

        if len(rows) < 3:
            return None

        today = datetime.date.today()
        diffs_weighted: list[tuple[float, float]] = []
        bb_probs_weighted: list[tuple[float, float]] = []
        rb_probs_weighted: list[tuple[float, float]] = []

        for diff, bb_p, rb_p, games, rdate, ev_p, row_seat in rows:
            try:
                d = datetime.date.fromisoformat(rdate)
                age = max(0, (today - d).days)
                w = RECENCY_DECAY ** age
                if sql_dow:
                    row_dow = str((d.weekday() + 1) % 7)
                    if row_dow == sql_dow:
                        w *= 2.0  # 同曜日2倍
                    else:
                        w *= 0.6  # 異曜日は60%に縮小
                # 同台番なら3倍重視
                if seat_number is not None and row_seat is not None and int(row_seat) == seat_number:
                    w *= 3.0
                # ゲーム数が多いほど信頼度が高い（重みを補正）
                games_factor = math.log1p(float(games or 0)) / math.log1p(1000.0)
                w_adj = w * max(0.3, games_factor)
            except Exception:
                w_adj = 0.5
            diffs_weighted.append((float(diff or 0), w_adj))
            if bb_p and bb_p > 0:
                bb_probs_weighted.append((float(bb_p), w_adj))
            if rb_p and rb_p > 0:
                rb_probs_weighted.append((float(rb_p), w_adj))

        # ── 差枚ガウス尤度 ──────────────────────────────────────────────
        total_w = sum(w for _, w in diffs_weighted)
        mean_d  = sum(d*w for d, w in diffs_weighted) / total_w
        var_d   = sum((d - mean_d)**2 * w for d, w in diffs_weighted) / total_w
        std_d   = math.sqrt(max(var_d, 1.0))

        # 機種の機械割から期待差枚を算出 (理論値がある場合)
        # 平均ゲーム数 × (機械割 - 1.0) × コインレート(3円/枚仮定)
        theory_kw: dict[str, float] | None = None
        if theory and "machine_kw" in theory:
            theory_kw = {str(k): float(v) for k, v in theory["machine_kw"].items()}

        avg_games_obs = (sum(float(r[3] or 0) for r in rows) / len(rows)) if rows else 800.0
        avg_g = max(avg_games_obs, 400.0)

        setting_expected: dict[str, float] = {}
        if theory_kw and all(s in theory_kw for s in settings):
            # 機械割から期待差枚 = avg_games * (kw - 1.0) * 3 (コイン1枚≈3円)
            for s in settings:
                kw = theory_kw[s]
                setting_expected[s] = avg_g * (kw - 1.0) * 3.0
        elif len(settings) == 6:
            ref = {"1": -350, "2": -200, "3": -50, "4": +100, "5": +250, "6": +450}
            for s in settings:
                setting_expected[s] = ref.get(s, 0.0)
        else:
            vals = [-350, -200, 0, +150, +300, +500][:len(settings)]
            for i, s in enumerate(settings):
                setting_expected[s] = vals[i]

        # sigma_d: データのばらつきに適応（過小評価を防ぐため最低400）
        sigma_d = max(std_d, 400.0)
        log_likes: dict[str, float] = {
            s: -0.5 * ((mean_d - setting_expected[s]) / sigma_d) ** 2
            for s in settings
        }

        # ── BB/RB 実測確率 vs 理論値 ガウス尤度 ─────────────────────────
        # サンプル数が多いほどsigmaを絞れる（CLTに基づく）
        n_bb = len(bb_probs_weighted)
        if theory and bb_probs_weighted and n_bb >= 3:
            tw_bb = sum(w for _, w in bb_probs_weighted)
            mean_bb = sum(p*w for p, w in bb_probs_weighted) / tw_bb
            var_bb = sum((p - mean_bb)**2 * w for p, w in bb_probs_weighted) / tw_bb
            std_bb = math.sqrt(max(var_bb, 1e-8))

            # サンプル数に基づく信頼度スケーリング（n>=20で最大精度）
            confidence = min(1.0, math.log1p(n_bb) / math.log1p(20))

            bb_el = next((el for el in theory.get("elements", [])
                          if any(kw in el["name"] for kw in ["BB", "BIG", "ボーナス合算", "AT初当"])), None)
            if bb_el:
                for s in settings:
                    theo_p = bb_el.get("p", {}).get(s, 0.0)
                    if theo_p > 0:
                        # sigma: サンプルが多いほど絞る (min 10%, max 25% of theo_p)
                        sigma_floor = theo_p * (0.25 - 0.15 * confidence)
                        sigma_bb = max(std_bb, sigma_floor)
                        log_likes[s] += confidence * (-0.5 * ((mean_bb - theo_p) / sigma_bb) ** 2)

        n_rb = len(rb_probs_weighted)
        if theory and rb_probs_weighted and n_rb >= 3:
            tw_rb = sum(w for _, w in rb_probs_weighted)
            mean_rb = sum(p*w for p, w in rb_probs_weighted) / tw_rb
            var_rb = sum((p - mean_rb)**2 * w for p, w in rb_probs_weighted) / tw_rb
            std_rb = math.sqrt(max(var_rb, 1e-8))

            confidence_rb = min(1.0, math.log1p(n_rb) / math.log1p(20))

            rb_el = next((el for el in theory.get("elements", [])
                          if any(kw in el["name"] for kw in ["RB", "REG", "単独RB"])), None)
            if rb_el:
                for s in settings:
                    theo_p = rb_el.get("p", {}).get(s, 0.0)
                    if theo_p > 0:
                        sigma_floor_rb = theo_p * (0.30 - 0.15 * confidence_rb)
                        sigma_rb = max(std_rb, sigma_floor_rb)
                        log_likes[s] += confidence_rb * (-0.5 * ((mean_rb - theo_p) / sigma_rb) ** 2)

        # 台番z-scoreを事前に反映（+1σ以上 = 高設定寄りに補正）
        if seat_bb_z is not None and abs(seat_bb_z) >= 0.3:
            high_settings = [s for s in settings if int(s) >= 4]
            low_settings  = [s for s in settings if int(s) < 4]
            # z-scoreのtanh変換で±30%まで補正
            shift = math.tanh(seat_bb_z * 0.6) * 0.3
            if shift > 0:
                for s in high_settings:
                    log_likes[s] += shift
            else:
                for s in low_settings:
                    log_likes[s] += -shift

        # softmax 正規化
        max_ll = max(log_likes.values())
        exps = {s: math.exp(ll - max_ll) for s, ll in log_likes.items()}
        z = sum(exps.values())
        return {s: v / z for s, v in exps.items()}

    except Exception:
        return None


def _is_daito(hall_name: str) -> bool:
    return "大東" in hall_name or "ベガスベガス大東" in hall_name


def _apply_daito_adjustments(
    w: dict[str, float],
    settings: list[str],
    machine_name: str,
    weekday: Optional[int],
    is_event_day: bool,
    day_of_month: Optional[int],
) -> dict[str, float]:
    w = dict(w)
    high = [s for s in settings if int(s) >= 4]
    low  = [s for s in settings if int(s) < 4]

    if machine_name and machine_name in DAITO_MACHINE_SCORES:
        score, appearances = DAITO_MACHINE_SCORES[machine_name]
        boost = min(score / 180.0, 0.25)
        for s in high: w[s] *= (1.0 + boost)
        for s in low:  w[s] *= max(0.5, 1.0 - boost * 0.4)

    if weekday is not None:
        day_avg   = DAITO_WEEKDAY_AVG.get(weekday, 1.80)
        day_delta = (day_avg - 1.80) * 0.4
        if day_delta > 0:
            for s in high: w[s] *= (1.0 + day_delta)
        elif day_delta < 0:
            for s in high: w[s] *= max(0.5, 1.0 + day_delta)

    if day_of_month is not None:
        digit = str(day_of_month % 10)
        adj = DAITO_DIGIT_ADJ.get(digit, 0.0)
        if adj != 0:
            for s in high: w[s] *= (1.0 + adj)

    if is_event_day:
        for s in high: w[s] *= 1.10

    return w


# ---------------------------------------------------------------------------
# 分析ユーティリティ
# ---------------------------------------------------------------------------

def day_rating(hall_name: str, weekday: int) -> dict:
    """指定曜日の「高設定が出やすさ」を返す（アナスロデータ優先）"""
    # アナスロ実データがあれば使う
    try:
        conn = sqlite3.connect(HALL_REPORTS_DB)
        sql_dow = str((weekday + 1) % 7)
        rows = conn.execute(
            """SELECT strftime('%w', report_date) as dow,
                      AVG(diff_coins) as avg_diff,
                      COUNT(*) as cnt
               FROM hall_day_seat
               WHERE hall_name=? AND bb_prob IS NOT NULL
                 AND machine_name NOT LIKE '末尾%'
               GROUP BY dow
               HAVING cnt >= 5""",
            (hall_name,)
        ).fetchall()
        conn.close()
        if rows:
            dow_data = {r[0]: (r[1], r[2]) for r in rows}
            target_dow_sql = str((weekday + 1) % 7)
            if target_dow_sql in dow_data:
                target_avg = dow_data[target_dow_sql][0]
                all_avgs = [v[0] for v in dow_data.values()]
                mn, mx = min(all_avgs), max(all_avgs)
                normalized = (target_avg - mn) / (mx - mn) if mx > mn else 0.5
                rank = sorted(dow_data.keys(), key=lambda k: -dow_data[k][0]).index(target_dow_sql) + 1
                return {
                    "weekday": _WEEKDAY_NAMES[weekday],
                    "avg_diff": round(target_avg, 1),
                    "normalized": round(normalized, 3),
                    "rank": rank,
                    "source": "anaslo_db",
                }
    except Exception:
        pass

    if _is_daito(hall_name):
        avg = DAITO_WEEKDAY_AVG.get(weekday, 1.80)
        all_scores = list(DAITO_WEEKDAY_AVG.values())
        mn, mx = min(all_scores), max(all_scores)
        score_0_1 = (avg - mn) / (mx - mn) if mx > mn else 0.5
        return {
            "weekday": _WEEKDAY_NAMES[weekday],
            "avg_score": avg,
            "normalized": round(score_0_1, 3),
            "rank": sorted(DAITO_WEEKDAY_AVG, key=lambda d: -DAITO_WEEKDAY_AVG[d]).index(weekday) + 1,
            "source": "daito_analysis",
        }

    from records.models import list_sessions
    sessions = list_sessions(hall_name=hall_name)
    probs: list[float] = []
    for s in sessions:
        if s.posterior is None:
            continue
        try:
            d = datetime.date.fromisoformat(s.date)
            if d.weekday() == weekday:
                hp = sum(p for k, p in s.posterior.items() if int(k) >= 4)
                probs.append(hp)
        except ValueError:
            pass
    avg_hp = sum(probs) / len(probs) if probs else 0.5
    return {
        "weekday": _WEEKDAY_NAMES[weekday],
        "avg_high_prob": round(avg_hp, 3),
        "sample_count": len(probs),
        "source": "history",
    }


def machine_ranking(hall_name: str) -> list[dict]:
    """機種別スコアランキング（アナスロ実データ優先）"""
    try:
        conn = sqlite3.connect(HALL_REPORTS_DB)
        rows = conn.execute(
            """SELECT machine_name,
                      ROUND(AVG(diff_coins)) as avg_diff,
                      COUNT(DISTINCT report_date) as appearances,
                      COUNT(DISTINCT seat_number) as units,
                      SUM(CASE WHEN diff_coins > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as win_rate
               FROM hall_day_seat
               WHERE hall_name=? AND machine_name NOT LIKE '末尾%'
                 AND machine_name != '_NODATA_' AND bb_prob IS NOT NULL
               GROUP BY machine_name
               HAVING COUNT(*) >= 5
               ORDER BY avg_diff DESC
               LIMIT 20""",
            (hall_name,)
        ).fetchall()
        conn.close()
        if rows:
            return [
                {
                    "machine": r[0], "avg_diff": r[1],
                    "appearances": r[2], "units": r[3],
                    "win_rate": round(r[4], 1),
                    "source": "anaslo_db",
                }
                for r in rows
            ]
    except Exception:
        pass

    if _is_daito(hall_name):
        return [
            {
                "machine": name, "score": score, "appearances": apps,
                "avg_score": round(score / apps, 2) if apps else 0,
            }
            for name, (score, apps) in sorted(
                DAITO_MACHINE_SCORES.items(), key=lambda x: -x[1][0]
            )
        ]

    from records.models import list_sessions
    sessions = list_sessions(hall_name=hall_name)
    machine_data: dict[str, list[float]] = defaultdict(list)
    for s in sessions:
        if s.posterior:
            hp = sum(p for k, p in s.posterior.items() if int(k) >= 4)
            machine_data[s.machine_name].append(hp)
    return [
        {
            "machine": m, "avg_high_prob": round(sum(v)/len(v), 3),
            "appearances": len(v),
        }
        for m, v in sorted(machine_data.items(), key=lambda x: -sum(x[1])/len(x[1]))
    ]
