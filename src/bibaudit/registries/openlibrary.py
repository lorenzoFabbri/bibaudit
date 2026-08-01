"""Open Library ISBN lookup — the registry consulted for books.

Every other registry in this tool is reached by DOI. Most books never had one
minted for them at all, so without this module every book in a bibliography
falls through to ``UNCONFIRMED`` — not because anything is wrong with the
entry, but because nothing was ever asked about it. On a real Zotero library
that is dozens of entries: a wall of false alarms on exactly the material that
is hardest to verify by hand, which is the fastest way to make a reader stop
reading the report at all.

Two lookups, matching the two shapes a book entry arrives in:

``by_isbns``
    The ordinary case: the entry carries an ISBN, and it is looked up the same
    way a DOI is — an identifier presented to the registry, a record or a
    confirmed absence back.
``search``
    The entry carries no identifier at all. Free-text title/author search,
    used only as a candidate source for
    :func:`~bibaudit.compare.confirm_without_id`, exactly as
    :meth:`~bibaudit.registries.crossref.Crossref.search` is for articles.

**Open Library's data is crowd-sourced and noticeably patchier than
Crossref's.** A great many records carry a title and nothing else — no
authors, no publication date, no page count — because the record was created
from a library catalogue entry or a cover scan and never enriched. That is not
this client's defect to fix, but it is a real difference in what a "match"
means here versus at Crossref: a title-only record is not evidence that a
book is the *right* book, only that some book with that title exists. The
corroboration rule in :func:`~bibaudit.compare.confirm_without_id` accounts
for this — a candidate carrying neither an author nor a year cannot confirm
an identifier-less entry on title alone — but any caller building its own
matching logic on top of :meth:`OpenLibrary.search` has to remember the
records it returns can be this thin.

Integration note
-----------------
Like every other registry module, all network access goes through the
injected :class:`~bibaudit.registries.http.Client`. The batch endpoint
(``/api/books``) never 404s the collection — an ISBN nobody has simply does
not appear as a key in the response object, the same way Crossref's
``works?filter=doi:...`` answers an empty ``items`` list rather than 404ing.
A confirmed-absent *individual* ISBN is therefore represented here as "not a
key in the returned mapping", not as ``None`` from ``get_json``.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Mapping, Sequence
from typing import Any

from ..model import Name, Record, Reference
from ..names import parse_name
from ..normalize import clean, fold, parse_year
from .http import Client

__all__ = ["OpenLibrary", "normalize_isbn13"]

_API_ROOT = "https://openlibrary.org"
_BOOKS_URL = f"{_API_ROOT}/api/books"
_SEARCH_URL = f"{_API_ROOT}/search.json"

#: ISBNs per ``bibkeys=`` request. Open Library documents no hard ceiling for
#: this endpoint, unlike Crossref's ``filter=`` route; 50 is a conservative
#: margin that keeps the request URL well inside the length every common HTTP
#: server and proxy accepts, while still turning a large library's worth of
#: ISBNs into a handful of requests rather than one per book.
_BATCH_SIZE = 50

#: Characters :func:`normalize_isbn13` strips before validating. ISBNs are
#: stored with hyphens in every common style guide (``978-0-13-468599-1``) and
#: sometimes with plain spaces in hand-typed bibliographies; neither carries
#: meaning for the check digit.
_STRIP_RE = re.compile(r"[\s-]+")


def _clean_isbn(raw: object) -> str:
    """*raw* with hyphens/spaces removed and the check character upper-cased.

    Upper-casing is only meaningful for the ISBN-10 check digit, which is the
    single letter ``X`` (standing for the value 10) — never for the ten plain
    digits around it, so upper-casing the whole cleaned string is a no-op
    everywhere else and correct exactly where it matters.
    """
    return _STRIP_RE.sub("", clean(raw)).upper()


def _is_valid_isbn10(text: str) -> bool:
    """True if *text* is exactly 10 characters and its check digit holds.

    The ISBN-10 check digit is a weighted sum, digits weighted 10 down to 2,
    plus the check character itself (0-9, or ``X`` standing for 10), that must
    be divisible by 11. This is the standard defined by ISO 2108, not a
    registry-specific rule.
    """
    if len(text) != 10 or not text[:9].isdigit():
        return False
    if not (text[9].isdigit() or text[9] == "X"):
        return False
    check = 10 if text[9] == "X" else int(text[9])
    total = sum((10 - i) * int(digit) for i, digit in enumerate(text[:9])) + check
    return total % 11 == 0


def _is_valid_isbn13(text: str) -> bool:
    """True if *text* is exactly 13 digits and its EAN-13 check digit holds.

    Alternating weights of 1 and 3 must sum to a multiple of 10 — the same
    checksum every EAN-13 barcode uses, ISBNs included since the 2007
    transition.
    """
    if len(text) != 13 or not text.isdigit():
        return False
    total = sum(int(digit) * (1 if i % 2 == 0 else 3) for i, digit in enumerate(text))
    return total % 10 == 0


def _isbn10_to_13(isbn10: str) -> str:
    """Convert a **validated** ISBN-10 to its ISBN-13 form.

    The rule is fixed by ISO 2108: replace the ISBN-10 check digit with the
    ``978`` Bookland prefix in front of the first nine digits, then compute a
    fresh EAN-13 check digit for the result — the ISBN-10 check digit itself
    carries no information forward, it is simply dropped.

    Never call this on an unvalidated string: a check-digit failure has
    already been decided by the caller, and computing a "converted" ISBN-13
    for a string that was never a real ISBN-10 would manufacture an
    identifier that looks legitimate and resolves nowhere for a reason the
    report could not explain.
    """
    core = "978" + isbn10[:9]
    total = sum(int(digit) * (1 if i % 2 == 0 else 3) for i, digit in enumerate(core))
    check = (10 - (total % 10)) % 10
    return f"{core}{check}"


def normalize_isbn13(raw: object) -> str | None:
    """Normalise *raw* to a validated ISBN-13, or ``None`` if it cannot be one.

    Hyphens and spaces are stripped and a trailing ``x`` upper-cased first, so
    ``"0-201-63361-2"`` and ``"0201633612"`` normalise identically. A 10-digit
    string is converted per :func:`_isbn10_to_13`; a 13-digit string is
    returned as-is. Either length is validated against its own check digit
    **before** anything is accepted — an ISBN whose check digit fails is not a
    typo this client can guess its way past, and converting a broken ISBN-10
    would silently manufacture a plausible-looking ISBN-13 for a string that
    was never valid in the first place.

    ``None`` covers three situations a caller must not conflate: nothing was
    given, the string is not 10 or 13 characters once cleaned, or the check
    digit is wrong. All three mean the same thing to this function — "not
    usable as a lookup key" — but they are *not* the same fact about a
    bibliography. A caller that already knows an ISBN was present (``ref.isbn``
    is truthy) and gets ``None`` back knows specifically that the identifier
    is malformed, and CLAUDE.md's rule that "a 404 is a fact, ignorance is
    ignorance" has a sibling here: a malformed identifier is neither — it is
    a defect in the bibliography's own data, provable without ever asking a
    registry, and reporting it as "Open Library does not have this book"
    would claim evidence that was never gathered.
    """
    text = _clean_isbn(raw)
    if len(text) == 13:
        return text if _is_valid_isbn13(text) else None
    if len(text) == 10:
        return _isbn10_to_13(text) if _is_valid_isbn10(text) else None
    return None


def _dedupe_normalized(isbns: Sequence[str]) -> list[str]:
    """Normalise *isbns*, dropping malformed values and duplicates, order kept.

    A malformed ISBN is silently excluded from the batch here rather than
    sent to Open Library: it can never resolve to a real book, and sending it
    anyway would waste a request and, worse, come back as "not a key in the
    response" — indistinguishable, at this layer, from a well-formed ISBN
    Open Library simply does not hold. A caller that needs to tell "malformed"
    apart from "confirmed absent" has to make that check itself, with
    :func:`normalize_isbn13`, before calling :meth:`OpenLibrary.by_isbns` —
    exactly as :mod:`bibaudit.audit` does.
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in isbns:
        isbn = normalize_isbn13(raw)
        if isbn and isbn not in seen:
            seen.add(isbn)
            out.append(isbn)
    return out


