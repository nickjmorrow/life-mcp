"""A tiny fake chat.db, AddressBook database and Logseq CLI, with the real schemas' columns."""
import datetime as dt
import os
import sqlite3

EPOCH = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)


def ns(iso_local: str) -> int:
    from zoneinfo import ZoneInfo
    t = dt.datetime.fromisoformat(iso_local).replace(tzinfo=ZoneInfo("America/Chicago"))
    return int((t - EPOCH).total_seconds() * 1_000_000_000)


def body(text: str) -> bytes:
    raw = text.encode()
    n = bytes([len(raw)]) if len(raw) < 0x80 else b"\x81" + len(raw).to_bytes(2, "little")
    return b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+" + n + raw + b"\x86\x84"


class Chat:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            create table handle(ROWID integer primary key, id text, service text);
            create table chat(ROWID integer primary key, chat_identifier text, display_name text);
            create table chat_handle_join(chat_id int, handle_id int);
            create table chat_message_join(chat_id int, message_id int, message_date int);
            create table message(ROWID integer primary key, text text, attributedBody blob, handle_id int, date int,
                                 is_from_me int, associated_message_type int default 0, item_type int default 0);""")

    def handle(self, ident):
        return self.db.execute("insert into handle (id, service) values (?, 'iMessage')", (ident,)).lastrowid

    def chat(self, handles, name=None):
        cid = self.db.execute("insert into chat (chat_identifier, display_name) values (?, ?)", ("c", name)).lastrowid
        for h in handles:
            self.db.execute("insert into chat_handle_join values (?, ?)", (cid, h))
        return cid

    def msg(self, chat, when, text, handle=0, me=False, attributed=False, kind=0):
        mid = self.db.execute("insert into message (text, attributedBody, handle_id, date, is_from_me, associated_message_type)"
                              " values (?,?,?,?,?,?)", (None if attributed else text, body(text) if attributed else None,
                                                         handle, ns(when), int(me), kind)).lastrowid
        self.db.execute("insert into chat_message_join values (?, ?, ?)", (chat, mid, ns(when)))
        self.db.commit()


def contacts(path, people):
    """people: [(first, last, nick, birthday 'YYYY-MM-DD' or '--MM-DD' or None, [phones], [emails])]"""
    db = sqlite3.connect(path)
    db.executescript("""
        create table ZABCDRECORD(Z_PK integer primary key, ZFIRSTNAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION, ZBIRTHDAY);
        create table ZABCDPHONENUMBER(ZOWNER, ZFULLNUMBER);
        create table ZABCDEMAILADDRESS(ZOWNER, ZADDRESS);""")
    for first, last, nick, bday, phones, emails in people:
        z = None
        if bday:
            y, m, d = (1604, *map(int, bday[2:].split("-"))) if bday.startswith("--") else map(int, bday.split("-"))
            z = (dt.datetime(y, m, d, 12, tzinfo=dt.timezone.utc) - EPOCH).total_seconds()
        pk = db.execute("insert into ZABCDRECORD (ZFIRSTNAME, ZLASTNAME, ZNICKNAME, ZBIRTHDAY) values (?,?,?,?)",
                        (first, last, nick, z)).lastrowid
        db.executemany("insert into ZABCDPHONENUMBER values (?, ?)", [(pk, p) for p in phones])
        db.executemany("insert into ZABCDEMAILADDRESS values (?, ?)", [(pk, e) for e in emails])
    db.commit()


class FakeLogseq:
    """pages: {title: {"id": int, "props": {name: value}, "text": str}}"""
    def __init__(self, pages=None):
        # a page with "deleted": True or "alias_of": title is left out of person-page queries, like the real ones
        self.pages = pages or {}
        self.calls = []
        self.next_id = 900

    @staticmethod
    def _listed(p):
        return p.get("person", True) and not p.get("deleted") and not p.get("alias_of")

    async def __call__(self, *args, json_out=False):
        self.calls.append(args)
        opts = dict(a[2:].split("=", 1) for a in args if a.startswith("--") and "=" in a)
        if args[0] == "query":
            q = opts["query"]
            if ":block/alias" in q and "?alias" in q:
                return {"result": [[p["id"], a] for p in self.pages.values() for a in p.get("aliases", [])]}
            if "?name ?value" in q:
                return {"result": [[p["id"], k, v] for p in self.pages.values() if self._listed(p)
                                   for k, v in p.get("props", {}).items()]}
            return {"result": [[p["id"], t] for t, p in self.pages.items() if self._listed(p)]}
        if args[0] == "show":
            if opts["page"] not in self.pages:
                from fastmcp.exceptions import ToolError
                raise ToolError(f"page not found: {opts['page']}")
            return self.pages[opts["page"]].get("text", opts["page"])
        if args[:2] == ("upsert", "page"):
            self.next_id += 1
            self.pages.setdefault(opts["page"], {"id": self.next_id, "props": {}, "text": opts["page"], "blocks": []})
            return {"result": [self.pages[opts["page"]]["id"]]}
        if args[:2] == ("upsert", "block"):
            self.pages[opts["target-page"]].setdefault("blocks", []).append(opts["content"])
            return {"result": [1]}
        raise AssertionError(args)
