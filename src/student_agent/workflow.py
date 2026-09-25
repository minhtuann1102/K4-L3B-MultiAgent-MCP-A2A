"""Phase 3 - Specialist Agents & MCP Gateway integration.

Design invariants (mirror ARCHITECTURE.md section 6):
- case_id is forwarded unchanged on every gateway.call().
- evidence_ref is NEVER fabricated; only server-issued refs are stored/emitted.
- tool_result_consumed is emitted once per successful gateway call.
- policy_decided is emitted after the policy agent runs.
- verification_completed is emitted once at the end.
- Retry budget: MAX_MCP_ATTEMPTS=2 (one retry) per evidence read.
- Iteration cap: MAX_ITERATIONS=5 handoff steps across all workers.
- Only tools advertised by MCP discovery are ever called.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .mcp_gateway import EvidenceGateway
from .state import ComplaintState
from .trace import TraceWriter

MAX_ITERATIONS = 10
MAX_MCP_ATTEMPTS = 2


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def _strings(value: Any) -> list[str]:
    """Normalize scalar/list identifiers without turning absent data into facts."""
    if isinstance(value, str) and value:
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    return []


def _first(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return None


def _ids(data: Any, *names: str) -> list[str]:
    if not isinstance(data, dict):
        return []
    found: list[str] = []
    for name in names:
        found.extend(_strings(data.get(name)))
    return list(dict.fromkeys(found))[:20]


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


def _tool(tools: Iterable[str], *candidates: str) -> str | None:
    """Select only a tool advertised by MCP; never probe unadvertised names."""
    advertised = set(tools)
    for candidate in candidates:
        if candidate in advertised:
            return candidate
    return None


# ---------------------------------------------------------------------------
# _consume - the central MCP call + trace emit
# ---------------------------------------------------------------------------


async def _consume(
    state: ComplaintState,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    *,
    actor: str,
    tool_name: str | None,
    arguments: dict[str, str],
) -> dict[str, Any] | None:
    """Call one MCP tool (with one retry) and emit tool_result_consumed."""
    if tool_name is None:
        return None
    evidence: dict[str, Any] | None = None
    last_error: Exception | None = None
    for _attempt in range(MAX_MCP_ATTEMPTS):
        try:
            evidence = await gateway.call(tool_name, case_id=state["case_id"], **arguments)
            break
        except Exception as exc:  # Retry only the idempotent evidence read once.
            last_error = exc
    if evidence is None:
        error_name = type(last_error).__name__ if last_error else "UnknownError"
        state["errors"] = [*state["errors"], f"{actor}:{error_name}"]
        return None
    # Principle 2: preserve server-issued evidence_ref verbatim - never fabricate.
    state["evidence"] = [*state["evidence"], evidence]
    # Principle 4: emit tool_result_consumed with the real server-issued ref.
    trace.emit(
        case_id=state["case_id"],
        event_type="tool_result_consumed",
        actor=actor,
        tool_name=tool_name,
        evidence_refs=[evidence["evidence_ref"]],
    )
    return evidence


# ---------------------------------------------------------------------------
# Specialist Agent 1: order-item-agent
# ---------------------------------------------------------------------------


async def _order_item_worker(
    state: ComplaintState, gateway: EvidenceGateway, trace: TraceWriter
) -> None:
    """Resolve the canonical order_id and populate customer context."""
    case = state["case"]
    supplied = _ids(case, "order_id", "order_ids")
    candidates = _ids(case, "candidate_order_ids", "order_candidates")
    order_id = (supplied or candidates or [""])[0]
    evidence = await _consume(
        state,
        gateway,
        trace,
        actor="order-item-agent",
        tool_name=_tool(
            state["available_tools"], "get_order", "get_order_summary", "resolve_order"
        ),
        arguments={"order_id": order_id} if order_id else {},
    )
    data = evidence.get("data", {}) if evidence else {}
    if isinstance(data, dict):
        state["analysis"]["customer"] = data
    resolved = _ids(data, "order_id", "order_ids", "resolved_order_id") or supplied
    state["resolved_order_ids"] = resolved
    state["rejected_candidates"] = [item for item in candidates if item not in resolved]
    state["entity_confidence"] = (
        0.9 if evidence and resolved else (0.5 if resolved else 0.0)
    )


# ---------------------------------------------------------------------------
# Specialist Agent 2: shipment-agent
# ---------------------------------------------------------------------------


async def _shipment_worker(
    state: ComplaintState,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    order_id: str,
) -> None:
    """Collect authoritative shipment facts for the resolved order."""
    trace.emit(
        case_id=state["case_id"],
        event_type="task_assigned",
        actor="coordinator",
        target="shipment-agent",
    )
    evidence = await _consume(
        state,
        gateway,
        trace,
        actor="shipment-agent",
        tool_name=_tool(
            state["available_tools"],
            "get_shipment",
            "get_shipment_summary",
            "get_order_shipment",
        ),
        arguments={"order_id": order_id},
    )
    if evidence:
        state["analysis"]["shipment"] = evidence["data"]
    state["iteration_count"] += 1


# ---------------------------------------------------------------------------
# Specialist Agent 3: payment-agent
# ---------------------------------------------------------------------------


async def _payment_worker(
    state: ComplaintState,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    order_id: str,
) -> None:
    """Collect authoritative payment/refund facts."""
    trace.emit(
        case_id=state["case_id"],
        event_type="task_assigned",
        actor="coordinator",
        target="payment-agent",
    )
    evidence = await _consume(
        state,
        gateway,
        trace,
        actor="payment-agent",
        tool_name=_tool(
            state["available_tools"],
            "get_order_payments",
            "get_payment_summary",
            "get_payments",
        ),
        arguments={"order_id": order_id},
    )
    if evidence:
        state["analysis"]["payment"] = evidence["data"]
    state["iteration_count"] += 1


# ---------------------------------------------------------------------------
# Specialist Agent 4: policy-agent
# ---------------------------------------------------------------------------


async def _policy_worker(
    state: ComplaintState,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> None:
    """Fetch the applicable refund/complaint policy."""
    trace.emit(
        case_id=state["case_id"],
        event_type="task_assigned",
        actor="coordinator",
        target="policy-agent",
    )
    policy_version = state["case"].get("policy_version", "standard_complaint")
    evidence = await _consume(
        state,
        gateway,
        trace,
        actor="policy-agent",
        tool_name=_tool(state["available_tools"], "get_policy", "get_refund_policy"),
        arguments={"policy_type": policy_version},
    )
    if evidence:
        state["analysis"]["policy"] = evidence["data"]
        policy_ref = evidence["evidence_ref"]
        trace.emit(
            case_id=state["case_id"],
            event_type="policy_decided",
            actor="policy-agent",
            decision_code=str(
                _first(evidence["data"], "policy_id", "policy_version", "id")
                or policy_version
            ),
            evidence_refs=[policy_ref],
        )
    state["iteration_count"] += 1


# ---------------------------------------------------------------------------
# Investigation orchestrator (coordinator -> specialists)
# ---------------------------------------------------------------------------


async def _customer_history_worker(
    state: ComplaintState,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    order_id: str,
) -> None:
    """Fetch customer purchase/complaint history for context."""
    trace.emit(
        case_id=state["case_id"],
        event_type="task_assigned",
        actor="coordinator",
        target="customer-history-agent",
    )
    # Prefer customer_unique_id_hint from case, fall back to order_id scope.
    customer_id = str(
        state["case"].get("customer_unique_id_hint")
        or _first(state["analysis"].get("customer", {}), "customer_unique_id", "customer_id")
        or order_id
    )
    evidence = await _consume(
        state,
        gateway,
        trace,
        actor="customer-history-agent",
        tool_name=_tool(state["available_tools"], "get_customer_history"),
        arguments={"customer_id": customer_id},
    )
    if evidence:
        state["analysis"]["customer_history"] = evidence["data"]
    state["iteration_count"] += 1


async def _items_worker(
    state: ComplaintState,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    order_id: str,
) -> None:
    """Fetch order items detail."""
    trace.emit(
        case_id=state["case_id"],
        event_type="task_assigned",
        actor="coordinator",
        target="items-agent",
    )
    evidence = await _consume(
        state,
        gateway,
        trace,
        actor="items-agent",
        tool_name=_tool(state["available_tools"], "get_order_items"),
        arguments={"order_id": order_id},
    )
    if evidence:
        state["analysis"]["items"] = evidence["data"]
    state["iteration_count"] += 1


async def _sellers_worker(
    state: ComplaintState,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    order_id: str,
) -> None:
    """Fetch seller details involved in the order."""
    trace.emit(
        case_id=state["case_id"],
        event_type="task_assigned",
        actor="coordinator",
        target="sellers-agent",
    )
    evidence = await _consume(
        state,
        gateway,
        trace,
        actor="sellers-agent",
        tool_name=_tool(state["available_tools"], "get_sellers"),
        arguments={"order_id": order_id},
    )
    if evidence:
        state["analysis"]["sellers"] = evidence["data"]
    state["iteration_count"] += 1


async def _payment_timeline_worker(
    state: ComplaintState,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    order_id: str,
) -> None:
    """Fetch payment event timeline (capture, refund, chargeback events)."""
    trace.emit(
        case_id=state["case_id"],
        event_type="task_assigned",
        actor="coordinator",
        target="payment-timeline-agent",
    )
    evidence = await _consume(
        state,
        gateway,
        trace,
        actor="payment-timeline-agent",
        tool_name=_tool(state["available_tools"], "get_payment_timeline"),
        arguments={"order_id": order_id},
    )
    if evidence:
        state["analysis"]["payment_timeline"] = evidence["data"]
    state["iteration_count"] += 1


async def _refund_timeline_worker(
    state: ComplaintState,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    order_id: str,
) -> None:
    """Fetch refund event timeline to confirm pending/failed refund status."""
    trace.emit(
        case_id=state["case_id"],
        event_type="task_assigned",
        actor="coordinator",
        target="refund-timeline-agent",
    )
    evidence = await _consume(
        state,
        gateway,
        trace,
        actor="refund-timeline-agent",
        tool_name=_tool(state["available_tools"], "get_refund_timeline"),
        arguments={"order_id": order_id},
    )
    if evidence:
        state["analysis"]["refund_timeline"] = evidence["data"]
    state["iteration_count"] += 1


async def _product_context_worker(
    state: ComplaintState,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    order_id: str,
) -> None:
    """Fetch product context for item classification and fraud signals."""
    trace.emit(
        case_id=state["case_id"],
        event_type="task_assigned",
        actor="coordinator",
        target="product-context-agent",
    )
    evidence = await _consume(
        state,
        gateway,
        trace,
        actor="product-context-agent",
        tool_name=_tool(state["available_tools"], "get_product_context"),
        arguments={"order_id": order_id},
    )
    if evidence:
        state["analysis"]["product_context"] = evidence["data"]
    state["iteration_count"] += 1


async def _investigation_worker(
    state: ComplaintState, gateway: EvidenceGateway, trace: TraceWriter
) -> None:
    """Run all 9 specialist domain agents in sequence under iteration cap."""
    order_id = state["resolved_order_ids"][0] if state["resolved_order_ids"] else ""
    if not order_id:
        return
    # Domain specialists — each guarded by iteration cap and error halt
    for worker in (
        lambda: _shipment_worker(state, gateway, trace, order_id),
        lambda: _payment_worker(state, gateway, trace, order_id),
        lambda: _policy_worker(state, gateway, trace),
        lambda: _customer_history_worker(state, gateway, trace, order_id),
        lambda: _items_worker(state, gateway, trace, order_id),
        lambda: _sellers_worker(state, gateway, trace, order_id),
        lambda: _payment_timeline_worker(state, gateway, trace, order_id),
        lambda: _refund_timeline_worker(state, gateway, trace, order_id),
        lambda: _product_context_worker(state, gateway, trace, order_id),
    ):
        if state["iteration_count"] >= MAX_ITERATIONS or state["errors"]:
            break
        await worker()


# ---------------------------------------------------------------------------
# Semantic analysis helpers (pure, no MCP calls)
# ---------------------------------------------------------------------------


def _shipment_verdict(shipment: dict[str, Any]) -> str:
    """Map raw shipment data to a schema-valid shipment verdict."""
    status = str(
        _first(shipment, "status", "delivery_status", "shipment_status") or ""
    ).lower()
    is_late = _bool_val(shipment, "is_late", "late", "delivered_late")
    carrier_fault = _bool_val(shipment, "carrier_fault", "logistics_fault")
    seller_fault = _bool_val(shipment, "seller_fault", "seller_delay")
    if "lost" in status or "missing" in status:
        return "lost"
    if "return" in status or "returned" in status:
        return "returned"
    if "conflict" in status:
        return "conflicting"
    if seller_fault or "seller" in status:
        return "seller_delay"
    if carrier_fault or "carrier" in status or "logistics" in status:
        return "logistics_delay"
    if is_late is True:
        return "seller_delay"
    if "deliver" in status or "completed" in status or is_late is False:
        return "on_time"
    return "insufficient_evidence"


def _payment_verdict(
    payment: dict[str, Any],
    refund_timeline: dict[str, Any] | None = None,
    payment_timeline: dict[str, Any] | None = None,
) -> str:
    """Map raw payment data to a schema-valid payment verdict.

    Optional timeline data provides cross-domain refinement:
    - refund_timeline: confirms a pending/failed refund has real event records
    - payment_timeline: detects duplicate capture events from event log
    """
    status = str(_first(payment, "status", "payment_status", "state") or "").lower()
    captured = _number(payment, "captured_total_brl", "captured_amount_brl", "paid_total_brl")
    refunded = _number(payment, "refunded_total_brl", "refund_total_brl")
    has_duplicate = _bool_val(payment, "duplicate_charge", "duplicate_capture")
    # Cross-check with payment timeline for duplicate capture events
    if not has_duplicate and isinstance(payment_timeline, dict):
        has_duplicate = _bool_val(payment_timeline, "duplicate_capture", "has_duplicate_event")
    if has_duplicate:
        return "duplicate_capture"
    if "mismatch" in status or "capture_mismatch" in status:
        return "capture_mismatch"
    # Cross-check with refund timeline: if timeline shows a failed event, trust it
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


def _primary_issue(
    state: ComplaintState,
    shipment_verdict: str,
    payment_verdict: str,
) -> str:
    """Derive primary_issue from collected evidence and case claims."""
    payment = state["analysis"].get("payment", {}) or {}
    case = state["case"]
    claims = _strings(
        [
            c.get("topic", "")
            for c in (case.get("customer_request", {}) or {}).get("claims", [])
            if isinstance(c, dict)
        ]
    )
    order_status = str(
        _first(state["analysis"].get("customer", {}), "order_status", "status") or ""
    ).lower()
    if payment_verdict == "duplicate_capture":
        return "duplicate_charge"
    if payment_verdict == "capture_mismatch":
        return "payment_mismatch"
    if payment_verdict == "refund_failed":
        return "refund_failed"
    if payment_verdict == "refund_pending":
        return "refund_pending"
    if "cancel" in order_status and _number(payment, "captured_total_brl", "paid_total_brl"):
        return "canceled_order_paid"
    if "unavailable" in order_status and _number(payment, "captured_total_brl", "paid_total_brl"):
        return "unavailable_order_paid"
    if shipment_verdict == "seller_delay":
        return "late_delivery_seller"
    if shipment_verdict == "logistics_delay":
        return "late_delivery_logistics"
    if "valid_split_payment" in claims:
        return "valid_split_payment"
    return "insufficient_evidence"


def _detect_conflicts(state: ComplaintState) -> list[dict[str, Any]]:
    """Detect data conflicts between MCP domain responses."""
    conflicts: list[dict[str, Any]] = []
    shipment = state["analysis"].get("shipment", {}) or {}
    payment = state["analysis"].get("payment", {}) or {}
    customer = state["analysis"].get("customer", {}) or {}
    captured = _number(payment, "captured_total_brl", "captured_amount_brl", "paid_total_brl")
    order_value = _number(customer, "order_value_brl", "total_brl", "price_brl")
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
        _first(shipment, "status", "delivery_status", "shipment_status") or ""
    ).lower()
    order_status = str(_first(customer, "order_status", "status") or "").lower()
    if (
        ship_status
        and order_status
        and "deliver" in order_status
        and "return" in ship_status
    ):
        conflicts.append(
            {
                "field": "shipment.status vs order.order_status",
                "sources": ["shipment-agent", "order-item-agent"],
                "selected_source": "shipment-agent",
                "resolution_code": "prefer_shipment_source",
            }
        )
    return conflicts[:5]


def _build_claim_assessments(
    case: dict[str, Any],
    state: ComplaintState,
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
                conf = 0.8 if state["analysis"].get("shipment") else 0.4
            elif shipment_verdict == "on_time":
                verdict = "unsupported"
                conf = 0.8
            else:
                verdict = "insufficient_evidence"
                conf = 0.3
        elif "refund" in topic or "payment" in topic or "split" in topic:
            if payment_verdict in ("refund_pending", "refund_failed", "duplicate_capture"):
                verdict = "supported"
                conf = 0.75
            elif payment_verdict == "reconciled":
                verdict = "unsupported"
                conf = 0.7
            else:
                verdict = "insufficient_evidence"
                conf = 0.3
        else:
            if refs:
                verdict = "partially_supported"
                conf = 0.5
            else:
                verdict = "insufficient_evidence"
                conf = 0.2
        assessments.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": round(conf, 2),
                "evidence_refs": relevant,
            }
        )
    return assessments


def _root_cause(
    primary_issue: str,
    shipment: dict[str, Any],
    state: ComplaintState,
) -> dict[str, Any]:
    """Build root_cause_analysis from evidence, linked to real MCP data."""
    cause_map: dict[str, tuple[str, str]] = {
        "late_delivery_seller": ("SELLER_DELAY", "seller"),
        "late_delivery_logistics": ("LOGISTICS_DELAY", "logistics_provider"),
        "canceled_order_paid": ("CANCELED_ORDER_PAID", "platform"),
        "unavailable_order_paid": ("UNAVAILABLE_ORDER_PAID", "seller"),
        "payment_mismatch": ("PAYMENT_CAPTURE_MISMATCH", "payment_provider"),
        "duplicate_charge": ("DUPLICATE_CAPTURE", "payment_provider"),
        "refund_pending": ("REFUND_NOT_PROCESSED", "platform"),
        "refund_failed": ("REFUND_FAILED", "payment_provider"),
        "valid_split_payment": ("SPLIT_PAYMENT_VALID", "platform"),
        "insufficient_evidence": ("EVIDENCE_INCOMPLETE", "unknown"),
    }
    cause_code, party_type = cause_map.get(primary_issue, ("UNCLASSIFIED_ISSUE", "unknown"))
    seller_ids = _ids(shipment, "seller_ids", "seller_id")
    party_id: str | None = seller_ids[0] if seller_ids else None
    ranked_causes: list[dict[str, Any]] = [{"cause_code": cause_code, "rank": 1}]
    if primary_issue != "insufficient_evidence" and state["errors"]:
        ranked_causes.append({"cause_code": "EVIDENCE_INCOMPLETE", "rank": 2})
    responsible: list[dict[str, Any]] = [{"party_type": party_type, "party_id": party_id}]
    return {"ranked_causes": ranked_causes[:5], "responsible_parties": responsible[:5]}


def _financial_resolution(
    state: ComplaintState,
    primary_issue: str,
    captured: float | None,
    refunded: float | None,
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Compute evidence-based financial resolution; never invent refund amounts."""
    refundable = max((captured or 0.0) - (refunded or 0.0), 0.0) if captured is not None else 0.0
    policy_eligible = _bool_val(policy, "refund_eligible", "eligible_for_refund")
    if policy_eligible is False:
        return {"currency": "BRL", "recommended_refund_brl": 0, "refund_lines": []}
    refund_lines: list[dict[str, Any]] = []
    recommended = 0.0
    order_id = (state["resolved_order_ids"] or [None])[0]
    if primary_issue in ("canceled_order_paid", "unavailable_order_paid"):
        if refundable > 0:
            recommended = round(refundable, 2)
            refund_lines.append(
                {"reason_code": primary_issue, "amount_brl": recommended, "entity_id": order_id}
            )
    elif primary_issue == "duplicate_charge":
        dup_amount = round(max((captured or 0.0) - (refunded or 0.0), 0.0), 2)
        if dup_amount > 0:
            recommended = dup_amount
            refund_lines.append(
                {"reason_code": "duplicate_capture", "amount_brl": recommended, "entity_id": order_id}
            )
    elif primary_issue in ("refund_pending", "refund_failed"):
        if refundable > 0:
            recommended = round(refundable, 2)
            refund_lines.append(
                {"reason_code": primary_issue, "amount_brl": recommended, "entity_id": order_id}
            )
    elif primary_issue in ("late_delivery_seller", "late_delivery_logistics"):
        comp_rate = float(_number(policy, "late_delivery_comp_rate") or 0.0)
        if comp_rate > 0 and captured is not None:
            recommended = round(captured * comp_rate, 2)
            refund_lines.append(
                {
                    "reason_code": "late_delivery_compensation",
                    "amount_brl": recommended,
                    "entity_id": order_id,
                }
            )
    return {"currency": "BRL", "recommended_refund_brl": recommended, "refund_lines": refund_lines[:10]}


