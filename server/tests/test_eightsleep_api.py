import asyncio
import json

import pytest

from eightsleep_api import EightSleepError
from fake_eightsleep import FakeAPI, make_api


def run(coro):
    return asyncio.run(coro)


def test_uses_saved_token_and_uid(tmp_path):
    fake = FakeAPI()
    fake.routes[("GET", "app:/v1/users/U1/temperature")] = (200, {"currentLevel": -10})
    api = make_api(tmp_path, fake)
    assert run(api.request("GET", "app", "/v1/users/{uid}/temperature")) == {"currentLevel": -10}
    assert fake.logins == 0


def test_expired_token_logs_in_and_saves(tmp_path):
    fake = FakeAPI()
    api = make_api(tmp_path, fake, expired=True)
    run(api.request("GET", "app", "/v1/users/{uid}/temperature"))
    assert fake.logins == 1
    saved = json.loads((tmp_path / ".eight-sleep-mcp" / "tokens.json").read_text())
    assert saved["access_token"] == "NEW1" and saved["user_id"] == "U1" and saved["expires_at"] > 0
    login = [c for c in fake.calls if c[1].startswith("auth:")][0][2]
    assert login == {"client_id": "cid", "client_secret": "csec", "grant_type": "password",
                     "username": "e", "password": "p"}


def test_401_relogs_once_and_saves(tmp_path):
    fake = FakeAPI()
    fake.expire_once = True
    api = make_api(tmp_path, fake)
    run(api.request("GET", "app", "/v1/users/{uid}/temperature"))
    assert fake.logins == 1 and len(fake.api_calls()) == 2


def test_error_message_plain(tmp_path):
    fake = FakeAPI()
    fake.routes[("PUT", "app:/v1/users/U1/temperature")] = (400, {"message": "level out of range"})
    with pytest.raises(EightSleepError, match=r"Eight Sleep didn't accept that \(HTTP 400\): level out of range"):
        run(make_api(tmp_path, fake).request("PUT", "app", "/v1/users/{uid}/temperature", {"currentLevel": 999}))


def test_subscription_message(tmp_path):
    fake = FakeAPI()
    fake.routes[("GET", "app:/v2/users/U1/alarms")] = (403, {"message": "Subscription required"})
    with pytest.raises(EightSleepError, match="needs an Autopilot subscription"):
        run(make_api(tmp_path, fake).request("GET", "app", "/v2/users/{uid}/alarms"))


def test_allow_404(tmp_path):
    fake = FakeAPI()
    fake.routes[("GET", "app:/v1/users/U1/temperature/nap-mode/status")] = (404, {"message": "none"})
    assert run(make_api(tmp_path, fake).request("GET", "app", "/v1/users/{uid}/temperature/nap-mode/status",
                                                allow_404=True)) is None


def test_mutation_gate(tmp_path):
    api = make_api(tmp_path, FakeAPI(), mutations=False)
    with pytest.raises(EightSleepError, match="Changing the bed is switched off"):
        api.require_mutations()


def test_missing_login(tmp_path):
    fake = FakeAPI()
    api = make_api(tmp_path, fake, expired=True)
    (tmp_path / ".eight-sleep-mcp" / "config.json").write_text("{}")
    with pytest.raises(EightSleepError, match="login isn't set up"):
        run(api.request("GET", "app", "/v1/users/{uid}/temperature"))
