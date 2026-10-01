"""Music tools: play his Apple Music library in any room (Music.app → AirPlay), and control
whatever is playing on the HomePods (pyatv), including music started from his phone."""
import asyncio
from typing import Annotated, Literal

from pydantic import Field

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from homepods import HomePodError, HomePods, kind_of
from music_app import MusicApp
from rooms import Speaker, resolve, room_of, rooms
from host import NAME as HOST

mcp = FastMCP("Music")
READ = {"readOnlyHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": False}

_music: MusicApp | None = None
_pods: HomePods | None = None


def music() -> MusicApp:
    global _music
    _music = _music or MusicApp()
    return _music


def pods() -> HomePods:
    global _pods
    _pods = _pods or HomePods()
    return _pods


MUSIC_KINDS = {"homepod": "homepod", "apple tv": "tv", "computer": "mac"}


async def _speakers() -> list[Speaker]:
    """Speakers as the HomePods themselves report them (pyatv): for control and now-playing."""
    found = await pods().discover()
    return [Speaker(name, info["kind"]) for name, info in sorted(found.items())]


async def _music_speakers() -> list[Speaker]:
    """Speakers as Music can play to them. The living-room pair shows up only as the Apple TV."""
    return [Speaker(d["name"], MUSIC_KINDS.get(d["kind"].lower(), "other"), d["id"])
            for d in await music().airplay_devices()]


def _names(speakers) -> list[str]:
    return [s.name for s in speakers]


async def _mac_outputs(states=("playing",)) -> tuple[list[dict], dict]:
    """The speakers the server Mac's Music is sending to (not the Mac itself), if its state is one of states."""
    st = await music().state()
    if st.get("state") not in states:
        return [], st
    outs = [{"id": i, "name": n, "kind": MUSIC_KINDS.get(k.lower(), "other")}
            for n, i, k in zip(st.get("devices") or [], st.get("device_ids") or [], st.get("device_kinds") or [])]
    return [o for o in outs if o["kind"] in ("homepod", "tv")], st


def _key(name: str, kind: str) -> tuple[str, str]:
    """Speakers match across Music and the HomePods by room and kind ("Living Room" is the TV and the pair)."""
    return room_of(name), kind


async def _gather(names: list[str], call):
    """Run call(name) for every speaker at once; returns (done, failed) name lists and results."""
    results = await asyncio.gather(*(call(n) for n in names), return_exceptions=True)
    done = [n for n, r in zip(names, results) if not isinstance(r, Exception)]
    failed = [n for n, r in zip(names, results) if isinstance(r, Exception)]
    return done, failed, dict(zip(names, results))


Rooms = Annotated[str | list[str], Field(description="Room(s): 'kitchen', 'kitchen and office', 'everywhere', or 'TV'")]
Room = Annotated[str | None, Field(description="Room, or omit for everything that's playing")]


@mcp.tool(annotations=READ)
async def music_list_speakers() -> str:
    """Every speaker by room: online, its own volume, and whether this Mac is playing there."""
    found = await pods().discover()
    outs, _ = await _mac_outputs()
    here = {_key(o["name"], o["kind"]) for o in outs}
    speakers = sorted(((n, i) for n, i in found.items() if i["kind"] in ("homepod", "tv")),
                      key=lambda x: (x[1]["kind"] != "homepod", x[0]))
    _, _, volumes = await _gather([n for n, _ in speakers], pods().volume)
    lines = []
    for name, info in speakers:
        tv = info["kind"] == "tv"
        vol = volumes.get(name)
        lines.append(f"{room_of(name) + ' TV' if tv else name} ({'Apple TV' if tv else 'HomePod'}): online"
                     + (f", volume {vol:g}" if isinstance(vol, (int, float)) else "")
                     + (f", {HOST} playing here" if _key(name, info["kind"]) in here else ""))
    return "\n".join(lines) or "No speakers found on the network."


