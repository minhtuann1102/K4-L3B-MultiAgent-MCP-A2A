"""Phase 4 tests covering policy decisions, verifier checks, and confidence calibration."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


class MockGateway:
    def __init__(self, data_map: dict[str, dict[str, Any]]) -> None:
        self.data_map = data_map

    async def list_tools(self) -> list[str]:
        return list(self.data_map.keys())

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        data = self.data_map.get(tool_name, {})
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{tool_name:0<20}",
            "result_hash": "sha256:" + "0" * 64,
            "domain": "policy" if "policy" in tool_name else "order",
            "data": data,
        }


def test_case_1_canceled_order_paid(tmp_path: Path) -> None:
    """Case 1: Canceled order with captured payment -> full refund, high confidence."""
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    gateway = MockGateway({
        "get_order": {"order_id": "order-1", "order_status": "canceled"},
        "get_order_payments": {"captured_total_brl": 150.0, "refunded_total_brl": 0.0},
        "get_policy": {"policy_id": "std-1", "refund_eligible": True},
    })

    case = {"case_id": "CASE_CANCELED_001", "order_id": "order-1"}
    result = asyncio.run(solve_case(case, gateway, trace))
    contracts.validate_output(result, "case 1 output")

    assert result["assessment"]["primary_issue"] == "canceled_order_paid"
    assert result["assessment"]["case_status"] == "action_required"
    assert result["financial_resolution"]["recommended_refund_brl"] == 150.0
    assert len(result["financial_resolution"]["refund_lines"]) == 1
    assert result["financial_resolution"]["refund_lines"][0]["amount_brl"] == 150.0
    assert result["assessment"]["confidence"] >= 0.90


def test_case_2_late_delivery_seller(tmp_path: Path) -> None:
    """Case 2: Seller failed to ship on time -> late_delivery_seller, seller responsible."""
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    gateway = MockGateway({
        "get_order": {"order_id": "order-2", "order_status": "delivered"},
        "get_shipment": {
            "status": "delivered_late",
            "seller_fault": True,
            "seller_id": "seller_abc",
        },
        "get_sellers": {"seller_ids": ["seller_abc"]},
        "get_order_payments": {"captured_total_brl": 200.0},
        "get_policy": {"policy_id": "std-1", "late_delivery_comp_rate": 0.1},
    })

    case = {"case_id": "CASE_SELLER_LATE_002", "order_id": "order-2"}
    result = asyncio.run(solve_case(case, gateway, trace))
    contracts.validate_output(result, "case 2 output")

    assert result["assessment"]["primary_issue"] == "late_delivery_seller"
    responsible_types = [
        p["party_type"] for p in result["root_cause_analysis"]["responsible_parties"]
    ]
    assert "seller" in responsible_types
    assert result["root_cause_analysis"]["responsible_parties"][0]["party_id"] == "seller_abc"
    assert result["shipment_analysis"]["verdict"] == "seller_delay"
    assert "seller_abc" in result["shipment_analysis"]["late_seller_ids"]


def test_case_3_late_delivery_logistics(tmp_path: Path) -> None:
    """Case 3: Carrier delay -> late_delivery_logistics, logistics_provider responsible."""
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    gateway = MockGateway({
        "get_order": {"order_id": "order-3", "order_status": "delivered"},
        "get_shipment": {"status": "delivered_late", "carrier_fault": True},
        "get_order_payments": {"captured_total_brl": 80.0},
        "get_policy": {"policy_id": "std-1"},
    })

    case = {"case_id": "CASE_CARRIER_LATE_003", "order_id": "order-3"}
    result = asyncio.run(solve_case(case, gateway, trace))
    contracts.validate_output(result, "case 3 output")

    assert result["assessment"]["primary_issue"] == "late_delivery_logistics"
    responsible_types = [
        p["party_type"] for p in result["root_cause_analysis"]["responsible_parties"]
    ]
    assert "logistics_provider" in responsible_types
    assert result["shipment_analysis"]["verdict"] == "logistics_delay"


def test_case_4_data_conflict_penalizes_confidence(tmp_path: Path) -> None:
    """Case 4: Data conflict between sources -> conflict recorded, confidence penalized."""
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    gateway = MockGateway({
        "get_order": {
            "order_id": "order-4",
            "order_value_brl": 500.0,
            "order_status": "delivered",
        },
        "get_shipment": {"status": "returned"},
        "get_order_payments": {"captured_total_brl": 100.0},
        "get_policy": {"policy_id": "std-1"},
    })

    case = {"case_id": "CASE_CONFLICT_004", "order_id": "order-4"}
    result = asyncio.run(solve_case(case, gateway, trace))
    contracts.validate_output(result, "case 4 output")

    assert len(result["data_conflicts"]) > 0
    assert result["assessment"]["confidence"] <= 0.80
    assert "data_conflict_detected" in result["assessment"]["secondary_issues"]


def test_trace_lifecycle_events_complete(tmp_path: Path) -> None:
    """Verify trace contains complete A2A lifecycle events in order."""
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)

    gateway = MockGateway({
        "get_order": {"order_id": "order-life"},
        "get_policy": {"policy_id": "std-1"},
    })

    trace.emit(case_id="CASE_LIFE_005", event_type="case_received", actor="coordinator")
    output = asyncio.run(
        solve_case({"case_id": "CASE_LIFE_005", "order_id": "order-life"}, gateway, trace)
    )
    assert output["case_id"] == "CASE_LIFE_005"
    trace.emit(case_id="CASE_LIFE_005", event_type="case_finalized", actor="coordinator")

    content = trace_path.read_text(encoding="utf-8")
    assert "case_received" in content
    assert "task_assigned" in content
    assert "tool_result_consumed" in content
    assert "handoff" in content
    assert "policy_decided" in content
    assert "verification_completed" in content
    assert "case_finalized" in content
