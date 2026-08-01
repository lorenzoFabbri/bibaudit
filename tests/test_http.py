"""HTTP transport, retry policy and the on-disk cache.

Nothing here touches the network. ``urllib.request.urlopen`` is replaced by
:class:`_Transport` in every test that expects a request and by an exploding
stub in every test that must not make one, and every cache lives under
``tmp_path``.

The invariant these tests exist for is the one the whole tool rests on: **a
404 is a fact, a timeout is ignorance**. If an outage is ever reported as a
missing record, ``bibaudit`` tells a researcher that a real, correctly cited
paper does not exist — and a report that does that once stops being read. So
the distinction is probed from three sides: inside the cache layer, inside the
retry loop, and on the paths that give up.
"""

from __future__ import annotations

import ast
import http.client
import json
import random
import shutil
import sys
import time
import urllib.error
import urllib.request
import warnings
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from bibaudit.registries import http as http_module
from bibaudit.registries.http import Cache, Client, Transient, default_cache_dir

#: Two URLs on the same host, and one on another host, used throughout so the
#: pinned jitter values below stay meaningful.
_URL = "https://api.crossref.org/works/10.1000/example"
_URL_SAME_HOST = "https://api.crossref.org/works/10.1000/other"
_URL_OTHER_HOST = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?id=1"

#: The exact jitter ``http._jitter`` derives from :data:`_URL` and
#: :data:`_URL_SAME_HOST`. These are pinned as literals on purpose. Deriving
#: them in the test with ``hashlib`` would only restate the implementation and
#: would still pass if someone swapped sha256 for the builtin ``hash()`` —
#: which is salted per process, so the same audit re-run on the same cached
#: responses would sleep differently and stop being reproducible. A literal is
#: the only assertion that notices.
_URL_JITTER = 0.03903257801174945
_URL_SAME_HOST_JITTER = 0.473334859235523

_BODY = b'{"message": {"DOI": "10.1000/example"}}'
_PAYLOAD = {"message": {"DOI": "10.1000/example"}}


class _Response:
    """The context-manager-with-``.read()`` that ``urlopen`` returns."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


class _Transport:
    """A scripted stand-in for ``urllib.request.urlopen``.

    Each outcome is either a ``bytes`` body (an HTTP 200) or an exception
    instance to raise. Requests past the end of the script raise
    ``AssertionError`` rather than looping the last outcome: a test that
    expects one request must fail loudly, not quietly, if the code under test
    starts making two.
    """

    def __init__(self, outcomes: Iterable[object]) -> None:
        self._outcomes = list(outcomes)
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float | None] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    @property
    def urls(self) -> list[str]:
        return [request.full_url for request in self.requests]

    def __call__(
        self, request: urllib.request.Request, timeout: float | None = None
    ) -> _Response:
        index = len(self.requests)
        self.requests.append(request)
        self.timeouts.append(timeout)
        if index >= len(self._outcomes):
            raise AssertionError(
                f"unexpected request #{index + 1} to {request.full_url}: "
                f"the test scripted only {len(self._outcomes)}"
            )
        outcome = self._outcomes[index]
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, bytes)
        return _Response(outcome)


class _Clock:
    """A frozen monotonic clock that advances only when something sleeps.

    Real wall-clock timing would make the throttling assertions flaky on a
    loaded machine and would make the suite genuinely wait out every backoff.
    """

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _http_error(
    code: int, *, retry_after: str | None = None, url: str = _URL
) -> urllib.error.HTTPError:
    """An ``HTTPError`` as ``urlopen`` raises it, optionally with Retry-After."""
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(url, code, f"status {code}", headers, None)


def _timeouts(count: int) -> list[TimeoutError]:
    """*count* distinct timeout instances (one per attempt)."""
    return [TimeoutError("timed out") for _ in range(count)]


def _client(cache: Cache | None = None, **kwargs: Any) -> Client:
    """A :class:`Client` that does not throttle unless a test asks it to.

    ``min_interval=0.0`` by default so that the sleeps a backoff test captures
    are backoff sleeps and nothing else; the throttling tests pass their own.
    """
    kwargs.setdefault("min_interval", 0.0)
    return Client(cache, **kwargs)


def _sole_record(root: Path) -> Path:
    """The one cache record under *root*, ignoring any atomic-write temp file."""
    files = [p for p in root.rglob("*.json") if not p.name.startswith(".tmp-")]
    assert len(files) == 1, f"expected exactly one cache record, found {files}"
    return files[0]


def _records(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.json") if not p.name.startswith(".tmp-")]


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> Callable[..., _Transport]:
    """Install a scripted transport in place of ``urllib.request.urlopen``."""

    def install(*outcomes: object) -> _Transport:
        fake = _Transport(outcomes)
        monkeypatch.setattr(urllib.request, "urlopen", fake)
        return fake

    return install


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Every ``time.sleep`` duration, in order, without actually waiting."""
    recorded: list[float] = []
    monkeypatch.setattr(time, "sleep", recorded.append)
    return recorded


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """A fake monotonic clock, for the per-host throttling assertions."""
    fake = _Clock()
    monkeypatch.setattr(time, "monotonic", fake.monotonic)
    monkeypatch.setattr(time, "sleep", fake.sleep)
    return fake


def _forbid_urlopen(*args: object, **kwargs: object) -> None:
    raise AssertionError("urlopen was called; this path must not open a socket")


@pytest.fixture(autouse=True)
def _never_the_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this module starts with ``urlopen`` disabled.

    Autouse, so the default is "no socket" and a test has to opt *in* to a
    transport rather than opt out of the network. A test added later that
    forgets the ``transport`` fixture then fails loudly on the first request
    instead of quietly reaching api.crossref.org — which would make the offline
    suite pass on the author's laptop and fail in a sealed CI container, or,
    worse, pass everywhere while depending on a live registry's current data.
    """
    monkeypatch.setattr(urllib.request, "urlopen", _forbid_urlopen)


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any call to ``urlopen`` an outright test failure.

    Redundant with :func:`_never_the_real_network` and kept because it is the
    *stated* precondition of the tests that request it: those tests are about
    a path never opening a socket, and naming the fixture is what says so.
    """
    monkeypatch.setattr(urllib.request, "urlopen", _forbid_urlopen)


class TestFourOhFourIsAFact:
    """A confirmed absence is ``None`` — and is remembered."""

    def test_404_returns_none(self, transport: Callable[..., _Transport], tmp_path: Path) -> None:
        transport(_http_error(404))
        assert _client(Cache(tmp_path / "c")).get_json(_URL) is None

    def test_404_is_not_retried(
        self, transport: Callable[..., _Transport], sleeps: list[float]
    ) -> None:
        """A missing record is settled on the first answer.

        Retrying it four times over 15 seconds would make an audit of a
        bibliography with a few fabricated DOIs take minutes for no new
        information.
        """
        fake = transport(_http_error(404))
        assert _client().get_json(_URL) is None
        assert fake.calls == 1
        assert sleeps == []

    def test_404_is_cached_and_not_refetched(
        self, transport: Callable[..., _Transport], tmp_path: Path
    ) -> None:
        """Regression: not-found answers were not being written to the cache,
        so every re-run of an audit re-fetched every fabricated DOI — the
        slowest lookups in the run, repeated forever, and the ones a user
        re-runs most while fixing them.

        The transport is scripted with two 404s so that a reintroduced bug
        fails on the call count rather than on an unexpected-request error.
        """
        cache = Cache(tmp_path / "c")
        fake = transport(_http_error(404), _http_error(404))
        assert _client(cache).get_json(_URL) is None
        assert _client(cache).get_json(_URL) is None
        assert fake.calls == 1

    def test_a_cached_404_replays_offline_as_a_fact(
        self, transport: Callable[..., _Transport], tmp_path: Path
    ) -> None:
        """``--offline`` must reproduce "this DOI does not exist", not lose it.

        If the cached absence came back as :class:`Transient` instead, the one
        genuinely fabricated reference in a bibliography would soften from a
        failing verdict to ``UNCHECKED`` the moment the audit was re-run
        offline — which is precisely the defect the tool exists to catch.
        """
        cache = Cache(tmp_path / "c")
        fake = transport(_http_error(404))
        assert _client(cache).get_json(_URL) is None

        assert _client(cache, offline=True).get_json(_URL) is None
        assert fake.calls == 1

    def test_a_cached_payload_is_still_returned_as_a_payload(
        self, transport: Callable[..., _Transport], tmp_path: Path
    ) -> None:
        """The absence marker must not swallow ordinary cached records."""
        cache = Cache(tmp_path / "c")
        transport(_BODY)
        assert _client(cache).get_json(_URL) == _PAYLOAD
        assert _client(cache).get_json(_URL) == _PAYLOAD

    def test_404_on_one_url_does_not_mark_another_absent(
        self, transport: Callable[..., _Transport], tmp_path: Path
    ) -> None:
        cache = Cache(tmp_path / "c")
        transport(_http_error(404), _BODY)
        client = _client(cache)
        assert client.get_json(_URL) is None
        assert client.get_json(_URL_SAME_HOST) == _PAYLOAD


