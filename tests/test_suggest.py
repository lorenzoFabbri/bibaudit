"""``--suggest``: what gets proposed, what never does, and that the original
bibliography is never touched.

Most cases here build a real :class:`~bibaudit.model.Result` by calling
:func:`bibaudit.compare.compare` on hand-built Reference/Record pairs, the
same way ``tests/test_compare.py`` does — so a suggestion is only ever tested
against an Issue :mod:`bibaudit.compare` would actually produce, not one
invented to fit this module.
"""

from __future__ import annotations

import pathlib

import bibtexparser
import pytest

from bibaudit.compare import compare
from bibaudit.model import Issue, Name, Record, Reference, Result
from bibaudit.suggest import build_suggestion, write_suggestions

_SMITH_BIB = """\
@article{smith2020,
  title = {A study of things},
  author = {Smith, John},
  year = {2020}
}

@article{jones2019,
  title = {Another study},
  author = {Jones, Ann},
  year = {2019},
  journal = {J. Test},
  volume = {5},
  pages = {1-10},
}
"""


def _write_bib(tmp_path: pathlib.Path, name: str, text: str) -> pathlib.Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _smith_result(**ref_overrides: object) -> Result:
    """smith2020: title/authors/year present and correct; everything else absent."""
    ref = Reference(
        key="smith2020",
        locator="smith.bib:1",
        kind="article",
        doi="10.1234/smith",
        title="A study of things",
        authors=[Name(family="Smith", given="John")],
        year=2020,
        **ref_overrides,  # type: ignore[arg-type]
    )
    record = Record(
        source="crossref",
        doi="10.1234/smith",
        title="A study of things",
        authors=[Name(family="Smith", given="John")],
        years={"print": 2020},
        container="Journal of Testing",
        container_short="J Test",
        volume="5",
        issue="2",
        pages="100-110",
        publisher="Test Publisher",
        kind="journal-article",
    )
    return compare(ref, {"crossref": record})


def _jones_result(*, agreeing: bool) -> Result:
    """jones2019 already carries journal/volume/pages; agreeing or disputed."""
    ref = Reference(
        key="jones2019",
        locator="smith.bib:7",
        kind="article",
        doi="10.1234/jones",
        title="Another study",
        authors=[Name(family="Jones", given="Ann")],
        year=2019,
        container="J. Test",
        volume="5",
        pages="1-10",
    )
    record = Record(
        source="crossref",
        doi="10.1234/jones",
        title="Another study",
        authors=[Name(family="Jones", given="Ann")],
        years={"print": 2019},
        container="J. Test" if agreeing else "Journal of Something Else Entirely",
        volume="5",
        pages="1-10" if agreeing else "999-999",
        kind="journal-article",
    )
    return compare(ref, {"crossref": record})


class TestFillsOnlyTrueGaps:
    def test_missing_scalar_fields_are_proposed(self, tmp_path: pathlib.Path) -> None:
        bib = _write_bib(tmp_path, "smith.bib", _SMITH_BIB)
        suggestion = build_suggestion(bib, [_smith_result()])
        assert suggestion is not None
        assert suggestion.entries_changed == 1
        # container -> journal, volume, issue -> number, pages, publisher: five gaps.
        assert suggestion.fields_filled == 5
        assert "journal = {Journal of Testing}" in suggestion.new_text
        assert "volume = {5}" in suggestion.new_text
        assert "number = {2}" in suggestion.new_text
        assert "pages = {100-110}" in suggestion.new_text
        assert "publisher = {Test Publisher}" in suggestion.new_text

    def test_untouched_entry_is_byte_identical(self, tmp_path: pathlib.Path) -> None:
        # jones2019 gets no suggestion in this scenario (see below); its own
        # source text must survive in the suggested file unchanged.
        bib = _write_bib(tmp_path, "smith.bib", _SMITH_BIB)
        suggestion = build_suggestion(bib, [_smith_result()])
        assert suggestion is not None
        jones_block = (
            "@article{jones2019,\n"
            "  title = {Another study},\n"
            "  author = {Jones, Ann},\n"
            "  year = {2019},\n"
            "  journal = {J. Test},\n"
            "  volume = {5},\n"
            "  pages = {1-10},\n"
            "}\n"
        )
        assert jones_block in suggestion.new_text

    def test_no_trailing_comma_gets_one_added_and_stays_parseable(
        self, tmp_path: pathlib.Path
    ) -> None:
        # smith2020's last field ("year = {2020}") has no trailing comma in
        # the fixture; the inserted fields must not produce invalid BibTeX.
        bib = _write_bib(tmp_path, "smith.bib", _SMITH_BIB)
        suggestion = build_suggestion(bib, [_smith_result()])
        assert suggestion is not None
        suggested_path = tmp_path / "smith.suggested.bib"
        suggested_path.write_text(suggestion.new_text, encoding="utf-8")
        library = bibtexparser.parse_file(str(suggested_path))
        assert not library.failed_blocks
        entry = next(e for e in library.entries if e.key == "smith2020")
        assert entry["journal"] == "Journal of Testing"
        assert entry["pages"] == "100-110"

    def test_matches_existing_indentation(self, tmp_path: pathlib.Path) -> None:
        bib = _write_bib(
            tmp_path,
            "wide.bib",
            "@article{smith2020,\n    title = {A study of things},\n    author = {Smith, John},\n"
            "    year = {2020},\n}\n",
        )
        suggestion = build_suggestion(bib, [_smith_result()])
        assert suggestion is not None
        assert "\n    journal = {Journal of Testing}," in suggestion.new_text


