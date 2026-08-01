"""Terminal and JSON rendering, and the exit code CI reads from them.

Every test here is offline and stream-based: ``render_text`` and
``render_json`` are handed an :class:`io.StringIO`, so nothing depends on a
terminal, a locale, or a registry.

The concerns are the ones a reader of the report has to be able to rely on:
a value is shown exactly as it was stored, the statement of the tool's limits
is never dropped, a registry outage never fails a build, and the JSON report
carries at least everything the terminal one does.
"""

from __future__ import annotations

import io
import json
from dataclasses import fields as dataclass_fields

import pytest

from bibaudit.model import (
    ARTIFACT_KIND,
    FAILING_VERDICTS,
    VERDICTS,
    Issue,
    Reference,
    Result,
)
from bibaudit.report import (
    LIMITS_NOTICE,
    Summary,
    render_citekey_problems,
    render_json,
    render_text,
)

#: The verdicts that must fail a run. Spelled out here rather than imported
#: from ``model.FAILING_VERDICTS``: a test that derives its expectation from
#: the value under test cannot notice that value changing.
FAILING = ("RETRACTED", "BAD-ID", "WRONG-WORK", "FIELD-MISMATCH", "UNCONFIRMED")

#: ...and the verdicts that must not. ``UNCHECKED`` is an unreachable
#: registry and ``INCOMPLETE`` is a thin entry; a tool that breaks the build
#: for either one gets switched off, and a switched-off check protects nobody.
NON_FAILING = (
    "DISPUTED", "ADJUDICATED", "REGISTRY-ARTIFACT", "INCOMPLETE", "TITLE-DRIFT",
    "COSMETIC", "UNCHECKED", "OK",
)

#: A real Crossref defect, kept exactly as the registry returns it: a deposit
#: encoded as UTF-8 and decoded as Latin-1 somewhere in the publisher's
#: pipeline (see docs/registry-artifacts.md, "Mojibake surnames"). Do not
#: "correct" the spelling here — the broken glyph is the whole point of the
#: fixture.
MOJIBAKE = "AragonÃ©s, N"
STORED_NAME = "Aragonés, N"

#: What ``fold()`` reduces the mojibake form to. A report printing this
#: instead of the value above would hide the exact character that produced
#: the finding, which is the one thing the reader needs to see.
MOJIBAKE_FOLDED = "aragona s"

ESC = "\033"


class _Tty(io.StringIO):
    """A StringIO that claims to be a terminal.

    ``report._supports_colour`` asks the stream itself, so colour behaviour
    cannot be exercised at all against a plain StringIO. Without this, a
    "NO_COLOR produces no escapes" test would pass even if ``NO_COLOR`` were
    ignored entirely.
    """

    def isatty(self) -> bool:
        return True


#: A difference ``benign.py`` explained: the registry is wrong, reproducibly,
#: for everybody, and ``docs/registry-artifacts.md`` says why.
ARTIFACT = Issue(
    field="authors", kind=ARTIFACT_KIND, severity="info",
    stored=STORED_NAME, registry=MOJIBAKE,
    note="registry surname is mojibake (UTF-8 read as Latin-1)",
)

#: A difference *this project* decided to accept. ``suppress.apply`` stamps the
#: ``suppressed:`` prefix onto the original kind, which is what tells the two
#: apart — see ``model.is_registry_artifact``.
ADJUDICATION = Issue(
    field="publisher", kind="suppressed:mismatch", severity="info",
    stored="Oxford University Press", registry="Elsevier",
    note="imprint mergers churn these names; not tracked here",
)


def make_result(
    key: str = "entry2020",
    verdict: str = "OK",
    *,
    issues: tuple[Issue, ...] = (),
    suppressed: tuple[Issue, ...] = (),
    locator: str = "references.bib:12",
    doi: str | None = "10.1234/example",
    consulted: dict[str, str] | None = None,
) -> Result:
    """One :class:`Result`, built directly so no comparison logic is involved."""
    ref = Reference(key=key, locator=locator, kind="article", doi=doi)
    return Result(
        ref=ref,
        verdict=verdict,
        issues=list(issues),
        suppressed=list(suppressed),
        consulted=consulted  # type: ignore[arg-type]
        if consulted is not None
        else {"crossref": "answered", "datacite": "not-asked", "pubmed": "answered"},
    )


def text_of(results: list[Result], **kwargs: object) -> str:
    """The terminal report as a string, with colour off (a StringIO is no tty)."""
    buffer = io.StringIO()
    render_text(results, stream=buffer, **kwargs)  # type: ignore[arg-type]
    return buffer.getvalue()


def json_of(results: list[Result]) -> dict[str, object]:
    """The JSON report, parsed back — every caller asserts on the round trip."""
    buffer = io.StringIO()
    render_json(results, stream=buffer)
    parsed: dict[str, object] = json.loads(buffer.getvalue())
    return parsed


