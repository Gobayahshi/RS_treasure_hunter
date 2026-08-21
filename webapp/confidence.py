"""
GPS 위치 인증 부정행위 방지 규칙 세트 (R1~R9).
파일럿 단계에서는 소프트웨어 신호만으로 판단하며, 값은 운영 데이터가 쌓이면 튜닝한다.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

RULES_CONFIG = {
    "store_radius_meters": 30,
    "min_dwell_seconds": 5,
    "max_acceptable_accuracy_meters": 100,
    "max_walking_speed_kmh": 12,  # 세션 내 샘플 간 이동은 도보 수준을 벗어나면 의심
    "max_teleport_speed_kmh": 150,  # 직전 세션(다른 매장)과 비교해 물리적으로 불가능한 이동속도
    "auto_approve_score": 85,
    "pending_review_score": 60,
}

EARTH_RADIUS_METERS = 6371000


def haversine_distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(a))


def speed_kmh(distance_meters: float, elapsed_seconds: float) -> float:
    if elapsed_seconds <= 0:
        return 0.0
    hours = elapsed_seconds / 3600
    km = distance_meters / 1000
    return km / hours


@dataclass
class LocationSampleInput:
    lat: float
    lng: float
    accuracy: float
    is_mock: bool
    captured_at: datetime


@dataclass
class PreviousSessionContext:
    store_id: str
    lat: float
    lng: float
    ended_at: datetime


@dataclass
class EvaluationResult:
    score: float
    status: str  # auto_approved | pending_review | rejected
    reasons: list = field(default_factory=list)
    points_eligible: bool = False


def evaluate_visit_session(
    store_lat: float,
    store_lng: float,
    samples: list[LocationSampleInput],
    started_at: datetime,
    ended_at: datetime,
    previous_session: Optional[PreviousSessionContext],
    already_claimed_today: bool,
    device_mismatch: bool,
) -> EvaluationResult:
    reasons: list[str] = []
    score = 100.0

    if not samples:
        return EvaluationResult(score=0, status="rejected", reasons=["NO_SAMPLES"])

    # R1: Mock Location 탐지 -> 즉시 반려
    if any(s.is_mock for s in samples):
        reasons.append("R1_MOCK_LOCATION_DETECTED")
        return EvaluationResult(score=0, status="rejected", reasons=reasons)

    # R2: 반경 체크 -> 반경 내 샘플이 하나도 없으면 즉시 반려
    distances = [haversine_distance_meters(s.lat, s.lng, store_lat, store_lng) for s in samples]
    within_radius_count = sum(1 for d in distances if d <= RULES_CONFIG["store_radius_meters"])
    if within_radius_count == 0:
        reasons.append("R2_OUT_OF_RADIUS")
        return EvaluationResult(score=0, status="rejected", reasons=reasons)
    within_radius_ratio = within_radius_count / len(samples)
    if within_radius_ratio < 0.8:
        score -= 25
        reasons.append("R2_PARTIAL_RADIUS_COVERAGE")

    # R3: GPS 정확도 -> 평균 오차가 너무 크면 감점(재시도 유도용 신호)
    avg_accuracy = sum(s.accuracy for s in samples) / len(samples)
    if avg_accuracy > RULES_CONFIG["max_acceptable_accuracy_meters"]:
        score -= 20
        reasons.append("R3_LOW_GPS_ACCURACY")

    # R4: 체류시간 -> 최소 체류시간 미달 시 반려
    dwell_seconds = (ended_at - started_at).total_seconds()
    if dwell_seconds < RULES_CONFIG["min_dwell_seconds"]:
        reasons.append("R4_INSUFFICIENT_DWELL_TIME")
        return EvaluationResult(score=0, status="rejected", reasons=reasons)

    # R5: 세션 내 이동 일관성 -> 도보 속도를 초과하는 급격한 이동(좌표 튐) 감점
    sorted_samples = sorted(samples, key=lambda s: s.captured_at)
    has_suspicious_jump = False
    for prev, curr in zip(sorted_samples, sorted_samples[1:]):
        distance = haversine_distance_meters(prev.lat, prev.lng, curr.lat, curr.lng)
        elapsed = (curr.captured_at - prev.captured_at).total_seconds()
        if speed_kmh(distance, elapsed) > RULES_CONFIG["max_walking_speed_kmh"]:
            has_suspicious_jump = True
            break
    if has_suspicious_jump:
        score -= 20
        reasons.append("R5_MOVEMENT_INCONSISTENCY")

    # R6: 텔레포트 탐지 -> 직전 세션(다른 매장)과 비교해 물리적으로 불가능한 이동속도면 즉시 반려
    if previous_session is not None:
        distance = haversine_distance_meters(
            previous_session.lat, previous_session.lng, store_lat, store_lng
        )
        elapsed = (started_at - previous_session.ended_at).total_seconds()
        if speed_kmh(distance, elapsed) > RULES_CONFIG["max_teleport_speed_kmh"]:
            reasons.append("R6_TELEPORT_DETECTED")
            return EvaluationResult(score=0, status="rejected", reasons=reasons)

    # R7: 동일 매장 중복 인증 -> 인증 자체는 유지하되 포인트는 지급하지 않음
    points_eligible = True
    if already_claimed_today:
        points_eligible = False
        reasons.append("R7_ALREADY_CLAIMED_TODAY")

    # R8: 디바이스-계정 바인딩 불일치 -> 감점 및 검토 유도
    if device_mismatch:
        score -= 30
        reasons.append("R8_DEVICE_MISMATCH")

    # R9: 근무 시간 외 이상 패턴 (06:00~22:00 외) -> 감점
    if started_at.hour < 6 or started_at.hour >= 22:
        score -= 15
        reasons.append("R9_OFF_HOURS_ACTIVITY")

    score = max(0.0, min(100.0, score))

    if score >= RULES_CONFIG["auto_approve_score"]:
        status = "auto_approved"
    elif score >= RULES_CONFIG["pending_review_score"]:
        status = "pending_review"
        points_eligible = False
    else:
        status = "rejected"
        points_eligible = False

    return EvaluationResult(score=score, status=status, reasons=reasons, points_eligible=points_eligible)


POINTS_BY_TIER = {"normal": 10, "rare": 30}


def points_for_tier(tier: str) -> int:
    return POINTS_BY_TIER.get(tier, POINTS_BY_TIER["normal"])
