"""Retraction detection as a first-class, independent source.

Every other registry client in this package answers "what is this work
called, who wrote it, where was it published" and, as a side effect, whether
*that registry itself* happens to carry a retraction linkage
(``crossref._retraction``, ``pubmed._retraction``). This module exists
because a side effect is not coverage: a retraction deposited nowhere but
Retraction Watch's own database, or recorded by NLM only as a cross-reference
neither of those functions reads, passed every existing check as a clean
citation. Three gaps, each found by running the tool against real DOIs:

(a) **Retraction Watch is only reached as far as Crossref surfaces it.**
    Crossref's ``updated-by`` linkage (see ``registries/crossref.py``) is
    populated from Retraction Watch's own database, but only when a
    publisher's deposit and Retraction Watch's record agree well enough for
    Crossref's pipeline to link them — and only for DOIs Crossref carries at
    all. Reading Retraction Watch's data directly, independent of whatever a
    publisher chose to deposit, closes that gap. See :func:`_load_index` and
    :func:`_parse_rw_csv`.

(b) **PubMed contributes no expression-of-concern signal at all.** NLM
    records a concern raised about a paper as an ``ECI`` cross-reference
    ("Expression of Concern In:") on *that paper's own* MEDLINE record, never
    as a ``PT`` (publication-type) value there — ``registries/pubmed.py``'s
    ``_retraction()`` reads only ``PT``, so ``ECI`` is invisible to it.
    Witnessed live: PMID 23741377 (10.1371/journal.pone.0064723, Neirinckx et
    al.) carries ``PT - Journal Article`` / ``PT - Research Support, Non-U.S.
    Gov't`` — nothing retraction-shaped — and separately ``ECI - PLoS One.
    2021 Oct 28;16(10):e0256488. doi: 10.1371/journal.pone.0256488. PMID:
    34710116``, naming the actual notice. Recorded verbatim in
    ``tests/data/pubmed_eci_concern.txt``. See :func:`_notice_from_pubmed`.

    The mirror-image case matters just as much and is handled the same
    careful way the retraction direction is handled in ``crossref.py`` and
    ``pubmed.py``: PMID 34710116, the concern notice itself, carries ``PT -
    Expression of Concern`` and an ``ECF`` ("Expression of Concern For:")
    field pointing *back* at 23741377 — not ``ECI``. A check that read either
    string as "this record has a concern about itself" would flag the notice
    and clear nothing about the paper it concerns; only ``ECI`` is read here,
    exactly as only ``updated-by`` (never ``update-to``) decides
    retractedness in ``crossref.py``, and for the identical reason. Recorded
    in ``tests/data/pubmed_eci_concern_notice.txt``.

(c) **A DOI only DataCite answers for gets no retraction check whatsoever.**
    DataCite's schema does carry a ``relatedIdentifiers`` relation named
    ``IsObsoletedBy``, which looked at first like a candidate. It is not one:
    querying the live DataCite API for it (2026-08-01) turns up thousands of
    ordinary Zenodo preprint version bumps and PANGAEA dataset updates —
    ``10.5281/zenodo.19216416``, superseded by a later version of the same
    preprint at ``10.5281/zenodo.21729616``, is typical, not exceptional.
    ``IsObsoletedBy`` means "a newer version of this deposit exists", the
    same routine event as Crossref's own non-retraction updates, and treating
    it as a retraction signal would fail every Zenodo software release with
    more than one version — the exact false-alarm failure mode CLAUDE.md's
    third rule forbids. So: no, it does not carry a usable signal, and
    ``registries/datacite.py`` is deliberately left alone.

    The honest fix is not a DataCite-specific check but the shape of this
    module's own interface: :meth:`Retractions.status_for` takes DOIs, not
    registries, and knows nothing about which registry answered a DOI's
    *bibliographic* fields. Retraction Watch and PubMed are asked about every
    DOI a caller hands this class, a Zenodo deposit's DOI included, so a
    caller that checks retraction status through this module for every
    resolved reference — not only the ones Crossref happened to answer for —
    gives a DataCite-only citation the same independent coverage as any
    other, rather than a silent "never asked". Wiring that call site is
    outside this module's job (see ``audit.py``); what belongs here is making
    sure the answer does not depend on which registry the caller reached DataCite through.

Sources, in order of independence from one another:

1. Retraction Watch's own bulk export, fetched directly (:func:`_load_index`)
   — not the subset Crossref happened to link.
2. PubMed/MEDLINE, via :class:`~bibaudit.registries.pubmed.PubMed`'s public
   ``by_dois`` rather than a re-implementation of esearch/esummary/efetch:
   its already-correct ``PT``-based retraction flag is read straight off
   ``Record.retracted``, and only the new ``ECI`` reading is added here
   (:func:`_notice_from_pubmed`).
3. Crossref's ``update-to``/``updated-by`` linkage — already implemented in
   ``registries/crossref.py`` and deliberately **not** duplicated here. A
   caller combining this module's answer with a Crossref
   :class:`~bibaudit.model.Record`'s own ``.retracted``/``.retraction_kind``
   gets the full picture; re-deriving Crossref's reciprocal-update tie-break
   from scratch in a second module is exactly the kind of drift that turns a
   correctly retracted paper into a cleared one.

A partial outage degrades rather than fails: if the Retraction Watch export
cannot be fetched, that source alone contributes nothing to this call and
PubMed still answers (see :meth:`Retractions._rw_signals`). Degrading is not
the same as going unmentioned, though — :meth:`Retractions.status_for` names
the failed source in :attr:`RetractionStatus.unreachable`, so a caller can
report the gap instead of printing a clean run over a source nobody reached.
A PubMed outage, by contrast, propagates as
:class:`~bibaudit.registries.http.Transient` exactly as
:meth:`PubMed.by_dois` already raises it — that is a real, reportable outage
of a registry this project relies on for retraction corroboration, not a
routine gap in one bulk file's freshness, and swallowing it here would be the
single worst thing this module could do: silence about a registry that could
have said "retracted" rendering as a clean citation.
"""

