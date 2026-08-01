"""Crossref registry client.

Crossref is bibaudit's primary registry: it is queried first for every entry
that carries a DOI, and it is the only registry queried for entries that
carry no identifier at all (see :meth:`Crossref.search`).

Two behaviours here are easy to get backwards and were verified against the
live API before being written, not assumed from memory:

* **Retraction direction.** Crossref links an original work and its
  retraction/correction notice with a pair of fields, and the two are *not*
  interchangeable. Per Crossref's own "Registering updates" documentation,
  the notice deposits an ``update-to`` entry naming the work it updates;
  Crossref's system then writes the reciprocal ``updated-by`` entry onto the
  *original* work's own record. So ``updated-by`` on a record is the signal
  that the record has itself been retracted/corrected — ``update-to`` on a
  record instead means the record *is* such a notice. Confirmed live against
  10.1016/S0140-6736(20)31180-6, the retracted Surgisphere hydroxychloroquine
  paper: its record carries ``updated-by`` entries of type ``retraction``,
  while its retraction notice (10.1016/S0140-6736(20)31324-6) carries
  ``update-to`` pointing back at it. Reading ``update-to`` to decide
  retractedness would flag notices and silently clear the papers they
  retract — exactly backwards.

  That verification was incomplete: it looked at the paper and not at the
  notice. Elsevier deposits the relation in **both** directions on the notice
  as well, so the notice also carries an ``updated-by`` of type ``retraction``
  and was reported as a retracted work. :func:`_reciprocal_updates` handles
  that one case and nothing wider; the argument for it, and for why the
  obvious version of it is catastrophic, is in its docstring.
* **The ``select`` query parameter is collection-only.** Crossref's single
  work route (``/works/{doi}``) rejects ``select`` with an HTTP 400
  ``parameter-not-allowed`` error; only the collection routes
  (``/works?filter=...``, ``/works?query.bibliographic=...``) accept it.
  ``by_dois``'s per-DOI fallback therefore requests the unfiltered record
  rather than repeating the ``select`` list used for the batch query.

Integration note
-----------------
This module calls the injected :class:`~bibaudit.registries.http.Client`
through ``get_json(url) -> dict[str, Any] | None``: the parsed JSON body of
``url`` on success, ``None`` if the server answered 404 (the work does not
exist — a fact), and a raised :class:`~bibaudit.registries.http.Transient` if
the request could not be completed after the client's own retry policy
(timeout, connection error, HTTP 429, or repeated 5xx — unknown, not a
fact). Caching and Crossref's polite-pool etiquette (a ``User-Agent``
carrying a contact ``mailto``, per-host rate limiting) are ``Client``'s
responsibility; this module only builds URLs and interprets responses.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from urllib.parse import quote

from ..model import Name, Record, Reference
from ..normalize import clean, fold, normalize_doi
from .http import Client, Transient

__all__ = ["Crossref"]

#: Crossref accepts at most this many ``doi:`` clauses reliably in one
#: ``filter=`` query before responses get unwieldy; 20 keeps each batch well
#: inside Crossref's practical URL-length comfort zone.
_BATCH_SIZE = 20

#: Deliberately double `_BATCH_SIZE`: each `doi:` filter clause matches at
#: most one work (DOIs are unique), so 40 is headroom rather than an
#: expected count.
_BATCH_ROWS = 40

_API_ROOT = "https://api.crossref.org"

#: Fields pulled for every work, batch or single. ``abstract`` is
#: deliberately absent — nothing in this tool compares abstracts, and
#: Crossref's abstract payloads are large enough to be worth not asking for.
_SELECT_FIELDS = (
    "DOI,title,subtitle,author,editor,issued,published-print,"
    "published-online,container-title,short-container-title,volume,issue,"
    "page,type,publisher,ISSN,update-to,updated-by"
)

#: Crossref update-notice types that mean a work has been pulled back in some
#: way, most to least severe. When a record's ``updated-by`` carries more
#: than one of these (10.1016/S0140-6736(20)31180-6 carries both
#: ``expression_of_concern`` and ``retraction`` entries, deposited by
#: different sources at different times), the more definitive event wins: a
#: later retraction supersedes an earlier expression of concern for the same
#: paper, and reporting the milder one would understate the defect.
#:
#: ``partial retraction`` (Crossref's ``partial_retraction``, folded) is
#: included: its absence was verified live to be a silent miss, not a
#: hypothetical one — 10.29328/journal.jcmhs.1001023 carries only a
#: ``partial_retraction`` entry in ``updated-by`` and nothing else, so
#: omitting the type from this tuple reports that record as clean. It ranks
#: below a full retraction/withdrawal/removal (only part of the work was
#: pulled) but above an expression of concern (which is a stated doubt, not
#: yet a corrective action).
_RETRACTION_PRIORITY = (
    "retraction",
    "withdrawal",
    "removal",
    "partial retraction",
    "expression of concern",
)


class Crossref:
    """Client for the Crossref REST API (``api.crossref.org``)."""

    name = "crossref"

    def __init__(self, client: Client) -> None:
        """Wrap *client* for caching, rate limiting and retry behaviour.

        Crossref requests never bypass *client*: this class only builds URLs
        and shapes responses into :class:`Record`.
        """
        self._client = client

    def by_dois(self, dois: Sequence[str]) -> dict[str, Record]:
        """Resolve *dois* to :class:`Record` objects, keyed by normalized DOI.

        A DOI absent from the returned mapping means Crossref does not have
        it (a 404 is a fact); if the registry cannot be reached at all, a
        ``Transient`` raised deep in a per-DOI fallback is left to propagate
        rather than being swallowed into a merely-incomplete result — an
        outage must not look like a pile of missing works.
        """
        normalized = _dedupe_normalized(dois)
        results: dict[str, Record] = {}

        for start in range(0, len(normalized), _BATCH_SIZE):
            batch = normalized[start : start + _BATCH_SIZE]
            try:
                items = self._fetch_batch(batch)
            except Transient:
                # One malformed DOI is enough to 400 Crossref's whole
                # `filter=doi:a,doi:b,...` query (verified live: a single
                # invalid clause fails validation for the entire filter, not
                # just that clause), which would otherwise cost all 20
                # lookups in the batch rather than just the bad one. Falling
                # back to `/works/{doi}` per DOI recovers the rest; that
                # route 404s cleanly on a bad DOI instead of validating it.
                for doi in batch:
                    record = self._fetch_one(doi)
                    if record is not None and record.doi:
                        results[record.doi] = record
                continue

            for item in items:
                record = self._record_from_work(item)
                if record.doi:
                    results[record.doi] = record

        return results

    def search(self, ref: Reference, rows: int = 5) -> list[Record]:
        """Free-text candidates for *ref* via Crossref's bibliographic search.

        Used only for entries with no identifier to look up directly.
        ``query.bibliographic`` is a single relevance-ranked text field, not
        field-scoped filters, so title, first-author surname and container
        are concatenated into one query string; Crossref decides what is
        relevant, and the caller of this method decides what counts as a
        match, not this function.
        """
        terms: list[str] = []
        if ref.title:
            terms.append(clean(ref.title))
        if ref.authors:
            surname = ref.authors[0].literal or ref.authors[0].family
            if surname:
                terms.append(clean(surname))
        if ref.container:
            terms.append(clean(ref.container))

        query = " ".join(t for t in terms if t).strip()
        if not query:
            return []

        url = (
            f"{_API_ROOT}/works?rows={rows}&select={_SELECT_FIELDS}"
            f"&query.bibliographic={quote(query, safe='')}"
        )
        payload = self._client.get_json(url)
        if payload is None:
            return []
        items = payload.get("message", {}).get("items", [])
        return [self._record_from_work(item) for item in items]

    def _fetch_batch(self, dois: Sequence[str]) -> list[dict[str, Any]]:
        """One ``works?filter=doi:...`` request covering up to 20 DOIs."""
        filter_value = ",".join(f"doi:{quote(doi, safe='')}" for doi in dois)
        url = (
            f"{_API_ROOT}/works?rows={_BATCH_ROWS}&select={_SELECT_FIELDS}"
            f"&filter={filter_value}"
        )
        payload = self._client.get_json(url)
        if payload is None:
            # The filter route answers an empty `items` list for DOIs that
            # do not exist rather than 404ing the collection itself; None
            # here would mean Crossref changed that behaviour. Treating it
            # as "no matches" is the safe reading either way.
            return []
        message: dict[str, Any] = payload.get("message", {})
        items: list[dict[str, Any]] = message.get("items", [])
        return items

    def _fetch_one(self, doi: str) -> Record | None:
        """Single-work lookup used only as the batch-failure fallback.

        No ``select`` is appended: Crossref's ``/works/{doi}`` route rejects
        that parameter with an HTTP 400 ``parameter-not-allowed`` error
        (verified live), unlike the collection routes used elsewhere in this
        module. The full record is fetched instead; the extra fields it
        carries (affiliations, abstract, funders, ...) are simply not read.
        """
        url = f"{_API_ROOT}/works/{quote(doi, safe='')}"
        payload = self._client.get_json(url)
        if payload is None:
            return None
        return self._record_from_work(payload["message"])

    def _record_from_work(self, work: dict[str, Any]) -> Record:
        """Build one :class:`Record` from a Crossref ``work`` object."""
        authors, used_editor = _authors(work)
        retracted, retraction_kind = _retraction(work)

        raw = dict(work)
        if used_editor:
            # Visible in the report/--suggest output: a caller diffing
            # against a bibliography's `author` field needs to know these
            # names came from `editor` instead, or a correct substitution
            # (editors standing in for an edited volume's absent authors)
            # reads as an unexplained author-list mismatch.
            raw["authors_source"] = "editor"

        return Record(
            source=self.name,
            doi=normalize_doi(work.get("DOI", "")) or None,
            title=_title(work),
            authors=authors,
            years=_years(work),
            container=_first(work.get("container-title")),
            container_alternates=_rest(work.get("container-title")),
            container_short=_first(work.get("short-container-title")),
            volume=_text(work.get("volume")),
            issue=_text(work.get("issue")),
            pages=_text(work.get("page")),
            publisher=_text(work.get("publisher")),
            kind=work.get("type"),
            retracted=retracted,
            retraction_kind=retraction_kind,
            raw=raw,
        )


def _dedupe_normalized(dois: Sequence[str]) -> list[str]:
    """Normalize *dois*, dropping blanks and duplicates, order preserved.

    Callers are not guaranteed to have normalized or deduplicated their DOI
    list already, and querying the same DOI twice across two batches would
    both waste a request and let the second response silently overwrite the
    first in the result mapping.
    """
    seen: set[str] = set()
    out: list[str] = []
    for doi in dois:
        key = normalize_doi(doi)
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _authors(work: dict[str, Any]) -> tuple[list[Name], bool]:
    """Creator list for *work*: ``author``, falling back to ``editor``.

    A DOI record for an edited volume legitimately has no ``author`` array —
    that is correct Crossref data for a book with no individual authors, not
    a registry defect — so falling back to ``editor`` there is the right
    read. Returns whether the fallback fired, so the caller can note it.
    """
    people = work.get("author")
    if people:
        return _parse_creators(people), False
    people = work.get("editor") or []
    return _parse_creators(people), bool(people)


def _parse_creators(people: list[dict[str, Any]]) -> list[Name]:
    """Convert Crossref creator objects to :class:`Name`, order preserved.

    Crossref gives ``{"family", "given"}`` for a person and ``{"name"}`` for
    an organisation. Treating the latter as a person's surname would turn
    "World Health Organization" into family="World Health Organization" and
    compare it token-by-token against a real surname; ``collective=True``
    routes it instead through the collective-author rules in
    :func:`bibaudit.names.compare_author_lists`.
    """
    names: list[Name] = []
    for person in people:
        literal = person.get("name")
        family = person.get("family")
        given = person.get("given")
        if literal:
            names.append(Name(literal=clean(literal), collective=True))
        elif family or given:
            names.append(Name(family=clean(family or ""), given=clean(given or "")))
        # else: a stub entry with no name data at all (seen on some ORCID-
        # only deposits) contributes nothing comparable; skipping it avoids
        # inventing a blank author that would misalign the positional
        # comparison against every author after it.
    return names


def _title(work: dict[str, Any]) -> str | None:
    """Join Crossref's separate ``title``/``subtitle`` arrays into one title.

    Reconstructing unconditionally as ``"title: subtitle"`` duplicates the
    subtitle for the publishers that deposit the full "Title: Subtitle"
    string in ``title`` *and* repeat the subtitle in ``subtitle`` — folding
    both sides catches that duplication regardless of case or punctuation
    differences between the two copies.
    """
    titles = work.get("title") or []
    title = clean(titles[0]) if titles else ""
    subtitles = work.get("subtitle") or []
    subtitle = clean(subtitles[0]) if subtitles else ""

    if not subtitle:
        return title or None
    if not title:
        return subtitle
    if fold(subtitle) in fold(title):
        return title
    return f"{title}: {subtitle}"


def _years(work: dict[str, Any]) -> dict[str, int]:
    """Publication years by slot, only for dates *work* actually carries.

    Kept as a mapping rather than one scalar so a caller can accept a
    citation of either the print year or an earlier online-first year
    (see :attr:`bibaudit.model.Record.years`); collapsing here would throw
    that distinction away before the caller ever sees it.
    """
    years: dict[str, int] = {}
    slots = (
        ("print", "published-print"),
        ("online", "published-online"),
        ("issued", "issued"),
    )
    for slot, field_name in slots:
        parts = (work.get(field_name) or {}).get("date-parts")
        if not parts or not parts[0] or parts[0][0] is None:
            continue
        years[slot] = int(parts[0][0])
    return years


#: The ``source`` Crossref stamps on an update relation contributed by
#: Retraction Watch rather than by the publisher. Retraction Watch is a curated
#: database whose entire subject is *which paper was retracted by which notice*,
#: so on a relation the publisher has deposited in both directions at once it is
#: the party with an opinion worth having about the direction. See
#: :func:`_reciprocal_updates`.
_RETRACTION_WATCH = "retraction-watch"


def _relations(work: dict[str, Any], field_name: str) -> set[tuple[str, str]]:
    """``(folded type, normalised DOI)`` pairs in one update array."""
    out: set[tuple[str, str]] = set()
    for entry in work.get(field_name) or []:
        doi = normalize_doi(entry.get("DOI", ""))
        folded = fold(entry.get("type", ""))
        if doi and folded:
            out.add((folded, doi))
    return out


def _reciprocal_updates(work: dict[str, Any]) -> set[tuple[str, str]]:
    """Update relations *work* deposits in both directions at once.

    Crossref's model makes ``update-to`` and ``updated-by`` opposites: a record
    carrying ``update-to: retraction -> X`` **is** the notice that retracted X,
    and one carrying ``updated-by: retraction <- X`` **was** retracted by X.
    Both, about the same X and the same type, cannot be true. Elsevier deposits
    exactly that on the Lancet's Surgisphere retraction notice
    10.1016/S0140-6736(20)31324-6, verbatim in
    ``tests/data/crossref_reciprocal_retraction_notice.json``::

        update-to  [{retraction, 10.1016/s0140-6736(20)31180-6, retraction-watch}, …]
        updated-by [{retraction, 10.1016/s0140-6736(20)31180-6, publisher}]

    Read naively, the notice has itself been retracted, so any manuscript about
    the Surgisphere scandal that cites the notice — the ordinary, correct thing
    to cite — is failed ``RETRACTED``. That is a false factual claim about a
    named work, made with the tool's full authority, and it is the one output
    this project may never produce.

    **Which direction wins is decided by Retraction Watch, and by nothing else.**
    Dropping every reciprocated ``updated-by`` was the obvious fix and it is
    catastrophic: the retracted paper 10.1016/S0140-6736(20)31180-6
    (``crossref_reciprocal_retracted_paper.json``) carries the mirror image —
    ``updated-by: retraction <- 31324-6`` *and* ``update-to: retraction ->
    31324-6`` — so the naive rule clears the retracted paper itself, which is
    the worst miss this tool can make. What separates the two records is that
    Retraction Watch recorded the *notice* side on the notice
    (``update-to``/``retraction-watch``) and the *retracted* side on the paper
    (``updated-by``/``retraction-watch``). Both times, RW is right.

    So a relation is discounted only when Retraction Watch says this record is
    the notice and does **not** also say it was retracted. Everything else keeps
    the ``updated-by`` entry, which is the safe direction: a tie means the
    finding stands. 10.29328/journal.jcmhs.1001023
    (``compare_crossref_partial_retraction.json``) is the tie — its genuine
    ``partial_retraction`` points at itself in both arrays, both sourced
    ``publisher`` — and it is still reported.
    """
    reciprocal = _relations(work, "update-to") & _relations(work, "updated-by")
    if not reciprocal:
        return set()

    def witnessed(field_name: str) -> set[tuple[str, str]]:
        return {
            (fold(entry.get("type", "")), normalize_doi(entry.get("DOI", "")))
            for entry in work.get(field_name) or []
            if entry.get("source") == _RETRACTION_WATCH
        }

    return reciprocal & (witnessed("update-to") - witnessed("updated-by"))


def _retraction(work: dict[str, Any]) -> tuple[bool, str | None]:
    """Whether *work* has been retracted/withdrawn, and by what kind of notice.

    Reads ``updated-by`` only — see the module docstring for why
    ``update-to`` names the *notice*, not the retracted work, and would flag
    exactly the wrong record. ``fold()`` collapses the hyphenated, spaced and
    underscored spellings Crossref emits for the same type (``expression of
    concern`` / ``expression-of-concern`` / ``expression_of_concern``) onto
    one comparison key.

    The one exception is a relation the same record also deposits in the
    opposite direction and Retraction Watch attributes to this record as the
    notice — see :func:`_reciprocal_updates`, which is where the whole argument
    for discounting anything lives.
    """
    discounted = _reciprocal_updates(work)
    by_kind: dict[str, str] = {}
    for entry in work.get("updated-by") or []:
        raw_type = entry.get("type", "")
        folded = fold(raw_type)
        if (folded, normalize_doi(entry.get("DOI", ""))) in discounted:
            continue
        if folded in _RETRACTION_PRIORITY:
            by_kind.setdefault(folded, clean(raw_type))

    for folded in _RETRACTION_PRIORITY:
        if folded in by_kind:
            return True, by_kind[folded]
    return False, None


def _first(values: list[Any] | None) -> str | None:
    """First cleaned string in a Crossref array field, or ``None`` if empty."""
    if not values:
        return None
    text = clean(values[0])
    return text or None


def _rest(values: list[Any] | None) -> list[str]:
    """Everything after the first, cleaned, blanks dropped.

    ``container-title`` is an array and for a book chapter it holds two real
    titles: the series and the volume. 10.1007/978-1-59745-423-0_7 deposits
    ``["Methods in Molecular Biology", "Methods in Biobanking"]``, and the
    ``@inbook`` entry citing it holds the second — which is not a disagreement,
    it is the other name Crossref itself gives the same container. Keeping only
    element 0 discarded the value that matched. See
    :attr:`bibaudit.model.Record.container_alternates`.
    """
    if not values:
        return []
    return [text for text in (clean(value) for value in values[1:]) if text]


def _text(value: Any) -> str | None:
    """Cleaned scalar field, or ``None`` in place of an empty string.

    ``Record``'s scalar fields are ``str | None``; leaving a field as ``""``
    instead of ``None`` would make "Crossref has this field and it is blank"
    indistinguishable from "Crossref never sent this field" everywhere else
    in the tool that checks ``if record.volume:``.
    """
    text = clean(value)
    return text or None