class TestNeverOverwritesADisagreement:
    def test_agreeing_fields_produce_no_suggestion(self, tmp_path: pathlib.Path) -> None:
        bib = _write_bib(tmp_path, "smith.bib", _SMITH_BIB)
        suggestion = build_suggestion(bib, [_jones_result(agreeing=True)])
        assert suggestion is None  # nothing missing, nothing to propose

    def test_disagreeing_fields_are_never_proposed(self, tmp_path: pathlib.Path) -> None:
        result = _jones_result(agreeing=False)
        assert any(i.kind == "mismatch" for i in result.issues), "fixture must actually disagree"
        bib = _write_bib(tmp_path, "smith.bib", _SMITH_BIB)
        suggestion = build_suggestion(bib, [result])
        # jones2019 already has a value for every checked field, so a
        # disagreement produces no "missing"/"proposed" issue to act on.
        assert suggestion is None


class TestAuthorsAreNeverSuggested:
    def test_missing_author_list_is_not_filled(self, tmp_path: pathlib.Path) -> None:
        ref = Reference(
            key="smith2020",
            locator="smith.bib:1",
            kind="article",
            doi="10.1234/smith",
            title="A study of things",
            authors=[],  # absent entirely
            year=2020,
        )
        record = Record(
            source="crossref",
            doi="10.1234/smith",
            title="A study of things",
            authors=[Name(family="Smith", given="John"), Name(family="Doe", given="Jane")],
            years={"print": 2020},
            kind="journal-article",
        )
        result = compare(ref, {"crossref": record})
        assert any(i.field == "authors" and i.kind == "missing" for i in result.issues)
        bib = _write_bib(tmp_path, "smith.bib", _SMITH_BIB)
        # No suggestion at all: authors is the only gap this Result carries,
        # and authors is deliberately excluded (see suggest.py's docstring).
        assert build_suggestion(bib, [result]) is None


class TestProposedDoi:
    def test_proposed_doi_is_filled(self, tmp_path: pathlib.Path) -> None:
        ref = Reference(key="nodoi2021", locator="smith.bib:1", kind="article", doi=None)
        result = Result(
            ref=ref,
            verdict="INCOMPLETE",
            issues=[
                Issue(
                    field="doi",
                    kind="proposed",
                    severity="warning",
                    stored="",
                    registry="10.9999/example",
                    source="crossref",
                    note="entry has no DOI; title 0.95 with author and year corroboration",
                )
            ],
        )
        bib = _write_bib(
            tmp_path,
            "nodoi.bib",
            "@article{nodoi2021,\n  title = {Something},\n  year = {2021},\n}\n",
        )
        suggestion = build_suggestion(bib, [result])
        assert suggestion is not None
        assert "doi = {10.9999/example}" in suggestion.new_text


