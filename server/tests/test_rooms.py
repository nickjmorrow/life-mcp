import pytest

from rooms import RoomError, Speaker, resolve, room_of, rooms

SPEAKERS = [Speaker("Bedroom", "homepod"), Speaker("Kitchen", "homepod"), Speaker("Office", "homepod"),
            Speaker("Living Room (2)", "homepod"), Speaker("Living Room (3)", "homepod"),
            Speaker("Living Room", "tv"), Speaker("TestMac", "mac")]


def names(xs):
    return [s.name for s in xs]


def test_room_of():
    assert room_of("Living Room (2)") == "Living Room" and room_of("Office") == "Office"


def test_rooms_groups_homepods_only():
    r = rooms(SPEAKERS)
    assert names(r["Living Room"]) == ["Living Room (2)", "Living Room (3)"]
    assert "TestMac" not in r


def test_living_room_excludes_tv():
    assert names(resolve("the living room", SPEAKERS)) == ["Living Room (2)", "Living Room (3)"]


def test_tv_when_asked():
    assert names(resolve("living room tv", SPEAKERS)) == ["Living Room"]
    assert names(resolve("the TV", SPEAKERS)) == ["Living Room"]


def test_several_and_everywhere():
    assert names(resolve(["kitchen", "Office"], SPEAKERS)) == ["Kitchen", "Office"]
    assert names(resolve("kitchen and office", SPEAKERS)) == ["Kitchen", "Office"]
    assert len(resolve("everywhere", SPEAKERS)) == 5


def test_unknown_room():
    with pytest.raises(RoomError, match="No room called 'garage'. Rooms: Bedroom, Kitchen, Living Room, Office"):
        resolve("garage", SPEAKERS)


MUSIC_LIST = [Speaker("TestMac", "mac"), Speaker("Bedroom", "homepod"), Speaker("Kitchen", "homepod"),
              Speaker("Living Room", "tv"), Speaker("Office", "homepod")]


def test_room_with_only_a_tv_is_the_tv():
    # Music lists the living-room HomePod pair only through the Apple TV they're attached to.
    assert names(resolve("the living room", MUSIC_LIST)) == ["Living Room"]
    assert names(resolve("everywhere", MUSIC_LIST)) == ["Bedroom", "Kitchen", "Living Room", "Office"]


def test_duplicate_living_room_prefers_the_homepod_pair():
    # Music lists both the Apple TV and the stereo pair as "Living Room" (seen live).
    music = [Speaker("TestMac", "mac", "33"), Speaker("Office", "homepod", "101"), Speaker("Living Room", "tv", "100"),
             Speaker("Bedroom", "homepod", "98"), Speaker("Kitchen", "homepod", "99"),
             Speaker("Living Room", "homepod", "56945"), Speaker("AirPods Max", "other", "56937")]
    picked = resolve("the living room", music)
    assert [(s.name, s.kind, s.id) for s in picked] == [("Living Room", "homepod", "56945")]
    assert [s.id for s in resolve("living room tv", music)] == ["100"]
