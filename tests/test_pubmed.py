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
notice that retracted it; PMID 37726507, recorded verbatim, in
``pubmed_wrapped_title.txt``); PMIDs and dates elsewhere are join keys.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from bibaudit.model import Name, Record
from bibaudit.names import names_agree
from bibaudit.normalize import normalize_doi
from bibaudit.registries import pubmed
from bibaudit.registries.http import Transient
from bibaudit.registries.pubmed import PmidAnswers, PubMed

DATA = Path(__file__).parent / "data"

#: The retracted/notice pair used throughout. Note the parentheses: a DOI
#: regex that stops at "(" truncates both of these, which is why
#: `normalize.DOI_PATTERN` allows them and why nothing here writes its own.
WAKEFIELD_DOI = "10.1016/S0140-6736(97)11096-0"
WAKEFIELD_PMID = "9500320"
RETRACTION_NOTICE_DOI = "10.1016/S0140-6736(10)60175-4"
RETRACTION_NOTICE_PMID = "20137807"

#: The citation every wrapping test reads, recorded verbatim in
#: ``tests/data/pubmed_wrapped_title.txt``: Donat-Vargas et al., *J Expo Sci
#: Environ Epidemiol* 2024. Its ``TI`` is 192 characters, so efetch breaks it
#: over three lines — one tagged, two indented continuations.
WRAPPED_PMID = "37726507"
WRAPPED_DOI = "10.1038/s41370-023-00600-7"


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
    efetch_bodies:
        One body per ``efetch`` request, in request order, for the tests where
        the point is that batches answer *differently* — one holding a
        citation, the next 404ing, a third coming back with a record nobody in
        it asked for. ``medline`` answers every request when this is not given,
        which cannot express any of those.
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
        efetch_bodies: Sequence[str | None] | None = None,
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
        self.efetch_bodies = None if efetch_bodies is None else list(efetch_bodies)
        self.efetch_calls = 0
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
        if self.efetch_bodies is None:
            return self.medline
        assert self.efetch_calls < len(self.efetch_bodies), (
            "more efetch requests than this stub was given bodies for"
        )
        body = self.efetch_bodies[self.efetch_calls]
        self.efetch_calls += 1
        return body


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
        tagged lines keeps "... and swimming" and drops the rest;
        `compare._check_title` then scores that against the bibliography's
        full title and reports a mismatch — or, below 0.55, WRONG-WORK — on a
        reference that is entirely correct.
        """
        record = _resolve_one("wrapped_title", pmid=WRAPPED_PMID, doi=WRAPPED_DOI)
        assert record.title == (
            "Lifetime exposure to brominated trihalomethanes in drinking water and "
            "swimming pool attendance are associated with chronic lymphocytic "
            "leukemia: a Multicase-Control Study in Spain (MCC-Spain)"
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
        record = _resolve_one("wrapped_title", pmid=WRAPPED_PMID, doi=WRAPPED_DOI)
        # The surviving tail is named rather than merely asserting the title
        # does not end in a period: a parser that truncated the title at the
        # first continuation line also satisfies `not endswith(".")`, and this
        # test used to pass while the title was wrong in a far worse way.
        assert record.title.endswith("a Multicase-Control Study in Spain (MCC-Spain)")

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
        record = _resolve_one("wrapped_title", pmid=WRAPPED_PMID, doi=WRAPPED_DOI)
        # `raw` is shown to be populated first: "key absent" is vacuously true
        # of a record that carried no raw fields at all.
        assert record.raw["TI"]
        assert "translated" not in record.raw


class TestAuthors:
    def test_fau_is_preferred_over_au(self) -> None:
        """``FAU`` carries the full forename and an unambiguous comma order.

        ``AU`` for the same person is "Donat-Vargas C": initials only, and no
        comma to say which half is the surname. Reading ``AU`` when ``FAU`` is
        present throws away the forename the comparison could have used.
        """
        record = _resolve_one("wrapped_title", pmid=WRAPPED_PMID, doi=WRAPPED_DOI)
        assert (record.authors[0].family, record.authors[0].given) == (
            "Donat-Vargas",
            "Carolina",
        )

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


    def test_an_et_al_marker_in_au_is_truncation_and_not_a_creator(self) -> None:
        """NLM writes the marker: 41 citations answer ``"et al"[au]``.

        Every one of them carries ``FAU`` as well, so this route has no
        witnessed instance. Being *recognised* is not what the guard buys —
        the synthetic comma this function inserts is deleted again by ``fold``
        before ``parse_name`` looks for the marker, so a marker reached
        through the surname-first repair is still a marker. What the guard
        keeps is the text: a report naming the creator a byline stopped at
        prints ``Name.literal``, and every value shown to a reader has to be
        the registry's own rather than one this module punctuated.
        """
        name = pubmed._parse_au_fallback("Et al")

        assert name.et_al
        assert (name.family, name.given) == ("", "")
        assert str(name) == "Et al"


class TestAFullAuthorTagWithoutItsComma:
    """``FAU`` is ``Surname, Initials``, and 10 values in 16,511 have no comma.

    Measured over a live sample of 3,500 citations drawn from five windows
    spanning 1992-2026: 9 records carry one, and each writes it character for
    character as that record's own ``AU`` line — NLM never backfilled the
    comma. Read with BibTeX's "Given Family" convention, ``Okano J`` becomes a
    creator surnamed ``J``, and a one-character registry surname is what
    ``names.Reason.REGISTRY_INITIAL_ONLY`` accepts *any* stored surname
    against. The parser was manufacturing the evidence for an escape that then
    cleared a fabricated name.

    ``tests/data/pubmed_fau_without_comma.txt`` is NCBI's own bytes for PMID
    11278851, whose two-creator byline carries one of each: ``FAU - Okano J``
    beside ``FAU - Rustgi, A K``.
    """

    def _record(self) -> Record:
        return _resolve_one(
            "fau_without_comma", pmid="11278851", doi="10.1074/jbc.M011164200"
        )

    def test_a_comma_less_value_keeps_medline_surname_first_order(self) -> None:
        record = self._record()

        assert (record.authors[0].family, record.authors[0].given) == ("Okano", "J")

    def test_the_comma_carrying_value_beside_it_is_unchanged(self) -> None:
        """The comma says which half is which, so ``parse_name`` still reads it.

        Routing every ``FAU`` through the abbreviated parser would take the
        last token as the initials block and split "Rustgi, A K" at the space
        instead of at the comma.
        """
        record = self._record()

        assert (record.authors[1].family, record.authors[1].given) == ("Rustgi", "A K")

    def test_the_manufactured_surname_no_longer_clears_a_fabricated_name(self) -> None:
        """The harm, end to end: a one-character surname agrees with anything."""
        record = self._record()

        agreed, _ = names_agree(Name(family="Zbragowitz"), record.authors[0])

        assert not agreed

    def test_an_editor_tag_is_read_by_the_same_convention(self) -> None:
        """``FED`` is ``FAU``'s tag for an edited volume and MEDLINE's, not BibTeX's."""
        names = pubmed._authors_from({"FED": ["Okano J"]})[0]

        assert (names[0].family, names[0].given) == ("Okano", "J")


