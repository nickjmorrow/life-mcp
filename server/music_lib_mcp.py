"""Music tools beyond playback: what he listens to, favorites, playlists, and the Apple Music catalog.

Library work goes through Music.app on the server Mac (music_library.py); anything outside his library
goes through the Apple Music API (applemusic_api.py). Songs are named as "Name", "Name — Artist",
a library id (16 hex digits) or a catalog id (all digits); outputs show ids in [brackets] so
Claude can pass them on. If the API isn't set up or signed in, the library tools still work.
"""
import asyncio
import random
import re
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import Field

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from applemusic_api import AppleMusic, AppleMusicError
from music_app import QUEUE
from music_library import HEX_ID, MusicLibrary, backup, label, parse_date, recent, resolve_songs, top
import music_mcp
from host import NAME as HOST

mcp = FastMCP("Music library")
READ = {"readOnlyHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": False}
DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True}

SYNC_WAIT_S = 25   # how long (claude.ai gives a tool call about a minute) to wait for something added through the API to reach the server Mac's library
SYNC_POLL_S = 5

_lib: MusicLibrary | None = None
_api: AppleMusic | None = None


def lib() -> MusicLibrary:
    global _lib
    _lib = _lib or MusicLibrary()
    return _lib


def api() -> AppleMusic:
    global _api
    _api = _api or AppleMusic()
    return _api


Songs = Annotated[list[str] | None, Field(description=(
    "Songs: 'Name', 'Name — Artist', a library id (16 hex) or a catalog id (8+ digits, from catalog search)"))]
LastSkipped = Annotated[int | None, Field(ge=1, le=50, description="Use the N most recently skipped songs")]
Playlist = Annotated[str, Field(description="Playlist name (exact or a unique part of it) or its id")]
ExactPlaylist = Annotated[str, Field(description="The playlist's exact name or its id")]
CATALOG_ID = re.compile(r"^\d{8,}$")   # short all-digit refs are song titles ("1979", "22")


def _when(value) -> str:
    d = parse_date(value)
    if not d:
        return "never"
    d = d.astimezone()
    return d.strftime("%b %-d %-I:%M %p" if d.year == datetime.now().year else "%b %-d %Y")


def _line(s: dict, extra: str = "") -> str:
    flags = (" ♥" if s.get("favorite") else "") + (" (disliked)" if s.get("disliked") else "")
    return f"{label(s)}{flags} [{s['id']}]" + (f" — {extra}" if extra else "")


def _cat_line(c: dict, in_library: set | None = None) -> str:
    who = c.get("artist") or c.get("curator") or ""
    bits = [b for b in (c.get("album") if c["type"] == "songs" else None, c.get("year"), c.get("genre")) if b]
    mine = " (in his library)" if in_library is not None and _key(c.get("name", ""), who) in in_library else ""
    return f"{c['name']}{' — ' + who if who else ''}{' (' + ', '.join(bits) + ')' if bits else ''}{mine} [{c['id']}]"


def _key(name: str, artist: str) -> tuple[str, str]:
    clean = lambda t: re.sub(r"\s*[\(\[].*?[\)\]]", "", t or "").strip().lower()
    return clean(name), clean(artist)


def _split(refs: list[str] | None) -> tuple[list[str], list[str]]:
    """(library refs, catalog ids)."""
    refs = [r.strip() for r in (refs or []) if r and r.strip()]
    return [r for r in refs if not CATALOG_ID.match(r)], [r for r in refs if CATALOG_ID.match(r)]


async def _pick(songs: list[str] | None, last_skipped: int | None) -> tuple[list[dict], list[str], list[dict]]:
    """(library songs, catalog ids, all library songs) from names/ids and/or the last N skipped."""
    names, catalog = _split(songs)
    if not names and not catalog and not last_skipped:
        raise ToolError("Say which songs (names or ids), or last_skipped=N.")
    everything = await lib().songs()
    chosen = resolve_songs(names, everything) if names else []
    if last_skipped:
        chosen += recent(everything, "skipped", limit=last_skipped)
    seen, unique = set(), []
    for s in chosen:
        if s["id"] not in seen:
            seen.add(s["id"])
            unique.append(s)
    return unique, catalog, everything