class TestAnOutageIsNotAFact:
    """Everything that is not a 404 must raise, never return ``None``."""

    def test_timeout_raises_transient_after_the_budget_is_exhausted(
        self, transport: Callable[..., _Transport], sleeps: list[float]
    ) -> None:
        """Five attempts (1 initial + 4 retries), four sleeps, then give up.

        Returning ``None`` here would report every reference in the file as
        fabricated the moment a registry goes down.
        """
        fake = transport(*_timeouts(5))
        with pytest.raises(Transient):
            _client().get_json(_URL)
        assert fake.calls == 5
        assert len(sleeps) == 4

    def test_a_timeout_that_clears_within_the_budget_returns_the_payload(
        self, transport: Callable[..., _Transport], sleeps: list[float]
    ) -> None:
        transport(TimeoutError("timed out"), TimeoutError("timed out"), _BODY)
        assert _client().get_json(_URL) == _PAYLOAD
        assert len(sleeps) == 2

    def test_connection_refused_raises_transient(
        self, transport: Callable[..., _Transport], sleeps: list[float]
    ) -> None:
        """A refused connection is a URLError, not an HTTPError: it carries no
        status code at all, so nothing about it says "this work is missing".
        """
        refused = [
            urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
            for _ in range(5)
        ]
        fake = transport(*refused)
        with pytest.raises(Transient):
            _client().get_json(_URL)
        assert fake.calls == 5

    def test_dns_failure_raises_transient(
        self, transport: Callable[..., _Transport], sleeps: list[float]
    ) -> None:
        """A laptop on a captive-portal wifi fails to resolve api.crossref.org."""
        failures = [
            urllib.error.URLError(OSError(8, "nodename nor servname provided"))
            for _ in range(5)
        ]
        transport(*failures)
        with pytest.raises(Transient):
            _client().get_json(_URL)

    def test_five_hundred_that_never_recovers_raises_transient(
        self, transport: Callable[..., _Transport], sleeps: list[float]
    ) -> None:
        fake = transport(*[_http_error(500) for _ in range(5)])
        with pytest.raises(Transient):
            _client().get_json(_URL)
        assert fake.calls == 5

    def test_five_hundred_that_recovers_returns_the_payload(
        self, transport: Callable[..., _Transport], sleeps: list[float]
    ) -> None:
        transport(_http_error(503), _BODY)
        assert _client().get_json(_URL) == _PAYLOAD

    def test_a_non_404_client_error_is_ignorance_not_absence(
        self, transport: Callable[..., _Transport], sleeps: list[float]
    ) -> None:
        """A 400 means "I did not understand the question", never "no such
        work" — Crossref returns it for a whole batch when one DOI in the
        filter is malformed, and reading that as 20 missing works would
        condemn 19 good references.
        """
        transport(*[_http_error(400) for _ in range(5)])
        with pytest.raises(Transient):
            _client().get_json(_URL)

    def test_an_outage_is_never_cached_as_an_absence(
        self, transport: Callable[..., _Transport], tmp_path: Path, sleeps: list[float]
    ) -> None:
        """The worst possible version of this bug: an outage written to the
        cache as "not found" would keep reporting a real paper as fabricated
        long after the registry came back, with no network access to correct
        it.
        """
        cache = Cache(tmp_path / "c")
        transport(*[_http_error(503) for _ in range(5)])
        with pytest.raises(Transient):
            _client(cache).get_json(_URL)
        assert _records(cache.path) == []

        transport(_BODY)
        assert _client(cache).get_json(_URL) == _PAYLOAD

    def test_a_body_truncated_mid_read_raises_transient(
        self, transport: Callable[..., _Transport], sleeps: list[float]
    ) -> None:
        """Regression, and a real defect this test was written to expose.

        Crossref serves every response chunked. When a connection drops after
        the headers but part-way through the body, ``response.read()`` raises
        ``http.client.IncompleteRead`` — which is **not** an ``OSError``, so
        the retry loop's ``except OSError`` never saw it. It escaped
        :meth:`Client._request` uncaught: not ``None`` (a fact), not
        ``Transient`` (ignorance), but a traceback out of ``audit.py``, which
        catches only ``Transient``. A whole audit died with no report at all
        because one reply was cut short. A half-read body is ignorance like any
        other timeout.
        """
        cut_short = [
            http.client.IncompleteRead(b'{"message": {"DO', 4096) for _ in range(5)
        ]
        fake = transport(*cut_short)
        with pytest.raises(Transient):
            _client().get_json(_URL)
        assert fake.calls == 5

    def test_a_body_truncated_once_is_retried_and_succeeds(
        self, transport: Callable[..., _Transport], sleeps: list[float]
    ) -> None:
        transport(http.client.IncompleteRead(b'{"message": {"DO', 4096), _BODY)
        assert _client().get_json(_URL) == _PAYLOAD

    def test_a_garbled_status_line_raises_transient(
        self, transport: Callable[..., _Transport], sleeps: list[float]
    ) -> None:
        """A captive portal or a broken proxy makes ``urlopen`` raise
        ``BadStatusLine`` rather than any ``OSError``; it says nothing at all
        about whether the work exists.
        """
        transport(*[http.client.BadStatusLine("<html>") for _ in range(5)])
        with pytest.raises(Transient):
            _client().get_json(_URL)

    def test_a_truncated_body_is_never_cached_as_an_absence(
        self, transport: Callable[..., _Transport], tmp_path: Path, sleeps: list[float]
    ) -> None:
        cache = Cache(tmp_path / "c")
        transport(*[http.client.IncompleteRead(b"{", 4096) for _ in range(5)])
        with pytest.raises(Transient):
            _client(cache).get_json(_URL)
        assert _records(cache.path) == []

    def test_transient_names_the_url_it_could_not_reach(
        self, transport: Callable[..., _Transport], sleeps: list[float]
    ) -> None:
        """A report saying only "registry unreachable" is unactionable."""
        transport(*_timeouts(5))
        with pytest.raises(Transient) as excinfo:
            _client().get_json(_URL)
        assert _URL in str(excinfo.value)

    def test_a_non_http_scheme_is_rejected_without_opening_anything(
        self, no_network: None
    ) -> None:
        """``file:///etc/passwd`` must never be opened by the registry client.

        ``urlopen`` handles ``file:`` and ``data:`` URLs happily, so a bare
        DOI or a local path reaching this method would silently read the disk
        instead of failing.
        """
        with pytest.raises(ValueError, match="unsupported URL scheme"):
            _client().get_json("file:///etc/passwd")

    def test_a_bare_doi_is_a_programming_error_not_a_retry(
        self, no_network: None, sleeps: list[float]
    ) -> None:
        with pytest.raises(ValueError):
            _client().get_json("10.1016/S0140-6736(03)14065-2")
        assert sleeps == []


