import asyncio
import json
import stat

import pytest

import music_app
from music_app import MusicApp, MusicError, run_jxa


# Logs one line per osascript call: "JXA <json arg>" or "AS <argv after the script>".
LOGGER = ('if [ "$1" = "-l" ]; then for a; do last="$a"; done; echo "JXA $last" >> LOG; '
          'else shift 2; echo "AS $*" >> LOG; fi')


def fake_osascript(tmp_path, monkeypatch, body):
    script = tmp_path / "osascript"
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(music_app, "OSASCRIPT", str(script))


def test_user_text_only_in_argv(tmp_path, monkeypatch):
    # The stand-in echoes its last argument (the JSON arg) so we can see it came via argv.
    fake_osascript(tmp_path, monkeypatch, 'for a; do last="$a"; done; echo "$last"')
    out = asyncio.run(run_jxa("function run(argv) {}", {"name": "Robert'); do shell script(\"rm -rf ~\")"}))
    assert out == {"name": "Robert'); do shell script(\"rm -rf ~\")"}


def test_not_authorized_message(tmp_path, monkeypatch):
    fake_osascript(tmp_path, monkeypatch, 'echo "execution error: Not authorized to send Apple events to Music. (-1743)" >&2; exit 1')
    with pytest.raises(MusicError, match="Privacy & Security → Automation"):
        asyncio.run(run_jxa("x"))


def test_other_error(tmp_path, monkeypatch):
    fake_osascript(tmp_path, monkeypatch, 'echo "execution error: Error: No playlist called X (-2700)" >&2; exit 1')
    with pytest.raises(MusicError, match="No playlist called X"):
        asyncio.run(run_jxa("x"))


def test_play_sets_speakers_then_plays(tmp_path, monkeypatch):
    # Every call's arguments are appended to a log, one line per call.
    log = tmp_path / "calls.txt"
    fake_osascript(tmp_path, monkeypatch, LOGGER.replace("LOG", str(log)) + '; echo "\\"ok\\""')
    asyncio.run(MusicApp().play({"kind": "playlist", "name": "Chill"}, ["99", "56945"], True))
    assert log.read_text().splitlines() == [
        "AS 99 56945",   # AppleScript ticks the speakers by Music's id; ids only as argv
        'JXA {"target": {"kind": "playlist", "name": "Chill"}, "shuffle": true, "queue": "Claude Queue", '
        '"marker": ' + json.dumps(music_app.QUEUE_MARKER) + ', "cap": 100}']


def test_airplay_devices_parsed(tmp_path, monkeypatch):
    fake_osascript(tmp_path, monkeypatch, "printf '33\\tTestMac\\tcomputer\\ttrue\\tfalse\\ttrue\\t100\\n100\\tLiving Room\\tApple TV\\tfalse\\tfalse\\ttrue\\t35\\n'")
    assert asyncio.run(MusicApp().airplay_devices()) == [
        {"id": "33", "name": "TestMac", "kind": "computer", "selected": True, "active": False, "available": True, "volume": 100},
        {"id": "100", "name": "Living Room", "kind": "Apple TV", "selected": False, "active": False, "available": True, "volume": 35}]


def test_set_volume_argv(tmp_path, monkeypatch):
    log = tmp_path / "calls.txt"
    fake_osascript(tmp_path, monkeypatch, LOGGER.replace("LOG", str(log)) + '; echo 20')
    assert asyncio.run(MusicApp().set_device_volume("101", 20)) == 20
    assert log.read_text().splitlines() == ["AS 101 20"]


def test_scripts_use_musics_dictionary_names():
    # JXA names come from Music's sdef: "AirPlay devices" → airPlayDevices (capital P).
    # A wrong case gives "Message not understood" only on the real app (seen live 2026-09-27).
    import re
    import subprocess
    sdef = subprocess.run(["sdef", "/System/Applications/Music.app"], capture_output=True, text=True).stdout
    if not sdef:
        pytest.skip("Music's scripting dictionary isn't available here")

    def jxa(term):
        words = term.split()
        return words[0][0].lower() + words[0][1:] + "".join(w[0].upper() + w[1:] for w in words[1:])

    known = {jxa(t) for t in re.findall(r'<(?:property|command|class|element)[^>]* name="([^"]+)"', sdef)}
    known |= {jxa(t) for t in re.findall(r'plural="([^"]+)"', sdef)}
    used = set()
    for name in ("STATE", "PLAYLISTS", "SEARCH", "PLAY", "CONTROL"):
        used |= set(re.findall(r"\bM\.([A-Za-z]+)", getattr(music_app, name)))
    assert used - {"make", "search", "pause", "play"} <= known, sorted(used - known)



def test_queue_playlist_is_guarded():
    # Only a playlist carrying our marker is ever emptied; a user's own "Claude Queue" or a smart
    # playlist is refused. Emptying is one event and the fill is capped (big artists timed out).
    src = music_app.PLAY
    assert "a.marker" in src and ".description()" in src and ".smart()" in src
    assert "q.tracks.delete()" in src and "slice(0, a.cap)" in src
    assert "forEach(x => x.delete())" not in src
