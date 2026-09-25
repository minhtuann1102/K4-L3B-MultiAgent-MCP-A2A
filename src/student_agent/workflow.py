"""Phase 4 - Multi-Agent Workflow with Policy & Verifier Agents.

Design invariants:
- case_id is forwarded unchanged on every gateway.call().
- evidence_ref is NEVER fabricated; only server-issued refs are stored/emitted.
- tool_result_consumed is emitted once per successful gateway call.
- policy_decided is emitted after PolicyAgent decides.
- verification_completed is emitted after VerifierAgent verifies.
- Retry budget: MAX_MCP_ATTEMPTS=2 per evidence read.
- Iteration cap: MAX_ITERATIONS=10 across specialists.
- Only tools advertised by MCP discovery are ever called.
"""
from __future__ import annotations

from typing import Any

from .agents import PolicyAgent, VerifierAgent
from .mcp_gateway import EvidenceGateway
from .policy.engine import _first, _ids, _tool
from .state import ComplaintState
from .trace import TraceWriter

MAX_ITERATIONS = 10
MAX_MCP_ATTEMPTS = 2


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
        except Exception as exc:
            last_error = exc
    if evidence is None:
        error_name = type(last_error).__name__ if last_error else "UnknownError"
        state["errors"] = [*state["errors"], f"{actor}:{error_name}"]
        return None
    state["evidence"] = [*state["evidence"], evidence]
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
    claimed = case.get("customer_request", {}).get("claimed_order_id")
    supplied = _ids(case, "order_id", "order_ids")
    if claimed and claimed not in supplied:
        supplied.append(claimed)
    candidates = _ids(case, "candidate_order_ids", "order_candidates")
    valid_candidates = [c for c in candidates if not c.startswith("candidate-") and len(c) == 32]
    order_id = (supplied or valid_candidates or candidates or [""])[0]
    evidence = await _consume(
        state,
        gateway,
        trace,
        actor="order-item-agent",
        tool_name=_tool(
            state["available_tools"], "get_order"
        ),
        arguments={"order_id": order_id} if order_id else {},
    )
    data = evidence.get("data", {}) if evidence else {}
    if isinstance(data, dict):
        state["analysis"]["customer"] = data
    resolved = _ids(data, "order_id", "order_ids", "resolved_order_id") or supplied or valid_candidates
    state["resolved_order_ids"] = resolved[:1]
    state["rejected_candidates"] = [item for item in candidates if item not in state["resolved_order_ids"]]
    state["entity_confidence"] = (
        0.95 if evidence and resolved else (0.85 if state["resolved_order_ids"] else 0.0)
    )


# ---------------------------------------------------------------------------
# Specialist Domain Workers
# ---------------------------------------------------------------------------


async def _shipment_worker(
    state: ComplaintState,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    order_id: str,
) -> None:
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
            "get_shipment_summary",
        ),
        arguments={"order_id": order_id},
    )
    if evidence:
        state["analysis"]["shipment"] = evidence["data"]
    state["iteration_count"] += 1


async def _payment_worker(
    state: ComplaintState,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    order_id: str,
) -> None:
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
        ),
        arguments={"order_id": order_id},
    )
    if evidence:
        state["analysis"]["payment"] = evidence["data"]
    state["iteration_count"] += 1


async def _policy_worker(
    state: ComplaintState,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> None:
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
        tool_name=_tool(state["available_tools"], "get_policy"),
        arguments={"policy_version": policy_version},
    )
    if evidence:
        state["analysis"]["policy"] = evidence["data"]
    state["iteration_count"] += 1


async def _customer_history_worker(
    state: ComplaintState,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    order_id: str,
) -> None:
    trace.emit(
        case_id=state["case_id"],
        event_type="task_assigned",
        actor="coordinator",
        target="customer-history-agent",
    )
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
        arguments={"customer_unique_id": customer_id},
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
    """Run specialist domain agents in sequence under iteration cap."""
    order_id = state["resolved_order_ids"][0] if state["resolved_order_ids"] else ""
    if not order_id:
        return
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
        # Only the iteration cap stops expansion; individual failures are recorded
        # in state["errors"] but must not silence all remaining specialist agents.
        if state["iteration_count"] >= MAX_ITERATIONS:
            break
        await worker()


# ---------------------------------------------------------------------------
# Public entrypoint: solve_case
# ---------------------------------------------------------------------------


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Execute complete multi-agent workflow: Specialists -> Policy -> Verifier."""
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

    # Step 1: Coordinator assigns order-item-agent
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="order-item-agent",
    )
    await _order_item_worker(state, gateway, trace)
    state["iteration_count"] += 1

    # Step 2: Handoff to investigation team
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="order-item-agent",
        target="investigation-team",
    )

    # Step 3: Investigation team collects domain evidence
    if state["iteration_count"] < MAX_ITERATIONS:
        await _investigation_worker(state, gateway, trace)

    # Step 4: Policy Agent evaluates policy and makes decision
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="investigation-team",
        target="policy-agent",
    )
    policy_agent = PolicyAgent()
    policy_decision = policy_agent.decide(
        case=case,
        analysis=state["analysis"],
        resolved_order_ids=state["resolved_order_ids"],
        has_errors=len(state["errors"]) > 0,
    )

    policy_refs = [
        item["evidence_ref"]
        for item in state["evidence"]
        if item.get("domain") == "policy" or "policy" in item.get("evidence_ref", "")
    ]
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=policy_decision.primary_issue,
        evidence_refs=policy_refs if policy_refs else None,
    )

    # Step 5: Verifier Agent checks invariants, calibrates confidence, builds output
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="policy-agent",
        target="verifier-agent",
    )
    verifier_agent = VerifierAgent()
    output, report = verifier_agent.verify_and_build(state, policy_decision)

    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier-agent",
        decision_code=output["assessment"]["primary_issue"],
        evidence_refs=output["evidence_refs"],
    )

    return output