def _resolution_actions(
    primary_issue: str,
    case_status: str,
    recommended_refund: float,
) -> list[str]:
    """Generate concrete resolution actions from evidence-based assessment."""
    if case_status == "needs_investigation":
        return ["Route case for manual investigation"]
    actions: list[str] = []
    if recommended_refund > 0:
        actions.append(f"Issue refund of {recommended_refund:.2f} BRL")
    action_map = {
        "late_delivery_seller": ["Notify seller of SLA breach", "Apply seller penalty per policy"],
        "late_delivery_logistics": ["File carrier delay report", "Notify customer of logistics delay"],
        "canceled_order_paid": ["Cancel order and process full refund"],
        "unavailable_order_paid": ["Process refund for unavailable item"],
        "duplicate_charge": ["Reverse duplicate payment capture"],
        "refund_pending": ["Trigger pending refund processing"],
        "refund_failed": ["Retry failed refund via alternate channel"],
        "payment_mismatch": ["Audit payment capture records"],
        "valid_split_payment": ["Confirm split payment legitimacy and close case"],
        "insufficient_evidence": ["Request additional documentation from customer"],
    }
    actions.extend(action_map.get(primary_issue, []))
    if not actions:
        actions.append("No action required")
    return list(dict.fromkeys(actions))[:8]


