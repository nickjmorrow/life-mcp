"""A phone-friendly flashcard review page over the same deck as the cards_* tools (cards_mcp.Deck).

It reads the front aloud (the browser's speech synthesis), waits while he answers in his head, reveals and
reads the back, then takes Again / Hard / Good / Easy and moves on. Review state is Logseq's own FSRS
properties, so this, the cards_* tools and Logseq's review screen share one schedule.

Listens on 127.0.0.1 only; put it on the tailnet with `tailscale serve` (never Funnel). Requests are refused
unless their Host is localhost or listed in the private config's cards_hosts (or CARDS_HOSTS, comma-separated), and writes
need an X-Cards header, which a web page on another site can't send without a CORS preflight this server
never allows — so other pages can't drive it (DNS rebinding, CSRF).

  uv run --frozen cards_web.py   # from server/; port CARDS_PORT, default 8769
"""
from __future__ import annotations

import os

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

import cards_mcp
import fsrs
import private
import server

PORT = int(os.environ.get("CARDS_PORT", "8769"))
# localhost, plus the names it's reached by on the tailnet: the private config's cards_hosts (or CARDS_HOSTS).
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"} | set(private.get("cards_hosts", [])) | {
    h.strip() for h in os.environ.get("CARDS_HOSTS", "").split(",") if h.strip()}
deck = cards_mcp.Deck(server.cli)


def allowed(request: Request, write: bool = False) -> bool:
    host = request.headers.get("host", "")
    if host not in HOSTS and host.split(":")[0] not in HOSTS:
        return False
    return not write or request.headers.get("x-cards") == "1"


def forbidden() -> JSONResponse:
    return JSONResponse({"error": "forbidden"}, status_code=403)


def view(c: dict | None, due: list, new: list, later: list) -> dict:
    if not c:
        nxt = fsrs.describe_wait(later[0]["card"].due, deck.clock()) if later else None
        return {"card": None, "due": len(due), "new": len(new), "next": nxt}
    return {"card": {"id": c["id"], "front": c["front"], "page": c["page"], "new": c["card"].state == "new"},
            "due": len(due), "new": len(new)}


async def next_card(request: Request):
    if not allowed(request):
        return forbidden()
    page = request.query_params.get("page") or None
    include_new = request.query_params.get("new", "1") != "0"
    due, new, later = deck.split(await deck.cards(page))
    queue = due + (new if include_new else [])
    return JSONResponse(view(queue[0] if queue else None, due, new, later))


async def answer(request: Request):
    if not allowed(request):
        return forbidden()
    return JSONResponse({"back": await deck.back(int(request.path_params["id"]))})


async def rate(request: Request):
    if not allowed(request, write=True):
        return forbidden()
    body = await request.json()
    rating = body.get("rating")
    if rating not in fsrs.RATINGS:
        return JSONResponse({"error": "rating must be again, hard, good or easy"}, status_code=400)
    _, new = await deck.rate(int(body["id"]), rating)
    return JSONResponse({"next_review": fsrs.describe_wait(new.due, deck.clock())})


async def pages(request: Request):
    if not allowed(request):
        return forbidden()
    due, new, later = deck.split(await deck.cards())
    counts: dict[str, list[int]] = {}
    for bucket, i in ((due, 0), (new, 1), (later, 2)):
        for c in bucket:
            counts.setdefault(c["page"], [0, 0, 0])[i] += 1
    return JSONResponse([{"page": p, "due": d, "new": n, "later": l} for p, (d, n, l) in sorted(counts.items())])


async def index(request: Request):
    if not allowed(request):
        return forbidden()
    return HTMLResponse(PAGE)


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>Cards</title>
<style>
:root { --bg:#0b1630; --panel:#13224a; --text:#e8ecf7; --muted:#9aa6c8; --sun:#ffd23f; --again:#ff6b6b; --hard:#ffa94d; --good:#69db7c; --easy:#4dabf7; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:17px/1.45 -apple-system, system-ui, sans-serif;
       min-height:100vh; display:flex; flex-direction:column; padding:16px; padding-bottom:max(16px, env(safe-area-inset-bottom)); }
