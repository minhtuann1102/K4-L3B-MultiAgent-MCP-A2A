import asyncio
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path('./src').resolve()))
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway
from student_agent.workflow import solve_case
from student_agent.trace import TraceWriter

async def main():
    root = Path('.')
    settings = Settings.load(root)
    contracts = Contracts(root / 'contracts' / 'schemas')
    case = json.loads((root / 'inputs' / 'L3B_CASE_001.json').read_text(encoding='utf-8'))
    
    trace_path = root / 'traces' / 'trace.jsonl'
    trace = TraceWriter(trace_path, contracts)
    
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gw:
        # Instead of calling solve_case, we can just run the workflow steps manually 
        # or we can import the workflow internals to print errors.
        tools = tuple(await gw.list_tools())
        state = {
            "case_id": case["case_id"],
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
        
        from student_agent.workflow import _order_item_worker, _investigation_worker
        await _order_item_worker(state, gw, trace)
        print("Errors after order worker:", state["errors"])
        await _investigation_worker(state, gw, trace)
        print("Errors after investigation:", state["errors"])

if __name__ == '__main__':
    asyncio.run(main())
