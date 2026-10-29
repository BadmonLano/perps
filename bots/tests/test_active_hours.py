import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import active_hours as ah  # noqa: E402

NY = ZoneInfo("America/New_York")


def at(h, m, day=29):   # 2026-10-29 is a Thursday
    return datetime(2026, 10, day, h, m, tzinfo=NY)


def test_default_is_always_on():
    for spec in ("", "00:00-23:59", "always", "24/7"):
        assert ah.in_active_window(at(23, 59), {"ACTIVE_HOURS": spec})[0]
        assert ah.in_active_window(at(0, 0), {"ACTIVE_HOURS": spec})[0]


def test_window_and_overnight_and_multi():
    e = {"ACTIVE_HOURS": "09:00-17:00", "ACTIVE_TZ": "America/New_York"}
    assert ah.in_active_window(at(9, 0), e)[0] and not ah.in_active_window(at(17, 0), e)[0]
    e["ACTIVE_HOURS"] = "22:00-02:00"
    assert ah.in_active_window(at(1, 30), e)[0] and not ah.in_active_window(at(12, 0), e)[0]
    e["ACTIVE_HOURS"] = "09:00-12:00,18:00-23:59"
    assert ah.in_active_window(at(23, 59), e)[0] and not ah.in_active_window(at(13, 0), e)[0]


def test_days_and_bad_input_fails_open():
    e = {"ACTIVE_HOURS": "00:00-23:59", "ACTIVE_DAYS": "mon-wed"}
    assert not ah.in_active_window(at(12, 0), e)[0]           # Thursday
    assert ah.in_active_window(at(12, 0, day=27), e)[0]       # Tuesday
    ok, why = ah.in_active_window(at(12, 0), {"ACTIVE_HOURS": "nonsense"})
    assert ok and "unreadable" in why


def test_lane_cadences_tunable():
    c = ah.lane_cadences({"LANE_CADENCE_MISC_LANE_MIN": "7"})
    assert c["misc_lane"] == 7 and c["warden_lane"] == 20