from __future__ import annotations

import csv
import io
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from ..model import Record
from ..normalize import clean, extract_dois, fold, normalize_doi, parse_year
from .http import Cache, Client, Transient, default_cache_dir
from .pubmed import PubMed

__all__ = ["RetractionNotice", "Retractions"]

#: Crossref Labs' distribution of the Retraction Watch database. Verified
#: live (2026-08-01): a keyless, unauthenticated ``GET`` streams a ~66 MB,
#: ~71,500-row CSV (``Content-Disposition: attachment; filename=retractions.csv``).
#: The historical ``labs.crossref.org/data/retraction-watch.csv`` URL this
#: brief was written against no longer serves the file; this is the
#: replacement, confirmed by fetching it rather than assumed from memory.
_RW_CSV_URL = "https://api.labs.crossref.org/data/retractionwatch"

#: Retraction status changes under a DOI that never otherwise changes — a
#: paper can be retracted five years after its bibliographic metadata was
#: last touched — so this file is refetched far more eagerly than the 90-day
#: default a :class:`~bibaudit.registries.http.Cache` gives ordinary
#: bibliographic lookups. Seven days bounds how stale a "clean" answer can be
#: without re-downloading a 66 MB file on every single invocation.
_RW_CACHE_TTL_DAYS = 7

#: What is actually cached is the parsed DOI index (a few MB of JSON), never
#: the raw CSV. Storing the raw file through :class:`Cache`'s JSON-object
#: envelope would mean ``json.dump``-ing a 66 MB string on every refresh for
#: no benefit: nothing downstream of :func:`_parse_rw_csv` ever wants the
#: unparsed rows again, and reparsing a cached raw CSV every run would defeat
#: the point of caching at all.
_RW_INDEX_CACHE_KEY = "index-v1"

