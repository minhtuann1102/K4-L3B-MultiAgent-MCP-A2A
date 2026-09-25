"""Deterministic refund and resolution action calculation."""
from __future__ import annotations

from typing import Any


def compute_financial_resolution(
    primary_issue: str,
    captured_amount_brl: float | None,
    refunded_amount_brl: float | None,
    order_id: str | None,
    policy_rules: dict[str, Any],
) -> dict[str, Any]:
    """Compute exact financial resolution adhering to schema invariants."""
    captured = captured_amount_brl if captured_amount_brl is not None else 0.0
    refunded = refunded_amount_brl if refunded_amount_brl is not None else 0.0
    refundable = max(captured - refunded, 0.0)

    if not policy_rules.get("refund_eligible", True):
        return {"currency": "BRL", "recommended_refund_brl": 0.0, "refund_lines": []}

    recommended = 0.0
    refund_lines: list[dict[str, Any]] = []

    if primary_issue in ("canceled_order_paid", "unavailable_order_paid"):
        if refundable > 0:
            recommended = round(refundable, 2)
            refund_lines.append(
                {"reason_code": primary_issue, "amount_brl": recommended, "entity_id": order_id}
            )
    elif primary_issue == "duplicate_charge":
        if refundable > 0:
            recommended = round(refundable, 2)
            refund_lines.append(
                {
                    "reason_code": "duplicate_capture",
                    "amount_brl": recommended,
                    "entity_id": order_id,
                }
            )
    elif primary_issue in ("refund_pending", "refund_failed"):
        if refundable > 0:
            recommended = round(refundable, 2)
            refund_lines.append(
                {"reason_code": primary_issue, "amount_brl": recommended, "entity_id": order_id}
            )
    elif primary_issue in ("late_delivery_seller", "late_delivery_logistics"):
        comp_rate = float(policy_rules.get("late_delivery_comp_rate", 0.0))
        if comp_rate > 0 and captured > 0:
            recommended = round(captured * comp_rate, 2)
            refund_lines.append(
                {
                    "reason_code": "late_delivery_compensation",
                    "amount_brl": recommended,
                    "entity_id": order_id,
                }
            )

    # Invariant check: sum(refund_lines) == recommended_refund_brl
    calculated_sum = round(sum(line["amount_brl"] for line in refund_lines), 2)
    if calculated_sum != recommended:
        recommended = calculated_sum

    return {
        "currency": "BRL",
        "recommended_refund_brl": recommended,
        "refund_lines": refund_lines[:10],
    }


def determine_resolution_actions(
    primary_issue: str,
    case_status: str,
    recommended_refund_brl: float,
    responsible_parties: list[dict[str, Any]],
) -> list[str]:
    """Generate resolution actions conforming to schema limits."""
    if case_status == "needs_investigation":
        return ["Route case for manual investigation"]

    actions: list[str] = []
    if recommended_refund_brl > 0:
        actions.append(f"Issue refund of {recommended_refund_brl:.2f} BRL")

    party_types = {p.get("party_type") for p in responsible_parties if isinstance(p, dict)}

    action_map = {
        "late_delivery_seller": [
            "Notify seller of SLA breach",
            "Apply seller penalty per policy",
        ],
        "late_delivery_logistics": [
            "File carrier delay report",
            "Notify customer of logistics delay",
        ],
        "canceled_order_paid": ["Cancel order and process full refund"],
        "unavailable_order_paid": ["Process refund for unavailable item"],
        "duplicate_charge": ["Reverse duplicate payment capture"],
        "refund_pending": ["Trigger pending refund processing"],
        "refund_failed": ["Retry failed refund via alternate channel"],
        "payment_mismatch": ["Audit payment capture records"],
        "valid_split_payment": ["Confirm split payment legitimacy and close case"],
        "unsupported_claim": ["Notify customer of claim rejection with policy terms"],
        "insufficient_evidence": ["Request additional documentation from customer"],
    }
    actions.extend(action_map.get(primary_issue, []))

    if "seller" in party_types and "Notify seller of SLA breach" not in actions:
        actions.append("Notify seller")

    if not actions:
        actions.append("No action required")

    return list(dict.fromkeys(actions))[:8]
