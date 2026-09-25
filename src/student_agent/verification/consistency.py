"""Consistency verification checks for policy decisions and evidence."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..policy.engine import PolicyDecision

EVIDENCE_REF_PATTERN = re.compile(r"^ev_[A-Za-z0-9_-]{20,96}$")


@dataclass
class ConsistencyReport:
    is_valid: bool
    warnings: list[str] = field(default_factory=list)
    has_conflicts: bool = False
    evidence_missing: bool = False
    policy_ambiguous: bool = False


def verify_consistency(
    decision: PolicyDecision,
    analysis: dict[str, Any],
    evidence_refs: list[str],
    case: dict[str, Any],
) -> ConsistencyReport:
    """Run multi-point consistency checks across decision, evidence, and financial bounds."""
    warnings: list[str] = []
    has_conflicts = len(decision.data_conflicts) > 0
    evidence_missing = False
    policy_ambiguous = False

    order_data = analysis.get("customer") or {}
    payment_data = analysis.get("payment") or {}

    # Check 1: Issue <-> Responsible party
    party_types = {
        p.get("party_type") for p in decision.responsible_parties if isinstance(p, dict)
    }
    if decision.primary_issue == "late_delivery_seller" and "seller" not in party_types:
        warnings.append("late_delivery_seller missing seller party")
    if (
        decision.primary_issue == "late_delivery_logistics"
        and "logistics_provider" not in party_types
    ):
        warnings.append("late_delivery_logistics missing logistics_provider party")
    payment_issues = ("payment_mismatch", "duplicate_charge", "refund_failed")
    if decision.primary_issue in payment_issues and "payment_provider" not in party_types:
        warnings.append(f"{decision.primary_issue} missing payment_provider party")

    # Check 2: Issue <-> Evidence support
    if decision.primary_issue == "canceled_order_paid":
        order_status = str(
            order_data.get("order_status") or order_data.get("status") or ""
        ).lower()
        if "cancel" not in order_status:
            warnings.append("canceled_order_paid not supported by order status")
            policy_ambiguous = True

    if (
        decision.primary_issue == "late_delivery_seller"
        and decision.shipment_verdict not in ("seller_delay", "conflicting")
    ):
        warnings.append("late_delivery_seller not supported by shipment verdict")
        policy_ambiguous = True

    if (
        decision.primary_issue == "late_delivery_logistics"
        and decision.shipment_verdict not in ("logistics_delay", "conflicting")
    ):
        warnings.append("late_delivery_logistics not supported by shipment verdict")
        policy_ambiguous = True

    if decision.primary_issue == "insufficient_evidence":
        evidence_missing = True

    # Check 3: Financial consistency
    refund_lines = decision.financial_resolution.get("refund_lines", [])
    rec_refund = decision.financial_resolution.get("recommended_refund_brl", 0.0)
    lines_total = round(sum(line.get("amount_brl", 0.0) for line in refund_lines), 2)
    if abs(lines_total - rec_refund) > 0.01:
        warnings.append(
            f"financial mismatch: refund_lines sum ({lines_total}) != recommended ({rec_refund})"
        )

    captured = payment_data.get("captured_total_brl") or payment_data.get("paid_total_brl")
    if (
        captured is not None
        and isinstance(captured, (int, float))
        and rec_refund > captured
        and decision.primary_issue not in ("late_delivery_seller", "late_delivery_logistics")
    ):
        warnings.append(f"refund ({rec_refund}) exceeds captured amount ({captured})")

    # Check 4: Evidence refs validity
    if not evidence_refs:
        evidence_missing = True
    else:
        for ref in evidence_refs:
            if not EVIDENCE_REF_PATTERN.fullmatch(ref):
                warnings.append(f"invalid evidence ref format: {ref}")

    return ConsistencyReport(
        is_valid=len(warnings) == 0,
        warnings=warnings,
        has_conflicts=has_conflicts,
        evidence_missing=evidence_missing,
        policy_ambiguous=policy_ambiguous,
    )
