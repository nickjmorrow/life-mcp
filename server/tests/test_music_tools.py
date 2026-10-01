import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import music_mcp
from fake_music import FakeMusic, FakePods


def setup(music=None, pods=None):
    music_mcp._music, music_mcp._pods = music or FakeMusic(), pods or FakePods()
    return music_mcp._music, music_mcp._pods


def call(tool, **args):
    async def go():
        async with Client(music_mcp.mcp) as client:
            return (await client.call_tool(tool, args)).content[0].text
    return asyncio.run(go())


def test_play_playlist_in_rooms():
    m, _ = setup()
    assert call("music_play", what="chill", rooms="kitchen and office") == \
        "Playing playlist Chill in Kitchen, Office"
    assert m.calls == [("play", {"kind": "playlist", "name": "Chill"}, ["99", "101"], None)]


def test_play_prefers_playlist():
    m, _ = setup()
    call("music_play", what="Chill", rooms="bedroom")
    assert m.calls[0][1]["kind"] == "playlist"
    call("music_play", what="Chill", rooms="bedroom", kind="album")
    assert m.calls[1][1] == {"kind": "album", "name": "Chill"}


def test_play_artist_from_search():
    m, _ = setup()
    # "Living Room" is both the Apple TV and the HomePod pair in Music's list; the room means the pair.
    assert call("music_play", what="radiohead", rooms="the living room", shuffle=True) == \
        "Playing artist Radiohead in Living Room, shuffled"
    assert m.calls[0] == ("play", {"kind": "artist", "name": "Radiohead"}, ["56945"], True)


def test_play_nothing_found():
    setup()
    with pytest.raises(ToolError, match="Nothing called 'mozart' in your library"):
        call("music_play", what="mozart", rooms="office")


def test_pause_routes_to_music_when_mac_playing():
    m, p = setup(FakeMusic(state="playing", devices_playing=["Kitchen"]))
    assert call("music_pause") == "Paused TestMac's music in Kitchen"
    assert m.calls == [("control", "pause")] and p.calls == []


def test_pause_routes_to_homepods_when_mac_idle():
    m, p = setup(FakeMusic(), FakePods(playing={"Bedroom"}))
    assert call("music_pause") == "Paused Bedroom"
    assert p.calls == [("control", "Bedroom", "pause")] and m.calls == []


def test_next_in_named_room_from_phone():
    _, p = setup(FakeMusic(), FakePods(playing={"Office"}))
    call("music_next", room="office")
    assert p.calls == [("control", "Office", "next")]


def test_set_volume_absolute_via_music_when_mac_plays_there():
    m, p = setup(FakeMusic(state="playing", devices_playing=["Office"]))
    assert call("music_set_volume", room="office", level=20) == "Office: volume 20"
    assert m.calls == [("volume", "101", 20)] and p.calls == []


def test_set_volume_relative_via_homepods():
    _, p = setup()
    assert call("music_set_volume", room="kitchen", change=-10) == "Kitchen: volume 30"
    assert p.calls == [("volume", "Kitchen", 30.0)]


def test_volume_everywhere_one_offline():
    _, p = setup(pods=FakePods(offline={"Bedroom"}))
    out = call("music_set_volume", room="everywhere", level=25)
    assert out.startswith("Volume 25 in Kitchen, Living Room (2), Living Room (3), Office")
    assert "Bedroom didn't respond" in out


def test_now_playing_all():
    setup(FakeMusic(state="playing", devices_playing=["Kitchen"]), FakePods(playing={"Office"}))
    out = call("music_now_playing")
    assert "Kitchen: playing Idioteque — Radiohead (Kid A), from TestMac" in out
    assert "Office: playing Song From Phone — Artist (Album)" in out
    assert "Bedroom: idle" in out


def test_move():
    m, _ = setup(FakeMusic(state="playing", devices_playing=["Kitchen"]))
    assert call("music_move", rooms="bedroom") == "Moved the music to Bedroom"
    assert m.calls == [("set_devices", ["98"])]


def test_move_when_nothing_playing():
    setup()
    with pytest.raises(ToolError, match="isn't playing anything to move"):
        call("music_move", rooms="bedroom")


