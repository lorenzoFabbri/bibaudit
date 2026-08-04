"""The command line: argument parsing, exit codes, and what may be written.

Every test drives ``cli.main(argv)`` in process rather than a subprocess, so
a failure shows the real traceback instead of a captured stdout blob.

Only the three registry *clients* are stubbed, at the seam ``audit._build``
uses. The adapters, the comparison matrix, the verdict rule, the suppression
loader and both reporters stay real, so a regression in any of them still
reaches these tests; stubbing ``audit()`` itself would hide all of it. An
autouse fixture makes any actual network call raise, so "offline" is a
property the tests enforce rather than assume.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import urllib.request
from collections.abc import Sequence
from pathlib import Path

import pytest

from bibaudit import __version__
from bibaudit.cli import _collect, build_parser, main
from bibaudit.model import FAILING_VERDICTS, Name, Record, Reference
from bibaudit.normalize import normalize_doi
from bibaudit.registries.http import Transient
from bibaudit.registries.retractions import RetractionNotice, RetractionStatus

#: The module object, not ``bibaudit.audit`` the function: the package
#: re-exports ``audit()`` under that name, so a plain
#: ``from bibaudit import audit`` binds the function and monkeypatching the
#: registry classes on it would fail.
audit_module = importlib.import_module("bibaudit.audit")

MAIN_DOI = "10.1093/ije/dyx269"
SECOND_DOI = "10.1234/example.2019"

BIB_MAIN = """\
@article{molinamontes2018family,
  author  = {Molina-Montes, E. and Gomez-Rubio, P.},
  title   = {Risk of pancreatic cancer associated with family history of cancer},
  journal = {International Journal of Epidemiology},
  year    = {2018},
  volume  = {47},
  number  = {2},
  pages   = {473--483},
  doi     = {10.1093/ije/dyx269},
}
"""

#: The same entry with no ``volume`` at all. ``--suggest`` may only fill a
#: field the entry has *no* value for, so a gap is what a suggestion test
#: needs; a disagreement would (correctly) propose nothing.
BIB_MISSING_VOLUME = """\
@article{molinamontes2018family,
  author  = {Molina-Montes, E. and Gomez-Rubio, P.},
  title   = {Risk of pancreatic cancer associated with family history of cancer},
  journal = {International Journal of Epidemiology},
  year    = {2018},
  number  = {2},
  pages   = {473--483},
  doi     = {10.1093/ije/dyx269},
}
"""

BIB_SECOND = """\
@article{smith2019cohort,
  author  = {Smith, J.},
  title   = {A cohort study of something entirely different},
  journal = {Journal of Epidemiology},
  year    = {2019},
  volume  = {30},
  number  = {1},
  pages   = {1--9},
  doi     = {10.1234/example.2019},
}
"""


def record_main(**overrides: object) -> Record:
    """The Crossref record that agrees with ``BIB_MAIN`` field for field."""
    base: dict[str, object] = {
        "source": "crossref",
        "doi": MAIN_DOI,
        "title": "Risk of pancreatic cancer associated with family history of cancer",
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


def record_second(**overrides: object) -> Record:
    base: dict[str, object] = {
        "source": "crossref",
        "doi": SECOND_DOI,
        "title": "A cohort study of something entirely different",
        "authors": [Name(family="Smith", given="J")],
        "years": {"print": 2019},
        "container": "Journal of Epidemiology",
        "volume": "30",
        "issue": "1",
        "pages": "1-9",
        "kind": "journal-article",
    }
    base.update(overrides)
    return Record(**base)  # type: ignore[arg-type]


class _StubRegistry:
    """Stands in for :class:`Crossref`, :class:`DataCite` or :class:`PubMed`.

    Implements only the one method :mod:`bibaudit.audit` calls on it
    (``by_dois``, the DOI-lookup path). The identifier-less *search* role is a
    separate seam, ``audit.py`` asks ``registries.search`` for it — see
    :class:`_StubSearch`. ``transient`` reproduces an outage the way the real
    client does — by raising :class:`Transient` — because the difference
    between "the registry does not have it" (an empty answer) and "the
    registry could not be asked" is the whole reason ``UNCHECKED`` exists.
    """

    def __init__(
        self,
        source: str,
        records: dict[str, Record] | None = None,
        *,
        transient: bool = False,
    ) -> None:
        self.source = source
        self.records = dict(records or {})
        self.transient = transient
        self.requested: list[str] = []

    def by_dois(self, dois: Sequence[str]) -> dict[str, Record]:
        self.requested.extend(dois)
        if self.transient:
            raise Transient(f"{self.source}: stubbed outage")
        wanted = {normalize_doi(doi) for doi in dois}
        return {doi: record for doi, record in self.records.items() if doi in wanted}


class _StubSearch:
    """Stands in for :class:`~bibaudit.registries.search.Search`.

    An identifier-less entry goes through ``registries.search``, not
    ``registries.crossref`` directly — see ``audit._audit_unidentified`` —
    so that is the seam this stub occupies.
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
        self.candidate_calls: list[Reference] = []

    def candidates(self, ref: Reference, rows: int = 5) -> list[Record]:
        self.candidate_calls.append(ref)
        if self.transient:
            raise Transient("search: stubbed outage")
        return list(self.candidate_results)


