"""Reading a BibTeX (``.bib``) file into References: parsing, DOI recovery,
creator handling, entry locators and duplicate detection.

``tests/data/sample.bib`` is deliberately irregular — two entries sharing one
physical line, a citekey reused across two blocks, a block whose closing
brace was never typed — because a real ``.bib`` file, hand-maintained or
exported by different tools over the years, looks exactly like this. A
parser only exercised on tidy one-entry-per-line input is not exercised by
anything a user will actually hand it.
"""

from __future__ import annotations

import pathlib
import warnings

import pytest

from bibaudit.adapters.bibtex import duplicate_report, entry_locator, read_bibtex
from bibaudit.model import Reference

_FIXTURE = pathlib.Path(__file__).parent / "data" / "sample.bib"


@pytest.fixture
def refs() -> list[Reference]:
    """The fixture's References, with its two expected warnings silenced.

    The warnings themselves (duplicate citekey, unterminated block) are
    asserted on directly in ``TestFailedBlocks``, which calls
    :func:`read_bibtex` again with ``pytest.warns`` instead of using this
    fixture — silencing them here only keeps the rest of the suite's output
    clean.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return read_bibtex(_FIXTURE)


def _by_key(refs: list[Reference], key: str) -> Reference:
    return next(r for r in refs if r.key == key)


class TestParsing:
    def test_entry_count(self, refs: list[Reference]) -> None:
        """13 ``@``-blocks are in the file; two never become References.

        ``dupkey2022``'s second block is dropped by bibtexparser itself
        (a reused citekey), and ``malformed2023`` never closes, so 11
        entries, not 13, reach ``read_bibtex``'s return value.
        """
        assert len(refs) == 11

    def test_entries_sharing_one_physical_line_are_both_read(self, refs: list[Reference]) -> None:
        """BibTeX has no one-entry-per-line rule; v2's block parser does not need one."""
        keys = {r.key for r in refs}
        assert {"oneline1", "oneline2"} <= keys
        assert _by_key(refs, "oneline1").title == "First inline entry"
        assert _by_key(refs, "oneline2").title == "Second inline entry"

    def test_at_sign_inside_a_title_does_not_break_parsing(self, refs: list[Reference]) -> None:
        """An ``@`` inside a field must not be read as a new block opening."""
        ref = _by_key(refs, "atsign2021")
        assert ref.title == "Response to @mentions in social media surveillance"


class TestDoiExtraction:
    def test_parenthesised_lancet_doi_survives(self, refs: list[Reference]) -> None:
        """Elsevier/Lancet DOIs contain parentheses; see normalize.DOI_PATTERN."""
        ref = _by_key(refs, "lancetdoi2003")
        assert ref.doi == "10.1016/s0140-6736(03)14065-2"

    def test_doi_only_in_url_field_is_recovered(self, refs: list[Reference]) -> None:
        """An entry with no ``doi`` field but a doi.org ``url`` still yields a DOI."""
        ref = _by_key(refs, "urlonlydoi2019")
        assert ref.doi == "10.1234/urlonly.2019"


class TestCreators:
    def test_editor_is_used_when_there_is_no_author(self, refs: list[Reference]) -> None:
        """An edited volume with no ``author`` field is correct BibTeX, not empty authors."""
        ref = _by_key(refs, "editedvolume2015")
        assert ref.raw.get("creator_role") == "editor"
        assert [a.family for a in ref.authors] == ["Hidalgo"]

    def test_collective_author_is_one_creator_not_several(self, refs: list[Reference]) -> None:
        ref = _by_key(refs, "collectiveauthor2018")
        assert len(ref.authors) == 1
        assert ref.authors[0].collective
        assert "Collaborative Group" in ref.authors[0].literal

    def test_and_others_is_kept_as_a_truncation_marker(self, refs: list[Reference]) -> None:
        ref = _by_key(refs, "etal2020")
        assert len(ref.authors) == 3
        assert ref.authors[-1].et_al


