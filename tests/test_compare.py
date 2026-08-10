"""Verdict derivation and per-field comparison.

These run entirely offline, so they assert the *logic* rather than the state of
any registry. Any live-registry check belongs behind the ``network`` marker
declared in ``pyproject.toml``; no such test exists yet. The one test that
carries the marker is ``tests/test_http.py``'s own check that the default run
deselects it.

Most cases build their records by hand. The retraction ones do not: they replay
saved registry responses through the real ``registries.crossref`` and
``registries.pubmed`` code, because the direction rule that decides whether a
work *was* retracted or *is* the notice lives in those clients, and a hand-built
``Record`` would restate the assumption under test instead of checking it. The
payloads in ``tests/data/compare_*`` were fetched from the live APIs on
2026-08-01 and are kept verbatim, defects included.
"""

from __future__ import annotations

import ast
import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

import bibaudit
from bibaudit.compare import (
    _NO_RETRACTION_SIGNAL,
    CHECKED_FIELDS,
    Thresholds,
    compare,
    confirm_without_id,
    verdict_for,
)
from bibaudit.model import (
    ARTIFACT_KIND,
    REGISTRIES,
    Issue,
    Name,
    Record,
    Reference,
    Result,
)
from bibaudit.registries import pubmed as pubmed_client
from bibaudit.registries.crossref import Crossref


def make_ref(**overrides: object) -> Reference:
    """A correct reference, which tests then damage one field at a time."""
    base: dict[str, object] = {
        "key": "molinamontes2018family",
        "locator": "references.bib:1",
        "kind": "article",
        "doi": "10.1093/ije/dyx269",
        "title": (
            "Risk of pancreatic cancer associated with family history of cancer "
            "and other medical conditions by accounting for smoking among relatives"
        ),
        "authors": [Name(family="Molina-Montes", given="E"), Name(family="Gomez-Rubio", given="P")],
        "year": 2018,
        "container": "International Journal of Epidemiology",
        "volume": "47",
        "issue": "2",
        "pages": "473-483",
    }
    base.update(overrides)
    return Reference(**base)  # type: ignore[arg-type]


def make_record(**overrides: object) -> Record:
    base: dict[str, object] = {
        "source": "crossref",
        "doi": "10.1093/ije/dyx269",
        "title": (
            "Risk of pancreatic cancer associated with family history of cancer "
            "and other medical conditions by accounting for smoking among relatives"
        ),
        "authors": [Name(family="Molina-Montes", given="E"), Name(family="Gomez-Rubio", given="P")],
        "years": {"print": 2018},
        "container": "International Journal of Epidemiology",
        "volume": "47",
        "issue": "2",
        "pages": "473-483",
        "kind": "journal-article",
    }
    base.update(overrides)
    return Record(**base)  # type: ignore[arg-type]


class TestCleanEntry:
    def test_a_fully_correct_entry_is_ok(self) -> None:
        result = compare(make_ref(), {"crossref": make_record()})
        assert result.verdict == "OK"
        assert not result.issues
        assert not result.fails


class TestFieldMismatch:
    @pytest.mark.parametrize(
        ("field", "bad_value"),
        [
            ("volume", "48"),
            ("issue", "3"),
            ("pages", "999-1001"),
            ("container", "Journal of Something Else"),
        ],
    )
    def test_each_field_is_actually_checked(self, field: str, bad_value: str) -> None:
        """The whole premise: a wrong field fails even though the DOI resolves."""
        result = compare(make_ref(**{field: bad_value}), {"crossref": make_record()})
        assert result.verdict == "FIELD-MISMATCH"
        assert any(i.field == field and i.severity == "error" for i in result.issues)

    def test_a_wrong_year_is_an_error(self) -> None:
        result = compare(make_ref(year=2015), {"crossref": make_record()})
        assert result.verdict == "FIELD-MISMATCH"

    def test_an_invented_coauthor_is_an_error(self) -> None:
        ref = make_ref(
            authors=[Name(family="Molina-Montes"), Name(family="Fabricated"), Name(family="X")]
        )
        result = compare(ref, {"crossref": make_record()})
        assert result.verdict == "FIELD-MISMATCH"
        assert any(i.field == "authors" for i in result.issues)


class TestAnInventedCoauthorAppendedToTheByline:
    """Where a generated bibliography actually puts the invented name.

    The test above inserts it in the *middle*, which shifts every following
    position and mismatches on each. Moved to the end it was compared against
    nothing: `names.compare_author_lists` walked the two lists in step and
    stopped at the shorter one, so the only trace was an author-count warning,
    which does not fail. Appending a fabricated name to an otherwise correct
    byline produced no failing verdict on 4,255 of 4,255 live entries.
    """

    def _appended(self, *extra: Name) -> Result:
        return compare(
            make_ref(authors=[*make_record().authors, *extra]),
            {"crossref": make_record()},
        )

    def test_one_appended_name_fails_the_build(self) -> None:
        result = self._appended(Name(family="Fabricado", given="Xavier Q"))

        assert result.verdict == "FIELD-MISMATCH"
        assert result.fails

    def test_the_report_names_the_creator_rather_than_counting(self) -> None:
        """A count line names nobody, and *which* name has no witness is the
        reader's whole question."""
        result = self._appended(Name(family="Fabricado", given="Xavier Q"))

        [issue] = [i for i in result.issues if i.field == "authors"]

        assert (issue.kind, issue.severity) == ("uncorroborated", "error")
        assert issue.stored == "#3 Fabricado, Xavier Q"
        assert issue.note == "the registry's byline ends at #2"

    def test_every_appended_name_gets_its_own_line(self) -> None:
        result = self._appended(
            Name(family="Alpha", given="A"),
            Name(family="Bravo", given="B"),
            Name(family="Charlie", given="C"),
        )

        assert [i.stored for i in result.issues if i.field == "authors"] == [
            "#3 Alpha, A", "#4 Bravo, B", "#5 Charlie, C",
        ]

    def test_a_correct_byline_is_untouched(self) -> None:
        assert self._appended().verdict == "OK"

    def test_a_creator_the_corroborating_registry_names_is_not_reported(self) -> None:
        """"Uncorroborated" is a claim about every record that answered.

        Only one byline is ever compared — the primary's — so a Crossref
        deposit that stops short of the paper's real byline had the entry's
        remaining creators reported against it while PubMed, already fetched,
        named every one of them.
        """
        full = [*make_record().authors, Name(family="Kaaks", given="R")]
        result = compare(
            make_ref(authors=full),
            {"crossref": make_record(), "pubmed": make_pubmed(authors=full)},
        )

        assert result.verdict == "INCOMPLETE"
        assert not result.fails
        # What is left is the length difference, which is what it is: the
        # primary's byline is short.
        assert [i.kind for i in result.issues if i.field == "authors"] == ["count"]

    def test_a_creator_neither_registry_names_still_fails(self) -> None:
        """The counter-test: two bylines that answered, and neither has them."""
        result = compare(
            make_ref(authors=[*make_record().authors, Name(family="Fabricado", given="X")]),
            {"crossref": make_record(), "pubmed": make_pubmed()},
        )

        assert result.verdict == "FIELD-MISMATCH"
        assert [i.kind for i in result.issues if i.field == "authors"] == ["uncorroborated"]

    def test_the_registry_that_supplied_the_byline_cannot_corroborate_itself(self) -> None:
        """Where Crossref deposited no creator at all, PubMed's list *is* the
        one being compared, and asking it to corroborate its own tail would
        clear every appended name on the DOI path."""
        result = compare(
            make_ref(authors=[*make_record().authors, Name(family="Fabricado", given="X")]),
            {"crossref": make_record(authors=[]), "pubmed": make_pubmed()},
        )

        assert result.verdict == "FIELD-MISMATCH"

    def test_a_surname_the_alphabet_discards_corroborates_nobody(self) -> None:
        """One creator with no usable key must not vouch for another.

        `family_key` returns "" for every surname outside the comparison
        alphabet, so a corroborator carrying one of those would corroborate any
        stored creator carrying one too — three characters of Han script
        standing in for a name nobody checked.
        """
        result = compare(
            make_ref(authors=[*make_record().authors, Name(family="王", given="L")]),
            {
                "crossref": make_record(),
                "pubmed": make_pubmed(authors=[*make_record().authors, Name(given="C")]),
            },
        )

        assert result.verdict == "FIELD-MISMATCH"
        assert [i.kind for i in result.issues if i.field == "authors"] == ["uncorroborated"]

    def test_an_entry_that_omits_a_creator_is_still_only_incomplete(self) -> None:
        """The direction matters. A byline shorter than the registry's is a
        citation abbreviating, not one crediting somebody no record carries."""
        result = compare(
            make_ref(authors=make_record().authors[:1]), {"crossref": make_record()}
        )

        assert result.verdict == "INCOMPLETE"
        assert not result.fails


class TestACitationCreditingADifferentPersonOfTheSameName:
    """The miss this comparison exists to catch, arriving inside the name.

    `names.compare_author_lists` compared surname keys and stopped there, so an
    entry crediting `Wade, Zbigniew` where the registry names `Wade, Nicholas`
    came back `OK` with nothing recorded at any verbosity — not even a
    suppressed line `--show-suppressed` could recover. The exceptions the check
    has to survive are in `tests/test_names.py`; what is pinned here is the
    issue a reader gets.
    """

    def _miscredited(self) -> Result:
        wrong = [Name(family="Molina-Montes", given="Zbigniew"), *make_record().authors[1:]]
        return compare(make_ref(authors=wrong), {"crossref": make_record()})

    def test_it_fails_the_build(self) -> None:
        result = self._miscredited()

        assert result.verdict == "FIELD-MISMATCH"
        assert result.fails

    def test_it_is_a_kind_of_its_own_naming_both_people(self) -> None:
        """`mismatch` says the registry names somebody else at this position;
        this says the registry names this family and a different member of it —
        and a project that has decided to live with its registries' forenames
        can adjudicate one in `.bibaudit.toml` without silencing the other."""
        [issue] = [i for i in self._miscredited().issues if i.field == "authors"]

        assert (issue.kind, issue.severity) == ("forename", "error")
        assert issue.stored == "#1 Molina-Montes, Zbigniew"
        assert issue.registry == "#1 Molina-Montes, E"
        assert issue.note == "the surnames agree and the forename initials do not"

    def test_the_same_byline_with_the_forename_the_registry_holds_is_ok(self) -> None:
        assert compare(make_ref(), {"crossref": make_record()}).verdict == "OK"


class TestIdentifierProblems:
    def test_unresolvable_doi_is_bad_id(self) -> None:
        result = compare(make_ref(), {})
        assert result.verdict == "BAD-ID"
        assert result.fails

    def test_no_identifier_and_no_match_is_unconfirmed(self) -> None:
        result = compare(make_ref(doi=None), {})
        assert result.verdict == "UNCONFIRMED"
        assert result.fails

    def test_an_unreachable_registry_is_never_a_failure(self) -> None:
        """A network outage must not look like a fabricated bibliography."""
        result = compare(make_ref(), {}, unreachable={"crossref", "datacite", "pubmed"})
        assert result.verdict == "UNCHECKED"
        assert not result.fails

    def test_only_identifiers_some_registry_is_asked_about_are_ranked(self) -> None:
        """``Reference.identifier`` may rank a DOI, a PMID and an ISBN, and nothing else.

        The branch above turns a truthy ``identifier`` that resolved nowhere
        into ``BAD-ID``, "resolves in no consulted registry". An identifier no
        ``audit`` branch builds a request from resolves nowhere by
        construction, so ranking one accuses every entry carrying it about a
        value no registry was ever asked. ``Reference.arxiv`` was ranked above
        ``isbn`` while ``audit`` had no arXiv branch and no adapter populated
        the field: ``Reference(arxiv="1706.03762")`` reached this branch and
        reported ``BAD-ID`` on the identifier of a paper Crossref and PubMed
        hold under a DOI neither was handed.

        Driven off the dataclass rather than off a literal list, so the
        assertion is about what the type can carry, not about what somebody
        remembered to write down.
        """
        probe = "8675309"  # a DOI, a PMID and an ISBN are all just strings here
        optional = [
            f.name
            for f in dataclasses.fields(Reference)
            if f.default is None and f.name != "year"
        ]
        ranked = set()
        for name in optional:
            ref = Reference(key="k", locator="refs.bib:1")
            setattr(ref, name, probe)
            if ref.identifier:
                ranked.add(name)
        assert ranked == {"doi", "pmid", "isbn"}


class TestWrongWork:
    def test_different_title_and_different_authors_is_wrong_work(self) -> None:
        record = make_record(
            title="An entirely unrelated paper about marine biology",
            authors=[Name(family="Darwin"), Name(family="Wallace")],
        )
        result = compare(make_ref(), {"crossref": record})
        assert result.verdict == "WRONG-WORK"

    def test_different_title_but_matching_authors_is_not_an_accusation(self) -> None:
        """A low title score alone is not evidence the DOI points elsewhere.

        With the author list intact, a registry title defect is far likelier,
        and the tool must not escalate to WRONG-WORK on that evidence.
        """
        record = make_record(title="Comment")
        result = compare(make_ref(), {"crossref": record})
        assert result.verdict != "WRONG-WORK"


def make_pubmed(**overrides: object) -> Record:
    """PubMed's view of the same work, as ``registries.pubmed`` builds it.

    ``years`` uses MEDLINE's ``issued`` slot. There is no ``kind``, which the
    real client sets from ``PT``: the tests that exercise it drive the record
    the client builds off saved MEDLINE bytes, so the vocabulary compared is
    NLM's own rather than one this helper invented.
    """
    base: dict[str, object] = {
        "source": "pubmed",
        "doi": "10.1093/ije/dyx269",
        "title": make_record().title,
        "authors": list(make_record().authors),
        "years": {"issued": 2018},
        "container": "International Journal of Epidemiology",
        "volume": "47",
        "issue": "2",
        "pages": "473-483",
    }
    base.update(overrides)
    return Record(**base)  # type: ignore[arg-type]


