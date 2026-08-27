"""PACHI TOOL Windows desktop entry point.

The FastAPI application runs only on localhost and is rendered inside the
native WebView2 window. User databases live outside the executable so an app
update can replace the program without deleting records.
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path


APP_NAME = "PACHI TOOL"
APP_DIR_NAME = "PachiTool"
DATABASE_NAMES = ("sessions.db", "opportunities.db", "hall_reports.db")


def user_data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    return base / APP_DIR_NAME / "data"


def legacy_data_dir() -> Path | None:
    if getattr(sys, "frozen", False):
        exe_path = Path(sys.executable).resolve()
        if len(exe_path.parents) >= 3:
            candidate = exe_path.parents[2] / "data"
            return candidate if candidate.exists() else None
        return None
    candidate = Path(__file__).resolve().parent / "data"
    return candidate if candidate.exists() else None


def migrate_legacy_databases(destination: Path, source: Path | None = None) -> list[str]:
    source = source or legacy_data_dir()
    if source is None or source.resolve() == destination.resolve():
        return []
    migrated: list[str] = []
    for name in DATABASE_NAMES:
        source_path = source / name
        destination_path = destination / name
        if source_path.exists() and not destination_path.exists():
            shutil.copy2(source_path, destination_path)
            migrated.append(name)
    return migrated


def configure_environment() -> Path:
    # テスト・移行時だけ明示した保存先を使えるようにする。通常起動は従来どおり
    # LOCALAPPDATA/PachiTool/data で、アプリ更新時も利用者データを保持する。
    configured_data_dir = os.environ.get("DATA_DIR", "").strip()
    data_dir = Path(configured_data_dir) if configured_data_dir else user_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    migrate_legacy_databases(data_dir)
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["HOST"] = "127.0.0.1"
    os.environ["RELOAD"] = "false"
    return data_dir


def reserve_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_until_ready(url: str, timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}api/machines", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception as exc:  # Server is still starting.
            last_error = exc
        time.sleep(0.2)
    raise RuntimeError(f"Local server did not start: {last_error}")


def show_error(message: str) -> None:
    ctypes.windll.user32.MessageBoxW(0, message, APP_NAME, 0x10)


def main() -> int:
    configure_environment()
    port = reserve_local_port()
    url = f"http://127.0.0.1:{port}/"

    try:
        import uvicorn
        import webview
        from api.main import app

        server_config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(server_config)
        server.install_signal_handlers = lambda: None
        server_thread = threading.Thread(target=server.run, name="pachi-tool-api", daemon=True)
        server_thread.start()
        wait_until_ready(url)

        if "--smoke-test" in sys.argv:
            from app_version import APP_VERSION

            with urllib.request.urlopen(f"{url}api/version", timeout=5) as response:
                version_payload = json.loads(response.read().decode("utf-8"))
                if response.status != 200 or version_payload.get("version") != APP_VERSION:
                    raise RuntimeError(
                        "Desktop build contains inconsistent version data: "
                        f"expected {APP_VERSION}, got {version_payload.get('version')}"
                    )
            with urllib.request.urlopen(f"{url}api/machines?scope=live_setting", timeout=5) as response:
                live_setting_machines = json.loads(response.read().decode("utf-8"))
                if response.status != 200 or not live_setting_machines:
                    raise RuntimeError("Verified live-setting machine data is missing")
            for machine_name in live_setting_machines:
                encoded_name = urllib.parse.quote(machine_name, safe="")
                with urllib.request.urlopen(f"{url}api/machines/{encoded_name}", timeout=5) as response:
                    machine_payload = json.loads(response.read().decode("utf-8"))
                    if response.status != 200 or not machine_payload.get("verified_for_live_setting"):
                        raise RuntimeError("Unverified machine leaked into the live-setting list")
            with urllib.request.urlopen(f"{url}api/opportunity/dashboard?month=2026-08", timeout=5) as response:
                if response.status != 200:
                    raise RuntimeError(f"Desktop API smoke test failed: HTTP {response.status}")
            with urllib.request.urlopen(
                f"{url}api/map/target_heat?visit_date=2026-08-10&days=120&long_days=365",
                # 長期履歴を数万行蓄積した端末でも、起動検証が
                # 通信失敗と誤判定しない待ち時間を確保する。
                timeout=30,
            ) as response:
                if response.status != 200:
                    raise RuntimeError(f"Target heat map smoke test failed: HTTP {response.status}")
            with urllib.request.urlopen(f"{url}mobile/", timeout=5) as response:
                mobile_html = response.read().decode("utf-8")
                required_markers = ('id="target-map-form"', 'id="setting-assess-form"')
                if response.status != 200 or not all(marker in mobile_html for marker in required_markers):
                    raise RuntimeError("Required mobile assets are missing from desktop build")
            server.should_exit = True
            server_thread.join(timeout=5)
            return 0

        window = webview.create_window(
            APP_NAME,
            url,
            width=1440,
            height=900,
            min_size=(900, 620),
            background_color="#070b18",
            text_select=True,
        )

        def maximize_window() -> None:
            window.maximize()

        try:
            webview.start(maximize_window, gui="edgechromium", private_mode=False, debug=False)
        finally:
            server.should_exit = True
            server_thread.join(timeout=5)
        return 0
    except Exception as exc:
        if "--smoke-test" in sys.argv:
            print(f"Desktop smoke test failed: {exc}", file=sys.stderr)
            return 1
        show_error(
            "PACHI TOOL could not start.\n\n"
            f"{exc}\n\n"
            "Please ask Codex to check the desktop app error."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
