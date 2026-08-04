"""Orchestration: collect references, resolve them, compare, report.

The pipeline is deliberately linear and side-effect free with respect to the
user's files. Nothing here writes anywhere except the HTTP cache.

Resolution order for a reference carrying an identifier:

1. **Crossref** — broadest coverage of DOI-registered scholarly works, and the
   only source that carries the Retraction Watch linkage.
2. **DataCite** — for the DOIs Crossref does not have: arXiv, Zenodo, figshare,
   most data and software deposits.
3. **PubMed** — *independent* corroboration, not a third opinion of the same
   opinion. OpenAlex, Semantic Scholar and Unpaywall re-crawl Crossref, so their
   agreement adds no evidence; PubMed's records are curated separately, which is
   why it is the one consulted for a second view.

A reference carrying **no** identifier at all takes a different path entirely,
``_audit_unidentified``, which asks ``registries.search``
(:class:`~bibaudit.registries.search.Search`) — Crossref, Europe PMC and
OpenAlex — rather than Crossref alone, so that one registry's silence on a
citation nothing can DOI-lookup does not by itself become ``UNCONFIRMED``. See
that module's docstring for why OpenAlex counts for discovery there but never
for corroboration.

**Books** take a third path, keyed on ISBN rather than DOI, because most books
never had a DOI minted at all: an entry carrying an ``isbn`` is resolved
through :class:`~bibaudit.registries.openlibrary.OpenLibrary`, exactly as a
DOI-bearing one is resolved through Crossref — see :func:`resolve`. A book or
chapter with **no** identifier at all adds OpenLibrary to the candidate search
alongside ``registries.search`` (see ``_audit_unidentified``), because a
title/author search that never asks the one registry organised around books
is not really searching for one. An ISBN whose check digit fails is neither a
fact (no registry was asked) nor ignorance (no registry could not be reached)
— it is a defect in the bibliography's own data, provable without a network
call, and is reported as a malformed identifier rather than as a book
OpenLibrary was asked about and does not have.

**Retraction status** is checked for every reference that resolves, however it
resolved — by DOI, by ISBN, or by a search-confirmed candidate that happens to
carry a DOI — against two sources :mod:`~bibaudit.compare` cannot otherwise
see: Retraction Watch's own bulk export and PubMed's ``ECI`` ("Expression of
Concern In:") cross-reference, both read through
:class:`~bibaudit.registries.retractions.Retractions`. Crossref's and PubMed's
*own* retraction flags (their ``updated-by``/``PT`` readings) are untouched by
this and keep working exactly as they did before; this only adds what those
two side-channels miss — see ``registries/retractions.py`` for the three gaps
that motivated it. A DOI *no* bibliographic registry could resolve is never
promoted to "resolved" on the strength of a retraction notice alone —
:func:`_merge_retraction_notices` only ever adds to an identifier's records
when some other registry already put an entry there, so a DOI nothing else
confirms still reaches ``compare``'s ``BAD-ID`` path exactly as it did before
retraction checking existed; see that function's docstring. PubMed's own
corroboration fetch (above) and this module's retraction fetch are handed the
identical, identically-ordered DOI list for exactly this reason: ``Retractions``
reuses ``PubMed.by_dois`` internally (see that module's docstring), and hand it
a *different* batch — even a subset — and its ``esearch`` calls chunk
differently and stop landing on the corroboration fetch's own cache entries,
turning one logical retraction check into a second, real round trip against a
rate-limited registry. See :func:`_resolve_retractions`.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from .compare import Thresholds, compare, confirm_without_id, verdict_for
from .model import Issue, Record, Reference, Result
from .normalize import normalize_doi, normalize_kind
from .registries.crossref import Crossref
from .registries.datacite import DataCite
from .registries.http import Cache, Client, Transient, default_cache_dir
from .registries.openlibrary import OpenLibrary, normalize_isbn13
from .registries.pubmed import PubMed
from .registries.retractions import RetractionNotice, Retractions
from .registries.search import Search
from .suppress import Suppressions

__all__ = ["AuditOptions", "audit", "resolve"]


@dataclass(slots=True)
class AuditOptions:
    """Everything that varies between runs.

    ``offline`` is the reproducibility switch: it serves every lookup from the
    cache and marks anything absent as ``UNCHECKED`` rather than reaching the
    network, so a previous run can be re-examined without changing its inputs.
    """

    cache_dir: Path = field(default_factory=default_cache_dir)
    cache_ttl_days: int = 90
    #: Sent to Crossref and NCBI. Not required, but it moves Crossref requests
    #: into the polite pool and is what those services ask for.
    mailto: str | None = None
    refresh: bool = False
    offline: bool = False
    #: PubMed corroboration roughly doubles request volume. Worth it for
    #: biomedical bibliographies, pointless for a corpus that has no PMIDs.
    corroborate: bool = True
    thresholds: Thresholds = field(default_factory=Thresholds)
    suppressions: Suppressions | None = None
    #: Look up entries that carry no identifier by title/author search.
    search_unidentified: bool = True
    timeout: float = 30.0
    #: Check every resolved reference's retraction status against Retraction
    #: Watch's own export and PubMed's ``ECI`` cross-reference (see
    #: ``registries/retractions.py``). On by default: a citation to a
    #: retracted paper is the one verdict where a miss costs the most, and
    #: Crossref's/PubMed's own side-channel signal alone (a publisher's
    #: ``updated-by`` deposit, MEDLINE's ``PT`` flag) already proved
    #: insufficient — that gap is this option's reason to exist. Turning it
    #: off does not disable retraction reporting outright: a Crossref or
    #: PubMed record that itself carries a retraction linkage still fails as
    #: ``RETRACTED``; only the independent corroboration is lost.
    retraction_check: bool = True
    #: Resolve books through Open Library — by ISBN when one is stored (see
    #: ``resolve``) and by title/author search when it is not (see
    #: ``_audit_unidentified``). On by default: without it, a book carrying
    #: only an ISBN is never looked up at all, and one with no identifier
    #: loses the one candidate source organised around books.
    use_isbn: bool = True
    #: Widen ``_audit_unidentified``'s search to Europe PMC. On by default:
    #: independent curation that indexes material PubMed does not —
    #: preprints, some grey literature, agency reports.
    use_europepmc: bool = True
    #: Widen ``_audit_unidentified``'s search to OpenAlex. On by default: its
    #: basic API needs no key (see ``registries/search.py``), and it only ever
    #: widens what can be *found*, never what gets *accepted* — see
    #: ``compare.confirm_without_id``.
    use_openalex: bool = True


@dataclass(slots=True)
class _Registries:
    crossref: Crossref
    datacite: DataCite
    pubmed: PubMed | None
    #: Multi-registry lookup for references with no identifier at all — see
    #: ``_audit_unidentified``. Kept separate from ``crossref`` above even
    #: though it wraps its own ``Crossref`` internally: the DOI-resolution
    #: path (``resolve``) and the search path have never shared a code path,
    #: and merging them here would only make it look like they did.
    search: Search
    #: ISBN lookup for books. ``None`` both when a test constructs
    #: ``_Registries`` by hand without naming every registry that exists, and
    #: when ``--no-isbn`` turned it off — ``resolve`` and
    #: ``_audit_unidentified`` both guard on ``is not None`` before touching
    #: it, so the two cases need no separate flag threaded through either.
    openlibrary: OpenLibrary | None = None
    #: Independent retraction corroboration (Retraction Watch, PubMed's
    #: ``ECI``). ``None`` for the same two reasons as ``openlibrary`` above —
    #: a hand-built test, or ``--no-retraction-check``. Guarded the same way,
    #: in :func:`_resolve_retractions` and nowhere else: this is the *only*
    #: place :class:`~bibaudit.registries.retractions.Retractions` is ever
    #: consulted, so there is exactly one call site to keep in sync with the
    #: flag.
    retractions: Retractions | None = None


def _build(options: AuditOptions) -> _Registries:
    cache = Cache(options.cache_dir, ttl_days=options.cache_ttl_days)
    client = Client(
        cache,
        mailto=options.mailto,
        timeout=options.timeout,
        refresh=options.refresh,
        offline=options.offline,
    )
    return _Registries(
        crossref=Crossref(client),
        datacite=DataCite(client),
        pubmed=PubMed(client) if options.corroborate else None,
        search=Search(
            client, use_europepmc=options.use_europepmc, use_openalex=options.use_openalex
        ),
        openlibrary=OpenLibrary(client) if options.use_isbn else None,
        retractions=Retractions(client) if options.retraction_check else None,
    )


def resolve(
    refs: Sequence[Reference],
    registries: _Registries,
) -> tuple[dict[str, dict[str, Record]], set[str]]:
    """Fetch registry records for every reference that carries a DOI or an ISBN.

    Returns ``(records_by_identifier, unreachable_registries)`` — keyed by
    normalised DOI for a DOI-bearing reference and by normalised ISBN-13 for
    an ISBN-bearing one with no DOI. The two never collide (a DOI always
    starts ``10.``; an ISBN-13 is thirteen digits), so one dict serves both
    without ambiguity. Keeping the unreachable set separate from "no record
    found" is what stops a network outage from being reported as a
    bibliography full of fabricated citations.
    """
    dois = sorted({normalize_doi(r.doi) for r in refs if r.doi})
    records: dict[str, dict[str, Record]] = {doi: {} for doi in dois}
    unreachable: set[str] = set()

    if dois:
        _resolve_dois(dois, registries, records, unreachable)
        _resolve_retractions(dois, registries, records, unreachable)

    # Only references with no DOI are worth an ISBN lookup: a reference
    # carrying both is vanishingly rare (a handful of ebook publishers mint
    # DOIs for books) and the DOI, already the stronger identifier by
    # `Reference.identifier`'s own ordering, is resolved above.
    isbns = sorted(
        {
            isbn13
            for r in refs
            if not r.doi and r.isbn and (isbn13 := normalize_isbn13(r.isbn))
        }
    )
    if isbns and registries.openlibrary is not None:
        for isbn13 in isbns:
            records.setdefault(isbn13, {})
        try:
            for isbn13, record in registries.openlibrary.by_isbns(isbns).items():
                records.setdefault(isbn13, {})["openlibrary"] = record
        except Transient:
            unreachable.add("openlibrary")

    return records, unreachable


def _resolve_dois(
    dois: Sequence[str],
    registries: _Registries,
    records: dict[str, dict[str, Record]],
    unreachable: set[str],
) -> None:
    """The Crossref -> DataCite -> PubMed pipeline, factored out of :func:`resolve`.

    Split out so the ISBN pass added alongside it in :func:`resolve` reads as
    a parallel, independent lookup rather than one more branch bolted onto an
    already-long function — the two share nothing but the *shape* of "try,
    record what answered, note what could not be reached".
    """
    try:
        for doi, record in registries.crossref.by_dois(dois).items():
            records.setdefault(doi, {})["crossref"] = record
    except Transient:
        unreachable.add("crossref")

    # Only DOIs Crossref did not answer for are worth asking DataCite about.
    leftover = [d for d in dois if "crossref" not in records.get(d, {})]
    if leftover and "crossref" not in unreachable:
        try:
            for doi, record in registries.datacite.by_dois(leftover).items():
                records.setdefault(doi, {})["datacite"] = record
        except Transient:
            unreachable.add("datacite")

    if registries.pubmed is not None:
        try:
            for doi, record in registries.pubmed.by_dois(dois).items():
                records.setdefault(doi, {})["pubmed"] = record
        except Transient:
            # Losing corroboration degrades the check; it does not invalidate it.
            unreachable.add("pubmed")


def _resolve_retractions(
    dois: Sequence[str],
    registries: _Registries,
    records: dict[str, dict[str, Record]],
    unreachable: set[str],
) -> None:
    """Fold independent retraction status into every DOI that already resolved.

    *dois* is passed through unchanged, not filtered to the subset that
    actually resolved — see the module docstring for why: it is the same list
    ``_resolve_dois`` already handed ``registries.pubmed.by_dois`` a moment
    earlier, and ``Retractions.status_for`` reuses that exact call
    internally. Checking a few extra, never-resolved DOIs against Retraction
    Watch's index costs nothing once the index is loaded (a dict lookup
    each); filtering here instead would shift ``PubMed.by_dois``'s batch
    boundaries and turn a cache hit into a second live query.

    Filtering happens on the way *out*, in :func:`_merge_retraction_notices`,
    which only ever adds to a DOI ``_resolve_dois`` already put an entry
    under — never creates the first one.

    The two sources fail through different channels and both must end in
    *unreachable*. A :class:`~bibaudit.registries.http.Transient` reaching this
    function is specifically PubMed's leg; Retraction Watch's own fetch failing
    is absorbed inside :meth:`Retractions.status_for` and returned as
    :attr:`~bibaudit.registries.retractions.RetractionStatus.unreachable`, so
    that set has to be folded in here. Both names carry a retraction signal in
    :data:`~bibaudit.compare._NO_RETRACTION_SIGNAL`'s terms, and it is
    *unreachable* that makes ``compare._status_issues`` state the gap rather
    than let a run that consulted nothing read as clean.
    """
    if registries.retractions is None:
        return
    try:
        status = registries.retractions.status_for(dois)
    except Transient as exc:
        # `RetractionOutage` names every retraction source that was down, not
        # just the leg that raised; a plain Transient from anywhere else is
        # PubMed's.
        unreachable |= getattr(exc, "unreachable", frozenset({"pubmed"}))
        return
    unreachable |= status.unreachable
    _merge_retraction_notices(records, status.notices)


def _merge_retraction_notices(
    records: Mapping[str, dict[str, Record]],
    notices: Mapping[str, RetractionNotice],
) -> None:
    """Fold *notices* into *records* in place, one DOI at a time.

    Never creates the *first* entry for a DOI (``if not found: continue``): a
    DOI no bibliographic registry could name would otherwise start looking
    "resolved" on the strength of a retraction notice alone, promoting a
    bare, fieldless stub to ``primary`` in ``compare`` and reporting a wall of
    "missing title", "missing authors" findings about a citation that should
    have been ``BAD-ID`` — see the module docstring.

    PubMed's contribution is folded into the DOI's *existing* ``"pubmed"``
    entry when one is already there, in place, rather than added under a
    second key: it is the very same MEDLINE record ``registries/pubmed.py``
    already fetched, read one field further (the ``ECI`` cross-reference —
    see ``registries/retractions.py``'s module docstring), not a second
    witness, and giving it a second key would let ``compare._status_issues``
    attribute one fact to two named sources. When no ``"pubmed"`` entry
    exists yet (``--no-corroborate``), a fresh one is inserted instead —
    nothing to clobber, and the DOI is not "resolved" purely by this stub
    because ``if not found: continue`` above already required a real entry to
    exist under some *other* key first.

    Retraction Watch is not a registry ``resolve`` ever asks about a DOI's
    bibliographic fields, so its contribution always becomes its own entry,
    keyed ``"retraction-watch"`` — the exact string
    ``registries/retractions.py`` already stamps on a notice sourced from it,
    so the two modules cannot drift into naming the same source two ways.
    """
    for doi, notice in notices.items():
        found = records.get(doi)
        if not found:
            continue
        sources = [source for source in notice.source.split(",") if source]
        if "pubmed" in sources:
            existing = found.get("pubmed")
            found["pubmed"] = (
                replace(existing, retracted=True, retraction_kind=notice.kind)
                if existing is not None
                else Record(
                    source="pubmed", doi=doi, retracted=True, retraction_kind=notice.kind
                )
            )
        if "retraction-watch" in sources:
            found["retraction-watch"] = Record(
                source="retraction-watch",
                doi=doi,
                retracted=True,
                retraction_kind=notice.kind,
            )


def _asked_registries(
    ref: Reference,
    found: Mapping[str, Record],
    unreachable: Collection[str],
    options: AuditOptions,
) -> set[str]:
    """Which registries :func:`resolve` actually queried on *ref*'s behalf.

    ``compare`` cannot work this out for itself. All it sees is the records it
    was handed, so a registry that was asked and answered "I do not hold this
    DOI" leaves no trace and — with ``asked`` omitted — is reported as
    ``not-asked``. That is a false statement about the evidence a verdict rests
    on, which is the exact failure :data:`~bibaudit.model.Consultation` was
    introduced to make representable, arriving from the other direction.

    Witnessed on the 438-entry epidemiology corpus this tool was developed
    against: every one of
    the four ``compare`` calls below omitted ``asked``, and the JSON report for
    ``marin2004xpc`` (10.1158/1055-9965.1788.13.11, Marin et al., *Cancer
    Epidemiol Biomarkers Prev* 2004) said ``"pubmed": "not-asked"``. PubMed was
    asked: the cached ``esearch`` for the batch holding that DOI lists 20
    ``[aid]`` clauses and returns 19 PMIDs. NLM does not index that article's
    DOI, and its saying so is corroborating evidence — the run's own record of
    what it consulted must not throw it away.

    The plan mirrored here is ``resolve``'s, clause for clause; ``tests/
    test_audit_corpus.py`` drives ``resolve`` with recording stubs and asserts
    the two still agree, so the copy cannot drift unnoticed.
    """
    if not ref.doi or not normalize_doi(ref.doi):
        # ``resolve`` only ever looks up DOIs, and it looks up the *normalised*
        # form — a stored value that normalises to nothing is dropped by every
        # registry client before a request is built, so no registry was asked
        # about this entry however non-empty its ``doi`` field looks. What is
        # asked about an entry carrying no identifier is decided in
        # ``_audit_unidentified``.
        return set()
    asked = {"crossref"}
    # ``resolve`` asks DataCite only about the DOIs Crossref did not answer for,
    # and only when Crossref was reachable at all.
    if "crossref" not in unreachable and "crossref" not in found:
        asked.add("datacite")
    if options.corroborate:
        asked.add("pubmed")
    if options.retraction_check:
        # ``_resolve_retractions`` queries both, for every DOI-bearing
        # reference, regardless of whether it resolved — see that function's
        # docstring on why the DOI list is never filtered first. So both
        # belong in ``asked`` unconditionally here too, even when
        # ``--no-corroborate`` already means PubMed's *bibliographic*
        # corroboration was never sought: ``Retractions`` still asked it, on
        # this reference's behalf, for its retraction status alone.
        asked.add("pubmed")
        asked.add("retraction-watch")
    return asked


def audit(refs: Sequence[Reference], options: AuditOptions | None = None) -> list[Result]:
    """Check every reference and return one :class:`Result` each.

    References are compared in input order so the report follows the file.
    """
    options = options or AuditOptions()
    registries = _build(options)
    records, unreachable = resolve(refs, registries)

    results: list[Result] = []
    for ref in refs:
        isbn13 = normalize_isbn13(ref.isbn) if ref.isbn else None
        if ref.doi:
            found = records.get(normalize_doi(ref.doi), {})
            result = compare(
                ref,
                found,
                thresholds=options.thresholds,
                unreachable=unreachable,
                asked=_asked_registries(ref, found, unreachable, options),
            )
        elif isbn13:
            found = records.get(isbn13, {})
            result = compare(
                ref,
                found,
                thresholds=options.thresholds,
                unreachable=unreachable,
                # Under ``--no-isbn`` there is no OpenLibrary to query, so
                # naming it would claim it was asked and held nothing. The
                # empty set is what makes ``compare`` report UNCHECKED instead
                # of accusing a book whose ISBN nobody looked up.
                asked={"openlibrary"} if registries.openlibrary is not None else set(),
            )
        elif ref.isbn:
            # The ISBN is present but its check digit fails: a fact about the
            # bibliography's own data, provable without asking a registry.
            # `compare`'s ordinary "no record found" path assumes a registry
            # was consulted and had nothing (see its `doi/unresolved` note);
            # reusing it unmodified here would claim OpenLibrary was asked
            # about this book and does not have it, when in truth nothing was
            # ever queried — see `OpenLibrary.normalize_isbn13`.
            result = compare(ref, {}, thresholds=options.thresholds, asked=set())
            if result.issues:
                result.issues[0].field = "isbn"
                result.issues[0].kind = "malformed"
                result.issues[0].severity = "error"
                result.issues[0].note = (
                    "ISBN fails its check digit; treated as a malformed "
                    "identifier, not queried against any registry"
                )
            # Both this branch and `--no-isbn` hand `compare` an empty `asked`,
            # and they must not share a verdict. There, a registry could have
            # answered and was not consulted, so the entry is UNCHECKED. Here
            # the check digit is arithmetic on the stored value and it failed:
            # no registry's answer could change that, so it is a finding about
            # the bibliography rather than a gap in coverage.
            result.verdict = "BAD-ID"
        elif options.search_unidentified:
            # Offline runs take this path too. The search goes through the same
            # Client, which either serves a cached response or raises Transient
            # without touching the network, so an uncached entry ends up
            # UNCHECKED — exactly what --offline promises. Skipping the search
            # outright instead handed the entry to compare() with an empty
            # `unreachable` set, and an offline run over a bibliography whose
            # entries carry no DOIs reported every one of them as UNCONFIRMED,
            # a *failing* verdict, having consulted nothing at all. Worse, the
            # verdict was not even stable: adding one DOI-bearing entry to the
            # same file turned those same entries into UNCHECKED, because that
            # other entry's outage was what populated `unreachable`.
            result = _audit_unidentified(ref, registries, options, unreachable)
        else:
            # ``--no-search`` on an entry with no identifier: nothing was looked
            # up, and the empty set says so rather than leaving every registry
            # to be inferred as "not asked" by accident.
            result = compare(
                ref,
                {},
                thresholds=options.thresholds,
                unreachable=unreachable,
                asked=set(),
            )

        if options.suppressions and options.suppressions.apply(result):
            # Issues were adjudicated away, so the verdict must be re-derived by
            # the same rule that produced it, never patched up ad hoc.
            result.verdict = verdict_for(
                result.issues,
                result.suppressed,
                retracted=result.verdict == "RETRACTED",
                # `verdict_for` consults `authors_ok` only when a wrong-work
                # title issue is present, and returns WRONG-WORK only when it is
                # False — so the verdict just derived recovers the flag exactly.
                # Letting it fall back to its default of True downgraded
                # WRONG-WORK to FIELD-MISMATCH whenever some *unrelated* field
                # (a publisher imprint, say) was suppressed. Adjudicating one
                # field must never change what the tool concludes about another.
                authors_ok=result.verdict != "WRONG-WORK",
            )
        results.append(result)
    return results


def _audit_unidentified(
    ref: Reference,
    registries: _Registries,
    options: AuditOptions,
    unreachable: set[str],
) -> Result:
    """Try to confirm a reference that carries no identifier.

    Confirmation needs title, first author and year to agree, and the work's
    type to be compatible. A title-only match is refused: searching a book title
    reliably returns a review of the book, and adopting that would turn a
    missing DOI into a wrong one. The bar itself lives entirely in
    ``compare.confirm_without_id`` and is identical for every candidate
    regardless of which registry found it — querying more sources widens what
    can be *found*, never what gets *accepted*.

    A book or chapter also gets OpenLibrary added to the candidate pool
    alongside ``registries.search``: none of Crossref, Europe PMC or OpenAlex
    is organised around books, so leaving OpenLibrary out here would mean the
    ISBN-keyed lookup in :func:`resolve` is the *only* way a book ever stops
    being ``UNCONFIRMED`` — no help at all to the book that was never given an
    ISBN in the bibliography either.
    """
    kind = normalize_kind(ref.kind)
    # Asked only for the kinds Open Library actually models. Asking it about a
    # journal article wastes a request and, since Open Library's search
    # ranks by title alone, risks manufacturing a book candidate for
    # `confirm_without_id` to consider and reject rather than never
    # considering at all.
    use_openlibrary = kind in {"book", "chapter"} and registries.openlibrary is not None

    # Every registry `registries.search` is configured to consult for an
    # entry with no identifier — DataCite and PubMed are still never asked
    # (neither has a useful free-text search for this tool's purposes), and
    # naming exactly which ones were is the difference between "three curated
    # sources had nothing" and "one was tried". See `Search.sources`.
    asked = set(registries.search.sources)
    if use_openlibrary:
        asked = asked | {"openlibrary"}

    candidates: list[Record] = []
    #: Sources that failed *for this entry*, as opposed to `unreachable`,
    #: which is the run-wide set. Kept separate for the same reason
    #: `_asked_registries` never lets the run-wide set answer a per-entry
    #: question: a registry that fell over resolving some other entry's DOI
    #: has no bearing on whether *this* search succeeded.
    failed: set[str] = set()

    try:
        candidates.extend(registries.search.candidates(ref))
    except Transient:
        # Every source `registries.search` consults failed for this entry —
        # see that method's own docstring for why this is raised only when
        # *every* one of them was unreachable. OpenLibrary is independent of
        # all three and still gets its own chance below.
        failed |= set(registries.search.sources)

    if use_openlibrary:
        assert registries.openlibrary is not None  # narrowed by `use_openlibrary`
        try:
            candidates.extend(registries.openlibrary.search(ref))
        except Transient:
            failed.add("openlibrary")

    if failed and failed == asked:
        # Nothing this entry could have been checked against was reachable —
        # true ignorance, not "nothing found".
        return compare(
            ref, {}, thresholds=options.thresholds,
            unreachable=unreachable | failed, asked=asked,
        )

    record, explanation = confirm_without_id(ref, candidates, thresholds=options.thresholds)
    if record is None:
        # Deliberately *not* `failed` or the run-wide `unreachable`. The
        # sources that *did* answer are the only thing asked about an entry
        # carrying no identifier, and they answered — so this entry's
        # evidence is complete, whatever happened to a DOI pass on behalf of
        # *other* entries or to one other search source. `compare` reads any
        # non-empty `unreachable` set paired with no record as ignorance
        # about the *whole* entry, which is only true when every source
        # failed — the branch above already handles that case, and passing
        # `failed` here (a genuine partial outage) would make it fire wrongly
        # too: a reference with no identifier that the sources which *did*
        # answer could not confirm — the exact shape a fabricated one takes —
        # must not stop being reported the moment one other source times out.
        result = compare(ref, {}, thresholds=options.thresholds, asked=asked)
        if result.issues:
            result.issues[-1].note = f"no confident match: {explanation}"
        return result

    found_records: dict[str, Record] = {record.source: record}
    # A search-confirmed candidate's own DOI is new to this run — it was
    # never in `resolve()`'s DOI list, precisely because the entry that led to
    # it had none stored — so checking it here duplicates no earlier query.
    # `record.doi` gates it because Open Library mints no DOIs (nothing to
    # check) and `registries.search` sometimes returns a grey-literature hit
    # with none either; both cases already skip the `doi/proposed` issue below
    # for the identical reason.
    if record.doi and registries.retractions is not None:
        asked = asked | {"pubmed", "retraction-watch"}
        try:
            status = registries.retractions.status_for([record.doi])
        except Transient as exc:
            failed = failed | getattr(exc, "unreachable", frozenset({"pubmed"}))
        else:
            # Same reason as in `_resolve_retractions`: a Retraction Watch
            # outage never raises, so the only way it reaches `consulted` —
            # which `asked` above has already promised to report on — is
            # through the returned set.
            failed = failed | status.unreachable
            _merge_retraction_notices({record.doi: found_records}, status.notices)

    result = compare(
        ref,
        found_records,
        thresholds=options.thresholds,
        # Safe here, unlike above: a record was found, so `primary` will not
        # be `None` and `compare`'s coarse "unreachable with no record means
        # ignorance" branch is never reached. Folding in both sets only makes
        # `Result.consulted` more accurate about a source that could not be
        # reached while another confirmed the entry.
        unreachable=unreachable | failed,
        asked=asked,
    )
    if record.doi:
        # The work is confirmed; what is missing is the identifier. That is a
        # proposal for a human to accept, never something the tool writes.
        # Open Library mints no DOIs, so a candidate it confirmed has none to
        # propose here — the entry is still confirmed by title/author/year,
        # it simply gains no "add this DOI" suggestion.
        result.issues.insert(
            0,
            Issue(
                field="doi",
                kind="proposed",
                severity="warning",
                stored="",
                registry=record.doi,
                source=record.source,
                note=f"entry has no DOI; {explanation}",
            ),
        )
        # Re-derived, never patched — but re-derived with `retracted` stated
        # explicitly this time. Inserting the issue above changes the list
        # `verdict_for` reads, and omitting `retracted` here defaults it to
        # `False`: a work the retraction check just above confirmed is
        # retracted would silently fall through to `FIELD-MISMATCH` (the
        # `status/retracted` issue is still an error, just not the one
        # `verdict_for` checks first), the exact downgrade CLAUDE.md's third
        # rule forbids. Mirrors the identical re-derivation `audit()` performs
        # after a suppression.
        result.verdict = verdict_for(
            result.issues, result.suppressed, retracted=result.verdict == "RETRACTED"
        )
    return result