@mcp.tool(annotations=READ)
async def music_now_playing(room: Room = None) -> str:
    """What's playing, per room: the server Mac's music and anything started from his phone or Siri."""
    speakers = await _speakers()
    targets = resolve(room, speakers) if room else [s for s in speakers if s.kind == "homepod"]
    outs, mac = await _mac_outputs()
    from_mac = {_key(o["name"], o["kind"]) for o in outs} if mac.get("track") else set()
    ask = [s.name for s in targets if _key(s.name, s.kind) not in from_mac]
    _, _, results = await _gather(ask, pods().now_playing)
    lines = []
    for s in targets:
        if s.name not in results:
            t = mac["track"]
            lines.append(f"{s.name}: playing {t['name']} — {t['artist']} ({t['album']}), from {HOST}")
            continue
        p = results[s.name]
        if isinstance(p, Exception):
            lines.append(f"{s.name}: {p}")
        elif p["state"] in ("playing", "paused") and p["title"]:
            lines.append(f"{s.name}: {p['state']} {p['title']} — {p['artist']} ({p['album']})")
        else:
            lines.append(f"{s.name}: {p['state']}")
    return "\n".join(lines)


async def _find(what: str, kind: str | None) -> dict:
    """Exact playlist, then exact artist/album/song, then a playlist containing the words, then the
    first matching song. With an explicit kind, only an exact match of that kind."""
    key = what.strip().lower()
    if not key:
        raise ToolError("Say what to play: a playlist, album, artist or song.")
    lists = await music().playlists()
    if kind in (None, "playlist"):
        exact = [p for p in lists if p.lower() == key]
        if exact:
            return {"kind": "playlist", "name": exact[0]}
        if kind == "playlist":
            partial = [p for p in lists if key in p.lower()]
            if partial:
                return {"kind": "playlist", "name": partial[0]}
            raise ToolError(f"No playlist called '{what}'. Playlists: {', '.join(lists[:20])}.")
    hits = await music().search(what, kind if kind in ("album", "artist", "song") else None)
    fields = {"artist": "artist", "album": "album", "song": "name"}
    for k in ([kind] if kind else ["artist", "album", "song"]):
        match = [h for h in hits if h[fields[k]].lower() == key]
        if match:
            return {"kind": k, "name": match[0][fields[k]]}
    if kind:
        raise ToolError(f"No {kind} called '{what}' in your library.")
    partial = [p for p in lists if key in p.lower()]
    if partial:
        return {"kind": "playlist", "name": partial[0]}
    if hits:
        return {"kind": "song", "name": hits[0]["name"]}
    raise ToolError(f"Nothing called '{what}' in your library (playlists, albums, artists or songs).")


@mcp.tool(annotations=WRITE)
async def music_play(
    what: Annotated[str, Field(description="A playlist, album, artist or song from his library")],
    rooms: Rooms,
    shuffle: Annotated[bool | None, Field(description="Shuffle on/off; omit to leave as is")] = None,
    kind: Annotated[Literal["playlist", "album", "artist", "song"] | None,
                    Field(description="Only if the name is ambiguous")] = None,
) -> str:
    """Play something from his Apple Music library in one or more rooms (the server Mac plays it over AirPlay)."""
    speakers = resolve(rooms, await _music_speakers())
    target = await _find(what, kind)
    await music().play(target, [s.id for s in speakers], shuffle)
    return f"Playing {target['kind']} {target['name']} in {', '.join(_names(speakers))}" + \
        (", shuffled" if shuffle else "")


async def _control(action: str, room: str | None, past: str) -> str:
    """Send to this Mac's Music what this Mac is playing, and to the HomePods what they're playing
    themselves (from his phone or Siri). Music keeps speakers ticked after a pause, so the server Mac only
    counts as playing there while it really is (or is paused, for resume)."""
    outs, _ = await _mac_outputs(("playing", "paused") if action == "resume" else ("playing",))
    mac_keys = {_key(o["name"], o["kind"]) for o in outs}
    speakers = await _speakers()
    if room:
        targets = resolve(room, speakers)
        use_mac = [t for t in targets if _key(t.name, t.kind) in mac_keys]
        others = [t.name for t in targets if t not in use_mac]
    else:
        use_mac = outs
        homepods = [sp.name for sp in speakers if sp.kind == "homepod" and _key(sp.name, sp.kind) not in mac_keys]
        wanted = ("paused",) if action == "resume" else ("playing",)
        _, _, states = await _gather(homepods, pods().now_playing)
        others = [n for n, r in states.items() if not isinstance(r, Exception) and r["state"] in wanted]
    parts = []
    if use_mac:
        await music().control(action)
        parts.append(f"{HOST}'s music in {', '.join(o['name'] for o in outs)}")
    done, failed, _ = await _gather(others, lambda n: pods().control(n, action))
    if done:
        parts.append(", ".join(done))
    if not parts and not failed:
        return "Nothing is playing."
    return f"{past} {'; '.join(parts) or 'nothing'}" + (f" ({', '.join(failed)} didn't respond)" if failed else "")