async def _playlist(name: str, exact_only: bool = False) -> dict:
    """A playlist by id, exact name, or (unless exact_only) a unique part of its name."""
    lists = await lib().playlists()
    key = name.strip().lower()
    by_id = [p for p in lists if HEX_ID.match(name.strip()) and p["id"].upper() == name.strip().upper()]
    if by_id:
        return by_id[0]
    exact = [p for p in lists if p["name"].lower() == key]
    if len(exact) > 1:
        raise ToolError(f"He has {len(exact)} playlists called '{name}': "
                        + ", ".join(f"[{p['id']}] {p['count']} songs" for p in exact) + ". Use the id.")
    if exact:
        return exact[0]
    partial = [] if exact_only else [p for p in lists if key in p["name"].lower()]
    if len(partial) == 1:
        return partial[0]
    if exact_only:
        close = [p["name"] for p in lists if key in p["name"].lower()] or [p["name"] for p in lists]
        raise ToolError(f"No playlist called exactly '{name}'. Playlists: {', '.join(close[:25])}.")
    shown = ", ".join(p["name"] for p in (partial or lists)[:25])
    raise ToolError(f"{'Several' if partial else 'No'} playlists match '{name}'. Playlists: {shown}.")


def _editable(p: dict) -> None:
    if p["smart"]:
        raise ToolError(f"'{p['name']}' is a smart playlist; Music fills it by its rules, so it can't be edited.")


async def _api_playlist_id(name: str, wait: bool = True) -> str:
    """The API's id for his library playlist with this name (a new one can take a moment to sync)."""
    deadline = asyncio.get_running_loop().time() + (SYNC_WAIT_S if wait else 0)
    while True:
        hits = [p for p in await api().library_playlists() if p["name"].lower() == name.lower()]
        if len(hits) > 1:
            raise ToolError(f"Apple Music sees {len(hits)} playlists called '{name}'; rename one first.")
        if hits and not hits[0].get("editable", True):
            raise ToolError(f"Apple Music won't let apps add catalog songs to '{name}'. Add them to his library "
                            "first (music_add_to_library), then add them by name.")
        if hits:
            return hits[0]["id"]
        if asyncio.get_running_loop().time() >= deadline:
            raise ToolError(f"Apple Music hasn't synced the playlist '{name}' yet; try adding catalog songs again in a minute.")
        await asyncio.sleep(SYNC_POLL_S)


async def _add_catalog_to_playlist(name: str, ids: list[str]) -> str:
    if not ids:
        return ""
    await api().add_to_playlist(await _api_playlist_id(name), ids)
    found = await api().songs(ids)
    return "; added from Apple Music: " + ", ".join(f"{c['name']} — {c.get('artist', '')}" for c in found)


# --- listening ------------------------------------------------------------------------------------

@mcp.tool(annotations=READ)
async def music_recent(
    kind: Annotated[Literal["played", "skipped"], Field(description="Recently played or recently skipped")] = "played",
    days: Annotated[int | None, Field(ge=1, le=3650, description="Only the last N days")] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 15,
) -> str:
    """Songs from his library he played or skipped most recently, on any device (HomePods, phone, Mac),
    newest first, with play/skip counts and favorite (♥)."""
    rows = recent(await lib().songs(), kind, days, limit)
    field = "played" if kind == "played" else "skipped"
    return "\n".join(f"{_when(s[field])}: {_line(s, f'plays {s['plays']}, skips {s['skips']}')}" for s in rows) \
        or f"Nothing {kind} {'in that window' if days else 'yet'}."


@mcp.tool(annotations=READ)
async def music_top(
    by: Annotated[Literal["song", "artist", "album", "genre"], Field(description="What to rank")] = "song",
    limit: Annotated[int, Field(ge=1, le=100)] = 15,
    days: Annotated[int | None, Field(ge=1, le=3650, description=(
        "Only songs played in the last N days (ranked by their all-time plays; Music keeps no per-day counts)"))] = None,
) -> str:
    """His most played songs, artists, albums or genres (all-time play counts from his library)."""
    rows = top(await lib().songs(), by, limit, days)
    if by == "song":
        return "\n".join(f"{i}. {n} — {p} plays" for i, (n, p, _) in enumerate(rows, 1)) or "No plays yet."
    return "\n".join(f"{i}. {n} — {p} plays across {c} song{'s' * (c != 1)}"
                     for i, (n, p, c) in enumerate(rows, 1)) or "No plays yet."


