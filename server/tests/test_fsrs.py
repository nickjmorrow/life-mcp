"""FSRS v4 scheduling matches what Logseq itself records (seen on real cards, 2026-10-01)."""
import fsrs

NOW = 1_790_000_000_000


def test_new_card_hard_matches_logseq():
    c = fsrs.review(fsrs.Card(), "hard", NOW)
    assert (c.state, c.stability, round(c.difficulty, 2), c.reps) == ("learning", 0.6, 5.87, 1)
    assert c.due - NOW == 5 * fsrs.MINUTE


def test_new_card_again_matches_logseq():
    c = fsrs.review(fsrs.Card(), "again", NOW)
    assert (c.stability, round(c.difficulty, 2), c.due - NOW) == (0.4, 6.81, fsrs.MINUTE)


def test_easy_new_card_graduates():
    c = fsrs.review(fsrs.Card(), "easy", NOW)
    assert c.state == "review" and c.scheduled_days == fsrs.interval_days(5.8) and c.due == NOW + c.scheduled_days * fsrs.DAY


def test_learning_good_graduates_and_review_grows():
    c = fsrs.review(fsrs.Card(), "good", NOW)
    c = fsrs.review(c, "good", c.due)
    assert c.state == "review" and c.scheduled_days >= 1
    later = fsrs.review(c, "good", c.due)
    assert later.scheduled_days > c.scheduled_days and later.stability > c.stability


def test_review_again_lapses_to_relearning():
    c = fsrs.review(fsrs.review(fsrs.Card(), "easy", NOW), "again", NOW + 6 * fsrs.DAY)
    assert (c.state, c.lapses) == ("relearning", 1) and c.due - (NOW + 6 * fsrs.DAY) == 5 * fsrs.MINUTE


def test_review_intervals_are_ordered():
    card = fsrs.review(fsrs.Card(), "easy", NOW)
    t = card.due
    ivls = {r: fsrs.review(card, r, t).scheduled_days for r in ("hard", "good", "easy")}
    assert ivls["hard"] < ivls["good"] < ivls["easy"]


def test_round_trip_logseq_shape():
    c = fsrs.review(fsrs.Card(), "good", NOW)
    back = fsrs.Card.from_logseq(c.to_logseq(), c.due)
    assert back == c
    assert set(c.to_logseq()) == {"lapses", "stability", "difficulty", "last-repeat", "reps", "state",
                                  "logseq/last-rating", "elapsed-days", "scheduled-days"}


def test_describe_wait():
    assert fsrs.describe_wait(NOW + 10 * fsrs.MINUTE, NOW) == "in 10 minutes"
    assert fsrs.describe_wait(NOW + fsrs.DAY, NOW) == "tomorrow"
    assert fsrs.describe_wait(NOW + 3 * fsrs.DAY, NOW) == "in 3 days"
