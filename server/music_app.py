"""Music.app on the server Mac, scripted with osascript (JavaScript for Automation).

Each operation is one small JXA script. User text only ever arrives as a JSON argv
argument, never in the script source. "Claude Queue" is the one playlist this code
fills and replays for album/artist/song requests; nothing else in the library changes.
"""
import asyncio
import json

from fastmcp.exceptions import ToolError
from host import NAME as HOST

OSASCRIPT = "/usr/bin/osascript"
TIMEOUT_S = 30
QUEUE = "Claude Queue"
# The queue playlist is recognised by this exact description (the play script below): changing the text
# would orphan the existing "Claude Queue" playlist and make a second one, so it keeps its original wording.
QUEUE_MARKER = "Made by Claude; its tracks are replaced each time."
QUEUE_CAP = 100  # tracks; filling is one Apple event per track


class MusicError(ToolError):
    """A Music problem to tell Claude about in plain words."""


async def _osascript(args: list[str], timeout: float = TIMEOUT_S) -> str:
    proc = await asyncio.create_subprocess_exec(
        OSASCRIPT, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        raise MusicError(f"Music on {HOST} didn't answer in time.")
    if proc.returncode:
        message = err.decode().strip()
        if "-1743" in message or "Not authorized" in message:
            raise MusicError(f"{HOST} isn't allowed to control Music yet: System Settings → Privacy & Security → "
                             "Automation → allow Music for the Life connector.")
        message = message.split("Error: ", 1)[-1].rsplit(" (-", 1)[0]
        raise MusicError(f"Music: {message[:200]}")
    return out.decode().strip()


async def run_jxa(script: str, arg: dict | None = None, timeout: float = TIMEOUT_S):
    """JavaScript for Automation; the one JSON argument is the only way user text gets in."""
    text = await _osascript(["-l", "JavaScript", "-e", script, json.dumps(arg or {})], timeout)
    return json.loads(text) if text else None


async def run_applescript(script: str, *args: str) -> str:
    """AppleScript (Music's exact dictionary words); user text only as argv."""
    return await _osascript(["-e", script, *args])


# AirPlay goes through AppleScript: JXA's element name for "AirPlay devices" isn't understood
# by Music (seen live 2026-09-27). Speakers are ticked one at a time ("selected"), like clicking
# in Music: setting "current AirPlay devices" to a list failed with -15014, and reading it back
# came out empty. Ids, because names repeat ("Living Room" = the Apple TV and the HomePod pair).
AS_AIRPLAY = """on run argv
  tell application "Music"
    set out to {}
    repeat with d in AirPlay devices
      set end of out to (id of d as text) & tab & (name of d) & tab & (kind of d as text) & tab & ¬
        (selected of d as text) & tab & (active of d as text) & tab & (available of d as text) & tab & ¬
        (sound volume of d as text)
    end repeat
  end tell
  set AppleScript's text item delimiters to linefeed
  return out as text
end run"""

AS_SET_DEVICES = """on run argv
  tell application "Music"
    set wanted to {}
    repeat with n in argv
      set end of wanted to (n as integer)
    end repeat
    repeat with i in wanted
      set selected of (first AirPlay device whose id is (i as integer)) to true
    end repeat
    repeat with d in AirPlay devices
      if wanted does not contain (id of d) then set selected of d to false
    end repeat
    return "ok"
  end tell
end run"""

AS_SET_VOLUME = """on run argv
  tell application "Music"
    set d to first AirPlay device whose id is (item 1 of argv as integer)
    set sound volume of d to (item 2 of argv as integer)
    return sound volume of d
  end tell
end run"""


_PRELUDE = 'function run(argv) { const a = JSON.parse(argv[0]); const M = Application("Music"); '

STATE = _PRELUDE + '''
  const s = String(M.playerState());
  let track = null;
  try { if (s !== "stopped") { const t = M.currentTrack; track = {name: t.name(), artist: t.artist(), album: t.album()}; } } catch (e) {}
  return JSON.stringify({state: s, track, shuffle: M.shuffleEnabled()});
}'''

PLAYLISTS = _PRELUDE + '''
  return JSON.stringify(M.userPlaylists().map(p => p.name()).filter(n => n !== a.queue));
}'''

SEARCH = _PRELUDE + '''
  const only = {album: "albums", artist: "artists", song: "songs"}[a.kind] || "all";
  const hits = M.search(M.libraryPlaylists[0], {for: a.query, only});
  return JSON.stringify(hits.slice(0, a.limit).map(t => ({name: t.name(), artist: t.artist(), album: t.album()})));
}'''

PLAY = _PRELUDE + '''
  if (a.shuffle !== null) M.shuffleEnabled = a.shuffle;
  const t = a.target;
  if (t.kind === "playlist") {
    const p = M.userPlaylists.whose({name: t.name})();
    if (!p.length) throw new Error("No playlist called " + t.name);
    p[0].play();
    return JSON.stringify("playlist " + t.name);
  }
  const lib = M.libraryPlaylists[0];
  const only = {album: "albums", artist: "artists", song: "songs"}[t.kind];
  let tracks = t.kind === "ids"   // exact songs, in this order (persistent ids)
    ? t.ids.map(id => lib.tracks.whose({persistentID: id})()).filter(x => x.length).map(x => x[0])
    : M.search(lib, {for: t.name, only});
  if (t.kind === "album") tracks = tracks.filter(x => x.album().toLowerCase() === t.name.toLowerCase());
  if (t.kind === "artist") tracks = tracks.filter(x => x.artist().toLowerCase() === t.name.toLowerCase() || x.albumArtist().toLowerCase() === t.name.toLowerCase());
  if (t.kind === "song") tracks = tracks.slice(0, 1);
  if (!tracks.length) throw new Error("Nothing in your library for " + t.kind + " " + t.name);
  let q = M.userPlaylists.whose({name: a.queue})();
  if (q.length) {
    q = q[0];
    // Only ever empty a playlist this code made: never his own playlist that happens to share the name.
    if (q.smart() || !q.description().startsWith("Made by Claude"))
      throw new Error("There's already a playlist called " + a.queue + " that Claude didn't make; rename it and try again.");
    if (q.description() !== a.marker) q.description = a.marker;  // earlier wordings began the same way
  } else {
    q = M.make({new: "userPlaylist", withProperties: {name: a.queue, description: a.marker}});
  }
  if (q.tracks.length) M.delete(q.tracks);   // one event (q.tracks.delete() fails: "Can't get object"); removes them from this playlist only
  tracks = tracks.slice(0, a.cap);
  tracks.forEach(x => x.duplicate({to: q}));
  q.play();
  return JSON.stringify(t.kind + " " + t.name + " (" + tracks.length + " track" + (tracks.length === 1 ? "" : "s") + ")");
}'''

CONTROL = _PRELUDE + '''
  ({pause: () => M.pause(), resume: () => M.play(), next: () => M.nextTrack(), previous: () => M.previousTrack()})[a.action]();
  return JSON.stringify(String(M.playerState()));
}'''

class MusicApp:
    async def airplay_devices(self) -> list[dict]:
        rows = [line.split("\t") for line in (await run_applescript(AS_AIRPLAY)).splitlines() if line.strip()]
        return [{"id": r[0], "name": r[1], "kind": r[2], "selected": r[3] == "true", "active": r[4] == "true",
                 "available": r[5] == "true", "volume": int(r[6])} for r in rows if len(r) == 7]

    async def state(self) -> dict:
        """Player state and track, plus the speakers Music is set to play to (from each speaker's flag)."""
        s = await run_jxa(STATE)
        chosen = [d for d in await self.airplay_devices() if d["selected"]]
        return {**s, "devices": [d["name"] for d in chosen], "device_ids": [d["id"] for d in chosen],
                "device_kinds": [d["kind"] for d in chosen]}

    async def playlists(self) -> list[str]:
        return await run_jxa(PLAYLISTS, {"queue": QUEUE})

    async def search(self, query: str, kind: str | None = None, limit: int = 25) -> list[dict]:
        return await run_jxa(SEARCH, {"query": query, "kind": kind, "limit": limit})

    async def play(self, target: dict, device_ids: list[str], shuffle: bool | None) -> str:
        await self.set_devices(device_ids)
        return await run_jxa(PLAY, {"target": target, "shuffle": shuffle, "queue": QUEUE,
                                    "marker": QUEUE_MARKER, "cap": QUEUE_CAP})

    async def control(self, action: str) -> str:
        return await run_jxa(CONTROL, {"action": action})

    async def set_devices(self, device_ids: list[str]) -> list[str]:
        await run_applescript(AS_SET_DEVICES, *device_ids)
        return device_ids

    async def set_device_volume(self, device_id: str, level: int) -> int:
        return int(await run_applescript(AS_SET_VOLUME, device_id, str(level)))
