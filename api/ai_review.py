"""Read-only AI adoption review bound to recorded local verification evidence.

An implementation test is never treated as a provider benchmark or a forecast
accuracy measurement. Loading this report cannot enable an AI or change ranking.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from app_version import APP_VERSION
from config import ROOT

PROTOCOL = "ai-adoption-review-1.0"
EVIDENCE_SCHEMA = "ai-release-verification-1.0"
EVIDENCE_PATH = ROOT / "data" / "ai_review" / "verification_3.49.json"
LABELS = {"adopted": "採用", "assistance_only": "補助限定", "pending": "保留", "rejected": "不採用"}

# Paths are controlled by code, never by an uploaded manifest or API request.
CORE_PATHS = (
    "app_version.py", "config.py", "api/ai_provider.py", "api/ai_service.py",
    "api/ai_comparison.py", "api/ai_evaluation.py", "api/ai_governance.py",
    "api/ai_evidence.py", "api/ai_guard.py", "api/ai_hall_analysis.py", "api/ai_knowledge.py",
    "api/ai_review.py", "api/routers/ai.py", "api/image_analysis.py", "api/routers/image_analysis.py",
    "hall/names.py", "hall/machine_scope.py", "hall/collection_sources.py", "hall/regions.py",
    "hall/seat_history.py", "requirements.txt",
    "mobile/ai-review.mjs", "mobile/ai-evidence.mjs", "mobile/ai-knowledge.mjs",
    "mobile/hall-ai.mjs", "mobile/ai-comparison.mjs", "mobile/ai-evaluation.mjs",
    "mobile/image-analysis.mjs", "mobile/ocr.mjs", "mobile/app.js", "mobile/index.html", "mobile/sw.js",
    "web/js/app.js", "web/index.html", "web/sw.js", "pachi-tool.spec", "desktop/version_info.txt",
    "data/ai_eval/suite_v2.json", "data/knowledge/documents_v1.json",
    "data/knowledge/retrieval_cases_v1.json", "data/opportunity_catalog.json",
    "scripts/verify_ai_release.py",
)
PYTHON_SUITES = (
    "tests/test_ai_provider.py", "tests/test_ai_evidence.py", "tests/test_ai_event_forecast.py",
    "tests/test_ai_hall_analysis.py", "tests/test_ai_comparison.py", "tests/test_ai_evaluation.py",
    "tests/test_ai_governance.py", "tests/test_ai_guard.py", "tests/test_ai_knowledge.py",
    "tests/test_image_analysis.py", "tests/test_ai_review.py",
)
UI_SUITES = (
    "tests/ai_review_ui_test.mjs", "tests/ai_evidence_ui_test.mjs", "tests/ai_knowledge_ui_test.mjs",
    "tests/hall_ai_ui_test.mjs", "tests/ai_comparison_ui_test.mjs", "tests/ai_evaluation_ui_test.mjs",
    "tests/image_analysis_ui_test.mjs", "tests/mobile_ui_contract_test.mjs", "tests/web_ui_contract_test.mjs",
)
# Include transitive application dependencies and regression tests as well as the
# explicit AI entry points. A newly added/deleted source also expires the record.
# Only repository-owned source directories are scanned, never manifest paths.
DEPENDENCY_PATHS = tuple(sorted(path.relative_to(ROOT).as_posix()
    for directory in ("api", "hall", "scraper", "records", "analysis", "mobile", "web", "tests")
    for path in (ROOT / directory).rglob("*")
    if path.is_file() and path.suffix in {".py", ".js", ".mjs", ".html", ".css"}))
REQUIRED_PATHS = tuple(dict.fromkeys((*CORE_PATHS, *PYTHON_SUITES, *UI_SUITES,
                                    "tests/asset_version.mjs", *DEPENDENCY_PATHS)))


def file_hash(path: Path) -> str:
    # Git/Windows line-ending conversion is not a source-code change.
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify_evidence(path=EVIDENCE_PATH, root=ROOT, *, now=None) -> dict:
    failed = {"valid": False, "status": "missing", "reason": "この版の検証記録がありません。再点検が必要です。",
              "changed_files": [], "verified_at": None, "python_passed": None, "ui_passed": None}
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 1_000_000:
            raise ValueError("oversized")
        report = json.loads(raw)
        if report["schema"] != EVIDENCE_SCHEMA or report["protocol"] != PROTOCOL or report["app_version"] != APP_VERSION:
            return {**failed, "status": "wrong_version", "reason": "検証記録の版が現在と一致しません。"}
        tested_at = datetime.fromisoformat(report["verified_at"])
        if tested_at.tzinfo is None or tested_at > (now or datetime.now(timezone.utc)):
            raise ValueError("invalid time")
        python, ui = report["python"], report["ui"]
        if (report["verification_kind"] != "local_automated" or report["external_ai_measured"] is not False
                or python["exit_code"] != 0 or type(python["passed"]) is not int or python["passed"] < 1
                or python["failed"] != 0 or python["errors"] != 0 or python["skipped"] != 0
                or ui["failed"] != 0 or ui["syntax_checked"] != ["mobile/app.js", "web/js/app.js"]):
            raise ValueError("incomplete verification")
        for suite in PYTHON_SUITES:
            stats = python["suites"][suite]
            if type(stats["passed"]) is not int or stats["passed"] < 1 or any(stats[k] != 0 for k in ("failed", "errors", "skipped")):
                raise ValueError("unverified suite")
        totals = {key: 0 for key in ("passed", "failed", "errors", "skipped")}
        for stats in python["suites"].values():
            for key in totals:
                if type(stats[key]) is not int or stats[key] < 0:
                    raise ValueError("invalid test counts")
                totals[key] += stats[key]
        if any(python[key] != count for key, count in totals.items()):
            raise ValueError("inconsistent test counts")
        if not isinstance(ui["passed_suites"], list) or not all(isinstance(name, str) for name in ui["passed_suites"]):
            raise ValueError("invalid ui suites")
        if not set(UI_SUITES).issubset(ui["passed_suites"]) or len(ui["passed_suites"]) != len(set(ui["passed_suites"])):
            raise ValueError("unverified ui")
        hashes = report["source_hashes"]
        if set(hashes) != set(REQUIRED_PATHS):
            raise ValueError("incomplete source manifest")
        changed = []
        for name in REQUIRED_PATHS:
            try:
                matches = file_hash(Path(root) / name) == hashes[name]
            except (OSError, UnicodeError):
                matches = False
            if not matches:
                changed.append(name)
        details = {"verified_at": report["verified_at"], "python_passed": python["passed"],
                   "ui_passed": len(ui["passed_suites"]), "changed_files": changed}
        if changed:
            return {**failed, **details, "status": "stale", "reason": "点検後にコード・資料が変わったか、確認用ファイルがありません。再点検が必要です。"}
        return {**details, "valid": True, "status": "verified", "reason": "現在のコード・資料とローカル検証記録が一致しています。"}
    except FileNotFoundError:
        return failed
    except (OSError, ValueError, KeyError, TypeError, AttributeError, UnicodeError):
        return {**failed, "status": "invalid", "reason": "検証記録を確認できません。合格とは扱いません。"}


FEATURES = (
    ("transport", "AI接続基盤", "pending", "通信形式・失敗時の処理は模擬試験済み。各社への実通信は未測定。", "設定した接続先での有料実測"),
    ("evidence", "根拠付き説明", "assistance_only", "保存済みの数値・出典を固定文で説明。確定設定や将来の勝率は判定しない。", "実店舗資料で説明の妥当性を確認"),
    ("hall", "店舗のクセ・機種傾向", "assistance_only", "公開履歴の曜日・機種・台番号などを比較。公開範囲の偏りが残る。", "同店・同機種・複数台の時点付き履歴を拡充"),
    ("comparison", "複数AI比較", "pending", "同じ根拠で比較する仕組みは点検済み。優秀なAIはまだ選定していない。", "同条件での外部AI成績と費用を蓄積"),
    ("evaluation", "固定問題による点検", "adopted", "開発用の点検として採用。架空問題の成績は実店舗予測の的中率と別。", "実店舗の独立した正解付き問題を追加"),
    ("image", "画像・数字の読み取り", "assistance_only", "元画像との照合・訂正を前提にした入力補助。実iPhoneの読み取り精度は未測定。", "実端末で照明・角度・数字の読み取りを確認"),
    ("guard", "誤回答チェック", "adopted", "対象・日付・数値・根拠IDの点検として採用。元資料の真偽は別途確認が必要。", "実例から混同・不足パターンを追加"),
    ("knowledge", "専用資料の検索", "assistance_only", "登録条件・版・出典付きで引用。資料の鮮度や全機種の網羅性は保証しない。", "出典再確認と機種別の適用条件を拡充"),
    ("governance", "費用・送信内容の管理", "adopted", "料金枠・同時実行・二重送信・個人情報の形式検査を開発用試験で確認。請求額の保証ではない。", "外部実測で報告使用量と実請求を照合"),
    ("prediction", "AIによる順位・着席判断の変更", "rejected", "実戦成績の根拠がないため、この版では採用しない。", "独立した事前予測と後日の答え合わせが必要"),
)


def build_review(*, evidence_path=EVIDENCE_PATH, root=ROOT, governance_status=None) -> dict:
    verification = verify_evidence(evidence_path, root)
    features = []
    for key, label, intended, scope, remaining in FEATURES:
        status = intended if verification["valid"] or intended == "rejected" else "pending"
        features.append({"id": key, "label": label, "status": status, "status_label": LABELS[status],
                         "scope": scope if verification["valid"] or intended == "rejected" else "再点検まで利用範囲の認定を保留。" + scope,
                         "next_step": remaining, "local_evidence_current": verification["valid"]})
    provider_rows = []
    for provider in (governance_status or {}).get("providers", []):
        # Configured credentials/prices are not performance evidence. Do not echo
        # arbitrary config, request metadata or source text into this report.
        provider_rows.append({k: provider.get(k) for k in ("provider", "label", "price_configured", "monthly_limit_usd", "month_reserved_usd", "remaining_usd")})
        provider_rows[-1].update(status="pending", status_label="保留", external_performance="未審査", adopted=False)
    return {"protocol": PROTOCOL, "app_version": APP_VERSION,
        "overall_status": "limited_local_use" if verification["valid"] else "recheck_required",
        "overall_label": "ローカル機能を用途限定で採用" if verification["valid"] else "再点検が必要",
        "verification": verification, "features": features, "providers": provider_rows,
        "counts": dict(Counter(item["status"] for item in features)),
        "active_explanation": "無料の統計説明（既定）", "adopted_external_provider": None,
        "changes_live_prediction": False, "external_calls": 0,
        "notice": "ここはAI機能を使える範囲の審査です。予測的中率・高設定率・利益を認定するものではありません。",
        "switch_policy": ["外部AIの実測結果は自動採用しない。次の審査で接続先・モデル・問題集・費用条件を固定して判断する。",
                          "外部説明は毎回確認して実行。停止・通信失敗・不正回答時は固定の統計説明に戻る。",
                          "復帰時は外部AIのチェックを外す。サーバー側でPACHI_AI_ALLOW_EXTERNAL=falseにして再起動すると外部送信を停止できる。"],
        "unverified": ["各社AIの実回答品質・実請求額", "実店舗の独立した正解による評価", "実iPhone・EXEでの操作", "公開版への配信・復帰試験"]}
