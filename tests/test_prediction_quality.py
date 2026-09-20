from datetime import date, timedelta

import pytest

from hall.prediction_quality import audit_rows, coverage


def row(**kwargs):
    return {"hall_name": "キコーナ四條畷店", "report_date": "2026-09-01", "machine_name": "L北斗",
            "avg_diff_coins": 0, "avg_games": 4000, "unit_count": 10, "win_rate_pct": 50,
            "source_url": "https://example.com", **kwargs}


def test_missing_is_not_zero_and_input_unchanged():
    source = [row(), row(report_date="2026-09-02", avg_diff_coins=None)]
    clean, audit = audit_rows(source, cutoff=date(2026, 9, 3))
    assert len(clean) == 1 and clean[0]["avg_diff_coins"] == 0
    assert audit["exclusion_reasons"] == {"missing_diff": 1}
    assert source[1]["avg_diff_coins"] is None


def test_alias_duplicates_and_conflicts():
    clean, audit = audit_rows([row(), row(hall_name="キコーナ四条畷店")], cutoff=date(2026, 9, 3))
    assert len(clean) == 1 and audit["duplicate_rows"] == 1
    clean, audit = audit_rows([row(), row(avg_diff_coins=100)], cutoff=date(2026, 9, 3))
    assert not clean and audit["conflict_keys"] == 1 and audit["excluded_rows"] == 2


@pytest.mark.parametrize("changes", [
    {"avg_diff_coins": float("inf")}, {"avg_games": -1}, {"avg_games": float("nan")},
    {"unit_count": 0}, {"unit_count": None}, {"unit_count": 1.5}, {"win_rate_pct": 110},
    {"report_date": "2026-09-31"}, {"report_date": "2026-09-10"},
])
def test_invalid_rows_quarantined(changes):
    clean, audit = audit_rows([row(**changes)], cutoff=date(2026, 9, 3))
    assert not clean and audit["excluded_rows"] == 1


def test_seat_machine_change_is_conflict_not_average():
    seat = {"hall_name": "店", "report_date": "2026-09-01", "machine_name": "L北斗", "seat_number": 1, "diff_coins": 100, "games": 4000}
    clean, audit = audit_rows([seat, {**seat, "machine_name": "L東京喰種"}], cutoff=date(2026, 9, 3), scope="seat")
    assert not clean and audit["conflict_keys"] == 1


def test_sparse_days_and_unknown_games_not_eligible():
    rows = [row(report_date=(date(2026, 7, 1) + timedelta(days=i*3)).isoformat(), avg_games=None) for i in range(20)]
    result = coverage(rows, cutoff=date(2026, 9, 3))
    assert result["games_known_pct"] == 0
    assert result["calendar_coverage_pct"] < 40
    assert not result["analysis_eligible"]


def test_suspicious_zero_and_good_inputs():
    rows = [row(report_date=(date(2026, 8, 1) + timedelta(days=i)).isoformat(), avg_diff_coins=100) for i in range(31)]
    assert coverage(rows, cutoff=date(2026, 9, 1))["analysis_eligible"]
    assert not coverage([{**r, "avg_diff_coins": 0} for r in rows], cutoff=date(2026, 9, 1))["analysis_eligible"]
