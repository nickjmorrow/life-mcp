"""Fakes for MusicApp and HomePods that record calls."""
from homepods import HomePodError

DEVICES = [  # what Music lists on TestMac (2026-09-27, after a manual AirPlay to Office)
    {"id": "33", "name": "TestMac", "kind": "computer", "selected": True, "active": False, "available": True, "volume": 100},
    {"id": "101", "name": "Office", "kind": "HomePod", "selected": False, "active": False, "available": True, "volume": 40},
    {"id": "100", "name": "Living Room", "kind": "Apple TV", "selected": False, "active": False, "available": True, "volume": 30},
    {"id": "56937", "name": "AirPods Max", "kind": "Bluetooth device", "selected": False, "active": False, "available": True, "volume": 7},
    {"id": "98", "name": "Bedroom", "kind": "HomePod", "selected": False, "active": False, "available": True, "volume": 40},
    {"id": "99", "name": "Kitchen", "kind": "HomePod", "selected": False, "active": False, "available": True, "volume": 40},
    {"id": "56945", "name": "Living Room", "kind": "HomePod", "selected": False, "active": False, "available": True, "volume": 40},
]
NAMES = {d["id"]: d["name"] for d in DEVICES}
KINDS = {d["id"]: d["kind"] for d in DEVICES}


class FakeMusic:
    def __init__(self, state="stopped", devices_playing=()):
        self.calls = []
        ids = [d["id"] for d in DEVICES if d["name"] in devices_playing and d["kind"] != "Apple TV"]
        self._state = {"state": state, "track": {"name": "Idioteque", "artist": "Radiohead", "album": "Kid A"}
                       if state != "stopped" else None, "shuffle": False,
                       "devices": [NAMES[i] for i in ids], "device_ids": ids,
                       "device_kinds": [KINDS[i] for i in ids]}
        self.lib_playlists = ["Chill", "Workout"]
        self.lib = [{"name": "Idioteque", "artist": "Radiohead", "album": "Kid A"},
                    {"name": "Everything In Its Right Place", "artist": "Radiohead", "album": "Kid A"},
                    {"name": "Chill", "artist": "Some Band", "album": "Chill"}]

    async def airplay_devices(self):
        return [dict(d, selected=d["id"] in self._state["device_ids"]) for d in DEVICES]

    async def state(self):
        return self._state

    async def playlists(self):
        return self.lib_playlists

    async def search(self, query, kind=None, limit=25):
        q = query.lower()
        return [t for t in self.lib if q in (t["name"] + t["artist"] + t["album"]).lower()][:limit]

    async def play(self, target, device_ids, shuffle):
        self.calls.append(("play", target, device_ids, shuffle))
        self._state.update(state="playing", devices=[NAMES[i] for i in device_ids], device_ids=device_ids,
                           device_kinds=[KINDS[i] for i in device_ids])
        return f"{target['kind']} {target['name']}"

    async def control(self, action):
        self.calls.append(("control", action))
        return "paused" if action == "pause" else "playing"

    async def set_devices(self, device_ids):
        self.calls.append(("set_devices", device_ids))
        self._state.update(devices=[NAMES[i] for i in device_ids], device_ids=device_ids,
                           device_kinds=[KINDS[i] for i in device_ids])
        return device_ids

    async def set_device_volume(self, device_id, level):
        self.calls.append(("volume", device_id, level))
        return level