class TestVerdictCoverage:
    def test_every_verdict_is_classified_as_failing_or_not(self) -> None:
        """A new verdict must be an explicit decision, not a default.

        ``FAILING``/``NON_FAILING`` above are this file's own list. If a
        verdict is added to ``model.VERDICTS`` and nobody decides whether it
        breaks a build, this test says so rather than letting the new verdict
        inherit whatever ``FAILING_VERDICTS`` happens to do.
        """
        assert set(FAILING) | set(NON_FAILING) == set(VERDICTS)
        assert not set(FAILING) & set(NON_FAILING)

    @pytest.mark.parametrize("verdict", VERDICTS)
    def test_every_verdict_the_model_defines_is_printable(self, verdict: str) -> None:
        """A verdict the reporter does not know about is invisible in both sections.

        The detail groups and the summary counts are both driven by the
        reporter's own verdict order, so a verdict added to the model but not
        to the reporter would produce a report in which those references
        simply do not appear — silently, and looking like a clean run.
        """
        output = text_of([make_result(verdict=verdict)], verbose=True)
        assert verdict in output

    @pytest.mark.parametrize("verdict", FAILING)
    def test_a_build_breaking_verdict_is_shown_without_verbose(self, verdict: str) -> None:
        """A reference that fails the run must name itself in the default report.

        The set of groups printed without ``--verbose`` used to be the first six
        entries of the reporter's own verdict order. That slice was only correct
        by coincidence: inserting a verdict into ``model.VERDICTS`` above
        ``UNCONFIRMED`` would have pushed a failing verdict out of the default
        report, so ``bibaudit check`` would have exited 1 while printing no
        entry that explained why. This asserts the property the slice was
        standing in for.
        """
        output = text_of([make_result(key="breaks2020", verdict=verdict)])
        assert "breaks2020" in output

    def test_the_reporter_prints_in_the_severity_order_the_model_declares(self) -> None:
        """One ordering, not two.

        ``report`` kept its own copy of the verdict order and the two copies
        drifted: ``INCOMPLETE`` and ``REGISTRY-ARTIFACT`` ended up in opposite
        relative positions, so the module that documents severity disagreed with
        the module that shows it to a reader. The entries are fed in reverse, so
        a reporter that simply printed them in input order would fail.
        """
        results = [
            make_result(key=f"entry{i:02d}", verdict=verdict)
            for i, verdict in enumerate(reversed(VERDICTS))
        ]
        output = text_of(results, verbose=True)
        positions = [output.index(f"entry{i:02d}") for i in range(len(VERDICTS))]
        assert positions == sorted(positions, reverse=True)

    def test_incomplete_outranks_registry_artifact(self) -> None:
        """The specific pair the two orderings disagreed about.

        A gap in the entry is something the reader can act on; a documented
        registry defect is explicitly nothing to do, so it prints later.
        """
        assert VERDICTS.index("INCOMPLETE") < VERDICTS.index("REGISTRY-ARTIFACT")

    @pytest.mark.parametrize("verdict", VERDICTS)
    def test_every_verdict_group_carries_an_explanation(self, verdict: str) -> None:
        """The heading has to say what the verdict means, or it reads as jargon."""
        heading_prefix = f"{verdict}  (1)"
        heading = next(
            line for line in text_of([make_result(verdict=verdict)], verbose=True).splitlines()
            if line.startswith(heading_prefix)
        )
        assert heading[len(heading_prefix):].strip(), f"{verdict} has no help text"


class TestExitCode:
    def test_a_clean_run_exits_zero(self) -> None:
        assert Summary([make_result(verdict="OK")]).exit_code() == 0

    def test_no_references_at_all_exits_zero(self) -> None:
        assert Summary([]).exit_code() == 0

    @pytest.mark.parametrize("verdict", FAILING)
    def test_each_failing_verdict_exits_one(self, verdict: str) -> None:
        assert Summary([make_result(verdict=verdict)]).exit_code() == 1

    @pytest.mark.parametrize("verdict", NON_FAILING)
    def test_no_benign_verdict_ever_fails_a_run(self, verdict: str) -> None:
        """An outage and a thin entry are not defects in a bibliography.

        ``UNCHECKED`` in particular: making a registry outage break the build
        teaches people to pass ``--no-verify``, and a check that is routinely
        bypassed protects nobody.
        """
        assert Summary([make_result(verdict=verdict)]).exit_code() == 0

    def test_the_exit_code_is_one_not_the_number_of_failures(self) -> None:
        """CI reads 0/1/2; a count would collide with the usage-error code 2."""
        results = [make_result(key=f"bad{i}", verdict="BAD-ID") for i in range(3)]
        assert Summary(results).exit_code() == 1

    def test_fail_on_narrows_which_verdicts_break_the_build(self) -> None:
        summary = Summary([make_result(verdict="FIELD-MISMATCH")])
        assert summary.exit_code(failing_verdicts=frozenset({"RETRACTED"})) == 0
        assert summary.exit_code(failing_verdicts=frozenset({"FIELD-MISMATCH"})) == 1

    def test_an_empty_fail_on_set_never_fails(self) -> None:
        """``--fail-on ''`` is documented as "never fail"; a retraction included."""
        summary = Summary([make_result(verdict="RETRACTED")])
        assert summary.exit_code(failing_verdicts=frozenset()) == 0

    def test_narrowing_does_not_mutate_the_default(self) -> None:
        """A previous run's ``--fail-on`` must not leak into the next call."""
        summary = Summary([make_result(verdict="FIELD-MISMATCH")])
        summary.exit_code(failing_verdicts=frozenset({"RETRACTED"}))
        assert summary.exit_code() == 1


