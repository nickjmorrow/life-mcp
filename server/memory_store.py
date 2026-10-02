"""Shared memory as files: Markdown topic files in a local git repo.

Under the root folder (700; every file 600):

    core.md              the few facts that change most answers; its entries stay within CORE_LIMIT characters
    topics/<name>.md     one file per topic, loaded when a chat touches it
    archive.md           facts that ended or were replaced; never loaded, found by asking for it by name
    .ids.json            the highest number issued so far for each id letter, so no id ever comes back, even after
                         the entry and its archive note are both removed

A topic file is a header, then entries (two spaces of indent per level of detail), with optional `## group` lines:

    # garden (g): plants, beds and tools; load for planting, watering or tools
    - [g1] grows tomatoes along the south fence (2026-09-27, phone)
      - [g2] a detail under it
    ## Linear
    - [g3] a fact about one app (2026-09-28, claude code, review 2026-11-01)

The header is `(g)` or `(g, reviewed)` for topics whose facts go out of date. Entries keep their id for life, even
when they move to another topic, and an id is never used twice: a new id is the highest number seen anywhere for
its letter (in every file, in the archive's "was h7" notes, and in `.ids.json`) plus one, and every write that issues
an id saves the new highest numbers in `.ids.json` in the same commit. The archive holds entries as
`- [z4] (was h7, archived 2027-01-05: no longer true) the text (date, source)`.

Every write reads the files, changes them and commits once, under an exclusive lock on `<root>/.lock`, so a phone
chat and a job writing at the same moment can't lose each other's changes or share an id. The files are replaced
atomically; if the commit fails, the files go back as they were. Reading takes no lock. `git add -A` takes in
anything edited by hand, too. git runs apart from the user's own setup: no global or system git config, no GIT_*
variables, a fixed author, no signing, and commits skip hooks.

Text is flattened to one line and refused if it has invisible control characters. A caller's `guard` sees every new
text before anything is written. Refusals raise MemoryError_, worded for the model to read.
"""
import dataclasses
import datetime as dt
import difflib
import fcntl
import json
import os
import re
import string
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

CORE = "core"
ARCHIVE = "archive"
CORE_LIMIT = 2500  # characters of entries; a write that would take core past it is refused
TOPIC_SOFT = 4000  # a save past it still works, and says a split is due
MAX_INDEX_LINES = 20  # the topic list (core counts as one line) never grows past this
SAME_RATIO = 0.95  # a repeat of an existing entry is skipped
SIMILAR_RATIO = 0.6  # a near-duplicate is held until the caller says add or replace
DEFAULT_REVIEW_DAYS = 90  # in a reviewed topic an entry is due this long after its date, unless it has its own

_CORE_PREFIX = "c"
_ARCHIVE_PREFIX = "z"
_CORE_ABOUT = "facts that change most answers"
_ARCHIVE_ABOUT = "facts that ended or were replaced, kept for reference; never loaded"
_HELD_SHOWN = 3  # how many similar entries a held save lists
_SNIPPET = 100  # characters of an entry shown in a held save
_GROUP_MAX = 60  # characters in a group name: it becomes a `## ...` line, which can sit in core
_GIT_TIMEOUT = 60  # seconds; a stuck git must not hold the lock forever
_LOCK = ".lock"
_MARKS = ".ids.json"
_AUTHOR = "Life connector"
_EMAIL = "life@localhost"

_NAME = re.compile(r"^[a-z][a-z0-9-]{0,29}$")
_ID = re.compile(r"^[a-z][1-9][0-9]*$")
_ENTRY = re.compile(r"^((?:  )*)- \[([a-z][1-9][0-9]*)\] (\S.*)$")
_GROUP = re.compile(r"^## (\S.*)$")
_HEADER = re.compile(r"^# (\S+) \(([a-z])(, reviewed)?\):[ ]?(.*)$")
_TAIL = re.compile(r"^(.*) \(([^()]*)\)$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REVIEW = re.compile(r"^review (\d{4}-\d{2}-\d{2})$")
_WAS = re.compile(r"^\(was ([a-z][1-9][0-9]*)[,)]")
_SOURCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$")
# Zero-width and text-direction controls can hide text from a reader; they're refused, never stripped.
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff\U000e0000-\U000e007f]")


class MemoryError_(ValueError):
    """A refusal, worded for the model to read (the MCP layer turns it into a ToolError)."""


@dataclasses.dataclass
class Entry:
    id: str
    text: str
    date: str | None  # when it was saved or last confirmed ("still true")
    source: str | None
    depth: int  # 0 for a top-level entry, 1 for a detail under one, and so on
    under: str | None  # the `## group` it sits in
    review: str | None = None  # an explicit date to check it again


@dataclasses.dataclass
class Topic:
    name: str
    prefix: str
    about: str
    count: int  # entries, details included
    reviewed: bool = False  # its entries come due for a "still true?" check


def today() -> str:
    """Today's date. A function so tests can move the clock."""
    return dt.date.today().isoformat()


# ---- small helpers ---------------------------------------------------------------------------------------------


