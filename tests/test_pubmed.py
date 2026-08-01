"""NCBI PubMed client: MEDLINE parsing, DOI-to-PMID attribution, request pacing.

Offline only. A :class:`_StubClient` stands in for
:class:`~bibaudit.registries.http.Client`, so every test here exercises what
``pubmed.py`` does with a recorded response and nothing reaches the network.

Two groups of tests matter more than the rest:

* **The wrapping tests.** ``efetch`` wraps a long value onto continuation
  lines that carry no tag of their own. A parser that keeps only tagged lines
  silently truncates a title and then reports a title mismatch against a
  perfectly correct entry — the false alarm this project says costs more than
  a miss.
* **The retraction tests.** ``PT  - Retracted Publication`` is on the article
  that was retracted; ``PT  - Retraction of Publication`` is on the notice
  that retracted it. They are one word apart and mean opposite things, so both
  directions are asserted: reading them backwards clears retracted work, which
  is the worst failure this tool can have.

The fixtures under ``tests/data/pubmed_*.txt`` are MEDLINE plain text in
``efetch``'s exact wire format — a tag padded to four columns, ``"- "``, the
value, and every continuation of a long value on a following line indented six
spaces with **no tag**. That wrapping is the thing under test. Re-flowing a
fixture onto single long lines, or "tidying" the indentation, deletes the point
of half this file while leaving it green. Field values are the real citations
where the case depends on them (the Wakefield 1998 Lancet paper and the 2010
notice that retracted it; the MCC-Spain shift-work paper named in
``docs/registry-artifacts.md``); PMIDs and dates elsewhere are join keys.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from bibaudit.model import Record
from bibaudit.normalize import normalize_doi
from bibaudit.registries import pubmed
from bibaudit.registries.http import Transient
from bibaudit.registries.pubmed import PubMed

DATA = Path(__file__).parent / "data"

#: The retracted/notice pair used throughout. Note the parentheses: a DOI
#: regex that stops at "(" truncates both of these, which is why
#: `normalize.DOI_PATTERN` allows them and why nothing here writes its own.
WAKEFIELD_DOI = "10.1016/S0140-6736(97)11096-0"
WAKEFIELD_PMID = "9500320"
RETRACTION_NOTICE_DOI = "10.1016/S0140-6736(10)60175-4"
RETRACTION_NOTICE_PMID = "20137807"


def _fixture(name: str) -> str:
    """Read ``tests/data/pubmed_<name>.txt`` verbatim, wrapping included."""
    return (DATA / f"pubmed_{name}.txt").read_text(encoding="utf-8")


def _params(url: str) -> dict[str, str]:
    """Decoded query parameters of a request URL."""
    query = urllib.parse.urlsplit(url).query
    return {k: v[0] for k, v in urllib.parse.parse_qs(query, keep_blank_values=True).items()}


class _FakeClock:
    """Stands in for the whole ``time`` module inside ``pubmed``.

    ``pubmed`` uses exactly two names from it, ``monotonic`` and ``sleep``.
    Substituting the module keeps the pacing assertions honest — the code
    really does ask to wait, and :class:`TestRequestPacing` reads how long —
    while a suite that waited a third of a second per request would be a suite
    nobody runs.
    """

    def __init__(self) -> None:
        self.now = 1_000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class _StubClient:
    """Stands in for :class:`~bibaudit.registries.http.Client`.

    ``pubmed.py`` reaches the network only through ``get_json`` (esearch,
    esummary) and ``get_text`` (efetch), so those two methods are the whole
    surface a fake needs.

    Parameters
    ----------
    esearch_ids:
        What ``esearch`` answers. The order is deliberately meaningful in the
        attribution tests: it is *not* the order the DOIs were queried in.
    pmid_by_doi:
        Alternative to ``esearch_ids`` for the batching tests: ``esearch`` then
        answers only for the DOIs that appear in *that request's own* term,
        the way NCBI does. A stub that returned every PMID to every query
        would keep a batching bug green, because the DOIs that were never
        searched for would still come back resolved. ``doi_by_pmid`` is
        derived from this when not given explicitly.
    doi_by_pmid:
        The DOI each summary carries in its own ``articleids``. A PMID absent
        from this mapping gets a summary with no DOI at all, which is what
        PubMed returns for a citation that was never assigned one.
    medline:
        The ``efetch`` body. ``None`` stands for a confirmed HTTP 404.
    summary_uid_order:
        ``result.uids`` order in the esummary payload, when it should differ
        from the order the PMIDs were requested in.
    unreachable:
        Endpoint filename (e.g. ``"efetch.fcgi"``) whose request raises
        :class:`~bibaudit.registries.http.Transient`, as `Client` does once
        its retry budget is spent.
    absent:
        Endpoint filename whose request answers ``None`` — `Client`'s way of
        reporting a confirmed HTTP 404, which is a fact and not an outage.
    """

    def __init__(
        self,
        *,
        esearch_ids: Sequence[str] = (),
        pmid_by_doi: Mapping[str, str] | None = None,
        doi_by_pmid: Mapping[str, str] | None = None,
        medline: str | None = "",
        summary_uid_order: Sequence[str] | None = None,
        clock: _FakeClock | None = None,
        unreachable: str | None = None,
        absent: str | None = None,
    ) -> None:
        self.esearch_ids = list(esearch_ids)
        self.pmid_by_doi = None if pmid_by_doi is None else dict(pmid_by_doi)
        if doi_by_pmid is None and pmid_by_doi is not None:
            doi_by_pmid = {pmid: doi for doi, pmid in pmid_by_doi.items()}
        self.doi_by_pmid = dict(doi_by_pmid or {})
        self.medline = medline
        self.summary_uid_order = None if summary_uid_order is None else list(summary_uid_order)
        self.clock = clock
        self.unreachable = unreachable
        self.absent = absent
        self.urls: list[str] = []
        #: Reading of the fake clock at each request, for the pacing test.
        self.request_times: list[float] = []

    def _note(self, url: str) -> None:
        self.urls.append(url)
        if self.clock is not None:
            self.request_times.append(self.clock.monotonic())
        if self.unreachable is not None and self.unreachable in url:
            raise Transient(f"{url}: registry unreachable after 5 attempts")

    def get_json(
        self, url: str, *, cache_key: str | None = None, headers: dict[str, str] | None = None
    ) -> dict[str, Any] | None:
        self._note(url)
        if self.absent is not None and self.absent in url:
            return None
        if "esearch.fcgi" in url:
            idlist = self._esearch_idlist(url)
            return {
                "header": {"type": "esearch", "version": "0.3"},
                "esearchresult": {
                    "count": str(len(idlist)),
                    "retmax": _params(url).get("retmax", "100"),
                    "idlist": idlist,
                },
            }
        if "esummary.fcgi" in url:
            return {"header": {"type": "esummary"}, "result": self._summaries(url)}
        raise AssertionError(f"unexpected JSON request: {url}")

    def _esearch_idlist(self, url: str) -> list[str]:
        """PMIDs this particular ``esearch`` query is entitled to answer with."""
        if self.pmid_by_doi is None:
            return list(self.esearch_ids)
        term = _params(url)["term"]
        # The DOI is matched with its surrounding quotes so "10.1000/x.1" does
        # not also match the term for "10.1000/x.11".
        return [pmid for doi, pmid in self.pmid_by_doi.items() if f'"{doi}"[aid]' in term]

    def _summaries(self, url: str) -> dict[str, Any]:
        requested = _params(url)["id"].split(",")
        uids = self.summary_uid_order if self.summary_uid_order is not None else requested
        result: dict[str, Any] = {"uids": list(uids)}
        for uid in uids:
            # `pii` is listed first on purpose: the DOI has to be found by its
            # `idtype`, never by taking articleids[0], which for a great many
            # PubMed records is the pii or the PMID itself.
            article_ids: list[dict[str, Any]] = [
                {"idtype": "pii", "idtypen": 4, "value": f"S0140-6736({uid})00000-0"},
                {"idtype": "pubmed", "idtypen": 1, "value": uid},
            ]
            doi = self.doi_by_pmid.get(uid)
            if doi is not None:
                article_ids.insert(1, {"idtype": "doi", "idtypen": 3, "value": doi})
            result[uid] = {"uid": uid, "source": "Lancet", "articleids": article_ids}
        return result

    def get_text(
        self, url: str, *, cache_key: str | None = None, headers: dict[str, str] | None = None
    ) -> str | None:
        self._note(url)
        assert "efetch.fcgi" in url, f"unexpected text request: {url}"
        return self.medline


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    """Replace ``pubmed``'s view of ``time`` for every test in this module.

    Autouse because the 3 requests/second pacing is real and applies to every
    call: without this, each test below would spend two thirds of a second
    asleep. :class:`TestRequestPacing` asks for this fixture by name and reads
    the pacing back off it.
    """
    fake = _FakeClock()
    monkeypatch.setattr(pubmed, "time", fake)
    return fake


def _resolve_one(fixture_name: str, *, pmid: str, doi: str) -> Record:
    """Run the real ``by_dois`` pipeline over one recorded MEDLINE fixture.

    Parsing is exercised through the public entry point rather than through
    the module's private helpers, so these tests also fail if the
    esearch -> esummary -> efetch wiring stops attaching a parsed record to
    the DOI that was asked for.
    """
    client = _StubClient(
        esearch_ids=[pmid],
        doi_by_pmid={pmid: doi},
        medline=_fixture(fixture_name),
    )
    return PubMed(client).by_dois([doi])[normalize_doi(doi)]


class TestMedlineWrapping:
    """Continuation lines are part of the field above them, not noise."""

    def test_title_wrapped_across_continuation_lines_is_parsed_whole(self) -> None:
        """The bug that turns a correct entry into a title mismatch.

        ``efetch`` breaks this title over three lines, the second and third
        indented six spaces with no tag of their own. A parser that reads only
        tagged lines keeps "... risk in the MCC-Spain" and drops the rest;
        `compare._check_title` then scores that against the bibliography's
        full title and reports a mismatch — or, below 0.55, WRONG-WORK — on a
        reference that is entirely correct.
        """
        record = _resolve_one("wrapped_title", pmid="28338828", doi="10.1093/aje/kwx137")
        assert record.title == (
            "Night shift work, chronotype, and colorectal cancer risk in the MCC-Spain "
            "case-control study: a population-based multicase-control study of common "
            "tumors in Spain"
        )

    def test_a_wrapped_author_name_is_one_author_not_two(self) -> None:
        """A consortium byline is long enough to wrap, and it is one creator.

        Treating the continuation line as a new ``FAU`` value would give this
        record five authors instead of three and report an author-count defect
        against a bibliography that has it right.
        """
        record = _resolve_one("wrapped_authors", pmid="33069326", doi="10.1371/journal.pone.0240506")
        assert len(record.authors) == 3
        assert record.authors[2].collective
        assert str(record.authors[2]) == (
            "MCC-Spain Multi Case-Control Study Group of the Consortium for "
            "Biomedical Research in Epidemiology and Public Health"
        )

    def test_a_wrapped_affiliation_never_leaks_into_an_author(self) -> None:
        """``AD`` sits between ``FAU`` lines and wraps too.

        Its continuation lines are the most common wrapped text in a MEDLINE
        record; appending them to whatever field was last seen instead of to
        ``AD`` would invent an author called "08003 Barcelona, Spain."
        """
        record = _resolve_one("wrapped_authors", pmid="33069326", doi="10.1371/journal.pone.0240506")
        # The list is asserted non-empty first. "No author mentions Barcelona"
        # is trivially true of an empty list, so on its own this test stayed
        # green for a parser that produced no authors at all.
        assert len(record.authors) == 3
        assert not any("Barcelona" in str(name) for name in record.authors)
        # And the continuation line went somewhere: onto ``AD``, whole.
        assert record.raw["AD"][0].endswith("08003 Barcelona, Spain.")

    def test_the_authors_before_and_after_a_wrap_are_intact(self) -> None:
        record = _resolve_one("wrapped_authors", pmid="33069326", doi="10.1371/journal.pone.0240506")
        assert [(n.family, n.given) for n in record.authors[:2]] == [
            ("Espinosa", "Ana"),
            ("Kogevinas", "Manolis"),
        ]


class TestTitleCleanup:
    def test_medline_house_style_period_is_stripped(self) -> None:
        """Every MEDLINE ``TI`` ends in a period that no other registry keeps.

        Leaving it makes `compare._check_title` print a cosmetic difference
        for every single PubMed-corroborated entry in a bibliography.
        """
        record = _resolve_one("wrapped_title", pmid="28338828", doi="10.1093/aje/kwx137")
        # The surviving tail is named rather than merely asserting the title
        # does not end in a period: a parser that truncated the title at the
        # first continuation line also satisfies `not endswith(".")`, and this
        # test used to pass while the title was wrong in a far worse way.
        assert record.title.endswith("multicase-control study of common tumors in Spain")

    def test_a_title_ending_in_an_abbreviation_keeps_its_period(self) -> None:
        """NLM does not double the period after "U.S." — one serves both.

        Stripping it yields "... mortality in the U.S", which the folded
        comparison forgives but the report does not: `compare._check_title`
        emits a cosmetic issue whenever the folded titles agree and the
        display strings differ, and `--suggest` would offer the mangled
        spelling as a replacement for the entry's correct one.
        """
        record = _resolve_one("abbreviated_title", pmid="27532363", doi="10.1002/ajim.22619")
        assert record.title == "Occupational exposures and the burden of cancer mortality in the U.S."

    def test_bracketed_translated_title_is_unwrapped(self) -> None:
        """PubMed brackets its English gloss of a non-English title.

        The brackets are NLM notation, not part of the title; a bibliography
        stores the gloss without them, so keeping them reports a difference on
        every French, German or Spanish citation.
        """
        record = _resolve_one(
            "translated_title", pmid="15455608", doi="10.1016/s0398-7620(04)99012-3"
        )
        assert record.title == (
            "Occupational exposure to pesticides and risk of cancer among agricultural workers"
        )

    def test_a_translated_title_is_marked_as_such_in_raw(self) -> None:
        """The report needs to say the registry title is a translation.

        The article's own title is French (``LA  - fre``, with the vernacular
        text in ``TT``), so an entry whose title is the French one is not
        wrong even though it matches nothing in ``TI``.
        """
        record = _resolve_one(
            "translated_title", pmid="15455608", doi="10.1016/s0398-7620(04)99012-3"
        )
        assert record.raw["translated"] is True

    def test_an_english_title_is_not_marked_as_translated(self) -> None:
        record = _resolve_one("wrapped_title", pmid="28338828", doi="10.1093/aje/kwx137")
        # `raw` is shown to be populated first: "key absent" is vacuously true
        # of a record that carried no raw fields at all.
        assert record.raw["TI"]
        assert "translated" not in record.raw


class TestAuthors:
    def test_fau_is_preferred_over_au(self) -> None:
        """``FAU`` carries the full forename and an unambiguous comma order.

        ``AU`` for the same person is "Papantoniou K": initials only, and no
        comma to say which half is the surname. Reading ``AU`` when ``FAU`` is
        present throws away the forename the comparison could have used.
        """
        record = _resolve_one("wrapped_title", pmid="28338828", doi="10.1093/aje/kwx137")
        assert (record.authors[0].family, record.authors[0].given) == ("Papantoniou", "Kyriaki")

    def test_au_only_record_keeps_medline_surname_first_order(self) -> None:
        """Pre-2002 citations have no ``FAU`` at all, only "van Eijck CH".

        That is surname-then-initials, the opposite of the "Given Family"
        order a comma-less string means in BibTeX. Parsing it as BibTeX would
        make the surname "CH" and fail the author check on every author of
        every older citation.
        """
        record = _resolve_one("au_only", pmid="7912306", doi="10.1016/s0140-6736(94)92543-x")
        assert (record.authors[0].family, record.authors[0].given) == ("van Eijck", "CH")

    def test_a_collective_author_in_au_is_not_split_into_a_person(self) -> None:
        """"Dutch Colorectal Cancer Group" is one organisation.

        The surname-first repair inserts a comma before the last token, so
        applying it here first would yield family "Dutch Colorectal Cancer",
        given "Group" — a person who does not exist, compared against a real
        surname.
        """
        record = _resolve_one("au_only", pmid="7912306", doi="10.1016/s0140-6736(94)92543-x")
        assert record.authors[2].collective
        assert str(record.authors[2]) == "Dutch Colorectal Cancer Group"


class TestRetractionSignals:
    """``PT`` says which side of a retraction a record is on. Both directions.

    NLM curates this independently of the publisher's Crossref deposit, which
    is the entire reason PubMed is consulted, so this is a second source for
    the one verdict that must never be missed.
    """

    def test_retracted_publication_marks_the_record_retracted(self) -> None:
        """PMID 9500320 (Wakefield et al., Lancet 1998) carries
        ``PT  - Retracted Publication``.
        """
        record = _resolve_one("retracted", pmid=WAKEFIELD_PMID, doi=WAKEFIELD_DOI)
        assert record.retracted is True
        assert record.retraction_kind == "Retracted Publication"

    def test_retraction_of_publication_does_not_mark_the_notice_retracted(self) -> None:
        """PMID 20137807 is the Lancet's notice, not a defective paper.

        It carries ``PT  - Retraction of Publication``: one word away from the
        value above and the opposite meaning. Any check looser than an exact
        match — a substring test for "retract", the obvious shortcut — reports
        this citable notice as retracted work while clearing the paper it
        retracted.
        """
        record = _resolve_one(
            "retraction_notice", pmid=RETRACTION_NOTICE_PMID, doi=RETRACTION_NOTICE_DOI
        )
        assert record.retracted is False
        assert record.retraction_kind is None

    def test_an_ordinary_article_is_not_retracted(self) -> None:
        """Guards the other tail: a ``PT`` list of ordinary types.

        This record's types include "Research Support, Non-U.S. Gov't", which
        a sloppy match on any ``PT`` containing "Retract"-adjacent text or on
        the presence of the tag at all would happily flag.
        """
        record = _resolve_one("wrapped_title", pmid="28338828", doi="10.1093/aje/kwx137")
        assert record.retracted is False
        assert record.retraction_kind is None

    def test_every_publication_type_is_kept_in_raw(self) -> None:
        """``PT`` is repeatable; keeping only the first loses the signal.

        On the Wakefield record "Journal Article" comes before "Retracted
        Publication", so a parser that stores one value per tag reports it as
        an ordinary article.
        """
        record = _resolve_one("retracted", pmid=WAKEFIELD_PMID, doi=WAKEFIELD_DOI)
        assert record.raw["PT"] == ["Journal Article", "Retracted Publication"]


class TestDoiAttribution:
    """A PMID is attributed by the summary's own DOI, never by position."""

    def test_pmids_are_matched_by_articleids_not_by_result_order(self) -> None:
        """Three orders in play here, and no two of them agree.

        The DOIs are queried as [paper, notice]; ``esearch`` answers
        [notice, paper]; and ``esummary`` is asked in sorted-PMID order, where
        "20137807" sorts before "9500320". Pairing candidates positionally
        anywhere in that chain hands the retracted paper's DOI the retraction
        notice's PMID — and with it the notice's title, pages and (crucially)
        its clean retraction status.
        """
        client = _StubClient(
            esearch_ids=[RETRACTION_NOTICE_PMID, WAKEFIELD_PMID],
            doi_by_pmid={
                WAKEFIELD_PMID: WAKEFIELD_DOI,
                RETRACTION_NOTICE_PMID: RETRACTION_NOTICE_DOI,
            },
            summary_uid_order=[WAKEFIELD_PMID, RETRACTION_NOTICE_PMID],
        )
        mapping = PubMed(client).pmids_for([WAKEFIELD_DOI, RETRACTION_NOTICE_DOI])
        assert mapping == {
            normalize_doi(WAKEFIELD_DOI): WAKEFIELD_PMID,
            normalize_doi(RETRACTION_NOTICE_DOI): RETRACTION_NOTICE_PMID,
        }

    def test_a_candidate_whose_own_doi_was_not_asked_for_is_dropped(self) -> None:
        """One ``esearch`` query ORs a whole batch of DOIs together.

        Its result is a pool of candidates for the batch, not an answer for
        any one DOI, and it can contain a record that merely cites the DOI in
        its own article-id list. Such a candidate must be discarded, not
        attributed to whichever DOI is at hand.
        """
        client = _StubClient(
            esearch_ids=[WAKEFIELD_PMID, RETRACTION_NOTICE_PMID],
            doi_by_pmid={
                WAKEFIELD_PMID: WAKEFIELD_DOI,
                RETRACTION_NOTICE_PMID: RETRACTION_NOTICE_DOI,
            },
        )
        mapping = PubMed(client).pmids_for([WAKEFIELD_DOI])
        assert mapping == {normalize_doi(WAKEFIELD_DOI): WAKEFIELD_PMID}

    def test_a_doi_with_no_pmid_is_absent_rather_than_an_error(self) -> None:
        """Most of the world is not in PubMed, and that is not a defect.

        A Zenodo DOI has no PMID; the biomedical DOI queried alongside it must
        still resolve, and neither a KeyError nor a fabricated pairing may
        come out of the gap.
        """
        client = _StubClient(
            esearch_ids=[WAKEFIELD_PMID],
            doi_by_pmid={WAKEFIELD_PMID: WAKEFIELD_DOI},
            medline=_fixture("retracted"),
        )
        result = PubMed(client).by_dois([WAKEFIELD_DOI, "10.5281/zenodo.1234567"])
        assert set(result) == {normalize_doi(WAKEFIELD_DOI)}

    def test_a_summary_carrying_no_doi_at_all_is_skipped(self) -> None:
        """Some PubMed citations were never assigned a DOI.

        Their summaries have ``articleids`` without a ``doi`` entry, and there
        is then nothing to attribute the PMID to.
        """
        client = _StubClient(esearch_ids=[WAKEFIELD_PMID], doi_by_pmid={})
        assert PubMed(client).pmids_for([WAKEFIELD_DOI]) == {}

    def test_no_candidates_means_no_further_requests(self) -> None:
        """``esearch`` finding nothing ends the pipeline there.

        Asking ``esummary`` and ``efetch`` for an empty id list would spend
        two of the three requests a second NCBI allows on questions with no
        possible answer.
        """
        client = _StubClient(esearch_ids=[])
        assert PubMed(client).by_dois(["10.5281/zenodo.1234567"]) == {}
        assert len(client.urls) == 1
        assert "esearch.fcgi" in client.urls[0]

    def test_the_esearch_term_keeps_a_doi_containing_parentheses_whole(self) -> None:
        """``10.1016/S0140-6736(97)11096-0`` is a real Lancet DOI.

        Truncating it at the bracket produces a term that matches nothing, and
        the reference is then reported as absent from PubMed rather than
        found — a fabricated citation and a Lancet citation look identical
        from there.
        """
        client = _StubClient(esearch_ids=[])
        PubMed(client).pmids_for([WAKEFIELD_DOI])
        assert _params(client.urls[0])["term"] == '("10.1016/s0140-6736(97)11096-0"[aid])'

    def test_blank_dois_make_no_request(self) -> None:
        """An adapter yields ``None``/``""`` for an entry with no DOI field."""
        client = _StubClient(esearch_ids=[])
        assert PubMed(client).by_dois(["", "   "]) == {}
        assert client.urls == []