@mcp.tool(annotations=READ)
async def music_taste() -> str:
    """A profile of his taste to recommend from: top artists and genres, what he plays lately, newest
    favorites, what he skips most, and Apple Music heavy rotation. Read this before suggesting music."""
    songs = await lib().songs()
    favorites = [s for s in songs if s["favorite"]]
    lines = [f"Library: {len(songs)} songs, {len(favorites)} favorites, "
             f"{sum(1 for s in songs if s['disliked'])} disliked."]
    lines.append("Top artists (all time): " + ", ".join(f"{n} ({p})" for n, p, _ in top(songs, "artist", 15)))
    lines.append("Top genres (all time): " + ", ".join(f"{n} ({p})" for n, p, _ in top(songs, "genre", 8)))
    lines.append("Artists lately (songs played in the last 30 days): "
                 + ", ".join(n for n, _, _ in top(songs, "artist", 15, days=30)))
    lines.append("Most played lately: " + "; ".join(n for n, _, _ in top(songs, "song", 10, days=30)))
    newest = sorted(favorites, key=lambda s: s["added"] or "", reverse=True)[:12]
    lines.append("Newest favorites: " + "; ".join(label(s) for s in newest))
    skippy = sorted((s for s in songs if (s["skips"] or 0) >= 3),
                    key=lambda s: s["skips"] / max(1, s["plays"] + s["skips"]), reverse=True)[:10]
    lines.append("Skips most (skips/plays): " + "; ".join(f"{label(s)} ({s['skips']}/{s['plays']})" for s in skippy))
    try:
        rotation = await api().heavy_rotation()
        lines.append("Apple Music heavy rotation: " + "; ".join(_cat_line(c) for c in rotation))
    except AppleMusicError as e:
        lines.append(f"(Apple Music history unavailable: {e})")
    return "\n".join(lines)


@mcp.tool(annotations=READ)
async def music_song_info(songs: Songs = None, last_skipped: LastSkipped = None) -> str:
    """Stats for songs in his library: plays, skips, last played/skipped, favorite, date added, and
    which playlists they're in."""
    chosen, _, _ = await _pick(songs, last_skipped)
    where = await lib().membership([s["id"] for s in chosen])
    return "\n".join(
        f"{_line(s)}: {s['album']}, {s['genre']}; plays {s['plays']} (last {_when(s['played'])}), skips {s['skips']} "
        f"(last {_when(s['skipped'])}); added {_when(s['added'])}; playlists: {', '.join(where.get(s['id'], [])) or 'none'}"
        for s in chosen)


@mcp.tool(annotations=READ)
async def music_history(limit: Annotated[int, Field(ge=1, le=100)] = 30) -> str:
    """Apple Music's own record of what he played recently on all his devices (including songs not in
    his library), plus heavy rotation. Needs the Apple Music sign-in."""
    played, rotation = await asyncio.gather(api().recently_played(limit), api().heavy_rotation())
    return ("Recently played:\n" + "\n".join(f"- {_cat_line(c)}" for c in played)
            + "\n\nHeavy rotation:\n" + "\n".join(f"- {_cat_line(c)}" for c in rotation))


# --- favorites ------------------------------------------------------------------------------------