class TestLimitsNotice:
    def test_the_terminal_report_states_the_limits_even_when_everything_passes(self) -> None:
        """"All checks pass" is the moment the notice matters most.

        A reader who takes a clean report to mean the citations support the
        claims they are attached to has been misled, and a report that only
        explains itself when something is wrong invites exactly that.
        """
        assert LIMITS_NOTICE in text_of([make_result(verdict="OK")])

    def test_the_terminal_report_states_the_limits_when_something_fails(self) -> None:
        assert LIMITS_NOTICE in text_of([make_result(verdict="BAD-ID")])

    def test_the_limits_notice_survives_an_empty_run(self) -> None:
        assert LIMITS_NOTICE in text_of([])

    def test_the_json_report_carries_the_same_notice(self) -> None:
        assert json_of([make_result(verdict="OK")])["limits"] == LIMITS_NOTICE

    @pytest.mark.parametrize(
        "results",
        [
            pytest.param([], id="empty-run"),
            pytest.param([make_result(verdict="BAD-ID")], id="failing-run"),
            pytest.param([make_result(verdict="UNCHECKED")], id="unchecked-run"),
        ],
    )
    def test_the_json_notice_is_present_whatever_the_run_found(
        self, results: list[Result]
    ) -> None:
        """"Both formats, always" includes the runs nobody looks twice at.

        A notice attached only to the results list disappears from a report
        with no results — and an empty JSON report with no statement of
        limits is the one most likely to be filed as evidence that a
        bibliography was checked.
        """
        assert json_of(results)["limits"] == LIMITS_NOTICE

    def test_the_notice_says_what_is_not_checked(self) -> None:
        """The specific claim being disclaimed, not a vague hedge."""
        assert "supports the statement it is attached to" in LIMITS_NOTICE


class TestValuesAreShownVerbatim:
    def test_a_mojibake_registry_value_is_printed_with_its_broken_glyphs(self) -> None:
        """The exact character that caused the finding is the finding.

        ``fold()`` turns ``AragonÃ©s`` into ``aragona s``; a report that
        printed the folded form would show a one-letter surname mismatch on a
        correct bibliography and give the reader no way to see that the
        registry's copy is mis-decoded.
        """
        issue = Issue(
            field="authors", kind="mismatch", severity="error",
            stored=f"#3 {STORED_NAME}", registry=f"#3 {MOJIBAKE}", source="crossref",
        )
        output = text_of([make_result(verdict="FIELD-MISMATCH", issues=(issue,))])
        assert MOJIBAKE in output
        assert STORED_NAME in output
        assert MOJIBAKE_FOLDED not in output

    def test_json_round_trips_the_broken_glyphs_unchanged(self) -> None:
        issue = Issue(
            field="authors", kind="mismatch", severity="error",
            stored=STORED_NAME, registry=MOJIBAKE, source="crossref",
        )
        payload = json_of([make_result(verdict="FIELD-MISMATCH", issues=(issue,))])
        reported = payload["results"][0]["issues"][0]  # type: ignore[index]
        assert reported["registry"] == MOJIBAKE
        assert reported["stored"] == STORED_NAME

    def test_the_reporter_does_not_re_normalise_what_it_is_handed(self) -> None:
        """``Issue`` values are display strings already; the report only prints them.

        A reporter that ran ``clean()`` over its input a second time would
        flatten an en-dash to a hyphen and a curly apostrophe to a straight
        one, quietly erasing the difference the issue exists to show.
        """
        # The en dash is the fixture, not a typo: ruff's ambiguous-character
        # rule is suppressed here because a hyphen would delete the very
        # difference this test exists to prove is preserved.
        issue = Issue(
            field="pages", kind="mismatch", severity="error",
            stored="1009–1018", registry="1009-1018", source="crossref",  # noqa: RUF001
        )
        output = text_of([make_result(verdict="FIELD-MISMATCH", issues=(issue,))])
        assert "1009–1018" in output  # noqa: RUF001

    def test_the_json_carries_the_full_value_the_terminal_abbreviates(self) -> None:
        """A reviewer re-deriving a verdict works from the JSON, not the screen.

        Terminal lines are bounded so a 438-entry report stays readable; the
        machine-readable report has no such excuse, and a truncated value
        there would make the audit non-reproducible.
        """
        long_title = "A very long epidemiological title " * 10
        issue = Issue(
            field="title", kind="mismatch", severity="error",
            stored=long_title, registry=long_title + " with a differing subtitle",
        )
        payload = json_of([make_result(verdict="FIELD-MISMATCH", issues=(issue,))])
        reported = payload["results"][0]["issues"][0]  # type: ignore[index]
        assert reported["stored"] == long_title
        assert reported["registry"] == long_title + " with a differing subtitle"

    def test_an_abbreviated_terminal_value_says_that_it_was_abbreviated(self) -> None:
        """Two long values that differ late must not print as identical lines.

        Epidemiology titles routinely run past the terminal line budget. Cut
        silently, a "title/mismatch" shows the reader two lines that look the
        same and the report reads as broken rather than as informative.
        """
        long_title = "A very long epidemiological title " * 10
        issue = Issue(field="title", kind="mismatch", severity="error", stored=long_title)
        output = text_of([make_result(verdict="FIELD-MISMATCH", issues=(issue,))])
        assert long_title not in output
        assert "…" in output