class TestRetraction:
    def test_a_retracted_work_fails_even_when_every_field_is_right(self) -> None:
        record = make_record(retracted=True, retraction_kind="retraction")
        result = compare(make_ref(), {"crossref": record})
        assert result.verdict == "RETRACTED"
        assert result.fails

    def test_pubmed_alone_is_enough_to_report_a_retraction(self) -> None:
        """The miss this check exists to prevent, and the worst one available.

        Retractedness used to be read off the primary registry only — Crossref,
        or DataCite if Crossref had nothing — so PubMed's answer was discarded
        the instant Crossref replied. A paper NLM records as ``PT - Retracted
        Publication`` whose publisher never deposited the Crossref ``updated-by``
        linkage therefore passed as clean, with every field agreeing.

        That defeats the stated reason PubMed is consulted at all: it is curated
        separately by NLM, so it is exactly the source that can know something
        Crossref does not.
        """
        result = compare(
            make_ref(),
            {
                "crossref": make_record(),
                "pubmed": make_pubmed(retracted=True, retraction_kind="Retracted Publication"),
            },
        )
        assert result.verdict == "RETRACTED"
        assert result.fails

    def test_the_report_names_which_registry_asserted_it(self) -> None:
        """"Only PubMed knows this" is a different message from "both agree".

        The first is also a bug report for the publisher — the Crossref
        retraction linkage was never deposited — and a reader who cannot tell
        the two apart cannot act on either.
        """
        result = compare(
            make_ref(),
            {
                "crossref": make_record(),
                "pubmed": make_pubmed(retracted=True, retraction_kind="Retracted Publication"),
            },
        )
        status = next(i for i in result.issues if i.field == "status")
        assert status.source == "pubmed"
        assert status.registry == "Retracted Publication"
        assert "recorded by pubmed" in status.note
        assert "not by crossref" in status.note

    def test_two_registries_agreeing_says_so(self) -> None:
        result = compare(
            make_ref(),
            {
                "crossref": make_record(retracted=True, retraction_kind="retraction"),
                "pubmed": make_pubmed(retracted=True, retraction_kind="Retracted Publication"),
            },
        )
        status = next(i for i in result.issues if i.field == "status")
        assert status.source == "crossref,pubmed"
        assert "recorded by crossref, pubmed" in status.note
        # Each registry's own wording is kept: NLM's controlled vocabulary and
        # Crossref's update type are not the same string and neither is a
        # summary of the other.
        assert status.registry == "crossref=retraction; pubmed=Retracted Publication"
        # No "and not by ..." clause: nothing dissented.
        assert "not by" not in status.note

    def test_a_datacite_deposit_can_be_corroborated_by_pubmed_too(self) -> None:
        """Crossref is not special here; the union is over everything that answered."""
        result = compare(
            make_ref(),
            {
                "datacite": make_record(source="datacite"),
                "pubmed": make_pubmed(retracted=True, retraction_kind="Retracted Publication"),
            },
        )
        assert result.verdict == "RETRACTED"

    def test_a_retraction_notice_is_not_a_retracted_work(self) -> None:
        """The direction, which is the one thing here that must not be guessed.

        ``Retracted Publication`` is a paper that was retracted. ``Retraction
        Notice`` *is* the notice announcing one — an ordinary, citable
        document, and citing it is not a defect in anybody's bibliography. The
        two strings open on the same stem, so any test looser than the equality
        ``registries.pubmed`` performs clears the retracted paper and flags the
        notice that retracted it: exactly backwards, and silently.

        ``compare`` must therefore take the flag and nothing else. This record
        is a notice — ``retracted=False`` with a retraction-shaped
        ``retraction_kind`` and title — and a comparison that sniffed either
        string would report it.
        """
        notice_title = "Retraction of: Risk of pancreatic cancer associated with family history"
        notice = make_record(
            title=notice_title,
            retracted=False,
            retraction_kind="Retraction Notice",
        )
        result = compare(make_ref(title=notice_title), {"crossref": notice})
        assert result.verdict != "RETRACTED"
        assert not any(i.field == "status" for i in result.issues)

    def test_and_the_same_shape_with_the_flag_set_does_fire(self) -> None:
        """The pairing for the test above: the guard is not a blanket exemption.

        Same record, same suspicious strings, ``retracted=True``. If the check
        above were passing because retraction detection had been broken rather
        than because the direction is respected, this would pass too.
        """
        retracted = make_record(retracted=True, retraction_kind="Retracted Publication")
        result = compare(make_ref(), {"crossref": retracted})
        assert result.verdict == "RETRACTED"

    def test_a_registry_that_never_answered_cannot_dissent(self) -> None:
        """"and not by X" must name only registries that actually replied.

        Listing a registry that timed out as having "no retraction linkage"
        would report ignorance as corroboration, in the one place where a
        reader is most likely to talk themselves out of acting.
        """
        result = compare(
            make_ref(),
            {"pubmed": make_pubmed(retracted=True, retraction_kind="Retracted Publication")},
            unreachable={"crossref"},
        )
        status = next(i for i in result.issues if i.field == "status")
        assert "crossref" not in status.note
        assert result.verdict == "RETRACTED"


_DATA = Path(__file__).parent / "data"


class _SavedResponse:
    """Serves one saved Crossref payload wherever a ``Client`` is expected.

    Only ``get_json`` is reached: ``Crossref.by_dois`` builds a
    ``works?filter=doi:...`` URL and reads ``message.items`` out of the reply, so
    returning the saved work under that shape exercises the real parse — the
    ``updated-by`` read included — without a network.
    """

    def __init__(self, work: dict[str, Any]) -> None:
        self._work = work

    def get_json(self, url: str, **_: Any) -> dict[str, Any]:
        return {"message": {"items": [self._work]}}


def crossref_work(fixture: str) -> dict[str, Any]:
    """The ``message`` object out of a saved ``api.crossref.org`` response."""
    payload = json.loads((_DATA / fixture).read_text(encoding="utf-8"))
    work: dict[str, Any] = payload["message"]
    return work


def crossref_record(fixture: str) -> Record:
    """The ``Record`` ``registries.crossref`` builds from a saved response."""
    work = crossref_work(fixture)
    [record] = Crossref(_SavedResponse(work)).by_dois([work["DOI"]]).values()  # type: ignore[arg-type]
    return record


def pubmed_record(fixture: str) -> Record:
    """The ``Record`` ``registries.pubmed`` builds from saved MEDLINE text.

    The two module-private helpers are called directly. ``PubMed.by_dois`` would
    need three stubbed E-utilities endpoints to reach them, and it is exactly
    these two — the MEDLINE parse and the ``PT`` read — that decide the
    retraction flag this file is checking.
    """
    text = (_DATA / fixture).read_text(encoding="utf-8")
    [fields] = pubmed_client._parse_medline_records(text)
    return pubmed_client._record_from_medline(fields)


def wakefield_paper_ref(**overrides: object) -> Reference:
    """The retracted paper, cited correctly.

    Wakefield AJ et al., *Lancet* 1998;351(9103):637-41, the study whose
    retraction is the most-cited retraction in medicine. Every field agrees with
    both registries, so the only thing any verdict below can be about is status.

    The author list is truncated with an et-al marker rather than spelling out
    all thirteen names: BibTeX's ``and others`` is what a real entry carries, and
    ``names.compare_author_lists`` treats a length comparison past that point as
    void.
    """
    base: dict[str, object] = {
        "key": "wakefield1998ileal",
        "locator": "references.bib:12",
        "kind": "article",
        "doi": "10.1016/S0140-6736(97)11096-0",
        "title": (
            "Ileal-lymphoid-nodular hyperplasia, non-specific colitis, and pervasive "
            "developmental disorder in children"
        ),
        "authors": [Name(family="Wakefield", given="AJ"), Name(et_al=True)],
        "year": 1998,
        "container": "The Lancet",
        "volume": "351",
        "issue": "9103",
        "pages": "637-641",
    }
    base.update(overrides)
    return Reference(**base)  # type: ignore[arg-type]


def wakefield_notice_ref(**overrides: object) -> Reference:
    """The *notice*, cited deliberately — as a paper about the retraction would.

    Editors of The Lancet, *Lancet* 2010;375(9713):445. This is an ordinary
    citable document and citing it is not a defect in anybody's bibliography.
    """
    base: dict[str, object] = {
        "key": "lancet2010retraction",
        "locator": "references.bib:20",
        "kind": "article",
        "doi": "10.1016/S0140-6736(10)60175-4",
        "title": (
            "Retraction—Ileal-lymphoid-nodular hyperplasia, non-specific colitis, "
            "and pervasive developmental disorder in children"
        ),
        "authors": [Name(family="The Editors of The Lancet")],
        "year": 2010,
        "container": "The Lancet",
        "volume": "375",
        "issue": "9713",
        "pages": "445",
    }
    base.update(overrides)
    return Reference(**base)  # type: ignore[arg-type]


class TestRetractionDirectionOnRealDeposits:
    """``update-to`` versus ``updated-by``, against the publisher's own deposit.

    Crossref's two linkage fields are reciprocal and are not interchangeable:
    the notice deposits ``update-to`` naming the work it retracts, and Crossref
    writes the matching ``updated-by`` onto the *retracted* work. Reading them
    backwards clears every retracted paper and flags every notice, silently and
    with the tool's full confidence — the worst outcome available here, and one
    no synthetic fixture can catch, because a synthetic fixture encodes whichever
    direction its author believed.

    So both halves of one real pair are replayed: 10.1016/S0140-6736(97)11096-0,
    the retracted Wakefield paper, and 10.1016/S0140-6736(10)60175-4, the
    Lancet's notice that retracted it. Fetched from api.crossref.org and NCBI
    E-utilities on 2026-08-01 and stored verbatim.
    """

    def test_the_fixtures_carry_the_linkage_in_opposite_directions(self) -> None:
        """The anchor. If this fails, the fixtures were edited, not the code.

        Everything else in this class rests on these two payloads differing in
        exactly one way, so the difference is asserted rather than assumed.
        """
        paper = crossref_work("compare_crossref_wakefield_retracted.json")
        notice = crossref_work("compare_crossref_wakefield_notice.json")

        assert [u["type"] for u in paper["updated-by"]] == ["correction", "retraction"]
        assert not paper.get("update-to")

        assert [u["type"] for u in notice["update-to"]] == ["retraction"]
        assert notice["update-to"][0]["DOI"] == paper["DOI"]
        assert not notice.get("updated-by")

    def test_the_retracted_paper_is_reported(self) -> None:
        result = compare(
            wakefield_paper_ref(),
            {
                "crossref": crossref_record("compare_crossref_wakefield_retracted.json"),
                "pubmed": pubmed_record("compare_pubmed_wakefield_retracted.txt"),
            },
        )
        assert result.verdict == "RETRACTED"
        assert result.fails
        status = next(i for i in result.issues if i.field == "status")
        assert status.kind == "retracted"
        # Both curated sources hold it independently: Crossref through the
        # Retraction Watch feed in `updated-by`, NLM through `PT - Retracted
        # Publication`. Neither is derived from the other.
        assert status.source == "crossref,pubmed"
        assert status.registry == "crossref=retraction; pubmed=Retracted Publication"

    def test_the_notice_that_retracted_it_is_not_reported(self) -> None:
        """Citing the notice on purpose must not be turned into an accusation.

        A paper *about* the Wakefield retraction cites 10.1016/S0140-6736(10)
        60175-4 deliberately and correctly. Flagging it would make the tool
        unusable for exactly the literature that discusses research integrity,
        and would do it while clearing the paper the notice retracted.
        """
        result = compare(
            wakefield_notice_ref(),
            {
                "crossref": crossref_record("compare_crossref_wakefield_notice.json"),
                "pubmed": pubmed_record("compare_pubmed_wakefield_notice.txt"),
            },
        )
        assert result.verdict != "RETRACTED"
        assert not any(i.field == "status" for i in result.issues)
        assert not result.fails

    def test_neither_registry_reads_the_notice_as_retracted(self) -> None:
        """The pairing, one layer down: the flag itself, not just the verdict.

        The verdict test above would also pass if retraction detection had
        broken outright, so the four flags are asserted directly — including
        NLM's, whose publication type for the notice is ``Retraction Notice``.
        It opens on the same stem as ``Retracted Publication``, which is why
        ``registries.pubmed`` matches on equality.
        """
        assert crossref_record("compare_crossref_wakefield_retracted.json").retracted
        assert pubmed_record("compare_pubmed_wakefield_retracted.txt").retracted
        assert not crossref_record("compare_crossref_wakefield_notice.json").retracted
        assert not pubmed_record("compare_pubmed_wakefield_notice.txt").retracted


class TestRetractionKinds:
    """"Retraction" is not the only way a work is pulled back.

    A withdrawal, a removal and a partial retraction are all reasons a citing
    author must be told, and each reaches ``compare`` as the same flag with
    different display wording. The verdict may not depend on the wording — only
    :data:`~bibaudit.compare._CONCERN_KINDS` does, and nothing in this class is
    in it.
    """

    def test_a_partial_retraction_from_a_real_deposit_still_fails(self) -> None:
        """10.29328/journal.jcmhs.1001023, whose only linkage is a partial one.

        ``partial_retraction`` was missing from ``crossref._RETRACTION_PRIORITY``
        once and this record passed as clean. Part of a paper being withdrawn is
        still a reason to check what was cited from it.
        """
        record = crossref_record("compare_crossref_partial_retraction.json")
        assert record.retraction_kind == "partial_retraction"
        ref = Reference(
            key="bumozah2022internet",
            locator="references.bib:44",
            kind="article",
            doi="10.29328/journal.jcmhs.1001023",
            title=(
                "Association Between Internet Gaming Disorder And Attention Deficit "
                "Hyperactivity Disorder: A Narrative Review"
            ),
            authors=[Name(family="Bumozah", given="Hanin"), Name(family="Alabdulbaqi", given="Donna")],
            year=2022,
            container="Journal of Community Medicine and Health Solutions",
            volume="3",
            issue="1",
            pages="069-075",
        )
        result = compare(ref, {"crossref": record})
        assert result.verdict == "RETRACTED"
        assert [i.kind for i in result.errors] == ["retracted"]

    @pytest.mark.parametrize(
        "kind",
        [
            "withdrawal",
            "removal",
            "partial_retraction",
            "Retracted Publication",
            # A registry that sets the flag but names no type at all: the flag
            # decides, and an unnamed kind must not soften it.
            None,
        ],
    )
    def test_every_kind_of_pulling_back_is_a_retraction(self, kind: str | None) -> None:
        record = make_record(retracted=True, retraction_kind=kind)
        result = compare(make_ref(), {"crossref": record})
        assert result.verdict == "RETRACTED"

    def test_the_registrys_own_wording_reaches_the_report(self) -> None:
        """A reader chasing this has to know which notice to look for."""
        record = make_record(retracted=True, retraction_kind="withdrawal")
        result = compare(make_ref(), {"crossref": record})
        status = next(i for i in result.issues if i.field == "status")
        assert status.registry == "withdrawal"


class TestExpressionOfConcern:
    """A concern is reported, and it is not called a retraction.

    Crossref ranks ``expression_of_concern`` in the same priority tuple as a
    retraction, so ``Record.retracted`` arrives here set for a paper that has not
    been retracted at all. The report then printed ``RETRACTED — the cited work
    has itself been retracted`` about a named, un-retracted paper.

    Both halves matter. Saying it is a retraction is a false statement the tool
    may not make; saying nothing would drop a finding an author must act on
    before submitting. So it stays an error — the entry fails exactly as it did
    before — and only the wording changes.
    """

    #: 10.1371/journal.pone.0064723 (Neirinckx et al., PLOS ONE 2013). Its
    #: Crossref ``updated-by`` carries two ``expression_of_concern`` entries —
    #: one sourced ``retraction-watch``, one ``publisher`` — and no retraction,
    #: withdrawal or removal of any kind.
    FIXTURE = "compare_crossref_expression_of_concern.json"

    def concern_ref(self) -> Reference:
        return Reference(
            key="neirinckx2013adult",
            locator="references.bib:88",
            kind="article",
            doi="10.1371/journal.pone.0064723",
            title=(
                "Adult Bone Marrow Neural Crest Stem Cells and Mesenchymal Stem Cells "
                "Are Not Able to Replace Lost Neurons in Acute MPTP-Lesioned Mice"
            ),
            authors=[Name(family="Neirinckx", given="Virginie"), Name(et_al=True)],
            year=2013,
            container="PLoS ONE",
            volume="8",
            issue="5",
            pages="e64723",
        )

    def test_the_real_record_still_arrives_flagged_as_retracted(self) -> None:
        """What ``compare`` is handed, stated plainly.

        If ``registries.crossref`` is ever fixed to stop conflating the two,
        this fails and the guard below becomes dead code that should be removed
        rather than left looking like protection.
        """
        record = crossref_record(self.FIXTURE)
        assert record.retracted is True
        assert record.retraction_kind == "expression_of_concern"

    def test_a_concern_is_not_reported_as_a_retraction(self) -> None:
        result = compare(self.concern_ref(), {"crossref": crossref_record(self.FIXTURE)})
        assert result.verdict != "RETRACTED"
        status = next(i for i in result.issues if i.field == "status")
        assert status.kind == "expression-of-concern"
        assert "has been retracted" not in status.note
        assert "not a retraction" in status.note

    def test_but_it_is_still_reported_and_still_fails(self) -> None:
        """The half that must not be lost. Nothing here is a relaxation.

        An author submitting a manuscript needs to know a cited paper is under
        an expression of concern, so the entry keeps a failing verdict and the
        concern is the only thing wrong with it.
        """
        result = compare(self.concern_ref(), {"crossref": crossref_record(self.FIXTURE)})
        assert result.fails
        assert [i.kind for i in result.errors] == ["expression-of-concern"]

    def test_a_real_retraction_on_the_same_registry_still_fires(self) -> None:
        """The pairing for the guard: it exempts one exact string, not a shape."""
        record = make_record(retracted=True, retraction_kind="retraction")
        assert compare(make_ref(), {"crossref": record}).verdict == "RETRACTED"

    @pytest.mark.parametrize(
        "kind",
        [
            # No registry emits these as an update type today; they are the
            # shapes a substring or prefix test would fall to, and the wording is
            # not invented — the Lancet Neurology notice
            # 10.1016/S1474-4422(26)00052-9 is titled "Resolution of expression
            # of concern", and resolving a concern is as often a retraction as an
            # exoneration. Any of them arriving as a `retraction_kind` must leave
            # the verdict at RETRACTED: a retracted paper reported as merely
            # doubted is the miss this whole class is guarding.
            "Resolution of expression of concern",
            "retraction following expression of concern",
            "expression of concern and retraction",
        ],
    )
    def test_a_retraction_that_merely_mentions_a_concern_is_not_softened(
        self, kind: str
    ) -> None:
        record = make_record(retracted=True, retraction_kind=kind)
        result = compare(make_ref(), {"crossref": record})
        assert result.verdict == "RETRACTED"

    def test_one_registrys_retraction_outranks_anothers_concern(self) -> None:
        """The union must not be weakened by the weaker signal.

        Crossref holding only a concern while NLM has already recorded the
        retraction is the ordinary lag between a publisher's deposit and NLM's
        curation, and the conclusion is the retraction.
        """
        result = compare(
            make_ref(),
            {
                "crossref": make_record(
                    retracted=True, retraction_kind="expression_of_concern"
                ),
                "pubmed": make_pubmed(retracted=True, retraction_kind="Retracted Publication"),
            },
        )
        assert result.verdict == "RETRACTED"
        kinds = [i.kind for i in result.issues if i.field == "status"]
        # Both are said, and the retraction is said first.
        assert kinds == ["retracted", "expression-of-concern"]

    def test_a_concerned_registry_is_not_listed_as_dissenting(self) -> None:
        """"and not by crossref ... carries no retraction linkage" would be wrong.

        Crossref did record something about this work. Printing it beside the
        registries that recorded nothing invites a reader to read corroboration
        of a weaker signal as a second opinion against the finding.
        """
        result = compare(
            make_ref(),
            {
                "crossref": make_record(
                    retracted=True, retraction_kind="expression_of_concern"
                ),
                "pubmed": make_pubmed(retracted=True, retraction_kind="Retracted Publication"),
            },
        )
        retraction = next(i for i in result.issues if i.kind == "retracted")
        assert "not by" not in retraction.note

    def test_a_registry_holding_nothing_is_still_listed_as_dissenting(self) -> None:
        """The pairing: silence from a registry that answered is still evidence."""
        result = compare(
            make_ref(),
            {
                "crossref": make_record(),
                "pubmed": make_pubmed(retracted=True, retraction_kind="Retracted Publication"),
            },
        )
        retraction = next(i for i in result.issues if i.kind == "retracted")
        assert "not by crossref" in retraction.note