class TestByDois:
    def test_each_record_in_a_batch_lands_on_its_own_doi(self) -> None:
        """One ``efetch`` body, two records, separated by a blank line.

        This is the whole pipeline end to end on the pair that matters: the
        retracted paper and the notice that retracted it, deliberately
        attributed in an order no positional pairing would reproduce. If they
        swap, the retracted paper reports as clean.
        """
        client = _StubClient(
            esearch_ids=[RETRACTION_NOTICE_PMID, WAKEFIELD_PMID],
            doi_by_pmid={
                WAKEFIELD_PMID: WAKEFIELD_DOI,
                RETRACTION_NOTICE_PMID: RETRACTION_NOTICE_DOI,
            },
            medline=f"{_fixture('retraction_notice')}\n{_fixture('retracted')}",
        )
        result = PubMed(client).by_dois([WAKEFIELD_DOI, RETRACTION_NOTICE_DOI])

        paper = result[normalize_doi(WAKEFIELD_DOI)]
        notice = result[normalize_doi(RETRACTION_NOTICE_DOI)]
        assert paper.title.startswith("Ileal-lymphoid-nodular hyperplasia")
        assert notice.title.startswith("Retraction--Ileal-lymphoid-nodular hyperplasia")
        assert (paper.retracted, notice.retracted) == (True, False)

    def test_the_record_carries_the_doi_it_was_looked_up_by(self) -> None:
        """MEDLINE text does not contain a parsed DOI field.

        The DOI on the record is the one that resolved to this PMID, so
        `compare` can key it against the reference; leaving it unset would
        make every PubMed record anonymous to the caller.
        """
        record = _resolve_one("retracted", pmid=WAKEFIELD_PMID, doi=WAKEFIELD_DOI)
        assert record.doi == normalize_doi(WAKEFIELD_DOI)

    def test_journal_title_and_iso_abbreviation_stay_in_separate_fields(self) -> None:
        """``JT`` is the journal, ``TA`` its NLM abbreviation.

        Swapping them makes every container comparison read against "Am J
        Epidemiol", and `benign._container_abbreviation` is written the other
        way round — it accepts an abbreviated *stored* value against a full
        registry one.
        """
        record = _resolve_one("wrapped_title", pmid="28338828", doi="10.1093/aje/kwx137")
        assert record.container == "American journal of epidemiology"
        assert record.container_short == "Am J Epidemiol"

    def test_volume_issue_pages_and_year_are_read_from_their_own_tags(self) -> None:
        record = _resolve_one("wrapped_title", pmid="28338828", doi="10.1093/aje/kwx137")
        assert (record.volume, record.issue, record.pages) == ("185", "12", "1265-1274")
        assert record.years == {"issued": 2017}

    def test_a_record_for_a_pmid_nobody_asked_for_is_discarded(self) -> None:
        """``efetch``'s body is attributed by each record's own ``PMID`` line.

        PubMed merges duplicate citations, so a request for a retired PMID can
        come back as the surviving record under a *different* number. Taking
        whatever the body contains and pinning it on the DOI at hand is the
        same positional-pairing mistake ``esummary`` exists to prevent, one
        step later in the pipeline — and here it would hand a reference the
        metadata, and the retraction status, of an unrelated paper.

        The unrequested record is placed *after* the wanted one on purpose: a
        parser that attributes it anyway overwrites the correct answer, which
        a body ordered the other way round would hide.
        """
        client = _StubClient(
            esearch_ids=[WAKEFIELD_PMID],
            doi_by_pmid={WAKEFIELD_PMID: WAKEFIELD_DOI},
            # PMID 28338828 is in the body but was never asked for.
            medline=f"{_fixture('retracted')}\n{_fixture('wrapped_title')}",
        )
        result = PubMed(client).by_dois([WAKEFIELD_DOI])

        assert set(result) == {normalize_doi(WAKEFIELD_DOI)}
        assert result[normalize_doi(WAKEFIELD_DOI)].title.startswith(
            "Ileal-lymphoid-nodular hyperplasia"
        )

    def test_efetch_answering_404_yields_no_records_rather_than_raising(self) -> None:
        """``None`` from the client is a confirmed 404, not an outage.

        An outage arrives as `Transient` and propagates out of `by_dois` on
        purpose, so a batch is never half-reported; a 404 here just means
        there is nothing to compare against.
        """
        client = _StubClient(
            esearch_ids=[WAKEFIELD_PMID],
            doi_by_pmid={WAKEFIELD_PMID: WAKEFIELD_DOI},
            medline=None,
        )
        assert PubMed(client).by_dois([WAKEFIELD_DOI]) == {}