@mcp.tool(annotations=WRITE)
async def music_pause(room: Room = None) -> str:
    """Pause music (the server Mac's, or whatever the HomePods are playing)."""
    return await _control("pause", room, "Paused")


@mcp.tool(annotations=WRITE)
async def music_resume(room: Room = None) -> str:
    """Resume paused music."""
    return await _control("resume", room, "Resumed")


@mcp.tool(annotations=WRITE)
async def music_next(room: Room = None) -> str:
    """Skip to the next song."""
    return await _control("next", room, "Skipped")


@mcp.tool(annotations=WRITE)
async def music_previous(room: Room = None) -> str:
    """Go back a song."""
    return await _control("previous", room, "Went back")


@mcp.tool(annotations=WRITE)
async def music_set_volume(
    room: Annotated[str, Field(description="Room, or 'everywhere'")],
    level: Annotated[int | None, Field(ge=0, le=100, description="Volume 0–100")] = None,
    change: Annotated[int | None, Field(ge=-100, le=100, description="Relative change, e.g. -10 'a bit quieter'")] = None,
) -> str:
    """Set a room's volume, absolute or relative."""
    if (level is None) == (change is None):
        raise ToolError("Give a level (0–100) or a change (e.g. -10), not both.")
    speakers = resolve(room, await _speakers())
    outs, _ = await _mac_outputs()
    by_key = {_key(o["name"], o["kind"]): o for o in outs}
    music_volumes = {d["id"]: d["volume"] for d in await music().airplay_devices()} if by_key else {}
    done, failed, finals = [], [], []

    def target(current):
        return max(0, min(100, level if level is not None else current + change))

    seen = set()
    for sp in speakers:
        o = by_key.get(_key(sp.name, sp.kind))
        if not o or o["id"] in seen:
            continue
        seen.add(o["id"])
        value = int(target(music_volumes.get(o["id"], 50)))
        try:
            await music().set_device_volume(o["id"], value)
            done.append(o["name"])
            finals.append(value)
        except ToolError:
            failed.append(o["name"])
    on_pods = [sp.name for sp in speakers if _key(sp.name, sp.kind) not in by_key]

    async def one(name):
        value = float(target(await pods().volume(name)))
        await pods().set_volume(name, value)
        return value

    d2, f2, results = await _gather(on_pods, one)
    done += d2
    failed += f2
    finals += [results[n] for n in d2]
    shown = ", ".join(sorted({f"{v:g}" for v in finals})) or "?"
    if len(done) == 1 and not failed:
        return f"{done[0]}: volume {shown}"
    return f"Volume {shown} in {', '.join(done) or 'nothing'}" + (f" ({', '.join(failed)} didn't respond)" if failed else "")


@mcp.tool(annotations=WRITE)
async def music_move(rooms: Rooms) -> str:
    """Move the server Mac's music to other rooms (it stops in the rest)."""
    if not (await _mac_outputs())[0]:
        raise ToolError(f"{HOST} isn't playing anything to move. Start something with music_play.")
    speakers = resolve(rooms, await _music_speakers())
    await music().set_devices([s.id for s in speakers])
    return f"Moved the music to {', '.join(_names(speakers))}"


@mcp.tool(annotations=READ)
async def music_list_playlists() -> str:
    """His Apple Music playlists."""
    return "\n".join(await music().playlists()) or f"No playlists (is Music on {HOST} signed in with Sync Library on?)."


@mcp.tool(annotations=READ)
async def music_search_library(
    query: str,
    kind: Annotated[Literal["album", "artist", "song"] | None, Field(description="Narrow the search")] = None,
) -> str:
    """Search his Apple Music library."""
    hits = await music().search(query, kind)
    return "\n".join(f"{h['name']} — {h['artist']} ({h['album']})" for h in hits) or f"Nothing matching '{query}'."