class TestAnInitialWrittenAheadOfTheSurname:
    """One citation in the same sample writes the name the other way round.

    PMID 31128948 carries ``K Sikorska`` in both ``FAU`` and ``AU``, beside
    fifteen colleagues written ``Koole, S N``-fashion. ``"Sikorska K"[au]``
    answers 215 citations and ``"K Sikorska"[au]`` exactly that one, so the
    surname is Sikorska and NLM's own order is what the record breaks.

    27 of 33,026 ``FAU``/``AU`` values in the sample open with a single letter
    and the other 26 must not be rewritten, which is what the two conditions
    in ``pubmed._initials_ahead_of_the_surname`` are for.
    """

    def test_the_witnessed_value_is_read_with_the_surname_it_has(self) -> None:
        name = pubmed._parse_au_fallback("K Sikorska")

        assert (name.family, name.given) == ("Sikorska", "K")

    @pytest.mark.parametrize(
        ("value", "family", "given"),
        [
            # A surname that genuinely begins with a lone letter. Every one in
            # the sample carries its initials as a further token, so none is
            # two tokens long.
            ("A Richmond J", "A Richmond", "J"),
            ("T Rahma A", "T Rahma", "A"),
            ("E Albuquerque RP", "E Albuquerque", "RP"),
            ("W Y Chan S", "W Y Chan", "S"),
            # A two-letter surname whose transliterated initials block is not
            # capitalised. ``"Ho Yi"[au]`` answers 14 citations and is Ho, Y.
            # I., so the leading token has to be one character and not merely
            # a short one.
            ("Ho Yi", "Ho", "Yi"),
            # A surname of one or two letters with its initials after it, which
            # is MEDLINE's order already. A length test cannot tell "DMTS" from
            # "Sikorska"; the capitals can.
            ("S DMTS", "S", "DMTS"),
            ("A LK", "A", "LK"),
            ("T T", "T", "T"),
        ],
    )
    def test_a_surname_that_begins_with_one_letter_is_left_alone(
        self, value: str, family: str, given: str
    ) -> None:
        name = pubmed._parse_au_fallback(value)

        assert (name.family, name.given) == (family, given)


