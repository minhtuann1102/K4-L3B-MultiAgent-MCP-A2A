import asyncio
import sys
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
        tools = await gw._session.list_tools()
        for t in tools.tools:
            print(f"Tool: {t.name}")
            print(t.input_schema)

if __name__ == '__main__':
    asyncio.run(main())
