"""HTTP transport and on-disk caching shared by every registry client.

Crossref, PubMed, DataCite and friends all talk to the network through
:class:`Client` and only :class:`Client`. Centralising it here is what makes
the "404 is a fact, an outage is not" rule enforceable everywhere at once:
every registry module gets ``None`` for a confirmed-absent record and
:class:`Transient` for a network that could not be asked, with no registry
module having to reimplement that distinction (or get it wrong) itself.

:class:`Cache` is the on-disk half. It stores exactly one JSON object per key:
``{"fetched_at": <iso8601 UTC>, "url": ..., "payload": ...}``. ``payload`` is
whatever the caller is caching, and — because :meth:`Cache.get` is typed to
return ``dict | None``, not ``dict | str | None`` — it is always a JSON
*object*, never a bare string. :meth:`Client.get_text` honours that by
wrapping its plain-text result as ``{"text": ...}`` before it ever reaches the
cache, and unwrapping it again on the way out. ``Cache.put(key, value)``
expects ``value`` in the same ``{"url": ..., "payload": ...}`` shape it
stores (with ``fetched_at`` filled in by the cache itself) — that is the only
way a two-argument ``put`` can carry both pieces of information the format
requires. A confirmed HTTP 404 is written to the cache too (as the marker
payload documented on ``_ABSENT_MARKER``), not skipped, so ``offline=True``
replays "this DOI does not exist" from a prior run instead of raising
:class:`Transient` because nothing was ever stored for it.

The cache is an optimisation, never a precondition: a :class:`Cache` whose
directory cannot be created warns once and then behaves exactly like no cache
at all (see :attr:`Cache.usable`), because an audit that cannot write to disk
is still an audit and must still produce its verdicts.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.message import Message
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version
from pathlib import Path
from typing import Any

__all__ = ["Cache", "Client", "Transient", "default_cache_dir"]


# The name is deliberately not ``TransientError``: ``Transient`` is named as
# such in CLAUDE.md and is part of this package's published contract, so N818's
# suffix convention loses to not breaking `except Transient` in callers.
class Transient(Exception):  # noqa: N818
    """A registry could not be reached, or kept failing — this is not a fact.

    Raised after the retry budget in :data:`_BACKOFF_SECONDS` is exhausted by
    timeouts, connection errors, truncated response bodies, HTTP 429, or
    HTTP 5xx — or immediately, with
    no network attempt at all, when :class:`Client` is offline and the answer
    is not cached. Deliberately distinct from returning ``None`` (which means
    a confirmed HTTP 404): a caller that
    let this collapse into "not found" would report every citation as
    fabricated the moment a registry has an outage. ``UNCHECKED`` (see
    ``model.FAILING_VERDICTS``) is the only verdict this exception may lead
    to; it must never manufacture ``UNCONFIRMED`` or worse.
    """


# Exponential backoff schedule between retries, in seconds. The task spec
# names four sleep values (1, 2, 4, 8) for "4 attempts"; read literally as
# "4 retries after the initial call" every value is consumed and no attempt
# budget is wasted sleeping after the final, unrecoverable failure. That
# yields 5 network round trips in the worst case: 1 initial + 4 retries.
_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0)

#: Upper bound on how long a Retry-After header is allowed to make us wait.
#: An unbounded honouring of a hostile or misconfigured Retry-After would let
#: one registry response stall an entire audit run indefinitely.
_RETRY_AFTER_CAP_SECONDS = 60.0

#: Ceiling of the deterministic per-URL jitter added to each backoff sleep.
#: Kept small relative to the 1-8s backoff steps themselves.
_JITTER_SPAN_SECONDS = 0.5

#: Cache-payload key marking a confirmed HTTP 404, so :meth:`Client._fetch_cached`
#: can tell "this key was never looked up" apart from "this key was looked up
#: and the registry does not have it". :class:`Cache` only ever stores JSON
#: *objects* (see the module docstring), so absence is represented as a marker
#: object rather than a bare ``null`` — an ordinary registry payload cannot
#: collide with it because ``__bibaudit_404__`` is not a field any registry API
#: emits.
_ABSENT_MARKER = "__bibaudit_404__"


def _jitter(url: str) -> float:
    """A small, reproducible sub-second delay derived from *url*.

    Real jitter exists to desynchronise clients that would otherwise back off
    in lockstep. ``random`` would satisfy that, but it would also mean the
    same fixture produces a different sleep time on every run, which
    contradicts a tool whose whole point is that a result is reproducible
    from registry responses alone. Hashing the URL gives a stable,
    request-specific offset instead of a random one.
    """
    digest = hashlib.sha256(url.encode("utf-8")).digest()
    fraction = int.from_bytes(digest[:2], "big") / 0xFFFF
    return fraction * _JITTER_SPAN_SECONDS


def _retry_after_seconds(headers: Message) -> float | None:
    """Numeric Retry-After value from *headers*, capped, or None.

    HTTP also permits Retry-After as an HTTP-date; only the numeric
    delta-seconds form is honoured, per the assignment's "when present and
    numeric" — parsing an HTTP-date correctly requires RFC 7231's dozen date
    formats for a header most registries never send in that form anyway.
    """
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    raw = raw.strip()
    # `isdecimal`, not `isdigit`: "²".isdigit() is True but float("²") raises
    # ValueError, and that exception would escape the retry loop entirely —
    # a caller expecting `Transient`-or-fact would get neither. A malformed
    # header must cost one ignored header, never an aborted audit.
    if not raw.isdecimal():
        return None
    return min(float(raw), _RETRY_AFTER_CAP_SECONDS)


def default_cache_dir() -> Path:
    """XDG-correct per-user cache directory for bibaudit, not yet created.

    ``$XDG_CACHE_HOME`` is honoured on every platform because some macOS users
    set it deliberately to relocate caches (e.g. onto a non-backed-up
    volume); absent that, the platform default applies: ``~/Library/Caches``
    on macOS, ``~/.cache`` (the XDG default) elsewhere.
    """
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        base = Path(xdg)
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path.home() / ".cache"
    return base / "bibaudit"


class Cache:
    """A TTL'd, on-disk JSON object store, keyed by an arbitrary string.

    One file per key, under ``root``, sharded into two-character
    subdirectories of ``sha256(key)`` so that a bibliography with thousands
    of DOIs never puts thousands of files in one directory (slow directory
    listings, and a hard limit on some filesystems). The key itself is hashed
    rather than used as a filename because registry keys are DOIs and URLs —
    full of ``/`` and ``:`` — which are not filesystem-safe.

    ``put(key, value)`` expects ``value`` shaped as ``{"url": ..., "payload":
    ...}``; the on-disk record adds ``fetched_at`` and is exactly
    ``{"fetched_at": <iso8601 UTC>, "url": ..., "payload": ...}``.
    ``get(key)`` returns only the ``payload`` half — never the envelope, and
    never a ``url``/``fetched_at`` a caller would have to know to ignore.

    **A cache that cannot be created is not an error.** Constructing a Cache
    on a root that cannot be made — a read-only image layer, an
    ``$XDG_CACHE_HOME`` pointing at somebody else's directory, a container
    running as a uid with no writable home — leaves a *usable but inert*
    object: every :meth:`get` misses, every :meth:`put` is dropped, and one
    warning says so. See :attr:`usable`.
    """

    def __init__(self, root: Path, ttl_days: int = 90) -> None:
        self._root = Path(root)
        self._ttl = timedelta(days=ttl_days)
        #: Set False by :meth:`_ensure_root` when the directory cannot exist.
        self._usable = False
        #: One warning per Cache for a *write* that fails after construction
        #: succeeded — see :meth:`put`. Not a counter: the point is to tell the
        #: user once that this run is not being cached, not to narrate every
        #: DOI.
        self._warned_on_write = False
        self._ensure_root()

    def _ensure_root(self) -> None:
        """Create the cache root, or record that there will be no cache.

        ``mkdir`` used to run here uncaught, so an ``OSError`` escaped the
        constructor. ``audit.run`` and ``cli._run_cache`` both construct a
        Cache without a ``try``, which meant ``bibaudit check`` on a
        locked-down CI container — a read-only rootfs, or ``HOME`` unset so
        ``Path.home()`` resolves somewhere unwritable — died with a bare
        ``PermissionError`` traceback before reading a single reference. That
        contradicted :meth:`put`'s own promise that "a caching problem must
        not turn that success into a crash": the cache is an optimisation and
        an ``--offline`` replay store, never a precondition for checking a
        bibliography.
        """
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._usable = False
            warnings.warn(
                f"cannot use {self._root} as a registry cache ({exc}); "
                "continuing without one. Every lookup in this run will go to "
                "the network, nothing will be stored for `--offline` to "
                "replay, and `--offline` itself will report every reference "
                "as UNCHECKED. Point --cache-dir or $XDG_CACHE_HOME at a "
                "writable location to restore caching.",
                RuntimeWarning,
                stacklevel=3,
            )
            return
        self._usable = True

    @property
    def path(self) -> Path:
        return self._root

    @property
    def usable(self) -> bool:
        """False when the cache root could not be created.

        Exposed so a caller can *report* the degradation rather than infer it
        from a suspiciously empty cache directory. Nothing in this module
        branches on it beyond skipping the filesystem work, because an inert
        cache and no cache at all must behave identically — a run that
        degrades has to produce the same verdicts, only slower.
        """
        return self._usable

    def _path_for(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._root / digest[:2] / f"{digest}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        """The cached payload for *key*, or ``None`` on any kind of miss.

        A miss covers three cases a caller must not have to tell apart:
        nothing was ever cached, the cached copy is older than ``ttl_days``,
        or the file is unreadable (partial write from a crash mid-write,
        disk corruption, hand-edited while debugging). Every one of those
        means "ask the registry again", never "raise and abort the audit".
        """
        if not self._usable:
            return None
        path = self._path_for(key)
        try:
            with path.open("r", encoding="utf-8") as handle:
                record = json.load(handle)
            fetched_at = datetime.fromisoformat(record["fetched_at"])
            payload = record["payload"]
            # The age check lives inside the `try` because a *naive*
            # `fetched_at` — a record hand-edited while debugging, or written
            # by anything that dropped the offset — makes this subtraction
            # raise `TypeError: can't subtract offset-naive and offset-aware
            # datetimes`. Outside the `try` that traceback escapes and kills
            # the whole audit; inside it, it is simply one more unreadable
            # record, which means "ask the registry again".
            expired = datetime.now(UTC) - fetched_at > self._ttl
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if not isinstance(payload, dict):
            # Violates this cache's own contract (payload is always a JSON
            # object); treat as corrupt rather than returning a value whose
            # type contradicts the declared `dict | None` signature.
            return None
        if expired:
            return None
        return payload

    def put(self, key: str, value: dict[str, Any]) -> None:
        """Store *value* (``{"url": ..., "payload": ...}``) under *key*.

        Written atomically — to a temp file in the same shard directory, then
        renamed over the target — so a process killed mid-write never leaves
        a half-written file for :meth:`get` to trip over. A write failure
        (full disk, read-only mount, permissions) is swallowed rather than
        raised: the registry lookup that produced *value* already succeeded,
        and a caching problem must not turn that success into a crash.

        The first such failure does emit one warning. The directory existing
        and the directory being *writable* are different facts, and only the
        first is checked at construction: a container image that bakes in an
        empty ``~/.cache/bibaudit`` and then mounts the rootfs read-only gets
        past :meth:`_ensure_root` and fails here instead, on every single
        write, in complete silence — a run that looks cached, re-fetches
        everything, and leaves ``--offline`` with nothing to replay. Saying it
        once is the difference between a slow run and an unexplained one.
        """
        if not self._usable:
            return
        record = {
            "fetched_at": datetime.now(UTC).isoformat(),
            "url": value.get("url"),
            "payload": value.get("payload"),
        }
        path = self._path_for(key)
        tmp_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
            tmp_path = Path(tmp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle)
            os.replace(tmp_path, path)
        except OSError as exc:
            # Best-effort removal of the half-written temp file. Failure to
            # clean up is suppressed because we are already on the "caching
            # did not work, carry on" path: the registry lookup that produced
            # *value* succeeded, and a full disk must not turn that success
            # into a traceback.
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    tmp_path.unlink(missing_ok=True)
            if not self._warned_on_write:
                self._warned_on_write = True
                warnings.warn(
                    f"cannot write to the registry cache at {self._root} "
                    f"({exc}); this run's registry responses are not being "
                    "stored, so it will not speed up a re-run and `--offline` "
                    "will have nothing to replay. Verdicts are unaffected.",
                    RuntimeWarning,
                    stacklevel=2,
                )

    def clear(self) -> None:
        """Discard every cached entry, leaving an empty cache directory.

        Re-runs :meth:`_ensure_root` rather than calling ``mkdir`` directly,
        so ``bibaudit cache clear`` on an unwritable cache directory explains
        itself instead of raising out of ``cli._run_cache`` — the same
        constructor crash, one command later.
        """
        shutil.rmtree(self._root, ignore_errors=True)
        self._ensure_root()


def _package_version() -> str:
    """The installed bibaudit version, or a safe placeholder before install.

    ``importlib.metadata.version`` raises when the package has not been
    installed into the environment (e.g. mid-development, running straight
    off ``src/`` with no build backend registered yet). A User-Agent is
    required on every request regardless, so a missing version must not
    prevent the client from being constructed.
    """
    try:
        return _installed_version("bibaudit")
    except PackageNotFoundError:
        return "0.0.0"


def _default_user_agent(mailto: str | None) -> str:
    """The default User-Agent, with ``mailto`` appended when supplied.

    Crossref's "polite pool" — lower latency, priority during load-shedding —
    is granted to requests whose User-Agent carries a working contact email
    in exactly this ``mailto=`` form; omitting it is a silent performance
    regression, not an error, so it is easy to get wrong without ever seeing
    a failure.
    """
    agent = f"bibaudit/{_package_version()} (+https://github.com/lorenzoFabbri/bibaudit)"
    if mailto:
        agent += f"; mailto={mailto}"
    return agent


class Client:
    """Polite, retrying, cached HTTP GET for registry lookups.

    All network access in the tool goes through this one class so the
    404-vs-outage distinction (see :class:`Transient`), the retry/backoff
    policy, and per-host politeness are enforced identically for Crossref,
    PubMed, or any registry added later — a registry module never opens a
    socket itself.

    ``offline=True`` is the reproducibility switch (``bibaudit check
    --offline``): a cache hit is still served, but a miss never reaches the
    network — it raises :class:`Transient` immediately, exactly what a
    caller already does for an unreachable registry, so an uncached lookup
    is reported as ``UNCHECKED`` rather than silently fetched.
    """

    def __init__(
        self,
        cache: Cache | None = None,
        *,
        mailto: str | None = None,
        user_agent: str | None = None,
        timeout: float = 30.0,
        min_interval: float = 0.2,
        refresh: bool = False,
        offline: bool = False,
    ) -> None:
        self._cache = cache
        self._timeout = timeout
        self._min_interval = min_interval
        self._refresh = refresh
        self._offline = offline
        # An explicit user_agent replaces the default wholesale, including the
        # mailto convenience — a caller who bothered to pass their own string
        # gets exactly that string, not a value silently modified afterwards.
        self._user_agent = user_agent if user_agent is not None else _default_user_agent(mailto)
        # Per-host last-request timestamps, owned by this instance only (no
        # module-level state) so two Client objects never throttle each other.
        self._last_request_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def _headers(self, extra: dict[str, str] | None) -> dict[str, str]:
        """Merge caller headers onto the client defaults, caller wins.

        Only User-Agent is injected by default. Caller-supplied headers are
        applied last so a specific registry module (e.g. one that needs an
        NCBI API-key header) can add or override anything without this
        module needing to know that registry's header vocabulary.
        """
        merged = {"User-Agent": self._user_agent}
        if extra:
            merged.update(extra)
        return merged

    def _throttle(self, host: str) -> None:
        """Block until at least ``min_interval`` seconds have passed since
        the last request to *host*.

        Enforced per host, not globally: polling Crossref and PubMed back to
        back should not make PubMed wait out Crossref's cooldown, and a
        global lock would serialise unrelated registries for no reason. The
        lock is held only long enough to reserve this call's slot in
        ``_last_request_at`` — never across the sleep itself, or a thread
        waiting out Crossref's cooldown would block a concurrent PubMed
        request from even checking its own, unrelated, timestamp.
        """
        with self._lock:
            now = time.monotonic()
            last = self._last_request_at.get(host)
            wait_until = max(now, last + self._min_interval) if last is not None else now
            self._last_request_at[host] = wait_until
        remaining = wait_until - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

    def _request(self, url: str, headers: dict[str, str]) -> bytes | None:
        """One logical GET against *url*: retries, throttles, and resolves
        the 404-vs-outage question.

        Returns the response body, or ``None`` for a confirmed HTTP 404.
        Raises :class:`Transient` once the retry budget is exhausted by
        anything else (timeout, connection failure, 429, 5xx, or any other
        non-2xx status this client does not treat as a confirmed absence).
        """
        if not url.lower().startswith(("http://", "https://")):
            # Not a network condition — a caller passing e.g. a bare DOI or a
            # file:// URI is a programming error, so it fails immediately
            # rather than burning the retry budget or reading local files.
            raise ValueError(f"unsupported URL scheme: {url!r}")

        host = urllib.parse.urlparse(url).netloc.lower()
        last_error: Exception | None = None
        attempts = len(_BACKOFF_SECONDS) + 1

        for attempt in range(attempts):
            self._throttle(host)
            retry_after: float | None = None
            try:
                request = urllib.request.Request(url, headers=headers, method="GET")
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    # `.read()` is typed `Any` by typeshed (urlopen's return type
                    # is a broad union); wrapping it pins the value to the
                    # `bytes | None` this method promises its callers.
                    return bytes(response.read())
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return None
                last_error = exc
                # Not scoped to 429: Crossref and PubMed both also use 503 to
                # signal "back off", with the same header, during maintenance
                # or load-shedding. Limiting this to 429 would silently fall
                # back to the fixed 1/2/4/8s schedule on a 503 that names its
                # own wait time, defeating the point of honouring the header.
                retry_after = _retry_after_seconds(exc.headers)
            except (OSError, http.client.HTTPException) as exc:
                # OSError covers urllib.error.URLError (DNS failure, refused
                # connection, TLS error) and TimeoutError — both are OSError
                # subclasses, and HTTPError (handled above) is a URLError
                # subclass, so this branch only ever sees non-HTTP failures.
                #
                # `http.client.HTTPException` is listed separately because it
                # is *not* an OSError, and leaving it out was a real hole: a
                # chunked reply (Crossref sends every response chunked) that is
                # cut short mid-body makes `response.read()` raise
                # `IncompleteRead`, and a proxy returning a garbled status line
                # makes `urlopen` raise `BadStatusLine`. Both escaped this
                # method uncaught, so a truncated response was neither a fact
                # (`None`) nor ignorance (`Transient`) — it was a traceback out
                # of `audit.py`, which catches only `Transient`, and the run
                # ended with no report at all. A half-read body is ignorance
                # like any other: retry it, then give up as `Transient`.
                last_error = exc

            if attempt == attempts - 1:
                break
            delay = retry_after if retry_after is not None else _BACKOFF_SECONDS[attempt] + _jitter(url)
            time.sleep(delay)

        raise Transient(
            f"{url}: registry unreachable after {attempts} attempts ({last_error!r})"
        ) from last_error

    def _fetch_cached(
        self,
        url: str,
        *,
        cache_key: str | None,
        headers: dict[str, str] | None,
        make_payload: Callable[[bytes], dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Shared cache-then-network path for :meth:`get_json`/:meth:`get_text`.

        *make_payload* turns the raw response body into the JSON-object shape
        :class:`Cache` requires (see the module docstring for why
        :meth:`get_text` wraps its string as ``{"text": ...}``).

        A confirmed HTTP 404 is written to the cache as :data:`_ABSENT_MARKER`,
        not skipped: it is a fact about the work, exactly as citable as a
        successful response, and the whole point of ``offline=True`` is that a
        prior run's findings — including "this DOI does not exist" — replay
        without a network. Leaving 404s uncached would mean a bibliography's
        one genuinely fabricated DOI silently turns into ``UNCHECKED`` (not a
        failing verdict) the moment the audit is re-run offline, which is
        exactly the defect this tool exists to catch.

        In offline mode the cache is still consulted — including when
        ``refresh`` was also requested, since there is no way to honour a
        forced refetch without a network — but a miss raises
        :class:`Transient` instead of falling through to :meth:`_request`.
        """
        key = cache_key or url
        # The `self._cache is not None` check lives inside this `if`, not in a
        # separately-computed bool, so it also narrows `self._cache` for the
        # `.get` call on the next line rather than merely gating it at runtime.
        if self._cache is not None and (not self._refresh or self._offline):
            cached = self._cache.get(key)
            if cached is not None:
                return None if _ABSENT_MARKER in cached else cached

        if self._offline:
            raise Transient(f"{url}: offline mode and no cached response for this URL")

        body = self._request(url, self._headers(headers))
        if body is None:
            if self._cache is not None:
                self._cache.put(key, {"url": url, "payload": {_ABSENT_MARKER: True}})
            return None
        payload = make_payload(body)

        if self._cache is not None:
            self._cache.put(key, {"url": url, "payload": payload})
        return payload

    def get_json(
        self,
        url: str,
        *,
        cache_key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """Fetch and JSON-decode *url*, serving from cache when fresh.

        Returns ``None`` for a confirmed HTTP 404. A malformed JSON body on
        an otherwise-successful response raises ``json.JSONDecodeError``
        rather than being folded into ``None`` or :class:`Transient`: it is
        neither a confirmed absence nor a network outage, and hiding it would
        turn a real registry data problem into a silent false negative.
        """
        return self._fetch_cached(
            url,
            cache_key=cache_key,
            headers=headers,
            make_payload=lambda body: json.loads(body.decode("utf-8")),
        )

    def get_text(
        self,
        url: str,
        *,
        cache_key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> str | None:
        """Fetch *url* as text, serving from cache when fresh.

        Decoded permissively (``errors="replace"``): unlike a JSON body,
        where a decode failure signals a real data problem worth surfacing,
        free text (an abstract, an HTML landing page) occasionally carries a
        mis-declared encoding, and one bad byte should not abort the run.
        """
        payload = self._fetch_cached(
            url,
            cache_key=cache_key,
            headers=headers,
            make_payload=lambda body: {"text": body.decode("utf-8", errors="replace")},
        )
        return None if payload is None else payload["text"]