def _clean(text: str, what: str) -> str:
    """One line of plain text: whitespace (newlines included) collapsed, invisible characters refused."""
    bad = _INVISIBLE.search(text)
    if bad:
        raise MemoryError_(f"The {what} has an invisible character (U+{ord(bad.group()):04X}), which isn't allowed "
                           "because it can hide text from a reader. Retype it without.")
    text = " ".join(text.split())
    if not text:
        raise MemoryError_(f"The {what} is empty.")
    return text


def _date(value: str, what: str) -> str:
    value = value.strip()
    try:
        if not _DATE.match(value):
            raise ValueError(value)
        dt.date.fromisoformat(value)
    except ValueError:
        raise MemoryError_(f"{what} must be a date like 2026-11-05 (year-month-day).") from None
    return value


def _source(value: str) -> str:
    value = " ".join(str(value).split())
    if not _SOURCE.match(value) or _REVIEW.match(value):  # "review <date>" would read back as a review date
        raise MemoryError_("The source should be a short plain label such as phone, web or claude code "
                           "(letters and numbers, no commas or brackets).")
    return value


def _entry_id(raw: str) -> str:
    cleaned = str(raw).strip().strip("[]").strip().lower()
    if not _ID.match(cleaned):
        raise MemoryError_(f"'{raw}' isn't in memory. Entry ids look like h12: a letter and a number.")
    return cleaned


def _topic_name(raw: str) -> str:
    return " ".join(str(raw).split()).lower()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip(" .")


def _ratio(a: str, b: str) -> float:
    """difflib similarity, or 0.0 when it can't reach SIMILAR_RATIO. difflib's junk heuristic (on by default for text
    over 200 characters) can score a near-identical pair around 0.07, so it's off."""
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if matcher.real_quick_ratio() < SIMILAR_RATIO or matcher.quick_ratio() < SIMILAR_RATIO:
        return 0.0
    return matcher.ratio()


def _where(topic: str, e: Entry) -> str:
    return topic + (f", under {e.under}" if e.under else "")


def _snippet(text: str) -> str:
    return text if len(text) <= _SNIPPET else text[:_SNIPPET - 1] + "\u2026"


def _parse_date(value: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _note(seen: dict[str, int], entry_id: str) -> None:
    """Record that this id's number has been used."""
    seen[entry_id[0]] = max(seen.get(entry_id[0], 0), int(entry_id[1:]))


def _parse_similar(similar: str | None) -> tuple[bool, str | None]:
    """(add, replace_id) from what the caller said about similar entries."""
    if similar is None or not similar.strip():
        return False, None
    value = similar.strip().lower()
    if value == "add":
        return True, None
    if value.startswith("replace:"):
        target = value[len("replace:"):].strip().strip("[]")
        if _ID.match(target):
            return False, target
    raise MemoryError_(f"similar must be 'add' (keep both) or 'replace:<id>' (close the old entry), not '{similar}'.")


def _split_meta(rest: str) -> tuple[str, str | None, str | None, str | None]:
    """(text, date, source, review) from what follows an entry's id. The trailing (...) is only metadata if it
    starts with a date, so a text that ends in parentheses stays whole."""
    tail = _TAIL.match(rest)
    if tail:
        parts = tail.group(2).split(", ")
        if _DATE.match(parts[0]):
            review = None
            if len(parts) > 1 and (found := _REVIEW.match(parts[-1])):
                review = found.group(1)
                parts = parts[:-1]
            if len(parts) <= 2:
                return tail.group(1), parts[0], (parts[1] if len(parts) == 2 else None), review
    return rest, None, None, None


def _line(depth: int, entry_id: str, text: str, date: str | None, source: str | None, review: str | None) -> str:
    meta = ""
    if date:  # source and review only make sense after a date
        parts = [date, *([source] if source else []), *([f"review {review}"] if review else [])]
        meta = f" ({', '.join(parts)})"
    return f"{'  ' * depth}- [{entry_id}] {text}{meta}"


def _entry_line(e: Entry) -> str:
    return _line(e.depth, e.id, e.text, e.date, e.source, e.review)


def _write_atomic(path: Path, text: str) -> None:
    """Write a whole file at once (600): a reader sees the old text or the new, never half."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _git_env() -> dict[str, str]:
    """git's environment: ours alone. No GIT_* leftovers, no user or system config (signing, hooks, identity)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME=_AUTHOR, GIT_AUTHOR_EMAIL=_EMAIL, GIT_COMMITTER_NAME=_AUTHOR,
               GIT_COMMITTER_EMAIL=_EMAIL, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               GIT_TERMINAL_PROMPT="0")
    return env


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


# ---- one topic file --------------------------------------------------------------------------------------------


@dataclasses.dataclass
class _Row:
    line: int  # index into the document's body
    entry: Entry
    parent: int | None  # index into the document's rows


