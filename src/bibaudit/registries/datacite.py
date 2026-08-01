"""DataCite REST API client.

Used as the fallback for DOIs Crossref does not register: arXiv, Zenodo,
figshare and most other data- or software-repository deposits mint DOIs
through DataCite rather than Crossref, so a resolver that only tried
Crossref would report every such citation as unresolvable.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping, Sequence

from ..model import Name, Record
from ..names import parse_name
from ..normalize import clean, fold, normalize_doi, parse_year
from .http import Client

__all__ = ["DataCite"]

_DOIS_URL = "https://api.datacite.org/dois/"


def _select_title(titles: Sequence[object]) -> str | None:
    """Pick the primary title out of DataCite's ``titles`` array.

    DataCite allows several titles per DOI, distinguished by ``titleType``
    (``Subtitle``, ``AlternativeTitle``, ``TranslatedTitle``, ...); the main
    title is the one entry that carries no ``titleType`` at all. Taking
    ``titles[0]`` unconditionally would sometimes pick a subtitle-tagged
    fragment instead of the work's actual title.

    Typed ``object`` per entry, not ``Mapping``: this is API response data,
    not internally constructed data, so the per-entry ``isinstance`` guards
    below are a real check against a real possibility, not dead code — and
    typing the parameter as ``Mapping`` would make mypy treat them as
    unreachable and mask that they exist.
    """
    if not titles:
        return None
    for entry in titles:
        # A deposit is schema-validated by DataCite at submission time, but
        # this client parses whatever the API actually returns; a malformed
        # or hand-edited record could hand back a non-object entry, and
        # `.get` on that raises instead of just skipping one unusable title.
        if not isinstance(entry, Mapping):
            continue
        if not entry.get("titleType"):
            value = clean(entry.get("title"))
            if value:
                return value
    # Every entry was type-tagged (no untyped title present) — fall back to
    # the first one rather than reporting no title at all.
    first = titles[0]
    if isinstance(first, Mapping):
        value = clean(first.get("title"))
        if value:
            return value
    return None


def _parse_creator(entry: object) -> Name | None:
    """One DataCite ``creators`` entry as a :class:`Name`.

    A personal creator carries ``familyName``/``givenName``; an
    organisational one (a consortium, a repository acting as depositor)
    carries only ``name`` and must stay one literal creator rather than being
    split into words the way a plain string would be.

    Absence of ``familyName`` does *not* by itself mean "organisation".
    Verified live against DataCite: OSF preprints (10.17605/osf.io/tmej9),
    an IRIS deposit (10.25431/11380_1192850, creator "Risso, G. L.") and a
    JOSER paper (10.6092/joser_2016_07_01_p55, seven "Given Family" authors)
    all mark every creator ``nameType: "Personal"`` yet never split out
    ``familyName``/``givenName`` — real people, given only as a raw string.
    Treating that as a collective author previously misclassified them: a
    single such creator silently disabled the author-list check entirely
    (``compare_author_lists``' collective-author escape), and a creator
    among several others produced a spurious mismatch whenever their surname
    was not the literal's last token (family-first order, e.g. "Risso, G.
    L."). ``nameType == "Organizational"`` is DataCite's own, authoritative
    signal for the true collective case (checked first, because
    ``names.parse_name`` alone would mis-split an organisation like
    "GBIF.org User" into a person); everything else lacking ``familyName``
    is handed to ``names.parse_name``, the same Family,Given / Given Family /
    collective-marker parser already trusted for bibliography authors.
    """
    if not isinstance(entry, Mapping):
        # As in `_select_title`: a malformed entry in an otherwise-valid
        # response should drop that one creator, not crash the whole lookup.
        return None
    family = clean(entry.get("familyName") or "")
    if family:
        return Name(family=family, given=clean(entry.get("givenName") or ""))

    literal = clean(entry.get("name") or "")
    if not literal:
        return None

    if fold(entry.get("nameType") or "") == "organizational":
        return Name(literal=literal, collective=True)

    return parse_name(literal)


def _authors_from(creators: Sequence[object]) -> list[Name]:
    parsed = (_parse_creator(entry) for entry in creators)
    return [name for name in parsed if name is not None]


class DataCite:
    """DataCite metadata lookup, one DOI at a time.

    DataCite's search API can filter by a query string, but there is no
    DOI-list batch endpoint with Crossref's "many DOIs, one request, results
    keyed back to each" semantics; building that out of the search API
    (escaping, pagination, matching hits back to the DOIs asked for) is more
    code than the single-DOI version it would replace, for a client that is
    only ever consulted for the DOIs Crossref did not have.
    """

    name = "datacite"

    def __init__(self, client: Client) -> None:
        self._client = client

    def by_dois(self, dois: Sequence[str]) -> dict[str, Record]:
        """Look up each of *dois* in turn, keyed by normalized DOI.

        A DOI absent from the result means DataCite answered 404 for it — a
        confirmed absence. A registry outage instead raises
        :class:`~bibaudit.registries.http.Transient` (surfaced by the
        underlying ``client`` calls) rather than being caught here, so the
        caller can tell "DataCite does not have this" from "DataCite could
        not be asked" instead of a partial result silently masquerading as
        the former.
        """
        out: dict[str, Record] = {}
        seen: set[str] = set()
        for raw_doi in dois:
            doi = normalize_doi(raw_doi)
            if not doi or doi in seen:
                continue
            seen.add(doi)
            record = self._fetch_one(doi)
            if record is not None:
                out[doi] = record
        return out

    def _fetch_one(self, doi: str) -> Record | None:
        """Fetch and parse one DOI, or ``None`` on a confirmed 404."""
        url = f"{_DOIS_URL}{urllib.parse.quote(doi, safe='/')}"
        payload = self._client.get_json(url)
        if payload is None:
            return None

        data = payload.get("data") if isinstance(payload, dict) else None
        attributes = data.get("attributes") if isinstance(data, dict) else None
        if not isinstance(attributes, dict) or not attributes:
            # A 200 with a shape this client does not recognise is not a
            # fact worth reporting as "confirmed absent" or worth crashing
            # the whole batch over; treat it as nothing usable was returned.
            return None

        container_obj = attributes.get("container")
        if not isinstance(container_obj, dict):
            container_obj = {}

        years: dict[str, int] = {}
        year = parse_year(attributes.get("publicationYear"))
        if year is not None:
            years["issued"] = year

        first_page = clean(container_obj.get("firstPage")) or None
        last_page = clean(container_obj.get("lastPage")) or None
        pages = f"{first_page}-{last_page}" if first_page and last_page else first_page

        types_obj = attributes.get("types")
        resource_type_general = (
            clean(types_obj.get("resourceTypeGeneral")) or None
            if isinstance(types_obj, dict)
            else None
        )

        titles_raw = attributes.get("titles")
        titles = titles_raw if isinstance(titles_raw, list) else []

        creators_raw = attributes.get("creators")
        creators = creators_raw if isinstance(creators_raw, list) else []

        # `Record.publisher` is deliberately left unset even though every
        # DataCite response carries `attributes.publisher`, and the value is
        # still reachable through `raw` below for anyone who needs it.
        #
        # Mapping it would switch on `compare._check_scalar("publisher", ...)`
        # for every DataCite-resolved reference at once, and that check grades
        # an unexplained difference `severity="error"`, which `verdict_for`
        # turns into FIELD-MISMATCH — a member of `model.FAILING_VERDICTS`.
        # `benign.py` has no publisher rule of any kind to absorb the known
        # cases, so this would be a brand-new source of build failures, which
        # rule 3 forbids.
        #
        # The reason it would misfire is not string instability ("Springer" vs
        # "Springer Science and Business Media LLC", true as that is) but a
        # semantic mismatch: DataCite defines `publisher` as whoever "holds,
        # archives, publishes, prints, distributes, releases, issues or
        # produces" the resource, so it is routinely the repository or the
        # journal, not the publishing house BibTeX's `publisher` means. Three
        # of this module's own fixtures show it: 10.24377/dteij.article3641
        # (a journal article) publishes as "Design and Technology Education:
        # An International Journal" — the *journal title*, which would collide
        # with a correct `publisher = {Liverpool John Moores University}`;
        # 10.48550/arxiv.1706.03762 publishes as "arXiv"; 10.15468/dl.pqqnhb
        # as "The Global Biodiversity Information Facility". The second and
        # third are DataCite-only deposits whose BibTeX entries carry no
        # `publisher` at all, so they would take the other branch of
        # `_check_scalar` and turn INCOMPLETE — a whole class of preprints
        # newly flagged for a field nobody writes in a preprint entry.
        #
        # Enabling it needs all of: a `publisher` clause in `benign.py`
        # covering at least publisher-equals-container and the corporate-form
        # variants, its false-positive *and* true-positive tests in
        # `tests/test_benign.py`, and a decision on whether a publisher
        # difference deserves "error" at all. Whoever does that must also
        # handle DataCite schema 4.5, which allows `publisher` to be an object
        # (`{"name": ..., "publisherIdentifier": ...}`) rather than a string —
        # `clean()` on that dict would not raise, it would stringify, and the
        # report would print a Python repr at a researcher.
        # See tests/test_datacite.py::TestPublisherIsDeliberatelyNotMapped.
        return Record(
            source=self.name,
            doi=doi,
            title=_select_title(titles),
            authors=_authors_from(creators),
            years=years,
            container=clean(container_obj.get("title")) or None,
            volume=clean(container_obj.get("volume")) or None,
            issue=clean(container_obj.get("issue")) or None,
            pages=pages,
            # Stored as DataCite's own raw string (e.g. "JournalArticle",
            # "Dataset"), matching Record.kind's contract: normalisation onto
            # the tool's internal vocabulary happens once, at comparison time
            # (normalize_kind in compare.py), not here.
            kind=resource_type_general,
            raw=dict(attributes),
        )
