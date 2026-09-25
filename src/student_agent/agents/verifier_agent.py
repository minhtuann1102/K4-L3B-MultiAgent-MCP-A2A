"""Verifier Agent for output invariant verification and final assembly."""
from __future__ import annotations

from typing import Any

from ..policy.engine import PolicyDecision, _bool_val, _first, _ids, _number
from ..state import ComplaintState
from ..verification.confidence import calibrate_confidence
from ..verification.consistency import ConsistencyReport, verify_consistency


def build_claim_assessments(
    case: dict[str, Any],
    shipment_verdict: str,
    payment_verdict: str,
    refs: list[str],
) -> list[dict[str, Any]]:
    """Link each input claim to relevant evidence refs with a verdict."""
    claims: list[dict[str, Any]] = (
        case.get("customer_request", {}) or {}
    ).get("claims", [])
    if not isinstance(claims, list):
        return []

    assessments: list[dict[str, Any]] = []
    for claim in claims[:5]:
        if not isinstance(claim, dict):
            continue
        claim_id = str(claim.get("claim_id", "")).strip()
        topic = str(claim.get("topic", "")).strip().lower()
        if not claim_id:
            continue
        relevant = refs[:3]
        if "late_delivery" in topic:
            if shipment_verdict in ("seller_delay", "logistics_delay"):
                verdict = "supported"
                conf = 0.85
            elif shipment_verdict == "on_time":
                verdict = "unsupported"
                conf = 0.85
            else:
                verdict = "insufficient_evidence"
                conf = 0.3
        elif "refund" in topic or "payment" in topic or "split" in topic:
            if payment_verdict in ("refund_pending", "refund_failed", "duplicate_capture"):
                verdict = "supported"
                conf = 0.80
            elif payment_verdict == "reconciled":
                verdict = "unsupported"
                conf = 0.75
            else:
                verdict = "insufficient_evidence"
                conf = 0.3
        else:
            verdict = "partially_supported" if refs else "insufficient_evidence"
            conf = 0.5 if refs else 0.2

        assessments.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": round(conf, 2),
                "evidence_refs": relevant,
            }
        )
    return assessments


class VerifierAgent:
    """Verifies decision consistency and builds schema-compliant final output."""

    def verify_and_build(
        self,
        state: ComplaintState,
        decision: PolicyDecision,
    ) -> tuple[dict[str, Any], ConsistencyReport]:
        case_id = state["case_id"]
        case = state["case"]
        analysis = state["analysis"]

        # Collect unique server-issued evidence refs
        refs = list(dict.fromkeys(item["evidence_ref"] for item in state["evidence"]))

        # Verify consistency
        report = verify_consistency(decision, analysis, refs, case)

        # Calibrate confidence
        is_insufficient = decision.primary_issue == "insufficient_evidence"
        confidence = calibrate_confidence(report, len(refs), is_insufficient)

        shipment = analysis.get("shipment", {}) or {}
        payment = analysis.get("payment", {}) or {}
        customer = analysis.get("customer", {}) or {}
        items = analysis.get("items", {}) or {}
        sellers = analysis.get("sellers", {}) or {}
        customer_history = analysis.get("customer_history", {}) or {}
        payment_timeline = analysis.get("payment_timeline", {}) or {}
        product_context = analysis.get("product_context", {}) or {}

        # Entity lists
        seller_ids = _ids(sellers, "seller_ids", "seller_id") or _ids(
            shipment, "seller_ids", "seller_id"
        )
        shipment_ids = _ids(shipment, "shipment_ids", "shipment_id")
        payment_refs = (
            _ids(payment, "payment_references", "payment_id", "payment_ids")
            or _ids(payment_timeline, "payment_references", "payment_id")
        )
        item_ids = (
            _ids(items, "item_ids", "order_item_ids", "items")
            or _ids(product_context, "item_ids", "product_ids", "items")
            or _ids(customer, "item_ids", "order_item_ids")
        )

        late_seller_ids: list[str] = (
            seller_ids[:20] if decision.shipment_verdict == "seller_delay" else []
        )
        timeline_complete = (
            _bool_val(shipment, "timeline_complete", "timeline_full") is True
            or bool(_first(shipment, "delivered_at", "actual_delivery_date"))
        )

        captured = _number(
            payment, "captured_total_brl", "captured_amount_brl", "paid_total_brl"
        )
        refunded = _number(payment, "refunded_total_brl", "refund_total_brl")
        refundable = (
            max((captured or 0.0) - (refunded or 0.0), 0.0)
            if captured is not None
            else None
        )

        claim_assessments = build_claim_assessments(
            case=case,
            shipment_verdict=decision.shipment_verdict,
            payment_verdict=decision.payment_verdict,
            refs=refs,
        )

        output: dict[str, Any] = {
            "schema_version": "day09-l3b-output-v2",
            "case_id": case_id,
            "assessment": {
                "primary_issue": decision.primary_issue,
                "secondary_issues": decision.secondary_issues,
                "case_status": decision.case_status,
                "confidence": confidence,
            },
            "affected_entities": {
                "order_ids": state["resolved_order_ids"],
                "item_ids": item_ids,
                "seller_ids": seller_ids,
                "payment_references": payment_refs,
                "shipment_ids": shipment_ids,
            },
            "entity_resolution": {
                "status": "resolved" if state["resolved_order_ids"] else "not_found",
                "resolved_order_ids": state["resolved_order_ids"],
                "rejected_candidates": state["rejected_candidates"],
                "confidence": state["entity_confidence"],
            },
            "customer_context": {
                "customer_unique_id": (
                    _first(customer, "customer_unique_id", "customer_id")
                    or _first(customer_history, "customer_unique_id", "customer_id")
                ),
                "related_order_ids": (
                    _ids(customer_history, "related_order_ids", "order_ids", "previous_order_ids")
                    or _ids(customer, "related_order_ids", "order_ids")
                ),
            },
            "shipment_analysis": {
                "verdict": decision.shipment_verdict,
                "late_seller_ids": late_seller_ids,
                "timeline_complete": timeline_complete,
            },
            "payment_analysis": {
                "verdict": decision.payment_verdict,
                "captured_total_brl": captured,
                "refunded_total_brl": refunded,
                "refundable_total_brl": refundable,
            },
            "root_cause_analysis": {
                "ranked_causes": decision.ranked_causes,
                "responsible_parties": decision.responsible_parties,
            },
            "evidence_refs": refs,
            "data_conflicts": decision.data_conflicts,
            "financial_resolution": decision.financial_resolution,
            "resolution_actions": decision.resolution_actions,
        }
        if claim_assessments:
            output["claim_assessments"] = claim_assessments

        return output, report