class FakePods:
    def __init__(self, playing=(), offline=()):
        self.calls = []
        self.playing, self.offline = set(playing), set(offline)
        self.volumes = {n: 40.0 for n in ("Bedroom", "Kitchen", "Office", "Living Room (2)", "Living Room (3)")}

    async def discover(self):
        # Offline speakers are still discovered (they answer the scan) but fail when commanded.
        out = {n: {"ip": "x", "model": "HomePod Mini", "kind": "homepod"} for n in self.volumes}
        out["Living Room"] = {"ip": "y", "model": "Apple TV 4K (gen 3)", "kind": "tv"}
        return out

    def _check(self, name):
        if name in self.offline:
            raise HomePodError(f"'{name}' isn't answering. Is it plugged in?")

    async def now_playing(self, name):
        self._check(name)
        if name in self.playing:
            return {"state": "playing", "title": "Song From Phone", "artist": "Artist", "album": "Album", "app": "Music"}
        return {"state": "idle", "title": None, "artist": None, "album": None, "app": None}

    async def volume(self, name):
        self._check(name)
        return self.volumes[name]

    async def set_volume(self, name, level):
        self._check(name)
        self.calls.append(("volume", name, level))
        self.volumes[name] = level

    async def control(self, name, action):
        self._check(name)
        self.calls.append(("control", name, action))


def lib_song(i, name, artist, plays=0, played=None, skips=0, skipped=None, favorite=False, genre="Pop",
             album="A", added="2026-01-01T00:00:00.000Z"):
    return {"id": f"{i:016X}", "name": name, "artist": artist, "album": album, "genre": genre, "plays": plays,
            "played": played, "skips": skips, "skipped": skipped, "favorite": favorite, "disliked": False,
            "added": added, "seconds": 180.0}


class FakeLibrary:
    """MusicLibrary stand-in: songs and playlists in memory, writes recorded and applied."""

    def __init__(self):
        self.calls = []
        self.songs_ = [
            lib_song(1, "I'll Be Good", "Feed Me", 85, "2026-05-09T15:01:04Z", 6, "2026-10-01T15:36:35Z", True, "Dubstep"),
            lib_song(2, "Rumpta", "Solomun & Skrillex", 28, "2026-09-25T21:36:13Z", 2, "2026-09-28T21:10:04Z", True, "Dance"),
            lib_song(3, "Rush", "Troye Sivan", 80, "2026-09-11T20:11:31Z", 8, "2026-09-27T17:15:26Z"),
            lib_song(4, "Idioteque", "Radiohead", 5, "2026-09-30T10:00:00Z", album="Kid A", added="2026-09-01T00:00:00Z"),
        ]
        self.lists = {"P1": {"id": "P1", "name": "workout", "smart": False, "kind": "none", "description": "", "songs": ["0000000000000003", "0000000000000004"]},
                      "P2": {"id": "P2", "name": "Favorite Songs", "smart": True, "kind": "none", "description": "", "songs": ["0000000000000001"]}}

    def _by_id(self, i):
        return next(s for s in self.songs_ if s["id"] == i)

    async def songs(self):
        return [dict(s) for s in self.songs_]

    async def playlists(self):
        return [{**{k: v for k, v in p.items() if k != "songs"}, "count": len(p["songs"])} for p in self.lists.values()]

    async def playlist_songs(self, pid):
        return [{k: self._by_id(i)[k] for k in ("id", "name", "artist", "album")} for i in self.lists[pid]["songs"]]

    async def set_flags(self, ids, favorite, disliked):
        self.calls.append(("flags", ids, favorite, disliked))
        out = []
        for i in ids:
            s = self._by_id(i)
            if disliked is not None:
                s["disliked"] = disliked
            if favorite is not None:
                s["favorite"] = favorite
            out.append({k: s[k] for k in ("id", "name", "artist", "favorite", "disliked")})
        return out

    async def create(self, name, ids, description=None):
        self.calls.append(("create", name, ids, description))
        self.lists["P9"] = {"id": "P9", "name": name, "smart": False, "kind": "none", "description": description or "", "songs": list(ids)}
        return {"id": "P9", "name": name, "count": len(ids)}

    async def add(self, pid, ids):
        self.calls.append(("add", pid, ids))
        self.lists[pid]["songs"] += ids
        return {"name": self.lists[pid]["name"], "count": len(self.lists[pid]["songs"])}

    async def remove(self, pid, ids):
        self.calls.append(("remove", pid, ids))
        before = self.lists[pid]["songs"]
        self.lists[pid]["songs"] = [i for i in before if i not in ids]
        return {"name": self.lists[pid]["name"], "removed": len(before) - len(self.lists[pid]["songs"]),
                "count": len(self.lists[pid]["songs"])}

    async def rename(self, pid, name, description):
        self.calls.append(("rename", pid, name, description))
        if name:
            self.lists[pid]["name"] = name
        if description is not None:
            self.lists[pid]["description"] = description
        return {"name": self.lists[pid]["name"], "description": self.lists[pid]["description"]}

    async def reorder(self, pid, ids, expect):
        assert expect == len(self.lists[pid]["songs"])
        self.calls.append(("reorder", pid, ids))
        self.lists[pid]["songs"] = list(ids)
        return {"name": self.lists[pid]["name"], "count": len(ids)}

    async def delete(self, pid):
        self.calls.append(("delete", pid))
        return {"name": self.lists.pop(pid)["name"]}

    async def membership(self, ids):
        return {i: [p["name"] for p in self.lists.values() if i in p["songs"]] for i in ids}


