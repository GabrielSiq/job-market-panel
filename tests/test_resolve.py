"""A probe that never answered is not a verdict.

`unresolved` is permanent in practice - the daily run does not re-probe it - so writing
that verdict on the strength of a 429, a 5xx or a timeout silently retires a company from
the panel forever. This is correctness rule 1 (absence of evidence is not evidence of
absence) applied to resolution rather than to the differ, where the project has already
learned it twice: in the healthcheck, and in reading Ashby's opt-in 404 as "no board".
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from tools import discover
from tools.resolve_ats import VENDORS as ALL_VENDORS
from tools.resolve_ats import Resolution, _probe, resolve_one

GREENHOUSE = next(v for v in ALL_VENDORS if v.name == "greenhouse")


def run(coro):
    return asyncio.run(coro)


async def _probe_once(handler) -> object:
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        return await _probe(client, GREENHOUSE, "sometoken")


class TestProbeSeparatesAMissFromNoAnswer:
    @pytest.mark.parametrize("code", [429, 500, 503, 403, 401])
    def test_an_unanswered_probe_is_not_a_miss(self, code):
        probe = run(_probe_once(lambda request: httpx.Response(code, json={})))
        assert probe.answered is False
        assert probe.board is None

    def test_a_timeout_is_not_a_miss(self):
        def handler(request):
            raise httpx.ConnectTimeout("timed out", request=request)

        probe = run(_probe_once(handler))
        assert probe.answered is False

    def test_404_is_a_real_answer(self):
        """The one negative we are entitled to act on."""
        probe = run(_probe_once(lambda request: httpx.Response(404)))
        assert probe.answered is True
        assert probe.board is None

    def test_an_empty_board_is_a_miss_not_a_find(self):
        """Indistinguishable from a slug collision; parking the token would be worse."""
        probe = run(_probe_once(lambda request: httpx.Response(200, json={"jobs": []})))
        assert probe.answered is True
        assert probe.board is None

    def test_a_live_board_is_found(self):
        payload = {"jobs": [{"id": 1, "company_name": "Acme"}, {"id": 2}]}
        probe = run(_probe_once(lambda request: httpx.Response(200, json=payload)))
        assert probe.board == (2, "Acme")


class TestAshbyFallbackAnswers:
    """Ashby's documented API is opt-in per organisation, so a 404 there is not a verdict
    and we ask the endpoint its own board page uses. That fallback must itself keep "no
    such board" apart from "no answer" - it used to run every response through
    raise_for_status, which made a plain 404 look like a transport failure and would have
    deferred the company on a question that was in fact answered."""

    def _ashby(self, handler):
        vendor = next(v for v in ALL_VENDORS if v.name == "ashby")

        async def go():
            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                return await _probe(client, vendor, "sometoken")

        return run(go())

    def test_a_404_from_both_endpoints_is_a_miss(self):
        assert self._ashby(lambda request: httpx.Response(404)).answered is True

    def test_a_rate_limited_fallback_is_no_verdict(self):
        def handler(request):
            return httpx.Response(404) if request.method == "GET" else httpx.Response(429)

        assert self._ashby(handler).answered is False

    def test_the_fallback_still_finds_an_opt_out_board(self):
        """Whatnot: 144 live postings behind a 404 on the documented API."""

        def handler(request):
            if request.method == "GET":
                return httpx.Response(404)
            return httpx.Response(
                200, json={"data": {"jobBoard": {"jobPostings": [{"id": 1}, {"id": 2}]}}}
            )

        assert self._ashby(handler).board == (2, None)


class TestResolutionStatus:
    def _resolve(self, handler, name="Acme") -> Resolution:
        async def go():
            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                return await resolve_one(client, name, asyncio.Semaphore(4))

        return run(go())

    def test_a_clean_sweep_of_404s_is_unresolved(self):
        result = self._resolve(lambda request: httpx.Response(404))
        assert result.status == "unresolved"
        assert result.no_verdict == 0

    def test_rate_limiting_defers_rather_than_retiring_the_company(self):
        result = self._resolve(lambda request: httpx.Response(429, json={}))
        assert result.status == "deferred"
        assert result.no_verdict > 0
        assert "did not answer" in result.note

    def test_the_note_says_which_vendor_and_why(self):
        """A verdict without its evidence is how 22 companies were deferred on day one
        with no way to tell throttling from an over-eager rule."""
        result = self._resolve(lambda request: httpx.Response(429, json={}))
        assert "greenhouse 429" in result.note
        assert set(result.no_verdict_reasons) <= {"greenhouse 429", "ashby 429", "lever 429"}

    def test_a_timeout_is_named_by_its_exception(self):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)

        assert "ReadTimeout" in self._resolve(handler).note

    def test_one_unanswered_probe_among_many_is_enough_to_defer(self):
        """The company might live behind exactly the probe that failed."""
        seen = {"n": 0}

        def handler(request):
            seen["n"] += 1
            return httpx.Response(500) if seen["n"] == 3 else httpx.Response(404)

        assert self._resolve(handler).status == "deferred"

    def test_a_found_board_is_never_deferred(self):
        """A flaky probe on one guess must not shadow a board another guess found."""
        seen = {"n": 0}

        def handler(request):
            seen["n"] += 1
            if seen["n"] == 1:
                return httpx.Response(503)
            return httpx.Response(200, json={"jobs": [{"id": 1, "company_name": "Acme"}]})

        result = self._resolve(handler)
        assert result.status == "verified"
        assert result.token is not None


class TestWatchlistRewrite:
    """`update_watchlist` rewrites the whole file, so anything it does not know about is
    at its mercy - and it now runs daily rather than only on a manual retry."""

    def _watchlist(self, tmp_path: Path, entries: list[dict]) -> Path:
        path = tmp_path / "watchlist.yaml"
        lines = ["# header comment", "companies:"]
        for e in entries:
            lines += [f"  - name: {json.dumps(e['name'])}"]
            lines += [
                f"    {k}: {json.dumps(v) if isinstance(v, str) else v}"
                for k, v in e.items()
                if k != "name"
            ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def _entry(self, name="Acme", **extra) -> dict:
        return {
            "name": name,
            "slug": "acme",
            "ats": "unknown",
            "token": None,
            "status": "deferred",
            "verified_by": None,
            "added": "2026-09-15",
            "source": "auto_discovered",
            "notes": "",
            **extra,
        }

    def test_hand_applied_fields_survive_a_rewrite(self, tmp_path):
        """`target_list: true` is on 24 entries, applied by hand, and nothing else
        records it. A rewrite that dropped it would be unrecoverable and silent."""
        path = self._watchlist(tmp_path, [self._entry(target_list=True)])
        result = Resolution(name="Acme", status="unresolved")
        discover.update_watchlist([result], path=path)

        after = discover.load_watchlist(path)[0]
        assert after["target_list"] is True

    def test_a_settled_deferred_entry_stops_being_re_probed(self, tmp_path):
        """Deferred means "ask again". Once the answer arrives it must be recorded, or
        the entry is re-probed every day forever."""
        path = self._watchlist(tmp_path, [self._entry()])
        discover.update_watchlist([Resolution(name="Acme", status="unresolved")], path=path)

        assert discover.load_watchlist(path)[0]["status"] == "unresolved"

    def test_an_unresolved_entry_is_still_left_alone(self, tmp_path):
        """The existing contract: a name already ruled out is not re-probed or rewritten."""
        path = self._watchlist(tmp_path, [self._entry(status="unresolved", added="2026-09-01")])
        discover.update_watchlist([Resolution(name="Acme", status="unresolved")], path=path)

        after = discover.load_watchlist(path)[0]
        assert after["status"] == "unresolved"
        assert str(after["added"]) == "2026-09-01"

    def test_a_deferred_entry_that_resolves_is_promoted(self, tmp_path):
        path = self._watchlist(tmp_path, [self._entry(target_list=True)])
        found = Resolution(
            name="Acme",
            ats="greenhouse",
            token="acmecorp",
            job_count=12,
            status="verified",
            verified_by="name_echo",
        )
        discover.update_watchlist([found], path=path)

        after = discover.load_watchlist(path)[0]
        assert (after["status"], after["ats"], after["token"]) == (
            "verified",
            "greenhouse",
            "acmecorp",
        )
        assert after["target_list"] is True
        assert str(after["added"]) == date.today().isoformat()
