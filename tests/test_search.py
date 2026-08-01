"""Multi-registry search for references that carry no identifier.

Offline only: a :class:`_FakeClient` stands in for
:class:`~bibaudit.registries.http.Client`, routing a request by which host it
targets (``api.crossref.org``, Europe PMC, ``api.openalex.org``) rather than by
matching the exact query string. That keeps these tests about what
``search.py`` does with each source's *response* — merging, deduplication,
failure isolation — independent of this module's own query-construction
details, which ``TestQueryConstruction`` below covers on its own.

The four things the assignment asks this suite to prove each get their own
class: a Crossref-only hit, a Europe-PMC-only hit, deduplication across
sources, and one source raising :class:`Transient` while another still
answers. ``TestTotalOutage`` is the fifth case that has to sit beside the
fourth: if *every* source fails, ``Search.candidates`` must raise rather than
return an empty list — see the module docstring on why the two are not
interchangeable.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from bibaudit.model import Name, Reference
from bibaudit.registries.http import Transient
from bibaudit.registries.search import Search

_CROSSREF_HOST = "api.crossref.org"
_EUROPEPMC_HOST = "europepmc"
_OPENALEX_HOST = "api.openalex.org"


class _FakeClient:
    """Stands in for :class:`~bibaudit.registries.http.Client`.

    Only ``get_json`` is exercised by ``search.py``, so that is all this fake
    needs to provide. *responders* maps a host substring to a callable
    producing that source's response (or raising :class:`Transient`); a URL
    matching no configured host is a test-authoring error, not a silent
    ``None``, so it raises loudly rather than letting a typo read as "that
    source had nothing".
    """

    def __init__(self, responders: dict[str, Callable[[str], dict[str, Any] | None]]) -> None:
        self._responders = responders
        self.urls: list[str] = []

    def get_json(
        self, url: str, *, cache_key: str | None = None, headers: dict[str, str] | None = None
    ) -> dict[str, Any] | None:
        self.urls.append(url)
        for host, responder in self._responders.items():
            if host in url:
                return responder(url)
        raise AssertionError(f"a test URL matched no configured responder: {url}")


def _forbidden(url: str) -> dict[str, Any]:
    raise AssertionError(f"a disabled or unexpected source was queried: {url}")


def _ref(**overrides: Any) -> Reference:
    base: dict[str, Any] = {
        "key": "smith2020shiftwork",
        "locator": "references.bib:1",
        "kind": "article",
        "title": "Effect of shift work on cancer risk",
        "authors": [Name(family="Smith", given="J")],
        "year": 2020,
    }
    base.update(overrides)
    return Reference(**base)


def _crossref_payload(*works: dict[str, Any]) -> dict[str, Any]:
    return {"message": {"items": list(works)}}


def _crossref_work(doi: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "DOI": doi,
        "title": ["Effect of shift work on cancer risk"],
        "type": "journal-article",
        "author": [{"family": "Smith", "given": "J"}],
        "issued": {"date-parts": [[2020]]},
    }
    base.update(overrides)
    return base


def _europepmc_payload(*results: dict[str, Any]) -> dict[str, Any]:
    return {"resultList": {"result": list(results)}}


def _europepmc_result(doi: str | None, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "doi": doi,
        "title": "Effect of shift work on cancer risk",
        "pubYear": "2020",
        "authorList": {"author": [{"lastName": "Smith", "firstName": "J"}]},
        "journalInfo": {
            "volume": "12",
            "issue": "3",
            "journal": {"title": "Journal of Occupational Epidemiology"},
        },
        "pageInfo": "101-110",
        "pubTypeList": {"pubType": ["research-article"]},
    }
    base.update(overrides)
    return base


def _openalex_payload(*works: dict[str, Any]) -> dict[str, Any]:
    return {"results": list(works)}


def _openalex_work(doi: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "doi": f"https://doi.org/{doi}",
        "title": "Effect of shift work on cancer risk",
        "publication_year": 2020,
        "type": "article",
        "authorships": [{"author": {"display_name": "J Smith"}}],
        "primary_location": {"source": {"display_name": "Journal of Occupational Epidemiology"}},
        "biblio": {"volume": "12", "issue": "3", "first_page": "101", "last_page": "110"},
    }
    base.update(overrides)
    return base


class TestCrossrefOnlyHit:
    def test_a_crossref_hit_survives_with_no_help_from_the_others(self) -> None:
        ref = _ref()
        client = _FakeClient(
            {
                _CROSSREF_HOST: lambda url: _crossref_payload(_crossref_work("10.1000/xyz")),
                _EUROPEPMC_HOST: lambda url: _europepmc_payload(),
                _OPENALEX_HOST: lambda url: _openalex_payload(),
            }
        )

        candidates = Search(client).candidates(ref)

        assert len(candidates) == 1
        assert candidates[0].source == "crossref"
        assert candidates[0].doi == "10.1000/xyz"
        assert candidates[0].title == "Effect of shift work on cancer risk"


class TestEuropePMCOnlyHit:
    def test_a_work_crossrefs_own_search_misses_is_still_found(self) -> None:
        """The whole reason this module exists rather than searching Crossref
        alone: Crossref's ``query.bibliographic`` answers with nothing for
        this reference, and Europe PMC's separately-curated index still has
        the work.
        """
        ref = _ref()
        client = _FakeClient(
            {
                _CROSSREF_HOST: lambda url: _crossref_payload(),
                _EUROPEPMC_HOST: lambda url: _europepmc_payload(_europepmc_result("10.2000/abc")),
                _OPENALEX_HOST: lambda url: _openalex_payload(),
            }
        )

        candidates = Search(client).candidates(ref)

        assert len(candidates) == 1
        record = candidates[0]
        assert record.source == "europepmc"
        assert record.doi == "10.2000/abc"
        assert record.title == "Effect of shift work on cancer risk"
        assert record.authors == [Name(family="Smith", given="J")]
        assert record.years == {"issued": 2020}
        assert record.container == "Journal of Occupational Epidemiology"
        assert record.volume == "12"
        assert record.pages == "101-110"

    def test_a_404_style_none_payload_yields_no_candidates(self) -> None:
        """Mirrors Crossref's own handling — see ``test_crossref.py``.

        Europe PMC's search endpoint does not 404 in practice, but ``search.py``
        must not assume that: a ``None`` response is "nothing usable", not a
        crash.
        """
        ref = _ref()
        client = _FakeClient(
            {
                _CROSSREF_HOST: lambda url: _crossref_payload(),
                _EUROPEPMC_HOST: lambda url: None,
                _OPENALEX_HOST: lambda url: _openalex_payload(),
            }
        )

        assert Search(client).candidates(ref) == []


class TestOpenAlexOnlyHit:
    def test_a_work_the_other_two_miss_is_still_found(self) -> None:
        """OpenAlex is discovery-only (see the module docstring), but
        discovery is exactly what this covers: a candidate it alone surfaces
        must still reach ``compare.confirm_without_id`` for it to have any
        chance of clearing the same bar as a Crossref or Europe PMC hit.
        """
        ref = _ref()
        client = _FakeClient(
            {
                _CROSSREF_HOST: lambda url: _crossref_payload(),
                _EUROPEPMC_HOST: lambda url: _europepmc_payload(),
                _OPENALEX_HOST: lambda url: _openalex_payload(_openalex_work("10.3000/qrs")),
            }
        )

        candidates = Search(client).candidates(ref)

        assert len(candidates) == 1
        record = candidates[0]
        assert record.source == "openalex"
        assert record.doi == "10.3000/qrs"
        assert record.authors == [Name(family="Smith", given="J")]
        assert record.years == {"issued": 2020}
        assert record.container == "Journal of Occupational Epidemiology"
        assert record.pages == "101-110"


class TestDeduplication:
    def test_the_same_doi_from_two_sources_is_kept_once(self) -> None:
        """OpenAlex largely re-crawls Crossref's own deposits (see the module
        docstring), so the same DOI turning up from both is the *ordinary*
        case, not an edge case. Crossref's copy wins — it is queried first,
        and it is not "the same metadata read a second time" the way
        OpenAlex's is.
        """
        ref = _ref()
        client = _FakeClient(
            {
                _CROSSREF_HOST: lambda url: _crossref_payload(
                    _crossref_work("10.1000/xyz", publisher="Crossref-attributed Publisher")
                ),
                _EUROPEPMC_HOST: lambda url: _europepmc_payload(),
                # Same work, different case and resolver-prefixed — exactly
                # how the same DOI arrives from two independent APIs.
                _OPENALEX_HOST: lambda url: _openalex_payload(_openalex_work("10.1000/XYZ")),
            }
        )

        candidates = Search(client).candidates(ref)

        assert len(candidates) == 1
        assert candidates[0].source == "crossref"
        assert candidates[0].publisher == "Crossref-attributed Publisher"

    def test_a_candidate_with_no_doi_is_never_deduplicated_away(self) -> None:
        """Europe PMC indexes grey literature that may carry no DOI at all.

        Deduplication keys on normalised DOI; a candidate with none supplies
        nothing to key on, so two such candidates from the same source must
        both survive rather than the second being read as a duplicate of the
        first.
        """
        ref = _ref()
        client = _FakeClient(
            {
                _CROSSREF_HOST: lambda url: _crossref_payload(),
                _EUROPEPMC_HOST: lambda url: _europepmc_payload(
                    _europepmc_result(None, title="Agency report, first edition"),
                    _europepmc_result(None, title="Agency report, second printing"),
                ),
                _OPENALEX_HOST: lambda url: _openalex_payload(),
            }
        )

        candidates = Search(client).candidates(ref)

        assert len(candidates) == 2
        assert all(c.doi is None for c in candidates)


class TestFailureIsolation:
    def test_one_source_failing_does_not_silence_another(self) -> None:
        ref = _ref()

        def europepmc_down(url: str) -> dict[str, Any]:
            raise Transient("europepmc: simulated outage")

        client = _FakeClient(
            {
                _CROSSREF_HOST: lambda url: _crossref_payload(_crossref_work("10.1000/xyz")),
                _EUROPEPMC_HOST: europepmc_down,
                _OPENALEX_HOST: lambda url: _openalex_payload(),
            }
        )

        candidates = Search(client).candidates(ref)

        assert len(candidates) == 1
        assert candidates[0].source == "crossref"

    def test_the_failing_source_can_be_any_of_the_three(self) -> None:
        """The isolation is not special-cased to one source over another."""
        ref = _ref()

        def crossref_down(url: str) -> dict[str, Any]:
            raise Transient("crossref: simulated outage")

        client = _FakeClient(
            {
                _CROSSREF_HOST: crossref_down,
                _EUROPEPMC_HOST: lambda url: _europepmc_payload(),
                _OPENALEX_HOST: lambda url: _openalex_payload(_openalex_work("10.3000/qrs")),
            }
        )

        candidates = Search(client).candidates(ref)

        assert len(candidates) == 1
        assert candidates[0].source == "openalex"


class TestTotalOutage:
    def test_every_source_failing_raises_rather_than_returning_empty(self) -> None:
        """An empty list must never mean "nobody was reachable".

        A total outage read as "found nothing" is exactly how a network
        problem turns into ``UNCONFIRMED`` — a failing verdict — for a
        citation the tool never actually got to check. See the module
        docstring's "Failure isolation" section.
        """
        ref = _ref()

        def down(url: str) -> dict[str, Any]:
            raise Transient("simulated outage")

        client = _FakeClient({_CROSSREF_HOST: down, _EUROPEPMC_HOST: down, _OPENALEX_HOST: down})

        with pytest.raises(Transient):
            Search(client).candidates(ref)

    def test_a_disabled_sources_outage_does_not_count_against_the_others(self) -> None:
        """Only *enabled* sources count towards "every source failed".

        With Europe PMC and OpenAlex switched off, Crossref is the only
        source consulted at all; its own outage is therefore a total outage
        for this call, and must still raise.
        """
        ref = _ref()

        def crossref_down(url: str) -> dict[str, Any]:
            raise Transient("crossref: simulated outage")

        client = _FakeClient({_CROSSREF_HOST: crossref_down})

        with pytest.raises(Transient):
            Search(client, use_europepmc=False, use_openalex=False).candidates(ref)


class TestSourceToggles:
    def test_a_disabled_source_is_never_queried(self) -> None:
        ref = _ref()
        client = _FakeClient(
            {
                _CROSSREF_HOST: lambda url: _crossref_payload(),
                _EUROPEPMC_HOST: _forbidden,
                _OPENALEX_HOST: _forbidden,
            }
        )

        Search(client, use_europepmc=False, use_openalex=False).candidates(ref)

        assert len(client.urls) == 1
        assert _CROSSREF_HOST in client.urls[0]

    def test_sources_reflects_the_toggles_in_query_order(self) -> None:
        client = _FakeClient({})

        assert Search(client).sources == ("crossref", "europepmc", "openalex")
        assert Search(client, use_europepmc=False).sources == ("crossref", "openalex")
        assert Search(client, use_openalex=False).sources == ("crossref", "europepmc")
        assert Search(client, use_europepmc=False, use_openalex=False).sources == ("crossref",)


class TestNoQueryMeansNoRequest:
    def test_an_empty_reference_makes_no_request_to_any_source(self) -> None:
        """Mirrors Crossref's own precedent (``test_crossref.py``): a
        reference with nothing to search on must not spend a request finding
        that out.
        """
        client = _FakeClient(
            {_CROSSREF_HOST: _forbidden, _EUROPEPMC_HOST: _forbidden, _OPENALEX_HOST: _forbidden}
        )

        assert Search(client).candidates(Reference(key="k1", locator="x.bib:1")) == []
        assert client.urls == []


class TestQueryConstruction:
    def test_europepmc_query_combines_title_author_and_year(self) -> None:
        ref = _ref()
        seen: dict[str, str] = {}

        def capture(url: str) -> dict[str, Any]:
            seen["url"] = url
            return _europepmc_payload()

        client = _FakeClient(
            {
                _CROSSREF_HOST: lambda url: _crossref_payload(),
                _EUROPEPMC_HOST: capture,
                _OPENALEX_HOST: lambda url: _openalex_payload(),
            }
        )

        Search(client).candidates(ref)

        assert "TITLE" in seen["url"]
        assert "shift" in seen["url"].lower()
        assert "AUTH" in seen["url"]
        assert "Smith" in seen["url"]
        assert "PUB_YEAR%3A2020" in seen["url"] or "PUB_YEAR:2020" in seen["url"]

    def test_openalex_query_is_title_only(self) -> None:
        """Per the assignment's endpoint shape (``filter=title.search:...``);
        author and year are not part of it — OpenAlex is here for discovery,
        never confirmation, and confirmation is where author and year belong.
        """
        ref = _ref()
        seen: dict[str, str] = {}

        def capture(url: str) -> dict[str, Any]:
            seen["url"] = url
            return _openalex_payload()

        client = _FakeClient(
            {
                _CROSSREF_HOST: lambda url: _crossref_payload(),
                _EUROPEPMC_HOST: lambda url: _europepmc_payload(),
                _OPENALEX_HOST: capture,
            }
        )

        Search(client).candidates(ref)

        assert "filter=title.search:" in seen["url"]
        assert "shift" in seen["url"].lower()
        assert "Smith" not in seen["url"]

    def test_no_title_makes_no_openalex_request(self) -> None:
        """OpenAlex's query is title-only (see ``test_openalex_query_is_title_only``),
        so a reference with an author and a year but no title cannot build
        one at all and OpenAlex must not be queried. Europe PMC's query
        builds from all three fields independently, so it is still queried
        and can still answer — proving the OpenAlex skip is not simply "no
        source was consulted".
        """
        ref = Reference(
            key="k1", locator="x.bib:1", authors=[Name(family="Smith", given="J")], year=2020,
        )
        client = _FakeClient(
            {
                _CROSSREF_HOST: lambda url: _crossref_payload(),
                _EUROPEPMC_HOST: lambda url: _europepmc_payload(_europepmc_result("10.9999/nope")),
                _OPENALEX_HOST: _forbidden,
            }
        )

        candidates = Search(client).candidates(ref)

        assert candidates and candidates[0].source == "europepmc"
