"""GPS 부정행위 방지 규칙(R1~R9) 테스트."""

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from confidence import (  # noqa: E402
    LocationSampleInput,
    PreviousSessionContext,
    evaluate_visit_session,
    to_kst,
)

STORE_LAT, STORE_LNG = 37.5, 127.0


def samples_at(start, count=3, lat=STORE_LAT, lng=STORE_LNG, accuracy=10.0, is_mock=False):
    return [
        LocationSampleInput(lat, lng, accuracy, is_mock, start + timedelta(seconds=i))
        for i in range(count)
    ]


def evaluate(start, samples=None, dwell=10, previous=None, claimed=False, mismatch=False):
    start = start or datetime(2026, 9, 16, 3, 0, 0)  # UTC 03시 = KST 12시
    return evaluate_visit_session(
        store_lat=STORE_LAT,
        store_lng=STORE_LNG,
        samples=samples if samples is not None else samples_at(start),
        started_at=start,
        ended_at=start + timedelta(seconds=dwell),
        previous_session=previous,
        already_claimed_today=claimed,
        device_mismatch=mismatch,
    )


def test_kst_conversion():
    assert to_kst(datetime(2026, 9, 16, 0, 0)).hour == 9
    assert to_kst(datetime(2026, 9, 16, 15, 0)).day == 17


def test_normal_visit_is_auto_approved():
    result = evaluate(datetime(2026, 9, 16, 3, 0))  # KST 12시
    assert result.status == "auto_approved"
    assert result.score == 100
    assert result.points_eligible is True


def test_daytime_in_kst_is_not_off_hours():
    """UTC 기준으로 보면 새벽이지만 한국 시간으로는 업무시간인 경우."""
    for utc_hour in (0, 3, 5, 22, 23):  # KST 09, 12, 14, 07, 08시
        result = evaluate(datetime(2026, 9, 16, utc_hour, 0))
        assert "R9_OFF_HOURS_ACTIVITY" not in result.reasons, utc_hour


def test_off_hours_in_kst_is_penalized():
    for utc_hour in (13, 16, 20):  # KST 22, 01, 05시
        result = evaluate(datetime(2026, 9, 16, utc_hour, 0))
        assert "R9_OFF_HOURS_ACTIVITY" in result.reasons, utc_hour
        assert result.score == 85


def test_mock_location_is_rejected():
    start = datetime(2026, 9, 16, 3, 0)
    result = evaluate(start, samples=samples_at(start, is_mock=True))
    assert result.status == "rejected"
    assert result.reasons == ["R1_MOCK_LOCATION_DETECTED"]


def test_out_of_radius_is_rejected():
    start = datetime(2026, 9, 16, 3, 0)
    result = evaluate(start, samples=samples_at(start, lat=37.6))  # 약 11km 밖
    assert result.status == "rejected"
    assert "R2_OUT_OF_RADIUS" in result.reasons


def test_short_dwell_is_rejected():
    result = evaluate(datetime(2026, 9, 16, 3, 0), dwell=2)
    assert result.status == "rejected"
    assert "R4_INSUFFICIENT_DWELL_TIME" in result.reasons


def test_low_accuracy_becomes_pending_review():
    start = datetime(2026, 9, 16, 3, 0)
    result = evaluate(start, samples=samples_at(start, accuracy=250))
    assert result.status == "pending_review"
    assert result.points_eligible is False


def test_teleport_is_rejected():
    start = datetime(2026, 9, 16, 3, 0)
    previous = PreviousSessionContext(
        store_id="other", lat=35.1, lng=129.0, ended_at=start - timedelta(minutes=5)  # 부산
    )
    result = evaluate(start, previous=previous)
    assert result.status == "rejected"
    assert "R6_TELEPORT_DETECTED" in result.reasons


def test_duplicate_same_day_keeps_visit_but_no_points():
    result = evaluate(datetime(2026, 9, 16, 3, 0), claimed=True)
    assert result.status == "auto_approved"
    assert result.points_eligible is False
    assert "R7_ALREADY_CLAIMED_TODAY" in result.reasons


def test_device_mismatch_needs_review():
    result = evaluate(datetime(2026, 9, 16, 3, 0), mismatch=True)
    assert result.status == "pending_review"
    assert "R8_DEVICE_MISMATCH" in result.reasons


def test_no_samples_is_rejected():
    result = evaluate(datetime(2026, 9, 16, 3, 0), samples=[])
    assert result.status == "rejected"