class TestTerminalReport:
    def test_a_failing_entry_shows_where_to_open_it(self) -> None:
        """A finding without a file and line is a puzzle, not a report."""
        result = make_result(key="molinamontes2018family", verdict="BAD-ID", locator="references.bib:412")
        output = text_of([result])
        assert "molinamontes2018family" in output
        assert "references.bib:412" in output

    def test_a_clean_run_says_pass_and_a_failing_one_says_fail(self) -> None:
        assert "PASS" in text_of([make_result(verdict="OK")])
        assert "FAIL" in text_of([make_result(verdict="WRONG-WORK")])

    def test_worse_verdicts_are_printed_before_benign_ones(self) -> None:
        output = text_of(
            [make_result(key="fine", verdict="OK"), make_result(key="gone", verdict="RETRACTED")],
            verbose=True,
        )
        assert output.index("RETRACTED") < output.index("gone") < output.index("fine")

    def test_informational_verdicts_are_hidden_unless_asked_for(self) -> None:
        """A report nobody finishes reading is an unread report.

        Cosmetic differences are counted in the summary so nothing is hidden,
        but they do not get a per-entry block until ``--verbose``.
        """
        result = make_result(key="cosmeticonly2019", verdict="COSMETIC")
        assert "cosmeticonly2019" not in text_of([result])
        assert "COSMETIC" in text_of([result])
        assert "cosmeticonly2019" in text_of([result], verbose=True)

    def test_info_severity_issues_are_hidden_inside_a_shown_entry(self) -> None:
        error = Issue(field="volume", kind="mismatch", severity="error", stored="48", registry="47")
        noise = Issue(field="title", kind="cosmetic", severity="info", stored="A Title", registry="A title")
        output = text_of([make_result(verdict="FIELD-MISMATCH", issues=(error, noise))])
        assert "volume/mismatch" in output
        assert "title/cosmetic" not in output

    def test_the_summary_counts_only_error_severity_issues_by_field(self) -> None:
        """"errors by field" that counted warnings would overstate the damage."""
        error = Issue(field="volume", kind="mismatch", severity="error", stored="48", registry="47")
        warning = Issue(field="pages", kind="missing", severity="warning", registry="473-483")
        summary = Summary([make_result(verdict="FIELD-MISMATCH", issues=(error, warning))])
        assert dict(summary.fields) == {"volume": 1}

    def test_suppressed_differences_are_counted_even_though_they_are_not_listed(self) -> None:
        """The report must always state how much of it is being taken on trust.

        A suppression that vanished from the summary would let a
        ``.bibaudit.toml`` silence a field across a whole bibliography while
        the report still read as a clean, fully checked run.
        """
        output = text_of([make_result(verdict="ADJUDICATED", suppressed=(ADJUDICATION,))])
        assert "suppressed" in output
        assert "--show-suppressed" in output

    def test_show_suppressed_prints_the_adjudication_reason(self) -> None:
        suppressed = Issue(
            field="authors", kind="suppressed:mismatch", severity="info",
            stored=STORED_NAME, registry=MOJIBAKE,
            note="Crossref mojibake; checked against the PDF 2026-07-30",
        )
        output = text_of(
            [make_result(verdict="REGISTRY-ARTIFACT", suppressed=(suppressed,))],
            show_suppressed=True,
        )
        assert "checked against the PDF 2026-07-30" in output
        assert MOJIBAKE in output

    def test_show_suppressed_does_not_reprint_every_clean_entry(self) -> None:
        """Asking to see the adjudications is not the same as asking for --verbose."""
        suppressed = Issue(
            field="authors", kind="suppressed:mismatch", severity="info",
            stored=STORED_NAME, registry=MOJIBAKE, note="checked against the PDF",
        )
        results = [
            make_result(key="clean2020", verdict="OK"),
            make_result(key="adjudicated2019", verdict="REGISTRY-ARTIFACT", suppressed=(suppressed,)),
        ]
        output = text_of(results, show_suppressed=True)
        assert "adjudicated2019" in output
        assert "clean2020" not in output

    def test_the_report_defaults_to_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        render_text([make_result(verdict="OK")])
        assert LIMITS_NOTICE in capsys.readouterr().out

    def test_the_returned_summary_describes_the_run(self) -> None:
        results = [make_result(key="a", verdict="OK"), make_result(key="b", verdict="BAD-ID")]
        buffer = io.StringIO()
        summary = render_text(results, stream=buffer)
        assert summary.total == 2
        assert summary.ok == 1
        assert [r.ref.key for r in summary.failing] == ["b"]


