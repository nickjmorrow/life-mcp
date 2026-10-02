import asyncio
import json
import stat

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import applemusic_api as am
from applemusic_api import AppleMusic, AppleMusicError


@pytest.fixture
def key(tmp_path):
    k = ec.generate_private_key(ec.SECP256R1())
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption()).decode()
    path = tmp_path / "key.p8"
    path.write_text(pem)
    return path, k.public_key()


def client(tmp_path, key, handler, signed_in=True):
    token_file = tmp_path / "am.json"
    if signed_in:
        am.save_user_token("user-token-123456789012345", token_file)
    return AppleMusic(transport=httpx.MockTransport(handler), token_file=token_file, key_file=key[0],
                      env={"APPLE_MUSIC_TEAM_ID": "TEAM123456", "APPLE_MUSIC_KEY_ID": "KEY123"})


def sf_or(handler):
    def h(req):
        if req.url.path == "/v1/me/storefront":
            return httpx.Response(200, json={"data": [{"id": "us"}]})
        return handler(req)
    return h


def test_developer_token_claims(key):
    tok = am.developer_token("TEAM123456", "KEY123", key[0].read_text(), now=1_000_000)
    assert jwt.get_unverified_header(tok)["kid"] == "KEY123"
    claims = jwt.decode(tok, key[1], algorithms=["ES256"], options={"verify_exp": False})
    assert claims == {"iss": "TEAM123456", "iat": 1_000_000, "exp": 1_000_000 + 30 * 86400}


def test_token_file_private(tmp_path):
    am.save_user_token("abc", tmp_path / "d" / "t.json")
    assert stat.S_IMODE((tmp_path / "d" / "t.json").stat().st_mode) == 0o600
    assert am.load_user_token(tmp_path / "d" / "t.json")["user_token"] == "abc"


def test_not_configured(tmp_path):
    c = AppleMusic(token_file=tmp_path / "x.json", key_file=tmp_path / "none.p8", env={})
    with pytest.raises(AppleMusicError, match="isn't set up"):
        c.dev_token()


def test_personal_calls_need_sign_in(tmp_path, key):
    c = client(tmp_path, key, lambda r: httpx.Response(200, json={}), signed_in=False)
    with pytest.raises(AppleMusicError, match="Not signed in.*applemusic_signin"):
        asyncio.run(c.recommendations())


def test_headers_and_search(tmp_path, key):
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, json={"results": {"songs": {"data": [
            {"id": "1440", "type": "songs", "attributes": {"name": "Idioteque", "artistName": "Radiohead",
             "albumName": "Kid A", "releaseDate": "2000-10-02", "genreNames": ["Alternative"],
             "url": "https://music.apple.com/us/song/1440"}}]}}})
    c = client(tmp_path, key, sf_or(h))
    res = asyncio.run(c.search("idioteque", ["songs", "albums"]))
    assert res["songs"] == [{"id": "1440", "type": "songs", "name": "Idioteque", "artist": "Radiohead",
                             "album": "Kid A", "year": "2000", "genre": "Alternative",
                             "url": "https://music.apple.com/us/song/1440"}]
    assert res["albums"] == []
    req = seen[0]
    assert req.url.path == "/v1/catalog/us/search"
    assert req.url.params["types"] == "songs,albums"
    assert req.headers["Authorization"].startswith("Bearer ey")


def test_errors_in_plain_words(tmp_path, key):
    for code, words in ((403, "expired"), (429, "too many"), (404, "not found"), (500, "error 500")):
        c = client(tmp_path, key, lambda r, code=code: httpx.Response(code, json={"errors": []}))
        with pytest.raises(AppleMusicError, match=words):
            asyncio.run(c.request("GET", "/v1/me/storefront"))


def test_recommendations_grouped(tmp_path, key):
    body = {"data": [{"attributes": {"title": {"stringForDisplay": "Made for You"}},
                      "relationships": {"contents": {"data": [
                          {"id": "pl.1", "type": "playlists", "attributes": {"name": "Chill Mix", "curatorName": "Apple Music"}}]}}},
                     {"attributes": {"title": {"stringForDisplay": "Empty"}}, "relationships": {"contents": {"data": []}}}]}
    c = client(tmp_path, key, lambda r: httpx.Response(200, json=body))
    assert asyncio.run(c.recommendations()) == [
        {"title": "Made for You", "items": [{"id": "pl.1", "type": "playlists", "name": "Chill Mix", "curator": "Apple Music"}]}]


