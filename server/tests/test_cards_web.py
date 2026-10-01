"""The phone review page: same deck, and other web pages can't drive it."""
from starlette.testclient import TestClient

import cards_mcp
import cards_web
from tests.test_cards import NOW, FakeCli


def client(monkeypatch, cli=None):
    cli = cli or FakeCli()
    monkeypatch.setattr(cards_web, "deck", cards_mcp.Deck(cli, clock=lambda: NOW))
    return TestClient(cards_web.app, base_url=f"http://127.0.0.1:{cards_web.PORT}"), cli


def test_review_loop(monkeypatch):
    c, cli = client(monkeypatch)
    assert "Start review" in c.get("/").text
    nxt = c.get("/api/next").json()
    assert nxt["card"]["id"] == 1 and nxt["due"] == 1 and "back" not in nxt["card"]
    assert c.get("/api/answer/1").json()["back"] == "An append-only sequence of records"
    r = c.post("/api/rate", json={"id": 1, "rating": "good"}, headers={"X-Cards": "1"})
    assert r.status_code == 200 and r.json()["next_review"] and cli.writes
    pages = {p["page"]: p for p in c.get("/api/pages").json()}
    assert pages["DDIA"]["due"] == 1 and pages["Geo"]["later"] == 1


def test_writes_need_the_header(monkeypatch):
    c, cli = client(monkeypatch)
    assert c.post("/api/rate", json={"id": 1, "rating": "good"}).status_code == 403 and not cli.writes


def test_foreign_host_refused(monkeypatch):
    c, _ = client(monkeypatch)
    assert c.get("/api/next", headers={"Host": "evil.example"}).status_code == 403
    assert c.get("/", headers={"Host": "evil.example:8769"}).status_code == 403


def test_bad_rating(monkeypatch):
    c, _ = client(monkeypatch)
    assert c.post("/api/rate", json={"id": 1, "rating": "meh"}, headers={"X-Cards": "1"}).status_code == 400


def test_page_filter_and_empty(monkeypatch):
    cli = FakeCli()
    cli.cards[1][3] = NOW + 10**9
    c, _ = client(monkeypatch, cli)
    assert c.get("/api/next?page=Geo").json()["card"] is None
    assert c.get("/api/next?page=DDIA&new=1").json()["card"]["id"] == 2
