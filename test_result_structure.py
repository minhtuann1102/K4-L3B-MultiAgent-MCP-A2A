"""Test CallToolResult structure."""
import asyncio
from pathlib import Path

from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway


async def test():
    root = Path(".")
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as _:
        pass  # Just connect to test
        
    # Direct MCP test
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    
    headers = {"Authorization": f"Bearer {settings.team_api_key}"}
    timeout = httpx2.Timeout(300.0, connect=30.0)
    
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        streamable_http_client(settings.mcp_endpoint, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        result = await session.call_tool("get_order", arguments={"case_id": "L3B_CASE_001", "order_id": "af0bbb47f125381ce9f3597dc70ef07b"})
        
        print("CallToolResult attributes:")
        print(f"  - type: {type(result)}")
        print(f"  - dir: {[a for a in dir(result) if not a.startswith('_')]}")
        print(f"  - model_fields: {result.model_fields.keys()}")
        print(f"  - content: {len(result.content)} items")
        print(f"  - structuredContent: {type(result.structuredContent)}")
        
        # Check for error-related attributes
        for attr in ['isError', 'is_error', 'error', 'resultType', 'result_type']:
            if hasattr(result, attr):
                print(f"  - {attr}: {getattr(result, attr)}")


asyncio.run(test())