def test_recently_played_pages(tmp_path, key):
    offsets = []

    def h(req):
        offsets.append(int(req.url.params["offset"]))
        n = 30 if offsets[-1] == 0 else 5
        return httpx.Response(200, json={"data": [{"id": str(i), "type": "songs", "attributes": {"name": f"s{i}"}}
                                                  for i in range(n)]})
    c = client(tmp_path, key, h)
    assert len(asyncio.run(c.recently_played(50))) == 35
    assert offsets == [0, 30]


def test_writes(tmp_path, key):
    seen = []

    def h(req):
        seen.append((req.method, req.url.path, dict(req.url.params), json.loads(req.content) if req.content else None))
        if req.method == "POST" and req.url.path == "/v1/me/library/playlists":
            return httpx.Response(201, json={"data": [{"id": "p.9", "type": "library-playlists", "attributes": {"name": "new"}}]})
        return httpx.Response(202 if req.method == "POST" else 204)
    c = client(tmp_path, key, h)
    asyncio.run(c.add_to_library(songs=["1", "2"], albums=["9"]))
    asyncio.run(c.add_to_playlist("p.9", ["1"]))
    made = asyncio.run(c.create_playlist("new", ["1"], "desc"))
    asyncio.run(c.rate("1", -1))
    asyncio.run(c.rate("1", None))
    assert seen[0][:3] == ("POST", "/v1/me/library", {"ids[songs]": "1,2", "ids[albums]": "9"})
    assert seen[1][3] == {"data": [{"id": "1", "type": "songs"}]}
    assert seen[2][3]["attributes"] == {"name": "new", "description": "desc"}
    assert made["id"] == "p.9"
    assert seen[3][:2] == ("PUT", "/v1/me/ratings/songs/1") and seen[3][3]["attributes"] == {"value": -1}
    assert seen[4][:2] == ("DELETE", "/v1/me/ratings/songs/1")


def test_403_says_what_apple_said(tmp_path, key):
    c = client(tmp_path, key, lambda r: httpx.Response(403, json={"errors": [{"detail": "No active subscription"}]}))
    with pytest.raises(AppleMusicError, match=r"refused that \(No active subscription\)"):
        asyncio.run(c.request("GET", "/v1/me/storefront"))


def test_clearing_unset_rating_is_fine_and_rating_reads(tmp_path, key):
    def h(req):
        if req.method == "DELETE":
            return httpx.Response(404)
        if "1" in req.url.path.rsplit("/", 1)[-1]:
            return httpx.Response(200, json={"data": [{"attributes": {"value": -1}}]})
        return httpx.Response(404)
    c = client(tmp_path, key, h)
    asyncio.run(c.rate("5", None))
    assert asyncio.run(c.rating("1")) == -1
    assert asyncio.run(c.rating("5")) is None


def test_bad_token_file_is_not_signed_in(tmp_path):
    (tmp_path / "t.json").write_text('{"nope": 1}')
    assert am.load_user_token(tmp_path / "t.json") is None


def test_catalog_works_before_sign_in(tmp_path, key):
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, json={"results": {}})
    c = client(tmp_path, key, h, signed_in=False)
    asyncio.run(c.search("x", ["songs"]))
    assert seen[0].url.path == "/v1/catalog/us/search" and "Music-User-Token" not in seen[0].headers


def test_401_names_this_mac(tmp_path, key, monkeypatch):
    monkeypatch.setattr(am, "HOST", "Edgar")
    c = client(tmp_path, key, lambda r: httpx.Response(401))
    with pytest.raises(AppleMusicError, match="rejected Edgar's developer token"):
        asyncio.run(c.request("GET", "/v1/catalog/us/search"))


def test_sign_in_port_default_and_override(monkeypatch):
    import importlib
    import applemusic_signin
    monkeypatch.delenv("APPLEMUSIC_SIGNIN_PORT", raising=False)
    assert importlib.reload(applemusic_signin).PORT == 8770
    monkeypatch.setenv("APPLEMUSIC_SIGNIN_PORT", "8799")
    assert importlib.reload(applemusic_signin).PORT == 8799
