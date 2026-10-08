import asyncio

import people_mcp


def test_build_registers_tools():
    names = {t.name for t in asyncio.run(people_mcp.build().list_tools())}
    assert names == {"people_find", "people_messages", "people_catch_up", "people_keep_in_touch", "people_note"}


def test_person_pages_come_from_grimoire(monkeypatch):
    import grimoire_people
    import people_data
    from fastmcp import Client
    backends = []

    class Data:
        def __init__(self, backend):
            backends.append(backend)

        async def find(self, name):
            return name
    monkeypatch.setattr(people_data, "PeopleData", Data)

    async def go():
        async with Client(people_mcp.build()) as c:
            await c.call_tool("people_find", {"name": "Sam"})
    asyncio.run(go())
    assert len(backends) == 1 and isinstance(backends[0], grimoire_people.GrimoirePages)
