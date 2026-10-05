import asyncio
import json
from pathlib import Path
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway
from student_agent.policy.engine import _ids

async def test():
    settings = Settings.load(Path('.'))
    contracts = Contracts(Path('contracts/schemas'))
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gw:
        # Check first 10 cases (001 to 010)
        for i in range(1, 11):
            cid = f"L3B_CASE_{i:03d}"
            case = json.loads(Path(f'inputs/{cid}.json').read_text(encoding='utf-8'))
            order_ids = _ids(case, "order_id", "order_ids", "candidate_order_ids")
            claimed_id = case.get("customer_request", {}).get("claimed_order_id")
            all_oids = list(dict.fromkeys(([claimed_id] if claimed_id else []) + order_ids))
            
            topics = [c.get("topic") for c in case.get("customer_request", {}).get("claims", [])]
            print(f"\n=== {cid} (claims: {topics}) ===")
            print(f"  candidate/claimed order_ids: {all_oids}")
            
            # Test tools with each order_id
            for oid in all_oids[:2]:
                print(f"  Testing with oid: {oid}")
                for tool in ["get_order", "get_order_items", "get_shipment_summary", "get_order_payments", "get_policy", "get_payment_timeline", "get_refund_timeline"]:
                    args = {"order_id": oid}
                    if tool == "get_policy":
                        args = {"policy_version": case.get("policy_version", "standard_complaint")}
                    try:
                        res = await gw._session.call_tool(tool, arguments={"case_id": cid, **args})
                        if not res.is_error:
                            print(f"    [OK] {tool}")
                        else:
                            txt = res.content[0].text if res.content else ""
                            # print short error
                            print(f"    [FAIL] {tool}: {txt[:40]}")
                    except Exception as e:
                        print(f"    [EXC] {tool}: {e}")

if __name__ == '__main__':
    asyncio.run(test())
