"""Deterministic policy evaluation engine."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .loader import load_policy_rules
from .refund import compute_financial_resolution, determine_resolution_actions


@dataclass
class PolicyDecision:
    primary_issue: str
    secondary_issues: list[str]
    case_status: str
    responsible_parties: list[dict[str, Any]]
    ranked_causes: list[dict[str, Any]]
    financial_resolution: dict[str, Any]
    resolution_actions: list[str]
    shipment_verdict: str
    payment_verdict: str
    data_conflicts: list[dict[str, Any]] = field(default_factory=list)


def _first(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return None


def _strings(value: Any) -> list[str]:
    if isinstance(value, str) and value:
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    return []


def _ids(data: Any, *names: str) -> list[str]:
    if not isinstance(data, dict):
        return []
    found: list[str] = []
    for name in names:
        found.extend(_strings(data.get(name)))
    return list(dict.fromkeys(found))[:20]


def _tool(tools: Iterable[str], *candidates: str) -> str | None:
    """Select only a tool advertised by MCP; never probe unadvertised names."""
    advertised = set(tools)
    for candidate in candidates:
        if candidate in advertised:
            return candidate
    return None


def _number(data: Any, *names: str) -> float | None:
    if not isinstance(data, dict):
        return None
    value = _first(data, *names)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return None


def _bool_val(data: Any, *names: str) -> bool | None:
    if not isinstance(data, dict):
        return None
    value = _first(data, *names)
    if isinstance(value, bool):
        return value
    return None


def evaluate_shipment_verdict(shipment: dict[str, Any] | None) -> str:
    """Classify shipment evidence into schema verdict."""
    if not shipment or not isinstance(shipment, dict):
        return "insufficient_evidence"

    status = str(_first(shipment, "status", "delivery_status", "shipment_status") or "").lower()
    is_late = _bool_val(shipment, "is_late", "late", "delivered_late")
    carrier_fault = _bool_val(shipment, "carrier_fault", "logistics_fault")
    seller_fault = _bool_val(shipment, "seller_fault", "seller_delay")

    if "lost" in status or "missing" in status:
        return "lost"
    if "return" in status or "returned" in status:
        return "returned"
    if "conflict" in status:
        return "conflicting"
    if (
        seller_fault
        or "seller_delay" in status
        or (is_late and not carrier_fault and seller_fault is not False)
    ):
        return "seller_delay"
    if carrier_fault or "carrier" in status or "logistics" in status or (is_late and carrier_fault):
        return "logistics_delay"
    if is_late is True:
        return "seller_delay"
    if "deliver" in status or "completed" in status or is_late is False:
        return "on_time"
    return "insufficient_evidence"


def evaluate_payment_verdict(
    payment: dict[str, Any] | None,
    refund_timeline: dict[str, Any] | None = None,
    payment_timeline: dict[str, Any] | None = None,
) -> str:
    """Classify payment evidence into schema verdict."""
    if not payment or not isinstance(payment, dict):
        return "insufficient_evidence"

    status = str(_first(payment, "status", "payment_status", "state") or "").lower()
    captured = _number(payment, "captured_total_brl", "captured_amount_brl", "paid_total_brl")
    refunded = _number(payment, "refunded_total_brl", "refund_total_brl")
    has_duplicate = _bool_val(payment, "duplicate_charge", "duplicate_capture")

    if not has_duplicate and isinstance(payment_timeline, dict):
        has_duplicate = _bool_val(payment_timeline, "duplicate_capture", "has_duplicate_event")

    if has_duplicate:
        return "duplicate_capture"
    if "mismatch" in status or "capture_mismatch" in status:
        return "capture_mismatch"

    if isinstance(refund_timeline, dict):
        rt_status = str(_first(refund_timeline, "status", "last_event_status") or "").lower()
        if "failed" in rt_status:
            return "refund_failed"
        if "pending" in rt_status:
            return "refund_pending"

    if "refund_failed" in status or "failed" in status:
        return "refund_failed"
    if "refund_pending" in status or "pending" in status:
        return "refund_pending"
    if refunded is not None and captured is not None and refunded >= captured:
        return "refunded"
    if captured is not None:
        return "reconciled"
    return "insufficient_evidence"


def detect_primary_issue(
    case: dict[str, Any],
    order_data: dict[str, Any] | None,
    shipment_verdict: str,
    payment_verdict: str,
    payment_data: dict[str, Any] | None,
) -> str:
    """Determine the primary issue according to deterministic business logic."""
    order_data = order_data or {}
    payment_data = payment_data or {}

    order_status = str(_first(order_data, "order_status", "status", "order_state") or "").lower()
    has_captured = _number(
        payment_data, "captured_total_brl", "paid_total_brl", "captured_amount_brl"
    )

    claims = [
        str(c.get("topic", "")).lower()
        for c in (case.get("customer_request", {}) or {}).get("claims", [])
        if isinstance(c, dict)
    ]

    # Rule 1: Payment issues
    if payment_verdict == "duplicate_capture":
        return "duplicate_charge"
    if payment_verdict == "capture_mismatch":
        return "payment_mismatch"
    if payment_verdict == "refund_failed":
        return "refund_failed"
    if payment_verdict == "refund_pending":
        return "refund_pending"

    # Rule 2: Canceled / Unavailable order but paid
    if "cancel" in order_status and has_captured is not None and has_captured > 0:
        return "canceled_order_paid"
    if "unavailable" in order_status and has_captured is not None and has_captured > 0:
        return "unavailable_order_paid"

    # Rule 3: Late delivery
    if shipment_verdict == "seller_delay":
        return "late_delivery_seller"
    if shipment_verdict == "logistics_delay":
        return "late_delivery_logistics"

    # Rule 4: Claims topic classification
    claim_str = " ".join(claims)
    if "seller_delay" in claim_str or "late_delivery_seller" in claim_str:
        return "late_delivery_seller"
    if "logistics_delay" in claim_str or "late_delivery_logistics" in claim_str or "late_delivery" in claim_str:
        return "late_delivery_logistics"
    if "valid_split_payment" in claim_str or "split" in claim_str:
        return "valid_split_payment"
    if "duplicate_charge" in claim_str or "duplicate" in claim_str:
        return "duplicate_charge"
    if "payment_mismatch" in claim_str or "capture_mismatch" in claim_str or "mismatch" in claim_str:
        return "payment_mismatch"
    if "canceled_order_paid" in claim_str or "cancel" in claim_str:
        return "canceled_order_paid"
    if "unavailable_order_paid" in claim_str or "unavailable" in claim_str:
        return "unavailable_order_paid"
    if "refund_failed" in claim_str:
        return "refund_failed"
    if "refund_pending" in claim_str:
        return "refund_pending"
    if "unsupported_claim" in claim_str or "unsupported" in claim_str:
        return "unsupported_claim"

    # Rule 5: Unsupported claim if order is on time and reconciled
    if shipment_verdict == "on_time" and payment_verdict == "reconciled":
        if claims:
            return "unsupported_claim"
        return "insufficient_evidence"

    if not order_data and not payment_data:
        # Check first claim topic if available
        for cl in claims:
            if cl in (
                "canceled_order_paid", "unavailable_order_paid", "late_delivery_seller",
                "late_delivery_logistics", "valid_split_payment", "payment_mismatch",
                "duplicate_charge", "refund_pending", "refund_failed", "unsupported_claim"
            ):
                return cl
        return "insufficient_evidence"

    return "insufficient_evidence"


def build_root_cause_analysis(
    primary_issue: str,
    shipment_data: dict[str, Any] | None,
    sellers_data: dict[str, Any] | None,
    has_errors: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Map primary_issue to ranked causes and responsible parties."""
    cause_party_map: dict[str, tuple[str, str]] = {
        "late_delivery_seller": ("SELLER_DELAY", "seller"),
        "late_delivery_logistics": ("LOGISTICS_DELAY", "logistics_provider"),
        "canceled_order_paid": ("CANCELED_ORDER_PAID", "platform"),
        "unavailable_order_paid": ("UNAVAILABLE_ORDER_PAID", "seller"),
        "payment_mismatch": ("PAYMENT_CAPTURE_MISMATCH", "payment_provider"),
        "duplicate_charge": ("DUPLICATE_CAPTURE", "payment_provider"),
        "refund_pending": ("REFUND_NOT_PROCESSED", "platform"),
        "refund_failed": ("REFUND_FAILED", "payment_provider"),
        "valid_split_payment": ("SPLIT_PAYMENT_VALID", "platform"),
        "unsupported_claim": ("UNSUPPORTED_CUSTOMER_CLAIM", "customer"),
        "insufficient_evidence": ("EVIDENCE_INCOMPLETE", "unknown"),
    }

    cause_code, party_type = cause_party_map.get(primary_issue, ("UNCLASSIFIED_ISSUE", "unknown"))

    seller_ids = _ids(sellers_data, "seller_ids", "seller_id") or _ids(
        shipment_data, "seller_ids", "seller_id"
    )
    party_id: str | None = None
    if party_type == "seller" and seller_ids:
        party_id = seller_ids[0]

    ranked_causes = [{"cause_code": cause_code, "rank": 1}]
    if primary_issue != "insufficient_evidence" and has_errors:
        ranked_causes.append({"cause_code": "EVIDENCE_INCOMPLETE", "rank": 2})

    responsible_parties = [{"party_type": party_type, "party_id": party_id}]
    return ranked_causes[:5], responsible_parties[:5]


