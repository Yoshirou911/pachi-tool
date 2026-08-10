from bs4 import BeautifulSoup

from scraper.opportunity_crawler import _compare, _extract_nana_tables


def test_nana_expected_value_table_is_structured_without_guessing_other_tables():
    soup = BeautifulSoup("""
      <table><tr><th>打ち出しG数</th><th>等価交換</th><th>5.6枚交換</th></tr>
      <tr><td>500</td><td>1,233円</td><td>320円</td></tr>
      <tr><td>550</td><td>1,617円</td><td>704円</td></tr></table>
      <table><tr><th>設定</th><th>AT確率</th></tr><tr><td>1</td><td>1/300</td></tr></table>
    """, "lxml")
    assert _extract_nana_tables(soup) == [
        {"value": 500, "equivalent_ev_yen": 1233, "exchange_56_ev_yen": 320},
        {"value": 550, "equivalent_ev_yen": 1617, "exchange_56_ev_yen": 704},
    ]


def test_crawler_detects_value_conflict_for_manual_review():
    profile = {"machine_name": "テスト", "condition_label": "通常", "exchange_type": "equivalent",
               "start_threshold": 500, "expected_value_yen": 1000}
    extracted, conflicts = _compare(profile, {"curve_points": [
        {"value": 500, "equivalent_ev_yen": 1233, "exchange_56_ev_yen": 320}
    ], "source_updated_on": "2026-08-10"})
    assert extracted["expected_value_yen"] == 1233
    assert conflicts and "変更" in conflicts[0]


def test_crawler_keeps_normal_and_reset_tables_separate():
    tables = [
        {"context": "天井短縮なし時の天井期待値", "points": [{"value": 500, "equivalent_ev_yen": 1368, "exchange_56_ev_yen": 230}]},
        {"context": "天井短縮あり時の天井期待値", "points": [{"value": 500, "equivalent_ev_yen": 6417, "exchange_56_ev_yen": 5286}]},
    ]
    normal, normal_conflicts = _compare(
        {"machine_name": "ToLOVEる", "condition_label": "通常", "exchange_type": "equivalent",
         "reset_status": "normal", "start_threshold": 500, "expected_value_yen": 1368},
        {"curve_tables": tables},
    )
    reset, reset_conflicts = _compare(
        {"machine_name": "ToLOVEる", "condition_label": "リセット", "exchange_type": "equivalent",
         "reset_status": "reset_confirmed", "start_threshold": 500, "expected_value_yen": 6417},
        {"curve_tables": tables},
    )
    assert normal["expected_value_yen"] == 1368 and not normal_conflicts
    assert reset["expected_value_yen"] == 6417 and not reset_conflicts