class TestRetryAfter:
    """The registry's own stated wait beats our guess — within a cap."""

    def test_429_retry_after_is_honoured(
        self, transport: Callable[..., _Transport], sleeps: list[float]
    ) -> None:
        transport(_http_error(429, retry_after="5"), _BODY)
        assert _client().get_json(_URL) == _PAYLOAD
        assert sleeps == [5.0]

    def test_503_retry_after_is_honoured_too(
        self, transport: Callable[..., _Transport], sleeps: list[float]
    ) -> None:
        """Regression: Retry-After used to be read only for 429.

        Crossref load-sheds with ``503`` plus ``Retry-After``; ignoring the
        header there meant retrying after 1s a service that had just asked
        for 30, which is how a polite client gets rate-limited into a full
        outage. The assertion is that the sleep is the header's value and not
        the first backoff step.
        """
        transport(_http_error(503, retry_after="30"), _BODY)
        assert _client().get_json(_URL) == _PAYLOAD
        assert sleeps == [30.0]

    def test_500_retry_after_is_honoured(
        self, transport: Callable[..., _Transport], sleeps: list[float]
    ) -> None:
        transport(_http_error(500, retry_after="12"), _BODY)
        assert _client().get_json(_URL) == _PAYLOAD
        assert sleeps == [12.0]

    def test_retry_after_is_capped(
        self, transport: Callable[..., _Transport], sleeps: list[float]
    ) -> None:
        """An hour-long Retry-After would stall a whole audit on one entry.

        Honouring it unbounded means a single misconfigured proxy hangs a CI
        job until it is killed, with no output.
        """
        transport(_http_error(429, retry_after="3600"), _BODY)
        assert _client().get_json(_URL) == _PAYLOAD
        assert sleeps == [60.0]

    def test_a_retry_after_http_date_falls_back_to_the_schedule(
        self, transport: Callable[..., _Transport], sleeps: list[float]
    ) -> None:
        """RFC 7231 also permits an HTTP-date. It is not parsed, and must not
        be mistaken for a number of seconds.
        """
        transport(_http_error(503, retry_after="Wed, 21 Oct 2026 07:28:00 GMT"), _BODY)
        assert _client().get_json(_URL) == _PAYLOAD
        assert sleeps == [pytest.approx(1.0 + _URL_JITTER)]

    def test_a_non_decimal_digit_retry_after_does_not_crash(
        self, transport: Callable[..., _Transport], sleeps: list[float]
    ) -> None:
        """``"²".isdigit()`` is True but ``float("²")`` raises ValueError.

        A broken proxy that puts a superscript in the header must cost us one
        ignored header, not an uncaught ValueError escaping the retry loop as
        something no caller is prepared to distinguish from a real defect.
        """
        transport(_http_error(503, retry_after="²"), _BODY)
        assert _client().get_json(_URL) == _PAYLOAD
        assert sleeps == [pytest.approx(1.0 + _URL_JITTER)]

    def test_a_missing_retry_after_uses_the_schedule(
        self, transport: Callable[..., _Transport], sleeps: list[float]
    ) -> None:
        transport(_http_error(503), _BODY)
        assert _client().get_json(_URL) == _PAYLOAD
        assert sleeps == [pytest.approx(1.0 + _URL_JITTER)]

    def test_a_429_without_retry_after_uses_the_schedule(
        self, transport: Callable[..., _Transport], sleeps: list[float]
    ) -> None:
        """PubMed rate-limits with a bare 429 and no header at all."""
        transport(_http_error(429), _BODY)
        assert _client().get_json(_URL) == _PAYLOAD
        assert sleeps == [pytest.approx(1.0 + _URL_JITTER)]

    def test_retry_after_surrounded_by_whitespace_is_still_honoured(
        self, transport: Callable[..., _Transport], sleeps: list[float]
    ) -> None:
        """Header values arrive with whatever spacing the origin sent.

        Treating ``" 30 "`` as unparseable would silently drop back to a 1s
        retry against a registry that had just asked for 30 — the exact
        rate-limit spiral the header exists to prevent, and invisible because
        nothing errors.
        """
        transport(_http_error(503, retry_after=" 30 "), _BODY)
        assert _client().get_json(_URL) == _PAYLOAD
        assert sleeps == [30.0]

    def test_a_negative_retry_after_is_ignored_rather_than_slept_backwards(
        self, transport: Callable[..., _Transport], sleeps: list[float]
    ) -> None:
        """``time.sleep`` raises ValueError on a negative argument, and that
        exception would escape the retry loop as neither a fact nor
        ``Transient``.
        """
        transport(_http_error(503, retry_after="-5"), _BODY)
        assert _client().get_json(_URL) == _PAYLOAD
        assert sleeps == [pytest.approx(1.0 + _URL_JITTER)]