class _StubRetractions:
    """Stands in for :class:`~bibaudit.registries.retractions.Retractions`.

    Mandatory to install, not merely convenient: this file never patches
    ``Client``, only the registry classes ``audit._build`` looks up by name
    (see ``_offline_environment`` — the real guard is ``urlopen`` itself
    raising), so an unstubbed ``Retractions`` would build a real one and
    reach for the network the moment any DOI-bearing bibliography in this
    file goes through ``resolve``.
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
        #: Retraction Watch's export failing, reported through the return
        #: value; ``transient`` is PubMed's, which raises.
        self.rw_unreachable = rw_unreachable
        self.status_for_calls: list[list[str]] = []

    def status_for(self, dois: Sequence[str]) -> RetractionStatus:
        self.status_for_calls.append(list(dois))
        if self.transient:
            raise Transient("retractions: stubbed outage")
        wanted = {normalize_doi(doi) for doi in dois}
        return RetractionStatus(
            notices={doi: notice for doi, notice in self.notices.items() if doi in wanted},
            unreachable=frozenset({"retraction-watch"}) if self.rw_unreachable else frozenset(),
        )


def install_registries(
    monkeypatch: pytest.MonkeyPatch,
    *,
    crossref: _StubRegistry | None = None,
    datacite: _StubRegistry | None = None,
    pubmed: _StubRegistry | None = None,
    search: _StubSearch | None = None,
    retractions: _StubRetractions | None = None,
) -> dict[str, _StubRegistry | _StubSearch | _StubRetractions]:
    """Replace the five registry classes ``audit._build`` instantiates."""
    stubs: dict[str, _StubRegistry | _StubSearch | _StubRetractions] = {
        "crossref": crossref if crossref is not None else _StubRegistry("crossref"),
        "datacite": datacite if datacite is not None else _StubRegistry("datacite"),
        "pubmed": pubmed if pubmed is not None else _StubRegistry("pubmed"),
        "search": search if search is not None else _StubSearch(),
        "retractions": retractions if retractions is not None else _StubRetractions(),
    }
    for attribute, name in (
        ("Crossref", "crossref"),
        ("DataCite", "datacite"),
        ("PubMed", "pubmed"),
        ("Search", "search"),
        ("Retractions", "retractions"),
    ):
        monkeypatch.setattr(
            audit_module, attribute, lambda _client, _n=name, **_kw: stubs[_n]
        )
    return stubs


def resolving_crossref() -> _StubRegistry:
    """A Crossref that knows both fixture DOIs and agrees with both entries."""
    return _StubRegistry("crossref", {MAIN_DOI: record_main(), SECOND_DOI: record_second()})


@pytest.fixture(autouse=True)
def _offline_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No network, no colour, and no writes to the user's real cache.

    ``urlopen`` is made to raise rather than merely be unused: a test that
    quietly reached the network would otherwise pass on a connected laptop
    and hang in CI, and the whole suite's promise is that it needs neither.
    ``AssertionError`` is deliberate — ``main`` converts ``OSError`` into
    exit code 2, so a network error raised as one would be swallowed and
    reported as an ordinary usage failure.
    """

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("a CLI test attempted a network request")

    monkeypatch.setattr(urllib.request, "urlopen", _forbidden)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setenv("NO_COLOR", "1")


@pytest.fixture
def bib(tmp_path: Path) -> Path:
    """A one-entry bibliography that a stubbed Crossref fully corroborates."""
    path = tmp_path / "references.bib"
    path.write_text(BIB_MAIN, encoding="utf-8")
    return path


def check(tmp_path: Path, *args: str) -> list[str]:
    """``check`` argv with the cache pinned inside the test's own directory."""
    return ["check", *args, "--cache-dir", str(tmp_path / "cache")]


def unwrapped(text: str) -> str:
    """*text* with runs of whitespace collapsed.

    argparse re-wraps help strings to the terminal width, so a phrase in a
    ``help=`` value can be split across lines at any point; asserting on the
    raw output would make a test pass or fail depending on ``$COLUMNS``.
    """
    return " ".join(text.split())


