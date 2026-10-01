"""An in-memory stand-in for server.cli, covering only the calls memory_mcp and the block tools make."""
import asyncio
import re

from fastmcp.exceptions import ToolError

PAGE_ID = 100


def ident(name):
    """A property's db ident, as Logseq makes them: lowercased name plus a random suffix."""
    return f"user.property/{name.lower()}-Xy12"


class FakeLogseq:
    def __init__(self, page=True):
        self.page = page
        self.blocks = {}  # id -> {"text", "parent", "props"}
        self.next_id = 1000
        self.calls = []
        self.fail = None  # set to an error message to make every call raise
        self.title = "Claude memories"  # the page every block is on

    def add(self, text, parent=PAGE_ID, **props):
        self.next_id += 1
        self.blocks[self.next_id] = {"text": text, "parent": parent,
                                     "props": {k.replace("_", "-"): v for k, v in props.items()}}
        return self.next_id

    def children(self, parent):
        return [i for i, b in self.blocks.items() if b["parent"] == parent]

    def find(self, text):
        return next(i for i, b in self.blocks.items() if b["text"] == text)

    def _tree(self, bid):
        return {"db/id": bid, "block/title": self.blocks[bid]["text"], "block/order": f"{bid:08d}",
                "block/children": [self._tree(c) for c in self.children(bid)]}

    def _subtree(self, bid):
        return [bid] + [d for c in self.children(bid) for d in self._subtree(c)]

    def _remove(self, bid):
        for c in self.children(bid):
            self._remove(c)
        del self.blocks[bid]

    async def ensure_properties(self, names):
        self.calls.append(("ensure_properties", tuple(names)))

    async def __call__(self, *args, json_out=False):
        await asyncio.sleep(0)  # let parallel tool calls interleave, as real CLI calls do
        self.calls.append(args)
        if self.fail:
            raise ToolError(self.fail)
        opts = dict(a[2:].split("=", 1) for a in args if a.startswith("--") and "=" in a)
        if args[0] == "show":
            if not self.page:
                raise ToolError("page not found: Claude memories")
            return {"root": {"db/id": PAGE_ID, "block/title": "Claude memories",
                             "block/children": [self._tree(c) for c in self.children(PAGE_ID)]}}
        if args[0] == "query" and "?page-title" in opts["query"]:  # the page a block is on
            return {"result": [[self.title]] if int(opts["inputs"].strip("[]")) in self.blocks else []}
        if args[0] == "query" and "?parent" in opts["query"]:  # the page's parent links
            return {"result": [[i, b["parent"]] for i, b in self.blocks.items()]}
        if args[0] == "query" and "?name" not in opts["query"]:  # property idents, for clearing
            return {"result": [[i, ident(k)] for i in self.blocks for k in self.blocks[i]["props"]]}
        if args[0] == "query":
            ids = set(self.blocks)
            return {"result": [[i, k, v] for i in ids for k, v in self.blocks[i]["props"].items()]}
        if args[:2] == ("upsert", "page"):
            self.page = True
            return {"result": [PAGE_ID]}
        if args[:2] == ("upsert", "block"):
            if "id" in opts:
                block = self.blocks[int(opts["id"])]
                if "content" in opts:
                    block["text"] = opts["content"]
                if "remove-properties" in opts:
                    gone = set(re.findall(r":(user\.property/[\w.-]+)", opts["remove-properties"]))
                    block["props"] = {k: v for k, v in block["props"].items() if ident(k) not in gone}
                if "update-properties" in opts:
                    pairs = re.findall(r'"((?:[^"\\]|\\.)*)"', opts["update-properties"])
                    block["props"].update(zip(pairs[::2], pairs[1::2]))
                return {"result": [int(opts["id"])]}
            parent = int(opts["target-id"]) if "target-id" in opts else PAGE_ID
            new = self.add(opts["content"], parent)
            if "update-properties" in opts:
                pairs = re.findall(r'"((?:[^"\\]|\\.)*)"', opts["update-properties"])
                self.blocks[new]["props"].update(zip(pairs[::2], pairs[1::2]))
            return {"result": [new]}
        if args[:2] == ("remove", "block"):
            # Logseq sync rejects removing a block whose subtree still has user properties,
            # and the block comes back (seen live 2026-09-27). Clear them first.
            if any(self.blocks[b]["props"] for b in self._subtree(int(opts["id"]))):
                raise AssertionError("sync would reject this remove: properties still set")
            self._remove(int(opts["id"]))
            return {}
        raise AssertionError(f"unexpected call {args}")
