"""Orchestration: adapters -> registries -> compare.

Everything here runs offline against stub registries, and an autouse fixture
fails any test that so much as opens a socket. That is not merely hygiene: the
worst failure this tool could have is turning a registry outage into a
bibliography full of fabrications, and a test that quietly reached the real
Crossref would pass for the wrong reason on the one day it mattered.

The first class is therefore the important one. ``Transient`` means *ignorance*
— the registry could not be asked — and must only ever produce ``UNCHECKED``.
An empty answer from a registry that did reply means *absence*, and that is a
finding. Any change that lets those two collapse into one is the bug these
tests exist to catch.
"""

from __future__ import annotations

import socket
import urllib.request
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

from bibaudit.adapters.bibtex import read_bibtex
from bibaudit.audit import AuditOptions, audit, resolve
from bibaudit.model import ANSWERED, UNREACHABLE, Name, Record, Reference
from bibaudit.normalize import normalize_doi
from bibaudit.registries.http import Cache, Client, Transient
from bibaudit.registries.retractions import RetractionNotice, RetractionStatus
from bibaudit.report import Summary
from bibaudit.suppress import Suppression, Suppressions

#: The package re-exports the ``audit`` *function* as ``bibaudit.audit``, so the
#: attribute of that name is the function, not the module. The module has to be
#: fetched by name to patch the registry classes it looks up at call time.
audit_module = import_module("bibaudit.audit")