class TestHeaderAndDiff:
    def test_header_names_the_registry_and_says_review(self, tmp_path: pathlib.Path) -> None:
        bib = _write_bib(tmp_path, "smith.bib", _SMITH_BIB)
        suggestion = build_suggestion(bib, [_smith_result()])
        assert suggestion is not None
        assert "GENERATED" in suggestion.new_text
        assert "crossref" in suggestion.new_text
        assert "review" in suggestion.new_text.lower() or "REVIEW" in suggestion.new_text

    def test_diff_is_a_valid_unified_diff_against_the_original(
        self, tmp_path: pathlib.Path
    ) -> None:
        bib = _write_bib(tmp_path, "smith.bib", _SMITH_BIB)
        suggestion = build_suggestion(bib, [_smith_result()])
        assert suggestion is not None
        assert suggestion.diff_text.startswith("--- ")
        assert "+journal = {Journal of Testing}" in suggestion.diff_text.replace("+  ", "+")


class TestWriteSuggestionsNeverTouchesTheOriginal:
    def test_original_bytes_are_unchanged(self, tmp_path: pathlib.Path) -> None:
        bib = _write_bib(tmp_path, "smith.bib", _SMITH_BIB)
        before = bib.read_bytes()
        write_suggestions([_smith_result()], [bib])
        assert bib.read_bytes() == before

    def test_writes_suggested_bib_and_diff(self, tmp_path: pathlib.Path) -> None:
        bib = _write_bib(tmp_path, "smith.bib", _SMITH_BIB)
        outcomes = write_suggestions([_smith_result()], [bib])
        assert len(outcomes) == 1
        assert (tmp_path / "smith.suggested.bib").is_file()
        assert (tmp_path / "smith.suggested.diff").is_file()
        assert outcomes[0].fields_filled == 5

    def test_no_files_written_when_nothing_is_suggestable(self, tmp_path: pathlib.Path) -> None:
        bib = _write_bib(tmp_path, "smith.bib", _SMITH_BIB)
        outcomes = write_suggestions([_jones_result(agreeing=True)], [bib])
        assert outcomes == []
        assert not (tmp_path / "smith.suggested.bib").exists()

    def test_multiple_bibliographies_each_get_their_own_output(
        self, tmp_path: pathlib.Path
    ) -> None:
        bib_a = _write_bib(tmp_path, "a.bib", _SMITH_BIB)
        bib_b = _write_bib(
            tmp_path,
            "b.bib",
            "@article{nodoi2021,\n  title = {Something},\n  year = {2021},\n}\n",
        )
        result_b = Result(
            ref=Reference(key="nodoi2021", locator="b.bib:1", kind="article", doi=None),
            verdict="INCOMPLETE",
            issues=[
                Issue(
                    field="doi", kind="proposed", severity="warning",
                    stored="", registry="10.9999/example", source="crossref",
                )
            ],
        )
        outcomes = write_suggestions([_smith_result(), result_b], [bib_a, bib_b])
        assert {o.source for o in outcomes} == {bib_a, bib_b}
        assert (tmp_path / "a.suggested.bib").is_file()
        assert (tmp_path / "b.suggested.bib").is_file()


class TestUnmatchedEntriesAreIgnored:
    def test_a_result_key_absent_from_the_bib_is_skipped(self, tmp_path: pathlib.Path) -> None:
        bib = _write_bib(tmp_path, "smith.bib", _SMITH_BIB)
        stray = Reference(key="not-in-this-file", locator="elsewhere.bib:1", kind="article", doi=None)
        stray_result = Result(
            ref=stray,
            verdict="INCOMPLETE",
            issues=[
                Issue(field="doi", kind="proposed", severity="warning", stored="", registry="10.1/x", source="crossref")
            ],
        )
        suggestion = build_suggestion(bib, [stray_result])
        assert suggestion is None

    @pytest.mark.parametrize("missing_path", ["does-not-exist.bib"])
    def test_write_suggestions_skips_a_missing_bibliography(
        self, tmp_path: pathlib.Path, missing_path: str
    ) -> None:
        outcomes = write_suggestions([_smith_result()], [tmp_path / missing_path])
        assert outcomes == []
