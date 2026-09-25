"""Test MCP connection and tool calls."""
import asyncio
import json
from pathlib import Path

from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway


async def test_mcp():
    root = Path(".")
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    
    print(f"MCP Endpoint: {settings.mcp_endpoint}")
    print(f"API Key: {settings.team_api_key[:20]}...")
    
    try:
        async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
            print("\n[OK] Connected to MCP gateway")
            
            tools = await gateway.list_tools()
            print(f"\n[OK] Available tools ({len(tools)}):")
            for tool in tools:
                print(f"  - {tool}")
            
            # Try a simple tool call
            print("\n--> Testing get_order with order_id from CASE_001...")
            evidence = await gateway.call(
                "get_order",
                case_id="L3B_CASE_001",
                order_id="af0bbb47f125381ce9f3597dc70ef07b"
            )
            print(f"[OK] Evidence received:")
            print(f"  - evidence_ref: {evidence.get('evidence_ref')}")
            print(f"  - domain: {evidence.get('domain')}")
            print(f"  - data keys: {list(evidence.get('data', {}).keys())}")
            
    except Exception as e:
        print(f"\n[ERROR] {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(test_mcp())