header { display:flex; gap:8px; align-items:center; justify-content:space-between; color:var(--muted); font-size:14px; }
select { background:var(--panel); color:var(--text); border:1px solid #2a3b6e; border-radius:8px; padding:6px 8px; font-size:14px; max-width:60%; }
main { flex:1; display:flex; flex-direction:column; justify-content:center; gap:18px; padding:24px 0; }
.front { font-size:24px; font-weight:600; }
.back { white-space:pre-wrap; border-top:1px solid #2a3b6e; padding-top:16px; color:#d6dcf0; }
.meta { color:var(--muted); font-size:13px; }
.bar { display:grid; grid-template-columns:repeat(4,1fr); gap:8px; }
button { font:600 17px -apple-system, system-ui, sans-serif; border:0; border-radius:12px; padding:16px 8px; color:#0b1630; }
.primary { width:100%; background:var(--sun); }
.again{background:var(--again)} .hard{background:var(--hard)} .good{background:var(--good)} .easy{background:var(--easy)}
.ghost { background:transparent; color:var(--muted); border:1px solid #2a3b6e; padding:8px 12px; font-size:14px; }
.done { text-align:center; color:var(--muted); }
</style></head><body>
<header><select id="page"><option value="">All pages</option></select>
  <span><button class="ghost" id="speak" title="Read aloud on/off">🔊 on</button> <button class="ghost" id="repeat">↻</button></span></header>
<main id="main"><button class="primary" id="start">Start review</button></main>
<script>
const $ = s => document.querySelector(s), main = $("#main");
let card = null, voice = localStorage.getItem("cards-voice") !== "off", lastSpoken = "";
$("#speak").textContent = voice ? "🔊 on" : "🔇 off";
function say(text) { lastSpoken = text; if (!voice || !window.speechSynthesis) return;
  speechSynthesis.cancel(); const u = new SpeechSynthesisUtterance(text); u.rate = 1.0; speechSynthesis.speak(u); }
async function api(path, opts = {}) { const r = await fetch(path, { ...opts, headers: { "X-Cards": "1", "Content-Type": "application/json" } });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.status); return r.json(); }
function esc(s) { return s.replace(/[&<>]/g, c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;" }[c])); }
async function loadPages() { try { for (const p of await api("/api/pages")) {
  const o = document.createElement("option"); o.value = p.page; o.textContent = `${p.page} (${p.due} due, ${p.new} new)`; $("#page").append(o); } } catch (e) {} }
async function next() {
  const data = await api("/api/next?page=" + encodeURIComponent($("#page").value));
  card = data.card;
  if (!card) { main.innerHTML = `<div class="done"><p>Nothing to review right now.</p>${data.next ? `<p>Next card due ${esc(data.next)}.</p>` : ""}</div>`; say("All done."); return; }
  main.innerHTML = `<div class="meta">${esc(card.page)} · ${card.new ? "new card" : "review"} · ${data.due} due, ${data.new} new</div>
    <div class="front">${esc(card.front)}</div><button class="primary" id="reveal">Reveal</button>`;
  $("#reveal").onclick = reveal; say(card.front);
}
async function reveal() {
  const { back } = await api("/api/answer/" + card.id);
  $("#reveal").remove();
  main.insertAdjacentHTML("beforeend", `<div class="back">${esc(back)}</div>
    <div class="bar"><button class="again">Again</button><button class="hard">Hard</button><button class="good">Good</button><button class="easy">Easy</button></div>`);
  for (const r of ["again","hard","good","easy"]) main.querySelector("." + r).onclick = () => rate(r);
  say(back);
}
async function rate(rating) { await api("/api/rate", { method: "POST", body: JSON.stringify({ id: card.id, rating }) }); next(); }
$("#start").onclick = () => { say(""); next(); };  // a tap unlocks speech on iPhone
$("#page").onchange = () => card && next();
$("#speak").onclick = () => { voice = !voice; localStorage.setItem("cards-voice", voice ? "on" : "off");
  $("#speak").textContent = voice ? "🔊 on" : "🔇 off"; if (!voice) speechSynthesis.cancel(); };
$("#repeat").onclick = () => say(lastSpoken);
document.addEventListener("keydown", e => { if (!card) return; const k = e.key;
  if ((k === " " || k === "Enter") && $("#reveal")) { e.preventDefault(); reveal(); }
  else if ("1234".includes(k) && !$("#reveal")) rate(["again","hard","good","easy"][+k - 1]); });
loadPages();
</script></body></html>"""

app = Starlette(routes=[
    Route("/", index), Route("/api/next", next_card), Route("/api/answer/{id:int}", answer),
    Route("/api/rate", rate, methods=["POST"]), Route("/api/pages", pages),
])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