def _title(entry: Mapping[str, Any]) -> str | None:
    """Join Open Library's ``title``/``subtitle`` strings into one title.

    Mirrors :func:`bibaudit.registries.crossref._title`'s join rule so the two
    registries state a chapter or book's title the same way: if the subtitle
    is already folded into the title (some catalogue entries duplicate it),
    the title alone is kept rather than doubling it.
    """
    title = clean(entry.get("title"))
    subtitle = clean(entry.get("subtitle"))
    if not subtitle:
        return title or None
    if not title:
        return subtitle
    if fold(subtitle) in fold(title):
        return title
    return f"{title}: {subtitle}"


def _authors(entry: Mapping[str, Any]) -> list[Name]:
    """Open Library's ``authors`` array (``[{"name": "Firstname Lastname"}]``).

    Parsed with :func:`~bibaudit.names.parse_name`, which already treats a
    name containing a corporate marker word ("Institute", "Committee",
    "Organization", ...) as a single collective creator rather than splitting
    it — the same rule that keeps "The Endogenous Hormones and Breast Cancer
    Collaborative Group" one author instead of two. Open Library credits a
    publisher-as-author on some institutional reports the same way, and
    routing every entry through this one parser is what keeps that handling
    in one place rather than reinvented per registry.
    """
    raw = entry.get("authors")
    if not isinstance(raw, list):
        return []
    names: list[Name] = []
    for author in raw:
        if not isinstance(author, Mapping):
            continue
        literal = clean(author.get("name"))
        if literal:
            names.append(parse_name(literal))
    return names


