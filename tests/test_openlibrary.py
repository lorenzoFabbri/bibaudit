"""Open Library registry client.

Offline only. :class:`_StubClient` stands in for
:class:`~bibaudit.registries.http.Client`. An autouse tripwire replaces
``urllib.request.urlopen`` for the whole module, so a client that opened its
own socket fails here instead of quietly working on a maintainer's laptop and
failing in CI.

Unlike ``tests/test_datacite.py`` and ``tests/test_crossref.py``, the JSON
payloads below are **not** recordings of a live response — this environment
has no network access, so nothing could be fetched to record. They are
hand-built to match the shape Open Library's own developer documentation
publishes for the Books API (``openlibrary.org/dev/docs/api/books``) and for
``search.json``, trimmed to the fields ``registries/openlibrary.py`` actually
reads. Anyone who can reach the network can confirm the shape directly:

    curl 'https://openlibrary.org/api/books?bibkeys=ISBN:0201633612&format=json&jscmd=data'
    curl 'https://openlibrary.org/search.json?title=Design+Patterns&limit=1'

The ISBN arithmetic is not open to that same doubt: ISBN-10/13 check digits
and the ISBN-10-to-13 conversion are ISO 2108, not a registry's own choice,
and every example below is verified against the standard algorithm, not
against Open Library. ``013000006X`` is a constructed example (no claim it is
a real book); ``0306406152``/``9780306406157`` and ``0201633612``/
``9780201633610`` are the ISBNs of two real, widely-cited books (SICP and
*Design Patterns*), chosen because their arithmetic is easy for a reader to
re-check by hand against a published reference.
"""

from __future__ import annotations

import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

import pytest

from bibaudit.compare import Thresholds, confirm_without_id
from bibaudit.model import Reference
from bibaudit.registries.http import Transient
from bibaudit.registries.openlibrary import OpenLibrary, normalize_isbn13

# SICP, real book: Abelson & Sussman, "Structure and Interpretation of
# Computer Programs".
_SICP_10 = "0-306-40615-2"
_SICP_13 = "9780306406157"

# "Design Patterns" (Gamma, Helm, Johnson, Vlissides), the worked example in
# Open Library's own Books API documentation.
_DESIGN_PATTERNS_10 = "0201633612"
_DESIGN_PATTERNS_13 = "9780201633610"

