"""Verdict derivation and per-field comparison.

These run entirely offline, so they assert the *logic* rather than the state of
any registry. Any live-registry check belongs behind the ``network`` marker
declared in ``pyproject.toml``; no such test exists yet, so
``uv run pytest -m network`` currently selects nothing.

Most cases build their records by hand. The retraction ones do not: they replay
saved registry responses through the real ``registries.crossref`` and
``registries.pubmed`` code, because the direction rule that decides whether a
work *was* retracted or *is* the notice lives in those clients, and a hand-built
``Record`` would restate the assumption under test instead of checking it. The
payloads in ``tests/data/compare_*`` were fetched from the live APIs on
2026-08-01 and are kept verbatim, defects included.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bibaudit.compare import Thresholds, compare, confirm_without_id, verdict_for
from bibaudit.model import ARTIFACT_KIND, Issue, Name, Record, Reference
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

    ``years`` uses MEDLINE's ``issued`` slot and there is no ``kind``: PubMed
    carries no Crossref-style type string, so a record built here must not
    accidentally corroborate more than the real client can.
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

        ``Retracted Publication`` is a paper that was retracted.  ``Retraction
        of Publication`` *is* the notice announcing one — an ordinary, citable
        document, and citing it is not a defect in anybody's bibliography. The
        two strings differ by one word, so any test looser than the equality
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
            retraction_kind="Retraction of Publication",
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
        NLM's, whose publication type for the notice is now ``Retraction
        Notice`` where the older snapshot in ``tests/data/`` still reads
        ``Retraction of Publication``. Both are one word away from ``Retracted
        Publication``, which is why ``registries.pubmed`` matches on equality.
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
        assert not result.issues

    def test_datacite_going_down_says_nothing_about_retraction(self) -> None:
        """DataCite's schema has no retraction element and its client sets none.

        Naming it would manufacture a doubt that a reachable DataCite could not
        have resolved, on every dataset and preprint in the file.
        """
        result = compare(make_ref(), {"crossref": make_record()}, unreachable={"datacite"})
        assert result.verdict == "OK"
        assert not result.issues

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
        }

    def test_a_registry_that_answered_and_held_nothing_still_counts_as_asked(self) -> None:
        """An authoritative "I do not have this" is evidence, not silence.

        It is the whole basis of BAD-ID: a 404 is a fact. A representation that
        could not distinguish it from "not asked" would understate exactly the
        evidence that justifies the tool's strongest field-level accusation.
        """
        result = compare(make_ref(), {}, asked={"crossref", "datacite", "pubmed"})
        assert result.verdict == "BAD-ID"
        assert set(result.consulted.values()) == {"answered"}

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


class TestYearTolerance:
    def test_online_first_year_is_accepted(self) -> None:
        """A work online in 2020 and printed in 2021 has two correct years."""
        record = make_record(years={"print": 2021, "online": 2020})
        assert compare(make_ref(year=2020), {"crossref": record}).verdict == "OK"
        assert compare(make_ref(year=2021), {"crossref": record}).verdict == "OK"

    def test_a_year_the_registry_does_not_hold_is_an_error(self) -> None:
        record = make_record(years={"print": 2021, "online": 2020})
        assert compare(make_ref(year=2017), {"crossref": record}).verdict == "FIELD-MISMATCH"


class TestIncompleteness:
    def test_a_field_the_registry_has_and_the_entry_lacks_is_a_warning(self) -> None:
        """Incompleteness is worth surfacing but is not evidence of fabrication."""
        result = compare(make_ref(pages=None), {"crossref": make_record()})
        assert result.verdict == "INCOMPLETE"
        assert not result.fails


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
