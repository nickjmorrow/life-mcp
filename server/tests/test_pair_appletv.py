import asyncio
import json
import os
import stat

import pytest

import pair_appletv

pair_appletv.TV = "192.0.2.10"  # documentation address; the private config has the real one


class FakeConf:
    identifier, all_identifiers = "AIRPLAY-ID", ["AIRPLAY-ID", "COMPANION-ID"]


class FakePairing:
    def __init__(self, proto, fail):
        self.proto, self.fail = proto, fail
        self.device_provides_pin, self.has_paired = True, False
        self.service = type("S", (), {"credentials": f"{proto.name}-creds"})()

    async def begin(self):
        pass

    def pin(self, pin):
        self.got = pin

    async def finish(self):
        if self.fail:
            raise RuntimeError("wrong PIN")
        self.has_paired = True

    async def close(self):
        pass


def setup(monkeypatch, tmp_path, fail_airplay=False):
    from pyatv.const import Protocol
    out = tmp_path / "appletv.json"
    monkeypatch.setattr(pair_appletv, "OUT", str(out))
    monkeypatch.setattr(pair_appletv, "WAIT_S", 2)

    async def scan(loop, hosts=None, timeout=None):
        return [FakeConf()]

    async def pair(conf, proto, loop, name=None):
        return FakePairing(proto, fail_airplay and proto == Protocol.AirPlay)
    monkeypatch.setattr(pair_appletv.pyatv, "scan", scan)
    monkeypatch.setattr(pair_appletv.pyatv, "pair", pair)
    return out


def test_saves_each_protocol_as_it_goes(monkeypatch, tmp_path):
    out = setup(monkeypatch, tmp_path, fail_airplay=True)
    prefix = str(tmp_path / "pin")
    for p in ("companion", "airplay"):
        open(f"{prefix}.{p}", "w").write("1234")
    monkeypatch.setattr(pair_appletv, "_fresh_pin", lambda path, since: "1234")
    with pytest.raises(SystemExit, match="AirPlay pairing failed"):
        asyncio.run(pair_appletv.main(prefix))
    saved = json.loads(out.read_text())
    assert saved["Companion"] == "Companion-creds" and "AirPlay" not in saved
    assert saved["all_identifiers"] == ["AIRPLAY-ID", "COMPANION-ID"]
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600


def test_ignores_an_old_pin_file(tmp_path):
    path = tmp_path / "pin.companion"
    path.write_text("9999")
    since = os.path.getmtime(path) + 1
    assert pair_appletv._fresh_pin(str(path), since, wait_s=1) is None


def test_existing_file_made_private(monkeypatch, tmp_path):
    out = setup(monkeypatch, tmp_path)
    out.write_text("{}")
    os.chmod(out, 0o644)
    monkeypatch.setattr(pair_appletv, "_fresh_pin", lambda path, since: "1234")
    asyncio.run(pair_appletv.main(str(tmp_path / "pin")))
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600


def test_pin_timeout_and_typo(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    monkeypatch.setattr(pair_appletv, "_fresh_pin", lambda path, since: None)
    with pytest.raises(SystemExit, match="No PIN arrived"):
        asyncio.run(pair_appletv.main(str(tmp_path / "pin")))
    monkeypatch.setattr(pair_appletv, "_fresh_pin", lambda path, since: "12a4")
    with pytest.raises(SystemExit, match="isn't a 4-digit PIN"):
        asyncio.run(pair_appletv.main(str(tmp_path / "pin")))


def test_corrupt_file(monkeypatch, tmp_path):
    out = setup(monkeypatch, tmp_path)
    out.write_text("{not json")
    with pytest.raises(SystemExit, match="isn't valid JSON"):
        asyncio.run(pair_appletv.main(str(tmp_path / "pin")))
