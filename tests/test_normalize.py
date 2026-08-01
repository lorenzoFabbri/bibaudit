"""Normalisation rules.

Most of these encode a specific failure observed on a real bibliography. Where
that is the case the test name says which, so a future change that breaks one
knows what it is breaking.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import bibaudit.normalize as normalize_module
from bibaudit.normalize import (
    clean,
    extract_dois,
    first_page,
    fold,
    is_article_number,
    normalize_doi,
    normalize_kind,
    parse_year,
    similarity,
)


class TestDoi:
    """DOI handling. The parenthesis case is the one that matters most."""

    @pytest.mark.parametrize(
        "raw",
        [
            "10.1016/S0140-6736(03)14065-2",
            "https://doi.org/10.1016/S0140-6736(03)14065-2",
            "http://dx.doi.org/10.1016/S0140-6736(03)14065-2",
            "doi:10.1016/S0140-6736(03)14065-2",
            "10.1016/S0140-6736(03)14065-2.",
        ],
    )
    def test_lancet_doi_survives_normalisation(self, raw: str) -> None:
        """Elsevier and Lancet DOIs contain parentheses.

        A character class that excludes ``()`` truncates these at the bracket
        and reports a live DOI as unresolvable — 12 such false "does not exist"
        hits appeared on a bibliography that was in fact clean.
        """
        assert normalize_doi(raw) == "10.1016/s0140-6736(03)14065-2"

    def test_unbalanced_trailing_bracket_is_punctuation(self) -> None:
        assert normalize_doi("(10.1234/abc)") == "10.1234/abc"

    def test_balanced_bracket_is_kept(self) -> None:
        assert normalize_doi("10.1016/S0140-6736(05)66455-0") == "10.1016/s0140-6736(05)66455-0"

    def test_case_is_folded(self) -> None:
        """The same entry often carries mixed case in its DOI and url fields."""
        assert normalize_doi("10.1158/1055-9965.EPI-20-0378") == normalize_doi(
            "10.1158/1055-9965.epi-20-0378"
        )

    def test_extract_finds_parenthesised_dois_in_prose(self) -> None:
        text = 'See 10.1016/S0140-6736(03)14065-2 and "10.1038/ng.3341", plus 10.1038/ng.3341.'
        assert extract_dois(text) == [
            "10.1016/s0140-6736(03)14065-2",
            "10.1038/ng.3341",
        ]

    def test_extract_deduplicates_preserving_order(self) -> None:
        text = "10.1038/ng.3341 10.1002/ijc.29590 10.1038/ng.3341"
        assert extract_dois(text) == ["10.1038/ng.3341", "10.1002/ijc.29590"]

    def test_a_registrant_prefix_needs_at_least_four_digits(self) -> None:
        """``10.1/x`` is not a DOI; the shortest real prefix is ``10.1000``."""
        assert extract_dois("see 10.1/b for details") == []


class TestClean:
    """Presentation-level cleanup."""

    def test_html_entities_and_tags_are_removed(self) -> None:
        """Crossref ships real markup inside title and container fields."""
        assert clean("Cancer Epidemiology, Biomarkers &amp; Prevention") == (
            "Cancer Epidemiology, Biomarkers & Prevention"
        )
        assert clean("Effect of <i>BRCA1</i> on H<sub>2</sub>O") == "Effect of BRCA1 on H2O"

    def test_bibtex_brace_armour_is_removed(self) -> None:
        assert clean("{PanGenEU} study") == "PanGenEU study"

    def test_latex_accents_reduce_to_the_base_letter(self) -> None:
        """Enough for a comparison key; fold() would drop the diacritic anyway."""
        assert clean(r"L{\"o}hr") == "Lohr"
        assert clean(r"Tard\'on") == "Tardon"

    def test_unicode_punctuation_is_regularised(self) -> None:
        """A curly apostrophe is the single commonest cosmetic title difference."""
        assert clean("Parkinson’s Disease") == "Parkinson's Disease"
        assert clean("1009–1018") == "1009-1018"


class TestFold:
    def test_ampersand_expands_before_punctuation_is_dropped(self) -> None:
        assert fold("Cancer Epidemiology & Prevention") == fold(
            "Cancer Epidemiology and Prevention"
        )

    def test_accents_are_folded(self) -> None:
        assert fold("Núria Malats") == fold("Nuria Malats")

    def test_case_and_punctuation_are_dropped(self) -> None:
        assert fold("Shift work and colorectal cancer risk") == fold(
            "SHIFT WORK, AND COLORECTAL-CANCER RISK!"
        )

    def test_spanish_words_are_not_truncated(self) -> None:
        """A non-Unicode word regex turns 'españa' into 'espa'."""
        assert "espana" in fold("Estudio en España")


class TestSimilarity:
    def test_identical_after_folding_is_one(self) -> None:
        assert similarity("A Study of X", "a study of x!") == 1.0

    def test_word_order_matters(self) -> None:
        """'Effect of A on B' and 'Effect of B on A' are different papers.

        A token-set ratio scores them identically, which is why the tool uses a
        sequence measure instead.
        """
        assert similarity("Effect of smoking on cancer", "Effect of cancer on smoking") < 0.9

    def test_empty_input_is_zero_not_an_error(self) -> None:
        assert similarity("", "anything") == 0.0


class TestPages:
    def test_only_the_opening_page_is_compared(self) -> None:
        """Registries record 1009-1018, 1009-18 and 1009 for the same article."""
        assert first_page("1009-1018") == first_page("1009-18") == first_page("1009")

    def test_en_dash_ranges_are_handled(self) -> None:
        assert first_page("473–483") == "473"

    def test_ehp_zero_padded_article_numbers(self) -> None:
        """Environmental Health Perspectives deposits 027004 for article 27004."""
        assert first_page("027004") == first_page("27004") == "27004"

    def test_letter_prefixes_are_preserved(self) -> None:
        assert first_page("e324-e336") == "e324"

    def test_article_number_detection(self) -> None:
        assert is_article_number("693933")
        assert is_article_number("e0123456")
        assert not is_article_number("1009-1018")
        assert not is_article_number("12")

    def test_a_four_digit_opening_page_is_not_an_article_number(self) -> None:
        """The true positive: ``2461`` is a page, and it has to stay one.

        10.1001/archinte.167.22.2461 opens on page 2461 of *Arch Intern Med*
        167(22). Calling that an article number let ``benign._pages_article_
        number`` excuse a stored ``2461`` against a registry ``2450-2455`` as
        "one article in two notations" — a plain page disagreement, silenced.
        """
        assert not is_article_number("2461")
        assert not is_article_number("27004")

    def test_a_letter_prefixed_number_needs_no_digit_floor(self) -> None:
        """No journal paginates ``A102``; the prefix alone identifies it."""
        assert is_article_number("A102")


class TestYear:
    @pytest.mark.parametrize(
        "raw", ["2021", "2021-05", "May 2021", "2021 Aug 12", "{2021}", "2021/2022"]
    )
    def test_year_is_found_in_every_date_format(self, raw: str) -> None:
        assert parse_year(raw) == 2021

    def test_absent_year_is_none_not_zero(self) -> None:
        assert parse_year("in press") is None
        assert parse_year(None) is None


class TestKind:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("journal-article", "article"),
            ("article", "article"),
            ("journalArticle", "article"),
            ("book-chapter", "chapter"),
            ("incollection", "chapter"),
            ("posted-content", "preprint"),
            ("phdthesis", "thesis"),
            ("something-invented", "other"),
        ],
    )
    def test_types_map_onto_the_internal_vocabulary(self, raw: str, expected: str) -> None:
        assert normalize_kind(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # "report" is spelled identically by Crossref, BibLaTeX, Zotero and
            # CSL, and it was listed twice. Every spelling must still resolve.
            ("report", "report"),
            ("techreport", "report"),
            ("report-component", "report"),
            ("Report", "report"),
            # CSL's spelling of a Zotero blogPost, which the duplicate had
            # displaced.
            ("post-weblog", "webpage"),
            ("blogPost", "webpage"),
            # DataCite's resourceTypeGeneral for a book chapter. Crossref's
            # "book-chapter", Zotero's "bookSection" and CSL's "chapter" were
            # all mapped; this spelling alone fell through to "other", so a
            # chapter deposited with DataCite had its type checked against
            # nothing.
            ("BookChapter", "chapter"),
        ],
    )
    def test_every_spelling_of_a_mapped_type_resolves(self, raw: str, expected: str) -> None:
        assert normalize_kind(raw) == expected

    def test_no_input_type_is_shadowed_by_a_repeated_key(self) -> None:
        """A repeated literal in ``_KIND_MAP`` is invisible once the module loads.

        Python keeps the last value for a duplicated key, so the earlier line is
        dead. Because the table is grouped by vocabulary, a duplicate is almost
        always a copy-paste that displaced the entry it was meant to be: a
        second ``"report": "report"`` had overwritten CSL's ``"post-weblog"``,
        and blog posts therefore normalised to ``other`` — no type check at all,
        with nothing in the running program to show for it.

        The source is parsed rather than the dict inspected, because by import
        time the evidence has already been collapsed away.
        """
        source = Path(inspect.getsourcefile(normalize_module) or "").read_text(encoding="utf-8")
        (assignment,) = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "_KIND_MAP" for t in node.targets
            )
        ]
        assert isinstance(assignment.value, ast.Dict)
        keys = [
            key.value
            for key in assignment.value.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        ]
        assert len(keys) == len(assignment.value.keys), "every key must be a string literal"
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        assert not duplicates, f"_KIND_MAP keys written twice: {duplicates}"

    def test_keys_are_written_in_the_form_normalize_kind_looks_up(self) -> None:
        """A key that ``fold()`` would rewrite can never be hit.

        ``normalize_kind`` looks up ``fold(value).replace(" ", "-")``, so a key
        spelled ``bookSection`` or ``Journal Article`` is unreachable however
        correct it looks. That failure is silent — the type just becomes
        ``other`` — which is precisely the class of defect this table's
        duplicate ``"report"`` belonged to.
        """
        unreachable = [
            key
            for key in normalize_module._KIND_MAP
            if fold(key).replace(" ", "-") != key
        ]
        assert not unreachable, f"_KIND_MAP keys that normalize_kind can never look up: {unreachable}"