def make_retraction_watch(kind: str) -> Record:
    """Retraction Watch's contribution, as ``audit._merge_retraction_notices``
    builds it: a status and nothing else, on a DOI some other registry resolved.
    """
    return Record(
        source="retraction-watch",
        doi="10.1093/ije/dyx269",
        retracted=True,
        retraction_kind=kind,
    )


class TestCorrection:
    """A correction is neither a retraction nor a concern, and says so.

    ``registries.retractions`` mints ``correction`` from Retraction Watch's
    ``RetractionNature`` column, and every kind outside the concern vocabulary
    counted as a retraction — so an entry citing a paper Retraction Watch logs
    a correction for printed ``RETRACTED — the cited work has itself been
    retracted``, with the word ``correction`` in the registry column, and
    exited 1. A false statement about a named work, made with the tool's full
    authority, arriving by the second of two routes.

    The pairing matters as much: the finding is stated rather than dropped,
    because a reader wanting the corrected version's numbers is who it is for.
    """

    def test_a_correction_is_not_reported_as_a_retraction(self) -> None:
        result = compare(
            make_ref(),
            {"crossref": make_record(), "retraction-watch": make_retraction_watch("correction")},
        )
        assert result.verdict != "RETRACTED"
        status = next(i for i in result.issues if i.field == "status")
        assert status.kind == "correction"
        assert "has been retracted" not in status.note

    def test_the_verdict_and_the_exit_code_do_not_move(self) -> None:
        """A corrected paper is perfectly citable. Failing a build over one is
        the false alarm this project's third rule is about.
        """
        result = compare(
            make_ref(),
            {"crossref": make_record(), "retraction-watch": make_retraction_watch("correction")},
        )
        assert result.verdict == "OK"
        assert not result.fails

    def test_but_it_is_still_reported_and_names_its_source(self) -> None:
        """The half that must not be lost: ``info`` is stated, not dropped."""
        result = compare(
            make_ref(),
            {"crossref": make_record(), "retraction-watch": make_retraction_watch("correction")},
        )
        status = next(i for i in result.issues if i.kind == "correction")
        assert status.severity == "info"
        assert status.source == "retraction-watch"
        assert status.registry == "correction"
        assert "the corrected version is the one to read the numbers off" in status.note

    @pytest.mark.parametrize(
        "kind",
        [
            # Membership is exact on the folded kind, never a substring: a
            # notice titled "Retraction and correction" is a retraction, and a
            # registry wording this tool has never seen cannot talk it out of
            # the finding.
            "retraction and correction",
            "correction and retraction",
            "corrected and republished article",
        ],
    )
    def test_a_retraction_that_merely_mentions_a_correction_is_not_softened(
        self, kind: str
    ) -> None:
        result = compare(make_ref(), {"crossref": make_record(retracted=True, retraction_kind=kind)})
        assert result.verdict == "RETRACTED"


class TestAMilderNoticeBesideARetraction:
    """One report may not say a work has been withdrawn and that it stands.

    Two sources reporting different kinds for one work is ordinary: of the 650
    DOIs whose strongest Retraction Watch row is a correction, 19 carry a
    Crossref ``updated-by`` retraction — 10.1111/anec.12955 among them, and
    10.1080/09513590600604673 for the concern. Both findings are printed — they
    are separate statements and all of them are true — but the milder note's
    closing clause said the work stands and the citation is legitimate, beside
    a ``status/retracted`` line saying it has been withdrawn.
    """

    def _with(self, kind: str) -> Result:
        return compare(
            make_ref(),
            {
                "crossref": make_record(retracted=True, retraction_kind="retraction"),
                "retraction-watch": make_retraction_watch(kind),
            },
        )

    def test_a_correction_does_not_say_the_work_stands(self) -> None:
        note = next(i.note for i in self._with("correction").issues if i.kind == "correction")
        assert "The work stands and citing it is correct" not in note
        assert "does not undo the retraction" in note

    def test_a_concern_does_not_say_the_work_stands(self) -> None:
        note = next(
            i.note for i in self._with("expression of concern").issues
            if i.kind == "expression-of-concern"
        )
        assert "the work stands" not in note
        assert "does not undo the retraction" in note

    def test_both_findings_are_still_printed_and_the_verdict_is_retracted(self) -> None:
        """Nothing is dropped for it. A source recording the milder notice has
        not contradicted the retraction, and its notice is a fact about the
        work the reader may want to chase.
        """
        result = self._with("correction")
        assert result.verdict == "RETRACTED"
        assert [i.kind for i in result.issues if i.field == "status"] == [
            "retracted",
            "correction",
        ]

    def test_the_ordinary_wording_survives_where_nothing_was_retracted(self) -> None:
        """The pairing: a corrected paper on its own still reads as citable."""
        result = compare(
            make_ref(),
            {"crossref": make_record(), "retraction-watch": make_retraction_watch("correction")},
        )
        note = next(i.note for i in result.issues if i.kind == "correction")
        assert "The work stands and citing it is correct" in note
        assert "does not undo the retraction" not in note


class TestRetractionEvidenceIsNeverAssumed:
    """An unreachable registry does not answer "not retracted".

    A 404 is a fact and a timeout is ignorance, and nowhere does the difference
    cost more than here: with PubMed down, a paper NLM records as retracted and
    Crossref does not know about produced a verdict of ``OK`` — "every checked
    field agrees" — with nothing anywhere in the report to say that the only
    registry that could have known was never heard from. The reader gets a clean
    bill of health assembled out of an outage.

    The verdict deliberately does not move: an outage is not a defect in
    anybody's bibliography, which is why ``UNCHECKED`` does not fail either, and
    ``test_audit.TestOutageIsNeverAFinding`` requires exactly this. What changes
    is that the run now says out loud which conclusion it could not support.
    """

    def test_a_pubmed_outage_leaves_a_stated_gap_not_a_clean_verdict(self) -> None:
        result = compare(make_ref(), {"crossref": make_record()}, unreachable={"pubmed"})
        status = next(i for i in result.issues if i.field == "status")
        assert status.kind == "retraction-unverified"
        assert status.source == "pubmed"
        assert "could not be reached" in status.note
        # And it does not claim the opposite of what it knows.
        assert "not retracted" not in status.note

    def test_the_verdict_and_the_exit_code_do_not_move(self) -> None:
        """A network outage may not fail a build or accuse a bibliography."""
        result = compare(make_ref(), {"crossref": make_record()}, unreachable={"pubmed"})
        assert result.verdict == "OK"
        assert not result.fails
        assert not result.answered("pubmed")

    def test_the_gap_is_stated_for_whichever_registry_went_down(self) -> None:
        """Crossref is not special: it carries the Retraction Watch linkage."""
        result = compare(
            make_ref(),
            {"pubmed": make_pubmed()},
            unreachable={"crossref"},
        )
        status = next(i for i in result.issues if i.field == "status")
        assert status.kind == "retraction-unverified"
        assert status.source == "crossref"

    def test_both_are_named_when_both_went_down(self) -> None:
        result = compare(
            make_ref(),
            {"datacite": make_record(source="datacite")},
            unreachable={"crossref", "pubmed"},
        )
        status = next(i for i in result.issues if i.field == "status")
        assert status.source == "crossref,pubmed"
        assert "crossref, pubmed" in status.note

    def test_an_ordinary_run_says_nothing(self) -> None:
        """The false-alarm side, and the one that decides whether this is read.

        Nothing is unreachable, so there is no gap to state. A line on every
        entry of a healthy run is noise, and a report people skim is worth less
        than one they read.
        """
        result = compare(
            make_ref(),
            {"crossref": make_record(), "pubmed": make_pubmed()},
        )
        assert result.verdict == "OK"
        assert not result.issues

    def test_a_registry_that_simply_had_nothing_is_not_an_outage(self) -> None:
        """PubMed holds no PMID for most non-biomedical work.

        That is an answer, not an outage, and treating it as one would put the
        line on nearly every entry of an ordinary bibliography — the same noise
        as above, with the added defect of being wrong about what happened.
        """
        result = compare(
            make_ref(), {"crossref": make_record()}, asked={"crossref", "datacite", "pubmed"}
        )
        assert result.verdict == "OK"
        # Retraction Watch really was not asked here and says so separately;
        # what must not appear is the *outage* finding.
        assert not any(i.kind == "retraction-unverified" for i in result.issues)

    def test_datacite_going_down_says_nothing_about_retraction(self) -> None:
        """DataCite's schema has no retraction element and its client sets none.

        Naming it would manufacture a doubt that a reachable DataCite could not
        have resolved, on every dataset and preprint in the file.
        """
        result = compare(make_ref(), {"crossref": make_record()}, unreachable={"datacite"})
        assert result.verdict == "OK"
        assert not result.issues

    def test_open_library_going_down_says_nothing_about_retraction(self) -> None:
        """Open Library is a book catalogue with no retraction element either.

        The same argument as DataCite above, found one audit later and by the
        opposite route: ``audit.py`` really does add ``"openlibrary"`` to
        *unreachable* when the ISBN leg times out, so before it was excluded
        here an Open Library outage printed "retraction status not corroborated:
        openlibrary could not be reached" against every book in the file. A
        caveat that appears on a whole class of entries for a reason nobody can
        act on is the noise CLAUDE.md's third rule exists to keep out.
        """
        result = compare(make_ref(), {"crossref": make_record()}, unreachable={"openlibrary"})
        assert result.verdict == "OK"
        assert not result.issues

    def test_a_search_source_going_down_says_nothing_about_retraction(self) -> None:
        """Europe PMC and OpenAlex are read for candidates and nothing else.

        ``registries/search.py`` builds both records without touching a
        retraction field — Europe PMC's ``commentCorrectionList`` included, so
        here the limit is this tool's rather than the source's. Either way a
        reachable Europe PMC would have resolved nothing, and an entry with no
        identifier has no candidate pool but these two and Crossref, so the
        doubt was stated on all three names at once.
        """
        result = compare(
            make_ref(),
            {"crossref": make_record()},
            unreachable={"europepmc", "openalex"},
        )
        assert result.verdict == "OK"
        assert not result.issues

    def test_a_retraction_watch_outage_leaves_a_stated_gap(self) -> None:
        """The opposite direction, and the reason the exclusion set stays an
        exclusion set: Retraction Watch exists *only* to carry this signal, so
        its going unreached is the most consequential gap this check can state.

        It reaches ``unreachable`` through ``Retractions.status_for``'s returned
        :class:`~bibaudit.registries.retractions.RetractionStatus`, not through a
        raise -- see ``audit._resolve_retractions``.
        """
        result = compare(
            make_ref(), {"crossref": make_record()}, unreachable={"retraction-watch"}
        )
        status = next(i for i in result.issues if i.field == "status")
        assert status.kind == "retraction-unverified"
        assert status.source == "retraction-watch"
        assert "could not be reached" in status.note
        # An outage still may not fail a build or accuse a bibliography.
        assert result.verdict == "OK"
        assert not result.fails

    def test_a_found_retraction_needs_no_caveat(self) -> None:
        """Once one registry has said it, another's silence changes nothing."""
        result = compare(
            make_ref(),
            {"crossref": make_record(retracted=True, retraction_kind="retraction")},
            unreachable={"pubmed"},
        )
        assert result.verdict == "RETRACTED"
        assert [i.kind for i in result.issues if i.field == "status"] == ["retracted"]

    def test_an_unfound_concern_still_leaves_the_retraction_gap_open(self) -> None:
        """A concern is not a retraction, so it does not close this question.

        Crossref recording a doubt while PubMed — the source that would carry
        ``PT - Retracted Publication`` — is unreachable means the work may
        already have been retracted and this run cannot tell.
        """
        result = compare(
            make_ref(),
            {"crossref": make_record(retracted=True, retraction_kind="expression_of_concern")},
            unreachable={"pubmed"},
        )
        kinds = [i.kind for i in result.issues if i.field == "status"]
        assert kinds == ["expression-of-concern", "retraction-unverified"]

    def test_a_total_outage_is_still_reported_as_unchecked(self) -> None:
        """No record answered at all, so there is no comparison to caveat.

        The entry is ``UNCHECKED`` and carries the existing ``unreachable``
        issue; a second line saying retraction could not be checked either would
        be true and useless.
        """
        result = compare(
            make_ref(), {}, unreachable={"crossref", "datacite", "pubmed"}
        )
        assert result.verdict == "UNCHECKED"
        assert [i.kind for i in result.issues] == ["unreachable"]