#: RW's ``RetractionNature`` values (2026-08-01 snapshot, 71,496 rows) mapped
#: onto this module's kind vocabulary. Keys are :func:`~bibaudit.normalize.fold`ed
#: so case and punctuation drift in a future export do not silently stop
#: matching. ``Reinstatement`` (160 rows) is deliberately absent: it is
#: handled in :func:`_parse_rw_csv` as *withdrawing* an earlier retraction for
#: the same DOI, not as a kind of notice in its own right, so it never becomes
#: a :attr:`RetractionNotice.kind` value.
_RW_KIND_MAP = {
    "retraction": "retraction",
    "expression of concern": "expression-of-concern",
    "correction": "correction",
}

#: How a conflict between two sources reporting on the *same* DOI is broken,
#: most to least definitive — mirrors ``crossref._RETRACTION_PRIORITY``'s "a
#: later retraction supersedes an earlier expression of concern" rule, so a
#: second source agreeing can only strengthen a finding, never soften one
#: already made. ``withdrawal``/``removal`` are not yet produced by either
#: source this module reads (RW's live vocabulary has no such category; see
#: ``_RW_KIND_MAP``), but are ranked here so a future RW category needs only
#: a new ``_RW_KIND_MAP`` entry, not a change to this ordering.
_KIND_PRIORITY = ("retraction", "withdrawal", "removal", "correction", "expression-of-concern")

#: `M/D/Y H:MM` is every witnessed value's shape (71,306 of 71,496 rows) —
#: confirmed month-first, not day-first: 41,661 rows have a day > 12, which
#: is only valid under that ordering, and none contradict it — always at
#: midnight. One row uses a 12-hour `H:MM:SS AM/PM` variant instead; both are
#: tried in :func:`_parse_rw_date` before a cell is given up on.
_RW_DATE_FORMATS = ("%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M:%S %p")


@dataclass(frozen=True, slots=True)
class RetractionNotice:
    """One post-publication status finding for a single DOI, from one source.

    Distinct from :class:`~bibaudit.model.Record`: this is evidence about
    whether a work still stands, never a description of the work itself, and
    keeping the two apart is what lets a caller merge this module's answer
    with a registry's own record without either overwriting the other's
    fields.
    """

    #: Normalised DOI of the work this notice is *about* — the retracted (or
    #: corrected, or under-concern) paper, never the notice's own DOI.
    doi: str
    #: ``retraction`` | ``withdrawal`` | ``removal`` | ``expression-of-concern`` | ``correction``.
    kind: str
    #: Which source asserted this: ``"retraction-watch"``, ``"pubmed"``, or
    #: (after :func:`_combine` merges agreeing sources) a comma-joined list of
    #: them, matching the convention ``compare._status_issues`` already uses
    #: for ``Issue.source``.
    source: str
    #: The retracting/notice document's own DOI, when the source supplies
    #: one. ``None`` is not "no notice exists" — it means *this* source did
    #: not carry a DOI for it (PubMed's ``PT`` flag never does; its ``ECI``
    #: citation sometimes does, and is parsed for one).
    notice_doi: str | None
    #: Best-effort date of the notice, as an ISO ``YYYY-MM-DD`` string when a
    #: full date was parsed, else whatever coarser text the source allows
    #: (PubMed's ``ECI`` citation and its own PT-flagged record carry no more
    #: than a year). ``None`` when nothing date-shaped was available at all.
    date: str | None


class RetractionOutage(Transient):
    """A propagating retraction outage that names every source it took down.

    A :class:`~bibaudit.registries.http.Transient` out of
    :meth:`Retractions.status_for` is PubMed's leg, but Retraction Watch may
    already have failed on the way there. Subclassing keeps every existing
    ``except Transient`` working while letting a caller that cares fold
    :attr:`unreachable` into its own set rather than assuming "pubmed" alone.
    """

    def __init__(self, message: str, unreachable: frozenset[str]) -> None:
        super().__init__(message)
        #: Every source known to be unreachable when this was raised.
        self.unreachable = unreachable