def detect_conflicts(
    order_data: dict[str, Any] | None,
    payment_data: dict[str, Any] | None,
    shipment_data: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Detect cross-source inconsistencies."""
    conflicts: list[dict[str, Any]] = []
    order_data = order_data or {}
    payment_data = payment_data or {}
    shipment_data = shipment_data or {}

    captured = _number(payment_data, "captured_total_brl", "captured_amount_brl", "paid_total_brl")
    order_value = _number(order_data, "order_value_brl", "total_brl", "price_brl")
    if captured is not None and order_value is not None and abs(captured - order_value) > 0.01:
        conflicts.append(
            {
                "field": "payment.captured_total_brl vs order.order_value_brl",
                "sources": ["payment-agent", "order-item-agent"],
                "selected_source": "payment-agent",
                "resolution_code": "prefer_payment_source",
            }
        )

    ship_status = str(
        _first(shipment_data, "status", "delivery_status", "shipment_status") or ""
    ).lower()
    order_status = str(_first(order_data, "order_status", "status") or "").lower()
    if ship_status and order_status and "deliver" in order_status and "return" in ship_status:
        conflicts.append(
            {
                "field": "shipment.status vs order.order_status",
                "sources": ["shipment-agent", "order-item-agent"],
                "selected_source": "shipment-agent",
                "resolution_code": "prefer_shipment_source",
            }
        )
    return conflicts[:5]


def evaluate_policy(
    case: dict[str, Any],
    analysis: dict[str, Any],
    resolved_order_ids: list[str],
    has_errors: bool = False,
) -> PolicyDecision:
    """Evaluate full policy decision from evidence state."""
    order_data = analysis.get("customer") or {}
    shipment_data = analysis.get("shipment") or {}
    payment_data = analysis.get("payment") or {}
    policy_data = analysis.get("policy") or {}
    sellers_data = analysis.get("sellers") or {}
    payment_timeline = analysis.get("payment_timeline") or {}
    refund_timeline = analysis.get("refund_timeline") or {}

    ship_verdict = evaluate_shipment_verdict(shipment_data)
    pay_verdict = evaluate_payment_verdict(payment_data, refund_timeline, payment_timeline)
    primary_issue = detect_primary_issue(case, order_data, ship_verdict, pay_verdict, payment_data)

    rules = load_policy_rules(policy_data)
    order_id = (resolved_order_ids or [None])[0]

    captured = _number(payment_data, "captured_total_brl", "captured_amount_brl", "paid_total_brl")
    refunded = _number(payment_data, "refunded_total_brl", "refund_total_brl")

    financial_res = compute_financial_resolution(
        primary_issue=primary_issue,
        captured_amount_brl=captured,
        refunded_amount_brl=refunded,
        order_id=order_id,
        policy_rules=rules,
    )

    ranked_causes, responsible_parties = build_root_cause_analysis(
        primary_issue=primary_issue,
        shipment_data=shipment_data,
        sellers_data=sellers_data,
        has_errors=has_errors,
    )

    recommended_refund = financial_res["recommended_refund_brl"]

    if ship_verdict == "insufficient_evidence":
        if primary_issue == "late_delivery_seller":
            ship_verdict = "seller_delay"
        elif primary_issue == "late_delivery_logistics":
            ship_verdict = "logistics_delay"
        elif primary_issue in ("valid_split_payment", "unsupported_claim"):
            ship_verdict = "on_time"

    if pay_verdict == "insufficient_evidence":
        if primary_issue == "duplicate_charge":
            pay_verdict = "duplicate_capture"
        elif primary_issue == "payment_mismatch":
            pay_verdict = "capture_mismatch"
        elif primary_issue == "refund_failed":
            pay_verdict = "refund_failed"
        elif primary_issue == "refund_pending":
            pay_verdict = "refund_pending"
        elif primary_issue in ("valid_split_payment", "unsupported_claim"):
            pay_verdict = "reconciled"

    if primary_issue in ("valid_split_payment", "unsupported_claim"):
        case_status = "no_action"
    elif primary_issue == "insufficient_evidence":
        case_status = "needs_investigation"
    else:
        case_status = "action_required"

    resolution_actions = determine_resolution_actions(
        primary_issue=primary_issue,
        case_status=case_status,
        recommended_refund_brl=recommended_refund,
        responsible_parties=responsible_parties,
    )

    data_conflicts = detect_conflicts(order_data, payment_data, shipment_data)

    secondary_issues: list[str] = []
    if data_conflicts:
        secondary_issues.append("data_conflict_detected")
    if has_errors:
        secondary_issues.append("mcp_call_failed")

    return PolicyDecision(
        primary_issue=primary_issue,
        secondary_issues=list(dict.fromkeys(secondary_issues))[:10],
        case_status=case_status,
        responsible_parties=responsible_parties,
        ranked_causes=ranked_causes,
        financial_resolution=financial_res,
        resolution_actions=resolution_actions,
        shipment_verdict=ship_verdict,
        payment_verdict=pay_verdict,
        data_conflicts=data_conflicts,
    )
