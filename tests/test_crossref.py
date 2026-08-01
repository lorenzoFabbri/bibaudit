"""Crossref registry client.

Offline only: a :class:`_FakeClient` stands in for
:class:`~bibaudit.registries.http.Client` so these tests exercise exactly what
``crossref.py`` does with a response, without needing the network. The
retraction-direction cases are the ones that matter most: reading
``update-to``/``updated-by`` backwards silently clears a retracted paper,
which is the single worst failure this tool could have. Each case here was
checked against the live API before being written (see
``docs/registry-artifacts.md`` and the module docstring for the DOIs used),
not assumed from memory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest

from bibaudit.model import Name, Reference
from bibaudit.registries.crossref import Crossref
from bibaudit.registries.http import Transient

DATA = Path(__file__).parent / "data"


def _recorded(name: str) -> dict[str, Any]:
    """One Crossref ``work`` object exactly as the live API returned it.

    The reciprocal-deposit cases below are the reason these are recorded rather
    than hand-built from ``_work``: a synthetic ``updated-by=[{...}]`` encodes
    whatever the author of the test believed the deposit looked like, which is
    how the direction rule came to be verified against a payload that did not
    contain the shape that breaks it.
    """
    with (DATA / name).open(encoding="utf-8") as handle:
        payload: dict[str, Any] = json.load(handle)
    message: dict[str, Any] = payload.get("message", payload)
    return message


class _FakeClient:
    """Stands in for :class:`~bibaudit.registries.http.Client`.

    Only ``get_json`` is exercised by ``crossref.py``, so that is all this
    fake needs to provide. *responder* maps a request URL to whatever
    ``get_json`` should return (or raise, for :class:`Transient` cases).
    """

    def __init__(self, responder: Any) -> None:
        self._responder = responder
        self.urls: list[str] = []

    def get_json(
        self, url: str, *, cache_key: str | None = None, headers: dict[str, str] | None = None
    ) -> dict[str, Any] | None:
        self.urls.append(url)
        return self._responder(url)


def _work(**overrides: Any) -> dict[str, Any]:
    """A minimal-but-valid Crossref ``work`` object, patched per test."""
    base: dict[str, Any] = {
        "DOI": "10.1000/example",
        "title": ["A title"],
        "type": "journal-article",
    }
    base.update(overrides)
    return base


class TestRetractionDirection:
    """`updated-by` on a record means *that record* was retracted.

    `update-to` means the opposite: the record carrying it *is* the notice.
    Confirmed live against 10.1016/S0140-6736(97)11096-0 (the retracted
    Wakefield paper, which carries `updated-by` entries) and its own
    retraction notice 10.1016/S0140-6736(10)60175-4 (which carries
    `update-to` pointing back and no `updated-by` at all).
    """

    def test_updated_by_retraction_marks_the_record_retracted(self) -> None:
        client = _FakeClient(
            lambda url: {
                "message": {
                    "items": [
                        _work(
                            DOI="10.1016/s0140-6736(97)11096-0",
                            **{"updated-by": [{"type": "retraction"}]},
                        )
                    ]
                }
            }
        )
        result = Crossref(client).by_dois(["10.1016/S0140-6736(97)11096-0"])
        record = result["10.1016/s0140-6736(97)11096-0"]
        assert record.retracted is True
        assert record.retraction_kind == "retraction"

    def test_update_to_alone_does_not_mark_the_record_retracted(self) -> None:
        """The notice itself carries `update-to`, not `updated-by`.

        Reading `update-to` to decide retractedness would flag the notice
        and clear the paper it retracts — exactly backwards.
        """
        client = _FakeClient(
            lambda url: {
                "message": {
                    "items": [
                        _work(
                            DOI="10.1016/s0140-6736(10)60175-4",
                            **{"update-to": [{"type": "retraction"}]},
                        )
                    ]
                }
            }
        )
        result = Crossref(client).by_dois(["10.1016/S0140-6736(10)60175-4"])
        record = result["10.1016/s0140-6736(10)60175-4"]
        assert record.retracted is False
        assert record.retraction_kind is None

    def test_no_updated_by_at_all_is_not_retracted(self) -> None:
        client = _FakeClient(lambda url: {"message": {"items": [_work()]}})
        result = Crossref(client).by_dois(["10.1000/example"])
        assert result["10.1000/example"].retracted is False

    def test_partial_retraction_is_caught(self) -> None:
        """Regression: `partial_retraction` was missing from the priority
        list, so 10.29328/journal.jcmhs.1001023 — whose `updated-by` carries
        only a `partial_retraction` entry, confirmed live — was silently
        reported as clean.
        """
        client = _FakeClient(
            lambda url: {
                "message": {
                    "items": [
                        _work(
                            DOI="10.29328/journal.jcmhs.1001023",
                            **{"updated-by": [{"type": "partial_retraction"}]},
                        )
                    ]
                }
            }
        )
        result = Crossref(client).by_dois(["10.29328/journal.jcmhs.1001023"])
        record = result["10.29328/journal.jcmhs.1001023"]
        assert record.retracted is True
        assert record.retraction_kind == "partial_retraction"

    def test_full_retraction_wins_over_a_milder_notice_on_the_same_record(self) -> None:
        """10.1016/S0140-6736(20)31180-6 carries both `expression_of_concern`
        and `retraction` in `updated-by`, deposited at different times. The
        more definitive event must win, or the report understates the
        defect.
        """
        client = _FakeClient(
            lambda url: {
                "message": {
                    "items": [
                        _work(
                            **{
                                "updated-by": [
                                    {"type": "expression_of_concern"},
                                    {"type": "retraction"},
                                ]
                            }
                        )
                    ]
                }
            }
        )
        result = Crossref(client).by_dois(["10.1000/example"])
        assert result["10.1000/example"].retraction_kind == "retraction"

    def test_unrelated_update_type_is_not_a_retraction(self) -> None:
        """A `new_version` or similar notice is not a corrective action."""
        client = _FakeClient(
            lambda url: {"message": {"items": [_work(**{"updated-by": [{"type": "new_version"}]})]}}
        )
        result = Crossref(client).by_dois(["10.1000/example"])
        assert result["10.1000/example"].retracted is False


class TestReciprocalUpdateDeposits:
    """A publisher that deposits the same relation in *both* directions.

    Crossref's model makes ``update-to`` and ``updated-by`` opposites, so a
    record cannot honestly carry both about the same DOI and the same type.
    Elsevier does exactly that on the Surgisphere pair, and the direction rule
    — which was verified live, but against the *paper* only — reads it wrong on
    the notice.

    Every payload here is the live response, kept verbatim under
    ``tests/data/``. The three of them are one another's counter-tests: the
    first is the false accusation being fixed, and the second and third are the
    two ways the obvious fix silently clears a genuinely retracted work.
    """

    def test_a_retraction_notice_is_not_reported_as_retracted(self) -> None:
        """10.1016/S0140-6736(20)31324-6 — the Lancet's Surgisphere notice.

        It carries ``update-to: retraction -> 31180-6`` (sourced
        ``retraction-watch``) *and* the publisher's reciprocal ``updated-by:
        retraction <- 31180-6``. Read naively it has itself been retracted, so
        any manuscript about the scandal that cites the notice — the correct
        thing to cite — was failed RETRACTED. A false factual claim about a
        named work is the one output this tool may never produce.
        """
        work = _recorded("crossref_reciprocal_retraction_notice.json")
        client = _FakeClient(lambda url: {"message": {"items": [work]}})

        record = Crossref(client).by_dois([work["DOI"]])[work["DOI"]]

        assert record.retracted is False
        assert record.retraction_kind is None

    def test_the_paper_that_notice_retracted_is_still_retracted(self) -> None:
        """The true positive, and the reason the obvious fix is unusable.

        10.1016/S0140-6736(20)31180-6 carries the mirror image — ``updated-by:
        retraction <- 31324-6`` *and* ``update-to: retraction -> 31324-6``. A
        rule that simply discounted every reciprocated ``updated-by`` would
        clear the retracted paper itself, which is the worst miss this tool can
        make. What separates the records is that Retraction Watch recorded the
        notice side on the notice and the retracted side on the paper.
        """
        work = _recorded("crossref_reciprocal_retracted_paper.json")
        client = _FakeClient(lambda url: {"message": {"items": [work]}})

        record = Crossref(client).by_dois([work["DOI"]])[work["DOI"]]

        assert record.retracted is True
        assert record.retraction_kind == "retraction"

    def test_a_self_referential_partial_retraction_is_still_retracted(self) -> None:
        """The tie, and it must fall to the finding.

        10.29328/journal.jcmhs.1001023 points its genuine ``partial_retraction``
        at *itself* in both arrays, both sourced ``publisher``. Retraction Watch
        has no opinion, so nothing licenses discounting anything, and the entry
        is still reported. A self-reference test would have cleared it.
        """
        work = _recorded("compare_crossref_partial_retraction.json")
        client = _FakeClient(lambda url: {"message": {"items": [work]}})

        record = Crossref(client).by_dois([work["DOI"]])[work["DOI"]]

        assert record.retracted is True
        assert record.retraction_kind == "partial_retraction"

    def test_a_reciprocal_deposit_without_retraction_watch_still_reports(self) -> None:
        """The boundary, stated on its own rather than inferred from a fixture.

        Reciprocity alone is never enough. Only Retraction Watch naming *this*
        record as the notice — and not also naming it as retracted — discounts
        the relation, so a publisher-only pair in both directions is reported.
        """
        both = [
            {"type": "retraction", "DOI": "10.1000/notice", "source": "publisher"}
        ]
        client = _FakeClient(
            lambda url: {
                "message": {
                    "items": [_work(**{"update-to": both, "updated-by": both})]
                }
            }
        )

        record = Crossref(client).by_dois(["10.1000/example"])["10.1000/example"]

        assert record.retracted is True

    def test_retraction_watch_naming_it_both_ways_still_reports(self) -> None:
        """Retraction Watch contradicting itself is not licence to clear a work.

        The discount requires RW on the ``update-to`` side and *not* on the
        ``updated-by`` side. With RW on both, the evidence is contradictory and
        the safe reading — the one that leaves the finding standing — is the
        only one this tool is allowed to take.
        """
        both = [
            {"type": "retraction", "DOI": "10.1000/notice", "source": "retraction-watch"}
        ]
        client = _FakeClient(
            lambda url: {
                "message": {
                    "items": [_work(**{"update-to": both, "updated-by": both})]
                }
            }
        )

        record = Crossref(client).by_dois(["10.1000/example"])["10.1000/example"]

        assert record.retracted is True

    def test_an_unreciprocated_updated_by_is_untouched(self) -> None:
        """A retraction with no reciprocal deposit is the ordinary case.

        The Wakefield paper carries ``updated-by`` and nothing pointing the
        other way, and nothing about this rule may reach it.
        """
        work = _recorded("compare_crossref_wakefield_retracted.json")
        client = _FakeClient(lambda url: {"message": {"items": [work]}})

        record = Crossref(client).by_dois([work["DOI"]])[work["DOI"]]

        assert record.retracted is True


class TestTitleSubtitle:
    def test_subtitle_is_appended_when_absent_from_title(self) -> None:
        client = _FakeClient(
            lambda url: {
                "message": {
                    "items": [_work(title=["Effect of X on Y"], subtitle=["A randomised trial"])]
                }
            }
        )
        record = Crossref(client).by_dois(["10.1000/example"])["10.1000/example"]
        assert record.title == "Effect of X on Y: A randomised trial"

    def test_subtitle_already_present_in_title_is_not_duplicated(self) -> None:
        """Some publishers deposit the full 'Title: Subtitle' in `title` and
        repeat the subtitle in `subtitle`; appending it again would produce
        'Title: Subtitle: Subtitle'.
        """
        client = _FakeClient(
            lambda url: {
                "message": {
                    "items": [
                        _work(
                            title=["Effect of X on Y: A randomised trial"],
                            subtitle=["A randomised trial"],
                        )
                    ]
                }
            }
        )
        record = Crossref(client).by_dois(["10.1000/example"])["10.1000/example"]
        assert record.title == "Effect of X on Y: A randomised trial"

    def test_no_title_falls_back_to_bare_subtitle(self) -> None:
        client = _FakeClient(lambda url: {"message": {"items": [_work(title=[], subtitle=["Only this"])]}})
        record = Crossref(client).by_dois(["10.1000/example"])["10.1000/example"]
        assert record.title == "Only this"


class TestYears:
    def test_print_online_and_issued_are_kept_separate(self) -> None:
        client = _FakeClient(
            lambda url: {
                "message": {
                    "items": [
                        _work(
                            **{
                                "published-print": {"date-parts": [[2021, 3]]},
                                "published-online": {"date-parts": [[2020, 11]]},
                                "issued": {"date-parts": [[2020, 11]]},
                            }
                        )
                    ]
                }
            }
        )
        record = Crossref(client).by_dois(["10.1000/example"])["10.1000/example"]
        assert record.years == {"print": 2021, "online": 2020, "issued": 2020}

    def test_null_date_parts_do_not_raise(self) -> None:
        client = _FakeClient(
            lambda url: {
                "message": {"items": [_work(**{"published-print": {"date-parts": [[None]]}})]}
            }
        )
        record = Crossref(client).by_dois(["10.1000/example"])["10.1000/example"]
        assert record.years == {}


class TestAuthors:
    def test_organisation_author_is_collective(self) -> None:
        client = _FakeClient(
            lambda url: {
                "message": {
                    "items": [
                        _work(
                            author=[
                                {"name": "World Health Organization"},
                                {"family": "Smith", "given": "J"},
                            ]
                        )
                    ]
                }
            }
        )
        record = Crossref(client).by_dois(["10.1000/example"])["10.1000/example"]
        assert record.authors[0].collective
        assert record.authors[0].literal == "World Health Organization"
        assert not record.authors[1].collective

    def test_author_order_is_preserved(self) -> None:
        client = _FakeClient(
            lambda url: {
                "message": {
                    "items": [
                        _work(
                            author=[
                                {"family": "Aaa", "given": "A"},
                                {"family": "Bbb", "given": "B"},
                                {"family": "Ccc", "given": "C"},
                            ]
                        )
                    ]
                }
            }
        )
        record = Crossref(client).by_dois(["10.1000/example"])["10.1000/example"]
        assert [n.family for n in record.authors] == ["Aaa", "Bbb", "Ccc"]

    def test_editor_used_only_when_author_is_absent(self) -> None:
        client = _FakeClient(
            lambda url: {
                "message": {
                    "items": [
                        _work(
                            author=[{"family": "Smith", "given": "J"}],
                            editor=[{"family": "Jones", "given": "K"}],
                        )
                    ]
                }
            }
        )
        record = Crossref(client).by_dois(["10.1000/example"])["10.1000/example"]
        assert [n.family for n in record.authors] == ["Smith"]
        assert "authors_source" not in record.raw

    def test_editor_used_when_author_is_empty_list(self) -> None:
        """An edited-volume DOI record legitimately has `author: []`."""
        client = _FakeClient(
            lambda url: {
                "message": {"items": [_work(author=[], editor=[{"family": "Jones", "given": "K"}])]}
            }
        )
        record = Crossref(client).by_dois(["10.1000/example"])["10.1000/example"]
        assert [n.family for n in record.authors] == ["Jones"]
        assert record.raw["authors_source"] == "editor"

    def test_stub_creator_with_no_name_data_is_skipped(self) -> None:
        client = _FakeClient(
            lambda url: {
                "message": {
                    "items": [
                        _work(author=[{"ORCID": "0000-0000-0000-0000"}, {"family": "Smith"}])
                    ]
                }
            }
        )
        record = Crossref(client).by_dois(["10.1000/example"])["10.1000/example"]
        assert [n.family for n in record.authors] == ["Smith"]


class TestByDoisBatching:
    def test_filter_url_percent_encodes_parentheses_and_slash(self) -> None:
        client = _FakeClient(
            lambda url: {
                "message": {"items": [_work(DOI="10.1016/s0140-6736(03)14065-2")]}
            }
        )
        Crossref(client).by_dois(["10.1016/S0140-6736(03)14065-2"])
        assert len(client.urls) == 1
        assert "filter=doi:10.1016%2Fs0140-6736%2803%2914065-2" in client.urls[0]

    def test_more_than_twenty_dois_split_into_batches_of_twenty(self) -> None:
        dois = [f"10.1000/{i}" for i in range(45)]

        def responder(url: str) -> dict[str, Any]:
            filter_clause = url.split("filter=", 1)[1]
            count = filter_clause.count("doi:")
            return {"message": {"items": [_work(DOI=f"10.1000/{i}") for i in range(count)]}}

        client = _FakeClient(responder)
        Crossref(client).by_dois(dois)
        assert len(client.urls) == 3
        counts = [url.split("filter=", 1)[1].count("doi:") for url in client.urls]
        assert counts == [20, 20, 5]

    def test_failed_batch_falls_back_to_individual_fetches(self) -> None:
        """A single malformed DOI 400s the whole batch filter (verified
        live). The other 19 DOIs in the batch must not be lost — the
        fallback recovers them one at a time via `/works/{doi}`.
        """
        batch = [f"10.2000/{i}" for i in range(20)]
        calls: list[str] = []

        def responder(url: str) -> dict[str, Any]:
            calls.append(url)
            if "filter=" in url:
                raise Transient("simulated: one bad DOI 400s the whole batch")
            doi = unquote(url.rsplit("/works/", 1)[1])
            return {"message": _work(DOI=doi)}

        client = _FakeClient(responder)
        result = Crossref(client).by_dois(batch)
        assert len(result) == 20
        assert len(calls) == 21  # 1 failed batch + 20 individual fallbacks

    def test_total_outage_during_fallback_propagates_rather_than_losing_the_batch(self) -> None:
        """If the fallback fetches themselves fail, that is a registry
        outage, not 20 confirmed-missing works — the exception must
        propagate rather than being folded into a partial result.
        """
        batch = [f"10.2000/{i}" for i in range(5)]

        def responder(url: str) -> dict[str, Any]:
            raise Transient("registry unreachable")

        client = _FakeClient(responder)
        with pytest.raises(Transient):
            Crossref(client).by_dois(batch)

    def test_empty_input_makes_no_request(self) -> None:
        client = _FakeClient(lambda url: {"message": {"items": []}})
        assert Crossref(client).by_dois([]) == {}
        assert client.urls == []

    def test_duplicate_and_blank_dois_are_deduplicated_before_any_request(self) -> None:
        client = _FakeClient(
            lambda url: {"message": {"items": [_work(DOI="10.1000/example")]}}
        )
        Crossref(client).by_dois(["10.1000/example", "10.1000/EXAMPLE", "", "10.1000/example"])
        assert len(client.urls) == 1
        assert client.urls[0].count("doi:") == 1


class TestSearch:
    def test_query_bibliographic_combines_title_author_and_container(self) -> None:
        ref = Reference(
            key="k1",
            locator="x.bib:1",
            title="Effect of shift work on cancer",
            authors=[Name(family="Papantoniou", given="K")],
            container="American Journal of Epidemiology",
        )
        client = _FakeClient(lambda url: {"message": {"items": []}})
        Crossref(client).search(ref)
        assert len(client.urls) == 1
        url = client.urls[0]
        assert "query.bibliographic=" in url
        assert "Papantoniou" in url
        assert "shift" in url.lower()

    def test_empty_reference_makes_no_request(self) -> None:
        client = _FakeClient(lambda url: {"message": {"items": []}})
        result = Crossref(client).search(Reference(key="k2", locator="x.bib:2"))
        assert result == []
        assert client.urls == []

    def test_a_404_style_none_payload_yields_no_candidates(self) -> None:
        ref = Reference(key="k3", locator="x.bib:3", title="Something")
        client = _FakeClient(lambda url: None)
        assert Crossref(client).search(ref) == []