class TestAdjudicationIsNotARegistryDefect:
    """The two things that used to share one verdict, and now must not.

    README defines ``REGISTRY-ARTIFACT`` as "a difference explained by a known
    registry defect". A ``.bibaudit.toml`` entry is not that: it is a person's
    say-so, it can go stale, and a reader looking at a report needs to know
    which of the two is holding an entry up. Both remain non-failing.
    """

    def test_the_two_verdicts_carry_different_explanations(self) -> None:
        artifact = text_of([make_result(verdict="REGISTRY-ARTIFACT")], verbose=True)
        adjudicated = text_of([make_result(verdict="ADJUDICATED")], verbose=True)
        artifact_help = next(
            line for line in artifact.splitlines() if line.startswith("REGISTRY-ARTIFACT")
        )
        adjudicated_help = next(
            line for line in adjudicated.splitlines() if line.startswith("ADJUDICATED")
        )
        assert "registry defect" in artifact_help
        assert ".bibaudit.toml" in adjudicated_help

    def test_neither_one_breaks_a_build(self) -> None:
        """Distinguishing them must not have made one of them fail CI.

        The whole reason both are tolerated is that both were deliberately
        accepted; turning ``ADJUDICATED`` into a failure would mean writing a
        suppression made the run worse than not writing one.
        """
        results = [
            make_result(key="a", verdict="ADJUDICATED", suppressed=(ADJUDICATION,)),
            make_result(key="b", verdict="REGISTRY-ARTIFACT", suppressed=(ARTIFACT,)),
        ]
        assert Summary(results).exit_code() == 0

    def test_an_adjudication_is_the_more_prominent_of_the_two(self) -> None:
        """Printed earlier, because it is the one that can rot.

        A documented registry defect is settled for everybody. An adjudication
        stops applying the moment a citekey is renamed, and the report looks
        identical to the day it was written.
        """
        assert VERDICTS.index("ADJUDICATED") < VERDICTS.index("REGISTRY-ARTIFACT")

    def test_the_summary_says_how_many_of_each(self) -> None:
        """One "suppressed: 40" line let local decisions hide inside defects."""
        results = [
            make_result(key="a", verdict="ADJUDICATED", suppressed=(ADJUDICATION,)),
            make_result(key="b", verdict="REGISTRY-ARTIFACT", suppressed=(ARTIFACT, ARTIFACT)),
        ]
        summary = Summary(results)
        assert (summary.suppressed, summary.artifacts, summary.adjudicated) == (3, 2, 1)

        output = text_of(results)
        assert "2 registry defect(s)" in output
        assert "1 adjudicated here" in output

    def test_the_json_summary_carries_the_split(self) -> None:
        """A consumer auditing how much is taken on trust needs both numbers."""
        results = [
            make_result(key="a", verdict="ADJUDICATED", suppressed=(ADJUDICATION,)),
            make_result(key="b", verdict="REGISTRY-ARTIFACT", suppressed=(ARTIFACT,)),
        ]
        summary = json_of(results)["summary"]  # type: ignore[index]
        assert summary["suppressed"] == 2
        assert summary["registry_artifacts"] == 1
        assert summary["adjudicated"] == 1

    def test_show_suppressed_labels_which_kind_each_one_is(self) -> None:
        """Reading the listing must not require knowing the verdict it sits under.

        An entry can carry both — a Crossref mojibake surname the tool
        recognises *and* a publisher difference the project waved through — and
        printed as an undifferentiated list the reader cannot tell which line
        was justified by evidence and which by a decision.
        """
        result = make_result(verdict="ADJUDICATED", suppressed=(ARTIFACT, ADJUDICATION))
        output = text_of([result], show_suppressed=True)
        assert f"authors/{ARTIFACT_KIND}" in output
        assert "publisher/suppressed:mismatch" in output

    def test_a_result_can_report_its_own_two_lists_apart(self) -> None:
        result = make_result(verdict="ADJUDICATED", suppressed=(ARTIFACT, ADJUDICATION))
        assert [i.field for i in result.artifacts] == ["authors"]
        assert [i.field for i in result.adjudicated] == ["publisher"]


