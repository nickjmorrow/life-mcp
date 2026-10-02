"""ChatLog: every tool call's name, for counting chats that skip memory_recall."""
import asyncio
import json

from fastmcp import Client, FastMCP

import server
import usage_log


def test_calls_logged_by_name():
    m = FastMCP("t")

    @m.tool
    def ping() -> str:
        return "pong"

    m.add_middleware(server.ChatLog())

    async def go():
        async with Client(m) as c:
            await c.call_tool("ping", {})
            await c.call_tool("ping", {})
    asyncio.run(go())
    rows = [json.loads(l) for l in usage_log.PATH.read_text().splitlines()]
    assert [(r["tool"], r["name"]) for r in rows] == [("call", "ping"), ("call", "ping")]
