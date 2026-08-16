"""
四條畷周辺 全店舗 アナスロ一括取得スクリプト
"""
import time
from scraper.anaslo import scrape_hall

HALLS = [
    # 四條畷駅 徒歩圏
    "キコーナ四條畷店",
    "ひま・わり四條畷店",
    # 野崎・住道方面
    "キコーナ野崎店",
    "ニコニコ住道店",
    "キコーナ大東店",
    # 大東市内の大型店
    "マルハン大東店",
    "スーパーコスモプレミアム大東店",
    "ベガスベガス大東店",
]

DAYS = 30

if __name__ == "__main__":
    total = len(HALLS)
    for i, hall in enumerate(HALLS, 1):
        print(f"\n{'='*60}")
        print(f"[{i}/{total}] {hall}")
        print(f"{'='*60}")
        try:
            scrape_hall(hall, prefecture="大阪府", max_days=DAYS)
        except Exception as e:
            print(f"⚠ エラー: {e}")
        if i < total:
            print(f"\n--- 次の店舗まで30秒待機 ---")
            time.sleep(30)

    print(f"\n{'='*60}")
    print("全店舗完了")
