"""Run local tests once and write a reviewable release verification artifact.

Usage: .venv/Scripts/python.exe scripts/verify_ai_release.py
No provider is called. Test runtime data stays in a disposable DATA_DIR.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from api.ai_review import EVIDENCE_PATH, EVIDENCE_SCHEMA, PROTOCOL, REQUIRED_PATHS, file_hash
from app_version import APP_VERSION


def test_environment(directory):
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("PACHI_AI_") or key.endswith("_API_KEY") or key in {
                "PYTEST_ADDOPTS", "PYTEST_PLUGINS"}:
            env.pop(key)
    env.update(DATA_DIR=str(Path(directory) / "data"), PACHI_AI_ALLOW_EXTERNAL="false",
               PYTHONIOENCODING="utf-8")
    return env


def main():
    node = shutil.which("node")
    if not node:
        raise SystemExit("Node.js is required for local UI checks")
    before = {name: file_hash(ROOT / name) for name in REQUIRED_PATHS}
    with tempfile.TemporaryDirectory(prefix="pachi-ai-release-") as directory:
        # No inherited provider credentials or production data directory in tests.
        env = test_environment(directory)
        report_file = Path(directory) / "pytest.xml"
        args = [sys.executable, "-m", "pytest", "tests", "-q", "-o", "addopts=", f"--junitxml={report_file}"]
        run = subprocess.run(args, cwd=ROOT, env=env, check=False)
        if run.returncode:
            raise SystemExit("Tests failed; previous verification evidence was left unchanged")
        stats = defaultdict(lambda: {"passed": 0, "failed": 0, "errors": 0, "skipped": 0})
        for case in ET.parse(report_file).getroot().iter("testcase"):
            suite = case.attrib.get("classname", "").split(".")
            name = "/".join(suite[:2]) + ".py"
            category = "failed" if case.find("failure") is not None else "errors" if case.find("error") is not None else "skipped" if case.find("skipped") is not None else "passed"
            stats[name][category] += 1
        ui_passed = []
        for test in sorted((ROOT / "tests").glob("*_test.mjs")):
            if subprocess.run([node, str(test)], cwd=ROOT, env=env, check=False).returncode:
                raise SystemExit("UI tests failed; previous verification evidence was left unchanged")
            ui_passed.append(test.relative_to(ROOT).as_posix())
        syntax_checked = ["mobile/app.js", "web/js/app.js"]
        for name in syntax_checked:
            if subprocess.run([node, "--check", name], cwd=ROOT, env=env, check=False).returncode:
                raise SystemExit("Script syntax check failed")
        after = {name: file_hash(ROOT / name) for name in REQUIRED_PATHS}
        if before != after:
            raise SystemExit("Source changed during verification; evidence not saved")
        report = {"schema": EVIDENCE_SCHEMA, "protocol": PROTOCOL, "app_version": APP_VERSION,
            "verified_at": datetime.now(timezone.utc).isoformat(), "verification_kind": "local_automated",
            "external_ai_measured": False, "source_hashes": after,
            "python": {"exit_code": 0, **{key: sum(row[key] for row in stats.values()) for key in ("passed", "failed", "errors", "skipped")}, "suites": dict(stats)},
            "ui": {"failed": 0, "passed_suites": ui_passed, "syntax_checked": syntax_checked}}
        from api.ai_review import verify_evidence
        candidate = Path(directory) / "verification.json"
        candidate.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if not verify_evidence(candidate, ROOT)["valid"]:
            raise SystemExit("Verification artifact did not satisfy the review contract")
        EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE_PATH.write_bytes(candidate.read_bytes())
        print(f"Recorded v{APP_VERSION}: {report['python']['passed']} Python tests / {len(ui_passed)} UI scripts")


if __name__ == "__main__":
    main()
