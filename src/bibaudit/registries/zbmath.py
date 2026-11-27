"""zbMATH Open, consulted for one thing: who wrote a work Crossref holds.

zbMATH Open is curated by FIZ Karlsruhe from the publishers' tables of contents
and its own editors' reviews, separately from Crossref's deposits, so a byline
it carries is a second witness in the sense ``CLAUDE.md``'s "Adding a registry"
test asks for. It indexes mathematics and statistics, which is the literature
whose older Crossref deposits most often stop after the first creator.

It is read for the byline and nothing else. Its record never becomes the one an
entry is compared against, it never corroborates a title or a year, and it
carries no retraction linkage; ``compare`` holds those three rules, and this
client builds a :class:`~bibaudit.model.Record` carrying the DOI and the
authors alone so that nothing else could be read from it by accident.

The query is ``en:<doi>``, one request per DOI. The ``doi:`` field answers 404
for a DOI containing parentheses that the ``en:`` field finds
(10.1016/S0019-9958(65)90241-X), and an ``|`` of several ``en:`` terms in which
one DOI carries parentheses returned only that one work on 2026-11-27, so
batching would turn a held record into an absent one. The API documents no
request ceiling; its terms ask for a reasonable rate, which the shared
:class:`~bibaudit.registries.http.Client` throttle already keeps.

Only a result whose ``links`` name the DOI itself counts, compared after
:func:`~bibaudit.normalize.normalize_doi`. Where a publisher's licence keeps a
name out of the API, zbMATH replaces it with :data:`_WITHHELD`; such a name is
dropped, and a record whose every name is withheld has no byline at all.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping

from ..model import Name, Record
from ..names import parse_name
from ..normalize import clean, normalize_doi
from .http import Client

__all__ = ["ZbMath"]

_SEARCH_URL = "https://api.zbmath.org/v1/document/_search"

#: Results asked for per query. ``en:`` matches external identifiers exactly, so
#: one result is the ordinary answer; the margin is for a DOI also recorded on
#: a second document, such as a reprint.
_RESULTS_PER_PAGE = 10

#: The text the API puts in place of content it may not redistribute, names
#: included: 4 of the 52 records it held for 101 DOIs sampled on 2026-11-27.
_WITHHELD = "zbMATH Open Web Interface contents unavailable due to conflicting licenses."


def search_url(doi: str) -> str:
    """The request :meth:`ZbMath.by_doi` issues for *doi*."""
    query = urllib.parse.urlencode(
        {"search_string": f"en:{doi}", "results_per_page": _RESULTS_PER_PAGE}
    )
    return f"{_SEARCH_URL}?{query}"


def _names_doi(document: Mapping[str, object], doi: str) -> bool:
    links = document.get("links")
    return isinstance(links, list) and any(
        isinstance(link, Mapping)
        and link.get("type") == "doi"
        and normalize_doi(link.get("identifier")) == doi
        for link in links
    )


def _authors(document: Mapping[str, object]) -> list[Name]:
    contributors = document.get("contributors")
    entries = contributors.get("authors") if isinstance(contributors, Mapping) else None
    names: list[Name] = []
    for entry in entries if isinstance(entries, list) else []:
        text = clean(entry.get("name")) if isinstance(entry, Mapping) else ""
        if text and text != _WITHHELD:
            names.append(parse_name(text))
    return names


class ZbMath:
    """Byline lookups against the zbMATH Open REST API."""

    name = "zbmath"

    def __init__(self, client: Client) -> None:
        self._client = client

    def by_doi(self, raw_doi: str) -> Record | None:
        """zbMATH's byline for *raw_doi*, or ``None`` where it holds none.

        ``None`` covers a 404, a result naming some other DOI, and a record
        whose every name is withheld: in each, zbMATH says nothing about who
        wrote the work. A failure to reach it raises
        :class:`~bibaudit.registries.http.Transient` from the client.
        """
        doi = normalize_doi(raw_doi)
        if not doi:
            return None
        payload = self._client.get_json(search_url(doi))
        results = payload.get("result") if isinstance(payload, Mapping) else None
        for document in results if isinstance(results, list) else []:
            if isinstance(document, Mapping) and _names_doi(document, doi):
                authors = _authors(document)
                return Record(source="zbmath", doi=doi, authors=authors) if authors else None
        return None
