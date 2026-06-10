"""Tests for the notebook KQL query builders (``azure/notebooks/_lib``).

The module imports its heavy notebook deps (pandas, azure-monitor-query,
ipywidgets) lazily, so the pure query-string helpers are testable with just
the dev extras installed.
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "notebooks" / "_lib"))

import engagement_query as eq


def test_per_meet_query_is_case_insensitive_on_meet_id():
    q = eq.per_meet_query("DemoMeet2026", "2026-03-28")
    # =~ instead of ==: meet IDs preserve the case the Pi registered, so a
    # hand-typed exact comparison silently returns 0 rows on case mismatch.
    assert "meet_id =~ 'DemoMeet2026'" in q
    assert "pi_local_date == '2026-03-28'" in q


def test_timespan_for_meet_day_covers_the_date_with_margin():
    start, end = eq.timespan_for_meet_day("2026-03-28")
    day = datetime(2026, 3, 28, tzinfo=UTC)
    assert start == day - timedelta(days=1)
    assert end == day + timedelta(days=2)
    assert start.tzinfo is not None and end.tzinfo is not None


def test_base_project_extends_message_board_fields():
    assert "mb_active" in eq.BASE_PROJECT
    assert "mb_page_index" in eq.BASE_PROJECT


def test_cross_meet_query_counts_message_board_views():
    q = eq.cross_meet_query()
    assert "message_board_views = countif(ev == 'viewer_message_board_view'" in q


def test_list_meets_query_groups_by_meet_and_date():
    q = eq.list_meets_query()
    assert "by meet_id, pi_local_date" in q
    assert "isnotempty(meet_id)" in q
