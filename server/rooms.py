"""Rooms for music: a speaker's AirPlay name minus a trailing " (n)". HomePods make a room;
an Apple TV counts when the request says "TV", or when its room has no HomePods of its own
(Music shows HomePods attached to an Apple TV only as that TV)."""
import re
from dataclasses import dataclass

from fastmcp.exceptions import ToolError


class RoomError(ToolError):
    """A room problem to tell Claude about in plain words."""


@dataclass(frozen=True)
class Speaker:
    name: str
    kind: str  # homepod | tv | mac | other
    id: str | None = None  # Music's AirPlay device id (names can repeat: "Living Room" is the TV and the pair)


def room_of(name: str) -> str:
    return re.sub(r"\s*\(\d+\)$", "", name).strip()


def _norm(text: str) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", text.lower()).split()
    return " ".join(w for w in words if w not in ("the", "my", "room's"))


def rooms(speakers: list[Speaker]) -> dict[str, list[Speaker]]:
    out: dict[str, list[Speaker]] = {}
    for s in speakers:
        if s.kind == "homepod":
            out.setdefault(room_of(s.name), []).append(s)
    # Music lists HomePods attached to an Apple TV (e.g. a stereo pair) only as that TV.
    for s in speakers:
        if s.kind == "tv" and room_of(s.name) not in out:
            out[room_of(s.name)] = [s]
    return {k: sorted(v, key=lambda s: s.name) for k, v in sorted(out.items())}


def resolve(request: str | list[str], speakers: list[Speaker]) -> list[Speaker]:
    parts = request if isinstance(request, list) else re.split(r",|\band\b|&", request)
    by_room = rooms(speakers)
    tvs = [s for s in speakers if s.kind == "tv"]
    chosen: list[Speaker] = []
    for part in (p for p in parts if p.strip()):
        key = _norm(part)
        if key in ("everywhere", "all", "all rooms", "whole house", "house"):
            picks = [s for group in by_room.values() for s in group]
        elif key.endswith("tv") or key == "tv":
            place = key.removesuffix("tv").strip()
            picks = [t for t in tvs if not place or _norm(room_of(t.name)) == place] or tvs
            if not picks:
                raise RoomError("There's no TV to play on.")
        else:
            match = [r for r in by_room if _norm(r) == key] or [r for r in by_room if key and key in _norm(r)]
            if len(match) != 1:
                raise RoomError(f"No room called '{part.strip()}'. Rooms: {', '.join(by_room)}.")
            picks = by_room[match[0]]
        chosen += [s for s in picks if s not in chosen]
    if not chosen:
        raise RoomError(f"Say which room. Rooms: {', '.join(by_room)}.")
    return chosen