@mcp.tool(annotations=WRITE)
async def music_set_favorite(
    action: Annotated[Literal["favorite", "unfavorite", "dislike", "clear"], Field(description=(
        "favorite; unfavorite (remove from favorites); dislike (Apple plays it less); clear (neither)"))],
    songs: Songs = None,
    last_skipped: LastSkipped = None,
) -> str:
    """Favorite, unfavorite or dislike songs, by name/id or "the last N I skipped". Changes sync to
    his phone through iCloud."""
    chosen, catalog, _ = await _pick(songs, last_skipped)
    favorite, disliked = {"favorite": (True, False), "unfavorite": (False, None),
                          "dislike": (False, True), "clear": (False, False)}[action]
    done, failed = [], []
    if chosen:
        try:
            done += [_line(c) for c in await lib().set_flags([s["id"] for s in chosen], favorite, disliked)]
        except ToolError as e:
            failed.append(f"{', '.join(label(s) for s in chosen)}: {e}")
    if catalog:
        names = {c["id"]: _cat_line(c) for c in await api().songs(catalog)}
        for cid in catalog:
            try:
                if action == "unfavorite":
                    if await api().rating(cid) == 1:      # leave a dislike alone
                        await api().rate(cid, None)
                else:
                    await api().rate(cid, {"favorite": 1, "dislike": -1}.get(action))
                done.append(names.get(cid, cid))
            except ToolError as e:
                failed.append(f"{names.get(cid, cid)}: {e}")
    past = {"favorite": "Favorited", "unfavorite": "Unfavorited", "dislike": "Disliked", "clear": "Cleared"}[action]
    out = (f"{past}:\n" + "\n".join(f"- {o}" for o in done)) if done else f"Nothing {past.lower()}."
    if failed:
        out += "\nNot changed:\n" + "\n".join(f"- {f}" for f in failed)
    return out


# --- playlists ------------------------------------------------------------------------------------

async def _new_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ToolError("Give the playlist a name.")
    if name.lower() == QUEUE.lower():
        raise ToolError(f"'{QUEUE}' is the playlist music_play refills; pick another name.")
    if any(p["name"].lower() == name.lower() for p in await lib().playlists()):
        raise ToolError(f"He already has a playlist called '{name}'. Add to it, or pick another name.")
    return name


@mcp.tool(annotations=READ)
async def music_show_playlist(playlist: Playlist, limit: Annotated[int, Field(ge=1, le=1000)] = 200) -> str:
    """The songs in one of his playlists, in order."""
    p = await _playlist(playlist)
    rows = await lib().playlist_songs(p["id"])
    head = f"{p['name']}{' (smart)' if p['smart'] else ''}: {len(rows)} songs" + \
        (f" — {p['description']}" if p.get("description") else "")
    return head + "\n" + "\n".join(f"{i}. {label(s)} [{s['id']}]" for i, s in enumerate(rows[:limit], 1)) \
        + (f"\n… and {len(rows) - limit} more" if len(rows) > limit else "")


@mcp.tool(annotations=WRITE)
async def music_create_playlist(
    name: Annotated[str, Field(min_length=1, max_length=200)],
    songs: Songs = None,
    last_skipped: LastSkipped = None,
    description: str | None = None,
) -> str:
    """Make a new playlist, optionally with songs from his library and/or the Apple Music catalog."""
    name = await _new_name(name)
    chosen, catalog, _ = await _pick(songs, last_skipped) if (songs or last_skipped) else ([], [], [])
    made = await lib().create(name, [s["id"] for s in chosen], description)
    note = await _add_catalog_to_playlist(made["name"], catalog) if catalog else ""
    return f"Made playlist '{made['name']}' with {made['count']} songs from his library{note}."


@mcp.tool(annotations=WRITE)
async def music_add_to_playlist(playlist: Playlist, songs: Songs = None, last_skipped: LastSkipped = None) -> str:
    """Add songs (from his library or the catalog) to the end of one of his playlists."""
    p = await _playlist(playlist)
    _editable(p)
    chosen, catalog, _ = await _pick(songs, last_skipped)
    parts = []
    if chosen:
        r = await lib().add(p["id"], [s["id"] for s in chosen])
        parts.append(f"Added {', '.join(label(s) for s in chosen)} to '{r['name']}' ({r['count']} songs now)")
    note = await _add_catalog_to_playlist(p["name"], catalog)
    return ("; ".join(parts) + note).lstrip("; ") or "Nothing to add."