@dataclass(frozen=True, slots=True)
class RetractionStatus:
    """What :meth:`Retractions.status_for` found, and what it could not reach.

    The second half is the point, and it is why this is a class rather than a
    bare mapping. Silence from Retraction Watch is indistinguishable from
    Retraction Watch having nothing to say unless the answer carries which of
    its sources went unanswered, and ignorance about retraction must never
    render as a clean bill of health.
    """

    #: Merged notices, keyed by normalised DOI, exactly as the old ``dict``
    #: return value was. A DOI absent from here carries no signal from any
    #: source that *answered* — read together with :attr:`unreachable`, never
    #: alone.
    notices: Mapping[str, RetractionNotice]
    #: Names of sources that could not be reached on this call, in the same
    #: vocabulary ``audit.py`` keeps its *unreachable* set in — currently only
    #: ever ``{"retraction-watch"}``, because a PubMed outage raises
    #: :class:`~bibaudit.registries.http.Transient` out of this module rather
    #: than being reported through here.
    unreachable: frozenset[str]


def _fold_nature(raw: str) -> str:
    return fold(raw or "")


def _parse_rw_date(raw: str) -> tuple[str | None, datetime]:
    """Best-effort ``(display date, sortable datetime)`` for one RW date cell.

    A date that cannot be parsed at all — blank, or a future export format
    this was never run against — still gets a usable sort key:
    :data:`datetime.min` sorts before every real date, so a row that *does*
    carry a genuine timestamp always outranks it when two rows compete for
    the same DOI in :func:`_parse_rw_csv`, and one bad cell never raises out
    of the whole load.
    """
    text = (raw or "").strip()
    for fmt in _RW_DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.date().isoformat(), parsed
    return None, datetime.min


def _looks_like_doi(value: str) -> bool:
    """Cheap shape check, not a validity one — good enough to reject blanks
    and RW's own "no DOI available" sentinel (``Unavailable``, 3,419 rows in
    the 2026-08-01 snapshot) without writing a second DOI regex.
    ``normalize.DOI_PATTERN`` is not reused here because these are already
    isolated CSV cells, not free text a pattern needs to be found *within* —
    see :func:`~bibaudit.normalize.normalize_doi`'s own docstring for why a
    bespoke DOI regex is a defect in this project.
    """
    return value.startswith("10.")


def _parse_rw_csv(text: str) -> dict[str, RetractionNotice]:
    """Index of Retraction Watch's export by the *original* paper's DOI.

    Streamed row by row through :class:`csv.DictReader` rather than
    materialised as ``list(csv.DictReader(...))`` first: only the rows whose
    ``OriginalPaperDOI`` is both present and DOI-shaped are ever retained, a
    small fraction of the ~71,500 total, so building the full row list before
    filtering would hold everything the streaming read avoids.

    A DOI can carry more than one row — RW logged both a 2004 ``Correction``
    and a 2010 ``Retraction`` for 10.1016/S0140-6736(97)11096-0, the
    Wakefield paper, as two separate entries — so the row with the *latest*
    parseable ``RetractionDate`` wins per DOI, not the first or last
    encountered. That is also how a later ``Reinstatement`` is handled: it
    is not a kind of notice of its own (see :data:`_RW_KIND_MAP`), it is the
    event that, when it is the most recent one for a DOI, removes that DOI
    from the index entirely — RW recorded 160 reinstatements in the
    2026-08-01 snapshot, meaning a retraction that was later reversed, and
    reporting the reversed retraction as a live finding would be the false
    alarm CLAUDE.md's third rule exists to prevent.
    """
    index: dict[str, RetractionNotice] = {}
    latest: dict[str, datetime] = {}

    for row in csv.DictReader(io.StringIO(text)):
        doi = normalize_doi(row.get("OriginalPaperDOI") or "")
        if not _looks_like_doi(doi):
            continue

        nature = _fold_nature(row.get("RetractionNature") or "")
        if nature == "reinstatement":
            kind: str | None = None
        elif not nature:
            # Undocumented by RW itself but witnessed live (190 of 71,496
            # rows): the whole database's subject is retractions, so an
            # untagged row is read as one rather than silently dropped — a
            # missed retraction costs more than a stray one whose specific
            # nature could not be classified.
            kind = "retraction"
        else:
            kind = _RW_KIND_MAP.get(nature)
            if kind is None:
                # A RetractionNature this module has never been taught.
                # Conservative per CLAUDE.md's third rule: skip rather than
                # guess "retraction" for a future category that might turn
                # out to be something far milder.
                continue

        date_str, sortable = _parse_rw_date(row.get("RetractionDate") or "")
        seen = latest.get(doi)
        if seen is not None and seen >= sortable:
            continue
        latest[doi] = sortable

        if kind is None:  # the winning row for this DOI is a reinstatement
            index.pop(doi, None)
            continue

        notice_doi = normalize_doi(row.get("RetractionDOI") or "")
        index[doi] = RetractionNotice(
            doi=doi,
            kind=kind,
            source="retraction-watch",
            notice_doi=notice_doi if _looks_like_doi(notice_doi) else None,
            date=date_str,
        )

    return index