class FakeAPI:
    """AppleMusic stand-in with a tiny catalog."""

    def __init__(self, signed_in=True):
        self.calls = []
        self.signed_in = signed_in
        self.catalog = {"1440111111": {"id": "1440111111", "type": "songs", "name": "Windowlicker", "artist": "Aphex Twin", "album": "Windowlicker", "year": "1999"},
                        "1440222222": {"id": "1440222222", "type": "songs", "name": "Idioteque", "artist": "Radiohead", "album": "Kid A", "year": "2000"}}
        self.playlists_ = [{"id": "p.1", "type": "library-playlists", "name": "workout", "editable": True}]
        self.ratings = {}

    def configured(self):
        return True

    def user(self):
        return {"user_token": "x", "saved": "2026-10-01T00:00:00+00:00"} if self.signed_in else None

    def _need(self):
        from applemusic_api import AppleMusicError
        if not self.signed_in:
            raise AppleMusicError("Not signed in to Apple Music yet.")

    async def storefront(self):
        return "us"

    async def search(self, term, types, limit=10):
        self.calls.append(("search", term, types))
        return {t: list(self.catalog.values()) if t == "songs" else [] for t in types}

    async def songs(self, ids):
        return [self.catalog[i] for i in ids if i in self.catalog]

    async def album_songs(self, album_id):
        return [self.catalog["1440111111"]]

    async def heavy_rotation(self, limit=10):
        self._need()
        return [{"id": "9", "type": "albums", "name": "Kid A", "artist": "Radiohead"}]

    async def recently_played(self, limit=30):
        self._need()
        return [self.catalog["1440111111"]]

    async def recommendations(self, limit=10):
        self._need()
        return [{"title": "Made for You", "items": [{"id": "pl.x", "type": "playlists", "name": "Chill Mix", "curator": "Apple Music"}]}]

    async def find_artist(self, name):
        return {"id": "77", "type": "artists", "name": "Radiohead"}

    async def artist(self, aid):
        return {"artist": {"id": aid, "type": "artists", "name": "Radiohead", "genre": "Alternative"},
                "top-songs": [self.catalog["1440222222"]], "similar-artists": [{"id": "78", "type": "artists", "name": "Thom Yorke"}],
                "latest-release": [], "full-albums": []}

    async def genres(self):
        return [{"id": "18", "type": "genres", "name": "Hip-Hop/Rap"}, {"id": "17", "type": "genres", "name": "Dance"}]

    async def charts(self, types, genre_id=None, limit=20):
        self.calls.append(("charts", types, genre_id))
        return {t: [self.catalog["1440111111"]] for t in types}

    async def add_to_library(self, songs=(), albums=(), playlists=()):
        self._need()
        self.calls.append(("library", list(songs), list(albums), list(playlists)))

    async def library_playlists(self):
        return self.playlists_

    async def add_to_playlist(self, pid, ids):
        self.calls.append(("playlist", pid, ids))

    async def rating(self, sid):
        return self.ratings.get(sid)

    async def rate(self, sid, value):
        self.calls.append(("rate", sid, value))