# Constructed to exercise the ISBN-10 'X' check digit; not a claim this is a
# real published book (see the module docstring's verified arithmetic).
_X_CHECK_DIGIT_10 = "013000006X"
_X_CHECK_DIGIT_13 = "9780130000064"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn any real HTTP call made from this module into a hard failure.

    ``openlibrary.py`` is only allowed to reach the network through the
    injected :class:`~bibaudit.registries.http.Client`, because that is what
    supplies the on-disk cache, the per-host throttle, and the "404 is a fact,
    a timeout is ignorance" distinction. A client that called ``urlopen``
    directly would satisfy every other assertion in this file while bypassing
    all three, and the default test run would start needing the internet.
    """

    def _tripwire(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "openlibrary.py opened a socket instead of going through the injected Client"
        )

    monkeypatch.setattr(urllib.request, "urlopen", _tripwire)


class _StubClient:
    """Stands in for :class:`~bibaudit.registries.http.Client`.

    *responder* maps a request URL to what ``get_json`` should hand back: a
    decoded payload, ``None`` for a confirmed HTTP 404, or a raised
    :class:`~bibaudit.registries.http.Transient` for an outage. Every URL
    asked for is recorded in :attr:`urls`.

    Any attribute other than ``get_json`` raises rather than being invented on
    demand — the point of injecting a client is that it is the *only* door to
    the network, and a stub that grew a ``.session`` the moment production
    code asked for one would let a second, uncached, unthrottled transport
    slip in unnoticed.
    """

    def __init__(self, responder: Callable[[str], dict[str, Any] | None]) -> None:
        self._responder = responder
        self.urls: list[str] = []

    def get_json(
        self,
        url: str,
        *,
        cache_key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        self.urls.append(url)
        return self._responder(url)

    def __getattr__(self, attr: str) -> Any:
        raise AssertionError(
            f"openlibrary.py reached for Client.{attr}, which this stub deliberately does not model"
        )


def _client(payload: dict[str, Any] | Exception | None) -> _StubClient:
    """A stub that answers every request the same way, regardless of URL."""

    def responder(url: str) -> dict[str, Any] | None:
        if isinstance(payload, Exception):
            raise payload
        return payload

    return _StubClient(responder)


#: A full ``jscmd=data`` book object, shaped after Open Library's own
#: published example for ISBN 0201558025 ("Design Patterns"), trimmed to the
#: fields this client reads and re-keyed to the ISBN-13 this test suite uses.
_FULL_BOOK: dict[str, Any] = {
    "title": "Design Patterns",
    "subtitle": "Elements of Reusable Object-Oriented Software",
    "authors": [
        {"name": "Erich Gamma", "url": "https://openlibrary.org/authors/OL234664A"},
        {"name": "Richard Helm", "url": "https://openlibrary.org/authors/OL234665A"},
    ],
    "publishers": [{"name": "Addison-Wesley Professional"}],
    "publish_date": "1994",
    "number_of_pages": 395,
    "key": "/books/OL1429049M",
}

#: The shape a crowd-sourced, never-enriched catalogue entry actually takes:
#: a title, and nothing else this client can read a fact from.
_THIN_BOOK: dict[str, Any] = {
    "title": "Design Patterns",
    "key": "/books/OL1429049M",
}


class TestNormalizeIsbn13:
    """ISO 2108 arithmetic — see the module docstring for how each was verified."""

    def test_isbn10_converts_to_the_correct_isbn13(self) -> None:
        assert normalize_isbn13(_SICP_10) == _SICP_13

    def test_isbn10_without_hyphens_converts_identically(self) -> None:
        assert normalize_isbn13(_SICP_10.replace("-", "")) == _SICP_13

    def test_isbn13_is_returned_unchanged_when_already_valid(self) -> None:
        assert normalize_isbn13(_SICP_13) == _SICP_13

    def test_a_lowercase_trailing_x_check_digit_is_upper_cased(self) -> None:
        assert normalize_isbn13(_X_CHECK_DIGIT_10.lower()) == _X_CHECK_DIGIT_13

    def test_an_uppercase_trailing_x_check_digit_converts(self) -> None:
        assert normalize_isbn13(_X_CHECK_DIGIT_10) == _X_CHECK_DIGIT_13

    def test_spaces_are_stripped_like_hyphens(self) -> None:
        assert normalize_isbn13("0 306 40615 2") == _SICP_13

    def test_a_bad_isbn10_check_digit_is_malformed_not_converted(self) -> None:
        """The last digit of a real ISBN, deliberately wrong.

        A check-digit failure must never be "repaired" into a plausible-
        looking ISBN-13 — see ``normalize_isbn13``'s own docstring for why a
        malformed identifier is neither a fact nor ignorance, and must never
        be silently coerced into one that resolves nowhere for a reason the
        report cannot explain.
        """
        assert normalize_isbn13("0-306-40615-3") is None

    def test_a_bad_isbn13_check_digit_is_malformed(self) -> None:
        assert normalize_isbn13("9780306406158") is None

    def test_the_wrong_number_of_digits_is_malformed(self) -> None:
        assert normalize_isbn13("030640615") is None  # nine digits
        assert normalize_isbn13("978030640615") is None  # twelve digits

    def test_empty_and_none_are_malformed_not_a_crash(self) -> None:
        assert normalize_isbn13("") is None
        assert normalize_isbn13(None) is None

    def test_non_isbn_text_is_malformed(self) -> None:
        assert normalize_isbn13("not-an-isbn-at-all") is None


class TestByIsbns:
    """The batch ``/api/books`` lookup, keyed by normalised ISBN-13."""

    def test_a_full_record_is_returned_keyed_by_isbn13(self) -> None:
        client = _client({f"ISBN:{_DESIGN_PATTERNS_13}": _FULL_BOOK})
        records = OpenLibrary(client).by_isbns([_DESIGN_PATTERNS_10])

        assert set(records) == {_DESIGN_PATTERNS_13}
        record = records[_DESIGN_PATTERNS_13]
        assert record.source == "openlibrary"
        assert record.title == "Design Patterns: Elements of Reusable Object-Oriented Software"
        assert [str(a) for a in record.authors] == ["Gamma, Erich", "Helm, Richard"]
        assert record.years == {"issued": 1994}
        assert record.publisher == "Addison-Wesley Professional"
        assert record.pages == "395"
        assert record.kind == "book"

    def test_the_request_asks_for_the_normalised_isbn13_bibkey(self) -> None:
        """An ISBN-10 in the bibliography is looked up by its ISBN-13 key.

        A registry storing one form must match a bibliography storing the
        other — the whole reason ``by_isbns`` normalises before it ever
        builds a URL.
        """
        client = _client({})
        OpenLibrary(client).by_isbns([_SICP_10])

        assert len(client.urls) == 1
        url = client.urls[0]
        assert url.startswith("https://openlibrary.org/api/books?")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert query["bibkeys"] == [f"ISBN:{_SICP_13}"]
        assert query["format"] == ["json"]
        assert query["jscmd"] == ["data"]

    def test_an_isbn_absent_from_the_response_is_a_confirmed_miss(self) -> None:
        """The batch endpoint never 404s the collection — see the module docstring.

        An ISBN nobody has simply is not a key in the returned object, and
        that has to read as "Open Library does not have this book", not as an
        error.
        """
        client = _client({})  # answered, holds nothing
        records = OpenLibrary(client).by_isbns([_SICP_10])
        assert records == {}

    def test_a_malformed_isbn_is_never_sent_to_the_network(self) -> None:
        """See ``normalize_isbn13``: a bad check digit is excluded, not queried.

        Sending it anyway would waste a request and come back indistinguishable
        from "Open Library does not have this ISBN" — exactly the conflation
        this client exists to avoid making.
        """
        client = _client({})
        records = OpenLibrary(client).by_isbns(["0-306-40615-3"])  # bad check digit

        assert records == {}
        assert client.urls == []

    def test_a_registry_outage_raises_transient_not_an_empty_result(self) -> None:
        client = _client(Transient("openlibrary: simulated outage"))
        with pytest.raises(Transient):
            OpenLibrary(client).by_isbns([_SICP_10])

    def test_duplicate_isbns_are_requested_once(self) -> None:
        client = _client({f"ISBN:{_SICP_13}": _FULL_BOOK})
        OpenLibrary(client).by_isbns([_SICP_10, _SICP_13, _SICP_10])

        query = urllib.parse.parse_qs(urllib.parse.urlparse(client.urls[0]).query)
        assert query["bibkeys"] == [f"ISBN:{_SICP_13}"]

    def test_no_isbns_makes_no_request_at_all(self) -> None:
        client = _client({})
        assert OpenLibrary(client).by_isbns([]) == {}
        assert client.urls == []


class TestRecordMapping:
    """``jscmd=data`` fields onto :class:`~bibaudit.model.Record`."""

    def _record(self, book: dict[str, Any]) -> Any:
        client = _client({f"ISBN:{_SICP_13}": book})
        return OpenLibrary(client).by_isbns([_SICP_13])[_SICP_13]

    def test_a_subtitle_already_folded_into_the_title_is_not_doubled(self) -> None:
        book = dict(_FULL_BOOK, title="Design Patterns: Elements of Reusable Object-Oriented Software")
        record = self._record(book)
        assert record.title == "Design Patterns: Elements of Reusable Object-Oriented Software"

    def test_no_subtitle_leaves_the_title_alone(self) -> None:
        book = {k: v for k, v in _FULL_BOOK.items() if k != "subtitle"}
        assert self._record(book).title == "Design Patterns"

    def test_a_corporate_author_is_treated_as_a_collective(self) -> None:
        """``names.parse_name`` already knows an institutional author when it sees one.

        Routing every Open Library creator through the same parser used for
        Crossref and BibTeX authors is what keeps "The Endogenous Hormones and
        Breast Cancer Collaborative Group"-shaped names from being split into
        several people, wherever the name came from.
        """
        book = dict(_FULL_BOOK, authors=[{"name": "World Health Organization"}])
        record = self._record(book)
        assert len(record.authors) == 1
        assert record.authors[0].collective is True
        assert record.authors[0].literal == "World Health Organization"

    def test_a_thin_record_has_no_authors_years_publisher_or_pages(self) -> None:
        record = self._record(_THIN_BOOK)
        assert record.title == "Design Patterns"
        assert record.authors == []
        assert record.years == {}
        assert record.publisher is None
        assert record.pages is None
        assert record.kind == "book"

    def test_publish_date_that_is_only_a_year_still_parses(self) -> None:
        book = dict(_FULL_BOOK, publish_date="1994")
        assert self._record(book).years == {"issued": 1994}

    def test_a_free_text_publish_date_still_yields_a_year(self) -> None:
        book = dict(_FULL_BOOK, publish_date="March 1994")
        assert self._record(book).years == {"issued": 1994}

    def test_number_of_pages_becomes_the_pages_field_as_a_string(self) -> None:
        assert self._record(dict(_FULL_BOOK, number_of_pages=395)).pages == "395"

    def test_zero_pages_is_not_a_real_extent(self) -> None:
        assert self._record(dict(_FULL_BOOK, number_of_pages=0)).pages is None

    def test_only_the_first_publisher_is_kept(self) -> None:
        book = dict(
            _FULL_BOOK,
            publishers=[{"name": "Addison-Wesley Professional"}, {"name": "Pearson"}],
        )
        assert self._record(book).publisher == "Addison-Wesley Professional"


class TestSearch:
    """The ``search.json`` free-text lookup, used for entries with no ISBN."""

    def _ref(self, **overrides: object) -> Reference:
        from bibaudit.model import Name

        base: dict[str, object] = {
            "key": "gamma1994design",
            "locator": "references.bib:1",
            "kind": "book",
            "title": "Design Patterns",
            "authors": [Name(family="Gamma", given="Erich")],
            "year": 1994,
        }
        base.update(overrides)
        return Reference(**base)  # type: ignore[arg-type]

    def test_title_and_author_are_sent_as_separate_parameters(self) -> None:
        client = _client({"docs": []})
        OpenLibrary(client).search(self._ref())

        assert len(client.urls) == 1
        parsed = urllib.parse.urlparse(client.urls[0])
        assert parsed.path == "/search.json"
        query = urllib.parse.parse_qs(parsed.query)
        assert query["title"] == ["Design Patterns"]
        assert query["author"] == ["Gamma"]

    def test_no_title_and_no_author_makes_no_request(self) -> None:
        client = _client({"docs": []})
        results = OpenLibrary(client).search(self._ref(title=None, authors=[]))
        assert results == []
        assert client.urls == []

    def test_a_full_search_doc_maps_every_field(self) -> None:
        client = _client(
            {
                "docs": [
                    {
                        "title": "Design Patterns",
                        "subtitle": "Elements of Reusable Object-Oriented Software",
                        "author_name": ["Erich Gamma", "Richard Helm"],
                        "first_publish_year": 1994,
                        "publisher": ["Addison-Wesley Professional"],
                        "number_of_pages_median": 395,
                    }
                ]
            }
        )
        [record] = OpenLibrary(client).search(self._ref())

        assert record.source == "openlibrary"
        assert record.title == "Design Patterns: Elements of Reusable Object-Oriented Software"
        assert [str(a) for a in record.authors] == ["Gamma, Erich", "Helm, Richard"]
        assert record.years == {"issued": 1994}
        assert record.publisher == "Addison-Wesley Professional"
        assert record.pages == "395"
        assert record.kind == "book"

    def test_a_thin_search_doc_maps_title_only(self) -> None:
        client = _client({"docs": [{"title": "Design Patterns"}]})
        [record] = OpenLibrary(client).search(self._ref())

        assert record.title == "Design Patterns"
        assert record.authors == []
        assert record.years == {}
        assert record.publisher is None
        assert record.pages is None

    def test_results_are_capped_at_rows(self) -> None:
        docs = [{"title": f"Book {i}"} for i in range(10)]
        client = _client({"docs": docs})
        results = OpenLibrary(client).search(self._ref(), rows=3)
        assert len(results) == 3

    def test_no_hits_is_an_empty_list_not_none(self) -> None:
        client = _client({"docs": []})
        assert OpenLibrary(client).search(self._ref()) == []

    def test_a_missing_docs_key_is_treated_as_no_hits(self) -> None:
        client = _client({})
        assert OpenLibrary(client).search(self._ref()) == []

    def test_a_registry_outage_raises_transient(self) -> None:
        client = _client(Transient("openlibrary: simulated outage"))
        with pytest.raises(Transient):
            OpenLibrary(client).search(self._ref())


class TestThinRecordsCannotConfirmABook:
    """The corroboration rule ``compare.confirm_without_id`` applies to search hits.

    Both tests drive real :class:`OpenLibrary` output through the real
    ``confirm_without_id`` — the point is not just that this client parses a
    thin record correctly, but that a thin record it hands back is refused by
    the caller responsible for deciding what counts as confirmation.
    """

    def _ref(self) -> Reference:
        from bibaudit.model import Name

        return Reference(
            key="gamma1994design",
            locator="references.bib:1",
            kind="book",
            title="Design Patterns",
            authors=[Name(family="Gamma", given="Erich")],
            year=1994,
        )

    def test_a_thin_candidate_does_not_confirm(self) -> None:
        client = _client({"docs": [{"title": "Design Patterns"}]})
        candidates = OpenLibrary(client).search(self._ref())

        record, reason = confirm_without_id(self._ref(), candidates)

        assert record is None
        assert "no author or year" in reason

    def test_a_fully_corroborated_candidate_confirms(self) -> None:
        client = _client(
            {
                "docs": [
                    {
                        "title": "Design Patterns",
                        "author_name": ["Erich Gamma"],
                        "first_publish_year": 1994,
                    }
                ]
            }
        )
        candidates = OpenLibrary(client).search(self._ref())

        record, reason = confirm_without_id(
            self._ref(), candidates, thresholds=Thresholds()
        )

        assert record is not None
        assert record.source == "openlibrary"
        assert "corroboration" in reason

    def test_a_thin_candidate_for_a_chapter_still_does_not_confirm(self) -> None:
        """A chapter is compatible in *kind* with a book candidate, not in evidence.

        ``confirm_without_id`` treats ``book``/``chapter`` as the same shape
        of work — Open Library has no chapter type of its own — but relaxing
        the *kind* check must not relax the corroboration requirement beside
        it.
        """
        ref = Reference(
            key="hainaut2011biobank",
            locator="references.bib:1",
            kind="chapter",
            title="Methods in Biobanking",
        )
        client = _client({"docs": [{"title": "Methods in Biobanking"}]})
        candidates = OpenLibrary(client).search(ref)

        record, reason = confirm_without_id(ref, candidates)
        assert record is None
        assert "no author or year" in reason