class TestHelp:
    def test_the_top_level_help_renders(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exit_info:
            main(["--help"])
        assert exit_info.value.code == 0
        assert "usage: bibaudit" in capsys.readouterr().out

    def test_the_top_level_help_states_what_the_tool_cannot_do(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The limit belongs where people meet the tool, not only in the report.

        Someone reading ``--help`` to decide whether to adopt it must not come
        away believing a green run means the citations support their claims.
        """
        with pytest.raises(SystemExit):
            main(["--help"])
        assert "supports the claim" in unwrapped(capsys.readouterr().out)

    @pytest.mark.parametrize("command", ["check", "cache"])
    def test_each_subcommand_help_renders(
        self, command: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exit_info:
            main([command, "--help"])
        assert exit_info.value.code == 0
        assert f"usage: bibaudit {command}" in capsys.readouterr().out

    def test_the_suggest_flag_promises_the_original_is_untouched(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The second rule has to be visible to someone who never reads the docs."""
        with pytest.raises(SystemExit):
            main(["check", "--help"])
        assert "the original is never written to" in unwrapped(capsys.readouterr().out)

    def test_version_prints_and_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The version, not just the program name.

        ``prog`` is ``bibaudit``, so asserting only that would pass on a
        ``--version`` that printed nothing but the usage line. A verdict is
        reproducible from a cached response *and the version that read it*,
        so this is the one string an audit trail cannot do without.
        """
        with pytest.raises(SystemExit) as exit_info:
            main(["--version"])
        assert exit_info.value.code == 0
        assert __version__ in capsys.readouterr().out


class TestArgumentParsing:
    def test_a_missing_subcommand_is_a_usage_error(self) -> None:
        with pytest.raises(SystemExit) as exit_info:
            build_parser().parse_args([])
        assert exit_info.value.code == 2

    def test_check_requires_at_least_one_path(self) -> None:
        with pytest.raises(SystemExit) as exit_info:
            build_parser().parse_args(["check"])
        assert exit_info.value.code == 2

    def test_fail_on_defaults_to_the_whole_failing_set(self) -> None:
        """The default has to be the set the model calls failing, not a subset.

        A hand-written default that drifted from ``FAILING_VERDICTS`` would
        make a verdict fail ``Result.fails`` while the process still exited 0.
        """
        args = build_parser().parse_args(["check", "references.bib"])
        assert set(args.fail_on.split(",")) == set(FAILING_VERDICTS)
        assert "RETRACTED" in args.fail_on

    def test_registry_and_report_defaults(self) -> None:
        args = build_parser().parse_args(["check", "references.bib"])
        assert args.offline is False
        assert args.refresh is False
        assert args.suggest is False, "--suggest must be opt-in; it writes files"
        assert args.no_corroborate is False, "PubMed corroboration is the default second opinion"
        assert args.cache_ttl == 90
        assert args.timeout == 30.0
        assert args.format == "text"

    def test_bibliography_is_repeatable(self) -> None:
        args = build_parser().parse_args(
            ["check", "notes/", "-b", "one.bib", "--bibliography", "two.bib"]
        )
        assert args.bibliography == [Path("one.bib"), Path("two.bib")]

    def test_an_unknown_report_format_is_refused(self) -> None:
        with pytest.raises(SystemExit) as exit_info:
            build_parser().parse_args(["check", "references.bib", "--format", "xml"])
        assert exit_info.value.code == 2

    def test_cache_takes_only_its_two_actions(self) -> None:
        assert build_parser().parse_args(["cache", "info"]).action == "info"
        assert build_parser().parse_args(["cache", "clear"]).action == "clear"
        with pytest.raises(SystemExit) as exit_info:
            build_parser().parse_args(["cache", "wipe"])
        assert exit_info.value.code == 2

    def test_thresholds_are_tunable_from_the_command_line(self) -> None:
        args = build_parser().parse_args(
            ["check", "references.bib", "--title-mismatch", "0.7", "--title-wrong-work", "0.4"]
        )
        assert args.title_mismatch == 0.7
        assert args.title_wrong_work == 0.4


class TestExitCodes:
    def test_a_corroborated_bibliography_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bib: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        install_registries(monkeypatch, crossref=resolving_crossref())
        assert main(check(tmp_path, str(bib))) == 0
        out = capsys.readouterr().out
        assert "PASS" in out
        # Anchors every other test that leans on `resolving_crossref`: PASS
        # alone is also what a run of nothing-but-INCOMPLETE prints, so
        # without this the stub could stop corroborating anything at all and
        # the "exits zero" tests would still be green.
        assert "OK" in out
        assert "FIELD-MISMATCH" not in out

    def test_a_field_mismatch_exits_one(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bib: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The premise of the tool: the DOI resolves and the entry is still wrong."""
        install_registries(
            monkeypatch,
            crossref=_StubRegistry("crossref", {MAIN_DOI: record_main(volume="48")}),
        )
        assert main(check(tmp_path, str(bib))) == 1
        out = capsys.readouterr().out
        assert "FIELD-MISMATCH" in out
        assert "48" in out and "47" in out

    def test_a_doi_no_registry_has_exits_one(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bib: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Every registry answered and none of them has it: that is a fact.

        The verdict is asserted, not just the exit code — BAD-ID, UNCONFIRMED
        and a mis-derived UNCHECKED all exit 1 or 0 for entirely different
        reasons, and only one of them is right here.
        """
        install_registries(monkeypatch)
        assert main(check(tmp_path, str(bib))) == 1
        assert "BAD-ID" in capsys.readouterr().out

    def test_an_unreachable_registry_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bib: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An outage is ignorance, not a finding.

        If a network failure broke the build, the first thing every project
        would do is stop running the check.
        """
        install_registries(
            monkeypatch,
            crossref=_StubRegistry("crossref", transient=True),
            datacite=_StubRegistry("datacite", transient=True),
            pubmed=_StubRegistry("pubmed", transient=True),
        )
        assert main(check(tmp_path, str(bib))) == 0
        assert "UNCHECKED" in capsys.readouterr().out

    def test_the_primary_registry_alone_timing_out_is_still_not_a_finding(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bib: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """One registry's outage must not become the whole run's "not found".

        Crossref times out; DataCite and PubMed answer, and have nothing.
        With every registry stubbed as unreachable at once (the test above)
        it takes only *one* of the three to be handled correctly for the run
        to come out UNCHECKED, so that test passes even when Crossref's own
        ``Transient`` is swallowed instead of recorded — the exact way the
        404-is-a-fact/timeout-is-ignorance rule gets broken, and the way that
        turns an ordinary Crossref outage into a bibliography reported as
        full of unresolvable DOIs.
        """
        install_registries(
            monkeypatch, crossref=_StubRegistry("crossref", transient=True)
        )
        assert main(check(tmp_path, str(bib), "--format", "json",
                          "--output", str(tmp_path / "audit.json"))) == 0
        payload = json.loads((tmp_path / "audit.json").read_text(encoding="utf-8"))
        assert payload["results"][0]["verdict"] == "UNCHECKED"
        # The three states are named, not collapsed into a bool: Crossref was
        # asked and could not answer, PubMed was asked and did. "Not known to be
        # unreachable" used to stand in for both, so a run that never built the
        # PubMed client at all still reported it as consulted.
        assert payload["results"][0]["consulted"]["crossref"] == "unreachable"
        assert payload["results"][0]["consulted"]["pubmed"] == "answered"

    def test_no_corroborate_stops_pubmed_being_asked(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bib: Path
    ) -> None:
        """The flag has to reach the auditor, not merely parse.

        The first half is the anchor: without it, "PubMed was not asked"
        would pass even if the reference had never been looked up at all.
        """
        stubs = install_registries(monkeypatch, crossref=resolving_crossref())
        assert main(check(tmp_path, str(bib))) == 0
        assert stubs["pubmed"].requested == [MAIN_DOI]

        stubs = install_registries(monkeypatch, crossref=resolving_crossref())
        assert main(check(tmp_path, str(bib), "--no-corroborate")) == 0
        assert stubs["pubmed"].requested == []

    def test_a_merely_incomplete_entry_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A thin entry is not a defect in the bibliography's honesty."""
        bib = tmp_path / "references.bib"
        bib.write_text(BIB_MISSING_VOLUME, encoding="utf-8")
        install_registries(monkeypatch, crossref=resolving_crossref())
        assert main(check(tmp_path, str(bib))) == 0
        assert "INCOMPLETE" in capsys.readouterr().out

    def test_a_retraction_exits_one(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bib: Path
    ) -> None:
        install_registries(
            monkeypatch,
            crossref=_StubRegistry(
                "crossref", {MAIN_DOI: record_main(retracted=True, retraction_kind="retraction")}
            ),
        )
        assert main(check(tmp_path, str(bib))) == 1

    def test_fail_on_narrows_what_breaks_the_build(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bib: Path
    ) -> None:
        install_registries(
            monkeypatch,
            crossref=_StubRegistry("crossref", {MAIN_DOI: record_main(volume="48")}),
        )
        assert main(check(tmp_path, str(bib), "--fail-on", "RETRACTED")) == 0
        assert main(check(tmp_path, str(bib), "--fail-on", "RETRACTED,FIELD-MISMATCH")) == 1

    def test_an_empty_fail_on_never_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bib: Path
    ) -> None:
        install_registries(
            monkeypatch,
            crossref=_StubRegistry(
                "crossref", {MAIN_DOI: record_main(retracted=True, retraction_kind="retraction")}
            ),
        )
        assert main(check(tmp_path, str(bib), "--fail-on", "")) == 0

    def test_fail_on_accepts_the_verdicts_in_any_case(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bib: Path
    ) -> None:
        """A lower-cased verdict silently matching nothing would disable CI."""
        install_registries(
            monkeypatch,
            crossref=_StubRegistry("crossref", {MAIN_DOI: record_main(volume="48")}),
        )
        assert main(check(tmp_path, str(bib), "--fail-on", " field-mismatch ")) == 1

    def test_the_json_exit_code_is_the_exit_code_the_process_returned(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bib: Path
    ) -> None:
        """``summary.exit_code`` is named after ``$?`` and is read as it.

        Computed from the default failing set regardless of ``--fail-on``,
        the payload said ``1`` in a run that exited ``0``, and a CI job that
        gated on the field rather than on the process failed a build this
        tool had deliberately passed.
        """
        install_registries(
            monkeypatch,
            crossref=_StubRegistry("crossref", {MAIN_DOI: record_main(volume="48")}),
        )
        target = tmp_path / "audit.json"
        argv = check(tmp_path, str(bib), "--format", "json", "--output", str(target))

        code = main([*argv, "--fail-on", "RETRACTED"])
        assert code == 0
        assert json.loads(target.read_text(encoding="utf-8"))["summary"]["exit_code"] == 0

        code = main([*argv, "--fail-on", "FIELD-MISMATCH"])
        assert code == 1
        assert json.loads(target.read_text(encoding="utf-8"))["summary"]["exit_code"] == 1


class TestUsageErrors:
    def test_an_unknown_path_exits_two_with_a_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A typo in a path is a usage error, not a stack trace."""
        assert main(check(tmp_path, str(tmp_path / "nope.bib"))) == 2
        captured = capsys.readouterr()
        assert "no such path" in captured.err
        assert "Traceback" not in captured.err

    def test_a_file_no_adapter_claims_exits_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        odd = tmp_path / "notes.docx"
        odd.write_text("not a bibliography", encoding="utf-8")
        assert main(check(tmp_path, str(odd))) == 2
        assert "do not know how to read" in capsys.readouterr().err

    def test_a_missing_extra_bibliography_exits_two(
        self, tmp_path: Path, bib: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(check(tmp_path, str(bib), "-b", str(tmp_path / "absent.bib")))
        assert code == 2
        assert "no such bibliography" in capsys.readouterr().err

    def test_a_directory_with_nothing_to_check_exits_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        assert main(check(tmp_path, str(empty))) == 2
        assert "no references found" in capsys.readouterr().err

    def test_an_unwritable_output_path_exits_two(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bib: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An I/O failure is exit 2, alongside the other "could not run" cases."""
        install_registries(monkeypatch, crossref=resolving_crossref())
        target = tmp_path / "missing-directory" / "report.txt"
        assert main(check(tmp_path, str(bib), "--output", str(target))) == 2
        err = capsys.readouterr().err
        assert "Traceback" not in err
        # A bare exit 2 with nothing on stderr is indistinguishable from a
        # crash; the line has to name the tool and the path that failed.
        assert err.startswith("bibaudit:")
        assert "report.txt" in err

    def test_a_suppression_without_a_reason_exits_two(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bib: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An unexplained suppression must stop the run, not silently apply."""
        (tmp_path / ".bibaudit.toml").write_text(
            '[[ignore]]\nkey = "molinamontes2018family"\nfield = "authors"\n', encoding="utf-8"
        )
        install_registries(monkeypatch, crossref=resolving_crossref())
        assert main(check(tmp_path, str(bib))) == 2
        assert "reason" in capsys.readouterr().err


class TestOffline:
    def test_offline_with_an_empty_cache_exits_zero(
        self, tmp_path: Path, bib: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The reproducibility switch must not reach the network, ever.

        No registry stub is installed here: the real Crossref, DataCite and
        PubMed clients run against a real, empty cache, and the autouse
        fixture makes any socket use raise. Everything uncached is therefore
        UNCHECKED, which is not a failure.
        """
        assert main(check(tmp_path, str(bib), "--offline")) == 0
        assert "UNCHECKED" in capsys.readouterr().out

    @pytest.fixture
    def unidentified(self, tmp_path: Path) -> Path:
        """A bibliography entry carrying no identifier of any kind."""
        bib = tmp_path / "references.bib"
        bib.write_text(
            "@article{noid2020,\n  title = {A paper with no identifier},\n"
            "  author = {Doe, J.},\n  year = {2020},\n}\n",
            encoding="utf-8",
        )
        return bib

    def test_offline_reports_an_unidentified_entry_as_unchecked(
        self, tmp_path: Path, unidentified: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Nothing was consulted, so nothing may be concluded.

        Again with the real clients and no network: an entry carrying no
        identifier cannot be looked up offline, and reporting it as
        UNCONFIRMED — a *failing* verdict meaning "no confident registry
        match" — would fail a build on the strength of a lookup that never
        happened.
        """
        assert main(check(tmp_path, str(unidentified), "--offline")) == 0
        assert "UNCHECKED" in capsys.readouterr().out

    def test_no_search_stops_the_title_lookup(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unidentified: Path
    ) -> None:
        """``--no-search`` has to reach the auditor, not just parse.

        The first half is the anchor: without it, "no search happened" would
        pass even if the entry were never examined at all.
        """
        stubs = install_registries(monkeypatch)
        main(check(tmp_path, str(unidentified)))
        assert len(stubs["search"].candidate_calls) == 1

        stubs = install_registries(monkeypatch)
        main(check(tmp_path, str(unidentified), "--no-search"))
        assert stubs["search"].candidate_calls == []


class TestSuggest:
    def test_the_original_bibliography_is_left_byte_identical(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The tool's second rule, checked the only way that means anything.

        A hash before and after: not "no obvious change", not "still parses"
        — the same bytes. This file is the user's manuscript source.
        """
        bib = tmp_path / "references.bib"
        bib.write_text(BIB_MISSING_VOLUME, encoding="utf-8")
        before = hashlib.sha256(bib.read_bytes()).hexdigest()

        install_registries(monkeypatch, crossref=resolving_crossref())
        assert main(check(tmp_path, str(bib), "--suggest")) == 0

        # The anchor, and it is not decoration: an unchanged hash is also
        # what a --suggest that did nothing at all produces. Unless the run
        # really did have something to write — and wrote it elsewhere — this
        # test proves nothing about the rule it is named for.
        assert bib.with_name("references.suggested.bib").is_file()
        assert hashlib.sha256(bib.read_bytes()).hexdigest() == before

    def test_the_suggestion_is_written_beside_the_input(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        bib = tmp_path / "references.bib"
        bib.write_text(BIB_MISSING_VOLUME, encoding="utf-8")
        install_registries(monkeypatch, crossref=resolving_crossref())

        main(check(tmp_path, str(bib), "--suggest"))

        suggested = bib.with_name("references.suggested.bib")
        diff = bib.with_name("references.suggested.diff")
        assert suggested.is_file() and diff.is_file()
        assert suggested.parent == bib.parent
        assert "volume = {47}" in suggested.read_text(encoding="utf-8")
        assert "review before use" in capsys.readouterr().err.lower()

    def test_nothing_is_written_without_the_flag(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Writing files must be something the user asked for, every time."""
        bib = tmp_path / "references.bib"
        bib.write_text(BIB_MISSING_VOLUME, encoding="utf-8")
        before = hashlib.sha256(bib.read_bytes()).hexdigest()
        install_registries(monkeypatch, crossref=resolving_crossref())

        # The same input that *does* produce a suggestion under --suggest
        # (see the two tests above), so "nothing was written" here is a
        # decision the flag made, not an empty proposal.
        main(check(tmp_path, str(bib)))

        assert not list(tmp_path.glob("*.suggested.*"))
        assert hashlib.sha256(bib.read_bytes()).hexdigest() == before

    def test_a_suggested_copy_is_not_audited_on_the_next_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``--suggest`` output is generated, like ``_site/`` — not a source.

        ``<name>.suggested.bib`` lands beside its input, so the next
        ``bibaudit check .`` over the project used to pick it up as a second
        bibliography: every entry checked twice, every DOI reported under
        "duplicate doi", and half the findings pointing at a file the user
        cannot usefully edit. The stale copy here carries a DOI no registry
        knows, so reading it would also exit 1.
        """
        project = tmp_path / "project"
        project.mkdir()
        (project / "references.bib").write_text(BIB_MAIN, encoding="utf-8")
        (project / "references.suggested.bib").write_text(
            BIB_MAIN.replace("molinamontes2018family", "suggestedcopy").replace(
                MAIN_DOI, "10.9999/suggested.copy"
            ),
            encoding="utf-8",
        )
        install_registries(monkeypatch, crossref=resolving_crossref())

        assert main(check(tmp_path, str(project))) == 0
        assert "duplicate" not in capsys.readouterr().out

    def test_a_suggested_copy_named_directly_is_still_audited(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Skipping the generated copy is a directory-walk rule, not a ban.

        Someone reviewing a proposal before applying it by hand runs
        ``bibaudit check references.suggested.bib``, and that has to work —
        otherwise the file the tool tells you to review is the one file it
        refuses to check.
        """
        suggested = tmp_path / "references.suggested.bib"
        suggested.write_text(BIB_MAIN, encoding="utf-8")
        install_registries(monkeypatch, crossref=resolving_crossref())

        assert main(check(tmp_path, str(suggested))) == 0

    def test_the_json_report_stays_parseable_when_a_suggestion_is_announced(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """stdout is a machine contract under --format json.

        The "I wrote a file" notice belongs on stderr; printed on stdout it
        would make every JSON consumer fail to parse the report.
        """
        bib = tmp_path / "references.bib"
        bib.write_text(BIB_MISSING_VOLUME, encoding="utf-8")
        install_registries(monkeypatch, crossref=resolving_crossref())

        main(check(tmp_path, str(bib), "--suggest", "--format", "json"))

        captured = capsys.readouterr()
        assert json.loads(captured.out)["tool"] == "bibaudit"
        assert "references.suggested.bib" in captured.err


class TestReportRouting:
    def test_output_goes_to_the_named_file_and_not_to_stdout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bib: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        install_registries(monkeypatch, crossref=resolving_crossref())
        target = tmp_path / "audit.txt"

        assert main(check(tmp_path, str(bib), "--output", str(target))) == 0

        assert "PASS" in target.read_text(encoding="utf-8")
        assert capsys.readouterr().out == ""

    def test_json_written_to_a_file_is_valid_json(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bib: Path
    ) -> None:
        install_registries(monkeypatch, crossref=resolving_crossref())
        target = tmp_path / "audit.json"

        assert main(check(tmp_path, str(bib), "--format", "json", "--output", str(target))) == 0

        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["results"][0]["key"] == "molinamontes2018family"
        assert payload["limits"]

    def test_verbose_reaches_the_reporter(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bib: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A flag that only parses is a flag that does nothing.

        Without ``--verbose`` an entry whose verdict is informational gets no
        per-entry block, so its citekey is the observable difference. The
        first assertion is the anchor: it fails if the entry is being listed
        anyway, which would make the second one pass for free.
        """
        install_registries(monkeypatch, crossref=resolving_crossref())
        argv = check(tmp_path, str(bib))

        assert main(argv) == 0
        assert "molinamontes2018family" not in capsys.readouterr().out

        assert main([*argv, "--verbose"]) == 0
        assert "molinamontes2018family" in capsys.readouterr().out

    def test_show_suppressed_lists_the_project_s_own_adjudications(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bib: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """End to end: ``.bibaudit.toml`` silences a real mismatch, and says so.

        A suppression that stopped being *reported* would let one line of
        project config turn a genuine FIELD-MISMATCH into a report that reads
        as a clean, fully checked run — which is the failure this tool exists
        to prevent, arriving from inside the tool. So the run must still
        count the suppression by default, and ``--show-suppressed`` must
        print the reason the human recorded.
        """
        (tmp_path / ".bibaudit.toml").write_text(
            '[[ignore]]\nkey = "molinamontes2018family"\nfield = "volume"\n'
            'reason = "checked against the PDF 2026-07-30"\n',
            encoding="utf-8",
        )
        install_registries(
            monkeypatch,
            crossref=_StubRegistry("crossref", {MAIN_DOI: record_main(volume="48")}),
        )
        argv = check(tmp_path, str(bib))

        assert main(argv) == 0, "an adjudicated difference must not fail the build"
        default_run = capsys.readouterr().out
        assert "suppressed" in default_run
        assert "checked against the PDF" not in default_run

        assert main([*argv, "--show-suppressed"]) == 0
        assert "checked against the PDF 2026-07-30" in capsys.readouterr().out

    def test_every_report_states_the_tool_s_limits(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bib: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Including — especially — the run where everything passed."""
        install_registries(monkeypatch, crossref=resolving_crossref())
        main(check(tmp_path, str(bib)))
        assert "cannot verify" in capsys.readouterr().out


class TestInputExpansion:
    def test_a_directory_is_expanded_into_the_bibliographies_it_holds(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        project = tmp_path / "project"
        project.mkdir()
        (project / "references.bib").write_text(BIB_MAIN, encoding="utf-8")
        install_registries(monkeypatch, crossref=resolving_crossref())

        assert main(check(tmp_path, str(project))) == 0

    def test_a_bibliography_reached_two_ways_is_read_once(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A document's front matter names the same file the command line does.

        Front-matter discovery resolves what it finds; the command line keeps
        what the user typed. Compared as written, `references.bib` and the
        absolute path to the same file are two inputs, and the bibliography is
        read twice.

        Asserted here rather than through a report because ``_deduplicate``
        collapses on the identifier, so an entry carrying a DOI survives the
        double read and one without it does not — the visible damage is a
        summary that counts part of a bibliography twice, and which part depends
        on how much of it has DOIs.
        """
        project = tmp_path / "project"
        project.mkdir()
        (project / "references.bib").write_text(BIB_MAIN, encoding="utf-8")
        (project / "paper.qmd").write_text(
            "---\nbibliography: references.bib\n---\n\nText citing "
            "@molinamontes2018family.\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(project)

        references, _, defined, bibs = _collect(
            argparse.Namespace(paths=["paper.qmd", "references.bib"], bibliography=[])
        )
        assert bibs == [Path("references.bib")]
        assert [ref.key for ref in references] == ["molinamontes2018family"]
        assert defined == {"molinamontes2018family"}

    def test_the_same_file_named_twice_is_read_once(self, tmp_path: Path) -> None:
        """`bibaudit check notes/ notes/references.bib` names one file twice."""
        project = tmp_path / "project"
        project.mkdir()
        bib = project / "references.bib"
        bib.write_text(BIB_MAIN, encoding="utf-8")

        references, _, _, bibs = _collect(
            argparse.Namespace(paths=[str(project), str(bib)], bibliography=[])
        )
        assert bibs == [bib]
        assert [ref.key for ref in references] == ["molinamontes2018family"]

    def test_the_bibliography_flag_does_not_re_add_a_named_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`--bibliography` reaches the same set through the same comparison."""
        bib = tmp_path / "references.bib"
        bib.write_text(BIB_MAIN, encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        references, _, _, bibs = _collect(
            argparse.Namespace(paths=[str(bib)], bibliography=["./references.bib"])
        )
        assert bibs == [bib]
        assert [ref.key for ref in references] == ["molinamontes2018family"]

    def test_generated_copies_of_the_sources_are_not_audited(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A rendered site holds a stale copy of the bibliography.

        Auditing ``_site/`` reports every finding twice and points the second
        one at a file the user cannot fix. The stale copy here carries a DOI
        no registry knows, so if it were read the run would exit 1.
        """
        project = tmp_path / "project"
        (project / "_site").mkdir(parents=True)
        (project / "references.bib").write_text(BIB_MAIN, encoding="utf-8")
        (project / "_site" / "references.bib").write_text(
            BIB_MAIN.replace("molinamontes2018family", "stalecopy").replace(
                MAIN_DOI, "10.9999/stale.copy"
            ),
            encoding="utf-8",
        )
        install_registries(monkeypatch, crossref=resolving_crossref())

        assert main(check(tmp_path, str(project))) == 0

    def test_a_project_living_under_a_dot_directory_is_still_read(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Hidden-directory filtering applies inside the tree, not above it.

        An Obsidian vault under ``~/.notes`` or a CI checkout under
        ``~/.cache`` has a dot component in every absolute path part. Testing
        those discarded every file in the tree and reported "no references
        found", which reads like an empty bibliography rather than a bug.
        """
        vault = tmp_path / ".vault"
        vault.mkdir()
        (vault / "references.bib").write_text(BIB_MAIN, encoding="utf-8")
        install_registries(monkeypatch, crossref=resolving_crossref())

        assert main(check(tmp_path, str(vault))) == 0

    def test_hidden_directories_inside_the_tree_are_still_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        project = tmp_path / "project"
        (project / ".quarto").mkdir(parents=True)
        (project / "references.bib").write_text(BIB_MAIN, encoding="utf-8")
        (project / ".quarto" / "references.bib").write_text(
            BIB_MAIN.replace("molinamontes2018family", "cachedcopy").replace(
                MAIN_DOI, "10.9999/cached.copy"
            ),
            encoding="utf-8",
        )
        install_registries(monkeypatch, crossref=resolving_crossref())

        assert main(check(tmp_path, str(project))) == 0

    def test_one_work_cited_from_two_surfaces_is_reported_once(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A DOI in a document and in the bibliography is one work, not two.

        Checking it twice doubles the request volume and prints the same
        finding under two headings, which reads as two problems.
        """
        bib = tmp_path / "references.bib"
        bib.write_text(BIB_MAIN, encoding="utf-8")
        page = tmp_path / "paper.qmd"
        page.write_text(f"Discussed at length: {MAIN_DOI}\n", encoding="utf-8")
        install_registries(monkeypatch, crossref=resolving_crossref())
        target = tmp_path / "audit.json"

        main(check(tmp_path, str(page), str(bib), "--format", "json", "--output", str(target)))

        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["summary"]["total"] == 1
        assert payload["results"][0]["key"] == "molinamontes2018family"

    def test_the_same_doi_under_two_citekeys_is_reported_as_a_duplicate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A paper pasted into the bibliography twice is a real, common defect.

        It is also invisible to the audit itself, which deduplicates by DOI
        before checking; the duplicate report is the only thing that can see
        it, so it has to run on the references as collected.
        """
        bib = tmp_path / "references.bib"
        bib.write_text(
            BIB_MAIN + BIB_MAIN.replace("molinamontes2018family", "molinamontes2018duplicate"),
            encoding="utf-8",
        )
        install_registries(monkeypatch, crossref=resolving_crossref())

        assert main(check(tmp_path, str(bib))) == 0
        assert "duplicate doi: 1" in capsys.readouterr().out


class TestCitekeys:
    @pytest.fixture
    def project(self, tmp_path: Path) -> tuple[Path, Path]:
        """A page citing one real key and one that resolves to nothing."""
        bib = tmp_path / "references.bib"
        bib.write_text(BIB_MAIN + BIB_SECOND, encoding="utf-8")
        page = tmp_path / "paper.qmd"
        page.write_text(
            "---\ntitle: A page\n---\n\n"
            "Established in [@molinamontes2018family], but see [@nosuchkey2020].\n",
            encoding="utf-8",
        )
        return page, bib

    def test_an_unresolved_citekey_fails_the_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        project: tuple[Path, Path], capsys: pytest.CaptureFixture[str],
    ) -> None:
        """It is a build failure waiting to happen: pandoc will drop the citation."""
        page, bib = project
        install_registries(monkeypatch, crossref=resolving_crossref())

        assert main(check(tmp_path, str(page), str(bib))) == 1
        assert "nosuchkey2020" in capsys.readouterr().out

    def test_an_unresolved_citekey_fails_the_json_run_too(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, project: tuple[Path, Path]
    ) -> None:
        """The exit code is the contract with CI; it cannot depend on the format.

        A project that switched to ``--format json`` to parse the report
        would otherwise silently stop failing on unresolved citekeys.

        The payload's ``summary.exit_code`` stays 0 here and the process
        exits 1, which is a *known gap*, not a decision worth defending: the
        JSON report has no citekey section, so it has nothing to derive the
        difference from. Asserted rather than left unstated so that whoever
        adds that section sees this line go red and closes the gap instead of
        discovering it in a CI job that parsed the field and passed. Until
        then, ``$?`` is the only complete answer for a JSON run.
        """
        page, bib = project
        install_registries(monkeypatch, crossref=resolving_crossref())
        target = tmp_path / "audit.json"

        code = main(check(tmp_path, str(page), str(bib), "--format", "json", "--output", str(target)))

        assert code == 1
        assert json.loads(target.read_text(encoding="utf-8"))["summary"]["exit_code"] == 0

    def test_the_citekey_check_can_be_turned_off(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, project: tuple[Path, Path]
    ) -> None:
        page, bib = project
        install_registries(monkeypatch, crossref=resolving_crossref())
        assert main(check(tmp_path, str(page), str(bib), "--no-citekey-check")) == 0

    def test_an_uncited_entry_is_reported_without_failing_the_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Tidying a bibliography must never be a prerequisite for a green build."""
        bib = tmp_path / "references.bib"
        bib.write_text(BIB_MAIN + BIB_SECOND, encoding="utf-8")
        page = tmp_path / "paper.qmd"
        page.write_text("Only one is cited [@molinamontes2018family].\n", encoding="utf-8")
        install_registries(monkeypatch, crossref=resolving_crossref())

        assert main(check(tmp_path, str(page), str(bib))) == 0
        assert "smith2019cohort" in capsys.readouterr().out

    def test_a_bibliography_named_with_b_resolves_the_citekeys(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, project: tuple[Path, Path]
    ) -> None:
        page, bib = project
        install_registries(monkeypatch, crossref=resolving_crossref())
        assert main(check(tmp_path, str(page), "-b", str(bib))) == 1


class TestCacheCommand:
    def test_info_reports_the_cache_it_would_use(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cache_dir = tmp_path / "cache"
        assert main(["cache", "info", "--cache-dir", str(cache_dir)]) == 0
        assert str(cache_dir) in capsys.readouterr().out

    def test_clear_empties_the_cache(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cache_dir = tmp_path / "cache"
        shard = cache_dir / "ab"
        shard.mkdir(parents=True)
        (shard / "abcdef.json").write_text("{}", encoding="utf-8")

        assert main(["cache", "clear", "--cache-dir", str(cache_dir)]) == 0

        assert list(cache_dir.rglob("*.json")) == []
        assert "cleared" in capsys.readouterr().out