class TestBannerAndExitCodeAgree:
    """The banner and ``$?`` must be answering the same question.

    ``bibaudit check --fail-on ''`` printed ``FAIL — 1 reference(s) need
    attention`` and exited 0. Two policies, both unlabelled: a reader watching
    the terminal and a CI job reading ``$?`` came to opposite conclusions about
    the same run, and neither could tell which was authoritative.
    """

    def test_an_empty_failing_set_does_not_print_fail(self) -> None:
        output = text_of([make_result(verdict="RETRACTED")], failing_verdicts=frozenset())
        assert "PASS" in output
        assert "FAIL" not in output

    def test_but_the_excluded_reference_is_still_named_in_the_banner(self) -> None:
        """PASS must not read as "nothing to see here".

        This is the pairing for the relaxation above: narrowing ``--fail-on``
        is allowed to change the exit code and *only* the exit code. A
        retracted citation that no longer breaks the build still has to be
        visible, or ``--fail-on ''`` becomes a way to make a report look clean.
        """
        output = text_of([make_result(verdict="RETRACTED")], failing_verdicts=frozenset())
        assert "1 reference(s) need attention but are outside the failing set" in output
        # ...and the entry itself is still listed, with its verdict.
        assert "RETRACTED" in output
        assert "entry2020" in output

    def test_the_non_default_policy_is_printed_so_it_cannot_be_guessed_at(self) -> None:
        output = text_of(
            [make_result(verdict="RETRACTED")],
            failing_verdicts=frozenset({"BAD-ID"}),
        )
        assert "failing set (--fail-on): BAD-ID" in output

    def test_an_empty_policy_says_so_in_words(self) -> None:
        """``failing set: `` with nothing after it reads like a rendering bug."""
        output = text_of([make_result(verdict="OK")], failing_verdicts=frozenset())
        assert "nothing fails this run" in output

    def test_a_narrowed_policy_still_fails_on_what_it_names(self) -> None:
        """The true positive: narrowing is not switching the check off."""
        output = text_of(
            [make_result(verdict="RETRACTED")],
            failing_verdicts=frozenset({"RETRACTED"}),
        )
        assert "FAIL — 1 reference(s) in the failing set" in output

    def test_the_default_run_prints_no_policy_line(self) -> None:
        """Noise on every ordinary run is how a banner stops being read."""
        output = text_of([make_result(verdict="OK")])
        assert "--fail-on" not in output

    def test_the_banner_and_the_exit_code_never_disagree(self) -> None:
        """The property the whole class exists for, over every verdict."""
        for verdict in VERDICTS:
            for policy in (FAILING_VERDICTS, frozenset(), frozenset({verdict})):
                results = [make_result(verdict=verdict)]
                output = text_of(results, failing_verdicts=policy)
                code = Summary(results).exit_code(failing_verdicts=policy)
                said_fail = "FAIL — " in output
                assert said_fail == (code == 1), (verdict, sorted(policy))


class TestRetractionCouldNotBeCorroborated:
    """A PASS during a registry outage must not read cleaner than the evidence.

    ``compare`` raises ``status/retraction-unverified`` at ``info`` severity —
    correctly, because an outage is not a defect in anybody's bibliography and
    promoting it would relabel every correct entry in the file. But ``info`` is
    filtered out of the default terminal report, so an NCBI outage printed
    ``PASS — no reference in the failing set`` with nothing anywhere to say that
    the one separately-curated retraction source had not been asked.

    This is the field where silence costs the most: an unread retraction goes
    into a manuscript and there is no remedy afterwards.
    """

    @staticmethod
    def _unverified(source: str = "pubmed") -> Issue:
        return Issue(
            field="status",
            kind="retraction-unverified",
            severity="info",
            source=source,
            note="retraction status not corroborated",
        )

    def test_the_outage_is_stated_beside_the_pass_banner(self) -> None:
        output = text_of(
            [make_result(issues=(self._unverified(),)) for _ in range(3)]
        )

        assert "PASS" in output
        assert "retraction status not corroborated for 3 reference(s)" in output
        assert "pubmed unreachable" in output

    def test_it_is_printed_once_for_the_run_not_once_per_reference(self) -> None:
        """438 identical lines are not a warning, they are wallpaper."""
        output = text_of(
            [make_result(key=f"e{n}", issues=(self._unverified(),)) for n in range(438)]
        )

        assert output.count("retraction status not corroborated") == 1

    def test_every_unreachable_registry_is_named(self) -> None:
        output = text_of([make_result(issues=(self._unverified("crossref,pubmed"),))])

        assert "crossref, pubmed unreachable" in output

    def test_a_run_with_no_outage_says_nothing(self) -> None:
        """The true positive for the noise half: silence when there is no gap.

        A line on every ordinary run is how a banner stops being read, and then
        the run where it matters is skipped with the rest.
        """
        output = text_of([make_result(verdict="OK")])

        assert "retraction status" not in output

    def test_it_does_not_touch_the_exit_code(self) -> None:
        """An outage is ignorance, never a finding — the 404-is-a-fact rule."""
        results = [make_result(issues=(self._unverified(),))]
        text_of(results)

        assert Summary(results).exit_code() == 0


