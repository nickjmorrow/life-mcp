"""His Apple Music library on the server Mac, beyond playback: listening stats, favorites, playlists.

Same rules as music_app.py: one small JXA script per operation, user text only as the JSON argv
argument. Songs are named by Music's persistent id (16 hex digits), which stays put across renames
and is what every write takes. Only plain user playlists are edited: never smart playlists,
folders, Music's own lists, or the "Claude Queue" that music_play refills.
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from music_app import QUEUE, MusicError, run_jxa

_PRELUDE = 'function run(argv) { const a = JSON.parse(argv[0]); const M = Application("Music"); const L = M.libraryPlaylists[0]; '

# One Apple event per property for the whole library (well under a second for a ~1,000-song library).
SONGS = _PRELUDE + '''
  const T = L.tracks;
  const f = {id: T.persistentID(), name: T.name(), artist: T.artist(), album: T.album(), genre: T.genre(),
             plays: T.playedCount(), played: T.playedDate(), skips: T.skippedCount(), skipped: T.skippedDate(),
             favorite: T.favorited(), disliked: T.disliked(), added: T.dateAdded(), seconds: T.duration()};
  return JSON.stringify(f.id.map((_, i) => Object.fromEntries(Object.keys(f).map(k => [k, f[k][i]]))));
}'''

PLAYLISTS = _PRELUDE + '''
  const P = M.userPlaylists;
  const f = {id: P.persistentID(), name: P.name(), smart: P.smart(), kind: P.specialKind(),
             description: P.description()};
  const counts = P().map(p => p.tracks.length);
  return JSON.stringify(f.id.map((id, i) => ({id, name: f.name[i], smart: f.smart[i], kind: String(f.kind[i]),
                                              description: f.description[i], count: counts[i]})));
}'''

PLAYLIST_SONGS = _PRELUDE + '''
  const p = M.userPlaylists.whose({persistentID: a.playlist})();
  if (!p.length) throw new Error("That playlist no longer exists");
  const T = p[0].tracks;
  if (!T.length) return "[]";
  const ids = T.persistentID(), names = T.name(), artists = T.artist(), albums = T.album();
  return JSON.stringify(ids.map((id, i) => ({id, name: names[i], artist: artists[i], album: albums[i]})));
}'''

# Shared by every write: find songs by id in the library, and an editable playlist by id.
_HELPERS = '''
  function song(id) {
    const t = L.tracks.whose({persistentID: id})();
    if (!t.length) throw new Error("No song with id " + id + " in the library");
    return t[0];
  }
  function editable(id) {
    const p = M.userPlaylists.whose({persistentID: id})();
    if (!p.length) throw new Error("That playlist no longer exists");
    const q = p[0];
    if (q.smart() || String(q.specialKind()) !== "none" || q.name() === a.queue)
      throw new Error(q.name() + " can't be edited (it's a smart or built-in playlist)");
    return q;
  }
'''

SET_FLAGS = _PRELUDE + _HELPERS + '''
  const out = [];
  a.songs.forEach(id => {
    const t = song(id);
    if (a.disliked !== null) t.disliked = a.disliked;
    if (a.favorite !== null) t.favorited = a.favorite;
    out.push({id, name: t.name(), artist: t.artist(), favorite: t.favorited(), disliked: t.disliked()});
  });
  return JSON.stringify(out);
}'''

CREATE = _PRELUDE + _HELPERS + '''
  const props = {name: a.name};
  if (a.description) props.description = a.description;
  const p = M.make({new: "userPlaylist", withProperties: props});
  a.songs.forEach(id => song(id).duplicate({to: p}));
  return JSON.stringify({id: p.persistentID(), name: p.name(), count: p.tracks.length});
}'''

ADD = _PRELUDE + _HELPERS + '''
  const p = editable(a.playlist);
  a.songs.forEach(id => song(id).duplicate({to: p}));
  return JSON.stringify({name: p.name(), count: p.tracks.length});
}'''

# Removes every copy of each song from this playlist only; the library keeps the song.
REMOVE = _PRELUDE + _HELPERS + '''
  const p = editable(a.playlist);
  let removed = 0;
  a.songs.forEach(id => {
    const hits = p.tracks.whose({persistentID: id})();
    for (let i = hits.length - 1; i >= 0; i--) { hits[i].delete(); removed++; }
  });
  return JSON.stringify({name: p.name(), removed, count: p.tracks.length});
}'''

RENAME = _PRELUDE + _HELPERS + '''
  const p = editable(a.playlist);
  if (a.name !== null) p.name = a.name;
  if (a.description !== null) p.description = a.description;
  return JSON.stringify({name: p.name(), description: p.description()});
}'''

# Music has no "move track" for playlists. The new order is appended first, then the original
# entries are deleted from the front by index: a timeout or error part-way leaves extra copies,
# never a playlist with songs missing.
REORDER = _PRELUDE + _HELPERS + '''
  const p = editable(a.playlist);
  const before = p.tracks.length;
  if (before !== a.expect) throw new Error("The playlist changed while reordering; nothing was moved");
  a.songs.map(song).forEach(t => t.duplicate({to: p}));
  const ts = p.tracks();
  for (let i = before - 1; i >= 0; i--) ts[i].delete();
  return JSON.stringify({name: p.name(), count: p.tracks.length});
}'''

DELETE = _PRELUDE + _HELPERS + '''
  const p = editable(a.playlist);
  const name = p.name();
  p.delete();
  return JSON.stringify({name});
}'''

# Which plain playlists hold each song (one read of ids per playlist).
MEMBERSHIP = _PRELUDE + '''
  const want = new Set(a.songs);
  const out = {};
  M.userPlaylists().forEach(p => {
    if (String(p.specialKind()) !== "none" || p.name() === a.queue || !p.tracks.length) return;
    const name = p.name();
    p.tracks.persistentID().forEach(id => { if (want.has(id)) (out[id] = out[id] || []).push(name); });
  });
  Object.keys(out).forEach(k => { out[k] = [...new Set(out[k])]; });
  return JSON.stringify(out);
}'''

BACKUPS = Path.home() / "Library" / "Application Support" / "life-mcp" / "music" / "playlist-backups"


def write_timeout(n: int) -> float:
    """Writes are one Apple event per song; give big playlists time instead of dying half-way."""
    return 30 + n * 0.5


def backup(playlist: dict, songs: list[dict], action: str, folder: Path | None = None) -> Path:
    """The playlist's songs, in order, before a remove/reorder/delete, so it can be put back."""
    folder = folder or BACKUPS
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9 _-]", "", playlist["name"])[:60] or "playlist"
    path = folder / f"{stamp} {action} {safe}.json"
    path.write_text(json.dumps({"playlist": playlist, "action": action, "songs": songs}, indent=1))
    os.chmod(path, 0o600)
    return path


