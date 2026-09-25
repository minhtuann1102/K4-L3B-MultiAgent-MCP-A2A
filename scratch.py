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
        try:
            output = await solve_case(case, gw, trace)
            print("OUTPUT:")
            print(json.dumps(output, indent=2))
        except Exception as e:
            import traceback
            print("ERROR IN SOLVE_CASE:")
            traceback.print_exc()

if __name__ == '__main__':
    asyncio.run(main())