class TestARetractionSourceNobodyAsked:
    """"Nobody asked" is not "nothing found", and must not read like it.

    The reference resolved by its PMID is the case: every retraction source but
    MEDLINE's own ``PT`` flag is keyed on a DOI it does not have, so three of
    the four go unconsulted — and the entry rendered ``verdict: OK, issues:
    []``. A clean bill of health issued by a run that asked almost nobody is
    the one output CLAUDE.md forbids outright, and an outage was already
    stated where this was silent.

    Kept as its own ``Issue.kind`` rather than folded into
    ``retraction-unverified``: an outage may be gone by the next run, an
    unasked source is a standing property of how the reference resolved, and
    ``Consultation`` keeps the same two states apart for the same reason.
    """

    def test_a_retraction_source_nobody_asked_leaves_a_stated_gap(self) -> None:
        result = compare(
            make_ref(),
            {"pubmed": make_pubmed()},
            asked={"pubmed"},
        )
        status = next(i for i in result.issues if i.field == "status")
        assert status.kind == "not-asked"
        assert status.severity == "info"
        assert status.source == "crossref,retraction-watch"
        assert "not the same as there being none" in status.note

    def test_the_note_says_which_of_the_two_reasons_it_was(self) -> None:
        """"Nobody asked" has two causes and the reader's next move differs.

        Nothing a rerun does asks Retraction Watch about a reference with no
        DOI; dropping a flag is what makes a source that *had* a key answer.
        One sentence covering both told them apart for neither.
        """
        result = compare(make_ref(doi=None, pmid="9500320"), {"pubmed": make_pubmed()}, asked={"pubmed"})
        note = next(i.note for i in result.issues if i.kind == "not-asked")

        assert "crossref, retraction-watch take a DOI this reference does not carry" in note

    def test_a_source_that_had_a_key_and_was_skipped_says_so_instead(self) -> None:
        """``--no-retraction-check`` on an entry Retraction Watch could answer for."""
        result = compare(
            make_ref(), {"crossref": make_record()}, asked={"crossref", "datacite", "pubmed"}
        )
        note = next(i.note for i in result.issues if i.kind == "not-asked")

        assert "retraction-watch was not queried on this run" in note

    def test_a_book_names_the_pmid_pubmed_would_also_have_taken(self) -> None:
        """PubMed is the one source with two keys, and the note has to say so.

        A book resolved by its ISBN carries neither, so all three DOI-keyed
        sources go unasked — but naming only the DOI would tell a reader a
        PMID beside the ISBN changes nothing, when it would have reached
        MEDLINE's own ``PT`` flag and its ``ECI`` cross-reference.
        """
        ref = make_ref(key="knuth1997art", kind="book", doi=None, isbn="0-201-89683-4")
        record = Record(source="openlibrary", title=ref.title, authors=list(ref.authors))
        result = compare(ref, {"openlibrary": record}, asked={"openlibrary"})
        note = next(i.note for i in result.issues if i.kind == "not-asked")

        assert "pubmed takes a DOI or a PMID this reference does not carry" in note

    def test_the_two_keys_are_not_pooled_across_the_sources_that_lack_them(
        self,
    ) -> None:
        """Each source is named beside the key it is looked up by.

        Pooled, the same book read ``crossref, pubmed, retraction-watch take a
        DOI or a PMID this reference does not carry`` — true of PubMed and loose
        about the other two, neither of which is ever asked with a PMID. A
        reader adding one to that entry would find two of the three still
        unasked, and the note had told them otherwise.
        """
        ref = make_ref(key="knuth1997art", kind="book", doi=None, isbn="0-201-89683-4")
        record = Record(source="openlibrary", title=ref.title, authors=list(ref.authors))
        result = compare(ref, {"openlibrary": record}, asked={"openlibrary"})
        note = next(i.note for i in result.issues if i.kind == "not-asked")

        assert "crossref, retraction-watch take a DOI this reference does not carry" in note
        assert "crossref, pubmed, retraction-watch take a DOI or a PMID" not in note

    def test_the_verdict_and_the_exit_code_do_not_move(self) -> None:
        """Coverage a reference's own identifier denies it is not its defect."""
        result = compare(make_ref(), {"pubmed": make_pubmed()}, asked={"pubmed"})
        assert result.verdict == "OK"
        assert not result.fails

    def test_an_ordinary_run_that_asked_everybody_says_nothing(self) -> None:
        """The false-alarm side, and the one that decides whether this is read.

        Every source that carries the signal was asked, so there is no gap to
        state. DataCite is unasked here and stays unnamed: its schema has no
        retraction element, so its silence is not ignorance about retraction —
        the same exclusion the outage note already applies.
        """
        result = compare(
            make_ref(),
            {"crossref": make_record(), "pubmed": make_pubmed()},
            asked={"crossref", "pubmed", "retraction-watch"},
        )
        assert result.verdict == "OK"
        assert not [i for i in result.issues if i.field == "status"]

    def test_a_caller_that_did_not_say_is_not_quoted_as_saying_nobody_asked(self) -> None:
        """``asked=None`` means the caller did not say, not that nobody asked.

        ``_consultations`` reads it as ``not-asked`` and documents that as an
        under-statement it is free to make about a *description*. A finding is
        an assertion, and asserting from the same silence that nobody was asked
        would print a gap on evidence nobody produced — the line ``compare``'s
        ``identifier/not-asked`` branch already draws between an empty ``asked``
        and ``None``.
        """
        result = compare(make_ref(), {"pubmed": make_pubmed()})
        assert result.consulted["retraction-watch"] == "not-asked"
        assert not [i for i in result.issues if i.field == "status"]

    def test_a_recorded_retraction_outranks_the_gap(self) -> None:
        """Nothing is unverified once a source that answered has answered it."""
        result = compare(
            make_ref(),
            {"pubmed": make_pubmed(retracted=True, retraction_kind="Retracted Publication")},
            asked={"pubmed"},
        )
        assert result.verdict == "RETRACTED"
        assert [i.kind for i in result.issues if i.field == "status"] == ["retracted"]

    def test_an_outage_and_an_unasked_source_are_two_findings(self) -> None:
        """Both are true at once, and the reader's next move differs.

        A rerun may settle the outage; nothing about a rerun asks Retraction
        Watch about a reference with no DOI. One line saying "not corroborated"
        over both would send the reader to the wrong remedy.
        """
        result = compare(
            make_ref(),
            {"pubmed": make_pubmed()},
            unreachable={"crossref"},
            asked={"crossref", "pubmed"},
        )
        kinds = [i.kind for i in result.issues if i.field == "status"]
        assert kinds == ["retraction-unverified", "not-asked"]
        gap = next(i for i in result.issues if i.kind == "not-asked")
        assert gap.source == "retraction-watch"
        # This entry does carry a DOI, so the reason is the run's, not the
        # reference's.
        assert "retraction-watch was not queried on this run" in gap.note


class TestASourceThatCannotDissent:
    """A source carrying no retraction signal is never named as disagreeing.

    The ``status/retracted`` note names every registry that answered for the
    work and carries no retraction linkage, so the reader can tell "both
    curated sources agree" from "only PubMed knows about this". A source this
    tool reads no linkage from carries none for *any* work, so naming it states
    a fact about the client as though it were a second opinion about the paper
    — and it weakens the one finding a reader must not talk themselves out of.

    The mirror image of the outage exclusion, and it went the other way: the
    exclusion set existed and this branch never consulted it, so even DataCite,
    named in that set since it was written, dissented.
    """

    def _retracted_by(self, *others: str) -> Result:
        records = {
            "crossref": make_record(retracted=True, retraction_kind="retraction"),
            **{name: make_record(source=name) for name in others},
        }
        return compare(make_ref(), records, asked={"crossref", *others})

    def test_a_source_with_no_retraction_signal_is_not_named_as_dissenting(self) -> None:
        note = next(
            i for i in self._retracted_by("datacite").issues if i.kind == "retracted"
        ).note

        assert note == "the cited work has been retracted; recorded by crossref"

    def test_a_search_source_is_not_named_either(self) -> None:
        note = next(
            i for i in self._retracted_by("europepmc").issues if i.kind == "retracted"
        ).note

        assert "europepmc" not in note

    def test_a_source_that_does_carry_the_signal_still_dissents(self) -> None:
        """PubMed answering with no ``PT - Retracted Publication`` is evidence.

        It is also a bug report for the publisher, which is why the sentence
        exists at all — so the filter above must not swallow it.
        """
        result = compare(
            make_ref(),
            {
                "pubmed": make_pubmed(retracted=True, retraction_kind="Retracted Publication"),
                "crossref": make_record(),
            },
        )
        note = next(i for i in result.issues if i.kind == "retracted").note

        assert "and not by crossref, which answered for this work" in note


class TestARecordWithNothingToCompare:
    """``OK`` means "every checked field agrees", never "nothing was checked".

    Every check returns in silence when the registry's value is empty —
    correctly, since a registry omitting a field is not evidence about a
    bibliography. A record omitting *all* of them therefore produced no issues
    at all and the entry reported ``OK``, which is the clean bill of health
    ``status/not-asked`` exists to forbid, reached by a run that asked, got an
    answer, and compared nothing.

    MEDLINE's whole-book records are the instance: PMID 20301295 files its
    title under ``BTI`` and its byline under ``FED``, and until those were read
    an entry with both fabricated passed. This covers the shape rather than
    that instance.
    """

    def _empty_answer(self, **overrides: object) -> Result:
        return compare(
            make_ref(**overrides),
            {"crossref": Record(source="crossref", doi="10.1093/ije/dyx269")},
            asked={"crossref", "datacite", "pubmed", "retraction-watch"},
        )

    def test_a_record_holding_nothing_is_not_a_pass(self) -> None:
        result = self._empty_answer()

        assert result.verdict == "UNCHECKED"
        assert not result.fails

    def test_the_finding_says_what_happened(self) -> None:
        """"Not reached" and "answered with nothing usable" are different facts."""
        gap = next(i for i in self._empty_answer().issues if i.kind == "uncompared")

        assert gap.severity == "info"
        assert gap.field == "doi"
        assert gap.stored == "10.1093/ije/dyx269"
        assert "the identifier resolved" in gap.note

    def test_the_finding_is_labelled_with_the_identifier_that_resolved(self) -> None:
        """A PMID-resolved entry has no DOI, and the label may not invent one.

        The field is what the report prints in its left-hand column and what a
        ``.bibaudit.toml`` adjudication is scoped by, so ``doi`` beside an
        entry carrying only a PMID names a field the reader cannot find and
        scopes a suppression to one nothing here is about.
        """
        result = compare(
            make_ref(doi=None, pmid="29329392"),
            {"pubmed": Record(source="pubmed", pmid="29329392")},
            asked={"pubmed"},
        )
        gap = next(i for i in result.issues if i.kind == "uncompared")

        assert gap.field == "identifier"
        assert gap.stored == "29329392"

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("title", make_ref().title),
            ("authors", [Name(family="Molina-Montes", given="E")]),
            ("years", {"print": 2018}),
            ("container", "International Journal of Epidemiology"),
            ("volume", "47"),
            ("issue", "2"),
            ("pages", "473-483"),
            ("publisher", "Oxford University Press"),
        ],
    )
    def test_one_comparable_field_is_enough_to_be_a_real_verdict(
        self, field: str, value: object
    ) -> None:
        """The guard must not fire on a registry that is merely sparse.

        Every member of ``_COMPARABLE_FIELDS`` on its own, because a member
        dropped from that tuple silently turns "the registry answered with
        this one field and the entry agrees" into "nothing was compared" —
        ``UNCHECKED`` on an entry that was, and a run that reports less
        evidence than it has.
        """
        result = compare(
            make_ref(),
            {"crossref": Record(source="crossref", doi="10.1093/ije/dyx269", **{field: value})},  # type: ignore[arg-type]
            asked={"crossref"},
        )

        assert not [i for i in result.issues if i.kind == "uncompared"]
        assert result.verdict != "UNCHECKED"

    def test_the_corroborators_fields_count_too(self) -> None:
        """Something was compared, whichever record supplied it."""
        result = compare(
            make_ref(),
            {
                "crossref": Record(source="crossref", doi="10.1093/ije/dyx269"),
                "pubmed": make_pubmed(),
            },
            asked={"crossref", "pubmed"},
        )

        assert result.verdict != "UNCHECKED"

    def test_anything_actually_found_outranks_it(self) -> None:
        """A retraction on a fieldless record is still the story of the entry."""
        result = compare(
            make_ref(),
            {
                "crossref": Record(
                    source="crossref", doi="10.1093/ije/dyx269",
                    retracted=True, retraction_kind="retraction",
                )
            },
            asked={"crossref"},
        )

        assert result.verdict == "RETRACTED"


def stamps_retracted(source: str) -> dict[str, bool]:
    """``Record.source`` -> whether that same ``Record(...)`` call sets ``retracted``.

    Per call rather than per module: one module may build records for more
    than one registry, and a flag read off the module answers for a client
    that never set it.

    ``source=`` is a literal, an attribute of the client class, or a
    module-level name; the last two are resolved against the module's own
    string assignments.
    """
    tree = ast.parse(source)
    literals = {
        target.id: node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    stamped: dict[str, bool] = {}
    for call in ast.walk(tree):
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "Record"
        ):
            continue
        carries = any(kw.arg == "retracted" for kw in call.keywords)
        holder = next((kw.value for kw in call.keywords if kw.arg == "source"), None)
        if isinstance(holder, ast.Constant):
            stamped[str(holder.value)] = carries
        elif isinstance(holder, ast.Attribute):
            stamped[literals[holder.attr]] = carries
        elif isinstance(holder, ast.Name):
            stamped[literals[holder.id]] = carries
    return stamped


class TestEveryRegistryClientIsAccountedFor:
    """``_NO_RETRACTION_SIGNAL`` is derived from the clients, not remembered.

    Its own comment promises that whoever adds the next registry is counted by
    default and has to come and opt out. Europe PMC and OpenAlex were added to
    ``registries/search.py`` and neither did, so both were named in
    "retraction status not corroborated" for a doubt neither could have
    resolved. A prose contract that has already failed once is worth a test.
    """

    def _stamped(self) -> dict[str, bool]:
        """Every ``Record.source`` a registry client stamps -> does it set ``retracted``.

        Read out of the syntax rather than by calling the clients, because
        calling them needs a payload per source and a payload is exactly what
        a newly added registry would not have here yet.
        """
        package = Path(str(bibaudit.__file__)).parent / "registries"
        stamped: dict[str, bool] = {}
        for path in sorted(package.glob("*.py")):
            stamped.update(stamps_retracted(path.read_text(encoding="utf-8")))
        return stamped

    def test_the_clients_are_readable_at_all(self) -> None:
        """Guards the derivation itself: a silent empty scan proves nothing."""
        assert set(self._stamped()) >= {"crossref", "datacite", "pubmed", "openlibrary"}

    def test_each_record_call_answers_for_its_own_source(self) -> None:
        """One module stamps two sources, and the flag was read off the module.

        ``registries/search.py`` builds a Europe PMC record and an OpenAlex
        record, so a single ``retracted=`` anywhere in it credited both. The
        day one of the two learns to read a retraction linkage the other is
        credited with it as well, ``test_no_client_that_does_read_one_is_excluded``
        goes red for the source that learnt nothing, and the mechanical way to
        make it pass is to drop *both* from ``_NO_RETRACTION_SIGNAL`` —
        reinstating exactly the manufactured doubt the set exists to prevent.
        """
        two_in_one_module = (
            "def a():\n"
            "    return Record(source='alpha', retracted=True)\n"
            "def b():\n"
            "    return Record(source='beta')\n"
        )

        assert stamps_retracted(two_in_one_module) == {"alpha": True, "beta": False}

    def test_every_client_that_reads_no_retraction_signal_is_excluded(self) -> None:
        missing = {
            name
            for name, carries in self._stamped().items()
            if not carries and name not in _NO_RETRACTION_SIGNAL
        }

        assert not missing, (
            f"{sorted(missing)} set no Record.retracted, so naming them in a "
            "retraction gap manufactures a doubt they could not have resolved. "
            "Add them to compare._NO_RETRACTION_SIGNAL, or read the signal."
        )

    def test_no_client_that_does_read_one_is_excluded(self) -> None:
        """The costly direction: excluding a real witness understates ignorance."""
        wrongly_excluded = {
            name
            for name, carries in self._stamped().items()
            if carries and name in _NO_RETRACTION_SIGNAL
        }

        assert not wrongly_excluded


