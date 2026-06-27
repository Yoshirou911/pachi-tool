"""
Groq API を使ったパチスロ分析AIサービス（無料）
- チャット (A): データを渡してQ&A
- 自動レポート (B): 今日の攻略レポート生成
- 設定推測コメント (C): 推測結果の解釈（機種理論値付き）
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Optional

try:
    from config import HALL_REPORTS_DB, SESSIONS_DB, MACHINES_DIR
except ImportError:
    _base = Path(__file__).parent.parent / "data"
    HALL_REPORTS_DB = _base / "hall_reports.db"
    SESSIONS_DB     = _base / "sessions.db"
    MACHINES_DIR    = _base / "machines"

MODEL = "llama-3.3-70b-versatile"

SYSTEM_PROMPT = """あなたはパチスロ専門のデータアナリストです。
ユーザーのパチスロ実戦データと店舗の台別データを分析し、
簡潔で実践的なアドバイスを日本語で提供します。

回答のルール:
- 必ず日本語で答える
- 数字は具体的に示す。「BB 1/200 → 設定4〜5相当」のように理論値と比較する
- 投資判断は「〇〇円以内」など明確に
- 絵文字は使わない
- 長すぎず、要点を絞る
- データが少ない場合は「〇〇Gのサンプルでは判断が難しい」と明記する"""


def _get_client():
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        return None
    try:
        from groq import Groq
        return Groq(api_key=key)
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# データ収集ヘルパー
# ---------------------------------------------------------------------------

def _hall_summary(hall_name: str, days: int = 30) -> str:
    try:
        conn = sqlite3.connect(HALL_REPORTS_DB)
        rows = conn.execute("""
            SELECT machine_name, seat_number,
                   COUNT(*) as cnt,
                   ROUND(AVG(diff_coins)) as avg_diff,
                   MAX(diff_coins) as max_diff,
                   MIN(diff_coins) as min_diff,
                   ROUND(AVG(games)) as avg_games
            FROM hall_day_seat
            WHERE hall_name=? AND machine_name != '_NODATA_'
              AND report_date >= date('now', ? || ' days')
            GROUP BY machine_name, seat_number
            HAVING cnt >= 3
            ORDER BY avg_diff DESC
            LIMIT 30
        """, (hall_name, f"-{days}")).fetchall()
        conn.close()
        if not rows:
            return f"{hall_name}: データなし"
        lines = [f"【{hall_name} 過去{days}日 台別成績ランキング】"]
        lines.append("機種 | 台番 | 出現日数 | 平均差枚 | 最大 | 最小 | 平均G")
        for r in rows[:15]:
            lines.append(f"{r[0]} | {r[1]}番 | {r[2]}日 | {r[3]:+}枚 | {r[4]:+} | {r[5]:+} | {r[6]}G")
        if len(rows) > 15:
            lines.append("---下位---")
            for r in rows[-5:]:
                lines.append(f"{r[0]} | {r[1]}番 | {r[2]}日 | {r[3]:+}枚 | {r[4]:+} | {r[5]:+} | {r[6]}G")
        return "\n".join(lines)
    except Exception as e:
        return f"データ取得エラー: {e}"


def _weekday_summary(hall_name: str) -> str:
    """曜日別傾向サマリー"""
    try:
        conn = sqlite3.connect(HALL_REPORTS_DB)
        rows = conn.execute("""
            SELECT strftime('%w', report_date) as dow,
                   COUNT(*) as cnt,
                   ROUND(AVG(diff_coins)) as avg_diff,
                   ROUND(AVG(CASE WHEN diff_coins > 0 THEN 1.0 ELSE 0.0 END)*100) as win_rate
            FROM hall_day_seat
            WHERE hall_name=? AND bb_prob IS NOT NULL
              AND machine_name NOT LIKE '末尾%'
            GROUP BY dow
            ORDER BY avg_diff DESC
        """, (hall_name,)).fetchall()
        conn.close()
        if not rows:
            return ""
        dow_names = {"0":"日","1":"月","2":"火","3":"水","4":"木","5":"金","6":"土"}
        lines = ["【曜日別傾向】"]
        for r in rows:
            name = dow_names.get(r[0], r[0])
            lines.append(f"{name}曜日: 平均{r[2]:+}枚 / 勝率{r[3]}% ({r[1]}件)")
        return "\n".join(lines)
    except Exception:
        return ""


def _session_summary(limit: int = 20) -> str:
    """直近セッション履歴（正しいカラム名使用）"""
    try:
        conn = sqlite3.connect(SESSIONS_DB)
        rows = conn.execute("""
            SELECT s.machine_name, s.date, s.games_total,
                   s.investment, s.returns, s.diff_coins,
                   s.posterior_json
            FROM sessions s
            ORDER BY s.created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        conn.close()
        if not rows:
            return "セッション履歴: なし"
        lines = ["【直近セッション履歴】"]
        lines.append("機種 | 日付 | G数 | 収支 | 推定設定")
        for r in rows:
            profit = (r[4] or 0) - (r[3] or 0)
            exp_s = ""
            if r[6]:
                try:
                    post = json.loads(r[6])
                    if isinstance(post, dict) and post:
                        exp = sum(float(k)*v for k,v in post.items())
                        high = sum(v for k,v in post.items() if float(k) >= 4)
                        exp_s = f" 推定設定{exp:.1f}(高設定{high:.0%})"
                except Exception:
                    pass
            lines.append(
                f"{r[0]} | {r[1]} | {r[2] or 0}G | {profit:+}枚{exp_s}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"セッション取得エラー: {e}"


def _tail_summary(hall_name: str) -> str:
    try:
        conn = sqlite3.connect(HALL_REPORTS_DB)
        rows = conn.execute("""
            SELECT (seat_number % 10) as tail,
                   COUNT(*) as cnt,
                   ROUND(AVG(diff_coins)) as avg_diff,
                   ROUND(AVG(CASE WHEN diff_coins > 0 THEN 1.0 ELSE 0.0 END)*100) as win_rate
            FROM hall_day_seat
            WHERE hall_name=? AND machine_name NOT LIKE '_%'
              AND machine_name NOT LIKE '末尾%' AND bb_prob IS NOT NULL
            GROUP BY tail
            ORDER BY avg_diff DESC
        """, (hall_name,)).fetchall()
        conn.close()
        if not rows:
            return ""
        lines = ["【末尾別傾向】"]
        for r in rows:
            lines.append(f"末尾{r[0]}: {r[2]:+}枚 / 勝率{r[3]}% ({r[1]}件)")
        return "\n".join(lines)
    except Exception:
        return ""


def _today_targets_summary(hall_name: str) -> str:
    """今日の狙い台サマリー（AIレポート用）"""
    try:
        conn = sqlite3.connect(HALL_REPORTS_DB)
        rows = conn.execute("""
            SELECT machine_name, seat_number,
                   COUNT(*) as days,
                   ROUND(AVG(diff_coins)) as avg_diff,
                   ROUND(AVG(CASE WHEN diff_coins > 0 THEN 1.0 ELSE 0.0 END)*100) as win_rate,
                   ROUND(MIN(diff_coins)) as worst
            FROM hall_day_seat
            WHERE hall_name=? AND machine_name NOT LIKE '末尾%'
              AND machine_name != '_NODATA_' AND bb_prob IS NOT NULL
              AND report_date >= date('now', '-30 days')
            GROUP BY machine_name, seat_number
            HAVING days >= 3
            ORDER BY avg_diff DESC
            LIMIT 5
        """, (hall_name,)).fetchall()
        conn.close()
        if not rows:
            return ""
        lines = ["【推奨狙い台TOP5（30日間平均）】"]
        for i, r in enumerate(rows, 1):
            lines.append(
                f"{i}. {r[0]} {r[1]}番 — 平均{r[2]:+}枚 / 勝率{r[4]}% / 最悪{r[5]:+}枚 ({r[2]}日)"
            )
        return "\n".join(lines)
    except Exception:
        return ""


def _machine_setting_tendency(hall_name: str) -> str:
    """機種別推定設定傾向サマリー（AIレポート用）"""
    try:
        import datetime
        from hall.prior import _estimate_prior_from_anaslo
        conn = sqlite3.connect(HALL_REPORTS_DB)
        rows = conn.execute(
            """SELECT machine_name, COUNT(*) as records,
                      ROUND(AVG(diff_coins)) as avg_diff,
                      ROUND(AVG(bb_prob)*100, 4) as avg_bb_pct
               FROM hall_day_seat
               WHERE hall_name=? AND bb_prob IS NOT NULL
                 AND machine_name NOT LIKE '末尾%' AND machine_name != '_NODATA_'
                 AND report_date >= date('now', '-60 days')
               GROUP BY machine_name HAVING records >= 5
               ORDER BY records DESC LIMIT 10""",
            (hall_name,)
        ).fetchall()
        conn.close()
        if not rows:
            return ""
        today_dow = datetime.date.today().weekday()
        lines = ["【機種別推定設定傾向（60日）】"]
        for r in rows[:8]:
            mname = r[0]
            avg_diff = r[2] or 0
            prior = _estimate_prior_from_anaslo(hall_name, mname, ["1","2","3","4","5","6"], today_dow)
            if prior:
                exp_s = sum(int(s)*p for s,p in prior.items())
                high_p = sum(p for s,p in prior.items() if int(s) >= 4)
                lines.append(f"  {mname}: 推定設定{exp_s:.1f} / 高設定{high_p:.0%} / 平均{avg_diff:+}枚 ({r[1]}件)")
        return "\n".join(lines)
    except Exception:
        return ""


def _bb_surge_summary(hall_name: str) -> str:
    """直近3日でBB確率が急上昇した台（設定入れ替えシグナル）"""
    try:
        import datetime as _dt
        conn = sqlite3.connect(HALL_REPORTS_DB)
        prev_date = (_dt.date.today() - _dt.timedelta(days=3)).isoformat()

        recent = conn.execute(
            """SELECT machine_name, seat_number, AVG(bb_prob) as avg_bb
               FROM hall_day_seat
               WHERE hall_name=? AND bb_prob IS NOT NULL AND report_date >= ?
                 AND machine_name NOT LIKE '末尾%' AND machine_name != '_NODATA_'
               GROUP BY machine_name, seat_number HAVING COUNT(*) >= 1""",
            (hall_name, prev_date)
        ).fetchall()

        baseline = conn.execute(
            """SELECT machine_name, seat_number, AVG(bb_prob) as avg_bb
               FROM hall_day_seat
               WHERE hall_name=? AND bb_prob IS NOT NULL
                 AND report_date < ? AND report_date >= date(?, '-60 days')
                 AND machine_name NOT LIKE '末尾%' AND machine_name != '_NODATA_'
               GROUP BY machine_name, seat_number HAVING COUNT(*) >= 3""",
            (hall_name, prev_date, prev_date)
        ).fetchall()
        conn.close()

        import statistics as _s
        m_bbs: dict[str, list[float]] = {}
        for r in baseline:
            m_bbs.setdefault(r[0], []).append(float(r[2]))
        machine_std = {m: (_s.stdev(v) if len(v) > 1 else 0.001) or 0.001 for m, v in m_bbs.items()}
        bmap = {(r[0], r[1]): float(r[2]) for r in baseline}

        surges = []
        for r in recent:
            base = bmap.get((r[0], r[1]))
            if base is None:
                continue
            std = machine_std.get(r[0], 0.001)
            z = (float(r[2]) - base) / std
            if z >= 0.8:
                surges.append((r[0], r[1], z, float(r[2])*100, base*100))

        if not surges:
            return ""
        surges.sort(key=lambda x: -x[2])
        lines = ["【BB確率急上昇台（設定入れ替えシグナル）直近3日】"]
        for m, s, z, rec_bb, base_bb in surges[:5]:
            lines.append(f"  {m} {s}番: +{z:.1f}σ（ベース{base_bb:.3f}% → 直近{rec_bb:.3f}%）")
        return "\n".join(lines)
    except Exception:
        return ""


def _top_streak_seats(hall_name: str) -> str:
    """連続プラス台（3日以上連続好調）"""
    try:
        conn = sqlite3.connect(HALL_REPORTS_DB)
        rows = conn.execute(
            """SELECT machine_name, seat_number, report_date, diff_coins
               FROM hall_day_seat
               WHERE hall_name=? AND machine_name NOT LIKE '末尾%' AND machine_name != '_NODATA_'
                 AND (bb_prob IS NOT NULL OR ev_pct IS NOT NULL)
                 AND report_date >= date('now', '-14 days')
               ORDER BY machine_name, seat_number, report_date DESC""",
            (hall_name,)
        ).fetchall()
        conn.close()

        from collections import defaultdict
        seat_data: dict = defaultdict(list)
        for m, s, d, diff in rows:
            seat_data[(m, s)].append(diff or 0)

        streaks = []
        for (m, s), diffs in seat_data.items():
            streak = 0
            for d in diffs:
                if d > 0:
                    streak += 1
                else:
                    break
            if streak >= 3:
                streaks.append((m, s, streak, sum(diffs[:streak])//streak))

        if not streaks:
            return ""
        streaks.sort(key=lambda x: -x[2])
        lines = ["【連続好調台（直近3日以上プラス）】"]
        for m, s, k, avg in streaks[:5]:
            lines.append(f"  {m} {s}番: {k}連続プラス（平均{avg:+}枚）")
        return "\n".join(lines)
    except Exception:
        return ""


def _event_day_hint(hall_name: str) -> str:
    """今日がイベント日候補かどうかをBBパターン統計から判定"""
    try:
        import datetime as _dt
        import statistics as _s
        conn = sqlite3.connect(HALL_REPORTS_DB)
        rows = conn.execute(
            """SELECT report_date, AVG(bb_prob) as avg_bb, COUNT(*) as cnt
               FROM hall_day_seat
               WHERE hall_name=? AND bb_prob IS NOT NULL
                 AND machine_name NOT LIKE '末尾%' AND machine_name != '_NODATA_'
                 AND report_date >= date('now', '-180 days')
               GROUP BY report_date HAVING cnt >= 3""",
            (hall_name,)
        ).fetchall()
        conn.close()
        if len(rows) < 10:
            return ""

        all_bbs = [float(r[1]) for r in rows]
        global_mean = _s.mean(all_bbs)
        global_std  = _s.stdev(all_bbs) if len(all_bbs) > 1 else 0.001

        today = _dt.date.today()
        today_tail = today.day % 10
        today_dow  = ["月","火","水","木","金","土","日"][today.weekday()]
        today_dow_int = (today.weekday() + 1) % 7  # strftime %w (0=Sun)
        is_five = today.day in (5, 15, 25)

        hits = []
        for tail in [today_tail]:
            p = [float(r[1]) for r in rows if _dt.date.fromisoformat(r[0]).day % 10 == tail]
            if len(p) >= 3:
                z = (sum(p)/len(p) - global_mean) / max(global_std, 1e-8)
                if z >= 0.5:
                    hits.append(f"末尾{tail}の日(+{z:.1f}σ)")
        for dow_i, dow_n in [(today_dow_int, today_dow)]:
            p = [float(r[1]) for r in rows if _dt.date.fromisoformat(r[0]).weekday() == (dow_i - 1) % 7]
            if len(p) >= 3:
                z = (sum(p)/len(p) - global_mean) / max(global_std, 1e-8)
                if z >= 0.5:
                    hits.append(f"{dow_n}曜日(+{z:.1f}σ)")
        if is_five:
            p = [float(r[1]) for r in rows if _dt.date.fromisoformat(r[0]).day in (5,15,25)]
            if len(p) >= 2:
                z = (sum(p)/len(p) - global_mean) / max(global_std, 1e-8)
                if z >= 0.5:
                    hits.append(f"5・15・25日(+{z:.1f}σ)")

        if not hits:
            return f"【今日({today.month}/{today.day} {today_dow}曜日)】イベント日特有のパターンなし"
        return (f"【今日({today.month}/{today.day} {today_dow}曜日)はイベント日候補！】\n"
                f"  該当パターン: {'、'.join(hits)}\n  → 高設定比率が統計的に上昇する傾向あり")
    except Exception:
        return ""


def _zone_summary(hall_name: str) -> str:
    """台番ゾーン別BB傾向（AIレポート用）"""
    try:
        import statistics as _s
        conn = sqlite3.connect(HALL_REPORTS_DB)
        rows = conn.execute(
            """SELECT seat_number, AVG(bb_prob) as avg_bb
               FROM hall_day_seat
               WHERE hall_name=? AND bb_prob IS NOT NULL AND seat_number IS NOT NULL
                 AND machine_name NOT LIKE '末尾%' AND machine_name != '_NODATA_'
                 AND report_date >= date('now', '-90 days')
               GROUP BY seat_number HAVING COUNT(*) >= 3""",
            (hall_name,)
        ).fetchall()
        conn.close()
        if len(rows) < 5:
            return ""

        zone_data: dict[int, list[float]] = {}
        for seat, bb in rows:
            z = ((int(seat)-1)//10)*10+1
            zone_data.setdefault(z, []).append(float(bb))

        if len(zone_data) < 2:
            return ""
        all_bbs = [v for vals in zone_data.values() for v in vals]
        gm = _s.mean(all_bbs)
        gs = _s.stdev(all_bbs) if len(all_bbs) > 1 else 0.001

        zones = [(z, sum(v)/len(v), (sum(v)/len(v)-gm)/max(gs,1e-8))
                 for z, v in zone_data.items()]
        zones.sort(key=lambda x: -x[2])
        top = zones[:3]
        lines = ["【台番ゾーン別BB傾向TOP3（高設定が集まりやすいゾーン）】"]
        for z_start, mean_bb, zs in top:
            lines.append(f"  {z_start}~{z_start+9}番台: BB {mean_bb*100:.3f}% ({'+' if zs>=0 else ''}{zs:.1f}σ)")
        return "\n".join(lines)
    except Exception:
        return ""


def _today_dow_best(hall_name: str) -> str:
    """本日曜日に強い機種TOP5"""
    try:
        import datetime
        today = datetime.date.today()
        sql_dow = str((today.weekday()+1) % 7)
        dow_ja = ["月","火","水","木","金","土","日"][today.weekday()]
        conn = sqlite3.connect(HALL_REPORTS_DB)
        rows = conn.execute(
            """SELECT machine_name, COUNT(*) as cnt,
                      ROUND(AVG(diff_coins)) as avg_diff,
                      ROUND(AVG(CASE WHEN diff_coins > 0 THEN 1.0 ELSE 0.0 END)*100) as win_rate
               FROM hall_day_seat
               WHERE hall_name=? AND strftime('%w',report_date)=?
                 AND machine_name NOT LIKE '末尾%' AND machine_name != '_NODATA_'
                 AND (bb_prob IS NOT NULL OR ev_pct IS NOT NULL)
                 AND report_date >= date('now', '-120 days')
               GROUP BY machine_name HAVING cnt >= 3
               ORDER BY avg_diff DESC LIMIT 5""",
            (hall_name, sql_dow)
        ).fetchall()
        conn.close()
        if not rows:
            return ""
        lines = [f"【{dow_ja}曜日に強い機種TOP5（過去120日同曜日）】"]
        for i, r in enumerate(rows, 1):
            lines.append(f"  {i}. {r[0]}: 平均{r[2]:+}枚 / 勝率{r[3]}% ({r[1]}日)")
        return "\n".join(lines)
    except Exception:
        return ""


def _load_machine_theory(machine_name: str) -> str:
    """機種の設定別理論値テーブルを文字列で返す（AIへの参考データ）"""
    path = MACHINES_DIR / f"{machine_name}.json"
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        settings = data.get("settings", [])
        elements = data.get("elements", [])
        kw = data.get("machine_kw", {})
        lines = [f"【{machine_name} 設定別理論値】"]
        # 機械割
        if kw:
            kw_str = " / ".join(f"設定{s}:{kw.get(s,'?'):.1%}" for s in settings if s in kw)
            lines.append(f"機械割: {kw_str}")
        # 各要素
        for el in elements:
            p = el.get("p", {})
            name = el["name"]
            vals = []
            for s in settings:
                prob = p.get(s, 0)
                if prob > 0:
                    if prob < 0.01:
                        # 1/x 表記
                        vals.append(f"設{s}:1/{1/prob:.0f}")
                    else:
                        # パーセント表記
                        vals.append(f"設{s}:{prob:.1%}")
            lines.append(f"  {name}: {' / '.join(vals)}")
        return "\n".join(lines)
    except Exception:
        return ""


def _call(client, messages: list, max_tokens: int = 800) -> str:
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            max_tokens=max_tokens,
            messages=messages,
        )
        return resp.choices[0].message.content
    except Exception as e:
        err = str(e)
        if "rate_limit" in err.lower():
            return "レート制限に達しました。少し待ってから再試行してください。"
        if "invalid_api_key" in err.lower() or "authentication" in err.lower():
            return "APIキーが無効です。GROQ_API_KEYを確認してください。"
        return f"AIエラー: {err[:200]}"


# ---------------------------------------------------------------------------
# A. チャット
# ---------------------------------------------------------------------------

def chat(message: str, hall_name: str, history: list[dict]) -> str:
    client = _get_client()
    if not client:
        return "GROQ_API_KEY が設定されていません。"

    context = "\n\n".join(filter(None, [
        _hall_summary(hall_name),
        _tail_summary(hall_name),
        _weekday_summary(hall_name),
        _today_targets_summary(hall_name),
        _session_summary(),
    ]))

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for h in history[-6:]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({
        "role": "user",
        "content": f"【参考データ】\n{context}\n\n【質問】\n{message}"
    })

    return _call(client, messages, max_tokens=800)


# ---------------------------------------------------------------------------
# B. 自動レポート
# ---------------------------------------------------------------------------

def generate_report(hall_name: str) -> str:
    client = _get_client()
    if not client:
        return "GROQ_API_KEY が設定されていません。"

    context = "\n\n".join(filter(None, [
        _event_day_hint(hall_name),
        _hall_summary(hall_name, days=30),
        _tail_summary(hall_name),
        _zone_summary(hall_name),
        _weekday_summary(hall_name),
        _today_dow_best(hall_name),
        _machine_setting_tendency(hall_name),
        _today_targets_summary(hall_name),
        _bb_surge_summary(hall_name),
        _top_streak_seats(hall_name),
        _session_summary(10),
    ]))

    prompt = f"""以下のデータを元に「{hall_name}の今日の攻略レポート」を作成してください。

{context}

レポートに含める内容（データがある部分のみ）:
0. 今日がイベント日候補かどうか（データある場合は必ず最初に言及）
1. 狙い目の台番ベスト3（機種名・台番・理由を明記。平均差枚・勝率・BB確率を根拠にする）
2. 機種別推定設定傾向から判断した「今日打つべき機種」TOP2（推定設定と高設定確率を根拠に）
3. BB確率急上昇台（設定入れ替えが疑われる台 — 最優先狙い候補）
4. 連続好調台（3日以上プラス継続中の台）
5. 本日曜日に強い機種（曜日特化傾向から）
6. 末尾傾向・ゾーン傾向から見た立ち回り方針（高設定が集まりやすいゾーンに言及）
7. 避けるべき台番ワースト2（具体的な数字で理由を示す）
8. 今日のひとこと総括と最重要狙い台1点

・データが少ない場合は「データ不足のため参考程度」と明記
・具体的な数字（差枚・勝率・推定設定・BBσ）を使って信頼性を示すこと
・BB急上昇はσ値が高いほど設定入れ替えの証拠として強力であることを考慮すること
・推奨台は「○○の△番台（平均+□□枚、高設定確率○○%）」の形式で示すこと
・ゾーン傾向が強い場合は「○○~○○番台エリアを優先」と立ち回り方針に組み込むこと"""

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    return _call(client, messages, max_tokens=1400)


# ---------------------------------------------------------------------------
# C. 設定推測コメント（機種理論値付き）
# ---------------------------------------------------------------------------

def comment_estimate(
    machine_name: str,
    games: int,
    element_counts: dict,
    posterior: dict,
    ev: float,
    recommendation: str,
    element_analysis: list | None = None,
    credible_interval: list | None = None,
    element_powers: dict | None = None,
    correlated_elements: list | None = None,
) -> str:
    client = _get_client()
    if not client:
        return ""

    post_str = " / ".join([
        f"設定{k}: {v:.1%}" for k, v in sorted(posterior.items())
    ])

    exp_setting = sum(float(k)*v for k,v in posterior.items())
    high_prob   = sum(v for k,v in posterior.items() if float(k) >= 4)

    # 実測値 vs 理論値の比較文
    rate_lines = []
    if element_analysis:
        for el in element_analysis:
            obs_n = el.get("observed_per_n")
            name  = el["name"]
            closest = el.get("closest_setting", "?")
            direction = el.get("direction", "")
            dir_txt = "理論より高め" if direction == "up" else "理論より低め"
            theory = el.get("theoretical", {})
            # 設定1と設定6の理論値を示す
            low_t  = theory.get("1", 0)
            high_t = theory.get("6", 0)
            if obs_n and low_t > 0 and high_t > 0:
                low_1  = f"1/{1/low_t:.0f}"  if low_t < 0.01  else f"{low_t:.1%}"
                high_6 = f"1/{1/high_t:.0f}" if high_t < 0.01 else f"{high_t:.1%}"
                obs_s  = f"1/{obs_n:.0f}"    if el["observed"] < 0.01 else f"{el['observed']:.1%}"
                rate_lines.append(
                    f"  {name}: 実測{obs_s} (設1理論{low_1}〜設6理論{high_6}) → {dir_txt}、設{closest}相当"
                )

    # 機種理論値テーブル
    theory_text = _load_machine_theory(machine_name)

    count_str = " / ".join(f"{k}:{v}" for k,v in element_counts.items()) if element_counts else "（未入力）"

    ci_str = ""
    if credible_interval and len(credible_interval) == 2:
        ci_str = f"\n90%信用区間: 設定{credible_interval[0]:.0f}〜設定{credible_interval[1]:.0f}"

    powers_str = ""
    if element_powers:
        sorted_powers = sorted(element_powers.items(), key=lambda x: -x[1])
        powers_str = "\n要素識別力ランキング: " + ", ".join(f"{n}({v:.1f})" for n,v in sorted_powers[:4])

    corr_str = ""
    if correlated_elements:
        corr_str = f"\n注意: 要素間に高相関あり（二重計上の可能性）: {', '.join(f'{a}↔{b}' for a,b,_ in correlated_elements)}"

    prompt = f"""機種: {machine_name}
総ゲーム数: {games}G
入力カウント: {count_str}

【実測 vs 理論比較】
{chr(10).join(rate_lines) if rate_lines else "（カウントデータなし）"}

【設定推測結果】
事後確率: {post_str}
期待設定値: {exp_setting:.2f} / 高設定(4以上)確率: {high_prob:.1%}{ci_str}
期待値: {ev:+.0f}枚/1000G
推奨: {recommendation}{powers_str}{corr_str}

{theory_text}

上記の推測結果について、4〜5文で実践的にコメントしてください:
1. 現在のデータが示す設定の可能性（理論値との比較を使って）
2. このゲーム数での信頼性（信用区間と識別力の高い要素に言及）
3. 続行・撤退の具体的判断ポイント
4. 追加で観察すべき最重要要素（識別力ランキングから選ぶ）"""

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    try:
        return _call(client, messages, max_tokens=400)
    except Exception:
        return ""