def _index_to_payload(index: Mapping[str, RetractionNotice]) -> dict[str, Any]:
    return {doi: asdict(notice) for doi, notice in index.items()}


def _index_from_payload(payload: Mapping[str, Any]) -> dict[str, RetractionNotice]:
    """The inverse of :func:`_index_to_payload`, tolerant of a stale or
    hand-edited cache file: an entry that does not parse back into a
    :class:`RetractionNotice` is dropped rather than raised over, the same
    "unreadable means ask again" contract :class:`Cache` itself keeps.
    """
    out: dict[str, RetractionNotice] = {}
    for doi, fields in payload.items():
        if not isinstance(fields, dict):
            continue
        try:
            out[doi] = RetractionNotice(
                doi=str(fields["doi"]),
                kind=str(fields["kind"]),
                source=str(fields["source"]),
                notice_doi=fields.get("notice_doi"),
                date=fields.get("date"),
            )
        except KeyError:
            continue
    return out


def _notice_from_pubmed(doi: str, record: Record) -> RetractionNotice | None:
    """PubMed's opinion on *doi*, from a :class:`~bibaudit.model.Record`
    already produced by :meth:`PubMed.by_dois`.

    ``record.retracted``/``record.retraction_kind`` are read straight off,
    not re-derived: ``registries.pubmed._retraction`` already tells
    ``Retracted Publication`` (this record was retracted) apart from
    ``Retraction of Publication`` (this record *is* the notice, and
    ``retracted`` is correctly ``False``) — see that function's docstring for
    the one-word direction rule this module would otherwise have to
    reimplement, incorrectly, to matter.

    What is new here is ``ECI``: NLM's "Expression of Concern In:"
    cross-reference, carried verbatim on ``record.raw`` because
    ``pubmed._record_from_medline`` stores every MEDLINE tag it saw
    (``raw = dict(fields)``) whether or not ``pubmed.py`` itself interprets
    it. Reading it here needs no MEDLINE parsing of its own — the module
    docstring above is where the direction guard against ``ECF`` (the
    mirror-image field, on the *notice's* own record) is explained.
    """
    if record.retracted:
        return RetractionNotice(
            doi=doi,
            kind="retraction",
            source="pubmed",
            notice_doi=None,
            date=str(record.year) if record.year is not None else None,
        )

    raw = record.raw if isinstance(record.raw, dict) else {}
    eci = raw.get("ECI")
    if not isinstance(eci, list) or not eci:
        return None
    citation = clean(eci[0])
    if not citation:
        return None

    notice_dois = extract_dois(citation)
    return RetractionNotice(
        doi=doi,
        kind="expression-of-concern",
        source="pubmed",
        notice_doi=notice_dois[0] if notice_dois else None,
        # The citation carries a full date ("2021 Oct 28"), but nothing
        # downstream of this module parses MEDLINE's day-name date prose, and
        # `normalize.parse_year` is already trusted elsewhere in this
        # project for exactly this shape of text — reusing it beats writing a
        # second, narrower date parser for one field.
        date=_year_from_citation(citation),
    )