def _publisher(entry: Mapping[str, Any]) -> str | None:
    """The first entry of Open Library's ``publishers`` array, if any.

    Only the first is kept, matching how :mod:`bibaudit.model` treats every
    other single-valued scalar field: a book co-published by two imprints is
    real, but this tool has no field for "one of several correct publishers"
    and inventing one for a case this narrow is not worth the complexity.
    """
    publishers = entry.get("publishers")
    if not isinstance(publishers, list) or not publishers:
        return None
    first = publishers[0]
    if not isinstance(first, Mapping):
        return None
    value = clean(first.get("name"))
    return value or None


def _years(entry: Mapping[str, Any]) -> dict[str, int]:
    """``publish_date`` as a single ``"issued"`` year, when parseable.

    Open Library's ``publish_date`` is free text ("1994", "March 1994",
    "1994-03-15") with no separate print/online distinction to preserve, so
    unlike :mod:`~bibaudit.registries.crossref` there is only ever one slot to
    fill.
    """
    year = parse_year(entry.get("publish_date"))
    return {"issued": year} if year is not None else {}


def _pages(entry: Mapping[str, Any]) -> str | None:
    """Open Library's ``number_of_pages`` as a comparable string, if present.

    This is the book's total length, not "the opening page of a citation
    inside a larger work" — the meaning :attr:`~bibaudit.model.Record.pages`
    carries for every other registry in this tool. There is no better field
    to put it in: :class:`~bibaudit.model.Record` has no separate "extent"
    slot, and the alternative is to drop the value Open Library actually
    supplies. What makes reusing ``pages`` safe is entirely on the
    :mod:`~bibaudit.compare` side — a bibliography almost never states a
    book's total length in its own ``pages`` field, and
    ``compare._check_scalar``/``_check_pages`` must not (and, since that
    module was updated alongside this one, no longer does) invent a "missing"
    warning for a book from this value. See ``docs/registry-artifacts.md``,
    "Open Library's number_of_pages is not a citation locator".
    """
    value = entry.get("number_of_pages")
    if isinstance(value, int) and value > 0:
        return str(value)
    return None


def _record_from_book(entry: Mapping[str, Any]) -> Record:
    """Build one :class:`Record` from one ``jscmd=data`` book object."""
    return Record(
        source="openlibrary",
        title=_title(entry),
        authors=_authors(entry),
        years=_years(entry),
        publisher=_publisher(entry),
        pages=_pages(entry),
        kind="book",
        raw=dict(entry),
    )