# ---------------------------------------------------------------------------
# Schema-valid empty output (safe fallback)
# ---------------------------------------------------------------------------


def _empty_output(case_id: str) -> dict[str, Any]:
    """A safe, schema-valid fallback. Makes no business claims."""
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "secondary_issues": [],
            "case_status": "needs_investigation",
            "confidence": 0.0,
        },
        "affected_entities": {
            "order_ids": [],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "entity_resolution": {
            "status": "not_found",
            "resolved_order_ids": [],
            "rejected_candidates": [],
            "confidence": 0.0,
        },
        "customer_context": {"customer_unique_id": None, "related_order_ids": []},
        "shipment_analysis": {
            "verdict": "insufficient_evidence",
            "late_seller_ids": [],
            "timeline_complete": False,
        },
        "payment_analysis": {
            "verdict": "insufficient_evidence",
            "captured_total_brl": None,
            "refunded_total_brl": None,
            "refundable_total_brl": None,
        },
        "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
        "evidence_refs": [],
        "data_conflicts": [],
        "financial_resolution": {"currency": "BRL", "recommended_refund_brl": 0, "refund_lines": []},
        "resolution_actions": ["Route case for manual investigation"],
    }


# ---------------------------------------------------------------------------
# Verifier: assemble final schema-valid output from accumulated state
# ---------------------------------------------------------------------------