class TestBackoffIsDeterministic:
    """The same URL must sleep the same way in every process, forever.

    A verdict has to be reproducible from a cached registry response, and a
    run that behaves differently each time is one where "it passed on my
    machine" stops meaning anything.
    """

    def test_the_schedule_is_one_two_four_eight_plus_a_pinned_jitter(
        self, transport: Callable[..., _Transport], sleeps: list[float]
    ) -> None:
        """The jitter literal is what pins reproducibility *across* processes.

        A URL-derived jitter computed with the builtin ``hash()`` would be
        stable within one run and different in the next, and only a fixed
        expected value notices that.
        """
        transport(*_timeouts(5))
        with pytest.raises(Transient):
            _client().get_json(_URL)
        assert sleeps == [
            pytest.approx(1.0 + _URL_JITTER),
            pytest.approx(2.0 + _URL_JITTER),
            pytest.approx(4.0 + _URL_JITTER),
            pytest.approx(8.0 + _URL_JITTER),
        ]

    def test_two_runs_of_the_same_url_sleep_identically(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runs: list[list[float]] = []
        for _ in range(2):
            recorded: list[float] = []
            monkeypatch.setattr(time, "sleep", recorded.append)
            monkeypatch.setattr(urllib.request, "urlopen", _Transport(_timeouts(5)))
            with pytest.raises(Transient):
                _client().get_json(_URL)
            runs.append(recorded)
        assert runs[0] == runs[1]

    def test_a_different_url_backs_off_differently(
        self, transport: Callable[..., _Transport], sleeps: list[float]
    ) -> None:
        """Jitter exists to desynchronise clients; a constant offset does not.

        If every request slept exactly 1/2/4/8s, a batch of lookups that all
        failed together would keep retrying in lockstep and keep hammering a
        registry that is already struggling.
        """
        transport(*_timeouts(5))
        with pytest.raises(Transient):
            _client().get_json(_URL_SAME_HOST)
        assert sleeps[0] == pytest.approx(1.0 + _URL_SAME_HOST_JITTER)
        assert sleeps[0] != pytest.approx(1.0 + _URL_JITTER)

    def test_jitter_stays_inside_its_documented_span(
        self, transport: Callable[..., _Transport], sleeps: list[float]
    ) -> None:
        """Jitter must stay small next to the backoff steps it perturbs — a
        jitter that could exceed a step would reorder the schedule.
        """
        transport(*_timeouts(5))
        with pytest.raises(Transient):
            _client().get_json(_URL_OTHER_HOST)
        for step, slept in zip((1.0, 2.0, 4.0, 8.0), sleeps, strict=True):
            assert 0.0 <= slept - step < 0.5

    def test_the_module_never_imports_random(self) -> None:
        """The static half of the guard below, and the half that catches the
        one form monkeypatching cannot.

        ``monkeypatch.setattr(random, "uniform", ...)`` only bites when the
        name is looked up on the module at call time. A module-level ``from
        random import uniform`` binds the real function before any test runs,
        so the patched attribute is never consulted and that guard stays green
        while every backoff has become irreproducible. Reading the source is
        the only check that sees it.
        """
        tree = ast.parse(Path(http_module.__file__).read_text(encoding="utf-8"))
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert "random" not in imported
        assert "secrets" not in imported  # the same defect wearing a better name

    def test_no_randomness_is_consulted(
        self,
        transport: Callable[..., _Transport],
        sleeps: list[float],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``random`` is the obvious way to write jitter and the wrong one.

        Every attribute lookup on the ``random`` module is made to explode, so
        a future ``random.uniform(0, 0.5)`` in the backoff turns this test red
        instead of quietly making runs irreproducible.
        """

        def forbidden(*args: object, **kwargs: object) -> float:
            raise AssertionError(
                "http.py consulted `random`: a backoff that differs between "
                "runs makes a verdict irreproducible"
            )

        for name in (
            "random",
            "uniform",
            "randint",
            "randrange",
            "getrandbits",
            "choice",
            "SystemRandom",
        ):
            monkeypatch.setattr(random, name, forbidden)

        transport(*_timeouts(5))
        with pytest.raises(Transient):
            _client().get_json(_URL)
        assert len(sleeps) == 4


class TestPerHostThrottling:
    """Politeness is per host, and it survives across separate calls."""

    def test_a_second_call_to_the_same_host_waits(
        self, clock: _Clock, transport: Callable[..., _Transport]
    ) -> None:
        """The interval must be enforced between *calls*, not just between the
        retries of one call — otherwise a 500-entry bibliography opens 500
        back-to-back sockets and earns a 429.
        """
        transport(_BODY, _BODY)
        client = Client(None, min_interval=0.2)
        client.get_json(_URL)
        client.get_json(_URL_SAME_HOST)
        assert clock.slept == [pytest.approx(0.2)]

    def test_the_interval_is_maintained_over_a_run_of_calls(
        self, clock: _Clock, transport: Callable[..., _Transport]
    ) -> None:
        transport(_BODY, _BODY, _BODY)
        client = Client(None, min_interval=0.2)
        for _ in range(3):
            client.get_json(_URL)
        assert clock.slept == [pytest.approx(0.2), pytest.approx(0.2)]

    def test_another_host_is_not_made_to_wait_out_the_first(
        self, clock: _Clock, transport: Callable[..., _Transport]
    ) -> None:
        """Crossref's cooldown is not PubMed's.

        A single global timestamp would serialise two independent registries
        and double the wall-clock time of every audit for no politeness gain.
        """
        transport(_BODY, _BODY)
        client = Client(None, min_interval=0.2)
        client.get_json(_URL)
        client.get_json(_URL_OTHER_HOST)
        assert clock.slept == []

    def test_two_clients_do_not_throttle_each_other(
        self, clock: _Clock, transport: Callable[..., _Transport]
    ) -> None:
        """The timestamps are instance state; module-level state would make an
        unrelated second Client (a test, a second registry) wait.
        """
        transport(_BODY, _BODY)
        Client(None, min_interval=0.2).get_json(_URL)
        Client(None, min_interval=0.2).get_json(_URL)
        assert clock.slept == []

    def test_a_cache_hit_is_not_throttled(
        self, clock: _Clock, transport: Callable[..., _Transport], tmp_path: Path
    ) -> None:
        """Re-running an audit entirely from cache must not take one fifth of
        a second per reference; on 800 references that is nearly three minutes
        of sleeping for zero requests.
        """
        cache = Cache(tmp_path / "c")
        transport(_BODY)
        client = Client(cache, min_interval=0.2)
        client.get_json(_URL)
        clock.slept.clear()
        client.get_json(_URL)
        assert clock.slept == []


class TestCacheRecordFormat:
    """``fetched_at`` is stored, and it is what the TTL is measured against."""

    def test_fetched_at_is_stored_alongside_url_and_payload(self, tmp_path: Path) -> None:
        cache = Cache(tmp_path / "c")
        cache.put(_URL, {"url": _URL, "payload": _PAYLOAD})
        record = json.loads(_sole_record(cache.path).read_text(encoding="utf-8"))
        assert record["url"] == _URL
        assert record["payload"] == _PAYLOAD
        assert datetime.fromisoformat(record["fetched_at"]).tzinfo is not None

    def test_get_returns_the_payload_not_the_envelope(self, tmp_path: Path) -> None:
        """A caller that had to strip ``fetched_at``/``url`` itself would
        eventually forget, and a registry parser would see a field that is not
        the registry's.
        """
        cache = Cache(tmp_path / "c")
        cache.put(_URL, {"url": _URL, "payload": _PAYLOAD})
        assert cache.get(_URL) == _PAYLOAD

    def test_an_entry_inside_the_ttl_is_served(self, tmp_path: Path) -> None:
        cache = Cache(tmp_path / "c", ttl_days=90)
        cache.put(_URL, {"url": _URL, "payload": _PAYLOAD})
        _set_fetched_at(_sole_record(cache.path), days_ago=89)
        assert cache.get(_URL) == _PAYLOAD

    def test_an_entry_past_the_ttl_is_a_miss(self, tmp_path: Path) -> None:
        cache = Cache(tmp_path / "c", ttl_days=90)
        cache.put(_URL, {"url": _URL, "payload": _PAYLOAD})
        _set_fetched_at(_sole_record(cache.path), days_ago=91)
        assert cache.get(_URL) is None

    def test_the_ttl_is_measured_from_fetched_at_not_the_file_mtime(
        self, tmp_path: Path
    ) -> None:
        """Rewriting the record leaves the mtime at "now" while ``fetched_at``
        says a year ago; the entry must still be stale.

        Reading staleness off the filesystem instead would silently reset
        every entry's age whenever a cache directory is copied, restored from
        a backup, or unpacked from a CI artifact.
        """
        cache = Cache(tmp_path / "c", ttl_days=90)
        cache.put(_URL, {"url": _URL, "payload": _PAYLOAD})
        path = _sole_record(cache.path)
        _set_fetched_at(path, days_ago=365)
        assert path.stat().st_mtime > (datetime.now(UTC).timestamp() - 60)
        assert cache.get(_URL) is None

    def test_a_shorter_ttl_expires_an_entry_the_default_would_keep(
        self, tmp_path: Path
    ) -> None:
        cache = Cache(tmp_path / "c", ttl_days=1)
        cache.put(_URL, {"url": _URL, "payload": _PAYLOAD})
        _set_fetched_at(_sole_record(cache.path), days_ago=2)
        assert cache.get(_URL) is None

    def test_clear_discards_every_entry_and_keeps_the_directory(
        self, tmp_path: Path
    ) -> None:
        cache = Cache(tmp_path / "c")
        cache.put(_URL, {"url": _URL, "payload": _PAYLOAD})
        cache.clear()
        assert cache.path.is_dir()
        assert _records(cache.path) == []
        assert cache.get(_URL) is None

    def test_a_successful_put_leaves_no_temp_file_behind(self, tmp_path: Path) -> None:
        """The atomic write goes through a temp file in the same directory; a
        leaked one per lookup would fill a user's cache with junk.
        """
        cache = Cache(tmp_path / "c")
        cache.put(_URL, {"url": _URL, "payload": _PAYLOAD})
        assert list(cache.path.rglob(".tmp-*")) == []


def _set_fetched_at(path: Path, *, days_ago: float) -> None:
    """Backdate an existing cache record's ``fetched_at``."""
    record = json.loads(path.read_text(encoding="utf-8"))
    record["fetched_at"] = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    path.write_text(json.dumps(record), encoding="utf-8")


class TestCorruptCacheIsAMiss:
    """Every unreadable record means "ask the registry again", never a crash.

    A cache is the one part of this tool a user is likely to poke at by hand,
    and the one part a crash-during-write can truncate. An audit that dies on
    a bad cache file makes the tool look broken and gives no verdict at all.
    """

    @pytest.mark.parametrize(
        ("label", "contents"),
        [
            ("empty file", ""),
            ("truncated mid-object", '{"fetched_at": "{now}", "pay'),
            ("not json at all", "<html>502 Bad Gateway</html>"),
            ("json array", "[1, 2, 3]"),
            ("bare json string", '"just a string"'),
            ("missing fetched_at", '{"url": "x", "payload": {"a": 1}}'),
            ("missing payload", '{"fetched_at": "{now}"}'),
            ("unparseable fetched_at", '{"fetched_at": "yesterday", "payload": {"a": 1}}'),
            ("null fetched_at", '{"fetched_at": null, "payload": {"a": 1}}'),
            ("payload is a string", '{"fetched_at": "{now}", "payload": "x"}'),
            ("payload is null", '{"fetched_at": "{now}", "payload": null}'),
            ("payload is a list", '{"fetched_at": "{now}", "payload": [1, 2]}'),
        ],
    )
    def test_a_damaged_record_is_a_miss(
        self, tmp_path: Path, label: str, contents: str
    ) -> None:
        """``{now}`` is substituted deliberately, and this is the whole point of
        the test rather than a detail.

        These rows first carried a hardcoded ``"2026-01-01T00:00:00+00:00"``,
        which is months past the default 90-day TTL — so the rows whose damage
        is *the payload's type* (``"x"``, ``null``, a list) were returning
        ``None`` because the record had expired, not because ``Cache.get``
        rejected the payload. Deleting the ``isinstance(payload, dict)`` guard
        from the module left the whole suite green. An in-TTL timestamp is what
        makes the assertion attributable to the damage; see
        :meth:`test_a_fresh_undamaged_record_is_served` for the control that
        proves the timestamp itself is not doing the work.
        """
        cache = Cache(tmp_path / "c")
        cache.put(_URL, {"url": _URL, "payload": _PAYLOAD})
        record = contents.replace("{now}", datetime.now(UTC).isoformat())
        _sole_record(cache.path).write_text(record, encoding="utf-8")
        assert cache.get(_URL) is None, label

    def test_a_fresh_undamaged_record_is_served(self, tmp_path: Path) -> None:
        """The control for :meth:`test_a_damaged_record_is_a_miss`: a record
        written by hand in exactly the same way, with the same in-TTL
        ``fetched_at`` and an undamaged payload, must come back.

        Without it, a ``Cache.get`` that returned ``None`` unconditionally
        would satisfy every row of the table above.
        """
        cache = Cache(tmp_path / "c")
        cache.put(_URL, {"url": _URL, "payload": _PAYLOAD})
        _sole_record(cache.path).write_text(
            json.dumps({"fetched_at": datetime.now(UTC).isoformat(), "payload": {"a": 1}}),
            encoding="utf-8",
        )
        assert cache.get(_URL) == {"a": 1}

    def test_a_payload_that_is_not_a_json_object_is_a_miss(self, tmp_path: Path) -> None:
        """``Cache.get`` promises ``dict | None``, and :meth:`Client.get_text`
        depends on it: text is stored wrapped as ``{"text": ...}`` precisely
        because a bare string is not a legal payload.

        Returning the bare string instead would hand a registry parser a value
        of a type it never type-checks, and — the quieter half — would make
        every ``get_text`` cache entry written by an older or third-party
        version replay as a string where a dict is expected.
        """
        cache = Cache(tmp_path / "c")
        cache.put(_URL, {"url": _URL, "payload": _PAYLOAD})
        path = _sole_record(cache.path)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["payload"] = "PMID- 28453765"  # what a naive get_text cache would store
        path.write_text(json.dumps(record), encoding="utf-8")
        assert cache.get(_URL) is None

    def test_a_naive_fetched_at_is_a_miss_not_a_crash(self, tmp_path: Path) -> None:
        """A timestamp without a timezone cannot be subtracted from an aware
        ``datetime.now(timezone.utc)`` — Python raises TypeError.

        A user who hand-edits a record while debugging (or a record written by
        any other tool) would otherwise abort the whole audit with a traceback
        from inside the cache.
        """
        cache = Cache(tmp_path / "c")
        cache.put(_URL, {"url": _URL, "payload": _PAYLOAD})
        path = _sole_record(cache.path)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["fetched_at"] = "2026-01-01T00:00:00"  # no offset: naive
        path.write_text(json.dumps(record), encoding="utf-8")
        assert cache.get(_URL) is None

    def test_undecodable_bytes_in_a_record_are_a_miss(self, tmp_path: Path) -> None:
        """A half-flushed record can end mid-UTF-8-sequence."""
        cache = Cache(tmp_path / "c")
        cache.put(_URL, {"url": _URL, "payload": _PAYLOAD})
        _sole_record(cache.path).write_bytes(b'{"fetched_at": "2026-01-01T00:00:00+00:00", "\xff')
        assert cache.get(_URL) is None

    def test_a_missing_record_is_a_miss(self, tmp_path: Path) -> None:
        assert Cache(tmp_path / "c").get("never stored") is None

    def test_a_write_that_cannot_happen_is_swallowed(self, tmp_path: Path) -> None:
        """The lookup already succeeded; a full disk must not turn that into a
        crash — the caller has a valid registry answer in hand either way.

        It does warn once, which is asserted separately in
        :class:`TestAWriteThatFailsAfterConstructionIsAnnounced`; suppressed
        here so this test keeps saying only "no exception, and no stale
        answer", which is the property it was written for.
        """
        cache = Cache(tmp_path / "c")
        cache.put(_URL, {"url": _URL, "payload": _PAYLOAD})
        shard = _sole_record(cache.path).parent
        shutil.rmtree(shard)
        shard.write_text("a file where the shard directory should be", encoding="utf-8")

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            cache.put(_URL, {"url": _URL, "payload": {"different": True}})  # must not raise
        assert cache.get(_URL) is None

    def test_a_corrupt_record_makes_the_client_refetch(
        self, transport: Callable[..., _Transport], tmp_path: Path
    ) -> None:
        cache = Cache(tmp_path / "c")
        transport(_BODY, _BODY)
        assert _client(cache).get_json(_URL) == _PAYLOAD
        _sole_record(cache.path).write_text("{oh no", encoding="utf-8")
        assert _client(cache).get_json(_URL) == _PAYLOAD


class TestCacheKeys:
    """Registry keys are DOIs and URLs; filenames are not."""

    def test_a_doi_with_slashes_and_parentheses_round_trips(self, tmp_path: Path) -> None:
        """``10.1016/S0140-6736(03)14065-2`` is a real and common Lancet DOI.

        Used directly as a filename its slash would create a phantom
        subdirectory and its parentheses would need shell quoting; either way
        the entry would never be read back and the cache would silently never
        hit.
        """
        cache = Cache(tmp_path / "c")
        key = "10.1016/S0140-6736(03)14065-2"
        cache.put(key, {"url": _URL, "payload": {"doi": key}})
        assert cache.get(key) == {"doi": key}
        # Round-tripping alone is not enough: `mkdir(parents=True)` would
        # happily create `c/xx/10.1016/` and read it straight back, so assert
        # the name carries none of the key's punctuation and sits exactly one
        # shard below the root.
        record = _sole_record(cache.path)
        assert record.stem.isalnum()
        assert record.parent.parent == cache.path

    def test_a_non_ascii_key_round_trips(self, tmp_path: Path) -> None:
        """Search keys are built from titles and author names: `Aragonés`,
        `Malats i Riera`, and the mojibake `AragonÃ©s` a registry hands back.
        """
        cache = Cache(tmp_path / "c")
        key = "search:Aragonés Núria — étude sur le café"
        cache.put(key, {"url": _URL, "payload": {"hit": True}})
        assert cache.get(key) == {"hit": True}
        # A filesystem that normalises Unicode (APFS does, ext4 does not)
        # would make two differently-composed spellings of the same name share
        # a file on one machine and not on another; an ASCII digest cannot.
        assert _sole_record(cache.path).stem.isascii()

    def test_a_key_longer_than_a_filename_limit_round_trips(self, tmp_path: Path) -> None:
        """A Crossref ``query.bibliographic`` URL carries a whole title and
        author list and easily exceeds the 255-byte filename limit; using the
        key as a name raises ENAMETOOLONG on every such lookup.
        """
        cache = Cache(tmp_path / "c")
        key = "https://api.crossref.org/works?query.bibliographic=" + ("shift+work+" * 100)
        cache.put(key, {"url": _URL, "payload": {"long": True}})
        assert cache.get(key) == {"long": True}

    def test_similar_keys_do_not_collide(self, tmp_path: Path) -> None:
        cache = Cache(tmp_path / "c")
        keys = [
            "10.1016/S0140-6736(03)14065-2",
            "10.1016/S0140-6736(05)66455-0",
            "10.1016/S0140-6736(03)14065-3",
            "10.1016/s0140-6736(03)14065-2",  # differs only in case
            "10.1016%2FS0140-6736(03)14065-2",  # differs only in encoding
            "10.1016/S0140-6736(03)14065-2?x=1",
        ]
        for index, key in enumerate(keys):
            cache.put(key, {"url": _URL, "payload": {"n": index}})
        assert [cache.get(key) for key in keys] == [{"n": i} for i in range(len(keys))]
        assert len(_records(cache.path)) == len(keys)

    def test_a_realistic_bibliography_of_keys_produces_one_record_each(
        self, tmp_path: Path
    ) -> None:
        """Six hand-picked keys are not enough to prove a key space is safe.

        Shortening the digest is the obvious "tidy up those filenames" change,
        and with six keys it passes: truncating sha256 to three hex characters
        (4096 buckets) left the whole suite green. At this size a shortened
        digest collides with near-certainty, and a collision here is the worst
        kind of failure the cache can produce — not a crash but one paper's
        registry record served for another paper's DOI, which is a *wrong
        verdict* on a reference the tool has said it verified.

        The keys share a long common prefix on purpose, so a hash of only the
        first N characters of the key fails too.
        """
        cache = Cache(tmp_path / "c")
        keys = [f"10.1016/j.envint.2021.10{n:04d}" for n in range(1000)]
        for index, key in enumerate(keys):
            cache.put(key, {"url": _URL, "payload": {"n": index}})
        assert len(_records(cache.path)) == len(keys)
        assert [cache.get(key) for key in keys] == [{"n": i} for i in range(len(keys))]

    def test_every_record_is_a_plain_file_under_the_cache_root(
        self, tmp_path: Path
    ) -> None:
        """A key's own separators must not leak into the directory layout: a
        cache directory a user can `rm -rf` and a `du` can measure is part of
        the tool being trustworthy on someone's laptop.
        """
        cache = Cache(tmp_path / "c")
        for key in ("10.1016/a/b/c", "https://example.org/x?y=z", "../../escape"):
            cache.put(key, {"url": _URL, "payload": {"k": key}})
            assert cache.get(key) == {"k": key}
        for path in _records(cache.path):
            assert path.parent.parent == cache.path
            assert path.name.endswith(".json")
            assert path.stem.isalnum()


class TestCacheAndNetworkTogether:
    """Where the cache decides whether a socket is opened at all."""

    def test_a_cache_hit_avoids_a_second_request(
        self, transport: Callable[..., _Transport], tmp_path: Path
    ) -> None:
        cache = Cache(tmp_path / "c")
        fake = transport(_BODY, _BODY)
        assert _client(cache).get_json(_URL) == _PAYLOAD
        assert _client(cache).get_json(_URL) == _PAYLOAD
        assert fake.calls == 1

    def test_an_entry_past_the_ttl_is_refetched(
        self, transport: Callable[..., _Transport], tmp_path: Path
    ) -> None:
        cache = Cache(tmp_path / "c", ttl_days=90)
        fake = transport(_BODY, b'{"message": {"DOI": "10.1000/fresher"}}')
        assert _client(cache).get_json(_URL) == _PAYLOAD
        _set_fetched_at(_sole_record(cache.path), days_ago=91)
        assert _client(cache).get_json(_URL) == {"message": {"DOI": "10.1000/fresher"}}
        assert fake.calls == 2

    def test_the_cache_key_overrides_the_url(
        self, transport: Callable[..., _Transport], tmp_path: Path
    ) -> None:
        """Registry modules key on the DOI, not on the URL that carried it, so
        that a batch fetch and a later single fetch of the same work share one
        entry instead of quietly doubling the request count.
        """
        cache = Cache(tmp_path / "c")
        fake = transport(_BODY, _BODY)
        client = _client(cache)
        assert client.get_json(_URL, cache_key="doi:10.1000/example") == _PAYLOAD
        assert client.get_json(_URL_SAME_HOST, cache_key="doi:10.1000/example") == _PAYLOAD
        assert fake.calls == 1

    def test_different_cache_keys_on_one_url_are_separate_entries(
        self, transport: Callable[..., _Transport], tmp_path: Path
    ) -> None:
        cache = Cache(tmp_path / "c")
        fake = transport(_BODY, _BODY)
        client = _client(cache)
        client.get_json(_URL, cache_key="a")
        client.get_json(_URL, cache_key="b")
        assert fake.calls == 2

    def test_a_client_without_a_cache_refetches_every_time(
        self, transport: Callable[..., _Transport]
    ) -> None:
        fake = transport(_BODY, _BODY)
        client = _client(None)
        assert client.get_json(_URL) == _PAYLOAD
        assert client.get_json(_URL) == _PAYLOAD
        assert fake.calls == 2

    def test_a_client_without_a_cache_does_not_crash_on_a_404(
        self, transport: Callable[..., _Transport]
    ) -> None:
        """The absence-marker write must be skipped, not attempted on None."""
        transport(_http_error(404))
        assert _client(None).get_json(_URL) is None


class TestOffline:
    """``--offline`` replays a previous run and never opens a socket."""

    def test_a_cache_hit_is_served_without_urlopen(
        self, no_network: None, tmp_path: Path
    ) -> None:
        cache = Cache(tmp_path / "c")
        cache.put(_URL, {"url": _URL, "payload": _PAYLOAD})
        assert _client(cache, offline=True).get_json(_URL) == _PAYLOAD

    def test_a_miss_raises_transient_without_urlopen(
        self, no_network: None, tmp_path: Path
    ) -> None:
        """A miss is ignorance, not absence: it must reach the caller as the
        same exception an unreachable registry produces, so the entry is
        reported ``UNCHECKED`` rather than silently fetched or, far worse,
        reported as a work that does not exist.
        """
        with pytest.raises(Transient):
            _client(Cache(tmp_path / "c"), offline=True).get_json(_URL)

    def test_a_miss_without_any_cache_at_all_raises_transient(
        self, no_network: None
    ) -> None:
        with pytest.raises(Transient):
            _client(None, offline=True).get_json(_URL)

    def test_an_expired_entry_offline_raises_rather_than_being_served_stale(
        self, no_network: None, tmp_path: Path
    ) -> None:
        cache = Cache(tmp_path / "c", ttl_days=90)
        cache.put(_URL, {"url": _URL, "payload": _PAYLOAD})
        _set_fetched_at(_sole_record(cache.path), days_ago=91)
        with pytest.raises(Transient):
            _client(cache, offline=True).get_json(_URL)

    def test_offline_still_reads_the_cache_when_refresh_was_also_asked_for(
        self, no_network: None, tmp_path: Path
    ) -> None:
        """``--refresh --offline`` is contradictory, and the safe resolution
        is to serve the cache: refusing every entry would report a whole clean
        bibliography as ``UNCHECKED``.
        """
        cache = Cache(tmp_path / "c")
        cache.put(_URL, {"url": _URL, "payload": _PAYLOAD})
        client = _client(cache, offline=True, refresh=True)
        assert client.get_json(_URL) == _PAYLOAD

    def test_offline_does_not_sleep_on_throttling(
        self, no_network: None, clock: _Clock, tmp_path: Path
    ) -> None:
        cache = Cache(tmp_path / "c")
        cache.put(_URL, {"url": _URL, "payload": _PAYLOAD})
        client = Client(cache, offline=True, min_interval=0.2)
        client.get_json(_URL)
        client.get_json(_URL)
        assert clock.slept == []


class TestRefresh:
    """``--refresh`` re-asks the registry but keeps the cache useful."""

    def test_refresh_skips_the_cached_copy(
        self, transport: Callable[..., _Transport], tmp_path: Path
    ) -> None:
        cache = Cache(tmp_path / "c")
        cache.put(_URL, {"url": _URL, "payload": {"stale": True}})
        transport(_BODY)
        assert _client(cache, refresh=True).get_json(_URL) == _PAYLOAD

    def test_refresh_still_writes_what_it_fetched(
        self, transport: Callable[..., _Transport], tmp_path: Path
    ) -> None:
        """Skipping the write as well would mean a ``--refresh`` run leaves the
        cache holding the values it just proved out of date, so the very next
        ordinary run reports on stale data.
        """
        cache = Cache(tmp_path / "c")
        cache.put(_URL, {"url": _URL, "payload": {"stale": True}})
        fake = transport(_BODY)
        _client(cache, refresh=True).get_json(_URL)

        assert _client(cache).get_json(_URL) == _PAYLOAD
        assert fake.calls == 1

    def test_refresh_replaces_a_stale_entry_rather_than_adding_one(
        self, transport: Callable[..., _Transport], tmp_path: Path
    ) -> None:
        cache = Cache(tmp_path / "c")
        cache.put(_URL, {"url": _URL, "payload": {"stale": True}})
        transport(_BODY)
        _client(cache, refresh=True).get_json(_URL)
        assert len(_records(cache.path)) == 1

    def test_refresh_records_a_newly_confirmed_absence(
        self, transport: Callable[..., _Transport], tmp_path: Path
    ) -> None:
        """A DOI that used to resolve and now 404s (a withdrawn deposit) must
        end the refresh run cached as absent, not as its old payload.
        """
        cache = Cache(tmp_path / "c")
        cache.put(_URL, {"url": _URL, "payload": _PAYLOAD})
        transport(_http_error(404))
        assert _client(cache, refresh=True).get_json(_URL) is None
        assert _client(cache).get_json(_URL) is None


class TestJsonAndText:
    """The two public accessors, and what they do with a body."""

    def test_get_json_decodes_the_body(
        self, transport: Callable[..., _Transport]
    ) -> None:
        transport(_BODY)
        assert _client().get_json(_URL) == _PAYLOAD

    def test_malformed_json_raises_rather_than_becoming_none_or_transient(
        self, transport: Callable[..., _Transport]
    ) -> None:
        """A 200 carrying broken JSON is neither a confirmed absence nor an
        outage; folding it into either would turn a real registry data problem
        into a silent pass.
        """
        transport(b"<html>maintenance</html>")
        with pytest.raises(json.JSONDecodeError):
            _client().get_json(_URL)

    def test_malformed_json_is_not_cached(
        self, transport: Callable[..., _Transport], tmp_path: Path
    ) -> None:
        """Caching an unparseable body would make the failure permanent and
        survive the outage that caused it.
        """
        cache = Cache(tmp_path / "c")
        transport(b"<html>maintenance</html>")
        with pytest.raises(json.JSONDecodeError):
            _client(cache).get_json(_URL)
        assert _records(cache.path) == []

    def test_get_text_returns_a_string(
        self, transport: Callable[..., _Transport]
    ) -> None:
        transport(b"PMID- 28453765\nTI  - Shift work and colorectal cancer risk\n")
        text = _client().get_text(_URL)
        assert text is not None
        assert text.startswith("PMID- 28453765")

    def test_get_text_round_trips_through_the_cache_as_a_string(
        self, transport: Callable[..., _Transport], tmp_path: Path
    ) -> None:
        """The cache stores JSON *objects* only, so text is wrapped as
        ``{"text": ...}`` on the way in and must be unwrapped on the way out.

        Storing the bare string instead would be rejected by ``Cache.get``'s
        own type check and the entry would never hit again — a cache that
        silently never works, which nothing else in the tool would notice.

        The body is a real, wrapped MEDLINE record: the continuation lines
        beginning with six spaces are part of the format and must survive.
        """
        medline = (
            b"PMID- 28453765\n"
            b"TI  - Night shift work and colorectal cancer risk in the MCC-Spain\n"
            b"      case-control study\n"
            b"AU  - Papantoniou K\n"
        )
        cache = Cache(tmp_path / "c")
        fake = transport(medline)
        first = _client(cache).get_text(_URL)
        second = _client(cache).get_text(_URL)
        assert first == second == medline.decode("utf-8")
        assert "\n      case-control study" in str(second)
        assert fake.calls == 1

    def test_get_text_replaces_undecodable_bytes_instead_of_failing(
        self, transport: Callable[..., _Transport]
    ) -> None:
        """This fixture is deliberately mis-encoded: ``\\xe9`` is latin-1 `é`
        in a stream declared UTF-8, which is exactly the defect that produces
        mojibake surnames like `AragonÃ©s` elsewhere in the pipeline. Do not
        "fix" it — a strict decode here would abort an entire audit over one
        byte in one abstract.
        """
        transport(b"AU  - Aragon\xe9s N\n")
        text = _client().get_text(_URL)
        assert text is not None
        assert "�" in text

    def test_get_text_returns_none_for_a_404(
        self, transport: Callable[..., _Transport]
    ) -> None:
        transport(_http_error(404))
        assert _client().get_text(_URL) is None

    def test_get_text_replays_a_cached_404_as_none(
        self, transport: Callable[..., _Transport], tmp_path: Path
    ) -> None:
        """The absence marker travels the same cache path as a real payload,
        but ``get_text`` unwraps that payload with ``payload["text"]``.

        A marker leaking through would not merely be wrong here, it would be a
        ``KeyError`` from inside the cache on the *second* run only — a failure
        no first run ever shows.
        """
        cache = Cache(tmp_path / "c")
        fake = transport(_http_error(404))
        assert _client(cache).get_text(_URL) is None
        assert _client(cache).get_text(_URL) is None
        assert fake.calls == 1

    def test_get_text_raises_transient_on_an_outage(
        self, transport: Callable[..., _Transport], sleeps: list[float]
    ) -> None:
        transport(*_timeouts(5))
        with pytest.raises(Transient):
            _client().get_text(_URL)


class TestRequestShape:
    """What actually goes out on the wire."""

    def test_the_default_user_agent_identifies_bibaudit(
        self, transport: Callable[..., _Transport]
    ) -> None:
        fake = transport(_BODY)
        _client().get_json(_URL)
        assert fake.requests[0].get_header("User-agent", "").startswith("bibaudit/")

    def test_mailto_is_appended_for_the_polite_pool(
        self, transport: Callable[..., _Transport]
    ) -> None:
        """Crossref grants lower latency and priority during load-shedding to
        requests whose User-Agent carries a contact address in this exact
        form. Getting it wrong is a silent performance regression: nothing
        ever errors, the audit is just slow and gets rate-limited first.
        """
        fake = transport(_BODY)
        _client(mailto="me@example.org").get_json(_URL)
        assert "mailto=me@example.org" in fake.requests[0].get_header("User-agent", "")

    def test_an_explicit_user_agent_replaces_the_default_entirely(
        self, transport: Callable[..., _Transport]
    ) -> None:
        fake = transport(_BODY)
        _client(user_agent="my-tool/1.0", mailto="me@example.org").get_json(_URL)
        assert fake.requests[0].get_header("User-agent") == "my-tool/1.0"

    def test_caller_headers_are_added(
        self, transport: Callable[..., _Transport]
    ) -> None:
        """PubMed's api_key and Accept negotiation live in the registry
        modules; this class must not need to know their vocabulary.
        """
        fake = transport(_BODY)
        _client().get_json(_URL, headers={"Accept": "application/json"})
        assert fake.requests[0].get_header("Accept") == "application/json"

    def test_caller_headers_win_over_the_default(
        self, transport: Callable[..., _Transport]
    ) -> None:
        fake = transport(_BODY)
        _client().get_json(_URL, headers={"User-Agent": "caller/2.0"})
        assert fake.requests[0].get_header("User-agent") == "caller/2.0"

    def test_the_client_timeout_is_forwarded_to_urlopen(
        self, transport: Callable[..., _Transport]
    ) -> None:
        """Without it urlopen blocks on the OS default, which on a hung TCP
        connection can be minutes — long enough that a CI job is killed with
        no report at all, the one outcome worse than a wrong report.
        """
        fake = transport(_BODY)
        _client(timeout=7.5).get_json(_URL)
        assert fake.timeouts == [7.5]

    def test_the_method_is_get(self, transport: Callable[..., _Transport]) -> None:
        fake = transport(_BODY)
        _client().get_json(_URL)
        assert fake.requests[0].get_method() == "GET"

    def test_a_url_with_parentheses_is_sent_unmangled(
        self, transport: Callable[..., _Transport]
    ) -> None:
        url = "https://api.crossref.org/works/10.1016%2FS0140-6736%2803%2914065-2"
        fake = transport(_BODY)
        _client().get_json(url)
        assert fake.urls == [url]


class TestDefaultCacheDir:
    def test_xdg_cache_home_is_honoured(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Some macOS users relocate caches onto a non-backed-up volume."""
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        assert default_cache_dir() == tmp_path / "xdg" / "bibaudit"

    def test_macos_falls_back_to_library_caches(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(sys, "platform", "darwin")
        assert default_cache_dir() == tmp_path / "Library" / "Caches" / "bibaudit"

    def test_other_platforms_fall_back_to_dot_cache(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(sys, "platform", "linux")
        assert default_cache_dir() == tmp_path / ".cache" / "bibaudit"

    def test_the_directory_is_not_created_as_a_side_effect(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Asking where the cache would live must not litter a user's home
        directory — ``bibaudit --help`` should create nothing.
        """
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        assert not default_cache_dir().exists()


def _blocked_root(tmp_path: Path) -> Path:
    """A cache root whose ``mkdir(parents=True)`` cannot succeed.

    A *file* stands where a parent directory would have to be, so
    ``mkdir(parents=True)`` raises ``NotADirectoryError``. Chosen over
    ``chmod(0o500)`` because a permission bit is not a barrier to uid 0 and
    much CI runs as root, which would make the whole class of tests below
    silently pass by not reproducing the failure at all. This shape also *is*
    one of the real cases: ``XDG_CACHE_HOME`` set to a path that turns out to
    be a file.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("this is a file", encoding="utf-8")
    return blocker / "bibaudit"


class TestAnUncreatableCacheDegrades:
    """A cache that cannot be created is a slower run, never a dead one.

    ``Cache.__init__`` used to let ``mkdir``'s ``OSError`` escape, and both
    ``audit.run`` and ``cli._run_cache`` construct a Cache without a ``try``.
    On a read-only image layer or an unwritable ``$XDG_CACHE_HOME`` — a
    locked-down CI container, which is exactly where a citation checker is
    meant to run — ``bibaudit check`` therefore died with a bare traceback
    before looking at one reference. That also contradicted ``Cache.put``'s
    own docstring, which promises a caching problem never becomes a crash.

    The danger in fixing it is the opposite one, and it is why the second half
    of this class exists: "carry on without a cache" must not become "carry on
    without checking". A degraded run has to reach the same verdicts, and an
    ``--offline`` run with no cache has to stay loudly ignorant rather than
    quietly clean.
    """

    def test_construction_does_not_raise(self, tmp_path: Path) -> None:
        with pytest.warns(RuntimeWarning):
            Cache(_blocked_root(tmp_path))

    def test_the_warning_names_the_directory_and_the_consequence(
        self, tmp_path: Path
    ) -> None:
        """Silent degradation is its own defect: a user who never sees this
        line has no way to explain why every run re-fetches everything and why
        ``--offline`` reports the whole bibliography ``UNCHECKED``.
        """
        root = _blocked_root(tmp_path)
        with pytest.warns(RuntimeWarning) as caught:
            Cache(root)
        message = str(caught[0].message)
        assert str(root) in message
        assert "continuing without one" in message
        assert "--cache-dir" in message

    def test_usable_reports_the_degradation(self, tmp_path: Path) -> None:
        with pytest.warns(RuntimeWarning):
            cache = Cache(_blocked_root(tmp_path))
        assert cache.usable is False
        assert Cache(tmp_path / "fine").usable is True

    def test_get_and_put_are_inert_rather_than_raising(self, tmp_path: Path) -> None:
        with pytest.warns(RuntimeWarning):
            cache = Cache(_blocked_root(tmp_path))
        cache.put(_URL, {"url": _URL, "payload": _PAYLOAD})  # must not raise
        assert cache.get(_URL) is None

    def test_clear_explains_instead_of_raising(self, tmp_path: Path) -> None:
        """``bibaudit cache clear`` is the same constructor crash one command
        later: ``cli._run_cache`` builds a Cache and calls ``clear`` with no
        ``try`` around either.
        """
        with pytest.warns(RuntimeWarning):
            cache = Cache(_blocked_root(tmp_path))
        with pytest.warns(RuntimeWarning, match="continuing without one"):
            cache.clear()

    def test_a_lookup_still_returns_the_registry_payload(
        self, transport: Callable[..., _Transport], tmp_path: Path
    ) -> None:
        with pytest.warns(RuntimeWarning):
            cache = Cache(_blocked_root(tmp_path))
        transport(_BODY)
        assert _client(cache).get_json(_URL) == _PAYLOAD

    def test_every_lookup_refetches_because_nothing_is_stored(
        self, transport: Callable[..., _Transport], tmp_path: Path
    ) -> None:
        """The honest cost of the degradation, pinned so it stays a cost and
        not a stale answer: an inert cache must behave exactly like no cache,
        never like a cache that returns something it never wrote.
        """
        with pytest.warns(RuntimeWarning):
            cache = Cache(_blocked_root(tmp_path))
        fake = transport(_BODY, _BODY)
        assert _client(cache).get_json(_URL) == _PAYLOAD
        assert _client(cache).get_json(_URL) == _PAYLOAD
        assert fake.calls == 2

    # -- the true positives that must survive the degradation --------------

    def test_a_404_is_still_a_fact(
        self, transport: Callable[..., _Transport], tmp_path: Path
    ) -> None:
        """The absence-marker write is skipped, and the *answer* is unchanged.

        A fabricated DOI is the finding this tool exists for. If losing the
        cache turned a confirmed 404 into anything other than ``None`` — a
        ``Transient``, or a crash on the skipped ``put`` — the one entry in a
        bibliography that is actually made up would come back ``UNCHECKED``,
        which does not fail a build.
        """
        with pytest.warns(RuntimeWarning):
            cache = Cache(_blocked_root(tmp_path))
        transport(_http_error(404))
        assert _client(cache).get_json(_URL) is None

    def test_an_outage_is_still_transient(
        self,
        transport: Callable[..., _Transport],
        sleeps: list[float],
        tmp_path: Path,
    ) -> None:
        """The other side of the same rule: a dead cache must not make an
        unreachable registry look like a confirmed absence.
        """
        with pytest.warns(RuntimeWarning):
            cache = Cache(_blocked_root(tmp_path))
        transport(*_timeouts(5))
        with pytest.raises(Transient):
            _client(cache).get_json(_URL)

    def test_offline_with_a_dead_cache_raises_rather_than_going_quiet(
        self, no_network: None, tmp_path: Path
    ) -> None:
        """The sharpest false-negative risk this fix creates.

        ``--offline`` plus a cache that silently stores nothing is a run where
        every lookup is a miss. If a miss were softened to "nothing to report",
        a whole bibliography would come back clean without a single registry
        having been consulted — a report that looks like assurance and is not.
        ``Transient`` is the honest answer, and ``UNCHECKED`` is where it
        lands.
        """
        with pytest.warns(RuntimeWarning):
            cache = Cache(_blocked_root(tmp_path))
        with pytest.raises(Transient):
            _client(cache, offline=True).get_json(_URL)


class TestAWriteThatFailsAfterConstructionIsAnnounced:
    """The directory existing and the directory being writable are two facts.

    A container image that bakes in an empty ``~/.cache/bibaudit`` and then
    mounts the rootfs read-only gets past ``_ensure_root`` and fails in
    ``put`` instead, on every write, in complete silence — a run that looks
    cached, re-fetches everything, and leaves ``--offline`` nothing to replay.
    """

    def test_the_first_failed_write_warns(self, tmp_path: Path) -> None:
        cache = Cache(tmp_path / "c")
        cache.put(_URL, {"url": _URL, "payload": _PAYLOAD})
        shard = _sole_record(cache.path).parent
        shutil.rmtree(shard)
        shard.write_text("a file where the shard directory should be", encoding="utf-8")
        with pytest.warns(RuntimeWarning, match="not being stored"):
            cache.put(_URL, {"url": _URL, "payload": {"different": True}})

    def test_it_warns_once_and_not_once_per_lookup(self, tmp_path: Path) -> None:
        """A bibliography has hundreds of entries. One line explains the run;
        four hundred identical lines bury the report the user came for.
        """
        cache = Cache(tmp_path / "c")
        cache.put(_URL, {"url": _URL, "payload": _PAYLOAD})
        shard = _sole_record(cache.path).parent
        shutil.rmtree(shard)
        shard.write_text("a file where the shard directory should be", encoding="utf-8")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for _ in range(5):
                cache.put(_URL, {"url": _URL, "payload": {"different": True}})
        assert len([w for w in caught if issubclass(w.category, RuntimeWarning)]) == 1

    def test_a_healthy_cache_stays_silent(self, tmp_path: Path) -> None:
        """The false-alarm half. A warning that fires on a working cache is
        worse than no warning, because it trains the reader to ignore it.
        """
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cache = Cache(tmp_path / "c")
            cache.put(_URL, {"url": _URL, "payload": _PAYLOAD})
            assert cache.get(_URL) == _PAYLOAD
            cache.clear()
        assert [str(w.message) for w in caught] == []


# --------------------------------------------------------------------------
# The ``network`` marker, and the deselection that gives it meaning.
#
# CLAUDE.md promises "network tests are opt-in and deselected by default", and
# the marker was declared in pyproject.toml with that promise written into its
# help text — but ``addopts`` carried no ``-m "not network"``, so nothing
# performed the deselection. Nothing carried the marker either, which is why
# the gap was invisible: the first networked test anybody wrote would simply
# have run in the default suite and started failing in a sealed container.
#
# The pair below closes it from both sides. Neither can run in the same
# invocation as the other, which is the point.
#
# A test that really does open a socket does not belong in this module: the
# autouse ``_never_the_real_network`` fixture above replaces ``urlopen`` for
# everything here. The marked test is deliberately trivial — it asserts only
# that it was reached, because "was it reached" is the entire mechanism under
# test.
# --------------------------------------------------------------------------


def test_the_default_run_deselects_network_marked_tests(
    pytestconfig: pytest.Config,
) -> None:
    """Asserted on pytest's *resolved* configuration, not on the TOML text.

    Reading ``addopts`` out of pyproject.toml would restate the file and keep
    passing if the option stopped reaching pytest — a typo in the table name,
    a second config file shadowing it, an ``-o addopts=`` in CI. ``markexpr``
    is what pytest actually filtered on.

    This test carries no marker, so ``-m network`` deselects it in turn; it
    can only ever observe the default invocation, which is the one whose
    behaviour is being promised.
    """
    assert pytestconfig.getoption("markexpr") == "not network"
    assert "network" in {
        mark.name for mark in test_a_network_marked_test_runs_only_when_selected.pytestmark
    }


@pytest.mark.network
def test_a_network_marked_test_runs_only_when_selected(
    pytestconfig: pytest.Config,
) -> None:
    """Runs under ``uv run pytest -m network`` and under nothing else.

    Trivially passing by design. Its value is that it exists and carries the
    marker: without one marked test in the suite, ``-m "not network"`` filters
    an empty set and the guarantee is untested as well as unenforced.
    """
    assert pytestconfig.getoption("markexpr") != "not network"
