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
    extract_pmid,
    first_page,
    fold,
    is_article_number,
    normalize_doi,
    normalize_kind,
    parse_year,
    pmc_number,
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


class TestALetterNfkdLeavesWhole:
    """A letter that does not decompose still folds to a letter, not a space.

    NFKD splits ``ü`` into ``u`` plus a combining diaeresis and the mark is
    dropped. Sharp s, ash, o-with-stroke, l-with-stroke, eth, thorn,
    d-with-stroke, h-with-stroke and dotless i are not a base plus a mark and
    it leaves them alone. They then met a rule that replaced anything outside
    ``[a-z0-9]`` with a space, so ``Straße`` folded to ``stra e`` and ``Kjær``
    to ``kj r`` — CLAUDE.md's own banned shape, where a surname is compared on
    a fragment of itself.

    It fires because MEDLINE romanises a byline and Crossref deposits it as
    written: Susanne Kjær is ``Kjaer, Susanne K`` on PMID 42550510 and ``Kjær``
    in Crossref's record for the same DOI.
    """

    @pytest.mark.parametrize(
        ("written", "romanised"),
        [
            ("Weiß", "Weiss"),
            ("Straße", "Strasse"),
            ("Kjær", "Kjaer"),
            ("Jørgensen", "Jorgensen"),
            ("Łukszo", "Lukszo"),
            ("Friðriksdóttir", "Fridriksdottir"),
            ("Þórsson", "Thorsson"),
            ("Đorđević", "Dordevic"),
            ("Ħamed", "Hamed"),
            ("Irmak", "Irmak"),
            ("Œuvre", "Oeuvre"),
        ],
    )
    def test_the_two_spellings_of_one_name_agree(
        self, written: str, romanised: str
    ) -> None:
        assert fold(written) == fold(romanised)

    def test_an_accent_on_top_of_one_comes_off_first(self) -> None:
        """``ǽ`` is ``æ`` plus an acute, so NFKD reduces it before the table."""
        assert fold("Ǽsir") == fold("Aesir")

    def test_no_letter_is_replaced_by_a_token_boundary(self) -> None:
        """The damage the mapping exists to prevent, stated as itself.

        A letter with no romanisation is removed rather than spaced, so even a
        letter this table has never heard of cannot split a surname in two.
        """
        assert " " not in fold("Ɓello")
        assert " " not in fold("Weiß")

    def test_punctuation_still_separates_two_words(self) -> None:
        """The other half: a space where a *mark* stood is information."""
        assert fold("colorectal-cancer risk") == "colorectal cancer risk"

    def test_a_title_in_another_script_folds_to_nothing_as_before(self) -> None:
        """Removing rather than spacing must not start keeping Cyrillic."""
        assert fold("Онкология") == ""
        assert fold("TNF-α levels") == "tnf levels"


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


