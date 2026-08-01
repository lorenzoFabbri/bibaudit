"""Project-local adjudications in ``.bibaudit.toml``.

A suppression is the one place where the tool stops checking something because
a human said so. Everything here defends the properties that keep that from
becoming a way to hide problems:

* an unexplained suppression is refused, because a suppression nobody explained
  is a silently deleted finding;
* a suppressed difference is *moved*, never dropped, and the verdict is
  re-derived from what is left rather than relabelled;
* a rule that silences nothing is visible, because a renamed citekey turns a
  live adjudication into a no-op that looks exactly like a live one;
* a broken config produces a sentence, not a traceback — the config is the
  user's, and a traceback reads as "the tool is broken".

Nothing here touches the network: ``audit`` is exercised with its resolver
stubbed out, so the wiring between ``apply`` and ``verdict_for`` is tested
without a registry.
"""

from __future__ import annotations

import importlib
import os.path
from pathlib import Path

import pytest

from bibaudit.audit import AuditOptions, audit
from bibaudit.compare import compare, verdict_for
from bibaudit.model import Issue, Name, Record, Reference, Result
from bibaudit.suppress import (
    CONFIG_NAME,
    Suppression,
    SuppressionError,
    Suppressions,
    load_suppressions,
)

TITLE = "Shift work and colorectal cancer risk in the MCC-Spain case-control study"


def make_ref(**overrides: object) -> Reference:
    base: dict[str, object] = {
        "key": "papantoniou2017colorectal",
        "locator": "references.bib:1",
        "kind": "article",
        "doi": "10.1093/aje/kwx137",
        "title": TITLE,
        "authors": [Name(family="Papantoniou", given="Kyriaki")],
        "year": 2017,
        "container": "American Journal of Epidemiology",
        "volume": "185",
        "pages": "1211-1221",
    }
    base.update(overrides)
    return Reference(**base)  # type: ignore[arg-type]


def make_record(**overrides: object) -> Record:
    base: dict[str, object] = {
        "source": "crossref",
        "doi": "10.1093/aje/kwx137",
        "title": TITLE,
        "authors": [Name(family="Papantoniou", given="Kyriaki")],
        "years": {"print": 2017},
        "container": "American Journal of Epidemiology",
        "volume": "185",
        "pages": "1211-1221",
        "kind": "journal-article",
    }
    base.update(overrides)
    return Record(**base)  # type: ignore[arg-type]


def make_result(*issues: Issue, key: str = "papantoniou2017colorectal") -> Result:
    return Result(ref=make_ref(key=key), verdict="FIELD-MISMATCH", issues=list(issues))


def mismatch(field: str = "volume", kind: str = "mismatch", note: str = "") -> Issue:
    return Issue(
        field=field, kind=kind, severity="error",
        stored="185", registry="186", source="crossref", note=note,
    )