@mcp.tool(annotations=WRITE)
async def music_remove_from_playlist(playlist: ExactPlaylist, songs: Songs = None, last_skipped: LastSkipped = None) -> str:
    """Take songs out of a playlist (every copy). The songs stay in his library."""
    p = await _playlist(playlist, exact_only=True)
    _editable(p)
    names, catalog = _split(songs)
    if catalog:
        raise ToolError("Name songs in a playlist by name or library id, not catalog id (see music_show_playlist).")
    if not names and not last_skipped:
        raise ToolError("Say which songs (names or ids), or last_skipped=N.")
    rows = await lib().playlist_songs(p["id"])
    # Match against this playlist's own entries: a song can have two library copies.
    chosen = resolve_songs(names, rows) if names else []
    if last_skipped:
        skipped = recent(await lib().songs(), "skipped", limit=last_skipped)
        keys = {(s["name"].lower(), s["artist"].lower()) for s in skipped} | {s["id"] for s in skipped}
        chosen += [r for r in rows if r["id"] in keys or (r["name"].lower(), r["artist"].lower()) in keys]
    ids = list(dict.fromkeys(s["id"] for s in chosen))
    if not ids:
        return f"None of those are in '{p['name']}'."
    saved = backup(p, rows, "remove")
    r = await lib().remove(p["id"], ids)
    gone = list(dict.fromkeys(label(s) for s in chosen))
    return (f"Removed {r['removed']} from '{r['name']}' ({r['count']} songs now): {', '.join(gone)}. "
            f"Old list saved to {saved.name}.")


@mcp.tool(annotations=WRITE)
async def music_rename_playlist(
    playlist: ExactPlaylist,
    new_name: Annotated[str | None, Field(max_length=200)] = None,
    description: str | None = None,
) -> str:
    """Rename a playlist and/or change its description."""
    if not new_name and description is None:
        raise ToolError("Give a new name, a description, or both.")
    p = await _playlist(playlist, exact_only=True)
    _editable(p)
    if new_name and new_name.strip().lower() != p["name"].lower():
        new_name = await _new_name(new_name)
    r = await lib().rename(p["id"], new_name.strip() if new_name else None, description)
    return f"'{p['name']}' is now '{r['name']}'" + (f" — {r['description']}" if r.get("description") else "")


@mcp.tool(annotations=WRITE)
async def music_reorder_playlist(
    playlist: ExactPlaylist,
    by: Annotated[Literal["plays", "newest", "artist", "name", "shuffle", "reverse", "custom"], Field(description=(
        "plays (most played first), newest (most recently added to his library first), artist, name, "
        "shuffle, reverse, or custom (songs listed in `order` first, the rest after)"))],
    order: Songs = None,
) -> str:
    """Reorder a playlist (Music has no move, so it's emptied and refilled in the new order)."""
    p = await _playlist(playlist, exact_only=True)
    _editable(p)
    rows = await lib().playlist_songs(p["id"])
    if not rows:
        return f"'{p['name']}' is empty."
    stats = {s["id"]: s for s in await lib().songs()}
    full = [stats.get(r["id"], {**r, "plays": 0, "added": ""}) for r in rows]
    if by == "plays":
        full.sort(key=lambda s: s["plays"] or 0, reverse=True)
    elif by == "newest":
        full.sort(key=lambda s: s["added"] or "", reverse=True)
    elif by in ("artist", "name"):
        full.sort(key=lambda s: (s[by].lower(), s["name"].lower()))
    elif by == "shuffle":
        random.shuffle(full)
    elif by == "reverse":
        full.reverse()
    else:
        if not order:
            raise ToolError("For a custom order, list the songs in `order`.")
        first = resolve_songs(order, full)
        rest = list(full)
        for s in first:                      # take one copy each; repeats stay in the rest
            rest.remove(next(x for x in rest if x["id"] == s["id"]))
        full = first + rest
    saved = backup(p, rows, "reorder")
    r = await lib().reorder(p["id"], [s["id"] for s in full], len(rows))
    return f"Reordered '{r['name']}' ({r['count']} songs) by {by}. Starts: " + \
        "; ".join(label(s) for s in full[:5]) + f". Old order saved to {saved.name}."


