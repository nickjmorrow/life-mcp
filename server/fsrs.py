"""FSRS v4 spaced-repetition scheduling, compatible with Logseq's built-in card review.

Logseq (DB version) stores each card's review state on the card block as two properties:
  logseq.property.fsrs/state  {lapses, stability, difficulty, last-repeat (ms), reps, state,
                               logseq/last-rating, elapsed-days, scheduled-days}
  logseq.property.fsrs/due    when the card is next due (ms since the epoch)
This module reads and writes that same shape with FSRS v4's default weights (the ones Logseq uses:
a first "hard" gives stability 0.6 and difficulty 5.87), so a card reviewed here or in Logseq stays on
one schedule. Pure functions: no I/O.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

W = (0.4, 0.6, 2.4, 5.8, 4.93, 0.94, 0.86, 0.01, 1.49, 0.14, 0.94, 2.18, 0.05, 0.34, 1.26, 0.29, 2.61)
RETENTION = 0.9
MAX_INTERVAL_DAYS = 36500
RATINGS = {"again": 1, "hard": 2, "good": 3, "easy": 4}
MINUTE, DAY = 60_000, 86_400_000


@dataclass(frozen=True)
class Card:
    state: str = "new"  # new | learning | review | relearning
    stability: float = 0.0
    difficulty: float = 0.0
    reps: int = 0
    lapses: int = 0
    last_repeat: int = 0  # ms
    elapsed_days: int = 0
    scheduled_days: int = 0
    last_rating: str = ""
    due: int = 0  # ms; 0 = never reviewed (new)

    @classmethod
    def from_logseq(cls, state: dict | None, due: int | None) -> Card:
        if not state:
            return cls()
        return cls(state=state.get("state", "new"), stability=float(state.get("stability", 0)),
                   difficulty=float(state.get("difficulty", 0)), reps=int(state.get("reps", 0)),
                   lapses=int(state.get("lapses", 0)), last_repeat=int(state.get("last-repeat", 0)),
                   elapsed_days=int(state.get("elapsed-days", 0)), scheduled_days=int(state.get("scheduled-days", 0)),
                   last_rating=str(state.get("logseq/last-rating", "")), due=int(due or 0))

    def to_logseq(self) -> dict:
        return {"lapses": self.lapses, "stability": round(self.stability, 4), "difficulty": round(self.difficulty, 4),
                "last-repeat": self.last_repeat, "reps": self.reps, "state": self.state,
                "logseq/last-rating": self.last_rating, "elapsed-days": self.elapsed_days,
                "scheduled-days": self.scheduled_days}


def _clamp_d(d: float) -> float:
    return min(max(d, 1.0), 10.0)


def init_stability(g: int) -> float:
    return max(W[g - 1], 0.1)


def init_difficulty(g: int) -> float:
    return _clamp_d(W[4] - W[5] * (g - 3))


def next_difficulty(d: float, g: int) -> float:
    nd = d - W[6] * (g - 3)
    return _clamp_d(W[7] * W[4] + (1 - W[7]) * nd)  # mean reversion toward the initial "good" difficulty


def retrievability(elapsed_days: float, s: float) -> float:
    return (1 + elapsed_days / (9 * s)) ** -1 if s > 0 else 0.0


def recall_stability(d: float, s: float, r: float, g: int) -> float:
    hard = W[15] if g == 2 else 1.0
    easy = W[16] if g == 4 else 1.0
    return s * (1 + math.exp(W[8]) * (11 - d) * s ** -W[9] * (math.exp(W[10] * (1 - r)) - 1) * hard * easy)


def forget_stability(d: float, s: float, r: float) -> float:
    return W[11] * d ** -W[12] * ((s + 1) ** W[13] - 1) * math.exp(W[14] * (1 - r))


def interval_days(s: float) -> int:
    return int(min(max(round(9 * s * (1 / RETENTION - 1)), 1), MAX_INTERVAL_DAYS))


def review(card: Card, rating: str, now: int) -> Card:
    """The card after rating it now (ms). rating: again | hard | good | easy."""
    g = RATINGS[rating]
    elapsed = max(0, (now - card.last_repeat) // DAY) if card.last_repeat else 0
    base = replace(card, reps=card.reps + 1, last_repeat=now, elapsed_days=int(elapsed), last_rating=rating)

    if card.state == "new" or card.stability <= 0:
        s, d = init_stability(g), init_difficulty(g)
        if g == 4:
            ivl = interval_days(s)
            return replace(base, state="review", stability=s, difficulty=d, scheduled_days=ivl, due=now + ivl * DAY)
        wait = {1: 1, 2: 5, 3: 10}[g]
        return replace(base, state="learning", stability=s, difficulty=d, scheduled_days=0, due=now + wait * MINUTE)

    r = retrievability(elapsed, card.stability)
    d = next_difficulty(card.difficulty, g)

    if card.state in ("learning", "relearning"):
        s = recall_stability(card.difficulty, card.stability, r, g) if g > 1 else card.stability
        if g <= 2:
            wait = 5 if g == 1 else 10
            return replace(base, state=card.state, stability=s, difficulty=d, scheduled_days=0, due=now + wait * MINUTE)
        good = interval_days(recall_stability(card.difficulty, card.stability, r, 3))
        ivl = good if g == 3 else max(interval_days(s), good + 1)
        return replace(base, state="review", stability=s, difficulty=d, scheduled_days=ivl, due=now + ivl * DAY)

    # review
    if g == 1:
        s = forget_stability(card.difficulty, card.stability, r)
        return replace(base, state="relearning", stability=s, difficulty=d, lapses=card.lapses + 1,
                       scheduled_days=0, due=now + 5 * MINUTE)
    s = recall_stability(card.difficulty, card.stability, r, g)
    hard = interval_days(recall_stability(card.difficulty, card.stability, r, 2))
    good = interval_days(recall_stability(card.difficulty, card.stability, r, 3))
    hard, good = min(hard, good), max(good, hard + 1)
    ivl = {2: hard, 3: good, 4: max(interval_days(s), good + 1)}[g]
    return replace(base, state="review", stability=s, difficulty=d, scheduled_days=ivl, due=now + ivl * DAY)


def describe_wait(due: int, now: int) -> str:
    """'in 10 minutes', 'in 3 days', 'tomorrow'."""
    mins = max(0, round((due - now) / MINUTE))
    if mins < 60:
        return f"in {mins} minute{'s' if mins != 1 else ''}"
    days = round((due - now) / DAY)
    if days < 1:
        return f"in {round(mins / 60)} hours"
    return "tomorrow" if days == 1 else f"in {days} days"
