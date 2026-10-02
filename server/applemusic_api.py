"""Apple Music API (api.music.apple.com): the catalog and his account, beyond what Music.app sees.

Two tokens: a developer token we sign ourselves (ES256 JWT from his MusicKit key and
Apple developer team) and his Music user token from the one-time sign-in page (applemusic_signin.py).
Secrets: APPLE_MUSIC_TEAM_ID and APPLE_MUSIC_KEY_ID in ~/.zshrc.local; the key and the user token
are files in ~/.config/life-mcp/ (600). Results come back as small flat dicts.
"""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import jwt
from fastmcp.exceptions import ToolError
from host import NAME as HOST

BASE = "https://api.music.apple.com"
CONFIG = Path.home() / ".config" / "life-mcp"
KEY_FILE = CONFIG / "applemusic_key.p8"
TOKEN_FILE = CONFIG / "applemusic.json"
DEV_TOKEN_DAYS = 30
SIGN_IN = ("sign in on the Mac he's at with `uv run applemusic_signin.py` in ~/Projects/life-mcp/server, then "
           "`~/Projects/mac-setup/secrets.py push-file ~/.config/life-mcp/applemusic.json` and "
           "`secrets.py pull` on the connector's Mac")


class AppleMusicError(ToolError):
    """An Apple Music API problem, in plain words."""


def developer_token(team_id: str, key_id: str, key_pem: str, now: float | None = None) -> str:
    now = int(now or time.time())
    return jwt.encode({"iss": team_id, "iat": now, "exp": now + DEV_TOKEN_DAYS * 86400},
                      key_pem, algorithm="ES256", headers={"kid": key_id})


def save_user_token(token: str, path: Path = TOKEN_FILE) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".new")
    tmp.write_text(json.dumps({"user_token": token, "saved": datetime.now(timezone.utc).isoformat()}))
    tmp.chmod(0o600)
    tmp.replace(path)


def load_user_token(path: Path = TOKEN_FILE) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("user_token"), str) else None


def _artwork(attrs: dict) -> str | None:
    art = attrs.get("artwork") or {}
    url = art.get("url")
    return url.replace("{w}", "300").replace("{h}", "300") if url else None


def flat(item: dict) -> dict:
    """One catalog or library resource as a small dict: id, type, name, artist, album, year, url."""
    a = item.get("attributes") or {}
    out = {"id": item.get("id"), "type": item.get("type"), "name": a.get("name") or a.get("title", "")}
    if a.get("artistName"):
        out["artist"] = a["artistName"]
    if a.get("albumName"):
        out["album"] = a["albumName"]
    if a.get("releaseDate"):
        out["year"] = a["releaseDate"][:4]
    if a.get("genreNames"):
        out["genre"] = a["genreNames"][0]
    if a.get("curatorName"):
        out["curator"] = a["curatorName"]
    if a.get("trackCount"):
        out["tracks"] = a["trackCount"]
    if (a.get("playParams") or {}).get("catalogId"):
        out["catalog_id"] = a["playParams"]["catalogId"]
    if a.get("url"):
        out["url"] = a["url"]
    desc = (a.get("description") or a.get("editorialNotes") or {})
    if isinstance(desc, dict) and desc.get("short"):
        out["about"] = desc["short"]
    return out