def test_list_speakers_and_playlists_and_search():
    setup(FakeMusic(state="playing", devices_playing=["Kitchen"]))
    speakers = call("music_list_speakers")
    assert "Kitchen (HomePod): online, volume 40, TestMac playing here" in speakers
    assert "Living Room TV (Apple TV): online" in speakers
    assert call("music_list_playlists") == "Chill\nWorkout"
    assert "Idioteque — Radiohead (Kid A)" in call("music_search_library", query="radio")


def test_now_playing_living_room_from_the_mac():
    # TestMac plays to "Living Room" (the Apple TV target); the pair's HomePods show TestMac's track.
    setup(FakeMusic(state="playing", devices_playing=["Living Room"]))
    out = call("music_now_playing", room="living room")
    assert out == ("Living Room (2): playing Idioteque — Radiohead (Kid A), from TestMac\n"
                   "Living Room (3): playing Idioteque — Radiohead (Kid A), from TestMac")


def test_volume_living_room_while_mac_plays_there():
    m, p = setup(FakeMusic(state="playing", devices_playing=["Living Room"]))
    assert call("music_set_volume", room="living room", level=25) == "Living Room: volume 25"
    assert m.calls == [("volume", "56945", 25)] and p.calls == []



# ── Review fixes ─────────────────────────────────────────────────────────


def test_pause_after_mac_paused_goes_to_the_phones_music():
    # Music keeps Office ticked after TestMac paused there; the phone now plays in the Kitchen.
    m, p = setup(FakeMusic(state="paused", devices_playing=["Office"]), FakePods(playing={"Kitchen"}))
    assert call("music_pause") == "Paused Kitchen"
    assert p.calls == [("control", "Kitchen", "pause")] and m.calls == []


def test_pause_everything_pauses_mac_and_the_phone():
    m, p = setup(FakeMusic(state="playing", devices_playing=["Office"]), FakePods(playing={"Kitchen"}))
    assert call("music_pause") == "Paused TestMac's music in Office; Kitchen"
    assert m.calls == [("control", "pause")] and p.calls == [("control", "Kitchen", "pause")]


def test_pause_named_room_not_mac_goes_to_homepod():
    m, p = setup(FakeMusic(state="playing", devices_playing=["Office"]), FakePods(playing={"Kitchen"}))
    assert call("music_pause", room="kitchen") == "Paused Kitchen"
    assert m.calls == [] and p.calls == [("control", "Kitchen", "pause")]


def test_resume_still_goes_to_mac_when_paused():
    m, p = setup(FakeMusic(state="paused", devices_playing=["Office"]))
    assert call("music_resume") == "Resumed TestMac's music in Office"
    assert m.calls == [("control", "resume")] and p.calls == []


def test_list_speakers_uses_homepod_volume_and_matches_the_pair_not_the_tv():
    _, p = setup(FakeMusic(state="playing", devices_playing=["Living Room"]))
    p.volumes["Living Room (2)"] = 62.0
    out = call("music_list_speakers")
    assert "Living Room (2) (HomePod): online, volume 62, TestMac playing here" in out
    assert "Living Room TV (Apple TV): online" in out
    assert "Living Room TV (Apple TV): online, volume" not in out and \
        "Living Room TV (Apple TV): online, TestMac playing here" not in out


def test_tv_volume_does_not_change_the_pair():
    m, p = setup(FakeMusic(state="playing", devices_playing=["Living Room"]), FakePods())
    p.volumes["Living Room"] = 30.0
    call("music_set_volume", room="living room tv", level=20)
    assert m.calls == [] and p.calls == [("volume", "Living Room", 20.0)]


def test_play_exact_artist_beats_partial_playlist():
    m, _ = setup()
    m.lib_playlists.append("Radiohead deep cuts")
    assert call("music_play", what="radiohead", rooms="office") == "Playing artist Radiohead in Office"


def test_play_partial_playlist_when_nothing_exact():
    m, _ = setup()
    assert call("music_play", what="work", rooms="office") == "Playing playlist Workout in Office"


def test_play_explicit_kind_must_match():
    setup()
    with pytest.raises(ToolError, match="No album called 'Nope' in your library"):
        call("music_play", what="Nope", rooms="office", kind="album")


def test_play_blank_refused():
    setup()
    with pytest.raises(ToolError, match="Say what to play"):
        call("music_play", what="  ", rooms="office")
