from pathlib import Path

import pytest
from tests.test_phase4 import MockGateway

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


@pytest.mark.anyio
async def test_all_10_domain_issue_patterns(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    mock_data = {
        "get_order": {
            "order_id": "test-order-1",
            "order_status": "delivered",
            "order_value_brl": 100.0,
        },
        "get_shipment_summary": {
            "status": "delivered_late",
            "seller_fault": True,
            "seller_id": "seller-1",
        },
        "get_order_payments": {"captured_total_brl": 100.0, "refunded_total_brl": 0.0},
        "get_policy": {
            "policy_id": "std-1",
            "refund_eligible": True,
            "late_delivery_comp_rate": 0.1,
        },
        "get_sellers": {"seller_ids": ["seller-1"]},
        "get_payment_timeline": {"has_duplicate_event": False},
        "get_refund_timeline": {"status": "pending"},
    }
    gw = MockGateway(mock_data)

    test_topics = [
        (
            "late_delivery_logistics",
            "late_delivery_logistics",
            "action_required",
            "logistics_delay",
        ),
        ("valid_split_payment", "valid_split_payment", "no_action", "on_time"),
        ("payment_mismatch", "payment_mismatch", "action_required", "on_time"),
        ("duplicate_charge", "duplicate_charge", "action_required", "on_time"),
        ("refund_pending", "refund_pending", "action_required", "on_time"),
        ("refund_failed", "refund_failed", "action_required", "on_time"),
        ("unsupported_claim", "unsupported_claim", "no_action", "on_time"),
        ("canceled_order_paid", "canceled_order_paid", "action_required", "on_time"),
        ("unavailable_order_paid", "unavailable_order_paid", "action_required", "on_time"),
        ("late_delivery_seller", "late_delivery_seller", "action_required", "seller_delay"),
    ]

    for topic, exp_issue, exp_status, exp_shipment in test_topics:
        case = {
            "case_id": f"TEST_{topic.upper()}",
            "order_id": "test-order-1",
            "customer_request": {
                "claims": [
                    {"claim_id": "claim-1", "topic": topic},
                    {"claim_id": "claim-2", "topic": "requested_full_refund"},
                ]
            },
        }
        res = await solve_case(case, gw, trace)
        contracts.validate_output(res, f"test {topic}")
        asmt = res["assessment"]

        assert asmt["primary_issue"] == exp_issue
        assert asmt["case_status"] == exp_status
        assert res["shipment_analysis"]["verdict"] == exp_shipment
        assert 3 <= len(res["evidence_refs"]) <= 5