def _year_from_citation(citation: str) -> str | None:
    year = parse_year(citation)
    return str(year) if year is not None else None


def _combine(candidates: Sequence[RetractionNotice]) -> RetractionNotice:
    """Merge two sources' notices about the same DOI into one.

    Only reached when both Retraction Watch and PubMed have something to say
    about the same work — the ordinary case is one source answering and the
    other silent, which needs no merge at all. The more definitive kind wins
    (see :data:`_KIND_PRIORITY`); the sources are joined, comma-separated,
    matching the convention ``compare._status_issues`` uses for
    ``Issue.source`` when more than one registry corroborates a finding; and
    a ``notice_doi``/``date`` either source supplies is kept rather than
    discarded in favour of the other source's ``None``.
    """
    if len(candidates) == 1:
        return candidates[0]

    def rank(notice: RetractionNotice) -> int:
        try:
            return _KIND_PRIORITY.index(notice.kind)
        except ValueError:
            return len(_KIND_PRIORITY)

    best = min(candidates, key=rank)
    sources = ",".join(sorted({c.source for c in candidates}))
    notice_doi = next((c.notice_doi for c in candidates if c.notice_doi), None)
    date = next((c.date for c in candidates if c.date), None)
    return replace(best, source=sources, notice_doi=notice_doi, date=date)