DOI = "10.1093/ije/dyx269"
TITLE = (
    "Risk of pancreatic cancer associated with family history of cancer "
    "and other medical conditions by accounting for smoking among relatives"
)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if any test in this module opens a network connection.

    ``audit()`` builds a real :class:`~bibaudit.registries.http.Client` unless a
    test replaces it, so a stub that is installed in the wrong place would
    otherwise silently fall through to the live registries and turn this file
    into an integration suite that passes or fails with the weather.
    """

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "a test opened a network connection; every verdict must be "
            "derivable from a stubbed or cached registry response"
        )

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)


class _ForbiddenClient:
    """Stands in for the HTTP client and refuses to be used at all.

    ``audit.py`` builds the client and hands it to the registry modules; it must
    never speak to it itself, and the comparison it runs afterwards must not
    reach for it either — comparison performs no I/O, which is what makes a
    verdict reproducible from a cached response. Any attribute access beyond
    construction is therefore a layering violation and raises rather than
    returning something a caller could quietly use.
    """

    def __init__(self, cache: object = None, **kwargs: object) -> None:
        self.cache = cache
        self.kwargs = kwargs

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(
            f"audit.py used the HTTP client directly (.{name}); only the "
            "registry modules may perform I/O"
        )


class _StubRegistry:
    """Stands in for Crossref, DataCite or PubMed's DOI-lookup role.

    Holds the records it will admit to knowing about, records every call so a
    test can assert *which* registry was asked *what*, and can be told to raise
    :class:`Transient` instead — the outage case that must never be reported as
    a missing work. The identifier-less *search* role is a separate seam, since
    ``audit.py`` asks ``registries.search`` — see :class:`_StubSearch`.
    """

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
        self.constructions = 0
        self.client: object = None
        self.by_dois_calls: list[list[str]] = []

    def make(self, client: object) -> _StubRegistry:
        """Constructor stand-in, so a test can see whether it was built at all."""
        self.constructions += 1
        self.client = client
        return self

    def by_dois(self, dois: Sequence[str]) -> dict[str, Record]:
        self.by_dois_calls.append(list(dois))
        if self.transient:
            raise Transient(f"{self.name}: simulated outage")
        # Answer only for what was asked, exactly as the real clients do: a stub
        # that volunteered records nobody requested would hide a routing bug.
        return {doi: self.records[doi] for doi in dois if doi in self.records}


class _StubSearch:
    """Stands in for :class:`~bibaudit.registries.search.Search`.

    The real ``Search`` wraps three HTTP sources (Crossref, Europe PMC,
    OpenAlex) behind one ``candidates()`` call; ``audit._audit_unidentified``
    only ever sees that one seam, so that is all this stub needs to provide.
    ``sources`` mirrors the real class's default of consulting all three, so a
    test that does not care about the exact roster still gets a realistic
    ``Result.consulted``.
    """

    def __init__(
        self,
        *,
        candidates: Sequence[Record] = (),
        transient: bool = False,
        sources: Sequence[str] = ("crossref", "europepmc", "openalex"),
    ) -> None:
        self.candidate_results = list(candidates)
        self.transient = transient
        self.sources = tuple(sources)
        self.constructions = 0
        self.client: object = None
        self.candidate_calls: list[Reference] = []

    def make(self, client: object, **kwargs: object) -> _StubSearch:
        """Accepts and ignores ``use_europepmc``/``use_openalex``.

        ``_build`` passes them through to the real ``Search`` so
        ``--no-europepmc``/``--no-openalex`` reach it; this stub does not
        model the two-source gating itself (``sources`` above already lets a
        test declare whatever roster it needs), it only has to not raise
        ``TypeError`` on the call it is standing in for.
        """
        self.constructions += 1
        self.client = client
        return self

    def candidates(self, ref: Reference, rows: int = 5) -> list[Record]:
        self.candidate_calls.append(ref)
        if self.transient:
            raise Transient("search: simulated outage")
        return list(self.candidate_results)


class _StubOpenLibrary:
    """Stands in for :class:`~bibaudit.registries.openlibrary.OpenLibrary`.

    Covers both roles ``audit.py`` asks of it: ``by_isbns`` (the ISBN-keyed
    counterpart of ``Crossref.by_dois`` in ``resolve``) and ``search`` (the
    free-text candidate source ``_audit_unidentified`` adds for a book or
    chapter carrying no identifier at all, alongside ``registries.search``).
    Left unstubbed, a book-kind test would otherwise construct a *real*
    ``OpenLibrary`` around ``_ForbiddenClient`` and fail the moment it tried
    to use it — this is a registry module, so using the client is exactly its
    job, just never on a test that never asked to exercise it.
    """

    def __init__(
        self,
        *,
        records: dict[str, Record] | None = None,
        candidates: Sequence[Record] = (),
        transient: bool = False,
        search_transient: bool = False,
    ) -> None:
        self.records = dict(records or {})
        self.candidate_results = list(candidates)
        self.transient = transient
        self.search_transient = search_transient
        self.constructions = 0
        self.client: object = None
        self.by_isbns_calls: list[list[str]] = []
        self.search_calls: list[Reference] = []

    def make(self, client: object) -> _StubOpenLibrary:
        self.constructions += 1
        self.client = client
        return self

    def by_isbns(self, isbns: Sequence[str]) -> dict[str, Record]:
        self.by_isbns_calls.append(list(isbns))
        if self.transient:
            raise Transient("openlibrary: simulated outage")
        return {isbn: self.records[isbn] for isbn in isbns if isbn in self.records}

    def search(self, ref: Reference, rows: int = 5) -> list[Record]:
        self.search_calls.append(ref)
        if self.search_transient:
            raise Transient("openlibrary: simulated outage")
        return list(self.candidate_results)


class _StubRetractions:
    """Stands in for :class:`~bibaudit.registries.retractions.Retractions`.

    ``audit.py`` reaches the real class through exactly one method,
    ``status_for`` — never its Retraction Watch cache or its internal
    ``PubMed`` instance, both private to that module — so that is all this
    stub provides. Answers nothing by default: a test that does not care
    about retraction status must not have to configure one just to keep
    ``resolve``/``_audit_unidentified`` from reaching a real client.
    """

    def __init__(
        self,
        *,
        notices: dict[str, RetractionNotice] | None = None,
        transient: bool = False,
        rw_unreachable: bool = False,
    ) -> None:
        self.notices = dict(notices or {})
        self.transient = transient
        #: The Retraction Watch export failing, which the real class reports
        #: through its return value rather than by raising -- ``transient``
        #: above is the *other* outage, PubMed's, which does raise.
        self.rw_unreachable = rw_unreachable
        self.constructions = 0
        self.client: object = None
        self.status_for_calls: list[list[str]] = []

    def make(self, client: object) -> _StubRetractions:
        self.constructions += 1
        self.client = client
        return self

    def status_for(self, dois: Sequence[str]) -> RetractionStatus:
        self.status_for_calls.append(list(dois))
        if self.transient:
            raise Transient("retractions: simulated outage")
        wanted = {normalize_doi(doi) for doi in dois}
        return RetractionStatus(
            notices={doi: notice for doi, notice in self.notices.items() if doi in wanted},
            unreachable=frozenset({"retraction-watch"}) if self.rw_unreachable else frozenset(),
        )


@dataclass(slots=True)
class _Stubs:
    crossref: _StubRegistry
    datacite: _StubRegistry
    pubmed: _StubRegistry
    search: _StubSearch
    openlibrary: _StubOpenLibrary
    retractions: _StubRetractions


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    crossref: _StubRegistry | None = None,
    datacite: _StubRegistry | None = None,
    pubmed: _StubRegistry | None = None,
    search: _StubSearch | None = None,
    openlibrary: _StubOpenLibrary | None = None,
    retractions: _StubRetractions | None = None,
) -> _Stubs:
    """Replace the registry clients audit.py builds, and forbid the HTTP client."""
    stubs = _Stubs(
        crossref=crossref if crossref is not None else _StubRegistry("crossref"),
        datacite=datacite if datacite is not None else _StubRegistry("datacite"),
        pubmed=pubmed if pubmed is not None else _StubRegistry("pubmed"),
        search=search if search is not None else _StubSearch(),
        openlibrary=openlibrary if openlibrary is not None else _StubOpenLibrary(),
        retractions=retractions if retractions is not None else _StubRetractions(),
    )
    monkeypatch.setattr(audit_module, "Client", _ForbiddenClient)
    monkeypatch.setattr(audit_module, "Crossref", stubs.crossref.make)
    monkeypatch.setattr(audit_module, "DataCite", stubs.datacite.make)
    monkeypatch.setattr(audit_module, "PubMed", stubs.pubmed.make)
    monkeypatch.setattr(audit_module, "Search", stubs.search.make)
    monkeypatch.setattr(audit_module, "OpenLibrary", stubs.openlibrary.make)
    monkeypatch.setattr(audit_module, "Retractions", stubs.retractions.make)
    return stubs


def _options(tmp_path: Path, **overrides: Any) -> AuditOptions:
    """Audit options with the cache pointed somewhere disposable."""
    return AuditOptions(cache_dir=tmp_path / "cache", **overrides)


def make_ref(**overrides: object) -> Reference:
    """A correct reference, which tests then damage one field at a time."""
    base: dict[str, object] = {
        "key": "molinamontes2018family",
        "locator": "references.bib:1",
        "kind": "article",
        "doi": DOI,
        "title": TITLE,
        "authors": [
            Name(family="Molina-Montes", given="E"),
            Name(family="Gomez-Rubio", given="P"),
        ],
        "year": 2018,
        "container": "International Journal of Epidemiology",
        "volume": "47",
        "issue": "2",
        "pages": "473-483",
    }
    base.update(overrides)
    return Reference(**base)  # type: ignore[arg-type]


def make_record(**overrides: object) -> Record:
    """The Crossref record that agrees with :func:`make_ref` in every field."""
    base: dict[str, object] = {
        "source": "crossref",
        "doi": DOI,
        "title": TITLE,
        "authors": [
            Name(family="Molina-Montes", given="E"),
            Name(family="Gomez-Rubio", given="P"),
        ],
        "years": {"print": 2018},
        "container": "International Journal of Epidemiology",
        "volume": "47",
        "issue": "2",
        "pages": "473-483",
        "kind": "journal-article",
    }
    base.update(overrides)
    return Record(**base)  # type: ignore[arg-type]


def make_pubmed_record(**overrides: object) -> Record:
    """The same work as NLM curates it: no publisher, no type string."""
    base: dict[str, object] = {
        "source": "pubmed",
        "doi": DOI,
        "title": TITLE,
        "authors": [
            Name(family="Molina-Montes", given="E"),
            Name(family="Gomez-Rubio", given="P"),
        ],
        "years": {"issued": 2018},
        "container": "International Journal of Epidemiology",
        "volume": "47",
        "issue": "2",
        "pages": "473-483",
    }
    base.update(overrides)
    return Record(**base)  # type: ignore[arg-type]


class TestTheNoNetworkGuardItself:
    """The guard every other test in this file leans on has to actually bite.

    Nothing else here would fail if ``_no_network`` stopped intercepting — a new
    transport, an ``urlopen`` bound at import time, an autouse decorator lost in
    a refactor. The file would keep passing while quietly turning into an
    integration suite against the live registries, which is the one way a
    verdict-path test can be green for the wrong reason.
    """

    def test_a_real_client_cannot_reach_a_registry(self, tmp_path: Path) -> None:
        client = Client(Cache(tmp_path / "cache"))
        with pytest.raises(AssertionError, match="opened a network connection"):
            client.get_json(f"https://api.crossref.org/works/{DOI}")

    def test_a_raw_socket_cannot_be_opened(self) -> None:
        with pytest.raises(AssertionError, match="opened a network connection"):
            socket.create_connection(("api.crossref.org", 443), timeout=0.1)


class TestOutageIsNeverAFinding:
    """A registry that could not be asked must never accuse a bibliography."""

    def test_a_reference_is_unchecked_when_no_registry_could_be_reached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One correct citation, three registries down: nothing may be alleged.

        The reference is impeccable. Every registry raised ``Transient``. The
        only honest report is that it was not checked.
        """
        _install(
            monkeypatch,
            crossref=_StubRegistry("crossref", transient=True),
            datacite=_StubRegistry("datacite", transient=True),
            pubmed=_StubRegistry("pubmed", transient=True),
        )
        result = audit([make_ref()], _options(tmp_path))[0]

        assert result.verdict == "UNCHECKED"
        assert not result.fails

    def test_a_whole_run_against_dead_registries_fails_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The failure this module exists to prevent: an outage read as fraud.

        Every shape of entry goes down a different branch of ``audit()`` — a
        Crossref DOI, a DataCite DOI, and one with no identifier at all — and
        none of them may end up with a failing verdict when the outage is total.
        """
        _install(
            monkeypatch,
            crossref=_StubRegistry("crossref", transient=True),
            datacite=_StubRegistry("datacite", transient=True),
            pubmed=_StubRegistry("pubmed", transient=True),
            search=_StubSearch(transient=True),
        )
        refs = [
            make_ref(key="a", doi=DOI),
            make_ref(key="b", doi="10.5281/zenodo.1234567", kind="dataset"),
            make_ref(key="c", doi=None),
            make_ref(key="d", doi="10.1016/S0140-6736(03)14065-2"),
        ]

        results = audit(refs, _options(tmp_path))

        assert [r.verdict for r in results] == ["UNCHECKED"] * 4
        assert Summary(results).exit_code() == 0
        assert not any(r.fails for r in results)

    def test_a_search_outage_does_not_become_unconfirmed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An entry with no DOI is confirmed by search; losing search is ignorance.

        ``UNCONFIRMED`` is a failing verdict and reads as "this may be invented".
        Reporting it because Crossref's search endpoint timed out would blame the
        bibliography for the network.
        """
        _install(monkeypatch, search=_StubSearch(transient=True))
        result = audit([make_ref(doi=None)], _options(tmp_path))[0]

        assert result.verdict == "UNCHECKED"
        assert not result.fails

    def test_a_pubmed_outage_does_not_disturb_a_crossref_verdict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Losing the corroborating registry degrades the check, not the verdict.

        It must still be visible in ``consulted`` that PubMed never answered, or
        the report claims a second opinion it does not have.
        """
        stubs = _install(
            monkeypatch,
            crossref=_StubRegistry("crossref", records={DOI: make_record()}),
            pubmed=_StubRegistry("pubmed", transient=True),
        )
        result = audit([make_ref()], _options(tmp_path))[0]

        assert result.verdict == "OK"
        assert result.consulted["pubmed"] == UNREACHABLE
        assert result.consulted["crossref"] == ANSWERED
        assert stubs.pubmed.by_dois_calls == [[DOI]]

    def test_a_retraction_watch_outage_is_stated_not_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end, the defect this whole path was widened to fix.

        The scenario is ordinary, not exotic: ``--offline`` (or any run whose
        cached Retraction Watch export has aged past its seven-day TTL) fails to
        load the index, every field agrees with Crossref, and the run reports
        ``OK`` and exits 0. It must not do so silently: ``consulted`` would
        otherwise assert ``answered`` for the one source that exists solely to
        carry retraction status, because ``_asked_registries`` adds
        ``"retraction-watch"`` to ``asked`` whenever ``--no-retraction-check``
        is not passed.

        The verdict and the exit code deliberately do not move. An outage is not
        a defect in anybody's bibliography. What must move is what the run says
        out loud.
        """
        _install(
            monkeypatch,
            crossref=_StubRegistry("crossref", records={DOI: make_record()}),
            retractions=_StubRetractions(rw_unreachable=True),
        )
        result = audit([make_ref()], _options(tmp_path))[0]

        assert result.consulted["retraction-watch"] == UNREACHABLE
        status = next(i for i in result.issues if i.field == "status")
        assert status.kind == "retraction-unverified"
        assert "retraction-watch" in status.source
        assert result.verdict == "OK"
        assert not result.fails
        assert Summary([result]).exit_code() == 0

    def test_a_datacite_outage_does_not_turn_its_dois_into_bad_ids(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Crossref legitimately 404s a Zenodo DOI; only DataCite can clear it.

        With DataCite unreachable, nobody who could know has answered, so the
        entry is unchecked — not an unresolvable identifier.
        """
        doi = "10.5281/zenodo.1234567"
        _install(
            monkeypatch,
            crossref=_StubRegistry("crossref"),
            datacite=_StubRegistry("datacite", transient=True),
        )
        result = audit([make_ref(doi=doi, kind="dataset")], _options(tmp_path))[0]

        assert result.verdict == "UNCHECKED"
        assert not result.fails

    def test_a_crossref_outage_does_not_turn_other_registries_dois_into_bad_ids(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Asserted on the verdict, not on the request pattern.

        Today ``resolve`` skips DataCite entirely when Crossref is unreachable,
        because "the DOIs Crossref did not answer for" is unknowable then. A
        future change may well ask DataCite anyway; what may never change is that
        an entry no reachable registry could speak to is UNCHECKED.
        """
        doi = "10.5281/zenodo.7654321"
        _install(
            monkeypatch,
            crossref=_StubRegistry("crossref", transient=True),
            datacite=_StubRegistry("datacite"),
        )
        result = audit([make_ref(doi=doi, kind="dataset")], _options(tmp_path))[0]

        assert result.verdict == "UNCHECKED"
        assert not result.fails


class TestAbsenceIsDistinguishableFromIgnorance:
    def test_a_404_everywhere_is_a_finding_an_outage_everywhere_is_not(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same reference, the same empty hands, two opposite conclusions.

        Every registry replying "I do not have this" is evidence about the
        citation. Every registry failing to reply is evidence about the network.
        The result must let a reader tell which happened without re-running.
        """
        doi = "10.9999/invented.doi"
        ref = make_ref(doi=doi)

        stubs = _install(monkeypatch, crossref=_StubRegistry("crossref"))
        answered = audit([ref], _options(tmp_path))[0]

        _install(
            monkeypatch,
            crossref=_StubRegistry("crossref", transient=True),
            datacite=_StubRegistry("datacite", transient=True),
            pubmed=_StubRegistry("pubmed", transient=True),
        )
        unreachable = audit([ref], _options(tmp_path / "second"))[0]

        # "Everywhere" has to be earned: the finding is only honest if every
        # registry that could have carried this DOI was actually asked. Crossref
        # alone answering "not mine" is not evidence about a Zenodo or figshare
        # DOI, and reporting BAD-ID on that basis is the false alarm that stops
        # the report being read.
        assert stubs.crossref.by_dois_calls == [[doi]]
        assert stubs.datacite.by_dois_calls == [[doi]]
        assert stubs.pubmed.by_dois_calls == [[doi]]

        assert answered.verdict == "BAD-ID"
        assert answered.fails
        assert [i.kind for i in answered.issues] == ["unresolved"]
        assert answered.consulted["crossref"] == ANSWERED
        assert answered.consulted["pubmed"] == ANSWERED
        assert Summary([answered]).exit_code() == 1

        assert unreachable.verdict == "UNCHECKED"
        assert not unreachable.fails
        assert [i.kind for i in unreachable.issues] == ["unreachable"]
        assert unreachable.consulted["crossref"] == UNREACHABLE
        assert unreachable.consulted["pubmed"] == UNREACHABLE
        # The two runs saw identical evidence — an empty answer — and CI must
        # come to opposite conclusions about them.
        assert Summary([unreachable]).exit_code() == 0

    def test_resolve_keeps_not_found_apart_from_could_not_ask(self) -> None:
        """``resolve`` returns both halves; collapsing them is the whole bug."""
        registries = audit_module._Registries(
            crossref=_StubRegistry("crossref", transient=True),  # type: ignore[arg-type]
            datacite=_StubRegistry("datacite"),  # type: ignore[arg-type]
            pubmed=_StubRegistry("pubmed"),  # type: ignore[arg-type]
            search=_StubSearch(),  # type: ignore[arg-type]
        )
        records, unreachable = resolve([make_ref()], registries)

        assert records == {DOI: {}}  # nobody had it
        assert unreachable == {"crossref"}  # and one of them was never asked

    def test_a_retraction_watch_outage_reaches_the_unreachable_set(self) -> None:
        """The Retraction Watch leg fails by *returning*, not by raising.

        ``Retractions.status_for`` absorbs its own bulk-export outage so that
        PubMed's independent answer is not lost with it, and reports the failed
        source in the returned ``RetractionStatus`` instead. If ``resolve`` drops
        that set, nothing downstream can tell the difference between "Retraction
        Watch had nothing on this DOI" and "Retraction Watch was never reached",
        and the second reads as the first — a clean bill of health from
        ignorance.
        """
        registries = audit_module._Registries(
            crossref=_StubRegistry("crossref"),  # type: ignore[arg-type]
            datacite=_StubRegistry("datacite"),  # type: ignore[arg-type]
            pubmed=_StubRegistry("pubmed"),  # type: ignore[arg-type]
            search=_StubSearch(),  # type: ignore[arg-type]
            retractions=_StubRetractions(rw_unreachable=True),  # type: ignore[arg-type]
        )
        _, unreachable = resolve([make_ref()], registries)

        assert "retraction-watch" in unreachable
        # PubMed answered; only the leg that failed may be named.
        assert "pubmed" not in unreachable

    def test_one_entrys_outage_does_not_mask_another_entrys_finding(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: an unrelated DataCite timeout hid an unconfirmable entry.

        Crossref's search is the only thing ever asked about an entry carrying
        no identifier, and here it answered — with nothing that confirms the
        entry. That is a finding: UNCONFIRMED is the shape a fabricated
        reference takes. Handing the *run-wide* outage set to the comparison
        made it take its "nothing answered" branch instead, so the same entry
        came back UNCHECKED — silently unreported — as soon as some other
        entry's Zenodo DOI made DataCite time out. Whether a fabrication is
        reported must not depend on its neighbours' network luck.
        """
        unconfirmable = make_ref(
            key="invented", doi=None, title="A study that does not exist"
        )

        _install(monkeypatch, crossref=_StubRegistry("crossref"))
        alone = audit([unconfirmable], _options(tmp_path))[0]

        _install(
            monkeypatch,
            crossref=_StubRegistry("crossref"),
            datacite=_StubRegistry("datacite", transient=True),
        )
        alongside = audit(
            [
                make_ref(key="zenodo", doi="10.5281/zenodo.1234567", kind="dataset"),
                unconfirmable,
            ],
            _options(tmp_path / "second"),
        )

        assert alone.verdict == "UNCONFIRMED"
        # The neighbour really is unchecked: only DataCite could have carried a
        # Zenodo DOI, and DataCite is the registry that went down.
        assert alongside[0].verdict == "UNCHECKED"
        # This entry's own evidence did not change between the two runs.
        assert alongside[1].verdict == "UNCONFIRMED"


class TestRegistryRouting:
    def test_datacite_is_asked_only_about_dois_crossref_did_not_answer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DataCite is the fallback, not a second opinion.

        Asking it about a DOI Crossref already answered would let its record
        overwrite Crossref's, and the two disagree systematically about types.
        """
        other = "10.5281/zenodo.1234567"
        stubs = _install(
            monkeypatch,
            crossref=_StubRegistry("crossref", records={DOI: make_record()}),
            datacite=_StubRegistry("datacite"),
        )
        audit([make_ref(), make_ref(key="b", doi=other, kind="dataset")], _options(tmp_path))

        assert stubs.crossref.by_dois_calls == [sorted([DOI, other])]
        assert stubs.datacite.by_dois_calls == [[other]]

    def test_a_datacite_only_doi_still_gets_a_real_verdict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Zenodo DOI is absent from Crossref and present in DataCite.

        Without the fallback the entry would be reported as BAD-ID — a correct
        citation accused of pointing nowhere.
        """
        doi = "10.5281/zenodo.1234567"
        record = Record(
            source="datacite",
            doi=doi,
            title="A tidy dataset of shift-work exposures",
            authors=[Name(family="Papantoniou", given="K")],
            years={"issued": 2021},
            volume="3",
            kind="Dataset",
        )
        _install(monkeypatch, crossref=_StubRegistry("crossref"),
                 datacite=_StubRegistry("datacite", records={doi: record}))
        ref = make_ref(
            key="zenodo", doi=doi, kind="dataset",
            title="A tidy dataset of shift-work exposures",
            authors=[Name(family="Papantoniou", given="K")],
            year=2021, container=None, volume="3", issue=None, pages=None,
        )

        result = audit([ref], _options(tmp_path))[0]

        assert result.verdict == "OK"

    def test_the_datacite_record_is_actually_compared_not_merely_counted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wrong field must still be caught when the evidence came from DataCite.

        "Something answered, so the entry is fine" would pass the test above; a
        mismatch attributed to DataCite proves its fields reached the comparison.
        """
        doi = "10.5281/zenodo.1234567"
        record = Record(
            source="datacite", doi=doi, title="A tidy dataset of shift-work exposures",
            authors=[Name(family="Papantoniou", given="K")], years={"issued": 2021},
            volume="3", kind="Dataset",
        )
        _install(monkeypatch, crossref=_StubRegistry("crossref"),
                 datacite=_StubRegistry("datacite", records={doi: record}))
        ref = make_ref(
            key="zenodo", doi=doi, kind="dataset",
            title="A tidy dataset of shift-work exposures",
            authors=[Name(family="Papantoniou", given="K")],
            year=2021, container=None, volume="4", issue=None, pages=None,
        )

        result = audit([ref], _options(tmp_path))[0]

        assert result.verdict == "FIELD-MISMATCH"
        assert [(i.field, i.source) for i in result.issues] == [("volume", "datacite")]

    def test_pubmed_is_asked_about_every_doi_not_only_the_leftovers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PubMed is consulted for *independent* corroboration.

        Demoting it to a fallback for the DOIs Crossref missed would quietly
        delete the only separately-curated evidence the tool has, and the checks
        that depend on it (DISPUTED) would stop firing without any test breaking.
        """
        other = "10.5281/zenodo.1234567"
        stubs = _install(
            monkeypatch,
            crossref=_StubRegistry("crossref", records={DOI: make_record()}),
        )
        audit([make_ref(), make_ref(key="b", doi=other, kind="dataset")], _options(tmp_path))

        assert stubs.pubmed.by_dois_calls == [sorted([DOI, other])]

    def test_pubmed_corroboration_reaches_the_comparison(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both records must arrive under their own registry names.

        If the PubMed record were passed under the wrong key it would either be
        ignored or promoted to primary, and a genuine registry disagreement — the
        DISPUTED verdict, which exists so a human decides — would never surface.
        """
        _install(
            monkeypatch,
            crossref=_StubRegistry("crossref", records={DOI: make_record(volume="47")}),
            pubmed=_StubRegistry("pubmed", records={DOI: make_pubmed_record(volume="48")}),
        )
        result = audit([make_ref(volume="48")], _options(tmp_path))[0]

        assert result.verdict == "DISPUTED"
        assert not result.fails
        assert any(i.field == "volume" and i.source == "both" for i in result.issues)

    def test_pubmed_is_never_built_when_corroboration_is_switched_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--no-corroborate`` halves request volume; it must actually do so."""
        stubs = _install(
            monkeypatch, crossref=_StubRegistry("crossref", records={DOI: make_record()})
        )
        result = audit([make_ref()], _options(tmp_path, corroborate=False))[0]

        assert stubs.pubmed.constructions == 0
        assert stubs.pubmed.by_dois_calls == []
        assert result.verdict == "OK"

    def test_one_registry_answering_is_enough_for_a_real_verdict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Crossref down, PubMed up: the check degrades, it does not abstain.

        Reporting UNCHECKED here would waste evidence the tool actually holds.
        """
        _install(
            monkeypatch,
            crossref=_StubRegistry("crossref", transient=True),
            pubmed=_StubRegistry("pubmed", records={DOI: make_pubmed_record()}),
        )
        results = audit(
            [make_ref(publisher=None), make_ref(key="b", publisher=None, volume="48")],
            _options(tmp_path),
        )

        assert results[0].verdict == "OK"
        assert results[1].verdict == "FIELD-MISMATCH"
        # The volume disagreement is the only *finding*. The second issue is the
        # ``status/retraction-unverified`` note: Crossref carries the Retraction
        # Watch linkage and Crossref is the registry that timed out, so nothing
        # that answered could have cleared this work of a retraction. It is
        # ``info``, so it neither moves the verdict nor fails the build — see
        # ``compare._status_issues`` — but it must be stated, because a run whose
        # only retraction source was down reads cleaner than the evidence allows.
        assert [(i.field, i.source) for i in results[1].issues] == [
            ("volume", "pubmed"),
            ("status", "crossref"),
        ]
        assert [(i.field, i.source) for i in results[1].errors] == [("volume", "pubmed")]

    def test_a_doi_only_pubmed_carries_still_gets_a_real_verdict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Crossref and DataCite both replied "not mine"; PubMed holds the paper.

        Both DOI registries answering empty is exactly the BAD-ID shape, and
        reporting it as one would accuse a live citation of pointing nowhere
        while the separately-curated registry is holding the record that clears
        it. The second entry disagrees on a field, which proves PubMed's values
        reached the comparison rather than merely satisfying a "something
        answered" check.
        """
        _install(
            monkeypatch,
            crossref=_StubRegistry("crossref"),
            datacite=_StubRegistry("datacite"),
            pubmed=_StubRegistry("pubmed", records={DOI: make_pubmed_record()}),
        )

        results = audit([make_ref(), make_ref(key="b", volume="48")], _options(tmp_path))

        assert results[0].verdict == "OK"
        assert results[1].verdict == "FIELD-MISMATCH"
        assert [(i.field, i.source) for i in results[1].issues] == [("volume", "pubmed")]

    def test_dois_are_normalised_before_lookup_and_on_the_way_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same work stored two ways must match the one record fetched for it.

        A DOI arrives as ``https://doi.org/10.1093/IJE/DYX269`` from a Zotero URL
        field and as ``10.1093/ije/dyx269`` from a .bib. Looking the record back
        up under the raw string would report the first one as BAD-ID: a
        fabrication warning on a perfectly correct citation.
        """
        stubs = _install(
            monkeypatch, crossref=_StubRegistry("crossref", records={DOI: make_record()})
        )
        refs = [
            make_ref(key="from_zotero", doi="https://doi.org/10.1093/IJE/DYX269"),
            make_ref(key="from_bibtex", doi=DOI),
        ]

        results = audit(refs, _options(tmp_path))

        assert [r.verdict for r in results] == ["OK", "OK"]
        assert stubs.crossref.by_dois_calls == [[DOI]]  # asked once, in folded form

    def test_a_doi_containing_parentheses_survives_routing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``10.1016/S0140-6736(03)14065-2`` is a real Lancet DOI.

        Any handling that truncates it at the bracket reports a live DOI as
        unresolvable; that mistake produced twelve false "does not exist" hits on
        a clean bibliography once already.
        """
        doi = "10.1016/s0140-6736(03)14065-2"
        stubs = _install(
            monkeypatch,
            crossref=_StubRegistry("crossref", records={doi: make_record(doi=doi)}),
        )
        result = audit([make_ref(doi="10.1016/S0140-6736(03)14065-2")], _options(tmp_path))[0]

        assert stubs.crossref.by_dois_calls == [[doi]]
        assert result.verdict == "OK"


class TestRetractionCorroboration:
    """Independent retraction status, layered on top of a registry's own flag."""

    def test_retraction_watch_alone_fails_a_doi_crossref_never_flagged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A retraction only Retraction Watch's own export carries still fails.

        Crossref's record here carries no ``updated-by`` linkage of its own —
        the publisher never deposited one, gap (a) in
        ``registries/retractions.py``'s module docstring — and the DOI is
        otherwise correct, so nothing but the independent retraction check
        could catch this.
        """
        notice = RetractionNotice(
            doi=DOI, kind="retraction", source="retraction-watch",
            notice_doi=None, date="2020-01-01",
        )
        _install(
            monkeypatch,
            crossref=_StubRegistry("crossref", records={DOI: make_record()}),
            retractions=_StubRetractions(notices={DOI: notice}),
        )

        result = audit([make_ref()], _options(tmp_path))[0]

        assert result.verdict == "RETRACTED"
        assert result.fails
        retracted_issue = next(i for i in result.issues if i.kind == "retracted")
        assert retracted_issue.source == "retraction-watch"

    def test_a_retraction_notice_never_resolves_an_otherwise_unresolved_doi(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A DOI nothing bibliographic confirms stays ``BAD-ID``, never a pass.

        Retraction Watch's export is keyed on whatever ``OriginalPaperDOI`` it
        scraped, independent of whether Crossref, DataCite or PubMed hold that
        DOI today. A stale or mistyped row must not manufacture a "resolved"
        entry out of a status-only notice — see
        ``_merge_retraction_notices``'s own docstring.
        """
        doi = "10.9999/invented.doi"
        notice = RetractionNotice(
            doi=doi, kind="retraction", source="retraction-watch",
            notice_doi=None, date=None,
        )
        _install(
            monkeypatch,
            crossref=_StubRegistry("crossref"),
            retractions=_StubRetractions(notices={doi: notice}),
        )

        result = audit([make_ref(doi=doi)], _options(tmp_path))[0]

        assert result.verdict == "BAD-ID"
        assert result.fails

    def test_no_retraction_check_disables_independent_corroboration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--no-retraction-check`` must actually stop ``Retractions`` being built."""
        notice = RetractionNotice(
            doi=DOI, kind="retraction", source="retraction-watch",
            notice_doi=None, date=None,
        )
        stubs = _install(
            monkeypatch,
            crossref=_StubRegistry("crossref", records={DOI: make_record()}),
            retractions=_StubRetractions(notices={DOI: notice}),
        )

        result = audit([make_ref()], _options(tmp_path, retraction_check=False))[0]

        assert stubs.retractions.constructions == 0
        assert stubs.retractions.status_for_calls == []
        assert result.verdict == "OK"

    def test_retraction_status_reuses_the_pubmed_corroboration_batch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same, identically-ordered DOI list reaches both PubMed calls.

        See the module docstring: handing ``Retractions.status_for`` a
        *different* batch — even a subset — would shift its internal
        ``PubMed.by_dois``'s own chunking and turn one logical retraction
        check into a second, real round trip against a rate-limited registry
        rather than a cache hit on the corroboration fetch's own responses.
        """
        other = "10.5281/zenodo.1234567"
        stubs = _install(
            monkeypatch,
            crossref=_StubRegistry("crossref", records={DOI: make_record()}),
        )
        audit(
            [make_ref(), make_ref(key="b", doi=other, kind="dataset")],
            _options(tmp_path),
        )

        assert stubs.retractions.status_for_calls == stubs.pubmed.by_dois_calls
        assert stubs.retractions.status_for_calls == [sorted([DOI, other])]

    def test_a_search_confirmed_entry_is_also_checked_for_retraction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A DOI found only by search still gets the same retraction check.

        Also proves the verdict re-derivation after inserting the
        ``doi/proposed`` issue does not silently drop a retraction found a
        moment earlier — see ``_audit_unidentified``'s own note on why
        ``retracted`` must be passed explicitly there.
        """
        notice = RetractionNotice(
            doi=DOI, kind="retraction", source="retraction-watch",
            notice_doi=None, date=None,
        )
        _install(
            monkeypatch,
            search=_StubSearch(candidates=[make_record()]),
            retractions=_StubRetractions(notices={DOI: notice}),
        )

        result = audit([make_ref(doi=None)], _options(tmp_path))[0]

        assert result.verdict == "RETRACTED"
        assert result.fails

    def test_pubmeds_own_witness_is_named_once_not_twice(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PubMed's ``ECI``-derived concern folds into its existing record.

        A second, separately-named ``"pubmed"`` entry would let
        ``compare._status_issues`` attribute one witness's finding to two
        named sources instead of one.
        """
        notice = RetractionNotice(
            doi=DOI, kind="expression-of-concern", source="pubmed",
            notice_doi=None, date=None,
        )
        _install(
            monkeypatch,
            crossref=_StubRegistry("crossref", records={DOI: make_record()}),
            pubmed=_StubRegistry("pubmed", records={DOI: make_pubmed_record()}),
            retractions=_StubRetractions(notices={DOI: notice}),
        )

        result = audit([make_ref()], _options(tmp_path))[0]

        concern = next(i for i in result.issues if i.kind == "expression-of-concern")
        assert concern.source == "pubmed"


class TestReferencesWithoutAnIdentifier:
    def test_an_entry_with_no_identifier_is_looked_up_by_search(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = make_ref(doi=None)
        stubs = _install(monkeypatch, search=_StubSearch(candidates=[make_record()]))

        result = audit([ref], _options(tmp_path))[0]

        assert stubs.search.candidate_calls == [ref]
        assert stubs.crossref.by_dois_calls == []  # nothing to look up by DOI
        # The work was identified, so the only thing wrong with the entry is the
        # identifier it omits. ``not result.fails`` alone would also have passed
        # for UNCHECKED, which is what a search that was never run produces.
        assert result.verdict == "INCOMPLETE"
        assert [i.field for i in result.issues] == ["doi"]

    def test_a_confirmed_match_is_proposed_and_never_written_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Report, never rewrite.

        Finding the DOI an entry is missing is a proposal for a human to accept.
        The moment orchestration sets ``ref.doi`` itself, the tool has started
        editing bibliographies, and a later run would "confirm" its own guess.
        """
        ref = make_ref(doi=None)
        _install(monkeypatch, search=_StubSearch(candidates=[make_record()]))

        result = audit([ref], _options(tmp_path))[0]

        proposed = [i for i in result.issues if i.kind == "proposed"]
        assert len(proposed) == 1
        assert proposed[0].field == "doi"
        assert proposed[0].registry == DOI
        assert proposed[0].severity == "warning"
        assert ref.doi is None

    def test_a_retraction_outage_on_the_search_path_is_reported_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An entry confirmed by search gets its own retraction check, and its
        own chance to lose a source.

        ``_audit_unidentified`` checks the confirmed candidate's DOI on the spot
        rather than through ``resolve``, so it folds the returned outage into
        its local ``failed`` set instead of the run-wide one. Drop that fold and
        the entry reports a clean retraction status over a source nobody
        reached, on the one path where the DOI was never in the run's DOI list
        to begin with.
        """
        _install(
            monkeypatch,
            search=_StubSearch(candidates=[make_record()]),
            retractions=_StubRetractions(rw_unreachable=True),
        )

        result = audit([make_ref(doi=None)], _options(tmp_path))[0]

        assert result.consulted["retraction-watch"] == UNREACHABLE
        assert any(i.kind == "retraction-unverified" for i in result.issues)
        assert result.ref.doi is None

    def test_a_match_found_only_through_a_non_crossref_source_is_labelled_correctly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Confirmation no longer assumes the match came from Crossref.

        ``registries.search`` widens *discovery*, not just to Crossref's own
        ranking of ``query.bibliographic`` — a candidate Europe PMC or OpenAlex
        surfaced must be usable as the primary record, and the proposed-DOI
        issue must name the registry that actually found it rather than
        hard-coding ``"crossref"``.
        """
        candidate = make_record(source="europepmc")
        _install(monkeypatch, search=_StubSearch(candidates=[candidate]))

        result = audit([make_ref(doi=None)], _options(tmp_path))[0]

        assert result.verdict == "INCOMPLETE"
        proposed = next(i for i in result.issues if i.kind == "proposed")
        assert proposed.source == "europepmc"
        # `Search.sources` names every registry this reference's evidence
        # rests on; `consulted` must say the same, or the report understates
        # how thoroughly an identifier-less entry was checked.
        assert result.consulted["crossref"] == ANSWERED
        assert result.consulted["europepmc"] == ANSWERED
        assert result.consulted["openalex"] == ANSWERED

    def test_a_title_only_match_is_still_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bar belongs to ``confirm_without_id`` and orchestration may not lower it.

        Taking the top-ranked search hit is the obvious shortcut and it is how a
        missing DOI becomes a wrong one: the title matches, the authors do not.
        """
        candidate = make_record(
            doi="10.1234/lookalike",
            authors=[Name(family="Darwin", given="C"), Name(family="Wallace", given="A")],
        )
        _install(monkeypatch, search=_StubSearch(candidates=[candidate]))

        result = audit([make_ref(doi=None)], _options(tmp_path))[0]

        assert result.verdict == "UNCONFIRMED"
        assert not any(i.kind == "proposed" for i in result.issues)
        assert "no confident match" in result.issues[-1].note

    def test_a_review_of_a_book_is_not_accepted_as_the_book(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Searching a book title reliably returns a review of the book."""
        review = make_record(
            doi="10.1234/review", kind="journal-article", years={"print": 1999}
        )
        _install(monkeypatch, search=_StubSearch(candidates=[review]))

        result = audit([make_ref(doi=None, kind="book", year=1998)], _options(tmp_path))[0]

        assert result.verdict == "UNCONFIRMED"
        assert not any(i.kind == "proposed" for i in result.issues)
        assert "type" in result.issues[-1].note

    def test_no_isbn_does_not_accuse_a_book_of_not_existing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--no-isbn`` switches off the only registry organised around books.

        A book whose valid ISBN nobody looked up must not come back ``BAD-ID``.
        "The identifier resolves in no consulted registry" is vacuously true
        when nothing was consulted, and reads as "this book does not exist" —
        ignorance rendered as absence, on a failing verdict.
        """
        stubs = _install(monkeypatch)
        ref = make_ref(key="knuth1997art", doi=None, kind="book", isbn="0-201-89683-4")

        result = audit([ref], _options(tmp_path, use_isbn=False))[0]

        assert result.verdict == "UNCHECKED"
        assert not result.fails
        assert Summary([result]).exit_code() == 0
        # Nothing was asked, and the report must say so rather than imply an
        # answer: OpenLibrary is `not-asked`, never `answered`.
        assert result.consulted.get("openlibrary", "not-asked") == "not-asked"
        assert stubs.openlibrary.by_isbns_calls == []

    def test_a_retraction_watch_outage_does_not_clear_fabricated_dois(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end, through the run-wide unreachable set.

        ``_resolve_retractions`` folds a Retraction Watch outage into the same
        set ``compare`` derives "nothing could be reached" from, so without the
        ``_NO_RESOLUTION_SIGNAL`` guard every invented DOI in the file turns
        from ``BAD-ID``/exit 1 into ``UNCHECKED``/exit 0 — a bibliography of
        fabricated citations passing CI because a side-channel was stale.
        """
        _install(
            monkeypatch,
            crossref=_StubRegistry("crossref", records={DOI: make_record()}),
            retractions=_StubRetractions(rw_unreachable=True),
        )
        refs = [
            make_ref(key="real"),
            make_ref(key="invented", doi="10.9999/does-not-exist"),
        ]
        results = audit(refs, _options(tmp_path))

        assert [r.verdict for r in results] == ["OK", "BAD-ID"]
        assert Summary(results).exit_code() == 1
        # ...and the gap that *is* real is still stated on the entry that resolved.
        assert any(i.kind == "retraction-unverified" for i in results[0].issues)

    def test_a_malformed_isbn_still_fails_with_no_registry_consulted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other side of the same empty ``asked`` set.

        Both cases consult nothing, and they must not share a verdict. A failed
        check digit is arithmetic on the stored value: no registry's answer
        could change it, so it is a finding about the bibliography and fails.
        Turning it into ``UNCHECKED`` alongside the case above would let a
        mistyped identifier pass CI.
        """
        _install(monkeypatch)
        ref = make_ref(key="bad1999isbn", doi=None, kind="book", isbn="0-306-40615-3")

        result = audit([ref], _options(tmp_path))[0]

        assert result.verdict == "BAD-ID"
        assert result.fails
        issue = result.issues[0]
        assert (issue.field, issue.kind) == ("isbn", "malformed")
        assert "check digit" in issue.note

    def test_search_is_not_attempted_when_switched_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--no-search`` skips the lookup; it must not also invent a pass."""
        stubs = _install(monkeypatch, search=_StubSearch(candidates=[make_record()]))

        result = audit(
            [make_ref(doi=None)], _options(tmp_path, search_unidentified=False)
        )[0]

        assert stubs.search.candidate_calls == []
        assert result.verdict == "UNCONFIRMED"

    def test_an_entry_with_a_doi_is_never_sent_to_search(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A DOI that resolves nowhere is a finding, not a prompt to go looking.

        Falling back to a title search here would let a plausible lookalike stand
        in for a citation whose identifier is wrong — which is precisely the
        defect being reported.
        """
        stubs = _install(monkeypatch, search=_StubSearch(candidates=[make_record()]))

        result = audit([make_ref(doi="10.9999/invented.doi")], _options(tmp_path))[0]

        assert stubs.search.candidate_calls == []
        assert result.verdict == "BAD-ID"


class TestResultShape:
    def test_results_follow_input_order_and_each_reference_appears_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The report has to follow the file a reader is about to open.

        Two entries share a DOI and one carries none, so any implementation that
        keyed results by identifier would drop or merge an entry here.
        """
        _install(
            monkeypatch,
            crossref=_StubRegistry("crossref", records={DOI: make_record()}),
            search=_StubSearch(candidates=[make_record()]),
        )
        refs = [
            make_ref(key="first"),
            make_ref(key="second", doi=None),
            make_ref(key="third", doi="https://doi.org/10.1093/IJE/DYX269"),
            make_ref(key="fourth", doi="10.5281/zenodo.1234567", kind="dataset"),
        ]

        results = audit(refs, _options(tmp_path))

        assert [r.ref.key for r in results] == ["first", "second", "third", "fourth"]
        assert len(results) == len(refs)
        # Identity, not equality: the reference in the report is the one that was
        # read, never a copy the tool edited on the way through.
        assert all(result.ref is ref for result, ref in zip(results, refs, strict=True))

    def test_an_empty_bibliography_is_not_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An adapter that matched nothing is a fact about the input, not a crash.

        A markdown file with no citations, or a .bib holding only comments, must
        exit 0 rather than taking the whole CI run down with it.
        """
        stubs = _install(monkeypatch)

        results = audit([], _options(tmp_path))

        assert results == []
        assert Summary(results).exit_code() == 0
        assert stubs.crossref.by_dois_calls == []
        assert stubs.pubmed.by_dois_calls == []


class TestOfflineMode:
    """The real clients, offline, against an empty cache — not a mock of them.

    These deliberately skip :func:`_install`: the offline promise is a property
    of ``Client`` and the registry modules together, and stubbing them out would
    test the stub. The autouse fixture is what guarantees nothing reaches the
    network while they run.
    """

    def test_an_offline_run_over_entries_with_no_doi_checks_and_fails_nothing(
        self, tmp_path: Path
    ) -> None:
        """Regression: this reported an unexamined bibliography as UNCONFIRMED.

        ``--offline`` documents that anything uncached is reported as UNCHECKED.
        Entries carrying no DOI used to skip the lookup path outright and reach
        the comparison with an empty "unreachable" set, so every one came back
        UNCONFIRMED — a *failing* verdict, from a run that consulted nothing at
        all, on a bibliography whose entries are simply missing their DOIs.

        No entry here carries an identifier, which is the shape that broke: with
        even one DOI-bearing entry present, its own outage populated the
        "unreachable" set and masked the defect for every other entry in the run.
        """
        refs = [
            make_ref(key="no_doi_a", doi=None),
            make_ref(key="no_doi_b", doi=None, title="Causal inference: what if", kind="book"),
        ]

        results = audit(refs, _options(tmp_path, offline=True))

        assert [r.verdict for r in results] == ["UNCHECKED", "UNCHECKED"]
        assert not any(r.fails for r in results)
        assert Summary(results).exit_code() == 0

    def test_an_offline_run_over_a_mixed_bibliography_checks_nothing(
        self, tmp_path: Path
    ) -> None:
        """Every branch of ``audit()`` must reach the same conclusion offline.

        A DOI Crossref registers, one only DataCite would have, and one entry
        with no identifier: three different paths, one honest answer, because a
        cold cache means nothing was verified.
        """
        refs = [
            make_ref(key="with_doi"),
            make_ref(key="datacite_doi", doi="10.5281/zenodo.1234567", kind="dataset"),
            make_ref(key="no_doi", doi=None),
        ]

        results = audit(refs, _options(tmp_path, offline=True))

        assert [r.verdict for r in results] == ["UNCHECKED"] * 3
        assert Summary(results).exit_code() == 0

    def test_an_offline_run_writes_nothing_and_fetches_nothing(self, tmp_path: Path) -> None:
        """Offline is the reproducibility switch: no network, no new cache state.

        The autouse fixture already fails the test if a socket opens; this also
        pins that a cold offline run leaves the cache exactly as it found it, so
        re-running it cannot produce a different answer the second time.
        """
        results = audit([make_ref(), make_ref(key="b", doi=None)], _options(tmp_path, offline=True))

        assert all(r.verdict == "UNCHECKED" for r in results)
        assert list((tmp_path / "cache").rglob("*.json")) == []


class TestLayering:
    def test_the_http_client_is_never_used_outside_a_registry_module(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Comparison performs no I/O, and orchestration performs none directly.

        The client installed here raises on any use whatsoever. The stub
        registries ignore it, so the run can only complete if every byte of
        evidence came from the records ``resolve`` had already collected — which
        is what makes a verdict re-derivable from a cached response by hand.
        """
        _install(
            monkeypatch,
            crossref=_StubRegistry("crossref", records={DOI: make_record()}),
            pubmed=_StubRegistry("pubmed", records={DOI: make_pubmed_record(volume="48")}),
        )

        results = audit([make_ref(volume="47"), make_ref(key="b", volume="48")], _options(tmp_path))

        # The first entry agrees with Crossref, the second with PubMed; deriving
        # either verdict needed both records and no further lookup.
        assert [r.verdict for r in results] == ["OK", "DISPUTED"]

    def test_registries_are_handed_identifiers_never_reference_objects(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A registry that saw a ``Reference`` would have to understand adapters.

        The DOI here arrives in the resolver-URL, mixed-case form a Zotero export
        produces, so "it is a string" is not enough to pass: what crosses the
        boundary must be a bare, normalised identifier. A registry handed
        ``https://doi.org/10.5281/ZENODO.1234567`` would have to know which
        adapter conventions to undo, which is the adapters' job and nobody
        else's.
        """
        stubs = _install(
            monkeypatch, crossref=_StubRegistry("crossref", records={DOI: make_record()})
        )
        audit(
            [make_ref(), make_ref(key="b", doi="https://doi.org/10.5281/ZENODO.1234567")],
            _options(tmp_path),
        )

        asked = [doi for call in stubs.crossref.by_dois_calls for doi in call]
        assert asked == sorted([DOI, "10.5281/zenodo.1234567"])
        assert all(normalize_doi(doi) == doi for doi in asked)

    def test_one_http_client_is_built_and_shared_by_every_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All network access goes through a single client, hence a single cache.

        Giving each registry its own client would fragment the on-disk cache and
        the per-host rate limiting, so ``--offline`` would replay only part of a
        previous run and Crossref would see a burst instead of a polite trickle.
        """
        stubs = _install(
            monkeypatch, crossref=_StubRegistry("crossref", records={DOI: make_record()})
        )
        audit([make_ref()], _options(tmp_path))

        all_stubs = (
            stubs.crossref, stubs.datacite, stubs.pubmed,
            stubs.search, stubs.openlibrary, stubs.retractions,
        )
        assert [s.constructions for s in all_stubs] == [1, 1, 1, 1, 1, 1]
        assert stubs.crossref.client is stubs.datacite.client
        assert stubs.datacite.client is stubs.pubmed.client
        assert stubs.pubmed.client is stubs.search.client
        assert stubs.search.client is stubs.openlibrary.client
        assert stubs.openlibrary.client is stubs.retractions.client
        assert isinstance(stubs.crossref.client, _ForbiddenClient)

    def test_run_options_reach_the_client_that_was_built(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Options that only exist to change I/O must actually change it.

        ``cache_dir`` defaults to the user's real cache directory, so a ``_build``
        that ignored it would have every test in this file reading and writing
        ``~/Library/Caches/bibaudit`` — and ``--offline`` would replay a run
        nobody asked for. ``refresh`` and ``mailto`` fail silently in the other
        direction: forgetting to pass them costs a stale answer and Crossref's
        polite pool respectively, and neither ever raises.
        """
        stubs = _install(monkeypatch, crossref=_StubRegistry("crossref"))

        audit(
            [make_ref()],
            _options(tmp_path, mailto="someone@example.org", refresh=True, timeout=7.5),
        )

        client = stubs.crossref.client
        assert isinstance(client, _ForbiddenClient)
        assert isinstance(client.cache, Cache)
        assert client.cache.path == tmp_path / "cache"
        assert client.kwargs == {
            "mailto": "someone@example.org",
            "timeout": 7.5,
            "refresh": True,
            "offline": False,
        }

    def test_no_reference_is_modified_by_the_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Report, never rewrite — every field, not only the proposed DOI.

        The tool has no authority to decide the registry is right and the
        bibliography wrong. An orchestration that "helpfully" filled in a volume
        the entry omits, or copied the registry's spelling of a title over the
        stored one, would make the next run confirm the previous run's edits
        instead of the user's file. Every branch is exercised: an entry that
        agrees, one that disagrees, one that *omits* fields the registry holds —
        the shape a helpful implementation is most tempted to complete — and one
        confirmed by search, the path that actually holds a registry DOI in its
        hand.
        """
        refs = [
            make_ref(key="agrees"),
            make_ref(key="disagrees", volume="99", publisher="Wrong Press"),
            make_ref(
                key="incomplete",
                volume=None, issue=None, pages=None, container=None, year=None,
            ),
            make_ref(key="no_doi", doi=None),
        ]
        before = deepcopy(refs)
        _install(
            monkeypatch,
            crossref=_StubRegistry("crossref", records={DOI: make_record()}),
            search=_StubSearch(candidates=[make_record()]),
        )

        audit(refs, _options(tmp_path))

        assert refs == before

    def test_reading_a_bibliography_consults_no_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Adapters parse; they never resolve.

        An adapter that looked a DOI up while reading would make parsing depend
        on the network, and would make ``bibaudit`` unusable offline for the one
        job that never needs a connection.
        """
        source = tmp_path / "audit_layering.bib"
        source.write_text(
            "@article{molinamontes2018family,\n"
            f"  title = {{{TITLE}}},\n"
            "  author = {Molina-Montes, E and Gomez-Rubio, P},\n"
            f"  doi = {{{DOI}}},\n"
            "  year = {2018},\n"
            "}\n",
            encoding="utf-8",
        )
        stubs = _install(
            monkeypatch, crossref=_StubRegistry("crossref", records={DOI: make_record()})
        )

        refs = read_bibtex(source)

        assert len(refs) == 1
        assert stubs.crossref.constructions == 0
        assert stubs.crossref.by_dois_calls == []

        audit(refs, _options(tmp_path))

        assert stubs.crossref.by_dois_calls == [[DOI]]


class TestSuppressions:
    def test_an_adjudicated_difference_is_moved_and_the_verdict_re_derived(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A suppression must re-derive the verdict, never patch it up ad hoc."""
        _install(
            monkeypatch,
            crossref=_StubRegistry(
                "crossref", records={DOI: make_record(publisher="Elsevier")}
            ),
        )
        suppressions = Suppressions(
            rules=[
                Suppression(
                    key="*", field="publisher",
                    reason="imprint mergers churn these names; not tracked here",
                )
            ]
        )

        result = audit(
            [make_ref(publisher="Oxford University Press")],
            _options(tmp_path, suppressions=suppressions),
        )[0]

        assert not result.issues
        assert [i.field for i in result.suppressed] == ["publisher"]
        # ADJUDICATED, not REGISTRY-ARTIFACT: this difference was silenced by a
        # rule somebody wrote in this project's .bibaudit.toml, which is a
        # human's say-so and can go stale. REGISTRY-ARTIFACT is reserved for a
        # defect documented in docs/registry-artifacts.md and true for everybody.
        # Both are non-failing and both stay visible; conflating them left a
        # reader unable to tell which claim they were looking at.
        assert result.verdict == "ADJUDICATED"
        assert result.adjudicated and not result.artifacts
        assert not result.fails

    def test_suppressing_one_field_does_not_soften_the_verdict_about_another(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: an unrelated suppression downgraded WRONG-WORK.

        The wrong-work judgement depends on whether the author lists corroborate
        — a low title score alone is not enough to accuse a bibliography of
        citing a different paper. Re-deriving the verdict after a suppression
        dropped that flag, so silencing a publisher difference turned
        "this DOI points at another paper" into a mere field mismatch, and the
        report understated a finding about a field nobody had adjudicated.
        """
        record = make_record(
            title="An entirely unrelated paper about marine biology",
            authors=[Name(family="Darwin", given="C"), Name(family="Wallace", given="A")],
            publisher="Elsevier",
        )
        ref = make_ref(publisher="Oxford University Press")
        _install(monkeypatch, crossref=_StubRegistry("crossref", records={DOI: record}))

        untouched = audit([ref], _options(tmp_path))[0]

        _install(monkeypatch, crossref=_StubRegistry("crossref", records={DOI: record}))
        suppressions = Suppressions(
            rules=[Suppression(key="*", field="publisher", reason="imprint churn")]
        )
        adjudicated = audit(
            [ref], _options(tmp_path / "second", suppressions=suppressions)
        )[0]

        assert untouched.verdict == "WRONG-WORK"
        assert adjudicated.verdict == "WRONG-WORK"
        assert [i.field for i in adjudicated.suppressed] == ["publisher"]

    def test_a_suppression_cannot_clear_a_retraction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Silencing the notice must not silence the retraction.

        Re-deriving the verdict from the surviving issues alone would leave an
        error-free issue list and report a retracted paper as an artifact — the
        one verdict that fails even when every field is correct, gone because a
        line was added to a config file.
        """
        _install(
            monkeypatch,
            crossref=_StubRegistry(
                "crossref",
                records={DOI: make_record(retracted=True, retraction_kind="retraction")},
            ),
        )
        suppressions = Suppressions(
            rules=[
                Suppression(
                    key="*", field="status",
                    reason="checked the notice; it names a different paper",
                )
            ]
        )

        result = audit([make_ref()], _options(tmp_path, suppressions=suppressions))[0]

        assert result.verdict == "RETRACTED"
        assert result.fails
