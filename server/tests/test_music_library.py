import asyncio
import json
from datetime import datetime, timezone

import pytest

import music_library as ml
from music_app import MusicError
from test_music_app import fake_osascript

NOW = datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)


def song(i, name, artist, plays=0, played=None, skips=0, skipped=None, favorite=False, genre="Pop", album="A"):
    return {"id": f"{i:016X}", "name": name, "artist": artist, "album": album, "genre": genre, "plays": plays,
            "played": played, "skips": skips, "skipped": skipped, "favorite": favorite, "disliked": False,
            "added": "2026-01-01T00:00:00.000Z", "seconds": 180.0}


SONGS = [
    song(1, "I'll Be Good", "Feed Me", 85, "2026-05-09T15:01:04.000Z", 6, "2026-10-01T15:36:35.000Z", True, "Dubstep"),
    song(2, "Rumpta", "Solomun & Skrillex", 28, "2026-09-25T21:36:13.000Z", 2, "2026-09-28T21:10:04.000Z", True, "Dance"),
    song(3, "Rush", "Troye Sivan", 80, "2026-09-11T20:11:31.000Z", 8, "2026-09-27T17:15:26.000Z"),
    song(4, "Love Again", "Dua Lipa", 10, "2026-09-30T10:00:00.000Z"),
    song(5, "Love Again (Akinyele Back)", "Run The Jewels", 247, "2026-09-29T10:00:00.000Z", genre="Hip-Hop/Rap"),
    song(6, "Rush", "Troye Sivan", 2),  # a second copy of the same song
    song(7, "Nobody", "Nobody"),
]


def test_recent_skips_newest_first():
    assert [s["name"] for s in ml.recent(SONGS, "skipped", limit=2, now=NOW)] == ["I'll Be Good", "Rumpta"]


def test_recent_window():
    assert [s["name"] for s in ml.recent(SONGS, "skipped", days=3, now=NOW)] == ["I'll Be Good", "Rumpta"]
    assert [s["name"] for s in ml.recent(SONGS, "played", days=3, now=NOW)] == ["Love Again", "Love Again (Akinyele Back)"]


def test_top_songs_and_artists():
    assert ml.top(SONGS, "song", 2)[0] == ("Love Again (Akinyele Back) — Run The Jewels", 247, 1)
    artists = ml.top(SONGS, "artist", 10)
    assert artists[0] == ("Run The Jewels", 247, 1)
    assert ("Troye Sivan", 82, 2) in artists
    assert all(plays for _, plays, _ in artists)  # never-played artists left out


def test_top_in_window_only_counts_recently_played():
    names = [n for n, _, _ in ml.top(SONGS, "genre", 10, days=3, now=NOW)]
    assert names == ["Hip-Hop/Rap", "Pop"]


@pytest.mark.parametrize("ref,expect", [
    ("Rumpta", 2),
    ("rumpta — solomun & skrillex", 2),
    ("Rush - Troye Sivan", 3),     # two copies: the one he plays
    ("0000000000000004", 4),
    ("i'll be", 1),                # contains, when nothing is exact
])
def test_resolve(ref, expect):
    assert ml.resolve_songs([ref], SONGS)[0]["id"] == f"{expect:016X}"


def test_resolve_exact_beats_contains():
    assert ml.resolve_songs(["Love Again"], SONGS)[0]["artist"] == "Dua Lipa"


def test_resolve_ambiguous_lists_choices():
    with pytest.raises(MusicError, match="several songs.*Dua Lipa.*Run The Jewels"):
        ml.resolve_songs(["love"], SONGS)


def test_resolve_missing():
    with pytest.raises(MusicError, match="No song matching 'zzz'"):
        ml.resolve_songs(["zzz"], SONGS)
    with pytest.raises(MusicError, match="No song with id"):
        ml.resolve_songs(["00000000000000FF"], SONGS)


def test_playlists_hide_builtins_and_queue(tmp_path, monkeypatch):
    rows = [{"id": "A", "name": "Music", "smart": True, "kind": "Music", "description": "", "count": 1047},
            {"id": "B", "name": "workout", "smart": False, "kind": "none", "description": "", "count": 272},
            {"id": "C", "name": "Claude Queue", "smart": False, "kind": "none", "description": "", "count": 3},
            {"id": "D", "name": "Favorite Songs", "smart": True, "kind": "none", "description": "", "count": 413}]
    fake_osascript(tmp_path, monkeypatch, f"echo '{json.dumps(rows)}'")
    assert [p["name"] for p in asyncio.run(ml.MusicLibrary().playlists())] == ["workout", "Favorite Songs"]


def test_writes_pass_ids_and_queue_in_argv(tmp_path, monkeypatch):
    log = tmp_path / "log"
    fake_osascript(tmp_path, monkeypatch, f'for a; do last="$a"; done; echo "$last" >> {log}; echo "{{}}"')
    lib = ml.MusicLibrary()
    asyncio.run(lib.remove("P1", ["0000000000000001"]))
    asyncio.run(lib.set_flags(["0000000000000002"], False, None))
    sent = [json.loads(line) for line in log.read_text().splitlines()]
    assert sent[0] == {"playlist": "P1", "songs": ["0000000000000001"], "queue": "Claude Queue"}
    assert sent[1] == {"songs": ["0000000000000002"], "favorite": False, "disliked": None}


def test_edit_scripts_refuse_smart_builtin_and_queue():
    for script in (ml.ADD, ml.REMOVE, ml.RENAME, ml.REORDER, ml.DELETE):
        assert "editable(a.playlist)" in script
    assert 'q.smart() || String(q.specialKind()) !== "none" || q.name() === a.queue' in ml._HELPERS


def test_title_with_dash_is_a_name_first():
    songs = SONGS + [song(20, "Here Comes the Sun - Remastered 2019", "The Beatles")]
    assert ml.resolve_songs(["Here Comes the Sun - Remastered 2019"], songs)[0]["id"] == f"{20:016X}"


def test_reorder_appends_before_deleting():
    body = ml.REORDER
    assert body.index("duplicate({to: p})") < body.index("ts[i].delete()")
    assert "a.expect" in body


def test_backup_is_private(tmp_path):
    path = ml.backup({"id": "P1", "name": "work/out"}, [{"id": "1"}], "delete", tmp_path)
    assert oct(path.stat().st_mode)[-3:] == "600" and "workout" in path.name
    assert json.loads(path.read_text())["songs"] == [{"id": "1"}]
