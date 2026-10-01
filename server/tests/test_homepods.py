from homepods import kind_of


def test_kind_of():
    assert kind_of("HomePod Mini") == "homepod" and kind_of("HomePod (gen 2)") == "homepod"
    assert kind_of("Apple TV 4K (gen 3)") == "tv"
    assert kind_of("Mac15,8") == "mac" and kind_of(None) == "other"


def test_slow_homepod_times_out(monkeypatch):
    # "Living Room (3)" answered discovery but hung on now-playing live; every call has a time limit.
    import asyncio
    import time
    import pytest
    import homepods

    class SlowMeta:
        async def playing(self):
            await asyncio.sleep(10)

    class SlowATV:
        metadata = SlowMeta()

        def close(self):
            return set()

    async def fake_connect(self, name):
        return SlowATV()

    monkeypatch.setattr(homepods, "CALL_S", 0.2)
    monkeypatch.setattr(homepods.HomePods, "_connect", fake_connect)
    start = time.time()
    with pytest.raises(homepods.HomePodError, match="'Living Room \\(3\\)' didn't answer"):
        asyncio.run(homepods.HomePods().now_playing("Living Room (3)"))
    assert time.time() - start < 2


def test_hanging_close_does_not_block(monkeypatch):
    # Closing a stuck connection must not hang the call (the live probe hung after the fix above).
    import asyncio
    import time
    import homepods

    class Meta:
        async def playing(self):
            class P:
                device_state = type("S", (), {"name": "Idle"})()
                title = artist = album = None
            return P()

        app = None

    class StuckCloseATV:
        metadata = Meta()

        def close(self):
            return {asyncio.ensure_future(asyncio.sleep(10))}

    async def fake_connect(self, name):
        return StuckCloseATV()

    monkeypatch.setattr(homepods, "CLOSE_S", 0.2)
    monkeypatch.setattr(homepods.HomePods, "_connect", fake_connect)
    start = time.time()
    assert asyncio.run(homepods.HomePods().now_playing("Living Room (3)"))["state"] == "idle"
    assert time.time() - start < 2


def test_timeout_does_not_wait_for_a_call_that_ignores_cancellation(monkeypatch):
    # Live: pyatv kept retrying after cancellation, so asyncio.wait_for (which waits for the
    # cancelled call to finish) hung. The limit must return on time and leave the call behind.
    import asyncio
    import time
    import pytest
    import homepods

    class StubbornMeta:
        async def playing(self):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await asyncio.sleep(3)  # ignores the first cancel, like pyatv's retry loop

    class ATV:
        metadata = StubbornMeta()

        def close(self):
            return set()

    async def fake_connect(self, name):
        return ATV()

    monkeypatch.setattr(homepods, "CALL_S", 0.2)
    monkeypatch.setattr(homepods.HomePods, "_connect", fake_connect)

    async def go():
        start = time.time()
        with pytest.raises(homepods.HomePodError, match="didn't answer"):
            await homepods.HomePods().now_playing("Living Room (3)")
        return time.time() - start

    assert asyncio.run(go()) < 1


def test_discovery_has_a_time_limit(monkeypatch):
    # Discovery was the one pyatv call without a limit; a hung scan hung the whole tool live.
    import asyncio
    import time
    import homepods

    async def stuck_scan(loop, timeout=None, hosts=None):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await asyncio.sleep(3)  # ignores the first cancel, like pyatv

    monkeypatch.setattr(homepods.pyatv, "scan", stuck_scan)
    monkeypatch.setattr(homepods, "SCAN_S", 0.1)

    async def go():
        start = time.time()
        found = await homepods.HomePods({"Office": "192.0.2.12"}).discover()
        return found, time.time() - start

    found, took = asyncio.run(go())
    assert took < 1.5
    assert found == {}  # nothing new found; the known addresses are kept for the next call


def test_a_blocking_homepod_call_cannot_freeze_the_server(monkeypatch):
    # Live: pyatv sat in a blocking recvfrom on the event loop's thread, so no asyncio time limit
    # could fire and the whole server froze. Each HomePod call runs in its own thread.
    import asyncio
    import time
    import pytest
    import homepods

    async def blocks(self, name):
        time.sleep(3)  # a blocking socket read, not an await

    monkeypatch.setattr(homepods.HomePods, "_now_playing", blocks)
    monkeypatch.setattr(homepods, "OP_S", 0.3)

    async def go():
        start = time.time()
        with pytest.raises(homepods.HomePodError, match="didn't answer"):
            await homepods.HomePods().now_playing("Office")
        return time.time() - start

    assert asyncio.run(go()) < 1.5


# ── Review fixes ─────────────────────────────────────────────────────────

from types import SimpleNamespace


def conf(name, ip, model="HomePod Mini"):
    return SimpleNamespace(name=name, address=ip, device_info=SimpleNamespace(model_str=model))


def test_address_taken_by_another_speaker_is_not_used(monkeypatch):
    # DHCP gave Kitchen's old address to Office: connect only to the speaker that was asked for.
    import asyncio
    import time
    import homepods
    connected = []

    async def scan(loop, timeout=None, hosts=None):
        if hosts == ["10.0.0.1"]:
            return [conf("Office", "10.0.0.1")]
        if hosts == ["10.0.0.2"]:
            return [conf("Kitchen", "10.0.0.2")]
        return [conf("Kitchen", "10.0.0.2"), conf("Office", "10.0.0.1")]

    class ATV:
        class metadata:
            app = None

            @staticmethod
            async def playing():
                return SimpleNamespace(device_state=SimpleNamespace(name="Idle"), title=None, artist=None, album=None)

        def close(self):
            return set()

    async def connect(c, loop):
        connected.append(c.name)
        return ATV()

    monkeypatch.setattr(homepods.pyatv, "scan", scan)
    monkeypatch.setattr(homepods.pyatv, "connect", connect)
    h = homepods.HomePods({"Kitchen": "10.0.0.1"})
    h._known, h._scanned_at = {"Kitchen": {"ip": "10.0.0.1"}}, time.monotonic()  # a fresh-looking stale cache
    assert asyncio.run(h.now_playing("Kitchen"))["state"] == "idle"
    assert connected == ["Kitchen"]


def test_result_arrives_before_the_worker_finishes_cleaning_up():
    # asyncio.run cancels and waits for leftovers; the answer must not wait for that.
    import asyncio
    import time
    import homepods

    async def make():
        async def stubborn():
            end = time.time() + 3
            while time.time() < end:
                try:
                    await asyncio.sleep(0.05)
                except asyncio.CancelledError:
                    pass  # ignores every cancel for 3 s
        asyncio.ensure_future(stubborn())
        return "ok"

    async def go():
        start = time.time()
        assert await homepods._isolated("Office", make, 5) == "ok"
        return time.time() - start

    assert asyncio.run(go()) < 1


def test_too_many_stuck_threads_restarts_the_service(monkeypatch):
    import asyncio
    import threading
    import time
    import pytest
    import homepods
    exits = []

    class Exited(Exception):
        pass

    def fake_exit(code):
        exits.append(code)
        raise Exited()

    stuck = [threading.Thread(target=time.sleep, args=(2,), name=f"pyatv stuck {i}", daemon=True) for i in range(3)]
    for t in stuck:
        t.start()
    monkeypatch.setattr(homepods, "MAX_STUCK_THREADS", 3)
    monkeypatch.setattr(homepods.os, "_exit", fake_exit)

    async def make():
        return "ok"

    with pytest.raises(Exited):
        asyncio.run(homepods._isolated("Office", make, 1))
    assert exits == [1]