class _Doc:
    """One topic file: its header fields and body lines, with the entries parsed out of the lines."""

    def __init__(self, name: str, prefix: str, about: str, reviewed: bool, body: list[str]):
        self.name, self.prefix, self.about, self.reviewed, self.body = name, prefix, about, reviewed, body
        self.rows: list[_Row] = []
        self.reparse()

    @classmethod
    def parse(cls, name: str, text: str) -> "_Doc":
        lines = text.split("\n")
        while lines and not lines[-1].strip():
            lines.pop()
        head = _HEADER.match(lines[0]) if lines else None
        if not head:
            raise MemoryError_(f"The memory file for '{name}' doesn't start with a header like "
                               f"'# {name} (x): what it holds'. Fix the file by hand before using memory.")
        body = lines[1:]
        while body and not body[0].strip():
            body.pop(0)
        return cls(name, head.group(2), head.group(4).strip(), bool(head.group(3)), body)

    @classmethod
    def blank(cls, name: str) -> "_Doc | None":
        if name == CORE:
            return cls(CORE, _CORE_PREFIX, _CORE_ABOUT, False, [])
        if name == ARCHIVE:
            return cls(ARCHIVE, _ARCHIVE_PREFIX, _ARCHIVE_ABOUT, False, [])
        return None

    def reparse(self) -> None:
        self.rows = []
        group: str | None = None
        open_rows: list[int] = []  # the entry above, and above that, as far as the current one is nested
        for i, line in enumerate(self.body):
            if heading := _GROUP.match(line):
                group = heading.group(1).strip()
                open_rows = []
            elif m := _ENTRY.match(line):
                depth = len(m.group(1)) // 2
                while open_rows and self.rows[open_rows[-1]].entry.depth >= depth:
                    open_rows.pop()
                text, date, source, review = _split_meta(m.group(3))
                entry = Entry(m.group(2), text, date, source, depth, group, review)
                self.rows.append(_Row(i, entry, open_rows[-1] if open_rows else None))
                open_rows.append(len(self.rows) - 1)

    def text(self) -> str:
        flag = ", reviewed" if self.reviewed else ""
        parts = [f"# {self.name} ({self.prefix}{flag}): {self.about}".rstrip()]
        if self.body:
            parts += ["", *self.body]
        return "\n".join(parts) + "\n"

    def size(self) -> int:
        """Characters of entries (the file without its header line): what the budgets count."""
        return len("\n".join(self.body))

    def find(self, entry_id: str) -> _Row | None:
        return next((r for r in self.rows if r.entry.id == entry_id), None)

    def span(self, row: _Row) -> tuple[int, int]:
        """The lines of an entry and everything nested under it."""
        end = row.line + 1
        while end < len(self.body):
            line = self.body[end]
            nested = _ENTRY.match(line)
            if _GROUP.match(line) or (nested and len(nested.group(1)) // 2 <= row.entry.depth):
                break
            end += 1
        while end > row.line + 1 and not self.body[end - 1].strip():
            end -= 1
        return row.line, end

    def subtree(self, row: _Row) -> list[_Row]:
        start, end = self.span(row)
        return [r for r in self.rows if start <= r.line < end]

    def insert(self, lines: list[str], under: str | None) -> None:
        """Add lines at the end of a group, or of the entries that have none (before the first group)."""
        heads = [(i, g.group(1).strip()) for i, line in enumerate(self.body) if (g := _GROUP.match(line))]
        if under is None:
            start, end = 0, (heads[0][0] if heads else len(self.body))
        else:
            n = next((n for n, (_, name) in enumerate(heads) if name.lower() == under.lower()), None)
            if n is None:
                self.body += [f"## {under}", *lines]
                self.reparse()
                return
            start, end = heads[n][0] + 1, (heads[n + 1][0] if n + 1 < len(heads) else len(self.body))
        while end > start and not self.body[end - 1].strip():
            end -= 1
        self.body[end:end] = lines
        self.reparse()

    def remove(self, start: int, end: int) -> None:
        del self.body[start:end]
        self.reparse()

    def set_line(self, index: int, line: str) -> None:
        self.body[index] = line
        self.reparse()


# ---- one write -------------------------------------------------------------------------------------------------


def _path_of(name: str) -> str:
    return "core.md" if name == CORE else "archive.md" if name == ARCHIVE else f"topics/{name}.md"


class _Tx:
    """The work of one write, under the lock: files read once, changes held until the end, then written together."""

    def __init__(self, store: "MemoryStore"):
        self.store = store
        self.docs: dict[str, _Doc] = {}
        self.base: dict[str, str | None] = {}  # each document's text when first read (None: it wasn't a file yet)
        self.base_ids: dict[str, set[str]] = {}
        self.base_size: dict[str, int] = {}
        self.gone: set[str] = set()  # topic files to delete
        self.message = ""
        self.marks = store._read_marks()  # the highest number issued per letter, as last saved
        self._seen: dict[str, int] | None = None
        self._taken: set[str] = set()  # id letters handed to new topics in this write

    def doc(self, name: str) -> _Doc:
        if name not in self.docs:
            doc = self.store._load(name)
            if doc is None:
                raise self.store._unknown(name)
            self.docs[name] = doc
            self.base[name] = doc.text()
            self.base_ids[name] = {r.entry.id for r in doc.rows}
            self.base_size[name] = doc.size()
        return self.docs[name]

    def topic(self, topic: str, saving: bool = False) -> _Doc:
        name = _topic_name(topic)
        if name == ARCHIVE:
            if saving:
                raise MemoryError_("Nothing is saved to the archive directly: entries move there when they end "
                                   "or are replaced.")
            return self.doc(ARCHIVE)
        if name not in self.store._names():
            raise self.store._unknown(name, saving)
        return self.doc(name)

    def all_docs(self, archive: bool = True) -> list[_Doc]:
        return [self.doc(n) for n in self.store._names() + ([ARCHIVE] if archive else [])]

    def find(self, entry_id: str, archive: bool = True) -> tuple[_Doc, _Row]:
        for doc in self.all_docs(archive):
            if row := doc.find(entry_id):
                return doc, row
        raise MemoryError_(f"[{entry_id}] isn't in memory.")

    def highest(self) -> dict[str, int]:
        """The highest number used so far for each id letter: saved in .ids.json, in any file, or issued in this write
        (archived entries count through their "was" notes). Taken before a write changes anything, so the numbers of
        entries it removes are still in it."""
        if self._seen is None:
            seen = dict(self.marks)
            for doc in self.all_docs():
                for r in doc.rows:
                    _note(seen, r.entry.id)
                    if doc.name == ARCHIVE and (was := _WAS.match(r.entry.text)):
                        _note(seen, was.group(1))
            self._seen = seen
        return self._seen

    def new_id(self, prefix: str) -> str:
        seen = self.highest()
        seen[prefix] = seen.get(prefix, 0) + 1
        return f"{prefix}{seen[prefix]}"

    def pick_prefix(self, name: str) -> str:
        """The first letter of a new topic's name that no topic, id or the archive already uses."""
        used = set(self.highest()) | {d.prefix for d in self.all_docs()} | {_ARCHIVE_PREFIX} | self._taken
        for letter in [c for c in name if c in string.ascii_lowercase] + list(string.ascii_lowercase):
            if letter not in used:
                self._taken.add(letter)
                return letter
        raise MemoryError_("Every prefix letter is in use, so no new topic can be made. Merge some topics first.")

    def archive_rows(self, doc: _Doc, entry_id: str, why: str, ended: str, text: str | None = None) -> None:
        """Move an entry and its details to the archive, each with a new z id and a note of where it came from."""
        row = doc.find(entry_id)
        assert row is not None
        lines = []
        for r in doc.subtree(row):
            e = r.entry
            body = text if (r is row and text is not None) else e.text
            note = f"(was {e.id}, archived {ended}: {why}) {body}"
            lines.append(_line(e.depth - row.entry.depth, self.new_id(_ARCHIVE_PREFIX), note, e.date, e.source, None))
        doc.remove(*doc.span(row))
        self.doc(ARCHIVE).insert(lines, None)

    def check_core(self) -> None:
        core = self.docs.get(CORE)
        if core and core.size() > CORE_LIMIT and core.size() > self.base_size[CORE]:
            others = ", ".join(n for n in self.store._names() if n != CORE) or "none exist yet"
            raise MemoryError_(f"Core is full: it would hold {core.size():,} characters and the limit is "
                               f"{CORE_LIMIT:,}. Core is only for facts that change most answers; save this to a "
                               f"topic instead ({others}).")

    def changes(self) -> list[tuple[str, str | None]]:
        """(path, new text or None to delete) for what changed, in a safe order: files that gain entries first, files
        that lose entries after, so a crash between two files leaves an entry twice rather than not at all."""
        rank: list[tuple[int, str, str]] = []
        for name, doc in self.docs.items():
            if name in self.gone:
                continue
            text = doc.text()
            if text != self.base[name]:
                ids = {r.entry.id for r in doc.rows}
                lost, gained = self.base_ids[name] - ids, ids - self.base_ids[name]
                rank.append((2 if lost else 0 if gained else 1, name, text))
        out: list[tuple[str, str | None]] = [(_path_of(n), t) for _, n, t in sorted(rank, key=lambda r: r[0])]
        out += [(_path_of(n), None) for n in sorted(self.gone) if self.base.get(n) is not None]
        if out and self.highest() != self.marks:  # a write that changes nothing else never touches the record
            out.append((_MARKS, json.dumps(self.highest(), sort_keys=True) + "\n"))
        return out


# ---- the store -------------------------------------------------------------------------------------------------


class MemoryStore:
    """Topic files in a git repo under `root`. `guard(text)` raises to refuse new text; `commit=False` skips git;
    `on_change(store)` runs after a write, before its commit, and returns extra files (relative path -> text) to
    write and commit with it (it may read the store, never write to it)."""

    def __init__(self, root: Path, guard: Callable[[str], None] | None = None, commit: bool = True,
                 on_change: Callable[["MemoryStore"], dict[str, str]] | None = None):
        self.root = Path(root)
        self.guard = guard
        self.commit = commit
        self.on_change = on_change
        self._busy = threading.local()

    # -- reading (no lock: every file is replaced whole) --

    def topics(self) -> list[Topic]:
        """Core first, then the topic files by name. The archive isn't a topic: ask for it by name."""
        return [Topic(d.name, d.prefix, d.about, len(d.rows), d.reviewed) for d in self._docs(archive=False)]

    def entries(self, topic: str) -> list[Entry]:
        return [r.entry for r in self._topic(topic).rows]

    def render(self, topic: str) -> str:
        return self._topic(topic).text().rstrip("\n")

    def size(self, topic: str) -> int:
        """Characters of entries in a topic: what CORE_LIMIT and TOPIC_SOFT count."""
        return self._topic(topic).size()

    def search(self, word: str, include_archive: bool = False) -> str:
        """Entries whose text, group or topic name has every word, across topics. The archive only when asked."""
        words = _norm(word).split()
        if not words:
            raise MemoryError_("Say what to look for.")
        blocks: list[str] = []
        for doc in self._docs(archive=include_archive):
            found: dict[str | None, list[str]] = {}
            for r in doc.rows:
                hay = _norm(f"{doc.name} {r.entry.under or ''} {r.entry.text}")
                if all(w in hay for w in words):
                    found.setdefault(r.entry.under, []).append(_entry_line(r.entry))
            blocks += [doc.name + (f" / {group}" if group else "") + "\n" + "\n".join(lines)
                       for group, lines in found.items()]
        if not blocks:
            return f"Nothing about '{word.strip()}' in memory. Topics: {', '.join(self._names())}."
        return "\n".join(blocks)

    def review_due(self, today: str) -> list[Entry]:
        """Entries to check again: an explicit review date that has come, or (in a reviewed topic, with no explicit
        date) a date older than DEFAULT_REVIEW_DAYS. An explicit date wins either way; the archive is never due."""
        now = _date(today, "today")
        due = []
        for doc in self._docs(archive=False):
            for r in doc.rows:
                e = r.entry
                start = _parse_date(e.date) if e.date else None
                if e.review:
                    when = e.review
                elif doc.reviewed and start:
                    when = (start + dt.timedelta(days=DEFAULT_REVIEW_DAYS)).isoformat()
                else:
                    continue
                if when <= now:
                    due.append(e)
        return due

    def head(self) -> str:
        """The repo's HEAD sha, or "" before the first commit (or with commit=False)."""
        if not (self.root / ".git").exists():
            return ""
        proc = self._git("rev-parse", "--verify", "-q", "HEAD", check=False)
        return proc.stdout.strip() if proc.returncode == 0 else ""

    # -- writing --

    def save(self, text: str, topic: str, source: str, under: str | None = None, review: str | None = None,
             similar: str | None = None) -> str:
        text = self._text(text)
        source = _source(source)
        group = self._text(under, "group name", _GROUP_MAX) if under and under.strip() else None
        due = _date(review, "The review date") if review and review.strip() else None
        add, replace_id = _parse_similar(similar)
        return self._write(lambda tx: self._save(tx, text, topic, source, group, due, add, replace_id))

    def update(self, entry_id: str, text: str | None = None, remove: bool = False,
               review: str | None = None) -> str:
        """Change an entry's text and/or review date, or (with no arguments) confirm it's still true: the date
        becomes today. A review date that had come due is cleared by the update, since that was the review."""
        eid = _entry_id(entry_id)
        if remove and (text is not None or review is not None):
            raise MemoryError_("Pass either remove=true or new text, not both.")
        new_text = self._text(text) if text is not None else None
        due = _date(review, "The review date") if review is not None and review.strip() else None
        return self._write(lambda tx: self._update(tx, eid, new_text, remove, due))

    def archive(self, entry_id: str, why: str, ended: str | None = None, text: str | None = None) -> str:
        """Close an entry: it moves to archive.md with its end date and why, optionally rewritten in the past tense."""
        eid = _entry_id(entry_id)
        reason = self._text(why, "reason")
        end = _date(ended, "The ended date") if ended is not None else None
        new_text = self._text(text) if text is not None else None
        return self._write(lambda tx: self._archive(tx, eid, reason, end or today(), new_text))

    def move(self, entry_id: str, topic: str) -> str:
        """Put an entry (and its details) in another topic. It keeps its id, date and source."""
        eid = _entry_id(entry_id)
        return self._write(lambda tx: self._move(tx, eid, topic))

    def merge(self, keep_id: str, merge_id: str, text: str) -> str:
        """Fold one entry into another: keep_id gets the new text plus "(merged from <merge_id>)" and the newer
        date; merge_id goes to the archive with a pointer to keep_id."""
        keep, gone = _entry_id(keep_id), _entry_id(merge_id)
        new_text = self._text(text)
        return self._write(lambda tx: self._merge(tx, keep, gone, new_text))

    def split(self, topic: str, groups: dict[str, list[str]], about: dict[str, str]) -> str:
        """Replace a topic by new ones (group name -> entry ids). Every top-level entry goes to exactly one group, and
        details follow their parent unless listed apart. Ids don't change. A group may reuse the topic's own name."""
        return self._write(lambda tx: self._split(tx, topic, groups, about))

    # -- reading helpers --

    def _path(self, name: str) -> Path:
        return self.root / _path_of(name)

    def _names(self) -> list[str]:
        """core, then the existing topic files by name (never the archive)."""
        directory = self.root / "topics"
        found = sorted(p.stem for p in directory.glob("*.md")
                       if _NAME.match(p.stem) and p.stem not in (CORE, ARCHIVE)) if directory.is_dir() else []
        return [CORE, *found]

    def _load(self, name: str) -> _Doc | None:
        try:
            return _Doc.parse(name, self._path(name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return _Doc.blank(name)

    def _docs(self, archive: bool = True) -> list[_Doc]:
        docs = [self._load(n) for n in self._names() + ([ARCHIVE] if archive else [])]
        return [d for d in docs if d is not None]

    def _topic(self, topic: str) -> _Doc:
        name = _topic_name(topic)
        doc = self._load(name) if name == ARCHIVE or name in self._names() else None
        if doc is None:
            raise self._unknown(name)
        return doc

    def _unknown(self, name: str, saving: bool = False) -> MemoryError_:
        names = ", ".join(self._names())
        if saving:
            return MemoryError_(f"There's no topic called '{name}'. Save it to one of: {names}.")
        return MemoryError_(f"There's no topic called '{name}'. Topics: {names}. "
                            "(Ask for 'archive' to see facts that ended.)")

    def _read_marks(self) -> dict[str, int]:
        try:
            raw = json.loads((self.root / _MARKS).read_text(encoding="utf-8"))
            return {k: v for k, v in raw.items() if re.fullmatch("[a-z]", k) and isinstance(v, int) and v > 0}
        except (FileNotFoundError, ValueError, AttributeError):
            return {}

    def _text(self, text: str, what: str = "memory text", limit: int | None = None) -> str:
        """New text as it will be written (one line, no invisible characters), refused if the guard refuses it.
        Every free-text argument goes through here: the entry text, group names, reasons and about lines."""
        text = _clean(text, what)
        if limit is not None and len(text) > limit:
            raise MemoryError_(f"The {what} is longer than {limit} characters; shorten it.")
        if self.guard is not None:
            self.guard(text)
        return text

    # -- the operations (each runs inside _write, holding the lock) --

    def _save(self, tx: _Tx, text: str, topic: str, source: str, group: str | None, review: str | None,
              add: bool, replace_id: str | None) -> str:
        doc = tx.topic(topic, saving=True)
        old_doc = None
        if replace_id:
            try:
                old_doc, _ = tx.find(replace_id, archive=False)
            except MemoryError_:
                raise MemoryError_(f"similar='replace:{replace_id}' names an entry that isn't in memory. Use an id "
                                   "from the held list, or similar='add' to keep both.") from None
        key = _norm(text)
        close = []  # (similarity, topic, entry) for everything live that's near this text, in any topic or group
        for other in tx.all_docs(archive=False):
            for r in other.rows:
                if (ratio := _ratio(key, _norm(r.entry.text))) >= SIMILAR_RATIO:
                    close.append((ratio, other.name, r.entry))
        close.sort(key=lambda match: -match[0])
        # An exact repeat is skipped only within its own group (the same sentence under two apps can both be true);
        # anything similar, anywhere, holds the save until the caller says add or replace.
        repeats = [e for ratio, _, e in close
                   if ratio >= SAME_RATIO and (e.under or "").lower() == (group or "").lower()]
        if repeats:
            return f"Already in memory as [{repeats[0].id}]; nothing saved."
        if close and not add and not replace_id:
            shown = ", ".join(f"[{e.id}] {_snippet(e.text)} ({_where(topic_name, e)})"
                              for _, topic_name, e in close[:_HELD_SHOWN])
            return (f"Held: similar entries {shown}. Call memory_save again with similar='add' to keep both, "
                    "or similar='replace:<id>' to close the old one.")

        entry_id = tx.new_id(doc.prefix)
        notes = []
        if old_doc is not None:
            tx.archive_rows(old_doc, replace_id, f"replaced by {entry_id}", today())
            notes.append(f"[{replace_id}] is closed and kept in the archive.")
        doc.insert([_line(0, entry_id, text, today(), source, review)], group)
        if doc.name != CORE and doc.size() > TOPIC_SOFT:
            notes.append(f"{doc.name} now holds {doc.size():,} characters, past the {TOPIC_SOFT:,} a topic should "
                         "stay under, so the weekly tidy will propose splitting it.")
        tx.message = f"memory: save {entry_id} ({source})" + (f", archive {replace_id}" if replace_id else "")
        said = "".join(" " + n for n in notes)
        return f"Saved [{entry_id}].{said} End your reply with: saved to memory: {text}"

    def _update(self, tx: _Tx, entry_id: str, text: str | None, remove: bool, review: str | None) -> str:
        doc, row = tx.find(entry_id)
        e = row.entry
        if remove:
            doc.remove(*doc.span(row))
            tx.message = f"memory: remove {entry_id}"
            return f"Removed [{entry_id}]. End your reply with: removed from memory: {e.text}"
        if doc.name == ARCHIVE:
            raise MemoryError_(f"[{entry_id}] is in the archive, where entries can only be removed, not changed.")
        new_text = text if text is not None else e.text
        new_review = review or (None if e.review and e.review <= today() else e.review)
        doc.set_line(row.line, _line(e.depth, entry_id, new_text, today(), e.source, new_review))
        tx.message = f"memory: update {entry_id}"
        return f"Updated [{entry_id}]. End your reply with: updated memory: {new_text}"

    def _archive(self, tx: _Tx, entry_id: str, why: str, ended: str, text: str | None) -> str:
        doc, _ = tx.find(entry_id)
        if doc.name == ARCHIVE:
            raise MemoryError_(f"[{entry_id}] is already in the archive.")
        tx.archive_rows(doc, entry_id, why, ended, text)
        tx.message = f"memory: archive {entry_id}"
        return f"Archived [{entry_id}] (ended {ended}: {why})."

    def _move(self, tx: _Tx, entry_id: str, topic: str) -> str:
        src, row = tx.find(entry_id)
        if src.name == ARCHIVE:
            raise MemoryError_(f"[{entry_id}] is in the archive and stays there.")
        if _topic_name(topic) == ARCHIVE:
            raise MemoryError_("Entries reach the archive by being archived, not moved: use archive.")
        dest = tx.topic(topic)
        if dest is src:
            raise MemoryError_(f"[{entry_id}] is already in {dest.name}.")
        start, end = src.span(row)
        indent = "  " * row.entry.depth
        lines = [ln[len(indent):] if ln.startswith(indent) else ln for ln in src.body[start:end]]
        src.remove(start, end)
        dest.insert(lines, row.entry.under)
        tx.message = f"memory: move {entry_id} to {dest.name}"
        return f"Moved [{entry_id}] to {dest.name}."

    def _merge(self, tx: _Tx, keep_id: str, gone_id: str, text: str) -> str:
        if keep_id == gone_id:
            raise MemoryError_("Pick two different entries to merge.")
        keep_doc, keep = tx.find(keep_id)
        gone_doc, gone = tx.find(gone_id)
        for doc, entry_id in ((keep_doc, keep_id), (gone_doc, gone_id)):
            if doc.name == ARCHIVE:
                raise MemoryError_(f"[{entry_id}] is in the archive; archived entries can't be merged.")
        if keep_doc is gone_doc and (gone in keep_doc.subtree(keep) or keep in gone_doc.subtree(gone)):
            raise MemoryError_("An entry can't be merged with a detail nested under it, or with what it's "
                               "nested under.")
        k, g = keep.entry, gone.entry
        date = max((d for d in (k.date, g.date) if d), default=today())  # a merge isn't a review: the newer date stands
        review = min((r for r in (k.review, g.review) if r), default=None)  # the earlier check still applies
        merged = f"{text} (merged from {gone_id})"
        keep_doc.set_line(keep.line, _line(k.depth, keep_id, merged, date, k.source, review))
        tx.archive_rows(gone_doc, gone_id, f"merged into {keep_id}", today())
        tx.message = f"memory: merge {gone_id} into {keep_id}"
        return f"Merged [{gone_id}] into [{keep_id}]; [{gone_id}] is kept in the archive."

    def _split(self, tx: _Tx, topic: str, groups: dict[str, list[str]], about: dict[str, str]) -> str:
        name = _topic_name(topic)
        if name in (CORE, ARCHIVE):
            raise MemoryError_(f"The {name} can't be split: it's one file by design"
                               + (", so move entries out of it instead." if name == CORE else "."))
        src = tx.topic(topic)
        wanted = {_topic_name(n): ids for n, ids in groups.items()}
        if len(wanted) != len(groups):
            raise MemoryError_("Two of the groups have the same name.")
        if len(wanted) < 2:
            raise MemoryError_("A split needs at least two groups: new topic names, each with entry ids.")
        if any(ln.strip() and not _ENTRY.match(ln) and not _GROUP.match(ln) for ln in src.body):
            raise MemoryError_(f"{src.name} has lines that aren't entries; tidy the file by hand before splitting it.")
        existing = self._names()
        for n in wanted:
            if n == ARCHIVE:
                raise MemoryError_("'archive' is reserved for facts that ended. Pick another name.")
            if not _NAME.match(n):
                raise MemoryError_(f"'{n}' isn't a topic name: use lowercase words, digits and hyphens, "
                                   "like 'sleep' or 'home-office'.")
            if n in existing and n != src.name:
                raise MemoryError_(f"There's already a topic called '{n}'. Pick a new name.")
        after = len(existing) - (src.name not in wanted) + len([n for n in wanted if n != src.name])
        if after > MAX_INDEX_LINES:
            raise MemoryError_(f"That would make {after} topics (core counts as one), and the topic list holds at "
                               f"most {MAX_INDEX_LINES}. Merge or retire a topic first.")

        abouts = {_topic_name(n): text for n, text in about.items()}
        blank = [n for n in wanted if not (abouts.get(n) or "").strip() and n != src.name]
        if blank:
            raise MemoryError_("Give each new topic an 'about' line, such as 'plants, beds, tools; load for "
                               f"planting or watering'. Missing: {', '.join(blank)}.")
        lines = {n: self._text(abouts[n], "about line") if (abouts.get(n) or "").strip() else src.about for n in wanted}

        placed: dict[str, str] = {}
        twice, strangers = [], []
        for n, ids in wanted.items():
            if not ids:
                raise MemoryError_(f"The group '{n}' has no entries; every group needs at least one.")
            for raw in ids:
                entry_id = _entry_id(raw)
                if entry_id in placed:
                    twice.append(entry_id)
                elif src.find(entry_id) is None:
                    strangers.append(entry_id)
                else:
                    placed[entry_id] = n
        loose = [r.entry.id for r in src.rows if r.parent is None and r.entry.id not in placed]
        problems = ([f"placed more than once: {', '.join(sorted(set(twice)))}"] if twice else []) \
            + ([f"not in {src.name}: {', '.join(strangers)}"] if strangers else []) \
            + ([f"not placed: {', '.join(loose)}"] if loose else [])
        if problems:
            raise MemoryError_("Every entry has to go to exactly one group (details follow their parent): "
                               + "; ".join(problems) + ".")

        home: dict[int, str] = {}  # row index -> group
        depth: dict[int, int] = {}
        for i, r in enumerate(src.rows):
            home[i] = placed.get(r.entry.id) or home[r.parent]  # type: ignore[index]
            depth[i] = 0 if r.parent is None or home[r.parent] != home[i] else depth[r.parent] + 1
        bodies = {n: ([], {}) for n in wanted}  # group -> (entries with no `##`, `##` name -> entries)
        for i, r in enumerate(src.rows):
            e = r.entry
            line = _line(depth[i], e.id, e.text, e.date, e.source, e.review)
            plain, headed = bodies[home[i]]
            (plain if e.under is None else headed.setdefault(e.under, [])).append(line)
        docs = {}
        for n in wanted:
            plain, headed = bodies[n]
            prefix = src.prefix if n == src.name else tx.pick_prefix(n)
            body = plain + [ln for group, ls in headed.items() for ln in [f"## {group}", *ls]]
            docs[n] = _Doc(n, prefix, lines[n], src.reviewed, body)
        for n, doc in docs.items():
            if n not in tx.docs:
                tx.docs[n], tx.base[n], tx.base_ids[n], tx.base_size[n] = doc, None, set(), 0
            else:
                tx.docs[n] = doc
        if src.name not in wanted:
            tx.gone.add(src.name)
        counts = [f"{n} ({_plural(len(d.rows), 'entry', 'entries')})" for n, d in docs.items()]
        tx.message = f"memory: split {src.name} into {', '.join(wanted)}"
        return f"Split {src.name} into {', '.join(counts[:-1])} and {counts[-1]}."

    # -- the write machinery --

    def _write(self, work: Callable[[_Tx], str]) -> str:
        """Run one change under the lock: `work` reads and edits documents and returns the reply, then every changed
        file is written and committed together."""
        if getattr(self._busy, "on", False):
            raise RuntimeError("A memory write can't start inside another one: on_change may read the store, "
                               "but not write to it.")
        with self._locked():
            self._busy.on = True
            try:
                tx = _Tx(self)
                tx.highest()  # before the work changes anything
                reply = work(tx)
                self._flush(tx)
                return reply
            finally:
                self._busy.on = False

    @contextmanager
    def _locked(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.root / _LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # closing the file lets go of the lock

    def _flush(self, tx: _Tx) -> None:
        changes = tx.changes()
        if not changes:
            return
        tx.check_core()  # refused before anything is written
        undo: list[tuple[Path, str | None]] = []
        try:
            for rel, text in changes:
                self._put(rel, text, undo)
            for rel, text in self._extra_files():
                self._put(rel, text, undo)
            if self.commit:
                self._commit(tx.message)
        except BaseException:
            for path, old in reversed(undo):  # put every file back as it was
                try:
                    if old is None:
                        path.unlink(missing_ok=True)
                    else:
                        _write_atomic(path, old)
                except OSError as e:
                    print(f"Memory: couldn't undo a failed write to {path}: {e!r}", file=sys.stderr)
            raise

    def _put(self, rel: str, text: str | None, undo: list[tuple[Path, str | None]]) -> None:
        path = self.root / rel
        try:
            old: str | None = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            old = None
        if old == text:
            return
        undo.append((path, old))
        if text is None:
            path.unlink()
        else:
            _write_atomic(path, text)

    def _extra_files(self) -> list[tuple[str, str]]:
        """What on_change adds to this commit. A broken hook or a bad path never blocks the memory change itself."""
        if self.on_change is None:
            return []
        try:
            files = dict(self.on_change(self) or {})
        except Exception as e:
            print(f"Memory: on_change failed ({e!r}); the change is saved without its extra files.", file=sys.stderr)
            return []
        good = []
        for rel, text in files.items():
            if isinstance(rel, str) and isinstance(text, str) and self._inside(rel):
                good.append((rel, text))
            else:
                print(f"Memory: ignoring on_change file {rel!r}: it isn't text for a file inside the memory folder.",
                      file=sys.stderr)
        return good

    def _inside(self, rel: str) -> bool:
        """A relative path within the folder that isn't git's or the store's own."""
        path = Path(rel)
        return bool(path.parts) and not path.is_absolute() and ".." not in path.parts \
            and path.parts[0] != ".git" and path.parts not in ((_LOCK,), (_MARKS,)) \
            and (self.root / path).resolve().is_relative_to(self.root.resolve())

    def _commit(self, message: str) -> None:
        if not (self.root / ".git").exists():
            self._git("init", "-q", "-b", "main")
        exclude = self.root / ".git" / "info" / "exclude"  # keep the lock and temp files out of the repo
        have = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        missing = [p for p in (_LOCK, "*.tmp") if p not in have.splitlines()]
        if missing:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with exclude.open("a", encoding="utf-8") as f:
                f.write(("\n" if have and not have.endswith("\n") else "") + "\n".join(missing) + "\n")
        self._git("add", "-A")
        if self._git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return  # nothing changed
        self._git("commit", "-q", "--no-verify", "-m", message)

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", f"core.excludesFile={os.devnull}", *args],
                              cwd=self.root, env=_git_env(), stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, encoding="utf-8", timeout=_GIT_TIMEOUT)
        if check and proc.returncode != 0:
            raise RuntimeError(f"git {args[0]} failed in the memory folder: {(proc.stderr or proc.stdout).strip()}")
        return proc