class TestEsearchBatching:
    """A bibliography is searched for in ``OR``-ed batches of twenty DOIs."""

    def test_dois_past_the_first_batch_are_still_searched_for(self) -> None:
        """A batching bug does not announce itself.

        The DOIs past the first batch are simply never queried, come back with
        no PMID, and are then reported as absent from PubMed — which is the
        shape a fabricated citation takes. Forty-five correct references would
        be reported as twenty found and twenty-five unconfirmed.

        The stub answers each ``esearch`` only for the DOIs in *that* query's
        term, so this cannot pass by the fake handing back every PMID
        regardless of what was asked.
        """
        pmid_by_doi = {f"10.1000/pubmedbatch.{i}": str(30000000 + i) for i in range(45)}
        client = _StubClient(pmid_by_doi=pmid_by_doi)

        assert PubMed(client).pmids_for(list(pmid_by_doi)) == pmid_by_doi

        searches = [url for url in client.urls if "esearch.fcgi" in url]
        assert [_params(url)["term"].count("[aid]") for url in searches] == [20, 20, 5]

    def test_a_repeated_doi_is_queried_once(self) -> None:
        """The same work cited twice in a manuscript is one lookup.

        Deduplicating after the request instead of before it spends NCBI's
        three-a-second budget on a question already asked.
        """
        client = _StubClient(
            esearch_ids=[WAKEFIELD_PMID], doi_by_pmid={WAKEFIELD_PMID: WAKEFIELD_DOI}
        )
        PubMed(client).pmids_for([WAKEFIELD_DOI, WAKEFIELD_DOI.upper(), WAKEFIELD_DOI])
        assert _params(client.urls[0])["term"].count("[aid]") == 1