class TestPmidExtraction:
    """What a note may and may not be read as declaring a PMID.

    A note field is prose. It carries the entry's own identifier on a line
    Zotero wrote, and it carries sentences about other documents — errata,
    comments, retraction notices, companion papers — several of which quote a
    PMID of their own. Reading one of those is not a cosmetic error: with no
    DOI to outrank it, the number becomes the lookup key, and the entry is
    resolved as and compared against a paper it merely mentions.
    """

    @pytest.mark.parametrize(
        "note",
        [
            "PMID: 28520842",
            "PMID:28520842",
            "PMID 28520842",
            "pmid = 28520842",
            "Citation Key: kim2017alcohol\nPMID: 28520842",
        ],
    )
    def test_a_line_that_opens_with_the_label_declares_the_identifier(self, note: str) -> None:
        """Zotero's own convention, and the hand-typed variants beside it."""
        assert extract_pmid(note) == "28520842"

    def test_the_pmcid_line_below_it_is_not_read(self) -> None:
        """The pair Zotero's PubMed translator writes into ``Extra``.

        ``PMCID`` is a different registry's number for a different object, and
        an entry filed under it resolves nowhere.
        """
        assert extract_pmid("PMID: 28520842\nPMCID: PMC5860629") == "28520842"

    @pytest.mark.parametrize(
        "note",
        [
            # MEDLINE back-matter, pasted into Extra as it comes: both name the
            # correction, never the work being cited.
            "Erratum in PMID: 12237289\nre-analysis of nine cohorts",
            "Comment in: JAMA. 2003;289:2560. PMID: 12759325",
            # A sentence recording the *absence* of one. The number after it is
            # a year.
            "no PMID: 2017 reanalysis has one",
            # The same absence with the label ending the line, so nothing may
            # be claimed across the break either.
            "superseded, see PMID\n2017 reanalysis in the same journal",
        ],
    )
    def test_a_label_inside_a_sentence_declares_nothing(self, note: str) -> None:
        assert extract_pmid(note) is None

    def test_a_label_alone_on_its_line_reaches_across_no_break(self) -> None:
        """The case the *horizontal*-whitespace separator class decides alone.

        The label opens its line here, so the anchor is satisfied and only the
        separator stands between it and the digits on the next one. ``\\s``
        there would read them as the declared identifier and resolve the entry
        by a year — the same wrong-lookup-key failure as a label read out of a
        sentence, reached by the one route the anchor cannot close.
        """
        assert extract_pmid("PMID\n2017 reanalysis in the same journal") is None

    def test_a_tab_between_the_label_and_the_number_still_declares_it(self) -> None:
        """And the class is horizontal whitespace, not a single space."""
        assert extract_pmid("PMID:\t28520842") == "28520842"

    def test_a_label_opening_a_wrapped_continuation_line_declares_nothing(self) -> None:
        """The shape MEDLINE itself emits, and the one that costs the most.

        ``efetch`` wraps a ``RIN``/``CIN``/``CON``/``EIN`` block at 80 columns
        and continues it six spaces in, so the label lands at the start of a
        line without opening one. Verbatim from PMID 42104705 (*Arch Esp Urol*
        79(3):504-512), a retracted paper: 42438885 is the notice that
        retracted it. Skipping the indent resolved a correct entry to that
        notice, called it ``WRONG-WORK`` for a title it never claimed, and left
        the report saying nothing about the retraction — ignorance rendered as
        a different accusation.
        """
        note = (
            "RIN - Arch Esp Urol. 2026 Jun;79(5):709. doi: 10.56434/j.arch.esp."
            "urol.20267905.83.\n      PMID: 42438885"
        )
        assert extract_pmid(note) is None

    def test_the_entrys_own_declaration_survives_pasted_back_matter(self) -> None:
        """The pairing: refusing the continuation must not refuse the note.

        An Extra box holding both is the ordinary case — Zotero's translator
        writes the identifier at column zero, and whatever the user pasted
        under it keeps MEDLINE's indent.
        """
        note = (
            "PMID: 42104705\n"
            "RIN - Arch Esp Urol. 2026 Jun;79(5):709.\n      PMID: 42438885"
        )
        assert extract_pmid(note) == "42104705"

    def test_a_malformed_declaration_stops_the_read(self) -> None:
        """The first labelled line settles it, and this one is unusable.

        Reading on would hand back 20137807 — the PMID of the retraction
        notice the note goes on to name — as this work's identifier. A miss
        costs a lookup; that hit reports a sound entry against a document
        published twelve years after the one it cites.
        """
        assert extract_pmid("PMID: 285208421234\nPMID: 20137807") is None

    def test_a_label_run_together_with_the_number_declares_nothing(self) -> None:
        """Opening the line is not enough — ``PMID`` has to end as a word too.

        ``PMID12345678`` puts no separator of any kind between the label and
        the digits, which is not how Zotero, MEDLINE, EndNote or a hand-typed
        note writes a declaration. Read as one, whatever record those digits
        name becomes the entry's lookup key. The separator, the indent and the
        mid-line position are each pinned above; this is the character
        immediately after the label, and it is the only thing the word boundary
        decides.
        """
        assert extract_pmid("PMID12345678") is None

    def test_a_number_alone_is_not_an_identifier(self) -> None:
        """An unlabelled run of digits in a note is a grant number, an
        accession or a sample size far more often than it is a PMID.
        """
        assert extract_pmid("28520842") is None
        assert extract_pmid("cohort of 28520842 person-years") is None


class TestPmcAccession:
    """The other number NLM puts on the same record.

    PMID 28520842 (*Am J Epidemiol* 2017) carries ``PMC  - PMC5860629``: one
    citation, two accession schemes. Telling them apart is what the prefix is
    for, and a bibliography with the accession in its ``pmid`` field is why
    anything reads it.
    """

    @pytest.mark.parametrize("raw", ["PMC5860629", "pmc5860629", " PMC5860629 "])
    def test_the_prefixed_form_yields_its_digits(self, raw: str) -> None:
        assert pmc_number(raw) == "5860629"

    def test_a_bare_number_is_not_an_accession(self) -> None:
        """The refusal the whole function exists for.

        PMIDs and PMC accessions are both bare ascending integers. Accepting a
        naked number here would make every PMID answer as an accession, and the
        rule that reads this would then explain away every disagreement it was
        written to report.
        """
        assert pmc_number("5860629") is None

    @pytest.mark.parametrize("raw", ["", "PMC", "PMC5860629.1", "PMCID: PMC5860629", None])
    def test_anything_else_is_refused(self, raw: object) -> None:
        assert pmc_number(raw) is None


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