def _record_from_search_doc(doc: Mapping[str, Any]) -> Record:
    """Build one :class:`Record` from one ``search.json`` result document.

    ``search.json``'s documents are a *different* shape from the ``jscmd=data``
    objects :meth:`OpenLibrary.by_isbns` reads — author names arrive as a flat
    ``author_name`` list of strings rather than ``{"name": ...}`` objects, the
    year is already an integer in ``first_publish_year`` rather than free text
    in ``publish_date``, and pagination is a *median* across editions
    (``number_of_pages_median``) rather than one edition's own count. Both are
    handled here rather than coerced into one shared parser, because
    pretending the two endpoints agree on field names would be the kind of
    silent guess this tool exists to avoid making.
    """
    title = clean(doc.get("title"))
    subtitle = clean(doc.get("subtitle"))
    if title and subtitle and fold(subtitle) not in fold(title):
        title = f"{title}: {subtitle}"

    author_names = doc.get("author_name")
    authors = (
        [parse_name(clean(name)) for name in author_names if clean(name)]
        if isinstance(author_names, list)
        else []
    )

    years: dict[str, int] = {}
    first_year = doc.get("first_publish_year")
    if isinstance(first_year, int):
        years["issued"] = first_year

    publishers = doc.get("publisher")
    publisher = (
        clean(publishers[0])
        if isinstance(publishers, list) and publishers and clean(publishers[0])
        else None
    )

    pages_median = doc.get("number_of_pages_median")
    pages = str(pages_median) if isinstance(pages_median, int) and pages_median > 0 else None

    return Record(
        source="openlibrary",
        title=title or None,
        authors=authors,
        years=years,
        publisher=publisher,
        pages=pages,
        kind="book",
        raw=dict(doc),
    )


class OpenLibrary:
    """Client for Open Library's Books and Search APIs (``openlibrary.org``)."""

    name = "openlibrary"

    def __init__(self, client: Client) -> None:
        """Wrap *client* for caching, rate limiting and retry behaviour.

        Open Library requests never bypass *client*: this class only builds
        URLs and shapes responses into :class:`Record`.
        """
        self._client = client

    def by_isbns(self, isbns: Sequence[str]) -> dict[str, Record]:
        """Resolve *isbns* to :class:`Record` objects, keyed by normalised ISBN-13.

        An ISBN absent from the returned mapping means Open Library does not
        have it, *or* it was malformed and never sent — see
        :func:`_dedupe_normalized`. If the registry cannot be reached at all,
        :class:`~bibaudit.registries.http.Transient` propagates from the
        underlying ``client`` call rather than being caught here, so an
        outage is never reported as a pile of missing books.
        """
        normalized = _dedupe_normalized(isbns)
        results: dict[str, Record] = {}

        for start in range(0, len(normalized), _BATCH_SIZE):
            batch = normalized[start : start + _BATCH_SIZE]
            bibkeys = ",".join(f"ISBN:{isbn}" for isbn in batch)
            url = f"{_BOOKS_URL}?bibkeys={urllib.parse.quote(bibkeys, safe=',:')}&format=json&jscmd=data"
            payload = self._client.get_json(url)
            if not payload:
                # The batch endpoint answers `{}` for a batch where nothing
                # matched rather than 404ing the collection — see the module
                # docstring. `None` (a genuine 404, should Open Library ever
                # start sending one) is handled identically: either way,
                # nothing in this batch resolved.
                continue
            for isbn in batch:
                entry = payload.get(f"ISBN:{isbn}")
                if isinstance(entry, dict) and entry:
                    results[isbn] = _record_from_book(entry)

        return results

    def search(self, ref: Reference, rows: int = 5) -> list[Record]:
        """Free-text candidates for *ref* via Open Library's search API.

        Used only for book/chapter entries with no identifier to look up
        directly, exactly as :meth:`~bibaudit.registries.crossref.Crossref.search`
        is for articles. Unlike Crossref's single ``query.bibliographic``
        field, Open Library's search takes title and author as separate
        parameters, so both are sent when available rather than concatenated
        into one string.

        The records this returns can be thin — see the module docstring — so
        the caller must run them through
        :func:`~bibaudit.compare.confirm_without_id`, never adopt the
        top-ranked hit directly.
        """
        params: dict[str, str] = {}
        if ref.title:
            params["title"] = clean(ref.title)
        if ref.authors:
            surname = ref.authors[0].literal or ref.authors[0].family
            if surname:
                params["author"] = clean(surname)
        if not params:
            return []
        params["limit"] = str(rows)

        url = f"{_SEARCH_URL}?{urllib.parse.urlencode(params)}"
        payload = self._client.get_json(url)
        if not payload:
            return []
        docs = payload.get("docs")
        if not isinstance(docs, list):
            return []
        return [
            _record_from_search_doc(doc)
            for doc in docs[:rows]
            if isinstance(doc, dict)
        ]