class TestOutageIsNotAbsence:
    """A registry that could not be asked has said nothing about the batch.

    ``by_dois`` answers for a whole batch at once, so swallowing a `Transient`
    would report every DOI in that batch as "PubMed does not have this" — the
    shape of a fabricated citation — because a network was down. The exception
    propagates; `audit.resolve` catches it and marks the registry unreachable.
    """

    def test_an_esearch_outage_propagates(self) -> None:
        client = _StubClient(esearch_ids=[WAKEFIELD_PMID], unreachable="esearch.fcgi")
        with pytest.raises(Transient):
            PubMed(client).by_dois([WAKEFIELD_DOI])

    def test_an_efetch_outage_propagates_rather_than_dropping_the_batch(self) -> None:
        """The DOIs already resolved to PMIDs are not evidence of anything.

        By this point ``esearch`` and ``esummary`` have succeeded, so it is
        tempting to return what is known and move on; but what is known is a
        set of PMIDs with no metadata to compare against, and reporting that
        as "PubMed had nothing to add" hides an outage inside a verdict.
        """
        client = _StubClient(
            esearch_ids=[WAKEFIELD_PMID],
            doi_by_pmid={WAKEFIELD_PMID: WAKEFIELD_DOI},
            unreachable="efetch.fcgi",
        )
        with pytest.raises(Transient):
            PubMed(client).by_dois([WAKEFIELD_DOI])

    def test_a_404_from_esearch_is_an_answer_not_an_outage(self) -> None:
        """``None`` is a confirmed HTTP 404 and means "no such record".

        It must come back as an empty result, never as `Transient`: a
        reference PubMed genuinely does not hold is a fact the report is
        entitled to state, and turning it into UNCHECKED would hide it.
        """
        client = _StubClient(absent="esearch.fcgi")
        assert PubMed(client).by_dois([WAKEFIELD_DOI]) == {}


