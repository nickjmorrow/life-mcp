import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import music_lib_mcp
import music_library
import music_mcp
from fake_music import FakeAPI, FakeLibrary, FakeMusic, FakePods, lib_song


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    lib, api, music = FakeLibrary(), FakeAPI(), FakeMusic()
    monkeypatch.setattr(music_lib_mcp, "_lib", lib)
    monkeypatch.setattr(music_lib_mcp, "_api", api)
    monkeypatch.setattr(music_lib_mcp, "SYNC_POLL_S", 0.01)
    monkeypatch.setattr(music_lib_mcp, "SYNC_WAIT_S", 0.2)
    monkeypatch.setattr(music_library, "BACKUPS", tmp_path / "backups")
    music_mcp._music, music_mcp._pods = music, FakePods()
    return lib, api, music


def call(tool, **args):
    async def go():
        async with Client(music_lib_mcp.mcp) as client:
            return (await client.call_tool(tool, args)).content[0].text
    return asyncio.run(go())


def test_recent_skips(fakes):
    out = call("music_recent", kind="skipped", limit=2)
    lines = out.splitlines()
    assert "I'll Be Good — Feed Me ♥ [0000000000000001]" in lines[0] and "skips 6" in lines[0]
    assert "Rumpta" in lines[1] and len(lines) == 2


def test_unfavorite_last_three_skipped(fakes):
    lib, _, _ = fakes
    out = call("music_set_favorite", action="unfavorite", last_skipped=3)
    assert lib.calls == [("flags", ["0000000000000001", "0000000000000002", "0000000000000003"], False, None)]
    assert out.startswith("Unfavorited:") and "♥" not in out


def test_dislike_named_and_catalog(fakes):
    lib, api, _ = fakes
    out = call("music_set_favorite", action="dislike", songs=["rush", "1440111111"])
    assert lib.calls == [("flags", ["0000000000000003"], False, True)]
    assert ("rate", "1440111111", -1) in api.calls
    assert "Windowlicker — Aphex Twin" in out


def test_favorite_needs_songs(fakes):
    with pytest.raises(ToolError, match="Say which songs"):
        call("music_set_favorite", action="favorite")


def test_top_and_taste(fakes):
    assert call("music_top", by="artist", limit=1) == "1. Feed Me — 85 plays across 1 song"
    taste = call("music_taste")
    assert "Library: 4 songs, 2 favorites" in taste and "heavy rotation: Kid A — Radiohead" in taste


def test_taste_without_api(fakes):
    fakes[1].signed_in = False
    assert "Apple Music history unavailable" in call("music_taste")


def test_song_info_lists_playlists(fakes):
    out = call("music_song_info", songs=["Idioteque"])
    assert "playlists: workout" in out and "plays 5" in out


def test_show_create_add_remove(fakes):
    lib, api, _ = fakes
    assert call("music_show_playlist", playlist="work").splitlines()[1] == "1. Rush — Troye Sivan [0000000000000003]"
    out = call("music_create_playlist", name="skips", last_skipped=2)
    assert lib.calls[-1] == ("create", "skips", ["0000000000000001", "0000000000000002"], None)
    assert "2 songs" in out
    call("music_add_to_playlist", playlist="workout", songs=["Rumpta", "1440111111"])
    assert lib.calls[-1] == ("add", "P1", ["0000000000000002"])
    assert ("playlist", "p.1", ["1440111111"]) in api.calls
    out = call("music_remove_from_playlist", playlist="workout", songs=["Rush"])
    assert "Removed 1" in out and lib.lists["P1"]["songs"] == ["0000000000000004", "0000000000000002"]


def test_create_refuses_existing_name(fakes):
    with pytest.raises(ToolError, match="already has a playlist"):
        call("music_create_playlist", name="Workout")


def test_smart_playlists_not_edited(fakes):
    with pytest.raises(ToolError, match="smart playlist"):
        call("music_add_to_playlist", playlist="Favorite Songs", songs=["Rush"])


def test_unknown_playlist_lists_them(fakes):
    with pytest.raises(ToolError, match="No playlists match 'zzz'. Playlists: workout, Favorite Songs"):
        call("music_show_playlist", playlist="zzz")


def test_reorder_by_plays_and_custom(fakes):
    lib, _, _ = fakes
    call("music_reorder_playlist", playlist="workout", by="plays")
    assert lib.lists["P1"]["songs"] == ["0000000000000003", "0000000000000004"]
    call("music_reorder_playlist", playlist="workout", by="custom", order=["Idioteque"])
    assert lib.lists["P1"]["songs"] == ["0000000000000004", "0000000000000003"]


def test_delete_needs_confirm(fakes):
    lib, _, _ = fakes
    assert call("music_delete_playlist", playlist="workout").startswith("Not deleted")
    assert "P1" in lib.lists
    assert "Deleted the playlist 'workout'" in call("music_delete_playlist", playlist="workout", confirm=True)
    assert "P1" not in lib.lists


def test_rename(fakes):
    assert call("music_rename_playlist", playlist="workout", new_name="gym") == "'workout' is now 'gym'"


def test_search_marks_library(fakes):
    out = call("music_search_catalog", query="x", types=["songs"])
    assert "Idioteque — Radiohead (Kid A, 2000) (in his library) [1440222222]" in out
    assert "Windowlicker — Aphex Twin (Windowlicker, 1999) [1440111111]" in out


def test_artist_and_recs_and_charts(fakes):
    out = call("music_artist", artist="radiohead")
    assert "Radiohead [77]" in out and "Similar artists:\n- Thom Yorke [78]" in out
    assert "Made for You:\n- Chill Mix — Apple Music [pl.x] (playlist)" in call("music_recommendations")
    call("music_charts", genre="dance")
    assert ("charts", ["songs"], "17") in fakes[1].calls