class TestJsonReport:
    def test_the_output_is_valid_json(self) -> None:
        payload = json_of([make_result(verdict="OK")])
        assert payload["tool"] == "bibaudit"

    def test_every_field_of_an_issue_survives_the_round_trip(self) -> None:
        """A hand-rolled subset of ``Issue`` would silently drop a field.

        ``note`` and ``source`` are the ones that carry *why* a difference was
        reported and *who* said so; without them a JSON consumer cannot
        re-derive the verdict, which is what makes the audit reproducible.
        """
        issue = Issue(
            field="year", kind="mismatch", severity="error", stored="2015",
            registry="print=2018", source="crossref", note="similarity 0.42",
        )
        payload = json_of([make_result(verdict="FIELD-MISMATCH", issues=(issue,))])
        reported = payload["results"][0]["issues"][0]  # type: ignore[index]
        assert {f.name for f in dataclass_fields(Issue)} <= set(reported)
        assert reported["note"] == "similarity 0.42"
        assert reported["source"] == "crossref"

    def test_it_carries_everything_the_terminal_report_shows(self) -> None:
        issue = Issue(
            field="volume", kind="mismatch", severity="error",
            stored="48", registry="47", source="crossref",
        )
        result = make_result(key="molinamontes2018family", verdict="FIELD-MISMATCH", issues=(issue,))
        terminal = text_of([result])
        entry = json_of([result])["results"][0]  # type: ignore[index]
        for shown in (result.ref.key, result.ref.locator, result.verdict, "volume", "48", "47"):
            assert shown in terminal
            assert shown in json.dumps(entry)

    def test_it_records_which_registries_answered(self) -> None:
        """Without ``consulted``, UNCHECKED is indistinguishable from "not found".

        A machine consumer needs to know that silence came from an outage
        rather than from a registry that answered and had nothing.
        """
        result = make_result(
            verdict="UNCHECKED",
            consulted={"crossref": "unreachable", "pubmed": "unreachable"},
        )
        entry = json_of([result])["results"][0]  # type: ignore[index]
        assert entry["consulted"] == {"crossref": "unreachable", "pubmed": "unreachable"}
        assert entry["fails"] is False

    def test_a_registry_that_was_never_queried_says_so(self) -> None:
        """``not-asked`` is a third state, and JSON has to carry it verbatim.

        The field is the machine-readable record of what evidence a verdict
        rests on. A bool could say only "unreachable or not", so a run with
        ``--no-corroborate`` published ``"pubmed": true`` on every reference —
        claiming a curated second opinion nobody had asked for. A consumer
        must be able to see the difference without knowing which flags the run
        was given.
        """
        result = make_result(
            consulted={"crossref": "answered", "datacite": "not-asked", "pubmed": "not-asked"},
        )
        entry = json_of([result])["results"][0]  # type: ignore[index]
        assert entry["consulted"]["pubmed"] == "not-asked"
        assert entry["consulted"]["crossref"] == "answered"
        # The distinction that matters: "we did not ask" must never render as
        # anything a reader could mistake for "we asked and it was fine".
        assert entry["consulted"]["datacite"] != entry["consulted"]["crossref"]

    def test_suppressed_issues_are_reported_separately_not_dropped(self) -> None:
        suppressed = Issue(
            field="publisher", kind="suppressed:mismatch", severity="info",
            note="imprint mergers; not tracked here",
        )
        payload = json_of([make_result(verdict="REGISTRY-ARTIFACT", suppressed=(suppressed,))])
        assert payload["summary"]["suppressed"] == 1  # type: ignore[index]
        assert payload["results"][0]["suppressed"][0]["field"] == "publisher"  # type: ignore[index]

    def test_the_summary_block_agrees_with_the_terminal_summary(self) -> None:
        error = Issue(field="volume", kind="mismatch", severity="error", stored="48", registry="47")
        results = [
            make_result(key="a", verdict="OK"),
            make_result(key="b", verdict="BAD-ID"),
            make_result(key="c", verdict="UNCHECKED"),
            make_result(key="d", verdict="FIELD-MISMATCH", issues=(error,)),
        ]
        payload = json_of(results)
        summary = payload["summary"]  # type: ignore[index]
        assert summary["total"] == 4
        assert summary["verdicts"] == {
            "OK": 1, "BAD-ID": 1, "UNCHECKED": 1, "FIELD-MISMATCH": 1
        }
        # The terminal summary prints an "errors by field" line; a JSON
        # consumer that could not reproduce it would have to re-derive the
        # counts from the issue list and guess which severities were counted.
        assert summary["errors_by_field"] == {"volume": 1}
        assert summary["exit_code"] == 1

    def test_the_summary_exit_code_follows_a_narrowed_failing_set(self) -> None:
        """``--fail-on`` has to reach the field that is named after ``$?``.

        Left on the default set, ``--fail-on '' --format json`` over a
        retracted citation reported ``"exit_code": 1`` in a run whose process
        exited 0, and a CI job gating on the field rather than on ``$?``
        failed a build the tool had deliberately passed.
        """
        buffer = io.StringIO()
        render_json(
            [make_result(verdict="RETRACTED")],
            stream=buffer,
            failing_verdicts=frozenset(),
        )
        assert json.loads(buffer.getvalue())["summary"]["exit_code"] == 0

        buffer = io.StringIO()
        render_json(
            [make_result(verdict="RETRACTED")],
            stream=buffer,
            failing_verdicts=frozenset({"RETRACTED"}),
        )
        assert json.loads(buffer.getvalue())["summary"]["exit_code"] == 1

    def test_a_run_with_no_references_is_still_valid_json(self) -> None:
        payload = json_of([])
        assert payload["results"] == []
        assert payload["summary"]["exit_code"] == 0  # type: ignore[index]

    def test_json_goes_to_stdout_by_default(self, capsys: pytest.CaptureFixture[str]) -> None:
        render_json([make_result(verdict="OK")])
        assert json.loads(capsys.readouterr().out)["tool"] == "bibaudit"