HEX_ID = re.compile(r"^[0-9A-Fa-f]{16}$")


def parse_date(value) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def label(s: dict) -> str:
    return f"{s['name']} — {s['artist']}"


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("–", "-").replace("—", "-")).strip().lower()


def resolve_songs(refs: list[str], songs: list[dict]) -> list[dict]:
    """Library songs for each reference: a persistent id, "Name", or "Name — Artist".
    Exact name (and artist) first, then contains; more than one hit is an error that lists them."""
    by_id = {s["id"].upper(): s for s in songs}
    out = []
    for ref in refs:
        ref = ref.strip()
        if HEX_ID.match(ref):
            if ref.upper() not in by_id:
                raise MusicError(f"No song with id {ref} in his library.")
            out.append(by_id[ref.upper()])
            continue
        parts = re.split(r"\s+[-—–]\s+", ref, maxsplit=1)
        tries = [(_clean(ref), "")] + ([(_clean(parts[0]), _clean(parts[1]))] if len(parts) > 1 else [])

        def fits(s, exact, name, artist):
            n, ar = _clean(s["name"]), _clean(s["artist"])
            ok_name = n == name if exact else name in n
            return ok_name and (not artist or (artist == ar if exact else artist in ar))
        hits = []
        for exact in (True, False):          # the whole text as a title, then "Name — Artist"
            for name, artist in tries:
                hits = hits or [s for s in songs if fits(s, exact, name, artist)]
        if not hits:
            raise MusicError(f"No song matching '{ref}' in his library.")
        distinct = {(_clean(s["name"]), _clean(s["artist"])) for s in hits}
        if len(distinct) > 1:
            shown = "; ".join(f"{label(s)} [{s['id']}]" for s in hits[:8])
            raise MusicError(f"'{ref}' matches several songs: {shown}. Say which (name — artist, or the id).")
        out.append(max(hits, key=lambda s: s.get("plays") or 0))  # same song twice: the one he plays
    return out