def test_not_signed_in_is_plain(fakes):
    fakes[1].signed_in = False
    with pytest.raises(ToolError, match="Not signed in"):
        call("music_recommendations")


def test_play_catalog_waits_for_library(fakes):
    lib, api, music = fakes
    out = call("music_play_catalog", song_or_album="1440222222", rooms="office")
    assert ("library", ["1440222222"], [], []) in api.calls
    assert music.calls[-1][0] == "play" and music.calls[-1][1]["ids"] == ["0000000000000004"]
    assert "playing it (1 song) in Office" in out


def test_play_catalog_not_synced_yet(fakes, monkeypatch):
    monkeypatch.setattr(music_lib_mcp, "SYNC_WAIT_S", 0)
    out = call("music_play_catalog", song_or_album="1440111111", rooms="office")
    assert "hasn't reached TestMac yet" in out


def test_short_digit_titles_are_names(fakes):
    lib, api, _ = fakes
    lib.songs_.append(lib_song(9, "1979", "The Smashing Pumpkins"))
    call("music_set_favorite", action="favorite", songs=["1979"])
    assert lib.calls[-1] == ("flags", ["0000000000000009"], True, False)
    assert not [c for c in api.calls if c[0] == "rate"]


def test_unfavorite_catalog_keeps_a_dislike(fakes):
    _, api, _ = fakes
    api.ratings = {"1440111111": -1, "1440222222": 1}
    call("music_set_favorite", action="unfavorite", songs=["1440111111", "1440222222"])
    assert [c for c in api.calls if c[0] == "rate"] == [("rate", "1440222222", None)]


def test_favorite_reports_partial_failure(fakes):
    _, api, _ = fakes
    async def boom(sid, value):
        raise ToolError("Apple Music refused that")
    api.rate = boom
    out = call("music_set_favorite", action="favorite", songs=["Rush", "1440111111"])
    assert "Favorited:\n- Rush — Troye Sivan" in out and "Not changed:\n- Windowlicker" in out


def test_destructive_tools_need_exact_names(fakes):
    for tool, extra in (("music_delete_playlist", {"confirm": True}), ("music_remove_from_playlist", {"songs": ["Rush"]}),
                        ("music_reorder_playlist", {"by": "plays"}), ("music_rename_playlist", {"new_name": "x"})):
        with pytest.raises(ToolError, match="No playlist called exactly 'work'"):
            call(tool, playlist="work", **extra)


def test_duplicate_playlist_names_need_the_id(fakes):
    lib, _, _ = fakes
    lib.lists["P3"] = {"id": "P3", "name": "workout", "smart": False, "kind": "none", "description": "", "songs": []}
    with pytest.raises(ToolError, match="2 playlists called 'workout'"):
        call("music_delete_playlist", playlist="workout", confirm=True)


def test_queue_name_refused(fakes):
    with pytest.raises(ToolError, match="music_play refills"):
        call("music_create_playlist", name=" claude queue ")
    with pytest.raises(ToolError, match="already has a playlist"):
        call("music_rename_playlist", playlist="workout", new_name="Favorite Songs")


def test_remove_matches_the_playlists_own_copy(fakes):
    lib, _, _ = fakes
    lib.songs_.append(lib_song(6, "Rush", "Troye Sivan", 1))   # second library copy, the one in the playlist
    lib.lists["P1"]["songs"] = ["0000000000000006", "0000000000000004"]
    out = call("music_remove_from_playlist", playlist="workout", songs=["Rush"])
    assert lib.lists["P1"]["songs"] == ["0000000000000004"] and "Old list saved" in out


def test_custom_reorder_keeps_repeats(fakes):
    lib, _, _ = fakes
    lib.lists["P1"]["songs"] = ["0000000000000003", "0000000000000004", "0000000000000003"]
    call("music_reorder_playlist", playlist="workout", by="custom", order=["Idioteque"])
    assert lib.lists["P1"]["songs"] == ["0000000000000004", "0000000000000003", "0000000000000003"]


def test_backups_written_before_destructive_edits(fakes, tmp_path):
    call("music_reorder_playlist", playlist="workout", by="reverse")
    call("music_delete_playlist", playlist="workout", confirm=True)
    names = sorted(p.name for p in (tmp_path / "backups").iterdir())
    assert any(" reorder workout.json" in n for n in names) and any(" delete workout.json" in n for n in names)


def test_play_catalog_checks_before_adding(fakes):
    _, api, _ = fakes
    with pytest.raises(ToolError, match="no song 9999999999"):
        call("music_play_catalog", song_or_album="9999999999", rooms="office")
    with pytest.raises(ToolError, match="catalog song or album id"):
        call("music_play_catalog", song_or_album="../me", rooms="office")
    assert not [c for c in api.calls if c[0] == "library"]


def test_play_catalog_prefers_the_same_album(fakes):
    lib, _, music = fakes
    lib.songs_.append(lib_song(8, "Idioteque", "Radiohead", album="Kid A", added="2026-10-01T16:00:00Z"))
    call("music_play_catalog", song_or_album="1440222222", rooms="office")
    assert music.calls[-1][1]["ids"] == ["0000000000000008"]


def test_catalog_playlist_not_editable(fakes):
    _, api, _ = fakes
    api.playlists_[0]["editable"] = False
    with pytest.raises(ToolError, match="won't let apps add"):
        call("music_add_to_playlist", playlist="workout", songs=["1440111111"])


def test_status_says_which_mac_needs_the_token(fakes, monkeypatch):
    fakes[1].signed_in = False
    monkeypatch.setattr(music_lib_mcp, "HOST", "Edgar")
    out = call("music_status")
    assert "not signed in on Edgar" in out and "secrets.py push-file" in out