class TestRequestPacing:
    """NCBI allows three E-utilities requests a second without an API key."""

    def test_requests_are_spaced_by_at_least_a_third_of_a_second(
        self, clock: _FakeClock
    ) -> None:
        """The cap covers all three endpoints together, not each separately.

        One ``by_dois`` call issues an ``esearch``, an ``esummary`` and an
        ``efetch`` back to back; NCBI counts those against one budget, and
        exceeding it gets a caller's IP blocked, which turns every reference
        in the run into an unverifiable one. `Client`'s own per-host throttle
        cannot be relied on for this — it is configurable and exists for
        Crossref's sake — so `PubMed` paces itself.
        """
        client = _StubClient(
            esearch_ids=[WAKEFIELD_PMID],
            doi_by_pmid={WAKEFIELD_PMID: WAKEFIELD_DOI},
            medline=_fixture("retracted"),
            clock=clock,
        )
        PubMed(client).by_dois([WAKEFIELD_DOI])

        assert len(client.request_times) == 3
        gaps = [
            later - earlier
            for earlier, later in zip(client.request_times, client.request_times[1:], strict=False)
        ]
        assert all(gap >= 1 / 3 for gap in gaps), gaps

    def test_no_api_key_is_sent(self) -> None:
        """The 3/s pacing above is the *unauthenticated* ceiling.

        An API key raises it to 10/s, so a key must never appear without the
        pacing being revisited in the same change — and a key hard-coded into
        this module would be a credential in a public repository besides.
        """
        client = _StubClient(
            esearch_ids=[WAKEFIELD_PMID],
            doi_by_pmid={WAKEFIELD_PMID: WAKEFIELD_DOI},
            medline=_fixture("retracted"),
        )
        PubMed(client).by_dois([WAKEFIELD_DOI])
        assert client.urls
        assert all("api_key" not in url for url in client.urls)
