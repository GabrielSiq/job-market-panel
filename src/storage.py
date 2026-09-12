"""Reading and writing the on-disk panel. Spec Sections 3.5, 6.2, 6.3.

Layout (one file per day, never one growing file - a growing file makes git store a new
blob of the whole thing on every commit):

    data/events/YYYY-MM-DD.jsonl.gz     appeared/disappeared, slim, every posting
    data/postings/YYYY-MM-DD.jsonl.gz   full records incl. description, tracked families
    data/runs/YYYY-MM-DD.jsonl          per-source (per-company for ATS) health log
    data/latest/new_postings.jsonl      stable path for downstream consumers
    data/state/pending_misses.json      in-flight disappearance candidates (see below)

**Raw capture is immutable.** Files for past days are never rewritten. Today's files are
rewritten wholesale on each run, which is what makes a same-day re-run idempotent: state
is replayed from days *before* today, so re-running recomputes the identical set of events
rather than appending duplicates.
"""

from __future__ import annotations

import gzip
import json
import logging
from collections.abc import Iterable, Iterator
from datetime import date
from pathlib import Path
from typing import Any

from src.models import JobPosting, PostingEvent, SourceRun

logger = logging.getLogger(__name__)

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"


class Storage:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or DATA_ROOT
        self.events = self.root / "events"
        self.postings = self.root / "postings"
        self.runs = self.root / "runs"
        self.latest = self.root / "latest"
        self.state = self.root / "state"
        for directory in (self.events, self.postings, self.runs, self.latest, self.state):
            directory.mkdir(parents=True, exist_ok=True)

    # --------------------------------------------------------------------------- write

    @staticmethod
    def _write_jsonl(path: Path, rows: Iterable[Any], *, compress: bool) -> int:
        payload = "".join(
            (row.model_dump_json() if hasattr(row, "model_dump_json") else json.dumps(row)) + "\n"
            for row in rows
        )
        data = payload.encode("utf-8")
        if compress:
            # mtime=0 and an empty filename keep the gzip header byte-identical for
            # identical content, so an unchanged re-run produces no git diff at all.
            # (GzipFile otherwise stamps both the time and the file object's name into
            # the header, which would make every re-run look like a change to git.)
            with (
                open(path, "wb") as handle,
                gzip.GzipFile(filename="", fileobj=handle, mode="wb", mtime=0) as gz,
            ):
                gz.write(data)
        else:
            path.write_bytes(data)
        return len(data)

    def write_events(self, on: date, events: list[PostingEvent]) -> Path:
        path = self.events / f"{on.isoformat()}.jsonl.gz"
        self._write_jsonl(path, events, compress=True)
        return path

    def write_postings(self, on: date, postings: list[JobPosting]) -> Path:
        path = self.postings / f"{on.isoformat()}.jsonl.gz"
        self._write_jsonl(path, postings, compress=True)
        return path

    def write_runs(self, on: date, runs: list[SourceRun]) -> Path:
        path = self.runs / f"{on.isoformat()}.jsonl"
        self._write_jsonl(path, runs, compress=False)
        return path

    def write_latest(self, postings: list[JobPosting]) -> Path:
        """The cross-repo interface (spec 3.2): the private brief fetches exactly this
        path over plain HTTPS. Stable from Phase 1 even though nothing reads it until
        Phase 5, so that phase is purely additive."""
        path = self.latest / "new_postings.jsonl"
        self._write_jsonl(path, postings, compress=False)
        return path

    # ---------------------------------------------------------------------------- read

    def event_files(self, before: date | None = None) -> list[Path]:
        files = sorted(self.events.glob("*.jsonl.gz"))
        if before is None:
            return files
        return [f for f in files if f.stem.removesuffix(".jsonl") < before.isoformat()]

    @staticmethod
    def read_jsonl_gz(path: Path) -> Iterator[dict[str, Any]]:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)

    # ------------------------------------------------------- pending-miss bookkeeping

    @property
    def _pending_path(self) -> Path:
        return self.state / "pending_misses.json"

    def read_pending_misses(self) -> dict[str, str]:
        """job_id -> ISO date of the first run that missed it.

        A deliberate, narrow departure from correctness rule 4 ("derive current state by
        replaying the event log; do not maintain a committed state file"). The two-miss
        rule needs to know whether a job was ALSO absent on the previous run, and the
        event log records only transitions - never "seen today" - so that fact is
        genuinely not reconstructible from it.

        The rule's stated concern is git bloat from rewriting full state daily. This file
        holds only in-flight candidates: typically a few hundred rows against a corpus of
        tens of thousands, a few KB. Storing the full daily observed set instead would
        cost roughly 55 MB/year and blow the storage budget on its own.

        Losing this file costs exactly one extra day of latency before a closed req is
        recorded, and nothing else - which is the kind of failure spec principle 2 says to
        accept rather than engineer around.
        """
        if not self._pending_path.exists():
            return {}
        try:
            data = json.loads(self._pending_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("pending_misses unreadable; disappearance detection delayed a day")
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def write_pending_misses(self, pending: dict[str, str]) -> None:
        self._pending_path.write_text(
            json.dumps(dict(sorted(pending.items())), indent=0) + "\n", encoding="utf-8"
        )
