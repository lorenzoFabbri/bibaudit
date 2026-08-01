"""Multi-registry lookup for references that carry no identifier.

``audit._audit_unidentified`` used to search Crossref alone, so one registry's
silence became ``UNCONFIRMED`` — a failing verdict — on exactly the material a
human finds hardest to check by hand: a citation with no DOI to click. This
module widens *discovery* by consulting three sources and merging what they
find, in this order:

1. **Crossref**, via :meth:`~bibaudit.registries.crossref.Crossref.search`
   (``query.bibliographic``). Reused, not duplicated — see that module for the
   query-construction rules.
2. **Europe PMC** (``ebi.ac.uk/europepmc``), queried on title, first-author
   surname and year. Genuinely independent curation, and it indexes material
   PubMed does not: preprints, some grey literature, agency reports.
3. **OpenAlex** (``api.openalex.org``), queried on title alone. **OpenAlex
   largely re-crawls Crossref's own deposits, so an OpenAlex hit is NOT
   independent corroboration of a Crossref hit** — it is the same metadata
   read a second time, not a second opinion. It is included only to widen
   *discovery* for works Crossref's own relevance ranking places outside the
   top few rows of a bibliographic-text query, never to confirm a match on its
   own. This mirrors why OpenAlex, Semantic Scholar and Unpaywall are absent
   from the *identified* (DOI-bearing) resolution path entirely — see
   ``audit.py``'s module docstring — except that here OpenAlex earns a narrow
   role Crossref's own search sometimes misses: finding the candidate at all.

   OpenAlex's basic, unauthenticated API needs no API key: as documented at
   https://docs.openalex.org/how-to-use-the-api/rate-limits-and-authentication,
   anonymous requests are served at a lower priority than the "polite pool"
   (granted via a ``mailto`` query parameter or an optional ``api_key``), but
   they are served. This module makes no polite-pool claim and adds neither —
   the shared :class:`~bibaudit.registries.http.Client` already carries
   whatever ``mailto`` was configured for Crossref/NCBI in its User-Agent, and
   OpenAlex's own docs make no promise that a bare User-Agent grants the
   polite pool the way Crossref's does. Were OpenAlex ever to require a key
   for *basic* access, the correct response is to stop querying it and say so
   here, not to add a credential this project has nowhere to source from a
   user who never signed up for one.

Widening *discovery* must never widen what gets *accepted*:
:func:`~bibaudit.compare.confirm_without_id` applies the exact same bar —
title similarity plus author and year corroboration plus type compatibility —
to a candidate regardless of which of the three sources found it. Nothing in
this module inspects ``Record.source`` to decide what to accept; that
decision belongs entirely to ``compare.py``, never here.

**Failure isolation.** One source timing out must never silence the other two
— see :meth:`Search.candidates`. A source's :class:`~.http.Transient` is
caught for that source alone; the merged result reflects whatever the
*other* sources answered. Only when every enabled source failed does
:meth:`Search.candidates` itself raise :class:`~.http.Transient`, because an
empty return would otherwise be indistinguishable from "every source was
asked and none of them had this" — a fact — when it might instead mean "no
source could be reached at all" — ignorance. Collapsing those two is exactly
the mistake ``Transient`` exists to make impossible everywhere else in this
tool (see ``CLAUDE.md`` and ``registries/http.py``), and a search layer is not
an exception to it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from urllib.parse import quote

from ..model import Name, Record, Reference
from ..names import parse_name
from ..normalize import clean, normalize_doi, parse_year
from .crossref import Crossref
from .http import Client, Transient

__all__ = ["Search"]

_EUROPEPMC_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
_OPENALEX_URL = "https://api.openalex.org/works"

#: Record.source values stamped by this module's own two clients. Crossref's
#: is ``Crossref.name`` and is reused as-is.
_EUROPEPMC = "europepmc"
_OPENALEX = "openalex"


class Search:
    """Free-text candidates for an identifier-less reference, from three sources.

    See the module docstring for the order sources are queried in, why
    OpenAlex is included for discovery only, and why one source's outage never
    silences the others.
    """

    def __init__(
        self, client: Client, *, use_europepmc: bool = True, use_openalex: bool = True
    ) -> None:
        self._client = client
        self._crossref = Crossref(client)
        self._use_europepmc = use_europepmc
        self._use_openalex = use_openalex

    @property
    def sources(self) -> tuple[str, ...]:
        """Registry names this instance queries, in query order.

        Exposed so a caller building ``Result.consulted`` (see
        ``audit._audit_unidentified``) can state which registries were asked
        on an identifier-less reference's behalf without re-deriving the
        ``use_europepmc``/``use_openalex`` gating logic in a second place,
        where it could drift from what :meth:`candidates` actually does.
        """
        names = [self._crossref.name]
        if self._use_europepmc:
            names.append(_EUROPEPMC)
        if self._use_openalex:
            names.append(_OPENALEX)
        return tuple(names)

    def candidates(self, ref: Reference, rows: int = 5) -> list[Record]:
        """Candidate works for *ref*, merged across every enabled source.

        Deduplicated on normalised DOI: a work Crossref and OpenAlex both
        return is one candidate, not two, kept under whichever source
        answered first (Crossref, then Europe PMC, then OpenAlex) — the
        earlier source in that list is never the *less* trustworthy one, so
        there is no reason to prefer a later duplicate. A candidate that
        carries no DOI at all (a grey-literature Europe PMC hit) is never
        deduplicated away, for lack of anything to key it on.

        Raises :class:`~.http.Transient` only when **every** enabled source
        failed — see the module docstring. A partial failure (one source
        down, others answering) is absorbed silently: the merged list simply
        reflects less evidence, the same way an unreachable DataCite or
        PubMed degrades a DOI-based check elsewhere in this tool without
        invalidating it. What must never happen is a *total* outage reading
        as "no source found a match", which is indistinguishable in a plain
        ``list[Record]`` from a genuine, informative silence — hence the
        exception rather than a quietly empty list.
        """
        merged: list[Record] = []
        seen_dois: set[str] = set()

        def absorb(hits: Sequence[Record]) -> None:
            for record in hits:
                doi = normalize_doi(record.doi) if record.doi else ""
                if doi:
                    if doi in seen_dois:
                        continue
                    seen_dois.add(doi)
                merged.append(record)

        attempted = 0
        failed = 0

        attempted += 1
        try:
            absorb(self._crossref.search(ref, rows=rows))
        except Transient:
            failed += 1

        if self._use_europepmc:
            attempted += 1
            try:
                absorb(self._search_europepmc(ref, rows))
            except Transient:
                failed += 1

        if self._use_openalex:
            attempted += 1
            try:
                absorb(self._search_openalex(ref, rows))
            except Transient:
                failed += 1

        if failed == attempted:
            raise Transient(
                f"no search source could be reached for {ref.key!r} "
                f"({attempted} attempted, {failed} unreachable)"
            )
        return merged

    def _search_europepmc(self, ref: Reference, rows: int) -> list[Record]:
        """Europe PMC candidates for *ref*, or ``[]`` for no query / no hits.

        The query is built from title, first-author surname and year — never
        a DOI (there is none) and never the container, unlike Crossref's own
        search: the assignment that motivated this module names exactly
        those three fields, and Europe PMC's ``TITLE``/``AUTH``/``PUB_YEAR``
        field query syntax lets each be scoped precisely rather than thrown
        into one bag of free text the way ``query.bibliographic`` requires.
        """
        query = _europepmc_query(ref)
        if not query:
            return []
        url = (
            f"{_EUROPEPMC_URL}?query={quote(query, safe='')}"
            f"&format=json&resultType=core&pageSize={rows}"
        )
        payload = self._client.get_json(url)
        if payload is None:
            return []
        result_list = payload.get("resultList")
        results = result_list.get("result") if isinstance(result_list, dict) else None
        if not isinstance(results, list):
            return []
        return [_record_from_europepmc(item) for item in results if isinstance(item, dict)]

    def _search_openalex(self, ref: Reference, rows: int) -> list[Record]:
        """OpenAlex candidates for *ref* by title, or ``[]`` for no title / no hits.

        Title only, per the assignment's endpoint shape
        (``filter=title.search:...``) — OpenAlex is here for discovery, not
        confirmation (see the module docstring), and a wider query would only
        change *which* re-crawl of Crossref's own data comes back, not
        whether it counts as corroboration.
        """
        title = clean(ref.title)
        if not title:
            return []
        url = f"{_OPENALEX_URL}?filter=title.search:{quote(title, safe='')}&per_page={rows}"
        payload = self._client.get_json(url)
        if payload is None:
            return []
        results = payload.get("results")
        if not isinstance(results, list):
            return []
        return [_record_from_openalex(item) for item in results if isinstance(item, dict)]


def _europepmc_query(ref: Reference) -> str:
    """Europe PMC field-scoped query: title, first-author surname, year."""
    clauses: list[str] = []
    title = clean(ref.title)
    if title:
        clauses.append(f'TITLE:"{title}"')
    if ref.authors:
        surname = clean(ref.authors[0].literal or ref.authors[0].family)
        if surname:
            clauses.append(f'AUTH:"{surname}"')
    if ref.year:
        clauses.append(f"PUB_YEAR:{ref.year}")
    return " AND ".join(clauses)


def _record_from_europepmc(item: dict[str, Any]) -> Record:
    """Build a :class:`Record` from one Europe PMC ``resultType=core`` hit."""
    doi = normalize_doi(item.get("doi") or "") or None
    title = clean(item.get("title")) or None

    years: dict[str, int] = {}
    year = parse_year(item.get("pubYear"))
    if year is not None:
        years["issued"] = year

    journal_info = item.get("journalInfo")
    journal_info = journal_info if isinstance(journal_info, dict) else {}
    journal = journal_info.get("journal")
    journal = journal if isinstance(journal, dict) else {}

    kind = None
    pub_type_list = item.get("pubTypeList")
    if isinstance(pub_type_list, dict):
        types = pub_type_list.get("pubType")
        if isinstance(types, list) and types:
            kind = clean(types[0]) or None

    return Record(
        source=_EUROPEPMC,
        doi=doi,
        title=title,
        authors=_europepmc_authors(item.get("authorList")),
        years=years,
        container=clean(journal.get("title")) or None,
        volume=clean(journal_info.get("volume")) or None,
        issue=clean(journal_info.get("issue")) or None,
        pages=clean(item.get("pageInfo")) or None,
        kind=kind,
        raw=dict(item),
    )


def _europepmc_authors(author_list: object) -> list[Name]:
    """``authorList.author`` -> :class:`Name` list, order preserved."""
    if not isinstance(author_list, dict):
        return []
    entries = author_list.get("author")
    if not isinstance(entries, list):
        return []
    names = (_europepmc_author(entry) for entry in entries)
    return [name for name in names if name is not None]


def _europepmc_author(entry: object) -> Name | None:
    """One Europe PMC ``authorList.author`` entry as a :class:`Name`.

    ``resultType=core`` normally supplies ``lastName``/``firstName``
    separately, which is used directly when present. The ``fullName``
    fallback below exists for the entries that carry only that — and it is
    formatted "Surname Initials" (``"Smith JA"``), the same order MEDLINE's
    abbreviated ``AU`` tag uses (see ``registries/pubmed.py``'s
    ``_parse_au_fallback``) and the *opposite* of the comma-less "Given
    Family" order :func:`~bibaudit.names.parse_name` assumes for BibTeX
    input. Handing "Smith JA" to that parser unmodified would swap surname
    and given name.
    """
    if not isinstance(entry, dict):
        return None

    collective = clean(entry.get("collectiveName") or "")
    if collective:
        return Name(literal=collective, collective=True)

    last = clean(entry.get("lastName") or "")
    if last:
        given = clean(entry.get("firstName") or entry.get("initials") or "")
        return Name(family=last, given=given)

    full = clean(entry.get("fullName") or "")
    if not full:
        return None
    tokens = full.split()
    if len(tokens) < 2:
        return Name(family=full)
    surname, initials = " ".join(tokens[:-1]), tokens[-1]
    return Name(family=surname, given=initials)


def _record_from_openalex(work: dict[str, Any]) -> Record:
    """Build a :class:`Record` from one OpenAlex ``works`` entry."""
    doi = normalize_doi(work.get("doi") or "") or None
    title = clean(work.get("title") or work.get("display_name")) or None

    years: dict[str, int] = {}
    year = parse_year(work.get("publication_year"))
    if year is not None:
        years["issued"] = year

    primary_location = work.get("primary_location")
    source = primary_location.get("source") if isinstance(primary_location, dict) else None
    container = clean(source.get("display_name")) if isinstance(source, dict) else ""

    biblio = work.get("biblio")
    biblio = biblio if isinstance(biblio, dict) else {}
    first_page = clean(biblio.get("first_page")) or None
    last_page = clean(biblio.get("last_page")) or None
    pages = f"{first_page}-{last_page}" if first_page and last_page else first_page

    return Record(
        source=_OPENALEX,
        doi=doi,
        title=title,
        authors=_openalex_authors(work.get("authorships")),
        years=years,
        container=container or None,
        volume=clean(biblio.get("volume")) or None,
        issue=clean(biblio.get("issue")) or None,
        pages=pages,
        # OpenAlex's own type string ("article", "book-chapter", "preprint",
        # ...); normalisation onto this tool's internal vocabulary happens
        # once, at comparison time (normalize_kind in compare.py), not here —
        # the same contract every other registry client in this package keeps.
        kind=work.get("type"),
        raw=dict(work),
    )


def _openalex_authors(authorships: object) -> list[Name]:
    """``authorships[].author.display_name`` -> :class:`Name` list, order preserved.

    OpenAlex's ``display_name`` is written "Given ... Family" — the same
    comma-less order :func:`~bibaudit.names.parse_name` assumes for BibTeX
    input — so it is handed to that parser directly, including its own
    collective-author detection (an OpenAlex consortium deposit reads no
    differently from one written by hand in a ``.bib``).
    """
    if not isinstance(authorships, list):
        return []
    names: list[Name] = []
    for entry in authorships:
        if not isinstance(entry, dict):
            continue
        author = entry.get("author")
        display_name = author.get("display_name") if isinstance(author, dict) else None
        display_name = clean(display_name or entry.get("raw_author_name") or "")
        if display_name:
            names.append(parse_name(display_name))
    return names
