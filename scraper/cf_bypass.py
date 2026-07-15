"""
Cloudflare bypass using real Chrome via Playwright.
cf_clearance クッキーを自動取得し、以降のリクエストに使用する。
"""
from __future__ import annotations
import time
import sqlite3
from pathlib import Path

try:
    from config import HALL_REPORTS_DB as DB_PATH
except ImportError:
    DB_PATH = Path(__file__).parent.parent / "data" / "hall_reports.db"


def get_cf_clearance(url: str = "https://ana-slo.com/", headless: bool = True) -> dict:
    """Playwright でリアルChromeを起動してCloudflareを突破し、クッキー群を返す。
    Returns: {"cf_clearance": "...", "cookie_str": "...", ...}
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome",
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-web-security",
            ],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
            ),
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            ignore_https_errors=True,
        )
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['ja', 'en-US']});
            window.chrome = {runtime: {}};
        """)

        page = ctx.new_page()
        print(f"  [Playwright] {url} を開いています...")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass

        # Cloudflareチャレンジが表示された場合は最大20秒待機
        for i in range(20):
            try:
                title = page.title()
                if "Just a moment" in title or "Checking" in title or "Attention Required" in title:
                    if i == 0:
                        print("  [Playwright] CF チャレンジ解決中...", end="", flush=True)
                    else:
                        print(".", end="", flush=True)
                    time.sleep(1)
                else:
                    if i > 0:
                        print()
                    break
            except Exception:
                time.sleep(1)

        time.sleep(2)  # Cookie が確実にセットされるまで少し待つ

        # ページからJSでcookieを取得（ctx.cookiesより確実）
        try:
            js_cookies = page.evaluate("document.cookie")
        except Exception:
            js_cookies = ""

        # contextのcookieも取得
        try:
            ctx_cookies = ctx.cookies()
        except Exception:
            ctx_cookies = []

        browser.close()

    # js_cookies: "name=value; name2=value2" 形式
    result = {}
    parts = []
    for part in js_cookies.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            result[k.strip()] = v.strip()
            parts.append(part)

    # ctx_cookies で補完（cf_clearance は HttpOnly なのでJSから取れない）
    for c in ctx_cookies:
        name = c["name"]
        if name not in result:
            result[name] = c["value"]
            parts.append(f"{name}={c['value']}")

    result["cookie_str"] = "; ".join(parts)
    cf = result.get("cf_clearance", "")
    print(f"  [Playwright] cf_clearance={'取得済み' if cf else 'なし'} ({len(ctx_cookies)}クッキー)")
    return result


def refresh_and_save_cookie(headless: bool = True) -> str:
    """cf_clearance を取得してDBとローカル変数に保存。cookie_str を返す。"""
    result = get_cf_clearance(headless=headless)
    cookie_str = result.get("cookie_str", "")
    if cookie_str:
        try:
            from scraper.anaslo import init_db
            conn = init_db()
            conn.execute(
                "INSERT OR REPLACE INTO scrape_settings (key, value, updated_at) "
                "VALUES ('cf_cookie_str', ?, datetime('now','localtime'))",
                (cookie_str,)
            )
            conn.commit()
            conn.close()
            print("  [CF] クッキーをDBに保存しました")
        except Exception as e:
            print(f"  [CF] DB保存エラー: {e}")
    return cookie_str
