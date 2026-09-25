"""Deterministic confidence calibration module."""
from __future__ import annotations

from .consistency import ConsistencyReport


def calibrate_confidence(
    report: ConsistencyReport,
    evidence_count: int,
    is_insufficient: bool,
) -> float:
    """Calculate calibrated confidence score within [0.0, 0.99]."""
    if is_insufficient or evidence_count == 0:
        return min(0.30, 0.08 * evidence_count)

    score = 0.95

    if report.evidence_missing or evidence_count < 3:
        score -= 0.25

    if report.has_conflicts:
        score -= 0.20

    if report.policy_ambiguous:
        score -= 0.15

    if report.warnings:
        score -= 0.10

    # Ensure range bounds and never absolute 1.0
    clamped = max(0.0, min(score, 0.99))
    return round(clamped, 3)
