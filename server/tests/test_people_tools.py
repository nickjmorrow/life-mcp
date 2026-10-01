import asyncio

import people_mcp


def test_build_registers_tools():
    async def cli(*a, **k):
        return {"result": []}
    names = {t.name for t in asyncio.run(people_mcp.build(cli).list_tools())}
    assert names == {"people_find", "people_messages", "people_catch_up", "people_keep_in_touch", "people_note"}