@mcp.tool(annotations=DESTRUCTIVE)
async def music_delete_playlist(
    playlist: ExactPlaylist,
    confirm: Annotated[bool, Field(description="True only after he has said yes to deleting this playlist")] = False,
) -> str:
    """Delete one of his playlists (the songs stay in his library). Ask him first, then call with confirm=true."""
    p = await _playlist(playlist, exact_only=True)
    _editable(p)
    if not confirm:
        return f"Not deleted. '{p['name']}' has {p['count']} songs; ask him, then call again with confirm=true."
    saved = backup(p, await lib().playlist_songs(p["id"]), "delete")
    r = await lib().delete(p["id"])
    return f"Deleted the playlist '{r['name']}' ({p['count']} songs; they're still in his library). " \
           f"Its song list is saved in {saved.name}."


# --- discovery (Apple Music catalog) ----------------------------------------------------------------

async def _library_keys() -> set:
    try:
        return {_key(s["name"], s["artist"]) for s in await lib().songs()}
    except ToolError:
        return set()


@mcp.tool(annotations=READ)
async def music_search_catalog(
    query: str,
    types: Annotated[list[Literal["songs", "albums", "artists", "playlists"]] | None, Field(
        description="What to look for; default all four")] = None,
    limit: Annotated[int, Field(ge=1, le=25)] = 8,
) -> str:
    """Search all of Apple Music (not just his library). Songs already in his library are marked.
    Pass the [ids] to music_add_to_playlist, music_add_to_library, music_play_catalog or music_artist."""
    types = types or ["songs", "albums", "artists", "playlists"]
    res, mine = await asyncio.gather(api().search(query, types, limit), _library_keys())
    parts = [f"{t.capitalize()}:\n" + "\n".join(f"- {_cat_line(c, mine if t == 'songs' else None)}" for c in res[t])
             for t in types if res.get(t)]
    return "\n\n".join(parts) or f"Nothing on Apple Music for '{query}'."


@mcp.tool(annotations=READ)
async def music_recommendations(limit: Annotated[int, Field(ge=1, le=30)] = 10) -> str:
    """Apple Music's personal recommendations for him (Made for You mixes, albums, playlists, stations)."""
    groups = await api().recommendations(limit)
    return "\n\n".join(f"{g['title']}:\n" + "\n".join(f"- {_cat_line(c)} ({c['type'].rstrip('s')})" for c in g["items"])
                       for g in groups) or "No recommendations right now."


@mcp.tool(annotations=READ)
async def music_artist(
    artist: Annotated[str, Field(description="Artist name or catalog artist id")],
) -> str:
    """An artist on Apple Music: top songs, latest release, albums and similar artists. Good for finding
    more music like what he loves."""
    aid = artist.strip() if artist.strip().isdigit() else (await api().find_artist(artist))["id"]
    a = await api().artist(aid)
    out = [f"{a['artist']['name']} [{aid}]" + (f" — {a['artist']['genre']}" if a['artist'].get('genre') else "")]
    mine = await _library_keys()
    for key, title in (("top-songs", "Top songs"), ("latest-release", "Latest release"),
                       ("full-albums", "Albums"), ("similar-artists", "Similar artists")):
        if a.get(key):
            out.append(f"{title}:\n" + "\n".join(f"- {_cat_line(c, mine if key == 'top-songs' else None)}"
                                                 for c in a[key][:12]))
    return "\n\n".join(out)


@mcp.tool(annotations=READ)
async def music_charts(
    types: Annotated[list[Literal["songs", "albums", "playlists"]] | None, Field(description="Default songs")] = None,
    genre: Annotated[str | None, Field(description="A genre name like 'Hip-Hop/Rap', 'Dance', 'Pop'")] = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 20,
) -> str:
    """Apple Music's current top charts, overall or for a genre."""
    types = types or ["songs"]
    gid = None
    if genre:
        genres = await api().genres()
        match = [g for g in genres if g["name"].lower() == genre.lower()] or \
                [g for g in genres if genre.lower() in g["name"].lower()]
        if not match:
            raise ToolError(f"No genre '{genre}'. Genres: {', '.join(g['name'] for g in genres[:40])}.")
        gid = match[0]["id"]
    res = await api().charts(types, gid, limit)
    return "\n\n".join(f"Top {t}{' in ' + genre if genre else ''}:\n"
                       + "\n".join(f"{i}. {_cat_line(c)}" for i, c in enumerate(res[t], 1)) for t in types if res.get(t))