class TestColour:
    """``NO_COLOR`` is honoured, and a redirected stream never gets escapes."""

    @pytest.fixture(autouse=True)
    def _plain_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("TERM", "xterm-256color")

    def test_a_terminal_gets_colour(self) -> None:
        """Anchors the tests below: without this they could pass vacuously."""
        stream = _Tty()
        render_text([make_result(verdict="BAD-ID")], stream=stream)
        assert ESC in stream.getvalue()

    def test_no_color_disables_every_escape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NO_COLOR", "1")
        stream = _Tty()
        render_text([make_result(verdict="BAD-ID")], stream=stream)
        assert ESC not in stream.getvalue()

    def test_no_color_set_to_the_empty_string_still_disables_colour(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The convention is presence, not truthiness.

        ``NO_COLOR=`` is how the variable is commonly exported, and a
        truthiness test on ``os.environ.get`` would ignore it — colouring the
        output of the very users who asked for none.
        """
        monkeypatch.setenv("NO_COLOR", "")
        stream = _Tty()
        render_text([make_result(verdict="BAD-ID")], stream=stream)
        assert ESC not in stream.getvalue()

    def test_a_dumb_terminal_gets_no_escapes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TERM", "dumb")
        stream = _Tty()
        render_text([make_result(verdict="BAD-ID")], stream=stream)
        assert ESC not in stream.getvalue()

    def test_a_redirected_stream_gets_no_escapes(self) -> None:
        """``--output report.txt`` and a CI log are the normal case."""
        assert ESC not in text_of([make_result(verdict="BAD-ID")])

    def test_the_citekey_report_honours_no_color_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Colour is decided in one place, or it is decided wrongly in one place."""
        monkeypatch.setenv("NO_COLOR", "1")
        stream = _Tty()
        render_citekey_problems({"missingkey": ["paper.qmd:3"]}, ["unusedkey"], stream=stream)
        assert ESC not in stream.getvalue()


class TestCitekeyProblems:
    def test_an_unresolved_citekey_is_counted_as_a_failure(self) -> None:
        stream = io.StringIO()
        count = render_citekey_problems({"nosuchkey": ["paper.qmd:3"]}, [], stream=stream)
        assert count == 1
        assert "nosuchkey" in stream.getvalue()
        assert "paper.qmd:3" in stream.getvalue()

    def test_an_uncited_entry_is_housekeeping_and_not_a_failure(self) -> None:
        """A bibliography entry nobody cites cannot break anyone's build.

        Returning it as a failure would make tidying the bibliography a
        prerequisite for a green CI run, which is how a citation check gets
        disabled.
        """
        stream = io.StringIO()
        count = render_citekey_problems({}, ["unusedentry2001"], stream=stream)
        assert count == 0
        assert "unusedentry2001" in stream.getvalue()

    def test_a_clean_project_prints_nothing_at_all(self) -> None:
        stream = io.StringIO()
        assert render_citekey_problems({}, [], stream=stream) == 0
        assert stream.getvalue() == ""

    def test_a_long_locator_list_is_capped_but_the_rest_is_counted(self) -> None:
        """Truncating without saying so would understate how widespread it is."""
        locators = [f"paper.qmd:{n}" for n in range(1, 8)]
        stream = io.StringIO()
        render_citekey_problems({"nosuchkey": locators}, [], stream=stream)
        output = stream.getvalue()
        assert "paper.qmd:1" in output
        assert "paper.qmd:7" not in output
        assert "2 more" in output