def recent(songs: list[dict], kind: str, days: int | None = None, limit: int = 20,
           now: datetime | None = None) -> list[dict]:
    """Songs by last played or last skipped, newest first."""
    field = {"played": "played", "skipped": "skipped"}[kind]
    since = (now or datetime.now(timezone.utc)) - timedelta(days=days) if days else None
    rows = [(parse_date(s[field]), s) for s in songs if s.get(field)]
    rows = [r for r in rows if not since or r[0] >= since]
    return [s for _, s in sorted(rows, key=lambda r: r[0], reverse=True)[:limit]]


def top(songs: list[dict], by: str = "song", limit: int = 20, days: int | None = None,
        now: datetime | None = None) -> list[tuple[str, int, int]]:
    """(name, plays, songs) by all-time play count; with days, only songs played in that window."""
    if days:
        since = (now or datetime.now(timezone.utc)) - timedelta(days=days)
        songs = [s for s in songs if s.get("played") and parse_date(s["played"]) >= since]
    if by == "song":
        rows = sorted(songs, key=lambda s: s["plays"] or 0, reverse=True)
        return [(label(s), s["plays"] or 0, 1) for s in rows[:limit] if s["plays"]]
    key = {"artist": "artist", "album": "album", "genre": "genre"}[by]
    totals: dict[str, list[int]] = {}
    for s in songs:
        name = s[key] or "(none)"
        t = totals.setdefault(name, [0, 0])
        t[0] += s["plays"] or 0
        t[1] += 1
    rows = sorted(totals.items(), key=lambda kv: kv[1][0], reverse=True)
    return [(name, plays, n) for name, (plays, n) in rows[:limit] if plays]


class MusicLibrary:
    async def songs(self) -> list[dict]:
        return await run_jxa(SONGS)

    async def playlists(self) -> list[dict]:
        """Plain playlists (his own, plus smart ones marked), without folders and Music's built-ins."""
        rows = await run_jxa(PLAYLISTS)
        return [p for p in rows if p["kind"] == "none" and p["name"] != QUEUE]

    async def playlist_songs(self, playlist_id: str) -> list[dict]:
        return await run_jxa(PLAYLIST_SONGS, {"playlist": playlist_id})

    async def set_flags(self, ids: list[str], favorite: bool | None, disliked: bool | None) -> list[dict]:
        return await run_jxa(SET_FLAGS, {"songs": ids, "favorite": favorite, "disliked": disliked},
                             timeout=write_timeout(len(ids)))

    async def create(self, name: str, ids: list[str], description: str | None = None) -> dict:
        return await run_jxa(CREATE, {"name": name, "songs": ids, "description": description},
                             timeout=write_timeout(len(ids)))

    async def add(self, playlist_id: str, ids: list[str]) -> dict:
        return await run_jxa(ADD, {"playlist": playlist_id, "songs": ids, "queue": QUEUE},
                             timeout=write_timeout(len(ids)))

    async def remove(self, playlist_id: str, ids: list[str]) -> dict:
        return await run_jxa(REMOVE, {"playlist": playlist_id, "songs": ids, "queue": QUEUE},
                             timeout=write_timeout(len(ids)))

    async def rename(self, playlist_id: str, name: str | None, description: str | None) -> dict:
        return await run_jxa(RENAME, {"playlist": playlist_id, "name": name, "description": description,
                                      "queue": QUEUE})

    async def reorder(self, playlist_id: str, ids: list[str], expect: int) -> dict:
        """ids = the whole new order; expect = how many songs the playlist has now."""
        return await run_jxa(REORDER, {"playlist": playlist_id, "songs": ids, "expect": expect, "queue": QUEUE},
                             timeout=write_timeout(2 * len(ids)))

    async def delete(self, playlist_id: str) -> dict:
        return await run_jxa(DELETE, {"playlist": playlist_id, "queue": QUEUE})

    async def membership(self, ids: list[str]) -> dict[str, list[str]]:
        return await run_jxa(MEMBERSHIP, {"songs": ids, "queue": QUEUE})