class TestACorporateByline:
    """``CN`` is MEDLINE's corporate author, and for some citations it is all
    there is.

    ``tests/data/pubmed_corporate_author.txt`` is NCBI's own bytes for PMID
    42538063 — a committee opinion in *Fertility and Sterility* credited to
    ``CN - Practice Committee of the American Society for Reproductive
    Medicine`` and carrying no ``FAU`` or ``AU`` line at all.
    """

    def test_a_corporate_byline_is_read_as_the_records_creator(self) -> None:
        """Left unread it is not a shorter byline, it is no byline.

        ``compare._check_authors`` returns before comparing anything when the
        record has no creators, so an entry could invent the whole byline and
        be told every checked field agrees.
        """
        record = _resolve_one(
            "corporate_author", pmid="42538063", doi="10.1016/j.fertnstert.2026.03.026"
        )
        assert [str(name) for name in record.authors] == [
            "Practice Committee of the American Society for Reproductive Medicine"
        ]

    def test_the_organisation_is_one_creator_and_keeps_its_whole_name(self) -> None:
        """Never split, and never reduced to a last token.

        This name carries marker words, but plenty of corporate bylines carry
        none — ``Frontiers Production Office`` and ``FinnGen`` are two from a
        live sample — so the tag decides that it is an organisation, not the
        words in it. Parsed as a person the surname here would be *Medicine*.
        """
        record = _resolve_one(
            "corporate_author", pmid="42538063", doi="10.1016/j.fertnstert.2026.03.026"
        )
        [creator] = record.authors
        assert creator.collective
        assert (creator.family, creator.given) == ("", "")

    def test_a_corporate_name_with_no_marker_word_is_still_one_creator(self) -> None:
        """The tag decides, not the words.

        ``names.parse_name`` reads a comma-less string as "Given Family" unless
        one of its marker words is in it, and 5 of the 35 ``CN`` values on a
        live sample of 3,000 citations carry none: PMID 42569236's whole byline
        is this one, and read as a person it is somebody surnamed *Office*.
        """
        creator = pubmed._corporate_creator("Frontiers Production Office")

        assert creator.collective
        assert (creator.family, creator.given) == ("", "")
        assert str(creator) == "Frontiers Production Office"

    def test_a_corporate_byline_is_not_reported_as_an_editor_list(self) -> None:
        """``FED``/``ED`` set a flag that tells the reader which list they are
        looking at. A corporate author is an author, and stamping the record
        with the editor flag would say the byline is the volume's editors.
        """
        record = _resolve_one(
            "corporate_author", pmid="42538063", doi="10.1016/j.fertnstert.2026.03.026"
        )
        assert "authors_source" not in record.raw

    def test_a_corporate_name_beside_a_personal_byline_is_left_alone(self) -> None:
        """PMID 42552006 writes ``CN - ITC Project Collaborators`` in the middle
        of its ``FAU`` list, and 26 of 30 sampled citations carrying ``CN``
        have it beside people that way.

        The position it belongs at is what a comparison would need, and the
        parser keeps each tag's values in order but not the tags against one
        another — so a list built by appending states a byline order the record
        does not carry. ``names._byline_collectives`` covers the case from the
        entry's side instead.
        """
        record = _resolve_one(
            "collective_creator", pmid="42552006", doi="10.1136/bmjopen-2025-107667"
        )
        assert len(record.authors) == 8
        assert not any(name.collective for name in record.authors)


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
        record = _resolve_one("wrapped_title", pmid=WRAPPED_PMID, doi=WRAPPED_DOI)
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
        mapping = PubMed(client)._pmids_by_doi([WAKEFIELD_DOI, RETRACTION_NOTICE_DOI])
        assert mapping == {
            normalize_doi(WAKEFIELD_DOI): [WAKEFIELD_PMID],
            normalize_doi(RETRACTION_NOTICE_DOI): [RETRACTION_NOTICE_PMID],
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
        mapping = PubMed(client)._pmids_by_doi([WAKEFIELD_DOI])
        assert mapping == {normalize_doi(WAKEFIELD_DOI): [WAKEFIELD_PMID]}

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
        assert PubMed(client)._pmids_by_doi([WAKEFIELD_DOI]) == {}

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
        PubMed(client)._pmids_by_doi([WAKEFIELD_DOI])
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

    def test_the_record_carries_the_pmid_it_was_fetched_under(self) -> None:
        """A DOI-resolved record still states which citation it is.

        An entry storing a PMID beside its DOI is making a second claim about
        which work it cites, and ``compare._check_pmid`` can only check it
        against something. Leaving this unset would make that claim
        uncheckable on the one path where it is worth checking — a DOI is what
        fetched the record, so the PMID is the identifier nothing verified.
        """
        record = _resolve_one("retracted", pmid=WAKEFIELD_PMID, doi=WAKEFIELD_DOI)
        assert record.pmid == WAKEFIELD_PMID

    def test_a_doi_two_pmids_claim_resolves_but_carries_no_pmid(self) -> None:
        """Two citations for one DOI make either PMID a correct thing to store.

        ``esummary`` answering with two records that both carry the queried
        DOI leaves no basis for choosing between them. The DOI still resolves —
        either record's title, authors and retraction status are the work's —
        but the PMID is dropped, because keeping the arbitrary one would let
        ``compare._check_pmid`` report a bibliography that stored the *other*
        number as disagreeing with a registry that named both.
        """
        client = _StubClient(
            esearch_ids=[WAKEFIELD_PMID, RETRACTION_NOTICE_PMID],
            doi_by_pmid={WAKEFIELD_PMID: WAKEFIELD_DOI, RETRACTION_NOTICE_PMID: WAKEFIELD_DOI},
            medline=f"{_fixture('retracted')}\n{_fixture('retraction_notice')}",
        )
        record = PubMed(client).by_dois([WAKEFIELD_DOI])[normalize_doi(WAKEFIELD_DOI)]

        assert record.pmid is None
        assert record.title.startswith("Ileal-lymphoid-nodular hyperplasia")

    def test_journal_title_and_iso_abbreviation_stay_in_separate_fields(self) -> None:
        """``JT`` is the journal, ``TA`` its NLM abbreviation.

        Swapping them makes every container comparison read against "J Expo
        Sci Environ Epidemiol", and `benign._container_abbreviation` is written
        the other way round — it accepts an abbreviated *stored* value against a
        full registry one.
        """
        record = _resolve_one("wrapped_title", pmid=WRAPPED_PMID, doi=WRAPPED_DOI)
        assert record.container == "Journal of exposure science & environmental epidemiology"
        assert record.container_short == "J Expo Sci Environ Epidemiol"

    def test_the_abbreviation_is_offered_as_an_alternate_container_too(self) -> None:
        """``TA`` is another title PubMed names for the same journal.

        NLM files a serial under a title of its own making — no leading
        article, and a place qualifier wherever the bare title would be
        ambiguous — so ``JT`` here is ``Lancet (London, England)``, which no
        bibliography stores. On the PMID path PubMed is the only registry
        consulted, leaving ``JT`` as the only container an entry could be
        compared against.
        """
        record = _resolve_one("retracted", pmid=WAKEFIELD_PMID, doi=WAKEFIELD_DOI)
        assert record.container == "Lancet (London, England)"
        assert record.container_alternates == ["Lancet"]

    def test_an_abbreviation_that_is_the_journal_title_is_not_repeated(self) -> None:
        """``PLoS One`` against ``PloS one`` is one title in two casings.

        Offering it as an alternate would put a line in the report saying the
        registry also carries a value the container check already folds to the
        one it holds.
        """
        record = _resolve_one("eci_concern", pmid="23741377", doi="10.1371/journal.pone.0064723")
        assert record.container_short == "PLoS One"
        assert record.container_alternates == []

    def test_volume_issue_pages_and_year_are_read_from_their_own_tags(self) -> None:
        record = _resolve_one("wrapped_title", pmid=WRAPPED_PMID, doi=WRAPPED_DOI)
        assert (record.volume, record.issue, record.pages) == ("34", "1", "47-57")
        # ``DP - 2024 Jan`` beside ``DEP - 20230919``. Both years are the
        # registry's own, so both reach the record.
        assert record.years == {"issued": 2024, "online": 2023}

    def test_a_record_for_a_pmid_nobody_asked_for_is_discarded(self) -> None:
        """``efetch``'s body is attributed by each record's own ``PMID`` line.

        One request carries fifty numbers and the reply is one text body, so
        which record answers for which number is a question the body itself
        has to settle. Taking whatever it contains and pinning it on the DOI
        at hand is the
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
            # PMID 37726507 is in the body but was never asked for.
            medline=f"{_fixture('retracted')}\n{_fixture('wrapped_title')}",
        )
        result = PubMed(client).by_dois([WAKEFIELD_DOI])

        assert set(result) == {normalize_doi(WAKEFIELD_DOI)}
        assert result[normalize_doi(WAKEFIELD_DOI)].title.startswith(
            "Ileal-lymphoid-nodular hyperplasia"
        )

    def test_every_citation_a_doi_came_back_under_is_fetched(self) -> None:
        """One of two was fetched, and it decided the retraction status.

        ``esummary`` attributing two PMIDs to one DOI means PubMed holds two
        citations of the work. Both describe the same paper, so which supplies
        the title and byline is arbitrary — but they need not carry the same
        ``PT``, and asking for one of them took retraction status off whichever
        number happened to sort last.
        """
        client = _StubClient(
            esearch_ids=[WAKEFIELD_PMID, WRAPPED_PMID],
            doi_by_pmid={WAKEFIELD_PMID: WAKEFIELD_DOI, WRAPPED_PMID: WAKEFIELD_DOI},
            medline=f"{_fixture('wrapped_title')}\n{_fixture('retracted')}",
        )
        PubMed(client).by_dois([WAKEFIELD_DOI])

        [efetch] = [url for url in client.urls if "efetch" in url]
        assert set(_params(efetch)["id"].split(",")) == {WAKEFIELD_PMID, WRAPPED_PMID}

    def test_the_retracted_citation_wins_the_tie(self) -> None:
        """A tie breaks towards the finding, as it does everywhere else here.

        The non-retracted citation is placed *first* in the ``efetch`` body on
        purpose: a rule that keeps whichever record arrived first reports a
        retracted paper as clean, and that is the worst miss this tool has.
        """
        client = _StubClient(
            esearch_ids=[WAKEFIELD_PMID, WRAPPED_PMID],
            doi_by_pmid={WAKEFIELD_PMID: WAKEFIELD_DOI, WRAPPED_PMID: WAKEFIELD_DOI},
            medline=f"{_fixture('wrapped_title')}\n{_fixture('retracted')}",
        )
        record = PubMed(client).by_dois([WAKEFIELD_DOI])[normalize_doi(WAKEFIELD_DOI)]

        assert record.retracted
        assert record.retraction_kind == "Retracted Publication"

    def test_the_status_crosses_over_but_the_fields_do_not(self) -> None:
        """The other thing two PMIDs for one DOI can mean.

        ``_pmids_by_doi``'s own docstring names it: one record listing another
        work's identifier among its own article ids, in the residual form where
        both claim a DOI that was asked for. Handing that record on whole
        because it is the retracted one gives the entry another work's title,
        byline, container and year — picked *because* it carries the accusation
        — and no later check recovers from the swap. The title asserted here is
        the citation that arrived first, and the retraction is still reported.
        """
        client = _StubClient(
            esearch_ids=[WAKEFIELD_PMID, WRAPPED_PMID],
            doi_by_pmid={WAKEFIELD_PMID: WAKEFIELD_DOI, WRAPPED_PMID: WAKEFIELD_DOI},
            medline=f"{_fixture('wrapped_title')}\n{_fixture('retracted')}",
        )
        record = PubMed(client).by_dois([WAKEFIELD_DOI])[normalize_doi(WAKEFIELD_DOI)]

        assert record.title is not None
        assert record.title.startswith("Lifetime exposure to brominated trihalomethanes")
        assert record.retracted

    def test_a_concern_on_the_citation_that_did_not_supply_the_fields_survives(
        self,
    ) -> None:
        """``PT`` is not the only per-citation status field.

        NLM records a concern as an ``ECI`` cross-reference on the citation it
        was raised against, and ``registries/retractions.py`` reads it off one
        record's ``raw``. Whichever citation ``efetch`` happened to return
        first therefore decided whether the concern was reported at all.
        """
        client = _StubClient(
            esearch_ids=[WRAPPED_PMID, "23741377"],
            doi_by_pmid={WRAPPED_PMID: WAKEFIELD_DOI, "23741377": WAKEFIELD_DOI},
            medline=f"{_fixture('wrapped_title')}\n{_fixture('eci_concern')}",
        )
        record = PubMed(client).by_dois([WAKEFIELD_DOI])[normalize_doi(WAKEFIELD_DOI)]

        assert record.title is not None
        assert record.title.startswith("Lifetime exposure to brominated trihalomethanes")
        assert "ECI" in record.raw

    def test_an_ambiguous_doi_still_carries_no_pmid(self) -> None:
        """Which citation supplied the fields is arbitrary, and stays unstated.

        ``compare._check_pmid`` would otherwise report a bibliography storing
        the number that lost the tie as disagreeing with PubMed, when PubMed
        named both.
        """
        client = _StubClient(
            esearch_ids=[WAKEFIELD_PMID, WRAPPED_PMID],
            doi_by_pmid={WAKEFIELD_PMID: WAKEFIELD_DOI, WRAPPED_PMID: WAKEFIELD_DOI},
            medline=f"{_fixture('wrapped_title')}\n{_fixture('retracted')}",
        )
        record = PubMed(client).by_dois([WAKEFIELD_DOI])[normalize_doi(WAKEFIELD_DOI)]

        assert record.pmid is None

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


class TestPublicationDates:
    """A work published ahead of its issue has two dates, and MEDLINE has both.

    ``tests/data/pubmed_epub_ahead_of_issue.txt`` is PMID 41474069 (*Int J
    Cancer*, 10.1002/ijc.70265) verbatim: ``DP - 2026 May 1`` beside ``DEP -
    20251231``. A bibliography populated when the paper went online stores
    2025 — a year this record itself carries — and on the PMID path PubMed is
    the only registry there is, so a reader of ``DP`` alone has no second date
    to accept it against.
    """

    def _record(self) -> Record:
        return _resolve_one("epub_ahead_of_issue", pmid="41474069", doi="10.1002/ijc.70265")

    def test_the_electronic_publication_date_is_kept_beside_the_issue_date(self) -> None:
        assert self._record().years == {"issued": 2026, "online": 2025}

    def test_the_issue_date_is_still_the_one_the_record_prefers(self) -> None:
        """Which year the *report* names is unchanged: ``DP`` is the citation.

        ``Record.year`` is what a mismatch line prints and what
        ``benign._year_online_first`` calls the registry's preference, so
        carrying the second date must not quietly reorder the first.
        """
        assert self._record().year == 2026


class TestParallelJournalTitles:
    """``JT`` joins one serial's two names with a spaced equals sign.

    ``tests/data/pubmed_parallel_title.txt`` is PMID 42526877 verbatim: ``JT -
    Journal of preventive medicine and public health = Yebang Uihakhoe chi``
    beside ``TA - J Prev Med Public Health``. Both halves are the journal's own
    name and a bibliography stores whichever its house style uses, so the whole
    of ``JT`` was the only value a correct entry could be compared against and
    it matched neither. 607 of the 37,989 serials in NLM's own list carry one.
    """

    def _record(self) -> Record:
        client = _StubClient(medline=_fixture("parallel_title"))
        return PubMed(client).by_pmids(["42526877"]).records["42526877"]

    def test_each_name_the_registry_joins_is_offered_on_its_own(self) -> None:
        record = self._record()

        assert record.container == (
            "Journal of preventive medicine and public health = Yebang Uihakhoe chi"
        )
        assert record.container_alternates == [
            "J Prev Med Public Health",
            "Journal of preventive medicine and public health",
            "Yebang Uihakhoe chi",
        ]

    def test_a_serial_with_one_name_gains_no_extra_alternate(self) -> None:
        """The abbreviation alone, exactly as before."""
        record = _resolve_one("retracted", pmid=WAKEFIELD_PMID, doi=WAKEFIELD_DOI)

        assert record.container_alternates == ["Lancet"]


class TestATitleNlmDoesNotHold:
    """``TI - [Not Available].`` states that a field is empty, and is not a title.

    ``tests/data/pubmed_title_unavailable.txt`` is PMID 42536854 verbatim
    (10.21149/17789, a Spanish-language paper in *Salud Publica de Mexico*).
    NLM writes that placeholder where it holds no English title, on 66,776
    citations, and the record's own ``TT`` carries the Spanish title the
    bibliography stores. Compared as a title the placeholder scored 0.14
    against the entry and reported ``title/wrong-work``.
    """

    def _record(self) -> Record:
        client = _StubClient(medline=_fixture("title_unavailable"))
        return PubMed(client).by_pmids(["42536854"]).records["42536854"]

    def test_the_transliterated_title_is_read_instead(self) -> None:
        assert self._record().title == (
            "El modelo transdisciplinario de Pelayo Correa: una arquitectura "
            "conceptual para entender el cancer gastrico"
        )

    def test_an_ordinary_title_is_untouched(self) -> None:
        """The match is exact, so a title is never mistaken for the placeholder."""
        record = _resolve_one("wrapped_title", pmid=WRAPPED_PMID, doi=WRAPPED_DOI)

        assert record.title is not None
        assert record.title.startswith("Lifetime exposure to brominated")


class TestBookRecords:
    """A MEDLINE book is a citation too, and it was read as if it were an article.

    ``tests/data/pubmed_book.txt`` is PMID 20301295 verbatim, the *GeneReviews*
    volume itself: ``BTI`` and no ``TI``, ``FED``/``ED`` and no ``FAU``/``AU``.
    ``pubmed_book_chapter.txt`` is PMID 20301425, one chapter of it: ``TI`` for
    the chapter, ``BTI`` for the volume, ``FAU`` for its three authors, and
    three dates — ``DP - 1993`` (the series' start year), ``CTDT - 19980904``
    (when the contribution was filed) and ``DRDT - 20260325`` (when it was last
    revised).

    None of those tags was read, so the volume's record carried no title, no
    byline and no container at all: every check in ``compare`` returns in
    silence against an empty registry value, and an entry with a fabricated
    title, byline, container and publisher was reported ``OK``. 196 of 200
    records in a live sample of ``pubmed books[sb]`` carry ``BTI`` and no
    ``JT``; 111 of 200 carry no ``FAU`` or ``AU``.
    """

    def _book(self) -> Record:
        client = _StubClient(medline=_fixture("book"))
        return PubMed(client).by_pmids(["20301295"]).records["20301295"]

    def _chapter(self) -> Record:
        client = _StubClient(medline=_fixture("book_chapter"))
        return PubMed(client).by_pmids(["20301425"]).records["20301425"]

    def test_a_volumes_own_title_is_the_book_title_tag(self) -> None:
        assert self._book().title == "GeneReviews((R))"

    def test_a_volume_is_not_its_own_container(self) -> None:
        """``BTI`` is the title here, and a work does not appear inside itself."""
        assert self._book().container is None

    def test_a_chapter_keeps_its_own_title_and_gains_the_volume_as_container(
        self,
    ) -> None:
        chapter = self._chapter()

        assert chapter.title == (
            "BRCA1- and BRCA2-Associated Hereditary Breast and Ovarian Cancer"
        )
        assert chapter.container == "GeneReviews((R))"

    def test_a_volumes_editors_stand_in_for_the_byline_it_has_not_got(self) -> None:
        """The same fallback ``crossref._authors`` makes onto ``editor``.

        An edited volume's byline *is* its editors, and an empty author list
        is not compared against the entry's at all.
        """
        book = self._book()

        assert [str(name) for name in book.authors[:2]] == [
            "Adam, Margaret P",
            "Bick, Sarah",
        ]
        assert book.raw["authors_source"] == "editor"

    def test_a_chapters_own_authors_outrank_the_volumes_editors(self) -> None:
        """Both tag pairs are on this record, and only one of them is the byline."""
        chapter = self._chapter()

        assert [str(name) for name in chapter.authors] == [
            "Petrucelli, Nancie",
            "Daly, Mary B",
            "Pal, Tuya",
        ]
        assert "authors_source" not in chapter.raw

    def test_every_date_the_record_carries_is_kept(self) -> None:
        """``DP`` alone is the series' start year for every chapter in it."""
        assert self._chapter().years == {
            "issued": 1993,
            "contributed": 1998,
            "revised": 2026,
        }

    def test_the_series_year_is_still_the_one_the_record_prefers(self) -> None:
        """``Record.year`` is what a mismatch line prints; it must not reorder."""
        assert self._chapter().year == 1993

    def test_the_publication_type_reaches_the_record(self) -> None:
        """``PT`` is a list and most of it is not a document type.

        This record is ``PT - Review`` then ``PT - Book Chapter``: taking the
        first *recognised* value rather than the first value is what keeps a
        descriptive type from silencing ``compare._check_kind`` entirely.
        """
        assert self._chapter().kind == "Book Chapter"
        assert self._book().kind == "Book"

    def test_an_article_records_type_is_read_the_same_way(self) -> None:
        """No book-shaped special case: the rule is "the first one recognised"."""
        record = _resolve_one("wrapped_title", pmid=WRAPPED_PMID, doi=WRAPPED_DOI)

        assert record.kind == "Journal Article"

    def test_a_second_recognised_type_is_carried_too(self) -> None:
        """This record's ``PT`` list is ``Review`` then ``Book Chapter``.

        Only one of the two is recognised, so the alternates are empty and the
        chapter is a chapter and nothing else.
        """
        assert self._chapter().kind_alternates == []

    def test_the_publisher_is_deliberately_not_read(self) -> None:
        """``PB`` is on both fixtures and ``compare`` does compare a publisher.

        MEDLINE writes the place into it — "University of Washington, Seattle"
        — where a bibliography writes the house, and ``benign.py`` has no
        publisher rule of any kind to absorb the difference. Reading it would
        turn a correct ``@book`` entry into a ``FIELD-MISMATCH``; see
        ``registries/datacite.py`` for the same decision and what reversing it
        needs first.
        """
        assert self._book().publisher is None
        assert self._book().raw["PB"] == ["University of Washington, Seattle"]


class TestSeveralPublicationTypes:
    """NLM writes ``PT`` alphabetically, and sometimes means every value in it.

    ``tests/data/pubmed_data_descriptor.txt`` is PMID 42557261 verbatim, a data
    descriptor in *Scientific Data*: ``PT - Dataset`` then ``PT - Journal
    Article``. Both are structural and the work is both, but ``Dataset`` sorts
    first, so reading the first recognised value alone made the record say the
    work is a dataset and not an article. ``"dataset"[pt] AND "journal
    article"[pt]`` returns 5,670 citations.
    """

    def _descriptor(self) -> Record:
        client = _StubClient(medline=_fixture("data_descriptor"))
        return PubMed(client).by_pmids(["42557261"]).records["42557261"]

    def test_the_alphabetically_first_type_still_leads(self) -> None:
        """The record reports what NLM wrote, in NLM's order — nothing is reranked."""
        assert self._descriptor().kind == "Dataset"

    def test_the_type_further_down_the_list_is_kept(self) -> None:
        assert self._descriptor().kind_alternates == ["Journal Article"]

    def test_a_type_the_vocabulary_does_not_know_is_still_left_out(self) -> None:
        """``PT`` is mostly not a document type, and an alternate is not a dump.

        ``Randomized Controlled Trial`` normalises to ``other``, which
        ``compare._check_kind`` reads as "no opinion", so carrying it would
        offer a type that agrees with everything.
        """
        fields = pubmed._parse_medline_records(_fixture("data_descriptor"))[0]
        fields["PT"] = ["Randomized Controlled Trial", "Journal Article", "Review"]
        record = pubmed._record_from_medline(fields)

        assert (record.kind, record.kind_alternates) == ("Journal Article", [])

    def test_one_type_written_twice_is_one_type(self) -> None:
        """Two spellings normalising to the same kind tell a reader nothing twice."""
        fields = pubmed._parse_medline_records(_fixture("data_descriptor"))[0]
        fields["PT"] = ["Journal Article", "Dataset", "Journal Article"]
        record = pubmed._record_from_medline(fields)

        assert (record.kind, record.kind_alternates) == ("Journal Article", ["Dataset"])


class TestByPmids:
    """A reference that stores its own PMID needs ``efetch`` and nothing else.

    ``esearch`` and ``esummary`` exist to turn a DOI into a PMID and to
    attribute the answer back to the DOI that asked. A caller holding the PMID
    has both already, so issuing them anyway would spend two thirds of NCBI's
    three-a-second budget rediscovering what it was told.
    """

    def test_a_pmid_is_fetched_without_esearch_or_esummary(self) -> None:
        client = _StubClient(medline=_fixture("retraction_notice"))
        PubMed(client).by_pmids([RETRACTION_NOTICE_PMID])

        assert [url.split("?")[0].rsplit("/", 1)[-1] for url in client.urls] == ["efetch.fcgi"]
        assert _params(client.urls[0])["id"] == RETRACTION_NOTICE_PMID

    def test_the_record_is_keyed_by_the_pmid_it_was_asked_for(self) -> None:
        """The key is the caller's own lookup key, as ``by_dois``' is its DOI.

        ``compare`` is handed records under the identifier the reference
        stores; keying them any other way would leave every PubMed record
        anonymous to the entry that asked for it.
        """
        client = _StubClient(medline=_fixture("retraction_notice"))
        result = PubMed(client).by_pmids([RETRACTION_NOTICE_PMID]).records

        assert set(result) == {RETRACTION_NOTICE_PMID}
        assert result[RETRACTION_NOTICE_PMID].title.startswith(
            "Retraction--Ileal-lymphoid-nodular hyperplasia"
        )

    def test_the_record_states_the_pmid_it_is(self) -> None:
        """Read off the block's own ``PMID`` line, not copied from the key.

        On this path the two are equal by construction — the attribution guard
        above requires it — so the value is only worth having because it is the
        same field ``by_dois`` fills from a lookup that had no PMID to start
        with, and one builder filling it two ways is how the two paths would
        drift.
        """
        client = _StubClient(medline=_fixture("retraction_notice"))
        result = PubMed(client).by_pmids([RETRACTION_NOTICE_PMID]).records

        assert result[RETRACTION_NOTICE_PMID].pmid == RETRACTION_NOTICE_PMID

    def test_medline_retraction_flags_survive_this_path_too(self) -> None:
        """``PT`` is read by the same parser, so a PMID lookup sees it as well.

        This is the whole of the retraction evidence available to a reference
        with no DOI: Retraction Watch's export is keyed on DOI, so a PMID-only
        entry citing the Wakefield paper has MEDLINE's own flag and nothing
        else standing between it and a clean report.
        """
        client = _StubClient(medline=_fixture("retracted"))
        record = PubMed(client).by_pmids([WAKEFIELD_PMID]).records[WAKEFIELD_PMID]

        assert record.retracted
        assert record.retraction_kind == "Retracted Publication"

    def test_a_pmid_pubmed_does_not_hold_is_absent_rather_than_an_error(self) -> None:
        """An empty body is PubMed answering, and its answer is "no such record".

        That is what NCBI really returns for a number it does not hold —
        ``efetch`` for the deleted PMIDs 20000157 and 35000082 answers HTTP 200
        with nothing in it — and it is the only shape of answer that may become
        ``BAD-ID``, so it has to come back as a plain missing key: absent from
        the records and absent from ``inconclusive`` both.
        """
        client = _StubClient(medline="")
        answers = PubMed(client).by_pmids(["99999999"])

        assert answers == PmidAnswers()

    def test_a_record_under_a_number_nobody_asked_for_is_not_adopted(self) -> None:
        """Taking whatever the body contains would pin an unrelated paper's
        metadata — and its retraction status — on this reference, which is the
        same misattribution ``esummary`` exists to prevent on the DOI path.
        """
        client = _StubClient(medline=_fixture("retraction_notice"))
        answers = PubMed(client).by_pmids([WAKEFIELD_PMID])

        assert answers.records == {}

    def test_a_record_under_another_number_is_ignorance_not_absence(self) -> None:
        """The third state, and the one a single dict could not hold.

        ``efetch`` answered — with a citation, under a number nobody asked for.
        Reporting the requested number as one PubMed does not hold would make
        an accusation out of an answer: ``compare`` reads a bare missing key as
        the registry's own "no such record" and returns ``BAD-ID``. What came
        back is named so a reader can look it up.
        """
        client = _StubClient(medline=_fixture("retraction_notice"))
        answers = PubMed(client).by_pmids([WAKEFIELD_PMID])

        assert answers.inconclusive == {WAKEFIELD_PMID: (RETRACTION_NOTICE_PMID,)}

    def test_a_stray_record_leaves_the_numbers_it_did_answer_for_alone(self) -> None:
        """One batch, one usable answer, one number left unsettled.

        The stray taints only what the batch did not answer for. A record
        attributed to its own requested number is evidence like any other, and
        discarding the batch wholesale would spend a lookup to learn nothing.
        """
        client = _StubClient(
            medline=f"{_fixture('retracted')}\n{_fixture('retraction_notice')}"
        )
        answers = PubMed(client).by_pmids([WAKEFIELD_PMID, "99999999"])

        assert set(answers.records) == {WAKEFIELD_PMID}
        assert answers.inconclusive == {"99999999": (RETRACTION_NOTICE_PMID,)}

    def test_a_value_that_is_not_a_pmid_makes_no_request(self) -> None:
        """``PMC5860629`` and a zero-padded number are not lookup keys.

        Sending one would spend a request to be told nothing, and the reply
        would then read as PubMed authoritatively not holding a number PubMed
        was never really asked about.
        """
        client = _StubClient(medline=_fixture("retracted"))
        assert PubMed(client).by_pmids(["", "PMC5860629", "0" + WAKEFIELD_PMID]) == PmidAnswers()
        assert client.urls == []

    def test_a_repeated_pmid_is_fetched_once(self) -> None:
        client = _StubClient(medline=_fixture("retracted"))
        PubMed(client).by_pmids([WAKEFIELD_PMID, WAKEFIELD_PMID])
        assert _params(client.urls[0])["id"] == WAKEFIELD_PMID

    def test_pmids_past_the_first_batch_are_still_fetched_and_still_paced(
        self, clock: _FakeClock
    ) -> None:
        """``efetch`` returns whole citations, so a batch is fifty, not two hundred.

        A batching bug here is silent in the same way ``esearch``'s is: the
        PMIDs past the cut are never requested, come back with no record, and
        are reported as PubMed not holding them — the shape of a fabricated
        citation. The pacing assertion rides along because a second batch is
        the first chance this path has to exceed NCBI's ceiling.
        """
        pmids = [str(30000000 + i) for i in range(60)]
        client = _StubClient(medline="", clock=clock)

        PubMed(client).by_pmids(pmids)

        requested = [_params(url)["id"].split(",") for url in client.urls]
        assert [len(batch) for batch in requested] == [50, 10]
        assert [pmid for batch in requested for pmid in batch] == pmids
        assert client.request_times[1] - client.request_times[0] >= 1 / 3

    def test_an_efetch_outage_propagates_rather_than_reading_as_absence(self) -> None:
        """The one distinction this module exists to keep: 404 versus timeout.

        Swallowing it would report every PMID in the batch as one PubMed does
        not hold, which ``audit`` would then render as ``BAD-ID`` — a network
        problem printed as an accusation about the bibliography.
        """
        client = _StubClient(medline="", unreachable="efetch.fcgi")
        with pytest.raises(Transient):
            PubMed(client).by_pmids([WAKEFIELD_PMID])

    def test_a_404_on_the_endpoint_says_nothing_about_the_numbers_in_it(self) -> None:
        """``None`` from the client is a confirmed HTTP 404 — on the *request*.

        NCBI answers 200 with an empty body for a PMID it does not hold, so a
        404 here is the endpoint failing, not fifty numbers being absent. Read
        as absence it would condemn a whole batch at once.
        """
        client = _StubClient(medline=None)
        answers = PubMed(client).by_pmids([WAKEFIELD_PMID])

        assert answers.records == {}
        assert answers.inconclusive == {WAKEFIELD_PMID: ()}

    def _three_batches(self, monkeypatch: pytest.MonkeyPatch) -> PmidAnswers:
        """One PMID per request, and three requests answering three ways.

        ``efetch`` takes fifty numbers at a time, so a bibliography's PMIDs are
        split across several requests and each one settles only its own. Here
        the first batch answers for the number it was asked about, the second
        404s, and the third comes back holding the *first* batch's record —
        a stray, because nobody in that batch asked for it.
        """
        monkeypatch.setattr(pubmed, "_EFETCH_BATCH", 1)
        client = _StubClient(
            efetch_bodies=[_fixture("retracted"), None, _fixture("retracted")]
        )
        return PubMed(client).by_pmids(
            [WAKEFIELD_PMID, "99999999", RETRACTION_NOTICE_PMID]
        )

    def test_an_answered_number_is_not_dragged_into_another_batchs_ignorance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A batch that answered is settled, whatever the later ones do.

        Widening either ``inconclusive`` write to the whole request puts the
        answered number back into doubt and throws away the one thing the run
        did establish; widening the stray test lets a record another batch
        asked for be adopted by the batch that received it, which is the
        misattribution the whole method refuses.
        """
        answers = self._three_batches(monkeypatch)

        assert set(answers.records) == {WAKEFIELD_PMID}
        assert WAKEFIELD_PMID not in answers.inconclusive

    def test_each_unanswered_number_carries_only_its_own_batchs_answer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The 404's ignorance names nothing; the stray batch's names the stray.

        Two different states, one dict, and the reader's next move differs:
        "nothing came back" may be a rerun away from an answer, "it came back
        about 9500320 instead" is something to look up. Scoping either write to
        the request rather than the batch overwrites one with the other.
        """
        answers = self._three_batches(monkeypatch)

        assert answers.inconclusive == {
            "99999999": (),
            RETRACTION_NOTICE_PMID: (WAKEFIELD_PMID,),
        }


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

        assert PubMed(client)._pmids_by_doi(list(pmid_by_doi)) == {
            doi: [pmid] for doi, pmid in pmid_by_doi.items()
        }

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
        PubMed(client)._pmids_by_doi([WAKEFIELD_DOI, WAKEFIELD_DOI.upper(), WAKEFIELD_DOI])
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
