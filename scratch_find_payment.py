import asyncio
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path('./src').resolve()))
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway
from student_agent.workflow import _ids

async def main():
    root = Path('.')
    settings = Settings.load(root)
    contracts = Contracts(root / 'contracts' / 'schemas')
    
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gw:
        for p in (root / 'inputs').glob('L3B_CASE_*.json'):
            case = json.loads(p.read_text(encoding='utf-8'))
            order_id = _ids(case, "order_id", "candidate_order_ids")
            if not order_id:
                continue
            try:
                ev = await gw.call("get_order_payments", case_id=case["case_id"], order_id=order_id[0])
                if ev and ev.get("data"):
                    print(f"Success for {case['case_id']}:")
                    print(json.dumps(ev["data"], indent=2))
                    break
            except Exception as e:
                pass

if __name__ == '__main__':
    asyncio.run(main())