class Retractions:
    """Retraction status from Retraction Watch and PubMed, keyed by DOI.

    Deliberately does not reimplement Crossref's ``update-to``/``updated-by``
    reading (see the module docstring, source 3): a caller wanting the full
    picture combines this class's answer with whatever
    ``registries/crossref.py`` already reports on its own
    :class:`~bibaudit.model.Record`.
    """

    name = "retractions"

    def __init__(self, client: Client, cache_dir: Path | None = None) -> None:
        """Wrap *client* for the Retraction Watch and PubMed fetches.

        *cache_dir* holds only the parsed Retraction Watch index (see
        :data:`_RW_INDEX_CACHE_KEY`), on its own 7-day TTL — deliberately a
        separate :class:`Cache` instance from whatever cache *client* itself
        was built with, whose TTL is tuned for bibliographic fields that do
        not change under an unchanged DOI, not for a status that does.
        Defaults to a dedicated subdirectory of
        :func:`~bibaudit.registries.http.default_cache_dir` so it never
        collides with the ordinary per-request registry cache.
        """
        self._client = client
        self._cache = Cache(
            cache_dir if cache_dir is not None else default_cache_dir() / "retraction-watch",
            ttl_days=_RW_CACHE_TTL_DAYS,
        )
        self._pubmed = PubMed(client)
        #: In-process memo, separate from the on-disk :class:`Cache`: a run
        #: checking hundreds of DOIs calls :meth:`status_for` once per
        #: reference, and reparsing the index from its cached JSON on every
        #: single call would cost real time for no benefit within one process.
        self._index: dict[str, RetractionNotice] | None = None

    def status_for(self, dois: Sequence[str]) -> RetractionStatus:
        """Retraction status for *dois*, keyed by normalized DOI.

        A DOI absent from :attr:`RetractionStatus.notices` carries no signal
        from either source — which is not the same claim as "confirmed not
        retracted"; see the module docstring's discussion of what a caller can
        and cannot conclude from silence here. Weaker still when
        :attr:`RetractionStatus.unreachable` is non-empty, which is precisely
        why that field is returned beside the notices rather than left for the
        caller to guess at. Never raises for a Retraction Watch outage (see
        :meth:`_rw_signals`); does raise
        :class:`~bibaudit.registries.http.Transient` for a genuine PubMed
        outage, exactly as :meth:`PubMed.by_dois` already does, because that
        is real ignorance about a registry this project relies on for
        retraction corroboration.
        """
        wanted = list(dict.fromkeys(doi for raw in dois if (doi := normalize_doi(raw))))
        if not wanted:
            # Nothing was asked, so nothing went unanswered: an empty request
            # must not manufacture an outage any more than it may manufacture
            # a finding.
            return RetractionStatus(notices={}, unreachable=frozenset())

        from_rw, rw_unreachable = self._rw_signals(wanted)
        try:
            from_pubmed = self._pubmed_signals(wanted)
        except Transient as exc:
            # PubMed's outage still propagates — it is a registry this project
            # relies on for corroboration. But Retraction Watch's fate is
            # already known by now and would be lost with this frame, leaving
            # the caller to report `retraction-watch: answered` for a source
            # that was never reached. Carry both out on the exception.
            raise RetractionOutage(
                str(exc), frozenset({"pubmed"}) | rw_unreachable
            ) from exc

        merged: dict[str, RetractionNotice] = {}
        for doi in {*from_rw, *from_pubmed}:
            candidates = [n for n in (from_rw.get(doi), from_pubmed.get(doi)) if n is not None]
            merged[doi] = _combine(candidates)
        return RetractionStatus(notices=merged, unreachable=rw_unreachable)

    def _rw_signals(
        self, dois: Sequence[str]
    ) -> tuple[dict[str, RetractionNotice], frozenset[str]]:
        """Retraction Watch's answer for *dois*, and whether it answered at all.

        "Raise Transient for that source only and let the others answer" means
        exactly this: the outage is caught here, at the boundary of the one
        source it belongs to, rather than propagated out of :meth:`status_for`
        and losing PubMed's independent answer with it. It must not vanish
        either, so it comes back as the second element for :meth:`status_for`
        to hand on. The ``warnings.warn`` is what a library consumer that never
        reads :attr:`RetractionStatus.unreachable` still sees.
        """
        try:
            index = self._load_index()
        except Transient as exc:
            warnings.warn(
                f"Retraction Watch data could not be fetched this run ({exc}); "
                "continuing with PubMed's retraction signal alone. This "
                "degrades corroboration for this call, it does not fail it.",
                RuntimeWarning,
                stacklevel=3,
            )
            return {}, frozenset({"retraction-watch"})
        wanted = set(dois)
        return {doi: notice for doi, notice in index.items() if doi in wanted}, frozenset()

    def _load_index(self) -> dict[str, RetractionNotice]:
        """The full Retraction Watch DOI index, fetched or replayed from cache.

        Raises :class:`~bibaudit.registries.http.Transient` on a genuine
        network outage; callers within this module catch it at the one place
        that must (:meth:`_rw_signals`). A confirmed-empty response (this
        endpoint has never been observed to 404, but nothing here assumes it
        cannot) is treated as "nothing found" rather than as an outage: it is
        a fact about this fetch, like any other confirmed absence in this
        project, not ignorance.
        """
        if self._index is not None:
            return self._index

        cached = self._cache.get(_RW_INDEX_CACHE_KEY)
        if cached is not None:
            self._index = _index_from_payload(cached)
            return self._index

        text = self._client.get_text(_RW_CSV_URL)
        index = _parse_rw_csv(text) if text is not None else {}
        self._cache.put(_RW_INDEX_CACHE_KEY, {"url": _RW_CSV_URL, "payload": _index_to_payload(index)})
        self._index = index
        return index

    def _pubmed_signals(self, dois: Sequence[str]) -> dict[str, RetractionNotice]:
        """PubMed's answer for *dois*, via the public ``by_dois`` -- see the
        module docstring for why this is reuse, not a second MEDLINE client.
        """
        records = self._pubmed.by_dois(dois)
        out: dict[str, RetractionNotice] = {}
        for doi, record in records.items():
            notice = _notice_from_pubmed(doi, record)
            if notice is not None:
                out[doi] = notice
        return out
