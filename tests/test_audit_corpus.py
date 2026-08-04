"""Findings from the first networked run over a real 438-entry bibliography.

The corpus is a private 438-entry epidemiology bibliography:
438 entries, every one carrying a DOI, exported through Crossref's own
content-negotiated BibTeX. Everything pinned here was produced by running the
tool against it with the network on and then re-reading the cached registry
response by hand; the fixtures in ``tests/data/audit_*.json`` are those cached
responses, verbatim.

Two kinds of test live here, and the difference matters:

* **Live tests** assert behaviour the tool has today and must not lose. Several
  of them are the "true positive still fires" half of a pair — the defect that
  *superficially resembles* a benign case and must survive whatever suppression
  is added for that case.
* **``xfail`` tests** are the false alarms the run exposed, each written as the
  assertion that will hold once the suppression exists. They are marked rather
  than deleted so the hole stays visible and stays attached to the concrete
  instance that revealed it. ``xfail_strict`` is off, so one turning green
  reports as ``xpassed`` and never breaks the suite for whoever fixes it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

from bibaudit.audit import AuditOptions, _asked_registries, audit, resolve
from bibaudit.compare import compare
from bibaudit.model import ANSWERED, NOT_ASKED, Name, Record, Reference, Result
from bibaudit.names import compare_author_lists, names_agree
from bibaudit.normalize import clean
from bibaudit.registries.crossref import Crossref
from bibaudit.registries.http import Transient
from bibaudit.registries.retractions import RetractionStatus

#: The package re-exports the ``audit`` *function* as ``bibaudit.audit``, so the
#: attribute of that name is the function, not the module. The module has to be
#: fetched by name to patch the registry construction it does at call time.
audit_module = import_module("bibaudit.audit")

DATA = Path(__file__).parent / "data"

#: The entry PubMed was asked about and did not hold. Marin et al., "A common
#: polymorphism in the XPC gene ...", Cancer Epidemiol Biomarkers Prev 2004.
MARIN_DOI = "10.1158/1055-9965.1788.13.11"


def _load(name: str) -> dict[str, Any]:
    """One cached Crossref ``work`` object."""
    with (DATA / name).open(encoding="utf-8") as handle:
        payload: dict[str, Any] = json.load(handle)
    return payload


def _creators(work: dict[str, Any]) -> list[Name]:
    """Crossref's creator array as :class:`Name` objects, order preserved.

    Mirrors ``crossref._parse_creators`` deliberately: these tests are about
    what a *creator list shaped like this* does to the comparison, and importing
    the private parser would make them silent if that parser stopped marking
    ``{"name": ...}`` entries collective.
    """
    names: list[Name] = []
    for person in work.get("author") or []:
        if person.get("name"):
            names.append(Name(literal=clean(person["name"]), collective=True))
        elif person.get("family") or person.get("given"):
            names.append(
                Name(family=clean(person.get("family") or ""), given=clean(person.get("given") or ""))
            )
    return names


def _people_only(names: list[Name]) -> list[Name]:
    return [n for n in names if not n.collective]


def _compare_chapter(container: str) -> Result:
    """The ``hainaut2011biobank`` entry, with *container* substituted, compared.

    Built from the cached Crossref record so the only thing varying between the
    three container tests is the value the bibliography holds.
    """
    work = _load("audit_crossref_series_and_book_container.json")
    record = Crossref(client=None)._record_from_work(work)  # type: ignore[arg-type]
    ref = Reference(
        key="hainaut2011biobank",
        locator="references.bib:87",
        kind="chapter",
        doi=work["DOI"],
        title=work["title"][0],
        authors=_creators(work),
        year=2011,
        container=container,
        pages="179-191",
    )
    return compare(ref, {"crossref": record})


# ---------------------------------------------------------------------------
# Stubs. Nothing here touches the network; the whole file must pass offline.
# ---------------------------------------------------------------------------


class _Recorder:
    """A registry stand-in that records what it was asked and answers narrowly."""

    def __init__(
        self,
        name: str,
        *,
        records: dict[str, Record] | None = None,
        transient: bool = False,
    ) -> None:
        self.name = name
        self.records = dict(records or {})
        self.transient = transient
        self.by_dois_calls: list[list[str]] = []

    def make(self, client: object) -> _Recorder:
        """Constructor stand-in, so ``_build`` itself is the code under test.

        Patching the registry *classes* rather than ``_build`` keeps the
        ``pubmed=... if options.corroborate else None`` gate inside real code,
        which is precisely what the ``--no-corroborate`` test is about.
        """
        return self

    def by_dois(self, dois: list[str]) -> dict[str, Record]:
        self.by_dois_calls.append(list(dois))
        if self.transient:
            raise Transient(f"{self.name}: simulated outage")
        return {doi: self.records[doi] for doi in dois if doi in self.records}


class _SearchRecorder:
    """Stands in for ``registries.search.Search``.

    ``audit._audit_unidentified`` goes through this one seam for an
    identifier-less entry rather than through Crossref directly; recording
    calls here is what lets ``search_calls`` (renamed ``candidate_calls``,
    matching ``Search.candidates``) still prove the lookup ran.
    """

    def __init__(
        self, *, sources: tuple[str, ...] = ("crossref", "europepmc", "openalex")
    ) -> None:
        self.sources = sources
        self.candidate_calls: list[Reference] = []

    def make(self, client: object, **kwargs: object) -> _SearchRecorder:
        """Accepts and ignores ``use_europepmc``/``use_openalex`` — see
        ``_build``, which now passes both through to the real ``Search``.
        """
        return self

    def candidates(self, ref: Reference, rows: int = 5) -> list[Record]:
        self.candidate_calls.append(ref)
        return []


class _RetractionsRecorder:
    """Stands in for ``registries.retractions.Retractions``.

    Mandatory, not optional like ``search`` above defaulting via a factory
    happens to make it look: this file never patches ``Client`` itself, so an
    unstubbed ``Retractions`` would build a *real* one around a live client and
    reach Retraction Watch's export and NCBI over the network the moment any
    DOI-bearing ``_ref()`` (the file's default) goes through ``resolve``.
    """

    def __init__(self) -> None:
        self.status_for_calls: list[list[str]] = []

    def make(self, client: object) -> _RetractionsRecorder:
        return self

    def status_for(self, dois: list[str]) -> RetractionStatus:
        self.status_for_calls.append(list(dois))
        return RetractionStatus(notices={}, unreachable=frozenset())


@dataclass(slots=True)
class _Stubs:
    crossref: _Recorder
    datacite: _Recorder
    pubmed: _Recorder | None
    search: _SearchRecorder = field(default_factory=_SearchRecorder)
    retractions: _RetractionsRecorder = field(default_factory=_RetractionsRecorder)


def _install(monkeypatch: pytest.MonkeyPatch, stubs: _Stubs) -> None:
    """Point ``audit``'s registry classes at *stubs*; no socket is ever opened."""
    monkeypatch.setattr(audit_module, "Crossref", stubs.crossref.make)
    monkeypatch.setattr(audit_module, "DataCite", stubs.datacite.make)
    if stubs.pubmed is not None:
        monkeypatch.setattr(audit_module, "PubMed", stubs.pubmed.make)
    monkeypatch.setattr(audit_module, "Search", stubs.search.make)
    monkeypatch.setattr(audit_module, "Retractions", stubs.retractions.make)


def _options(tmp_path: Path, **kwargs: Any) -> AuditOptions:
    return AuditOptions(cache_dir=tmp_path / "cache", **kwargs)


def _ref(doi: str | None = MARIN_DOI, **kwargs: Any) -> Reference:
    return Reference(
        key="marin2004xpc",
        locator="references.bib:1",
        kind="article",
        doi=doi,
        title="A common polymorphism in the XPC gene",
        authors=[Name(family="Marin", given="M S")],
        year=2004,
        **kwargs,
    )


def _record(source: str = "crossref", **kwargs: Any) -> Record:
    kwargs.setdefault("doi", MARIN_DOI)
    kwargs.setdefault("title", "A common polymorphism in the XPC gene")
    kwargs.setdefault("authors", [Name(family="Marin", given="M S")])
    kwargs.setdefault("years", {"issued": 2004})
    return Record(source=source, **kwargs)


# ---------------------------------------------------------------------------
# What a run says it consulted
# ---------------------------------------------------------------------------


class TestConsultationRecordsWhatWasActuallyAsked:
    """``Result.consulted`` is the run's own record of the evidence it weighed.

    Every one of these is a statement about honesty, not about verdicts:
    ``asked`` reaches nothing in ``compare`` except ``result.consulted``, so
    none of them can make a citation pass that would otherwise fail.
    """

    def test_a_registry_that_answered_with_nothing_is_not_reported_as_unasked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``marin2004xpc`` finding, reduced to its shape.

        Crossref holds 10.1158/1055-9965.1788.13.11; PubMed does not index that
        article's DOI. The live run reported ``"pubmed": "not-asked"`` for it,
        while the cached ``esearch`` proves PubMed was asked about that exact
        DOI in a batch of twenty and returned nineteen PMIDs. "Asked and had
        nothing" is corroborating evidence and has to survive into the report.
        """
        stubs = _Stubs(
            crossref=_Recorder("crossref", records={MARIN_DOI: _record()}),
            datacite=_Recorder("datacite"),
            pubmed=_Recorder("pubmed"),  # asked, holds nothing
        )
        _install(monkeypatch, stubs)

        result = audit([_ref()], _options(tmp_path))[0]

        assert stubs.pubmed is not None
        assert stubs.pubmed.by_dois_calls == [[MARIN_DOI]]
        assert result.consulted["pubmed"] == ANSWERED
        assert result.answered("pubmed")

    def test_no_corroborate_and_no_retraction_check_together_report_pubmed_as_unasked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The paired negative, and the reason the fix is not "always answered".

        Without this, "PubMed answered and had nothing" and "PubMed was never
        consulted at all" collapse back into one string, and a report claims a
        curated second opinion nobody sought — which is the defect
        ``model.Consultation`` was introduced to end. Both flags are turned
        off here, not just ``--no-corroborate``: ``Retractions`` asks PubMed
        for its retraction status independently of bibliographic corroboration
        (see ``AuditOptions.retraction_check``), so ``--no-corroborate`` alone
        no longer leaves PubMed wholly unconsulted — the paired test right
        below this one is exactly that case.
        """
        stubs = _Stubs(
            crossref=_Recorder("crossref", records={MARIN_DOI: _record()}),
            datacite=_Recorder("datacite"),
            pubmed=_Recorder("pubmed"),
        )
        _install(monkeypatch, stubs)

        result = audit(
            [_ref()], _options(tmp_path, corroborate=False, retraction_check=False)
        )[0]

        assert stubs.pubmed is not None
        assert stubs.pubmed.by_dois_calls == []
        assert stubs.retractions.status_for_calls == []
        assert result.consulted["pubmed"] == NOT_ASKED
        assert not result.answered("pubmed")

    def test_no_corroborate_alone_still_asks_pubmed_for_retraction_status(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--no-corroborate`` drops bibliographic corroboration, not retraction status.

        The two are independent flags for independent reasons: corroboration
        roughly doubles request volume and is worth skipping for a corpus with
        no PMIDs, but a citation to a retracted paper is the one verdict this
        project treats as too costly to risk missing — see
        ``AuditOptions.retraction_check``. So PubMed is still asked, just for
        its ``ECI``/``PT`` retraction signal alone, and the report must say so
        rather than claim nobody was consulted.
        """
        stubs = _Stubs(
            crossref=_Recorder("crossref", records={MARIN_DOI: _record()}),
            datacite=_Recorder("datacite"),
            pubmed=_Recorder("pubmed"),
        )
        _install(monkeypatch, stubs)

        result = audit([_ref()], _options(tmp_path, corroborate=False))[0]

        assert stubs.pubmed is not None
        # The *bibliographic* corroboration call never happens...
        assert stubs.pubmed.by_dois_calls == []
        # ...but the independent retraction check still asks about this DOI.
        assert stubs.retractions.status_for_calls == [[MARIN_DOI]]
        assert result.consulted["pubmed"] == ANSWERED
        assert result.answered("pubmed")

    def test_datacite_is_unasked_when_crossref_answered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``resolve`` asks DataCite only about what Crossref did not answer for.

        The whole 438-entry corpus took this branch: Crossref held every DOI, so
        DataCite was genuinely never queried, and ``not-asked`` is the truth.
        """
        stubs = _Stubs(
            crossref=_Recorder("crossref", records={MARIN_DOI: _record()}),
            datacite=_Recorder("datacite"),
            pubmed=_Recorder("pubmed"),
        )
        _install(monkeypatch, stubs)

        result = audit([_ref()], _options(tmp_path))[0]

        assert stubs.datacite.by_dois_calls == []
        assert result.consulted["datacite"] == NOT_ASKED

    def test_datacite_that_answered_with_nothing_is_reported_answered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A DOI nobody holds: both registries answered, and both must say so.

        This is the evidence a ``BAD-ID`` rests on. Reporting the registries
        that established the absence as ``not-asked`` would make the strongest
        finding the tool can make look like the weakest.
        """
        stubs = _Stubs(
            crossref=_Recorder("crossref"),
            datacite=_Recorder("datacite"),
            pubmed=_Recorder("pubmed"),
        )
        _install(monkeypatch, stubs)

        result = audit([_ref()], _options(tmp_path))[0]

        assert stubs.datacite.by_dois_calls == [[MARIN_DOI]]
        assert result.verdict == "BAD-ID"
        assert result.consulted["crossref"] == ANSWERED
        assert result.consulted["datacite"] == ANSWERED
        assert result.consulted["pubmed"] == ANSWERED

    @pytest.mark.parametrize("corroborate", [True, False])
    @pytest.mark.parametrize("crossref_has_it", [True, False])
    @pytest.mark.parametrize("crossref_down", [True, False])
    def test_the_asked_set_matches_what_resolve_actually_queried(
        self, corroborate: bool, crossref_has_it: bool, crossref_down: bool
    ) -> None:
        """Anti-drift: ``_asked_registries`` restates ``resolve``'s query plan.

        It is a second copy of that plan, and a second copy is a liability. This
        drives the real ``resolve`` with recording stubs and compares its claim
        against which stubs were actually called, so the copy cannot rot into a
        confident lie about the evidence.
        """
        ref = _ref()
        stubs = _Stubs(
            crossref=_Recorder(
                "crossref",
                records={MARIN_DOI: _record()} if crossref_has_it else None,
                transient=crossref_down,
            ),
            datacite=_Recorder("datacite"),
            pubmed=_Recorder("pubmed") if corroborate else None,
        )
        registries = audit_module._Registries(
            crossref=stubs.crossref,  # type: ignore[arg-type]
            datacite=stubs.datacite,  # type: ignore[arg-type]
            pubmed=stubs.pubmed,  # type: ignore[arg-type]
            search=stubs.search,  # type: ignore[arg-type]
            retractions=stubs.retractions,  # type: ignore[arg-type]
        )

        records, unreachable = resolve([ref], registries)
        found = records[MARIN_DOI]

        really_called = {
            name
            for name, stub in (
                ("crossref", stubs.crossref),
                ("datacite", stubs.datacite),
                ("pubmed", stubs.pubmed),
            )
            if stub is not None and stub.by_dois_calls
        }
        # `_resolve_retractions` queries both, for every DOI-bearing reference
        # regardless of whether it resolved — see its own docstring — so
        # `stubs.retractions` being called at all (it always is: `ref` always
        # carries a DOI here) means both names were genuinely asked.
        if stubs.retractions.status_for_calls:
            really_called |= {"pubmed", "retraction-watch"}
        claimed = _asked_registries(
            ref, found, unreachable, AuditOptions(corroborate=corroborate)
        )

        assert claimed == really_called

    def test_an_entry_with_no_identifier_claims_every_search_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Search widens to three sources; DataCite and PubMed still never see it.

        An identifier-less entry goes through ``registries.search``, not
        ``registries.crossref`` directly, and by default that consults
        Crossref, Europe PMC and OpenAlex (see
        ``registries.search.Search.sources``). DataCite and PubMed remain
        untouched — neither has a free-text search this tool uses.
        """
        stubs = _Stubs(
            crossref=_Recorder("crossref"),
            datacite=_Recorder("datacite"),
            pubmed=_Recorder("pubmed"),
        )
        _install(monkeypatch, stubs)

        result = audit([_ref(doi=None)], _options(tmp_path))[0]

        assert stubs.search.candidate_calls
        assert result.consulted["crossref"] == ANSWERED
        assert result.consulted["europepmc"] == ANSWERED
        assert result.consulted["openalex"] == ANSWERED
        assert result.consulted["datacite"] == NOT_ASKED
        assert result.consulted["pubmed"] == NOT_ASKED

    def test_no_search_claims_nothing_at_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--no-search`` on an identifier-less entry consulted nobody."""
        stubs = _Stubs(
            crossref=_Recorder("crossref"),
            datacite=_Recorder("datacite"),
            pubmed=_Recorder("pubmed"),
        )
        _install(monkeypatch, stubs)

        result = audit(
            [_ref(doi=None)], _options(tmp_path, search_unidentified=False)
        )[0]

        assert stubs.search.candidate_calls == []
        assert all(state == NOT_ASKED for state in result.consulted.values())


# ---------------------------------------------------------------------------
# The corpus's dominant false alarm: collectives interleaved in the byline
# ---------------------------------------------------------------------------


class TestInterleavedCollectiveCreators:
    """Crossref credits consortia *inside* the author array; BibTeX cannot.

    Crossref's record for 10.1158/1055-9965.epi-23-0009 (Kim et al., *Cancer
    Epidemiol Biomarkers Prev* 2023, stored as ``kim2023abo``) has nine
    creators: seven people and, at positions 5 and 7, ``{"name": "for the
    Pancreatic Cancer Cohort Consortium (PanScan)"}`` and ``{"name": "for the
    Pancreatic Cancer Case-Control Consortium (PanC4)"}``. Crossref's *own*
    content-negotiated BibTeX — which is what produced this bibliography —
    emits neither, so the entry holds exactly the seven people.

    Compared position against position, registry #5 is a consortium and stored
    #5 is Alison P. Klein, and every position after each insertion is shifted.
    On the 438-entry corpus this shape accounts for 27 of 30 ``FIELD-MISMATCH``
    verdicts and all 15 remaining ``authors``-only ``INCOMPLETE`` ones — 42
    entries, 9.6% of the file, none of which disagrees with Crossref about a
    single person. It also accounts for all 1446 differences suppressed as
    ``reordered``: with the lists offset, every stored name is present somewhere
    in the registry list and vice versa, so that escape absorbs the whole tail
    and reports it as a reordering that never happened.

    Measured over the corpus: all 44 works whose Crossref creator array carries
    a collective align **exactly** — zero mismatches, zero count difference —
    once the collectives are dropped from the registry side. That measurement is
    the evidence the suppression rests on, and it is restated as a test below so
    the licence stops holding the moment the fixture stops supporting it.

    Closed by ``names._interleaved_collectives``. Three tests here are its
    true-positive half — a substituted person, a person missing from the stored
    list, and a *person's* name deposited in the collective slot — and each must
    keep failing the entry.
    """

    def test_the_registry_creator_array_really_does_interleave_collectives(self) -> None:
        """Guard on the fixture itself, so the tests below cannot go vacuous."""
        work = _load("audit_crossref_interleaved_collective.json")
        creators = _creators(work)

        assert len(creators) == 9
        assert [i for i, n in enumerate(creators, 1) if n.collective] == [5, 7]
        assert creators[4].literal.endswith("(PanScan)")

    def test_a_collective_the_bibliography_omits_is_not_a_person_substitution(self) -> None:
        """Closed by ``names._interleaved_collectives``.

        Both consortia are named in the reason, not counted: the whole claim is
        that these particular creators are organisations, and a suppression a
        reader cannot look up is a check nobody can audit.
        """
        work = _load("audit_crossref_interleaved_collective.json")
        registry = _creators(work)
        stored = _people_only(registry)

        diff = compare_author_lists(stored, registry)

        assert diff.mismatches == []
        assert not diff.count_differs
        assert "PanScan" in diff.reasons[1]
        assert "PanC4" in diff.reasons[1]

    def test_a_consortium_with_no_marker_word_in_its_name_is_still_excused(self) -> None:
        """Which creators are collectives is Crossref's answer, not a word list.

        An earlier version of this rule also demanded a marker word
        (*consortium*, *group*, *collaborators*) in the literal, as a guard
        against a deposit filing a person under ``name``. On the real corpus it
        failed 8 correct entries: ``PanScan and PanC4 consortia`` is plural, and
        ``DiscovEHR``, ``GSK``, ``AstraZeneca``, ``Bristol Myers Squibb`` and
        ``UK Biobank`` carry no marker at all. The ``<organization>`` slot in
        Crossref's deposit schema is the evidence; a vocabulary is a guess about
        the string, and it guessed wrong.
        """
        work = _load("audit_crossref_interleaved_collective.json")
        registry = _creators(work)
        stored = _people_only(registry)
        registry.insert(3, Name(literal="DiscovEHR", collective=True))

        diff = compare_author_lists(stored, registry)

        assert diff.clean
        assert "DiscovEHR" in diff.reasons[1]

    def test_a_collective_does_not_excuse_a_person_who_also_disagrees(self) -> None:
        """The boundary between "the consortia are the difference" and "not only".

        Dropping the collectives is licensed by what is left aligning exactly.
        Here the registry carries a consortium *and* names someone the
        bibliography does not, so the consortia are not the whole story and the
        entry has to keep being reported — this is the shape a real attribution
        error hides behind.
        """
        work = _load("audit_crossref_interleaved_collective.json")
        registry = _creators(work)
        stored = _people_only(registry)
        registry.insert(3, Name(literal="DiscovEHR", collective=True))
        registry.insert(4, Name(family="Nash", given="Harvey A."))

        diff = compare_author_lists(stored, registry)

        assert not diff.clean

    def test_dropping_the_collectives_aligns_the_two_lists_exactly(self) -> None:
        """The evidence that licenses the suppression, stated as a test.

        This is not the fix; it is the fact the fix may rely on. If Crossref's
        people ever stopped matching the stored byline for this record, the
        suppression above would no longer be justified and this test says so
        before anybody writes it.
        """
        work = _load("audit_crossref_interleaved_collective.json")
        registry = _creators(work)

        diff = compare_author_lists(_people_only(registry), _people_only(registry))

        assert diff.clean

    def test_a_substituted_person_is_still_reported(self) -> None:
        """The true-positive half: same shape, one real person swapped out.

        A byline carrying interleaved consortia is exactly the cover a genuine
        attribution error would hide behind. Whatever rule excuses the
        collectives must not excuse this: Harvey A. Risch replaced by someone
        who was never on the paper, in a list that is otherwise identical.
        """
        work = _load("audit_crossref_interleaved_collective.json")
        registry = _creators(work)
        stored = _people_only(registry)
        assert stored[5].family == "Risch"
        stored[5] = Name(family="Nash", given="Harvey A.")

        diff = compare_author_lists(stored, registry)

        assert diff.mismatches, "a substituted person must survive any collective rule"
        assert any("Nash" in stored_value for _, stored_value, _ in diff.mismatches)

    def test_a_dropped_person_is_still_reported(self) -> None:
        """The other true positive: an author missing from the *stored* list.

        A citation that has lost a real co-author looks, on a count alone,
        exactly like one that merely omits a consortium. The difference is that
        the dropped name is a person, and it has to keep being reported.
        """
        work = _load("audit_crossref_interleaved_collective.json")
        registry = _creators(work)
        stored = _people_only(registry)
        del stored[1]  # Chen Yuan, second author, simply gone

        diff = compare_author_lists(stored, registry)

        assert not diff.clean


# ---------------------------------------------------------------------------
# The suppression that is too wide: "compound surname shortened"
# ---------------------------------------------------------------------------


class TestCompoundSurnamesAreNotInterchangeable:
    """A shared final surname element is not evidence of the same person.

    ``names_agree``'s last escape accepts two surnames when *either* is compound
    and their **final tokens** are equal. It was written for a registry that
    kept only the tail of a hyphenated name — ``Chapelon`` for
    *Clavel-Chapelon* — and that case is real and must keep passing.

    The rule as written says something much larger, and this corpus is where it
    shows. Spanish and Portuguese bylines carry two surnames, the second
    inherited from the mother and shared by very large numbers of unrelated
    people. Counted over the 438-entry bibliography: 2917 distinct surnames, 452
    of them compound, forming **255 distinct pairs that this escape currently
    treats as the same person**. Among them, in the corpus itself:

    * ``Krebs-Smith, Susan M.`` (``reedy2014dietquality``, 10.3945/jn.113.189407)
      and ``Davey Smith, George`` (``carrerastorres2017mr``, 10.1093/jnci/djx012);
    * ``González-González, Rocío`` (``gil2019colorectal``, 10.1002/pds.4686) and
      ``Martínez-González, M`` (``willame2023access``,
      10.1016/j.vaccine.2022.11.031).

    Because a hole like this makes the tool report *less*, it produces a clean
    report and is invisible. The pairing is therefore inverted from the rest of
    this file: the true positive is the thing at risk, and the benign cases are
    what any narrowing must not cost.

    ``names_agree`` now requires the shorter token list to be a **suffix** of the
    longer one, which separates 123 of the 255 pairs and still admits every
    witnessed benign case: ``clavel chapelon``/``chapelon``, ``marianini rios``/
    ``cristina marianini rios`` (Crossref's own defect on
    10.1007/s10689-024-00397-w, which this corpus reports as REGISTRY-ARTIFACT),
    and ``and castello``/``castello``. The 132 that remain are one list being a
    genuine tail of the other, which is exactly the documented tolerance. All
    five tests below are live: the two benign ones guard the narrowing from
    being over-tightened, the three collisions guard it from being loosened
    back.
    """

    def test_a_registry_that_kept_only_the_final_element_is_still_accepted(self) -> None:
        """The benign case the escape exists for; narrowing must not cost it."""
        agreed, reason = names_agree(
            Name(family="Clavel-Chapelon", given="F"), Name(family="Chapelon", given="F")
        )

        assert agreed
        assert reason == "compound surname shortened"

    def test_a_registry_that_glued_the_forename_on_is_still_accepted(self) -> None:
        """Crossref's defect on 10.1007/s10689-024-00397-w (marianinirios2024risk).

        The deposit puts the whole name in ``family``:
        ``"Cristina-Marianini-Rios"``. The stored surname is a suffix of it, and
        the live corpus run reports the entry as REGISTRY-ARTIFACT.
        """
        agreed, _ = names_agree(
            Name(family="Marianini-Rios", given="Cristina"),
            Name(family="Cristina-Marianini-Rios", given="Cristina"),
        )

        assert agreed

    @pytest.mark.parametrize(
        ("stored", "registry"),
        [
            (Name(family="Krebs-Smith", given="Susan M."), Name(family="Davey Smith", given="S")),
            (
                Name(family="González-González", given="Rocío"),
                Name(family="Martínez-González", given="R"),
            ),
            (Name(family="Gómez-Rubio", given="Paulina"), Name(family="Fernández-Rubio", given="P")),
        ],
    )
    def test_two_people_sharing_a_final_surname_are_still_different_people(
        self, stored: Name, registry: Name
    ) -> None:
        """The true positive this suppression currently swallows.

        Every pair here is two people who both appear in the corpus, under
        different DOIs. Initials are made to agree because a registry giving a
        single initial is the normal case, and the escape does not consult them
        anyway — so agreement there must not be what rescues the comparison.
        """
        agreed, _ = names_agree(stored, registry)

        assert not agreed


# ---------------------------------------------------------------------------
# Crossref's container-title is an array, and for a chapter it holds two titles
# ---------------------------------------------------------------------------


class TestSeriesAndBookContainer:
    """A book chapter has two container titles and the entry may cite either.

    Crossref's record for 10.1007/978-1-59745-423-0_7 (Hainaut et al., stored as
    ``hainaut2011biobank``) carries ``"container-title": ["Methods in Molecular
    Biology", "Methods in Biobanking"]`` — the series and the volume. The entry
    is an ``@inbook`` whose ``booktitle`` is ``Methods in Biobanking``, which is
    Crossref's *second* element, exactly.

    ``crossref._first`` keeps only element 0, so the volume title is discarded
    before the comparison sees it. On the live run this was the corpus's one
    ``DISPUTED`` verdict, reported as Crossref and PubMed disagreeing, when in
    fact Crossref itself supplies the stored value.
    """

    def test_the_registry_record_really_does_carry_both_titles(self) -> None:
        work = _load("audit_crossref_series_and_book_container.json")
        assert work["container-title"] == [
            "Methods in Molecular Biology",
            "Methods in Biobanking",
        ]

    def test_a_chapter_citing_the_volume_title_is_not_a_difference(self) -> None:
        """Stated against ``compare``, not against one field of ``Record``.

        Which side is taught about the second element — the Crossref client, or
        the container check — is an open choice; that the entry stops being
        reported is not.

        Closed by ``Record.container_alternates`` plus ``compare``'s
        ``also_accepted``. This asserts no *finding* rather than the original's
        "no container issue at all": the check emits one ``info`` line naming
        the other title Crossref carries, which is the same treatment
        ``year/alternate-date`` gets and for the same reason — the reader is
        looking at a landing page headed *Methods in Molecular Biology* and
        needs to know the tool saw both titles rather than silently ignoring the
        difference. ``info`` is filtered out of the default terminal report and
        moves no verdict, so nothing is flagged.
        """
        result = _compare_chapter("Methods in Biobanking")

        container = [i for i in result.issues if i.field == "container"]
        assert [i for i in container if i.severity != "info"] == []
        assert not result.fails
        assert len(container) == 1
        assert "Methods in Biobanking" in container[0].note

    def test_the_series_title_is_still_accepted(self) -> None:
        """An entry citing the series rather than the volume is equally right.

        Whatever rule admits element 1 must not cost element 0, which is what a
        naive "use the last container-title" would do.
        """
        result = _compare_chapter("Methods in Molecular Biology")

        assert [i.field for i in result.issues if i.field == "container"] == []

    def test_a_container_matching_neither_title_is_still_reported(self) -> None:
        """The true-positive half.

        Accepting any element of ``container-title`` must not become accepting
        anything at all. *Methods in Enzymology* is a real, different book
        series; a chapter attributed to it is a citation a reader cannot follow,
        and it has to keep being reported.
        """
        result = _compare_chapter("Methods in Enzymology")

        assert [i.field for i in result.issues if i.field == "container"] != []
