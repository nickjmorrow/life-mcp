import pytest

from appletv import TVError, match_app

APPS = [{"name": "YouTube", "id": "com.google.ios.youtube"}, {"name": "Prime Video", "id": "com.amazon.aiv.AIVApp"},
        {"name": "Disney+", "id": "com.disney.disneyplus"}, {"name": "HBO Max", "id": "com.wbd.stream"},
        {"name": "App\xa0Store", "id": "com.apple.TVAppStore"}, {"name": "TV", "id": "com.apple.TVWatchList"},
        {"name": "Twitch", "id": "tv.twitch"}]


@pytest.mark.parametrize("said,app", [("youtube", "YouTube"), ("prime", "Prime Video"), ("Disney plus", "Disney+"),
                                      ("disney", "Disney+"), ("hbo", "HBO Max"), ("app store", "App\xa0Store"),
                                      ("the tv app", "TV")])
def test_match_app(said, app):
    assert match_app(said, APPS)["name"] == app


def test_match_app_unknown():
    with pytest.raises(TVError, match="No app called 'hulu'. Apps: "):
        match_app("hulu", APPS)


def test_not_paired(tmp_path):
    import asyncio
    from appletv import AppleTV
    with pytest.raises(TVError, match="isn't paired yet"):
        asyncio.run(AppleTV(creds_path=str(tmp_path / "missing.json")).status())


class FakeConf:
    def __init__(self, main_id, ids, address="192.0.2.10"):
        self.identifier, self.all_identifiers, self.address = main_id, ids, address
        self.creds = {}

    def set_credentials(self, proto, value):
        self.creds[proto] = value


class FakeATV:
    def __init__(self, app_raises=False):
        import types
        from pyatv.const import DeviceState, PowerState
        self.app_raises = app_raises
        self.power = types.SimpleNamespace(power_state=PowerState.On)
        playing = types.SimpleNamespace(device_state=DeviceState.Idle, title=None, artist=None)

        async def get():
            return playing
        self.metadata = self
        self.playing = get

    @property
    def app(self):
        if self.app_raises:
            import pyatv.exceptions
            raise pyatv.exceptions.NotSupportedError("app")
        return None

    def close(self):
        return set()


def fake_tv(monkeypatch, tmp_path, confs, atv=None):
    import json

    import appletv
    path = tmp_path / "appletv.json"
    path.write_text(json.dumps({"identifier": "AIRPLAY-ID", "Companion": "c", "AirPlay": "a"}))

    async def scan(loop, hosts=None, timeout=None):
        return confs

    async def connect(conf, loop):
        return atv or FakeATV()
    monkeypatch.setattr(appletv.pyatv, "scan", scan)
    monkeypatch.setattr(appletv.pyatv, "connect", connect)
    return appletv.AppleTV(creds_path=str(path))


def test_found_by_any_identifier(monkeypatch, tmp_path):
    """pyatv's main identifier depends on which services a scan saw; match any of them."""
    import asyncio
    tv = fake_tv(monkeypatch, tmp_path, [FakeConf("COMPANION-ID", ["COMPANION-ID", "AIRPLAY-ID"])])
    assert asyncio.run(tv.status())["power"] == "on"


def test_status_without_app_feature(monkeypatch, tmp_path):
    import asyncio
    tv = fake_tv(monkeypatch, tmp_path, [FakeConf("AIRPLAY-ID", ["AIRPLAY-ID"])], FakeATV(app_raises=True))
    assert asyncio.run(tv.status())["app"] is None


class FakeKeyboard:
    def __init__(self, focused=True, text=""):
        from pyatv.const import KeyboardFocusState
        self.text_focus_state = KeyboardFocusState.Focused if focused else KeyboardFocusState.Unfocused
        self.text, self.calls = text, []

    async def text_get(self):
        return self.text

    async def text_set(self, t):
        self.calls.append(("set", t))
        self.text = t

    async def text_append(self, t):
        self.calls.append(("append", t))
        self.text += t

    async def text_clear(self):
        self.calls.append(("clear",))
        self.text = ""


def keyboard_tv(monkeypatch, tmp_path, kb):
    atv = FakeATV()
    atv.keyboard = kb
    return fake_tv(monkeypatch, tmp_path, [FakeConf("AIRPLAY-ID", ["AIRPLAY-ID"])], atv)


@pytest.mark.parametrize("mode,start,text,want,call", [
    ("replace", "old", "infuse", "infuse", ("set", "infuse")),
    ("append", "inf", "use", "infuse", ("append", "use")),
    ("clear", "old", "", "", ("clear",)),
])
def test_type_text(monkeypatch, tmp_path, mode, start, text, want, call):
    import asyncio
    kb = FakeKeyboard(text=start)
    tv = keyboard_tv(monkeypatch, tmp_path, kb)
    assert asyncio.run(tv.type_text(text, mode)) == want
    assert kb.calls == [call]


def test_type_text_needs_a_focused_field(monkeypatch, tmp_path):
    import asyncio
    kb = FakeKeyboard(focused=False)
    tv = keyboard_tv(monkeypatch, tmp_path, kb)
    with pytest.raises(TVError, match="No text field is selected"):
        asyncio.run(tv.type_text("x", "replace"))
    assert kb.calls == []


def test_read_text(monkeypatch, tmp_path):
    import asyncio
    assert asyncio.run(keyboard_tv(monkeypatch, tmp_path, FakeKeyboard(text="abc")).read_text()) == (True, "abc")
    assert asyncio.run(keyboard_tv(monkeypatch, tmp_path, FakeKeyboard(focused=False)).read_text()) == (False, None)
