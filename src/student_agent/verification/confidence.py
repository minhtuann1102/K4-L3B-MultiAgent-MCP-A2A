"""Deterministic confidence calibration module."""
from __future__ import annotations

from .consistency import ConsistencyReport


def calibrate_confidence(
    report: ConsistencyReport,
    evidence_count: int,
    is_insufficient: bool,
    primary_issue: str = "",
) -> float:
    """Calculate calibrated confidence score within [0.0, 0.99]."""
    if is_insufficient:
        return 0.30

    if primary_issue == "unsupported_claim":
        base = 0.85
    elif primary_issue == "valid_split_payment":
        base = 0.90
    elif primary_issue in ("canceled_order_paid", "unavailable_order_paid"):
        base = 0.92
    elif primary_issue in ("late_delivery_seller", "late_delivery_logistics"):
        base = 0.90
    elif primary_issue in (
        "duplicate_charge",
        "payment_mismatch",
        "refund_pending",
        "refund_failed",
    ):
        base = 0.88
    else:
        base = 0.85

    score = base

    if report.evidence_missing or evidence_count < 2:
        score -= 0.10

    if report.has_conflicts:
        score -= 0.05

    if report.policy_ambiguous:
        score -= 0.05

    # Ensure range bounds and never absolute 1.0
    clamped = max(0.20, min(score, 0.95))
    return round(clamped, 2)
