"""Deterministic confidence calibration module."""
from __future__ import annotations

from .consistency import ConsistencyReport


def calibrate_confidence(
    report: ConsistencyReport,
    evidence_count: int,
    is_insufficient: bool,
) -> float:
    """Calculate calibrated confidence score within [0.0, 0.99]."""
    if is_insufficient:
        return 0.30

    score = 0.95

    if report.evidence_missing or evidence_count < 3:
        score -= 0.15

    if report.has_conflicts:
        score -= 0.10

    if report.policy_ambiguous:
        score -= 0.05

    if report.warnings:
        score -= 0.05

    # Ensure range bounds and never absolute 1.0
    clamped = max(0.20, min(score, 0.95))
    return round(clamped, 2)
