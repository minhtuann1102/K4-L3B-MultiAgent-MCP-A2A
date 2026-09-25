import asyncio
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path('./src').resolve()))
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway

async def main():
    root = Path('.')
    settings = Settings.load(root)
    contracts = Contracts(root / 'contracts' / 'schemas')
    
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gw:
        case_id = "L3B_CASE_015"
        order_id = "c609f82bcf7a90292a5940205ebd7e93"
        
        try:
            ev = await gw.call("get_order_payments", case_id=case_id, order_id=order_id)
            print("get_order_payments data:")
            print(json.dumps(ev["data"], indent=2))
        except Exception as e:
            import traceback
            traceback.print_exc()

if __name__ == '__main__':
    asyncio.run(main())