class TestConsulted:
    """``result.consulted`` is the record of what evidence a verdict rests on.

    It was computed as ``{name: name not in unreachable}`` — "not known to be
    unreachable", which is not "answered". Every run therefore reported
    ``"pubmed": true`` even under ``--no-corroborate``, where PubMed is never
    constructed, and ``"datacite": true`` on runs where DataCite was skipped
    entirely. The JSON report is what a reviewer re-derives a verdict from, and
    it was overstating its own evidence.
    """

    def test_a_registry_that_was_never_queried_is_not_reported_as_consulted(self) -> None:
        """``--no-corroborate`` shape: only Crossref was ever asked."""
        result = compare(
            make_ref(), {"crossref": make_record()}, asked={"crossref", "datacite"}
        )
        assert result.consulted["crossref"] == "answered"
        assert result.consulted["datacite"] == "answered"
        assert result.consulted["pubmed"] == "not-asked"
        assert not result.answered("pubmed")

    def test_asked_and_could_not_answer_is_its_own_state(self) -> None:
        """A timeout is ignorance, and it is not the same fact as "not asked".

        Collapsing them either way loses something: an outage reported as
        "not asked" hides a degraded run, and "not asked" reported as an outage
        invents one.
        """
        result = compare(
            make_ref(),
            {"crossref": make_record()},
            unreachable={"pubmed"},
            asked={"crossref", "datacite", "pubmed"},
        )
        assert result.consulted == {
            "crossref": "answered",
            "datacite": "answered",
            "pubmed": "unreachable",
            # Stated on every reference, asked or not — see `model.STATUS_SOURCES`.
            "retraction-watch": "not-asked",
        }

    def test_a_registry_that_answered_and_held_nothing_still_counts_as_asked(self) -> None:
        """An authoritative "I do not have this" is evidence, not silence.

        It is the whole basis of BAD-ID: a 404 is a fact. A representation that
        could not distinguish it from "not asked" would understate exactly the
        evidence that justifies the tool's strongest field-level accusation.
        """
        result = compare(make_ref(), {}, asked={"crossref", "datacite", "pubmed"})
        assert result.verdict == "BAD-ID"
        assert all(result.consulted[name] == "answered" for name in REGISTRIES)

    def test_not_asked_never_turns_a_bad_id_into_unchecked(self) -> None:
        """The trap: the tempting fix for the bug above is far worse than it.

        Marking an unqueried registry "unreachable" to stop it being claimed as
        consulted would push ``compare`` down its "nothing answered" branch, and
        every genuine BAD-ID — a fabricated or mistyped DOI, which is the
        finding this tool most exists to make — would come back UNCHECKED and
        pass CI.
        """
        result = compare(make_ref(), {}, asked={"crossref"})
        assert result.verdict == "BAD-ID"
        assert result.fails
        assert result.consulted["pubmed"] == "not-asked"

    def test_an_identifier_nobody_was_asked_about_is_not_a_bad_id(self) -> None:
        """The other half of the trap above.

        ``asked={"crossref"}`` must stay ``BAD-ID``: one registry answering
        "not mine" is evidence. An *empty* ``asked`` is the opposite situation.
        Nothing was consulted, so "resolves in no consulted registry" is
        vacuously true and reads as an accusation the run cannot support —
        a decision not to look is not evidence that the work does not exist.
        ``--no-isbn`` is the caller that reaches this.
        """
        result = compare(make_ref(), {}, asked=set())
        assert result.verdict == "UNCHECKED"
        assert not result.fails
        status = next(i for i in result.issues if i.kind == "not-asked")
        assert status.severity == "info"
        assert "not checked" in status.note

    def test_a_retraction_source_outage_does_not_excuse_a_fabricated_doi(self) -> None:
        """The worst way this tool could fail, and the narrowest path to it.

        Retraction Watch holds no bibliographic record and can never resolve an
        identifier, so its export being unreachable says nothing about whether a
        work exists. It shares the run-wide *unreachable* set with the
        registries that *can* answer that question, and the "nothing answered"
        branch must not read it: a bibliography full of invented DOIs would come
        back UNCHECKED and exit 0 because a side-channel was down, while
        ``consulted`` reported that Crossref, DataCite and PubMed all answered.
        """
        result = compare(
            make_ref(),
            {},
            unreachable={"retraction-watch"},
            asked={"crossref", "datacite", "pubmed", "retraction-watch"},
        )
        assert result.verdict == "BAD-ID"
        assert result.fails
        assert result.consulted["crossref"] == "answered"

    def test_a_real_registry_outage_is_still_unchecked(self) -> None:
        """The guard above must not go too far the other way: a registry that
        could have held the work being unreachable is exactly the ignorance
        UNCHECKED exists for.
        """
        result = compare(
            make_ref(),
            {},
            unreachable={"crossref", "retraction-watch"},
            asked={"crossref", "retraction-watch"},
        )
        assert result.verdict == "UNCHECKED"
        assert not result.fails

    def test_omitting_asked_entirely_still_reports_bad_id(self) -> None:
        """``asked=None`` means the caller did not say, not that it asked
        nothing. Collapsing the two would turn every genuine BAD-ID from a
        caller that never passed ``asked`` into a silent UNCHECKED — the exact
        regression the test above this one guards from the other direction.
        """
        result = compare(make_ref(), {})
        assert result.verdict == "BAD-ID"
        assert result.fails

    def test_an_outage_everywhere_is_still_unchecked(self) -> None:
        result = compare(
            make_ref(),
            {},
            unreachable={"crossref", "datacite", "pubmed"},
            asked={"crossref", "datacite", "pubmed"},
        )
        assert result.verdict == "UNCHECKED"
        assert not result.fails

    def test_a_caller_that_says_nothing_claims_nothing(self) -> None:
        """Omitting ``asked`` must not resurrect the overstatement.

        With no roster the only registries that can be *proved* to have taken
        part are those that answered or timed out. That understates the
        evidence, which is the safe direction — it can make a verdict look less
        well supported, never more — but it must never invent participation.
        """
        result = compare(make_ref(), {"crossref": make_record()})
        assert result.consulted["crossref"] == "answered"
        assert result.consulted["pubmed"] == "not-asked"

    def test_a_record_from_an_unlisted_registry_is_reported_too(self) -> None:
        """Whoever adds the next registry gets it in the record automatically."""
        result = compare(
            make_ref(),
            {"crossref": make_record()},
            asked={"crossref", "europepmc"},
        )
        assert result.consulted["europepmc"] == "answered"


class TestAlternateDate:
    """Citing the online-first date is right, and the report should say so.

    ``benign._year_online_first`` was written to supply that sentence and could
    never run: ``_check_year`` returned early whenever the stored year was one
    the registry carried, which is a superset of exactly the condition the rule
    tests. Its documented purpose — that the *reason* is stated — was never
    fulfilled, so a reader comparing ``year = {2020}`` against a landing page
    showing 2021 had no way to learn the tool had seen both.
    """

    def test_the_report_states_which_of_the_registrys_dates_is_cited(self) -> None:
        record = make_record(years={"print": 2021, "online": 2020})
        result = compare(make_ref(year=2020), {"crossref": record})
        note = next(i for i in result.issues if i.field == "year")
        assert note.severity == "info"
        assert "online" in note.note
        assert "2021" in note.note
        # Both dates are shown, so the reader can see what was compared.
        assert note.registry == "online=2020, print=2021"

    def test_saying_so_does_not_make_the_entry_look_defective(self) -> None:
        """The false-alarm side, and the reason this is not an artifact.

        Online-first is the norm at most journals, so recording it as a
        REGISTRY-ARTIFACT would relabel a large slice of an ordinary
        epidemiology bibliography as though the publisher's metadata were
        broken. Neither value is wrong here and the verdict must not move.
        """
        record = make_record(years={"print": 2021, "online": 2020})
        assert compare(make_ref(year=2020), {"crossref": record}).verdict == "OK"
        assert compare(make_ref(year=2021), {"crossref": record}).verdict == "OK"

    def test_citing_the_preferred_date_is_not_worth_a_line(self) -> None:
        """A note on every correct entry is noise, and noise is the failure mode."""
        record = make_record(years={"print": 2021, "online": 2020})
        result = compare(make_ref(year=2021), {"crossref": record})
        assert not result.issues

    def test_a_year_no_registry_holds_is_still_an_error(self) -> None:
        """The pairing: making the accepted case speak did not make it accept more."""
        record = make_record(years={"print": 2021, "online": 2020})
        result = compare(make_ref(year=2017), {"crossref": record})
        year = next(i for i in result.issues if i.field == "year")
        assert year.severity == "error"
        assert result.verdict == "FIELD-MISMATCH"

    def test_a_year_only_the_corroborating_registry_holds_is_explained_too(self) -> None:
        """NLM's ``DP`` and Crossref's ``issued`` disagree constantly.

        ``benign`` cannot explain this one — it only sees the primary record —
        so the fallback wording has to name the primary's preference itself
        rather than leaving the reader with an unexplained silence.
        """
        result = compare(
            make_ref(year=2017),
            {
                "crossref": make_record(years={"print": 2018}),
                "pubmed": make_pubmed(years={"issued": 2017}),
            },
        )
        year = next(i for i in result.issues if i.field == "year")
        assert year.severity == "info"
        assert "pubmed:issued" in year.note
        assert "crossref prefers 2018" in year.note
        assert result.verdict == "OK"


class TestDoiAlias:
    """The DOI is the lookup key, so it can only disagree with itself benignly.

    ``benign._doi_redirecting_prefix`` existed for the JSTOR case and was
    unreachable: ``classify`` was never once called with ``field="doi"``. A dead
    rule in a suppression list is worse than no rule, because
    ``docs/registry-artifacts.md`` promised JSTOR redirects were "reported as a
    note" and nothing reported anything.
    """

    def test_an_aggregator_doi_that_redirects_is_stated_and_not_failed(self) -> None:
        ref = make_ref(doi="10.2307/2669548")
        record = make_record(doi="10.1080/01621459.1999.10474144")
        result = compare(ref, {"crossref": record})
        alias = next(i for i in result.issues if i.field == "doi")
        assert alias.severity == "info"
        assert alias.kind == "alias"
        assert "aggregator DOI redirects" in alias.note
        assert alias.stored == "10.2307/2669548"
        assert alias.registry == "10.1080/01621459.1999.10474144"
        assert result.verdict == "OK"
        assert not result.fails

    def test_an_unexplained_alias_is_still_only_a_note(self) -> None:
        """No prefix rule matched, and it still cannot be a failure.

        The record is here *because* the stored DOI resolved to it, so the
        bibliography's identifier demonstrably works. Reporting that as a
        mismatch would accuse an entry of being wrong on the strength of the
        evidence that it is right.
        """
        result = compare(make_ref(), {"crossref": make_record(doi="10.9999/alias")})
        alias = next(i for i in result.issues if i.field == "doi")
        assert alias.severity == "info"
        assert "resolved to a record registered under a different DOI" in alias.note
        assert result.verdict == "OK"

    def test_a_case_difference_is_not_an_alias(self) -> None:
        """DOIs are case-insensitive, and Crossref echoes them back as deposited.

        ``10.1158/1055-9965.EPI-20-0378`` and ``...epi-20-0378`` turn up in the
        DOI and URL fields of the very same entry. A note on that would appear
        on entries that are character-for-character correct.
        """
        ref = make_ref(doi="10.1158/1055-9965.EPI-20-0378")
        record = make_record(doi="10.1158/1055-9965.epi-20-0378")
        result = compare(ref, {"crossref": record})
        assert not any(i.field == "doi" for i in result.issues)

    def test_a_doi_pointing_at_a_different_paper_is_still_wrong_work(self) -> None:
        """The pairing: the DOI note must not become an excuse.

        A redirect is benign *because the work is the same*. If the resolved
        record is a different paper, that is still WRONG-WORK — the alias note
        sits alongside the accusation, it does not replace it.
        """
        ref = make_ref(doi="10.2307/2669548")
        record = make_record(
            doi="10.1234/somethingelse",
            title="An entirely unrelated paper about marine biology",
            authors=[Name(family="Darwin"), Name(family="Wallace")],
        )
        result = compare(ref, {"crossref": record})
        assert result.verdict == "WRONG-WORK"
        assert result.fails

    def test_a_registry_with_no_doi_of_its_own_is_not_an_alias(self) -> None:
        """MEDLINE records carry no DOI until the client attaches the queried one."""
        result = compare(make_ref(), {"crossref": make_record(doi=None)})
        assert not any(i.field == "doi" for i in result.issues)