@mcp.tool(annotations=WRITE)
async def music_add_to_library(
    songs: Annotated[list[str] | None, Field(description="Catalog song ids")] = None,
    albums: Annotated[list[str] | None, Field(description="Catalog album ids")] = None,
    playlists: Annotated[list[str] | None, Field(description="Catalog playlist ids (pl.…)")] = None,
) -> str:
    """Add Apple Music catalog songs, albums or playlists to his library."""
    if not (songs or albums or playlists):
        raise ToolError("Give catalog ids from music_search_catalog.")
    await api().add_to_library(songs or [], albums or [], playlists or [])
    named = [f"{c['name']} — {c.get('artist', '')}" for c in await api().songs(songs or [])]
    return "Added to his library: " + "; ".join(named + [f"album {a}" for a in albums or []]
                                                 + [f"playlist {p}" for p in playlists or []])


@mcp.tool(annotations=WRITE)
async def music_play_catalog(
    song_or_album: Annotated[str, Field(description="Catalog song or album id (from music_search_catalog)")],
    rooms: music_mcp.Rooms,
    kind: Literal["song", "album"] = "song",
) -> str:
    """Play something from Apple Music that isn't in his library yet: it's added to his library, then
    played in the rooms once it reaches this Mac (usually seconds; up to a minute)."""
    cid = song_or_album.strip()
    if not CATALOG_ID.match(cid):
        raise ToolError("Give a catalog song or album id (digits) from music_search_catalog.")
    wanted = await api().songs([cid]) if kind == "song" else await api().album_songs(cid)
    if not wanted:
        raise ToolError(f"Apple Music has no {kind} {cid}" + (" (is it an album? use kind=album)" if kind == "song" else "") + ".")
    await api().add_to_library(**({"songs": [cid]} if kind == "song" else {"albums": [cid]}))
    keys = [(*_key(c["name"], c.get("artist", "")), (c.get("album") or "").lower()) for c in wanted]
    deadline = asyncio.get_running_loop().time() + SYNC_WAIT_S
    while True:
        newest = {}
        for s in await lib().songs():       # the same song on another album is a different version
            k = (*_key(s["name"], s["artist"]), (s["album"] or "").lower())
            if k not in newest or (s["added"] or "") > (newest[k]["added"] or ""):
                newest[k] = s
        ids = [newest[k]["id"] for k in keys if k in newest]
        if len(ids) == len(keys) or asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(SYNC_POLL_S)
    title = wanted[0]["name"] if kind == "song" else wanted[0].get("album", "the album")
    if not ids:
        return f"Added {title} to his library, but it hasn't reached {HOST} yet; try music_play in a minute."
    speakers = music_mcp.resolve(rooms, await music_mcp._music_speakers())
    await music_mcp.music().play({"kind": "ids", "ids": ids, "name": title}, [s.id for s in speakers], None)
    missing = len(keys) - len(ids)
    return f"Added {title} to his library and playing it ({len(ids)} song{'s' * (len(ids) != 1)}) in " \
           f"{', '.join(s.name for s in speakers)}" + \
           (f"; {missing} more hadn't synced to {HOST} yet" if missing else "")


@mcp.tool(annotations=READ)
async def music_status() -> str:
    """Whether the Apple Music API is set up and signed in, and the library's size."""
    a = api()
    lines = [f"Library on {HOST}: {len(await lib().songs())} songs, {len(await lib().playlists())} playlists."]
    if not a.configured():
        lines.append("Apple Music API: not set up (no MusicKit key); catalog tools won't work.")
        return "\n".join(lines)
    u = a.user()
    if not u:
        lines.append(f"Apple Music API: key set up, but not signed in (run applemusic_signin.py on {HOST}).")
        return "\n".join(lines)
    try:
        age = f"{(datetime.now(timezone.utc) - datetime.fromisoformat(u['saved'])).days} days ago"
    except (KeyError, TypeError, ValueError):
        age = "at an unknown time"
    try:
        sf = await a.storefront()
        lines.append(f"Apple Music API: signed in {age} (lasts about 6 months), storefront {sf}.")
    except AppleMusicError as e:
        lines.append(f"Apple Music API: {e}")
    return "\n".join(lines)
