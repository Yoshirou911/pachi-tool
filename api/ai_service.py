"""
Claude API を使ったパチスロ分析AIサービス
- チャット (A): データを渡してQ&A
- 自動レポート (B): 今日の攻略レポート生成
- 設定推測コメント (C): 推測結果の解釈
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional

import anthropic

try:
    from config import HALL_REPORTS_DB, SESSIONS_DB
except ImportError:
    _base = Path(__file__).parent.parent / "data"
    HALL_REPORTS_DB = _base / "hall_reports.db"
    SESSIONS_DB = _base / "sessions.db"

CHAT_MODEL = "claude-haiku-4-5-20251001"
REPORT_MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """あなたはパチスロ専門のデータアナリストです。
ユーザーのパチスロ実戦データと店舗の台別データを分析し、
簡潔で実践的なアドバイスを日本語で提供します。

回答のルール:
- 必ず日本語で答える
- 数字は具体的に示す
- 投資判断は「〇〇円以内」など明確に
- 絵文字は使わない
- 長すぎず、要点を絞る"""


def _get_client() -> Optional[anthropic.Anthropic]:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return None
    return anthropic.Anthropic(api_key=key)


def _hall_summary(hall_name: str, days: int = 30) -> str:
    """DB から店舗の台番サマリーを生成"""
    try:
        conn = sqlite3.connect(HALL_REPORTS_DB)
        rows = conn.execute("""
            SELECT machine_name, seat_number,
                   COUNT(*) as cnt,
                   ROUND(AVG(diff_coins)) as avg_diff,
                   MAX(diff_coins) as max_diff,
                   MIN(diff_coins) as min_diff
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
        lines = [f"【{hall_name} 過去{days}日 台番ランキング（上位/下位）】"]
        lines.append("機種名 | 台番 | 出現日数 | 平均差枚 | 最大 | 最小")
        for r in rows[:15]:
            lines.append(f"{r[0]} | {r[1]}番 | {r[2]}日 | {r[3]:+}枚 | {r[4]:+} | {r[5]:+}")
        if len(rows) > 15:
            lines.append("---下位---")
            for r in rows[-5:]:
                lines.append(f"{r[0]} | {r[1]}番 | {r[2]}日 | {r[3]:+}枚 | {r[4]:+} | {r[5]:+}")
        return "\n".join(lines)
    except Exception as e:
        return f"データ取得エラー: {e}"


def _session_summary(limit: int = 20) -> str:
    """直近のセッション履歴サマリー"""
    try:
        conn = sqlite3.connect(SESSIONS_DB)
        rows = conn.execute("""
            SELECT machine_name, started_at,
                   final_games, final_bb, final_rb,
                   coins_in, coins_out
            FROM sessions
            ORDER BY started_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        conn.close()
        if not rows:
            return "セッション履歴: なし"
        lines = ["【直近セッション履歴】"]
        lines.append("機種 | 日時 | G数 | BB | RB | 収支")
        for r in rows:
            profit = (r[6] or 0) - (r[5] or 0)
            lines.append(
                f"{r[0]} | {r[1][:10]} | {r[2] or 0}G | "
                f"BB{r[3] or 0} | RB{r[4] or 0} | {profit:+}枚"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"セッション取得エラー: {e}"


def _tail_summary(hall_name: str) -> str:
    """末尾別傾向サマリー"""
    try:
        conn = sqlite3.connect(HALL_REPORTS_DB)
        rows = conn.execute("""
            SELECT (seat_number % 10) as tail,
                   COUNT(*) as cnt,
                   ROUND(AVG(diff_coins)) as avg_diff
            FROM hall_day_seat
            WHERE hall_name=? AND machine_name NOT LIKE '_%'
            GROUP BY tail
            ORDER BY avg_diff DESC
        """, (hall_name,)).fetchall()
        conn.close()
        if not rows:
            return ""
        lines = ["【末尾別傾向】"]
        for r in rows:
            lines.append(f"末尾{r[0]}: {r[2]:+}枚 ({r[1]}件)")
        return "\n".join(lines)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# A. チャット
# ---------------------------------------------------------------------------

def chat(message: str, hall_name: str, history: list[dict]) -> str:
    """ユーザーの質問にデータを踏まえて回答"""
    client = _get_client()
    if not client:
        return "ANTHROPIC_API_KEY が設定されていません。"

    context = "\n\n".join(filter(None, [
        _hall_summary(hall_name),
        _tail_summary(hall_name),
        _session_summary(),
    ]))

    messages = []
    for h in history[-6:]:  # 直近6往復まで
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({
        "role": "user",
        "content": f"【参考データ】\n{context}\n\n【質問】\n{message}"
    })

    try:
        resp = client.messages.create(
            model=CHAT_MODEL,
            max_tokens=800,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        return resp.content[0].text
    except Exception as e:
        err = str(e)
        if "credit balance is too low" in err:
            return "クレジット残高が不足しています。console.anthropic.com でチャージしてください。"
        return f"AIエラー: {err[:200]}"


# ---------------------------------------------------------------------------
# B. 自動レポート
# ---------------------------------------------------------------------------

def generate_report(hall_name: str) -> str:
    """今日の攻略レポートを生成"""
    client = _get_client()
    if not client:
        return "ANTHROPIC_API_KEY が設定されていません。"

    context = "\n\n".join(filter(None, [
        _hall_summary(hall_name, days=30),
        _tail_summary(hall_name),
        _session_summary(10),
    ]))

    prompt = f"""以下のデータを元に「{hall_name}の今日の攻略レポート」を作成してください。

{context}

レポートに含める内容:
1. 狙い目の台番ベスト3（機種名・台番・理由）
2. 避けるべき台番ワースト3
3. 末尾傾向から見た立ち回り方針
4. 自分の実戦履歴から見た課題点
5. 今日のひとこと（総括）

データが少ない場合は「データ不足のため参考程度」と明記してください。"""

    try:
        resp = client.messages.create(
            model=REPORT_MODEL,
            max_tokens=1200,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text
    except Exception as e:
        err = str(e)
        if "credit balance is too low" in err:
            return "クレジット残高が不足しています。console.anthropic.com でチャージしてください。"
        return f"AIエラー: {err[:200]}"


# ---------------------------------------------------------------------------
# C. 設定推測コメント
# ---------------------------------------------------------------------------

def comment_estimate(machine_name: str, games: int, bb: int, rb: int,
                     posterior: dict, ev: float, recommendation: str) -> str:
    """設定推測結果にAIコメントを追加"""
    client = _get_client()
    if not client:
        return ""

    post_str = " | ".join([
        f"設定{k}: {v:.1%}" for k, v in sorted(posterior.items())
    ])

    prompt = f"""機種: {machine_name}
ゲーム数: {games}G / BB: {bb}回 / RB: {rb}回
事後確率: {post_str}
期待値: {ev:+.0f}枚/1000G
推奨: {recommendation}

上記の設定推測結果について、以下を3〜4文で簡潔にコメントしてください:
- 現在のデータから読み取れる設定の可能性
- 続行・撤退の判断ポイント
- 追加で注目すべき挙動（もしあれば）"""

    try:
        resp = client.messages.create(
            model=CHAT_MODEL,
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text
    except Exception:
        return ""
