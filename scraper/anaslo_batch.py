"""
四條畷周辺 全店舗 アナスロ一括取得スクリプト
"""
import time
from scraper.anaslo import scrape_hall

HALLS = [
    # 大東市
    "ベガスベガス大東店",
    "マルハン大東店",
    "ニコニコ住道店",
    "スーパーコスモプレミアム大東店",
    # 枚方市
    "マルハン枚方店",
    "ニコニコ枚方店",
    "ベガビック1700枚方店",
    "G-ONE枚方宮之阪店",
    # 寝屋川市
    "キコーナ寝屋川南店",
    "ニコニコ寝屋川南インター店",
    "マルハン寝屋川店",
    "ベラジオ寝屋川店",
    "ニコニコ寝屋川店スロット館",
    # 交野市
    "123交野店",
    # 守口・門真
    "キコーナ守口店",
    "テキサス門真",
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