class AppleMusic:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, token_file: Path = TOKEN_FILE,
                 key_file: Path = KEY_FILE, env: dict | None = None):
        self._transport = transport
        self._token_file = token_file
        self._key_file = key_file
        self._env = os.environ if env is None else env
        self._dev: tuple[str, float] | None = None
        self._storefront: str | None = None

    # --- tokens -------------------------------------------------------------------------------

    def configured(self) -> bool:
        return bool(self._env.get("APPLE_MUSIC_TEAM_ID") and self._env.get("APPLE_MUSIC_KEY_ID")
                    and self._key_file.exists())

    def dev_token(self) -> str:
        if self._dev and self._dev[1] > time.time() + 86400:
            return self._dev[0]
        if not self.configured():
            raise AppleMusicError(f"The Apple Music API isn't set up on {HOST} yet (no MusicKit key). "
                                  "Library tools still work.")
        now = time.time()
        token = developer_token(self._env["APPLE_MUSIC_TEAM_ID"], self._env["APPLE_MUSIC_KEY_ID"],
                                self._key_file.read_text(), now)
        self._dev = (token, now + DEV_TOKEN_DAYS * 86400)
        return token

    def user(self) -> dict | None:
        return load_user_token(self._token_file)

    # --- http ---------------------------------------------------------------------------------

    async def request(self, method: str, path: str, *, params=None, body=None, personal: bool = False):
        headers = {"Authorization": f"Bearer {self.dev_token()}"}
        if personal or path.startswith("/v1/me"):
            u = self.user()
            if not u:
                raise AppleMusicError(f"Not signed in to Apple Music yet: {SIGN_IN}.")
            headers["Music-User-Token"] = u["user_token"]
        async with httpx.AsyncClient(base_url=BASE, transport=self._transport, timeout=20) as client:
            try:
                r = await client.request(method, path, params=params, json=body, headers=headers)
            except httpx.HTTPError as e:
                raise AppleMusicError(f"Couldn't reach Apple Music: {e.__class__.__name__}.")
        if r.status_code == 404 and method == "DELETE":
            return {}                       # already gone (e.g. clearing a rating that isn't set)
        detail = ""
        if r.status_code >= 400:
            try:
                detail = "; ".join(e.get("detail") or e.get("title", "") for e in r.json().get("errors", []))[:200]
            except (ValueError, AttributeError):
                pass
        if r.status_code == 401:
            self._dev = None
            raise AppleMusicError(f"Apple Music rejected {HOST}'s developer token (check the MusicKit key and "
                                  "APPLE_MUSIC_KEY_ID / APPLE_MUSIC_TEAM_ID).")
        if r.status_code == 403:
            # Also what Apple sends for no subscription or a playlist the API may not edit.
            raise AppleMusicError("Apple Music refused that" + (f" ({detail})" if detail else "")
                                  + f". If it keeps happening for everything, the sign-in may have expired: {SIGN_IN}.")
        if r.status_code == 429:
            raise AppleMusicError("Apple Music says too many requests; try again in a minute.")
        if r.status_code == 404:
            raise AppleMusicError("Apple Music doesn't have that (not found).")
        if r.status_code >= 400:
            raise AppleMusicError(f"Apple Music error {r.status_code}" + (f": {detail}" if detail else "."))
        return r.json() if r.content else {}

    async def storefront(self) -> str:
        """His storefront once signed in; before that, the US catalog (not cached, so a later
        sign-in picks up his real one)."""
        if not self.user():
            return "us"
        if not self._storefront:
            data = await self.request("GET", "/v1/me/storefront")
            self._storefront = data["data"][0]["id"]
        return self._storefront

    async def _catalog(self, path: str, **params):
        return await self.request("GET", f"/v1/catalog/{await self.storefront()}{path}", params=params or None)

    # --- catalog ------------------------------------------------------------------------------

    async def search(self, term: str, types: list[str], limit: int = 10) -> dict[str, list[dict]]:
        data = await self._catalog("/search", term=term, types=",".join(types), limit=min(limit, 25))
        res = data.get("results", {})
        return {t: [flat(i) for i in res.get(t, {}).get("data", [])] for t in types}

    async def songs(self, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        data = await self._catalog("/songs", ids=",".join(ids))
        return [flat(i) for i in data.get("data", [])]

    async def album_songs(self, album_id: str) -> list[dict]:
        data = await self._catalog(f"/albums/{album_id}/tracks", limit=100)
        return [flat(i) for i in data.get("data", [])]

    async def artist(self, artist_id: str) -> dict:
        data = await self._catalog(f"/artists/{artist_id}", views="top-songs,similar-artists,latest-release,full-albums")
        item = data["data"][0]
        views = item.get("views", {})
        return {"artist": flat(item),
                **{k: [flat(i) for i in views.get(k, {}).get("data", [])]
                   for k in ("top-songs", "similar-artists", "latest-release", "full-albums")}}

    async def find_artist(self, name: str) -> dict:
        hits = (await self.search(name, ["artists"], 5))["artists"]
        if not hits:
            raise AppleMusicError(f"No artist called '{name}' on Apple Music.")
        exact = [h for h in hits if h["name"].lower() == name.strip().lower()]
        return (exact or hits)[0]

    async def genres(self) -> list[dict]:
        return [flat(i) for i in (await self._catalog("/genres")).get("data", [])]

    async def charts(self, types: list[str], genre_id: str | None = None, limit: int = 20) -> dict[str, list[dict]]:
        params = {"types": ",".join(types), "limit": min(limit, 50)}
        if genre_id:
            params["genre"] = genre_id
        data = await self._catalog("/charts", **params)
        res = data.get("results", {})
        return {t: [flat(i) for chart in res.get(t, []) for i in chart.get("data", [])][:limit] for t in types}

    # --- his account --------------------------------------------------------------------------

    async def recommendations(self, limit: int = 10) -> list[dict]:
        data = await self.request("GET", "/v1/me/recommendations", params={"limit": min(limit, 30)})
        groups = []
        for rec in data.get("data", []):
            title = ((rec.get("attributes") or {}).get("title") or {}).get("stringForDisplay", "For you")
            items = [flat(i) for i in (rec.get("relationships", {}).get("contents", {}).get("data", []))]
            if items:
                groups.append({"title": title, "items": items})
        return groups

    async def recently_played(self, limit: int = 30) -> list[dict]:
        out, offset = [], 0
        while len(out) < limit:
            page = min(30, limit - len(out))
            data = await self.request("GET", "/v1/me/recent/played/tracks",
                                      params={"types": "songs,library-songs", "limit": page, "offset": offset})
            items = data.get("data", [])
            out += [flat(i) for i in items]
            if len(items) < page:
                break
            offset += page
        return out

    async def heavy_rotation(self, limit: int = 10) -> list[dict]:
        data = await self.request("GET", "/v1/me/history/heavy-rotation", params={"limit": min(limit, 10)})
        return [flat(i) for i in data.get("data", [])]

    async def add_to_library(self, songs: list[str] = (), albums: list[str] = (), playlists: list[str] = ()) -> None:
        params = {f"ids[{k}]": ",".join(v) for k, v in (("songs", songs), ("albums", albums),
                                                         ("playlists", playlists)) if v}
        if params:
            await self.request("POST", "/v1/me/library", params=params)

    async def library_playlists(self) -> list[dict]:
        out, path = [], "/v1/me/library/playlists?limit=100"
        while path:
            data = await self.request("GET", path)
            out += [{**flat(i), "editable": (i.get("attributes") or {}).get("canEdit", False)}
                    for i in data.get("data", [])]
            path = data.get("next")
        return out

    async def add_to_playlist(self, playlist_id: str, song_ids: list[str]) -> None:
        await self.request("POST", f"/v1/me/library/playlists/{playlist_id}/tracks",
                           body={"data": [{"id": i, "type": "songs"} for i in song_ids]})

    async def create_playlist(self, name: str, song_ids: list[str], description: str | None = None) -> dict:
        body = {"attributes": {"name": name, **({"description": description} if description else {})}}
        if song_ids:
            body["relationships"] = {"tracks": {"data": [{"id": i, "type": "songs"} for i in song_ids]}}
        data = await self.request("POST", "/v1/me/library/playlists", body=body)
        return flat(data["data"][0])

    async def rating(self, song_id: str) -> int | None:
        """1 (loved/favorite), -1 (disliked) or None."""
        try:
            data = await self.request("GET", f"/v1/me/ratings/songs/{song_id}")
        except AppleMusicError as e:
            if "not found" in str(e):
                return None
            raise
        items = data.get("data") or []
        return (items[0].get("attributes") or {}).get("value") if items else None

    async def rate(self, song_id: str, value: int | None) -> None:
        """1 = love/favorite, -1 = dislike, None = clear."""
        path = f"/v1/me/ratings/songs/{song_id}"
        if value is None:
            await self.request("DELETE", path)
        else:
            await self.request("PUT", path, body={"type": "rating", "attributes": {"value": value}})