class TestTheRecordASuppressionRuleIsAskedAbout:
    """Every rule in ``benign`` is handed the record the right-hand value came from.

    Three of them read it — ``_container_leading_article`` MEDLINE's ``TA``,
    ``_pmid_pmc_accession`` its ``PMC`` line, ``_container_abbreviation``
    Crossref's ``short-container-title`` — and a Crossref deposit asked to
    explain a value PubMed supplied failed a correct entry citing *The Lancet*.
    No ``title`` or ``pages`` rule reads it today, so on those two checks the
    argument is unobservable in a verdict and a revert would go unnoticed; both
    fields let the corroborator fill a gap, so the day one does read it, it
    would be the primary's record answering for somebody else's value.
    """

    def _record_handed_to(
        self, field: str, monkeypatch: pytest.MonkeyPatch, ref: Reference, records: dict[str, Record]
    ) -> Record | None:
        seen: dict[str, Record] = {}
        real = bibaudit.benign.classify

        def spy(
            name: str, stored: object, registry: object, reference: Reference, record: Record
        ) -> str | None:
            seen.setdefault(name, record)
            return real(name, stored, registry, reference, record)

        monkeypatch.setattr(bibaudit.benign, "classify", spy)
        compare(ref, records)
        return seen.get(field)

    def test_a_title_rule_is_asked_about_the_record_the_title_came_from(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        record = self._record_handed_to(
            "title",
            monkeypatch,
            make_ref(title="A different paper about family history of cancer"),
            {
                "crossref": make_record(title=None),
                "pubmed": make_pubmed(title=make_record().title),
            },
        )

        assert record is not None
        assert record.source == "pubmed"

    def test_a_pages_rule_is_asked_about_the_record_the_pages_came_from(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        record = self._record_handed_to(
            "pages",
            monkeypatch,
            make_ref(pages="999-1001"),
            {"crossref": make_record(pages=None), "pubmed": make_pubmed(pages="473-483")},
        )

        assert record is not None
        assert record.source == "pubmed"


class TestTheListOfFieldsAdjudicated:
    """``CHECKED_FIELDS`` is a promise about what "verified" covers.

    Nothing in ``src/`` reads the tuple; ``docs/why.md`` prints it as "the
    whole list of stored fields compared against the registry", and the
    boundary it draws is what the page is for. A field this tool disagrees with
    a registry about and does not name there is a promise quietly broken, and a
    name in it nothing compares is one quietly invented.
    """

    #: One damaged reference per field, each against a record that is otherwise
    #: the entry's own. The title sits in the ``mismatch`` band on purpose:
    #: below ``wrong_work`` the finding is about the identifier rather than the
    #: field, and it is the field boundary being asserted here.
    def _mismatched_fields(self) -> set[str]:
        cases: list[tuple[dict[str, object], dict[str, object]]] = [
            ({"title": "Risk of pancreatic cancer associated with family history"}, {}),
            ({"authors": [Name(family="Zbragowitz", given="Q")]}, {}),
            ({"year": 1999}, {}),
            ({"container": "Journal of Something Else"}, {}),
            ({"volume": "48"}, {}),
            ({"issue": "3"}, {}),
            ({"pages": "999-1001"}, {}),
            ({"publisher": "Elsevier"}, {"publisher": "Oxford University Press"}),
            ({"pmid": "9500320"}, {}),
        ]
        seen: set[str] = set()
        for ref_kwargs, record_kwargs in cases:
            records = {
                "crossref": make_record(**record_kwargs),
                "pubmed": make_record(source="pubmed", pmid="29329392"),
            }
            result = compare(make_ref(**ref_kwargs), records)
            seen |= {i.field for i in result.issues if i.kind == "mismatch"}
        return seen

    def test_the_tuple_names_every_field_a_mismatch_can_be_reported_on(self) -> None:
        assert self._mismatched_fields() == set(CHECKED_FIELDS)

    def test_the_two_fields_inspected_without_being_adjudicated_stay_out(self) -> None:
        """The DOI is the lookup key and the entry's type is coarse everywhere.

        Both are read and both can produce an issue; neither can produce a
        ``mismatch``, which is why ``docs/why.md`` can say a stored field
        outside the tuple is never adjudicated while these two are still
        checked.
        """
        wrong_type = compare(make_ref(kind="book"), {"crossref": make_record()})
        aliased = compare(
            make_ref(doi="10.2307/2669548"),
            {"crossref": make_record(doi="10.1111/j.1540-5907.2000.tb00000.x")},
        )

        assert [(i.field, i.kind) for i in wrong_type.issues] == [("kind", "incompatible")]
        assert not [i for i in aliased.issues if i.kind == "mismatch"]
        assert not {"doi", "kind"} & set(CHECKED_FIELDS)


class TestPmidCheck:
    """A PMID stored beside a DOI is a second claim, and it can be wrong.

    The DOI fetched the record, so it cannot disagree with the bibliography in
    any way that counts (see :class:`TestDoiAlias`). A PMID sitting next to it
    fetched nothing: it is the entry's own assertion that PubMed holds this
    work under that number, and a citation whose two identifiers name two works
    is exactly the shape a mis-transcribed reference takes.

    The reference these build on is a real pairing. According to PubMed, PMID
    29329392 is Molina-Montes et al., *Int J Epidemiol* 2018;47(2):473-483,
    [DOI](https://doi.org/10.1093/ije/dyx269) — the entry ``make_ref`` returns.
    PMID 9500320 is Wakefield et al., *Lancet* 1998,
    [DOI](https://doi.org/10.1016/s0140-6736(97)11096-0), and 20137807 is the
    2010 notice retracting it,
    [DOI](https://doi.org/10.1016/S0140-6736(10)60175-4).
    """

    def test_a_pmid_naming_another_work_is_reported_as_a_warning(self) -> None:
        """One side of this comparison was looked up and the other was not.

        The stored number is never put to PubMed, so "these two identifiers
        name two works" is an inference from one lookup. A bibliography holding
        a PMID that has since stopped answering — ``efetch`` for 20000157
        returns an empty body today — would fail a build on that inference,
        and a false alarm costs more than a miss.
        """
        result = compare(
            make_ref(pmid="9500320"),
            {"crossref": make_record(), "pubmed": make_record(source="pubmed", pmid="29329392")},
        )
        issue = next(i for i in result.issues if i.field == "pmid")

        assert (issue.kind, issue.severity) == ("mismatch", "warning")
        assert (issue.stored, issue.registry) == ("9500320", "29329392")
        assert issue.source == "pubmed"
        assert "not itself looked up" in issue.note
        assert result.verdict == "INCOMPLETE"
        assert not result.fails

    def test_the_pmid_pubmed_holds_for_the_doi_is_silent(self) -> None:
        result = compare(
            make_ref(pmid="29329392"),
            {"crossref": make_record(), "pubmed": make_record(source="pubmed", pmid="29329392")},
        )
        assert not any(i.field == "pmid" for i in result.issues)
        assert result.verdict == "OK"

    def test_pubmed_answering_as_the_only_registry_is_still_compared(self) -> None:
        """Crossref silent, PubMed holding the DOI: the check is not corroborator-only.

        The record fills the ``primary`` slot in that case, and reading the
        PMID off the corroborator alone would silence the check on precisely
        the entries PubMed is the sole witness for.
        """
        result = compare(
            make_ref(pmid="9500320"), {"pubmed": make_record(source="pubmed", pmid="29329392")}
        )
        assert any(i.field == "pmid" and i.kind == "mismatch" for i in result.issues)

    def test_a_registry_holding_no_pmid_is_not_a_disagreement(self) -> None:
        """Absence of evidence is not a mismatch.

        Crossref carries no PMID at all, and this is also the shape of PubMed
        having been asked and having had nothing, of ``--no-corroborate``, and
        of a DOI PubMed answered for under more than one PMID — where
        ``registries/pubmed.py`` withholds the value rather than pick one. None
        of them may read as "PubMed says your PMID is wrong".
        """
        result = compare(make_ref(pmid="9500320"), {"crossref": make_record()})
        assert not any(i.field == "pmid" for i in result.issues)
        assert result.verdict == "OK"

    def test_a_pmid_that_did_the_looking_up_is_never_compared(self) -> None:
        """With no DOI stored, the PMID is the key and the record resolved from it.

        ``audit._pmid_key`` fetches under that number, so a disagreement here
        cannot mean what it means above — it would be the record contradicting
        the question it answered. The DOI gets the same treatment for the same
        reason.
        """
        result = compare(
            make_ref(doi=None, pmid="9500320"),
            {"pubmed": make_record(source="pubmed", doi=None, pmid="29329392")},
        )
        assert not any(i.field == "pmid" for i in result.issues)

    def test_a_pmcid_in_the_pmid_field_is_left_alone(self) -> None:
        """``PMCID: PMC5860629`` sits one line under the PMID in a Zotero Extra block.

        A value that is not PMID-shaped was never a candidate for the number
        PubMed holds, and accusing it of disagreeing would report an encoding
        or a copy-paste as a wrong citation.
        """
        result = compare(
            make_ref(pmid="PMC5860629"),
            {"crossref": make_record(), "pubmed": make_record(source="pubmed", pmid="29329392")},
        )
        assert not any(i.field == "pmid" for i in result.issues)
        assert result.verdict == "OK"

    def test_a_notice_cited_by_its_own_pair_of_identifiers_is_clean(self) -> None:
        """Citing a retraction notice is legitimate and must stay silent.

        Nothing in this check reads ``updated-by``, ``update-to`` or MEDLINE's
        ``RIN``/``ROF``, so there is no relation direction to get backwards:
        the notice's own DOI and its own PMID agree, and that is the whole of
        the test.
        """
        result = compare(
            make_ref(doi="10.1016/S0140-6736(10)60175-4", pmid="20137807"),
            {
                "crossref": make_record(doi="10.1016/S0140-6736(10)60175-4"),
                "pubmed": make_record(
                    source="pubmed", doi="10.1016/S0140-6736(10)60175-4", pmid="20137807"
                ),
            },
        )
        assert not any(i.field == "pmid" for i in result.issues)

    def test_the_paper_doi_beside_the_notice_pmid_is_still_reported(self) -> None:
        """The pairing for the test above: the silence must not become a licence.

        These are two Lancet documents twelve years apart with two titles, and
        a reader following one identifier lands somewhere the other does not.
        """
        result = compare(
            make_ref(doi="10.1016/S0140-6736(97)11096-0", pmid="20137807"),
            {
                "crossref": make_record(doi="10.1016/S0140-6736(97)11096-0"),
                "pubmed": make_record(
                    source="pubmed", doi="10.1016/S0140-6736(97)11096-0", pmid="9500320"
                ),
            },
        )
        issue = next(i for i in result.issues if i.field == "pmid")

        assert (issue.stored, issue.registry) == ("20137807", "9500320")
        assert issue.kind == "mismatch"
        assert result.verdict == "INCOMPLETE"

    def test_the_records_own_pmc_accession_is_not_a_second_citation(self) -> None:
        """PMID 28520842 carries ``PMC  - PMC5860629``: one record, two numbers.

        NLM issues both for one deposited article and Zotero keeps them on
        adjacent lines of one ``Extra`` box, so a ``pmid`` field comes to hold
        the PMC number with its prefix dropped. That number names the record
        being compared against, which is the one thing this check's claim —
        two identifiers, two citations — cannot be true of.
        """
        result = compare(
            make_ref(pmid="5860629"),
            {
                "crossref": make_record(),
                "pubmed": make_record(
                    source="pubmed", pmid="28520842", raw={"PMC": ["PMC5860629"]}
                ),
            },
        )

        assert not any(i.field == "pmid" for i in result.issues)
        artifact = next(i for i in result.suppressed if i.field == "pmid")
        assert artifact.note == "stored number is this record's own PMC accession"
        assert result.verdict == "REGISTRY-ARTIFACT"
        assert not result.fails

    def test_an_unrelated_number_is_still_reported_when_a_pmc_line_exists(self) -> None:
        """The other half: the rule reads the accession, not the presence of one.

        A record carrying a ``PMC`` line must not become a record whose PMID
        cannot be questioned.
        """
        result = compare(
            make_ref(pmid="9500320"),
            {
                "crossref": make_record(),
                "pubmed": make_record(
                    source="pubmed", pmid="28520842", raw={"PMC": ["PMC5860629"]}
                ),
            },
        )

        assert any(i.field == "pmid" and i.kind == "mismatch" for i in result.issues)

    def test_the_suppression_names_the_registry_that_carries_both_numbers(self) -> None:
        """Crossref supplies no PMID and no ``PMC`` line, so it explains nothing.

        Every suppression invites a reader to check it against the registry
        printed beside it, and this is the one check whose right-hand value can
        never have come from the primary: only PubMed sets ``Record.pmid``.
        Under Crossref's name the line asks the reader to look for a ``PMC``
        accession on a record that has no such field.
        """
        result = compare(
            make_ref(pmid="5860629"),
            {
                "crossref": make_record(),
                "pubmed": make_record(
                    source="pubmed", pmid="28520842", raw={"PMC": ["PMC5860629"]}
                ),
            },
        )

        [artifact] = [i for i in result.suppressed if i.field == "pmid"]
        assert artifact.source == "pubmed"


class TestAnAnswerAboutAnotherRecord:
    """A registry that answered *around* an identifier has not answered about it.

    ``PubMed.by_pmids`` declines to adopt a record whose own ``PMID`` line is
    not the number requested — right, since that record is another paper's
    metadata and another paper's retraction status. But the request did come
    back with something, and ``BAD-ID`` rests on PubMed's own "no record under
    that number", which is a different answer: ``efetch`` for a deleted PMID
    returns HTTP 200 and an empty body.
    """

    def test_an_identifier_answered_around_is_unchecked_not_bad_id(self) -> None:
        result = compare(
            make_ref(doi=None, pmid="9500320"),
            {},
            asked={"pubmed"},
            inconclusive={"pubmed": ("20137807",)},
        )

        assert result.verdict == "UNCHECKED"
        assert not result.fails
        issue = result.issues[0]
        assert (issue.field, issue.kind, issue.severity) == (
            "identifier",
            "inconclusive",
            "info",
        )
        assert issue.stored == "9500320"
        assert issue.source == "pubmed"
        # Named so a reader can look up what came back instead.
        assert "20137807" in issue.note

    def test_a_batch_that_was_not_answered_at_all_names_no_substitute(self) -> None:
        """The 404-on-``efetch`` case: ignorance with nothing to name.

        The note must not invent a number, and the verdict must not be the one
        an answer would have earned.
        """
        result = compare(
            make_ref(doi=None, pmid="9500320"),
            {},
            asked={"pubmed"},
            inconclusive={"pubmed": ()},
        )

        assert result.verdict == "UNCHECKED"
        assert result.issues[0].note.startswith("pubmed did not answer for it")

    def test_the_note_says_that_an_answer_about_another_record_settles_nothing(
        self,
    ) -> None:
        """Without that clause the line reads as an accusation, which is the
        one thing an ``UNCHECKED`` may never be.

        "pubmed answered with 20137807 instead" on its own invites the reader
        to conclude the stored number is wrong. It is not evidence of that: no
        registry was ever asked what 9500320 names, and only an *absence* may
        accuse a bibliography.
        """
        result = compare(
            make_ref(doi=None, pmid="9500320"),
            {},
            asked={"pubmed"},
            inconclusive={"pubmed": ("20137807",)},
        )

        assert result.issues[0].note.endswith(
            "an answer about another record is not one about this identifier"
        )

    def test_an_answer_of_no_such_record_is_still_bad_id(self) -> None:
        """The finding this must not swallow.

        PubMed answering that it holds nothing under a number is the whole of
        the evidence a ``BAD-ID`` on a PMID rests on, and it reaches ``compare``
        as an empty ``inconclusive``.
        """
        result = compare(make_ref(doi=None, pmid="9500320"), {}, asked={"pubmed"})

        assert result.verdict == "BAD-ID"
        assert result.fails


class TestAConsortiumMedlineFilesApartFromTheByline:
    """Crossref credits a consortium in the byline; MEDLINE files it in ``CN``.

    Replayed against ``tests/data/pubmed_collective_creator.txt``, NCBI's own
    bytes for PMID 42552006 (10.1136/bmjopen-2025-107667). Crossref's byline is
    nine creators with ``for the ITC Project Collaborators`` at position 6;
    MEDLINE's ``FAU`` list is the same eight people. Compared position against
    position the consortium reads as a substitution for Kress, and every
    position after it is shifted.
    """

    def _entry(self, authors: list[Name]) -> Result:
        ref = Reference(
            key="chunghall2026itc",
            locator="references.bib:12",
            kind="article",
            pmid="42552006",
            title=(
                "Changes in knowledge of the major harms of tobacco smoking and "
                "secondhand smoke exposure among adults who smoke: findings from "
                "26 countries of the International Tobacco Control (ITC) Project "
                "(2002-2022) and 32 countries of the Global Adult Tobacco Survey "
                "(GATS) (2008-2020)"
            ),
            authors=authors,
            year=2026,
            container="BMJ Open",
            volume="16",
            issue="8",
            pages="e107667",
        )
        return compare(ref, {"pubmed": pubmed_record("pubmed_collective_creator.txt")})

    def _crossref_byline(self) -> list[Name]:
        return [
            Name(family="Chung-Hall", given="Janet"),
            Name(family="Fong", given="Geoffrey T"),
            Name(family="Meng", given="Gang"),
            Name(family="Craig", given="Lorraine V"),
            Name(family="Indome", given="Eunice O"),
            Name(literal="for the ITC Project Collaborators", collective=True),
            Name(family="Kress", given="Alissa C"),
            Name(family="Shi", given="Jing"),
            Name(family="Ahluwalia", given="Indu B"),
        ]

    def test_the_shifted_byline_does_not_fail_the_build(self) -> None:
        result = self._entry(self._crossref_byline())

        assert not result.fails
        assert not [i for i in result.issues if i.field == "authors"]

    def test_the_consortium_is_named_in_the_suppression(self) -> None:
        """A suppression a reader cannot look up is one nobody can challenge."""
        [artifact] = [i for i in self._entry(self._crossref_byline()).suppressed
                      if i.field == "authors"]

        assert artifact.note == (
            "byline carries collective creator(s) the registry files apart: "
            "for the ITC Project Collaborators"
        )

    def test_a_substituted_person_still_fails(self) -> None:
        """The alignment is the evidence, not a tolerance the escape grants."""
        byline = self._crossref_byline()
        byline[7] = Name(family="Nobody", given="X Y")
        result = self._entry(byline)

        assert result.verdict == "FIELD-MISMATCH"
        # The shape did not hold, so the ordinary positional comparison ran and
        # the whole shifted tail is reported — including the substitution.
        assert "mismatch" in [i.kind for i in result.issues if i.field == "authors"]
        # The byline is one longer than MEDLINE's, so its last creator has no
        # position to be compared at. MEDLINE does carry `Ahluwalia`, one place
        # earlier, and saying otherwise would be false against the record being
        # quoted — so that position is stated, not accused.
        assert [i.note for i in result.suppressed] == [
            "credited elsewhere in the registry byline"
        ]


class TestAConsortiumThatIsTheWholeByline:
    """MEDLINE's ``CN`` is the byline, and there is no ``FAU`` beside it.

    Replayed against ``tests/data/pubmed_corporate_author.txt``, NCBI's own
    bytes for PMID 42538063 (10.1016/j.fertnstert.2026.03.026): a committee
    opinion in *Fertility and Sterility* credited to ``CN - Practice Committee
    of the American Society for Reproductive Medicine`` and to nobody else.

    The class next door is the case where ``CN`` sits *beside* a personal
    byline and is left unread, because the position it belongs at is what a
    comparison would need. Here there is no position to get wrong: the record
    has exactly one creator, and reading none of them meant an entry could
    invent the whole byline and be told every checked field agrees.
    """

    def _entry(self, authors: list[Name]) -> Result:
        ref = Reference(
            key="asrm2026implantation",
            locator="references.bib:31",
            kind="article",
            pmid="42538063",
            title="Recurrent implantation failure: a committee opinion",
            authors=authors,
            year=2026,
            container="Fertility and Sterility",
            volume="126",
            issue="2",
            pages="277-293",
        )
        return compare(ref, {"pubmed": pubmed_record("pubmed_corporate_author.txt")})

    def test_a_fabricated_byline_is_no_longer_compared_against_nothing(self) -> None:
        """Every other field of this entry is the record's own.

        With the corporate byline unread the record carried no creators, so
        ``_check_authors`` returned before comparing anything and the entry was
        reported ``OK`` — the clean bill of health on an invented byline that
        the full author comparison exists to prevent.
        """
        result = self._entry([
            Name(family="Fabricated", given="Author Q."),
            Name(family="Invented", given="Second R."),
            Name(family="Nonexistent", given="Third S."),
        ])

        [artifact] = [i for i in result.suppressed if i.field == "authors"]
        assert artifact.note == "registry lists a collective author"
        assert artifact.registry == (
            "Practice Committee of the American Society for Reproductive Medicine"
        )
        assert artifact.source == "pubmed"

    def test_the_organisation_is_one_creator_named_whole(self) -> None:
        """An entry citing the committee is right, and matches it outright.

        The name carries no comparison marker word this project would have to
        recognise for it to be one creator — it is one because ``CN`` is where
        NLM writes an organisation — and it survives to the report unsplit.
        """
        result = self._entry([
            Name(
                literal="Practice Committee of the American Society for Reproductive Medicine",
                collective=True,
            )
        ])

        assert not result.fails
        assert not [i for i in result.issues if i.field == "authors"]


class TestMedlineJournalTitleOnThePmidPath:
    """The journal name a PMID-resolved entry is compared against is MEDLINE's.

    A reference whose only identifier is a PMID is resolved by PubMed alone, so
    ``compare`` promotes PubMed to primary and there is no corroborator to
    supply a second spelling of anything. The container it compares is
    therefore ``JT``, and ``JT`` is NLM's own filing title: the leading article
    dropped, a place-of-publication qualifier appended wherever the bare title
    would be ambiguous. ``Lancet (London, England)`` is what a correct Lancet
    entry was being failed against, and *The Lancet*, *BMJ* and *Science* are
    not obscure journals.

    Replayed against ``tests/data/compare_pubmed_wakefield_notice.txt``, NCBI's
    own bytes. The notice rather than the paper, because the paper is retracted
    and a ``RETRACTED`` verdict would hide whichever verdict the container
    produced.
    """

    def _notice(self, container: str) -> Result:
        return compare(
            wakefield_notice_ref(doi=None, pmid="20137807", container=container),
            {"pubmed": pubmed_record("compare_pubmed_wakefield_notice.txt")},
        )

    def test_the_journals_own_name_does_not_fail_the_build(self) -> None:
        """*The Lancet* is what the masthead, Crossref and every .bib say."""
        result = self._notice("The Lancet")
        assert not result.fails
        assert not [i for i in result.issues if i.field == "container"]
        assert [i.note for i in result.suppressed if i.field == "container"] == [
            "registry files the journal without its leading article"
        ]

    def test_the_abbreviation_is_matched_against_the_registrys_own_alternate(self) -> None:
        """An entry exported from PubMed or EndNote stores ``Lancet``.

        Not a suppression: ``TA`` is a title PubMed itself carries for the
        journal, so this is the chapter-with-two-containers path, and the note
        has to name PubMed as the registry that holds the value — the reader is
        being invited to check it against a page headed with the other one.
        """
        result = self._notice("Lancet")
        assert not result.fails
        note = next(i for i in result.issues if i.field == "container")
        assert (note.kind, note.severity) == ("alternate-title", "info")
        assert note.note == "pubmed also carries 'Lancet' for this work"

    def test_a_sibling_journal_still_fails(self) -> None:
        """The pairing. *The Lancet Oncology* is a different journal.

        Citing the parent title for a paper that appeared in the offshoot is
        one of the commonest real citation errors there is, and it survives
        both routes: the qualifier is never stripped, and only a leading
        article may differ.
        """
        result = self._notice("The Lancet Oncology")
        assert result.verdict == "FIELD-MISMATCH"
        assert [i.kind for i in result.issues if i.field == "container"] == ["mismatch"]

    def test_an_unrelated_journal_still_fails(self) -> None:
        result = self._notice("BMJ")
        assert result.verdict == "FIELD-MISMATCH"
        assert [i.kind for i in result.issues if i.field == "container"] == ["mismatch"]


class TestAlternateContainerWitness:
    """An alternate title is only checkable if the note names who holds it.

    The two registries do not supply the same alternates: PubMed's is
    MEDLINE's ``TA``, Crossref's are the several ``container-title`` values a
    chapter carries. A note telling the reader that *Crossref* also carries
    ``Int J Cancer`` sends them to a landing page that does not, which is the
    same class of defect as reporting a registry that was never asked.
    """

    def _result(self) -> Result:
        return compare(
            make_ref(container="Int J Cancer"),
            {
                "crossref": make_record(container="International Journal of Cancer"),
                "pubmed": make_pubmed(
                    container="International journal of cancer",
                    container_alternates=["Int J Cancer"],
                ),
            },
        )

    def test_the_corroborators_alternate_is_read_at_all(self) -> None:
        """Reading only the primary's leaves ``TA`` unseen on the DOI path.

        The entry is then explained away by ``benign._container_abbreviation``
        instead — a suppression standing in for a value PubMed actually
        carries, which is a weaker claim about a correct entry.
        """
        result = self._result()

        assert not result.fails
        assert [i.kind for i in result.issues if i.field == "container"] == ["alternate-title"]
        assert not result.suppressed

    def test_the_note_names_the_registry_that_holds_it(self) -> None:
        [note] = [i for i in self._result().issues if i.field == "container"]

        assert note.note == "pubmed also carries 'Int J Cancer' for this work"
        # The *source* is still the registry whose value was compared, which is
        # the other one: two registries in one finding, each named for what it
        # actually did.
        assert note.source == "crossref"


class TestTheBylineSuppressionNamesWhoSuppliedTheByline:
    """An author artifact names the registry the compared byline came from.

    ``_check_authors`` falls back to the corroborator when the primary's
    ``authors`` is empty — a Crossref deposit with no ``author`` array and
    PubMed's ``FAU`` list beside it — and it already names that registry on the
    count and mismatch issues. The suppression printed from the same walk named
    the primary, so one byline produced a REGISTRY-ARTIFACT crediting a
    registry that supplied no creator at all.
    """

    def _result(self) -> Result:
        return compare(
            make_ref(
                authors=[
                    Name(family="Gomez-Rubio", given="P"),
                    Name(family="Molina-Montes", given="E"),
                ]
            ),
            {
                "crossref": make_record(authors=[]),
                "pubmed": make_pubmed(),
            },
        )

    def test_the_reordering_is_suppressed_at_all(self) -> None:
        result = self._result()

        assert not result.fails
        assert [i.note for i in result.suppressed if i.field == "authors"] == [
            "reordered",
            "reordered",
        ]

    def test_the_suppression_names_the_registry_that_supplied_the_byline(self) -> None:
        sources = {i.source for i in self._result().suppressed if i.field == "authors"}

        assert sources == {"pubmed"}


class TestTheRecordThatSuppliedTheValueIsTheOneJudged:
    """A benign rule is shown the record the compared value came from.

    The primary registry is Crossref or DataCite on every DOI-resolved
    reference and PubMed is the corroborator, so a Crossref deposit carrying
    no ``container-title`` puts PubMed's ``JT`` on the right-hand side. Judging
    that against Crossref's record asks a registry holding neither ``JT`` nor
    ``TA`` to explain a MEDLINE filing title, and *The Lancet* — correct, and
    named by both the masthead and Crossref — failed the build.
    """

    def _result(self, container: str) -> Result:
        return compare(
            make_ref(container=container),
            {
                "crossref": make_record(container=None),
                "pubmed": make_pubmed(
                    container="Lancet (London, England)",
                    container_alternates=["Lancet"],
                ),
            },
        )

    def test_the_corroborators_own_alternate_explains_its_own_value(self) -> None:
        result = self._result("The Lancet")

        assert not result.fails
        assert not [i for i in result.issues if i.field == "container"]
        assert [i.note for i in result.suppressed if i.field == "container"] == [
            "registry files the journal without its leading article"
        ]

    def test_the_suppression_names_the_registry_whose_value_it_explains(self) -> None:
        [artifact] = [i for i in self._result("The Lancet").suppressed if i.field == "container"]

        assert artifact.source == "pubmed"

    def test_a_different_journal_still_fails(self) -> None:
        """*The Lancet Oncology* is another journal in the same family."""
        result = self._result("The Lancet Oncology")

        assert result.verdict == "FIELD-MISMATCH"
        assert [i.kind for i in result.issues if i.field == "container"] == ["mismatch"]


class TestMedlineSocietyExpansionOnThePmidPath:
    """``JT`` also spells out the society, and ``TA`` does not always rescue it.

    Replayed against ``tests/data/pubmed_society_expansion.txt``, NCBI's own
    bytes for PMID 32430337 (Michaud et al., 10.1158/1055-9965.EPI-20-0378).
    Its ``TA`` is a genuine abbreviation, ``Cancer Epidemiol Biomarkers Prev``,
    so the entry that stores the journal's own name has only the expanded
    ``JT`` to be compared against — and was failed against it.
    """

    def _entry(self, container: str) -> Result:
        ref = Reference(
            key="michaud2020methylation",
            locator="references.bib:44",
            kind="article",
            pmid="32430337",
            title=(
                "DNA Methylation-Derived Immune Cell Profiles, CpG Markers of "
                "Inflammation, and Pancreatic Cancer Risk"
            ),
            authors=[Name(family="Michaud", given="Dominique S"), Name(et_al=True)],
            year=2020,
            container=container,
            volume="29",
            issue="8",
            pages="1577-1585",
        )
        return compare(ref, {"pubmed": pubmed_record("pubmed_society_expansion.txt")})

    def test_the_journals_own_name_does_not_fail_the_build(self) -> None:
        result = self._entry("Cancer Epidemiology, Biomarkers & Prevention")

        assert not result.fails
        assert not [i for i in result.issues if i.field == "container"]
        assert [i.note for i in result.suppressed if i.field == "container"] == [
            "registry appends its own subtitle to the journal name"
        ]

    def test_the_abbreviation_is_still_matched_against_the_registrys_own_alternate(
        self,
    ) -> None:
        """``TA`` reaches the entry exported from PubMed or EndNote, as before."""
        result = self._entry("Cancer Epidemiol Biomarkers Prev")

        assert not result.fails
        note = next(i for i in result.issues if i.field == "container")
        assert (note.kind, note.severity) == ("alternate-title", "info")

    def test_a_different_journal_still_fails(self) -> None:
        """*Cancer Epidemiology* is another journal, and citing it is an error."""
        result = self._entry("Cancer Epidemiology")

        assert result.verdict == "FIELD-MISMATCH"
        assert [i.kind for i in result.issues if i.field == "container"] == ["mismatch"]


class TestAParallelJournalTitleOnThePmidPath:
    """One serial, two names, joined in ``JT`` by a spaced equals sign.

    Replayed against ``tests/data/pubmed_parallel_title.txt``, NCBI's own bytes
    for PMID 42526877: ``JT - Journal of preventive medicine and public health
    = Yebang Uihakhoe chi``. Both halves are the journal's own name, and on the
    PMID path the whole of ``JT`` was the only value an entry could be compared
    against — so an entry storing either half reported `container/mismatch`.
    """

    def _entry(self, container: str) -> Result:
        ref = Reference(
            key="park2026humidifier",
            locator="references.bib:52",
            kind="article",
            pmid="42526877",
            title=(
                "The Humidifier Disinfectant Disaster in Korea: Implications for "
                "Preventive Medicine and Public Health"
            ),
            authors=[Name(family="Park", given="Sue K"), Name(et_al=True)],
            year=2026,
            container=container,
        )
        return compare(ref, {"pubmed": pubmed_record("pubmed_parallel_title.txt")})

    def test_the_english_name_does_not_fail_the_build(self) -> None:
        result = self._entry("Journal of Preventive Medicine and Public Health")

        assert not result.fails
        note = next(i for i in result.issues if i.field == "container")
        assert (note.kind, note.severity) == ("alternate-title", "info")
        assert note.note == (
            "pubmed also carries 'Journal of preventive medicine and public health' "
            "for this work"
        )

    def test_the_other_name_is_accepted_too(self) -> None:
        """NLM puts the transliterated title second here and first elsewhere."""
        result = self._entry("Yebang Uihakhoe chi")

        assert not result.fails

    def test_a_different_journal_still_fails(self) -> None:
        result = self._entry("Journal of Preventive Medicine")

        assert result.verdict == "FIELD-MISMATCH"
        assert [i.kind for i in result.issues if i.field == "container"] == ["mismatch"]


class TestAMastheadAcronymOnThePmidPath:
    """The publisher's masthead sets the acronym ahead of the name; NLM does not.

    Replayed against ``tests/data/pubmed_acronym_prefix.txt``, NCBI's own bytes
    for PMID 42550479 (10.1093/jnci/djag268). ``JT`` is *Journal of the
    National Cancer Institute* and the journal's own site heads every page
    *JNCI: Journal of the National Cancer Institute*, which is what a reference
    manager stores. On the PMID path ``JT`` is the only container there is.
    """

    def _entry(self, container: str) -> Result:
        ref = Reference(
            key="wang2026chip",
            locator="references.bib:31",
            kind="article",
            pmid="42550479",
            title=(
                "The joint association of clonal hematopoiesis of indeterminate "
                "potential and biological aging with cancer risk among 332,911 "
                "individuals"
            ),
            authors=[Name(family="Wang", given="Li"), Name(et_al=True)],
            year=2026,
            container=container,
        )
        return compare(ref, {"pubmed": pubmed_record("pubmed_acronym_prefix.txt")})

    def test_the_masthead_form_does_not_fail_the_build(self) -> None:
        result = self._entry("JNCI: Journal of the National Cancer Institute")

        assert not result.fails
        assert [i.note for i in result.suppressed if i.field == "container"] == [
            "stored name prefixes the journal's own acronym"
        ]

    def test_the_registrys_own_spelling_needs_no_suppression(self) -> None:
        result = self._entry("Journal of the National Cancer Institute")

        assert not result.fails
        assert not [i for i in result.suppressed if i.field == "container"]

    def test_another_journal_behind_the_acronym_still_fails(self) -> None:
        result = self._entry("JCO: Journal of Clinical Oncology")

        assert result.verdict == "FIELD-MISMATCH"
        assert [i.kind for i in result.issues if i.field == "container"] == ["mismatch"]


class TestMedlineDatesOnThePmidPath:
    """A work published ahead of its issue has two years, and MEDLINE has both.

    Replayed against ``tests/data/pubmed_epub_ahead_of_issue.txt``, NCBI's own
    bytes for PMID 41474069 (*Int J Cancer*, 10.1002/ijc.70265): ``DP - 2026
    May 1``, ``DEP - 20251231``. On the DOI path Crossref supplies the same
    pair as ``published-print``/``published-online`` and either is accepted; a
    PMID-resolved entry has PubMed and nothing else, so a bibliography
    populated when the paper went online was failed against the issue year of
    the very record that carries both.
    """

    def _entry(self, year: int) -> Result:
        ref = Reference(
            key="keynote042china",
            locator="references.bib:31",
            kind="article",
            pmid="41474069",
            title=(
                "Five-year outcomes of pembrolizumab versus chemotherapy in Chinese "
                "patients with non-small-cell lung cancer and programmed cell death "
                "ligand 1 tumor proportion score >/=1%: KEYNOTE-042 China study"
            ),
            authors=[Name(family="Wu", given="Yi-Long"), Name(et_al=True)],
            year=year,
            container="International Journal of Cancer",
            volume="158",
            issue="9",
            pages="2429-2439",
        )
        return compare(ref, {"pubmed": pubmed_record("pubmed_epub_ahead_of_issue.txt")})

    def test_the_year_the_work_went_online_is_accepted_and_named(self) -> None:
        result = self._entry(2025)

        assert not result.fails
        [issue] = [i for i in result.issues if i.field == "year"]
        assert (issue.kind, issue.severity) == ("alternate-date", "info")
        assert issue.note == "cites the online date; registry prefers 2026"

    def test_the_issue_year_is_accepted_with_nothing_to_explain(self) -> None:
        result = self._entry(2026)

        assert not result.fails
        assert not [i for i in result.issues if i.field == "year"]

    def test_a_year_neither_date_carries_still_fails(self) -> None:
        """The pairing. Accepting a second date is not accepting any date."""
        result = self._entry(2024)

        assert result.verdict == "FIELD-MISMATCH"
        assert [i.kind for i in result.issues if i.field == "year"] == ["mismatch"]


class TestYearTolerance:
    def test_online_first_year_is_accepted(self) -> None:
        """A work online in 2020 and printed in 2021 has two correct years."""
        record = make_record(years={"print": 2021, "online": 2020})
        assert compare(make_ref(year=2020), {"crossref": record}).verdict == "OK"
        assert compare(make_ref(year=2021), {"crossref": record}).verdict == "OK"

    def test_a_year_the_registry_does_not_hold_is_an_error(self) -> None:
        record = make_record(years={"print": 2021, "online": 2020})
        assert compare(make_ref(year=2017), {"crossref": record}).verdict == "FIELD-MISMATCH"


class TestAPageLocatorNeitherSideCanRead:
    """``first_page`` returned ``""`` for what it could not parse, and ``""``
    equals ``""`` — so two unreadable locators agreed with each other and the
    entry came back ``OK``.

    SAGE's online-only pagination is the witnessed shape: 24 of 3,250 MEDLINE
    ``PG`` values on a live sample of 3,500 citations spanning 1992-2026 are
    ``NP…`` or Roman front matter, and the 21 whose publisher deposited a page
    at all agree with MEDLINE on the opening locator character for character.
    """

    def test_a_different_opening_locator_is_an_error(self) -> None:
        result = compare(
            make_ref(pages="NP585-NP599"),
            {"crossref": make_record(pages="NP580-NP599")},
        )

        assert result.verdict == "FIELD-MISMATCH"
        assert any(i.field == "pages" and i.severity == "error" for i in result.issues)

    def test_a_closing_page_written_short_is_still_the_same_article(self) -> None:
        """MEDLINE's ``NP2661-76`` against Crossref's ``NP2661-NP2676``."""
        result = compare(
            make_ref(pages="NP2661-76"),
            {"crossref": make_record(pages="NP2661-NP2676")},
        )

        assert result.verdict == "OK"

    def test_two_locators_with_nothing_readable_agree_only_as_the_same_text(
        self,
    ) -> None:
        """The residue the emptiness guard covers.

        A value carrying no alphanumeric is not a locator, so the empty string
        it yields is not a match — but reporting the identical text against
        itself would be a finding about nothing.
        """
        same = compare(make_ref(pages="-"), {"crossref": make_record(pages="-")})
        different = compare(make_ref(pages="-"), {"crossref": make_record(pages="?")})

        assert same.verdict == "OK"
        assert different.verdict == "FIELD-MISMATCH"


class TestAnEntryWithNoTitleAtAll:
    """A title the registry has and the entry does not name is an error.

    Not the ``INCOMPLETE`` warning every other absent field gets. Title is what
    ``confirm_without_id`` matches on and what ``_check_title`` scores
    ``wrong-work`` from, so an entry with none has nothing for either to work
    with, and reporting it as a gap in the entry would understate that.
    """

    def test_the_registrys_title_is_named_in_the_finding(self) -> None:
        """A finding that does not print the title is one nobody can act on."""
        result = compare(make_ref(title=None), {"crossref": make_record()})

        missing = [i for i in result.issues if i.field == "title" and i.kind == "missing"]
        assert [i.severity for i in missing] == ["error"]
        assert missing[0].registry.startswith("Risk of pancreatic cancer")
        assert missing[0].source == "crossref"

    def test_it_fails(self) -> None:
        result = compare(make_ref(title=None), {"crossref": make_record()})

        assert result.verdict == "FIELD-MISMATCH"
        assert result.fails

    def test_a_registry_with_no_title_either_reports_nothing_about_titles(self) -> None:
        """Both sides silent is ignorance, and ignorance is not a finding."""
        result = compare(
            make_ref(title=None), {"crossref": make_record(title=None)}
        )

        assert not [i for i in result.issues if i.field == "title"]


class TestAPageTheCorroboratingRegistryAgreesWith:
    """Two registries disagree about the pages and the entry matches one of them.

    That is a disagreement between the registries, not a defect in the
    bibliography, so it is reported at ``info`` against both values rather than
    as a ``pages/mismatch`` error against the primary's — the same rule
    ``year/alternate-date`` follows. Reported at all, because which registry a
    reader should believe is theirs to decide.
    """

    def _result(self) -> Result:
        return compare(
            make_ref(pages="473-483"),
            {
                "crossref": make_record(pages="470-483"),
                "pubmed": make_record(source="pubmed", pages="473-83"),
            },
        )

    def test_it_does_not_fail_the_entry(self) -> None:
        result = self._result()

        assert result.verdict != "FIELD-MISMATCH"
        assert not result.fails

    def test_both_registries_values_are_printed(self) -> None:
        """A line naming one of them leaves the reader unable to check it."""
        disputed = [i for i in self._result().issues if i.field == "pages"]

        assert [i.kind for i in disputed] == ["disputed"]
        assert disputed[0].severity == "info"
        assert disputed[0].source == "both"
        assert "crossref='470-483'" in disputed[0].registry
        assert "pubmed='473-83'" in disputed[0].registry

    def test_a_page_neither_registry_carries_is_still_an_error(self) -> None:
        """The escape needs the corroborator to actually agree with the entry."""
        result = compare(
            make_ref(pages="999-1000"),
            {
                "crossref": make_record(pages="470-483"),
                "pubmed": make_record(source="pubmed", pages="473-83"),
            },
        )

        assert any(
            i.field == "pages" and i.kind == "mismatch" and i.severity == "error"
            for i in result.issues
        )


class TestIncompleteness:
    def test_a_field_the_registry_has_and_the_entry_lacks_is_a_warning(self) -> None:
        """Incompleteness is worth surfacing but is not evidence of fabrication."""
        result = compare(make_ref(pages=None), {"crossref": make_record()})
        assert result.verdict == "INCOMPLETE"
        assert not result.fails

    def test_the_corroborator_fills_a_field_the_primary_never_deposited(self) -> None:
        """A gap in Crossref's deposit is not a gap in the evidence.

        Crossref deposits routinely omit the issue; MEDLINE's ``IP`` carries
        it. Reading the primary alone would report the entry's correct issue as
        a disagreement with nothing, and its wrong one as no disagreement at
        all. Filling the gap is not arbitration — it happens only where the
        primary has said nothing.
        """
        records = {
            "crossref": make_record(issue=None),
            "pubmed": make_record(source="pubmed", issue="9"),
        }

        assert compare(make_ref(issue="9"), records).verdict == "OK"
        wrong = compare(make_ref(issue="2"), records)
        issue = next(i for i in wrong.issues if i.field == "issue")
        assert (issue.registry, issue.source) == ("9", "pubmed")


class TestCosmetic:
    def test_apostrophe_glyphs_are_regularised_away_entirely(self) -> None:
        """A curly versus straight apostrophe is not reported at all.

        ``clean()`` maps the Unicode punctuation variants onto their ASCII
        equivalents, so the two titles are equal before comparison begins. This
        is deliberate: which apostrophe a publisher deposited is not a fact
        about the citation, and reporting it would be noise on a large
        bibliography.
        """
        ref = make_ref(title="Alcohol Intake and Parkinson’s Disease Risk")
        record = make_record(title="Alcohol Intake and Parkinson's Disease Risk")
        assert compare(ref, {"crossref": record}).verdict == "OK"

    def test_capitalisation_differences_are_cosmetic(self) -> None:
        """Registries mix sentence case, Title Case and ALL CAPS freely."""
        ref = make_ref(title="RISK OF PANCREATIC CANCER ASSOCIATED WITH FAMILY "
                             "HISTORY OF CANCER AND OTHER MEDICAL CONDITIONS BY "
                             "ACCOUNTING FOR SMOKING AMONG RELATIVES")
        result = compare(ref, {"crossref": make_record()})
        assert result.verdict == "COSMETIC"
        assert not result.fails


class TestDisputed:
    def test_registries_disagreeing_with_each_other_is_not_a_defect(self) -> None:
        """The tool has no basis for choosing between two curated sources."""
        crossref = make_record(volume="47")
        pubmed = Record(source="pubmed", volume="48", title=crossref.title, years={"issued": 2018})
        result = compare(make_ref(volume="48"), {"crossref": crossref, "pubmed": pubmed})
        assert result.verdict == "DISPUTED"
        assert not result.fails


class TestKindCompatibility:
    def test_a_book_resolving_to_a_journal_article_is_flagged(self) -> None:
        """Proposing a DOI for a book usually turns up a review of the book."""
        ref = make_ref(kind="book")
        result = compare(ref, {"crossref": make_record(kind="journal-article")})
        assert any(i.field == "kind" for i in result.issues)

    def test_preprint_and_article_are_compatible(self) -> None:
        ref = make_ref(kind="preprint")
        result = compare(ref, {"crossref": make_record(kind="journal-article")})
        assert not any(i.field == "kind" for i in result.issues)


class TestAWorkFiledUnderTwoTypes:
    """A registry naming several types means all of them, and the entry may cite any.

    Replayed against ``tests/data/pubmed_data_descriptor.txt``, NCBI's own
    bytes for PMID 42557261 — a data descriptor in *Scientific Data*, ``PT -
    Dataset`` then ``PT - Journal Article``. NLM writes ``PT``
    alphabetically, so the type that leads the record is not the structural
    one, and a correct ``@article`` was reported as disagreeing with the
    resolved work's type: INCOMPLETE, in the summary counts, and a failure
    under ``--fail-on INCOMPLETE``. Any type the registry itself carries is
    acceptable, on the same terms as any year and any container title it
    carries.
    """

    def _entry(self, kind: str) -> Result:
        ref = Reference(
            key="zeng2026fnirs",
            locator="references.bib:3",
            kind=kind,
            pmid="42557261",
            title=(
                "An fNIRS Dataset for Cognitive Decoding during a Multi-day "
                "Block-design Stroop Task"
            ),
            authors=[Name(family="Zeng", given="Lingwei")],
            year=2026,
            container="Scientific data",
            volume="13",
            issue="1",
        )
        return compare(ref, {"pubmed": pubmed_record("pubmed_data_descriptor.txt")})

    def test_the_type_further_down_the_registrys_list_is_accepted(self) -> None:
        result = self._entry("article")

        assert not [i for i in result.issues if i.field == "kind"]

    def test_the_type_leading_the_registrys_list_is_accepted_too(self) -> None:
        """Neither of the two is privileged: the registry stated both."""
        assert not [i for i in self._entry("dataset").issues if i.field == "kind"]

    def test_a_type_the_registry_named_neither_of_still_disagrees(self) -> None:
        """The pairing. A ``@book`` against this record is still a finding."""
        [issue] = [i for i in self._entry("book").issues if i.field == "kind"]

        assert issue.severity == "warning"
        assert issue.stored == "book"

    def test_the_finding_names_every_type_the_registry_carries(self) -> None:
        """A reader deciding who is wrong needs to see what the registry said."""
        [issue] = [i for i in self._entry("book").issues if i.field == "kind"]

        assert issue.registry == "dataset, article"


class TestVerdictFor:
    def test_error_free_issue_set_does_not_fail(self) -> None:
        issues = [Issue(field="pages", kind="missing", severity="warning")]
        assert verdict_for(issues, []) == "INCOMPLETE"

    def test_retraction_dominates_everything(self) -> None:
        issues = [Issue(field="volume", kind="mismatch", severity="error")]
        assert verdict_for(issues, [], retracted=True) == "RETRACTED"


#: What ``benign`` produces: a documented registry defect, true for everybody.
ARTIFACT = Issue(
    field="authors", kind=ARTIFACT_KIND, severity="info",
    note="registry surname is mojibake (UTF-8 read as Latin-1)",
)

#: What ``suppress.apply`` produces from a project's ``.bibaudit.toml``: one
#: person's decision, on one project, recorded with a reason.
ADJUDICATION = Issue(
    field="publisher", kind="suppressed:mismatch", severity="info",
    note="imprint mergers churn these names; not tracked here",
)


class TestAdjudicationIsNotARegistryDefect:
    """Two claims that shared one verdict and should not have.

    README defines ``REGISTRY-ARTIFACT`` as "a difference explained by a known
    registry defect", which is a statement about the world, reproducible by
    anyone from ``docs/registry-artifacts.md``. A ``.bibaudit.toml``
    adjudication is a statement about this project's judgement. Both are
    non-failing and both stay visible; a reader has to be able to tell which one
    is holding an entry up.
    """

    def test_a_documented_registry_defect_still_reads_as_one(self) -> None:
        assert verdict_for([], [ARTIFACT]) == "REGISTRY-ARTIFACT"

    def test_a_project_local_decision_is_labelled_as_a_decision(self) -> None:
        assert verdict_for([], [ADJUDICATION]) == "ADJUDICATED"

    def test_a_human_decision_outranks_a_documented_defect(self) -> None:
        """An entry carrying both must not report only the reassuring half."""
        assert verdict_for([], [ARTIFACT, ADJUDICATION]) == "ADJUDICATED"
        assert verdict_for([], [ADJUDICATION, ARTIFACT]) == "ADJUDICATED"

    def test_neither_one_softens_a_real_finding(self) -> None:
        """The pairing: adjudicating one field says nothing about another.

        A wrong volume is still a wrong volume on an entry whose publisher
        difference was waved through, and an adjudication that quietly
        outranked an error would be a way to hide findings by writing an
        unrelated rule.
        """
        error = [Issue(field="volume", kind="mismatch", severity="error")]
        assert verdict_for(error, [ADJUDICATION]) == "FIELD-MISMATCH"
        assert verdict_for(error, [ADJUDICATION], retracted=True) == "RETRACTED"

    def test_an_incompleteness_still_outranks_both(self) -> None:
        """A gap the reader can fix beats anything already decided about."""
        warning = [Issue(field="pages", kind="missing", severity="warning")]
        assert verdict_for(warning, [ADJUDICATION]) == "INCOMPLETE"

    def test_the_artifacts_compare_produces_are_recognised_as_artifacts(self) -> None:
        """Anchors the classification against the real producer, not a fixture.

        ``verdict_for`` distinguishes the two by the ``kind`` each producer
        stamps on the issue. If ``compare`` ever stopped writing
        ``registry-artifact`` there, every documented registry defect would
        start reporting as somebody's local decision — and this fixture-based
        class would not have noticed.
        """
        ref = make_ref(container="Int J Cancer")
        record = make_record(container="International Journal of Cancer")
        result = compare(ref, {"crossref": record})
        assert result.verdict == "REGISTRY-ARTIFACT"
        assert [i.kind for i in result.suppressed] == [ARTIFACT_KIND]


class TestConfirmWithoutId:
    def test_confirmation_requires_more_than_a_title_match(self) -> None:
        """Searching a book title reliably returns a review of the book."""
        ref = make_ref(doi=None, kind="book", year=1998)
        candidate = Record(
            source="crossref",
            doi="10.1234/review",
            title=ref.title,
            kind="journal-article",
            years={"print": 1999},
        )
        record, reason = confirm_without_id(ref, [candidate])
        assert record is None
        assert "type" in reason

    def test_a_fully_corroborated_candidate_is_accepted(self) -> None:
        ref = make_ref(doi=None)
        candidate = make_record(doi="10.1093/ije/dyx269")
        record, reason = confirm_without_id(ref, [candidate])
        assert record is not None
        assert record.doi == "10.1093/ije/dyx269"
        assert "corroboration" in reason

    def test_a_disagreeing_first_author_is_refused(self) -> None:
        ref = make_ref(doi=None)
        candidate = make_record(authors=[Name(family="Wallace"), Name(family="Darwin")])
        record, _ = confirm_without_id(ref, [candidate])
        assert record is None


class TestThresholds:
    def test_books_get_a_lower_bar_than_articles(self) -> None:
        thresholds = Thresholds()
        assert thresholds.title_bands("book") < thresholds.title_bands("article")