def write_config(directory: Path, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / CONFIG_NAME
    path.write_text(body, encoding="utf-8")
    return path


def run_audit(
    refs: list[Reference],
    records: dict[str, dict[str, Record]],
    suppressions: Suppressions,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> list[Result]:
    """Run the real ``audit`` pipeline with the registry lookup stubbed out.

    The point of going through ``audit`` rather than calling ``apply`` directly
    is that the re-derivation of the verdict lives there; a test that skips it
    would not notice if that wiring were removed.
    """
    # Fetched through importlib because ``bibaudit.audit`` as an *attribute* of
    # the package is the re-exported function of the same name, not the module,
    # which is also why monkeypatch's dotted-string form cannot find it.
    audit_module = importlib.import_module("bibaudit.audit")
    monkeypatch.setattr(audit_module, "resolve", lambda refs, registries: (records, set()))
    options = AuditOptions(
        cache_dir=tmp_path / "cache",
        corroborate=False,
        search_unidentified=False,
        suppressions=suppressions,
    )
    return audit(refs, options)


class TestReasonIsRequired:
    def test_a_suppression_without_a_reason_is_refused(self, tmp_path: Path) -> None:
        """An unexplained suppression is a finding deleted by someone unknown."""
        write_config(tmp_path, '[[ignore]]\nkey = "smith2020"\nfield = "volume"\n')
        with pytest.raises(SuppressionError) as exc:
            load_suppressions(tmp_path)
        assert "reason" in str(exc.value)

    def test_a_blank_reason_is_refused(self, tmp_path: Path) -> None:
        """Whitespace is not an explanation, and it is the obvious way round the rule."""
        write_config(tmp_path, '[[ignore]]\nkey = "smith2020"\nreason = "   "\n')
        with pytest.raises(SuppressionError):
            load_suppressions(tmp_path)

    def test_the_refusal_names_the_file_and_the_entry(self, tmp_path: Path) -> None:
        config = write_config(
            tmp_path,
            '[[ignore]]\nkey = "a"\nreason = "checked against the PDF"\n'
            '[[ignore]]\nkey = "b"\nfield = "pages"\n',
        )
        with pytest.raises(SuppressionError) as exc:
            load_suppressions(tmp_path)
        assert str(config) in str(exc.value)
        assert "#2" in str(exc.value)

    def test_one_unexplained_entry_voids_the_whole_file(self, tmp_path: Path) -> None:
        """Loading the good rules and dropping the bad one would hide the mistake.

        The run must stop and say so; a partially loaded suppression file is a
        bibliography checked under rules nobody wrote down.
        """
        write_config(
            tmp_path,
            '[[ignore]]\nkey = "a"\nreason = "checked against the PDF"\n[[ignore]]\nkey = "b"\n',
        )
        with pytest.raises(SuppressionError):
            load_suppressions(tmp_path)


class TestParsedFields:
    """What ``.bibaudit.toml`` says has to be what the loaded rule holds.

    Every other test in this file builds :class:`Suppression` objects in Python,
    so nothing else notices if ``_parse`` drops a field on the floor. Dropping
    ``field`` is the dangerous one: the constructor default is ``"*"``, so a rule
    the user wrote for one field silently silences the whole entry — the exact
    failure the unknown-key check elsewhere in this module exists to prevent.
    """

    def test_every_field_of_a_rule_survives_the_round_trip(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            '[[ignore]]\n'
            'key    = "papantoniou2017colorectal"\n'
            'field  = "authors"\n'
            'kind   = "mismatch"\n'
            'reason = "Crossref returns mojibake surnames; checked against the PDF"\n',
        )
        rule = load_suppressions(tmp_path).rules[0]
        assert (rule.key, rule.field, rule.kind) == (
            "papantoniou2017colorectal", "authors", "mismatch"
        )
        assert rule.reason == "Crossref returns mojibake surnames; checked against the PDF"

    def test_a_narrowed_rule_from_the_file_really_narrows(self, tmp_path: Path) -> None:
        """The round trip is only worth anything if the loaded rule behaves.

        ``field`` and ``kind`` are read straight into globs, so a rule that
        parsed correctly but was applied with the wrong attribute would still
        show the right values on the dataclass.
        """
        write_config(
            tmp_path,
            '[[ignore]]\nkey = "*"\nfield = "pages"\nkind = "mismatch"\n'
            'reason = "checked the PDF"\n',
        )
        suppressions = load_suppressions(tmp_path)

        wrong_field = make_result(mismatch("volume"))
        assert suppressions.apply(wrong_field) == 0

        wrong_kind = make_result(mismatch("pages", kind="missing"))
        assert suppressions.apply(wrong_kind) == 0

        covered = make_result(mismatch("pages", kind="mismatch"))
        assert suppressions.apply(covered) == 1

    def test_an_omitted_key_or_field_defaults_to_everything(self, tmp_path: Path) -> None:
        """Documented in the module docstring, and relied on by `key = "*"` rules."""
        write_config(tmp_path, '[[ignore]]\nreason = "publisher names churn"\n')
        rule = load_suppressions(tmp_path).rules[0]
        assert (rule.key, rule.field, rule.kind) == ("*", "*", "*")


class TestGlobMatching:
    def test_an_exact_rule_matches_only_its_own_entry_and_field(self) -> None:
        rule = Suppression(key="papantoniou2017colorectal", field="volume", reason="checked")
        assert rule.matches("papantoniou2017colorectal", mismatch("volume"))
        assert not rule.matches("clavelchapelon1997e3n", mismatch("volume"))
        assert not rule.matches("papantoniou2017colorectal", mismatch("pages"))

    def test_a_field_wildcard_silences_one_entry_entirely(self) -> None:
        rule = Suppression(key="papantoniou2017colorectal", field="*", reason="checked")
        assert rule.matches("papantoniou2017colorectal", mismatch("volume"))
        assert rule.matches("papantoniou2017colorectal", mismatch("pages"))
        assert not rule.matches("other2020key", mismatch("volume"))

    def test_a_key_wildcard_silences_one_field_across_the_bibliography(self) -> None:
        rule = Suppression(key="*", field="publisher", reason="imprint mergers")
        assert rule.matches("anything2020", mismatch("publisher"))
        assert not rule.matches("anything2020", mismatch("volume"))

    def test_a_key_glob_matches_a_prefix(self) -> None:
        rule = Suppression(key="epic*", field="volume", reason="checked")
        assert rule.matches("epic2019diet", mismatch("volume"))
        assert not rule.matches("mccspain2017", mismatch("volume"))

    def test_kind_narrows_a_rule_to_one_sort_of_difference(self) -> None:
        """`field = "pages"` alone would also silence a *missing* pages field.

        Adjudicating "the registry's page range is wrong" is not the same as
        accepting that the entry has no pages at all.
        """
        rule = Suppression(key="*", field="pages", kind="mismatch", reason="checked the PDF")
        assert rule.matches("any2020", mismatch("pages", kind="mismatch"))
        assert not rule.matches("any2020", mismatch("pages", kind="missing"))

    def test_kind_defaults_to_everything(self) -> None:
        rule = Suppression(key="*", field="pages", reason="checked the PDF")
        assert rule.matches("any2020", mismatch("pages", kind="missing"))

    def test_matching_is_case_sensitive_on_every_platform(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Citekeys are case-sensitive, and a verdict must not depend on the OS.

        ``fnmatch.fnmatch`` folds case through ``os.path.normcase``, which is a
        no-op on POSIX and ``str.lower`` on Windows — so a rule written for
        `Smith2020` would silence `smith2020` on one machine and not the other,
        and the two would disagree about whether the bibliography passes. The
        patch below is what makes that difference visible on a Mac: it puts
        ``normcase`` into its Windows behaviour.
        """
        monkeypatch.setattr(os.path, "normcase", str.lower)
        rule = Suppression(key="Smith2020", field="volume", reason="checked")
        assert not rule.matches("smith2020", mismatch("volume"))


class TestApply:
    def test_a_matching_issue_is_moved_not_deleted(self) -> None:
        result = make_result(mismatch("volume"))
        suppressions = Suppressions([Suppression(key="*", field="volume", reason="checked the PDF")])

        assert suppressions.apply(result) == 1
        assert result.issues == []
        assert len(result.suppressed) == 1
        assert result.errors == []

    def test_the_moved_issue_records_who_excused_it_and_what_it_said(self) -> None:
        """The report has to be able to state how much is being taken on trust."""
        result = make_result(mismatch("volume", note="similarity 0.60"))
        suppressions = Suppressions(
            [Suppression(key="*", field="volume", reason="Crossref has the wrong volume")]
        )
        suppressions.apply(result)

        issue = result.suppressed[0]
        assert issue.severity == "info"
        assert issue.kind == "suppressed:mismatch"
        assert "Crossref has the wrong volume" in issue.note
        assert "similarity 0.60" in issue.note
        assert (issue.stored, issue.registry) == ("185", "186")

    def test_an_unmatched_issue_is_left_exactly_as_it_was(self) -> None:
        result = make_result(mismatch("volume"), mismatch("pages"))
        suppressions = Suppressions([Suppression(key="*", field="volume", reason="checked")])

        assert suppressions.apply(result) == 1
        assert [i.field for i in result.issues] == ["pages"]
        assert result.issues[0].severity == "error"

    def test_an_empty_rule_set_changes_nothing(self) -> None:
        result = make_result(mismatch("volume"))
        assert Suppressions([]).apply(result) == 0
        assert len(result.issues) == 1

    def test_a_rule_for_another_entry_does_not_reach_this_one(self) -> None:
        result = make_result(mismatch("volume"), key="papantoniou2017colorectal")
        suppressions = Suppressions(
            [Suppression(key="clavelchapelon1997e3n", field="volume", reason="checked")]
        )
        assert suppressions.apply(result) == 0
        assert result.issues[0].severity == "error"


class TestUnusedRules:
    def test_a_rule_that_silences_nothing_is_reported(self) -> None:
        """A renamed citekey turns a live adjudication into a silent no-op.

        Nothing else in the run distinguishes "this difference was adjudicated"
        from "this rule has not matched anything since 2024".
        """
        suppressions = Suppressions(
            [
                Suppression(key="papantoniou2017colorectal", field="volume", reason="checked"),
                Suppression(key="renamed2017entry", field="pages", reason="checked"),
            ]
        )
        suppressions.apply(make_result(mismatch("volume")))

        assert [r.key for r in suppressions.unused] == ["renamed2017entry"]

    def test_a_rule_is_only_used_once_it_actually_silences_something(self) -> None:
        suppressions = Suppressions([Suppression(key="*", field="publisher", reason="churn")])
        suppressions.apply(make_result(mismatch("volume")))
        assert len(suppressions.unused) == 1

    def test_use_accumulates_across_entries(self) -> None:
        """A rule is unused for the *run*, not for one reference."""
        suppressions = Suppressions([Suppression(key="second2020", field="volume", reason="ok")])
        suppressions.apply(make_result(mismatch("volume"), key="first2020"))
        assert suppressions.unused
        suppressions.apply(make_result(mismatch("volume"), key="second2020"))
        assert not suppressions.unused


class TestVerdictIsReDerived:
    def test_apply_does_not_touch_the_verdict_itself(self) -> None:
        """Relabelling in place is the mistake this guards against.

        ``apply`` removes evidence; only the caller decides that a verdict may
        be recomputed, and it recomputes it with the same rule that produced it.
        """
        result = make_result(mismatch("volume"))
        Suppressions([Suppression(key="*", field="volume", reason="checked")]).apply(result)
        assert result.verdict == "FIELD-MISMATCH"

    def test_the_verdict_is_recomputed_from_what_is_left(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = make_ref(volume="186")
        records = {"10.1093/aje/kwx137": {"crossref": make_record()}}
        suppressions = Suppressions(
            [Suppression(key="papantoniou2017colorectal", field="volume", reason="checked the PDF")]
        )

        assert compare(ref, records["10.1093/aje/kwx137"]).verdict == "FIELD-MISMATCH"
        result = run_audit([ref], records, suppressions, tmp_path, monkeypatch)[0]

        assert result.verdict != "FIELD-MISMATCH"
        assert not result.fails
        assert len(result.suppressed) == 1

    def test_a_suppression_cannot_upgrade_a_verdict_it_does_not_cover(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Silencing the volume must not silence the title mismatch beside it."""
        ref = make_ref(volume="186", title="An entirely unrelated paper about marine biology")
        records = {"10.1093/aje/kwx137": {"crossref": make_record()}}
        suppressions = Suppressions(
            [Suppression(key="*", field="volume", reason="checked the PDF")]
        )

        result = run_audit([ref], records, suppressions, tmp_path, monkeypatch)[0]

        assert result.fails
        assert any(i.field == "title" and i.severity == "error" for i in result.issues)

    def test_a_suppression_cannot_clear_a_retraction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No project-local adjudication makes a retracted paper safe to cite.

        `field = "*"` is the broadest rule the format allows, and it still must
        not turn RETRACTED into anything else.
        """
        ref = make_ref()
        records = {
            "10.1093/aje/kwx137": {
                "crossref": make_record(retracted=True, retraction_kind="retraction")
            }
        }
        suppressions = Suppressions([Suppression(key="*", field="*", reason="disputed retraction")])

        result = run_audit([ref], records, suppressions, tmp_path, monkeypatch)[0]

        assert result.verdict == "RETRACTED"
        assert result.fails

    def test_a_suppressed_entry_never_reads_as_ok(self) -> None:
        """The reader must be able to see that something was taken on trust."""
        result = make_result(mismatch("volume"))
        Suppressions([Suppression(key="*", field="volume", reason="checked")]).apply(result)
        assert verdict_for(result.issues, result.suppressed) != "OK"
        # The verdict alone cannot say *who* excused the difference — the same
        # one is used for documented registry defects — so the issue has to
        # carry the distinction. Without the marker a reader cannot tell a
        # project-local adjudication from a defect in Crossref's own data.
        assert result.suppressed[0].kind.startswith("suppressed:")
        assert result.suppressed[0].kind != "registry-artifact"

    def test_a_rule_for_one_field_cannot_clear_an_unresolvable_identifier(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BAD-ID is not a field difference, and a field rule must leave it alone.

        The DOI resolving in no registry is the finding the whole tool exists
        for. A `field = "volume"` adjudication that reached it would clear the
        entry on the strength of an unrelated sentence in the config.
        """
        ref = make_ref()
        suppressions = Suppressions(
            [Suppression(key="*", field="volume", reason="checked the PDF")]
        )

        result = run_audit([ref], {"10.1093/aje/kwx137": {}}, suppressions, tmp_path, monkeypatch)[0]

        assert result.verdict == "BAD-ID"
        assert result.fails
        assert result.suppressed == []

    def test_the_suppressed_difference_is_still_counted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = make_ref(volume="186")
        records = {"10.1093/aje/kwx137": {"crossref": make_record()}}
        suppressions = Suppressions([Suppression(key="*", field="volume", reason="checked")])

        result = run_audit([ref], records, suppressions, tmp_path, monkeypatch)[0]

        assert [i.field for i in result.suppressed] == ["volume"]
        assert "checked" in result.suppressed[0].note


class TestDiscovery:
    def test_the_config_beside_the_bibliography_is_used(self, tmp_path: Path) -> None:
        write_config(tmp_path, '[[ignore]]\nkey = "a"\nreason = "checked"\n')
        suppressions = load_suppressions(tmp_path / "references.bib")
        assert [r.key for r in suppressions.rules] == ["a"]
        assert suppressions.source == tmp_path / CONFIG_NAME

    def test_a_directory_works_as_well_as_a_file(self, tmp_path: Path) -> None:
        write_config(tmp_path, '[[ignore]]\nkey = "a"\nreason = "checked"\n')
        assert load_suppressions(tmp_path).rules

    def test_the_search_walks_up_to_the_project_root(self, tmp_path: Path) -> None:
        write_config(tmp_path, '[[ignore]]\nkey = "a"\nreason = "checked"\n')
        (tmp_path / "sources").mkdir()
        assert load_suppressions(tmp_path / "sources" / "epic.qmd").rules

    def test_a_config_beside_a_git_directory_is_still_found(self, tmp_path: Path) -> None:
        """The repository root is exactly where the file normally sits.

        The `.git` stop has to be checked *after* the config in the same
        directory, or the common case never loads at all.
        """
        write_config(tmp_path, '[[ignore]]\nkey = "a"\nreason = "checked"\n')
        (tmp_path / ".git").mkdir()
        assert load_suppressions(tmp_path / "references.bib").rules

    def test_the_search_stops_at_a_git_directory(self, tmp_path: Path) -> None:
        """An unrelated parent project must not silence findings in this one."""
        write_config(tmp_path, '[[ignore]]\nkey = "*"\nfield = "*"\nreason = "someone else\'s"\n')
        project = tmp_path / "project"
        project.mkdir()
        (project / ".git").mkdir()
        assert load_suppressions(project / "references.bib").rules == []

    def test_no_config_means_no_suppressions(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        (project / ".git").mkdir()
        suppressions = load_suppressions(project / "references.bib")
        assert not suppressions
        assert suppressions.source is None


class TestMalformedConfig:
    def test_broken_toml_is_a_sentence_not_a_traceback(self, tmp_path: Path) -> None:
        config = write_config(tmp_path, '[[ignore]\nkey = "a"\nreason = "checked"\n')
        with pytest.raises(SuppressionError) as exc:
            load_suppressions(tmp_path)
        assert str(config) in str(exc.value)

    def test_a_single_table_instead_of_an_array_is_explained(self, tmp_path: Path) -> None:
        """`[ignore]` for `[[ignore]]` is the easiest mistake in TOML to make."""
        write_config(tmp_path, '[ignore]\nkey = "a"\nreason = "checked"\n')
        with pytest.raises(SuppressionError) as exc:
            load_suppressions(tmp_path)
        assert "[[ignore]]" in str(exc.value)

    def test_a_scalar_ignore_is_explained(self, tmp_path: Path) -> None:
        write_config(tmp_path, "ignore = 5\n")
        with pytest.raises(SuppressionError) as exc:
            load_suppressions(tmp_path)
        assert "[[ignore]]" in str(exc.value)

    def test_an_entry_that_is_not_a_table_is_explained(self, tmp_path: Path) -> None:
        write_config(tmp_path, 'ignore = ["papantoniou2017colorectal"]\n')
        with pytest.raises(SuppressionError) as exc:
            load_suppressions(tmp_path)
        assert "not a table" in str(exc.value)

    def test_a_file_that_is_not_utf8_is_explained(self, tmp_path: Path) -> None:
        """Editors on Windows still write Latin-1, and a UnicodeDecodeError here
        aborts the whole run with a stack trace pointing into the tool."""
        tmp_path.joinpath(CONFIG_NAME).write_bytes(
            b'[[ignore]]\nkey = "arago\xf1es2020"\nreason = "checked"\n'
        )
        with pytest.raises(SuppressionError) as exc:
            load_suppressions(tmp_path)
        assert "UTF-8" in str(exc.value)

    def test_a_misspelled_key_is_refused_rather_than_widening_the_rule(
        self, tmp_path: Path
    ) -> None:
        """`fields` for `field` leaves `field = "*"`, silencing the whole entry.

        A typo in this file must never make a suppression broader than what the
        user wrote, because nothing downstream would show that it had.
        """
        write_config(
            tmp_path,
            '[[ignore]]\nkey = "a"\nfields = "volume"\nreason = "checked"\n',
        )
        with pytest.raises(SuppressionError) as exc:
            load_suppressions(tmp_path)
        assert "fields" in str(exc.value)

    def test_an_empty_config_is_valid_and_silences_nothing(self, tmp_path: Path) -> None:
        write_config(tmp_path, "# nothing adjudicated yet\n")
        suppressions = load_suppressions(tmp_path)
        assert not suppressions
        assert suppressions.rules == []