class TestLocator:
    def test_line_numbers_point_at_each_entrys_opening_brace(self, refs: list[Reference]) -> None:
        by_key = {r.key: r.locator for r in refs}
        assert by_key["oneline1"] == "sample.bib:9"
        assert by_key["oneline2"] == "sample.bib:9"  # both entries open on the same line
        assert by_key["atsign2021"] == "sample.bib:10"
        assert by_key["lancetdoi2003"] == "sample.bib:16"
        assert by_key["dupkey2022"] == "sample.bib:62"  # the surviving, first block

    def test_entry_locator_agrees_with_read_bibtexs_own_locator(self, refs: list[Reference]) -> None:
        """entry_locator is a standalone re-lookup; it must find the same line
        read_bibtex already attached to each Reference.
        """
        for ref in refs:
            assert entry_locator(_FIXTURE, ref.key) == ref.locator

    def test_unknown_key_gets_a_placeholder_not_a_keyerror(self) -> None:
        assert entry_locator(_FIXTURE, "nonexistent-key") == "sample.bib:?"


class TestFailedBlocks:
    """bibtexparser's own recovery from two different defects, surfaced as warnings."""

    def test_duplicate_citekey_keeps_only_the_first_entry(self, refs: list[Reference]) -> None:
        matches = [r for r in refs if r.key == "dupkey2022"]
        assert len(matches) == 1
        assert matches[0].title == "The entry that should survive"

    def test_duplicate_citekey_raises_a_warning_naming_key_and_line(self) -> None:
        with pytest.warns(RuntimeWarning, match=r"sample\.bib:68.*duplicate citekey 'dupkey2022'"):
            read_bibtex(_FIXTURE)

    def test_unterminated_block_raises_a_warning_naming_its_line(self) -> None:
        with pytest.warns(RuntimeWarning, match=r"sample\.bib:74.*block failed to parse"):
            read_bibtex(_FIXTURE)

    def test_unterminated_block_produces_no_reference(self, refs: list[Reference]) -> None:
        assert not any(r.key == "malformed2023" for r in refs)


class TestDuplicateReport:
    """Grouping probable accidental duplicates: shared DOI, reused key, near title."""

    def test_shared_doi_is_reported_regardless_of_case(self, refs: list[Reference]) -> None:
        """shareddoia2021/shareddoib2021 differ only in DOI case; normalize_doi
        folds both to the same key before grouping.
        """
        report = duplicate_report(refs)
        assert len(report["doi"]) == 1
        assert "shareddoia2021" in report["doi"][0]
        assert "shareddoib2021" in report["doi"][0]

    def test_entries_with_distinct_dois_produce_no_extra_groups(self, refs: list[Reference]) -> None:
        report = duplicate_report(refs)
        joined = " ".join(report["doi"])
        assert "lancetdoi2003" not in joined
        assert "urlonlydoi2019" not in joined

    def test_report_always_carries_all_three_categories(self, refs: list[Reference]) -> None:
        """Empty categories are still present as empty lists, not omitted."""
        assert duplicate_report(refs).keys() == {"doi", "key", "title"}

    def test_reused_citekey_across_merged_sources_is_reported(self) -> None:
        """read_bibtex on one file never returns two References sharing a key
        (bibtexparser drops the second block first, see TestFailedBlocks); this
        category exists for a caller that merges references from more than one
        file, where the same key can legitimately reappear.
        """
        merged = [
            Reference(key="shared", locator="a.bib:1", title="Paper A"),
            Reference(key="shared", locator="b.bib:9", title="Paper B"),
        ]
        report = duplicate_report(merged)
        assert len(report["key"]) == 1
        assert "a.bib:1" in report["key"][0]
        assert "b.bib:9" in report["key"][0]

    def test_near_identical_titles_are_reported(self) -> None:
        """One paper cited twice under two keys, once by hand and once from an export."""
        near = [
            Reference(key="a", locator="a.bib:1", title="Shift work and colorectal cancer risk"),
            Reference(key="b", locator="b.bib:2", title="Shift work and colorectal cancer risks"),
        ]
        assert len(duplicate_report(near)["title"]) == 1

    def test_dissimilar_titles_are_not_reported(self) -> None:
        different = [
            Reference(key="a", locator="a.bib:1", title="A study of pancreatic cancer"),
            Reference(key="b", locator="b.bib:2", title="An entirely unrelated paper on marine biology"),
        ]
        assert duplicate_report(different)["title"] == []