def _build_output(state: ComplaintState) -> dict[str, Any]:
    """Assemble schema-valid output from real MCP evidence only."""
    result = _empty_output(state["case_id"])
    case = state["case"]

    # Collect only server-issued evidence refs - never fabricate
    refs = list(dict.fromkeys(item["evidence_ref"] for item in state["evidence"]))

    shipment = state["analysis"].get("shipment", {}) or {}
    payment = state["analysis"].get("payment", {}) or {}
    customer = state["analysis"].get("customer", {}) or {}
    policy = state["analysis"].get("policy", {}) or {}
    items = state["analysis"].get("items", {}) or {}
    sellers = state["analysis"].get("sellers", {}) or {}
    customer_history = state["analysis"].get("customer_history", {}) or {}
    payment_timeline = state["analysis"].get("payment_timeline", {}) or {}
    refund_timeline = state["analysis"].get("refund_timeline", {}) or {}
    product_context = state["analysis"].get("product_context", {}) or {}

    # Semantic verdicts (evidence-driven, cross-domain timeline refinement)
    ship_verdict = _shipment_verdict(shipment) if shipment else "insufficient_evidence"
    pay_verdict = (
        _payment_verdict(payment, refund_timeline or None, payment_timeline or None)
        if payment
        else "insufficient_evidence"
    )
    primary = _primary_issue(state, ship_verdict, pay_verdict)

    captured = _number(payment, "captured_total_brl", "captured_amount_brl", "paid_total_brl")
    refunded = _number(payment, "refunded_total_brl", "refund_total_brl")
    refundable = max((captured or 0.0) - (refunded or 0.0), 0.0) if captured is not None else None

    fin_res = _financial_resolution(state, primary, captured, refunded, policy)
    recommended_refund = fin_res["recommended_refund_brl"]

    if primary == "insufficient_evidence" or not refs:
        case_status = "needs_investigation"
        confidence = min(0.3, 0.1 * len(refs)) if refs else 0.0
    elif recommended_refund > 0:
        case_status = "action_required"
        confidence = min(0.85, 0.45 + 0.1 * len(refs))
    else:
        case_status = "no_action"
        confidence = min(0.80, 0.40 + 0.1 * len(refs))

    # Prefer dedicated domain data; fall back to cross-domain extraction
    seller_ids = (
        _ids(sellers, "seller_ids", "seller_id")
        or _ids(shipment, "seller_ids", "seller_id")
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

    late_seller_ids: list[str] = seller_ids[:20] if ship_verdict == "seller_delay" else []
    timeline_complete = (
        _bool_val(shipment, "timeline_complete", "timeline_full") is True
        or bool(_first(shipment, "delivered_at", "actual_delivery_date"))
    )

    root_cause = _root_cause(primary, shipment, state)
    conflicts = _detect_conflicts(state)

    secondary: list[str] = []
    if conflicts:
        secondary.append("data_conflict_detected")
    if state["errors"]:
        secondary.append("mcp_call_failed")
    if state["rejected_candidates"]:
        secondary.append("candidates_rejected")

    claim_assessments = _build_claim_assessments(case, state, ship_verdict, pay_verdict, refs)
    res_actions = _resolution_actions(primary, case_status, recommended_refund)

    result["assessment"] = {
        "primary_issue": primary,
        "secondary_issues": list(dict.fromkeys(secondary))[:10],
        "case_status": case_status,
        "confidence": round(confidence, 3),
    }
    result["affected_entities"] = {
        "order_ids": state["resolved_order_ids"],
        "item_ids": item_ids,
        "seller_ids": seller_ids,
        "payment_references": payment_refs,
        "shipment_ids": shipment_ids,
    }
    result["entity_resolution"] = {
        "status": "resolved" if state["resolved_order_ids"] else "not_found",
        "resolved_order_ids": state["resolved_order_ids"],
        "rejected_candidates": state["rejected_candidates"],
        "confidence": state["entity_confidence"],
    }
    result["customer_context"] = {
        "customer_unique_id": (
            _first(customer, "customer_unique_id", "customer_id")
            or _first(customer_history, "customer_unique_id", "customer_id")
        ),
        "related_order_ids": (
            _ids(customer_history, "related_order_ids", "order_ids", "previous_order_ids")
            or _ids(customer, "related_order_ids", "order_ids")
        ),
    }
    result["shipment_analysis"] = {
        "verdict": ship_verdict,
        "late_seller_ids": late_seller_ids,
        "timeline_complete": timeline_complete,
    }
    result["payment_analysis"] = {
        "verdict": pay_verdict,
        "captured_total_brl": captured,
        "refunded_total_brl": refunded,
        "refundable_total_brl": refundable,
    }
    result["root_cause_analysis"] = root_cause
    result["evidence_refs"] = refs
    result["data_conflicts"] = conflicts
    result["financial_resolution"] = fin_res
    result["resolution_actions"] = res_actions
    if claim_assessments:
        result["claim_assessments"] = claim_assessments
    return result


# ---------------------------------------------------------------------------
# Public entrypoint: solve_case
# ---------------------------------------------------------------------------


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run bounded supervisor/worker handoffs and return only audited evidence.

    Protocol (ARCHITECTURE.md section 6a):
      coordinator -> order-item-agent -> handoff ->
        shipment-agent -> payment-agent -> policy-agent ->
      verifier -> verification_completed
    """
    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("case must contain a non-empty case_id")

    tools = tuple(await gateway.list_tools())
    state: ComplaintState = {
        "case_id": case_id,
        "case": case,
        "available_tools": tools,
        "current_worker": "order-item-agent",
        "iteration_count": 0,
        "evidence": [],
        "errors": [],
        "resolved_order_ids": [],
        "rejected_candidates": [],
        "entity_confidence": 0.0,
        "analysis": {},
    }

    # Step 1: coordinator assigns order-item-agent
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="order-item-agent",
    )
    await _order_item_worker(state, gateway, trace)
    state["iteration_count"] += 1

    # Step 2: order-item-agent hands off to investigation team
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="order-item-agent",
        target="investigation-team",
    )

    # Step 3: investigation team (shipment / payment / policy)
    if state["iteration_count"] < MAX_ITERATIONS and not state["errors"]:
        await _investigation_worker(state, gateway, trace)

    # Step 4: verifier assembles output from evidence only
    output = _build_output(state)

    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code=output["assessment"]["primary_issue"],
        evidence_refs=output["evidence_refs"],
    )
    return output
