"""The Quarto/Obsidian Markdown adapter.

This adapter decides *which* citations get checked at all, so its failures are
silent by construction: a DOI it does not extract is never verified and never
mentioned, and a citekey it invents is a false alarm against a bibliography
that is right. Both directions are covered here, and every case traces to
something observed rather than imagined.

Two real corpora stand behind the fixtures in ``tests/data/markdown_*``:

*   A 34-file Quarto catalog of epidemiological data sources
    (``sources/*.qmd`` + ``about.qmd`` + ``index.qmd`` + ``templates/*.qmd``).
    Scanning it yields **438 citekeys, all 438 resolving against its
    ``references.bib``**, and **411 distinct DOIs, every one of them also in
    that ``references.bib``** — 442 table rows, of which 31 are the same paper
    listed under two data sources. Every DOI in it lives inside an R code
    chunk, in one of exactly two shapes:
    ``read.delim(text = "...")`` pipe tables (``markdown_epic_read_delim.qmd``,
    ``markdown_mws_read_delim.qmd``) and ``data.frame(DOI = c(...))`` vectors
    (``markdown_pangeneu_data_frame.qmd``). The two fixtures of the first shape
    carry **different column sets** — EPIC has ``Data`` and ``N`` columns that
    Million Women Study does not — which is why the header is parsed and no
    column index is ever hardcoded.
*   A personal Obsidian vault (49 notes under ``work/postdoc-cnio`` and
    ``reference``, bibliography at ``material/postdoc-papers.bib``). Scanning
    it yields **130 citekeys over 432 citation sites, all 130 resolving**
    against that 147-entry ``.bib``. ``markdown_obsidian_note.md`` reproduces
    a note's structure — front-matter ``bibliography:``, ``[@key]`` prose
    citations, plain ``[[note]]`` wikilinks, an embed, a ``mermaid`` fence, a
    Markdown table, an institutional e-mail address — with the prose
    paraphrased, since the vault is private.

Tests that exist only to prove a false positive is *not* produced name the
concrete text that would otherwise be misread, per this project's rule that a
false alarm costs more than a miss. Tests that guard a *relaxation* — a rule
loosened so a real citation stops being dropped — are paired with a test that
the thing the rule was keeping out is still kept out; those pairs are marked
"true positive" in their names.
"""

from __future__ import annotations

import pathlib

import pytest

from bibaudit.adapters import markdown as md

DATA = pathlib.Path(__file__).parent / "data"


def _write(tmp_path: pathlib.Path, relpath: str, text: str) -> pathlib.Path:
    path = tmp_path / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _chunk(body: str) -> str:
    """*body* wrapped in a Quarto R code chunk, as the real tables are."""
    return f"```{{r}}\n{body}\n```\n"


def _dois(scan: md.MarkdownScan) -> list[str]:
    return [r.doi or "" for r in scan.references]


def _by_doi(scan: md.MarkdownScan, doi: str) -> md.Reference:
    matches = [r for r in scan.references if r.doi == doi]
    assert len(matches) == 1, f"expected exactly one reference for {doi}, got {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------------------
# Pandoc citation syntax, shared by Quarto and Obsidian
# ---------------------------------------------------------------------------


class TestPandocCitations:
    """The syntax Quarto and Obsidian's Pandoc Reference List plugin share."""

    def test_bracketed_and_bare(self, tmp_path: pathlib.Path) -> None:
        note = _write(
            tmp_path,
            "note.md",
            "Evidence is mixed [@smith2020; @jones2019].\n\n"
            "| Year | Ref |\n|---|---|\n| 2020 | @smith2020 |\n",
        )
        scan = md.scan_markdown([note])
        assert set(scan.citekeys) == {"smith2020", "jones2019"}

    def test_every_citation_site_is_kept_not_deduplicated(
        self, tmp_path: pathlib.Path
    ) -> None:
        # A key cited three times keeps three locators: the report has to be
        # able to say *where* a bad citation was used, not merely that it was.
        note = _write(tmp_path, "note.md", "[@a] and [@a]\nagain [@a]\n")
        scan = md.scan_markdown([note])
        assert scan.citekeys == {"a": ["note.md:1", "note.md:1", "note.md:2"]}

    def test_suppress_author_form(self, tmp_path: pathlib.Path) -> None:
        # Pandoc's "[-@key]" prints the year only. The "-" is in the citekey
        # regex's lookbehind class precisely so this stays a citation.
        note = _write(tmp_path, "note.md", "Reported earlier [-@smith2020].\n")
        assert list(md.scan_markdown([note]).citekeys) == ["smith2020"]

    def test_citation_with_a_locator_suffix(self, tmp_path: pathlib.Path) -> None:
        note = _write(tmp_path, "note.md", "As argued [@smith2020, p. 33].\n")
        assert list(md.scan_markdown([note]).citekeys) == ["smith2020"]

    def test_composite_citation_with_a_prefix(self, tmp_path: pathlib.Path) -> None:
        note = _write(tmp_path, "note.md", "[@brown2018preventable; see also @garciaclosas2005nat2]\n")
        assert set(md.scan_markdown([note]).citekeys) == {
            "brown2018preventable",
            "garciaclosas2005nat2",
        }

    @pytest.mark.parametrize(
        "sentence",
        [
            "as cited in @riboli1997epic.",
            "as shown by @riboli1997epic, the design holds",
            "did @riboli1997epic find this?",
            "per @riboli1997epic: the results follow",
            "see @riboli1997epic; also elsewhere",
        ],
    )
    def test_trailing_sentence_punctuation_is_not_part_of_the_key(
        self, tmp_path: pathlib.Path, sentence: str
    ) -> None:
        # "riboli1997epic." would resolve against nothing and be reported as a
        # broken citation on a bibliography that is correct.
        note = _write(tmp_path, "note.md", sentence + "\n")
        assert list(md.scan_markdown([note]).citekeys) == ["riboli1997epic"]


class TestCitekeyExclusions:
    """Text that looks like ``@key`` and is not a citation."""

    def test_xref_prefix_list_has_not_drifted(self) -> None:
        # If a prefix is added to or dropped from the module, this test is the
        # place that has to be updated deliberately — a silent change either
        # invents citekeys from cross-references or drops real ones.
        assert set(md._XREF_PREFIXES) == {
            "fig-", "tbl-", "sec-", "eq-", "lst-", "thm-", "def-",
            "exm-", "exr-", "cor-", "lem-", "prp-", "cnj-",
        }

    @pytest.mark.parametrize("prefix", md._XREF_PREFIXES)
    def test_quarto_cross_references_are_not_citations(
        self, tmp_path: pathlib.Path, prefix: str
    ) -> None:
        # Real instances from the corpus: "@tbl-bifap-acronyms" names a table
        # and "@fig-bifap-coding" a figure. Neither is a bibliography entry,
        # and both are written in prose exactly like a citation.
        note = _write(
            tmp_path,
            "note.qmd",
            f"See @{prefix}bifap-coding and [@{prefix}bifap-acronyms] plus [@real2020].\n",
        )
        assert list(md.scan_markdown([note]).citekeys) == ["real2020"]

    @pytest.mark.parametrize(
        "line",
        [
            "Write to a.researcher@example-institute.org for access.",
            "Contact <a.researcher@example-institute.org> first.",
            "[Write](mailto:a.researcher@example-institute.org) first.",
            "a.researcher@example-institute.org is the address.",
        ],
    )
    def test_email_addresses_are_not_citations(
        self, tmp_path: pathlib.Path, line: str
    ) -> None:
        # The vault's research-stays notes list host contacts as institutional
        # e-mail addresses; "@dkfz.de" and "@ki.se" would otherwise be reported
        # as unresolvable citekeys in a note whose citations are all fine.
        note = _write(tmp_path, "note.md", line + "\n")
        assert md.scan_markdown([note]).citekeys == {}

    def test_r_slot_access_is_not_a_citation(self, tmp_path: pathlib.Path) -> None:
        # "obj@field" is S4 slot access. Code chunks are masked for the citekey
        # pass anyway; this pins both halves of that guarantee at once.
        note = _write(tmp_path, "note.qmd", _chunk('x <- obj@field\n') + "\nReal [@real2020].\n")
        assert list(md.scan_markdown([note]).citekeys) == ["real2020"]

    def test_citation_syntax_inside_backticks_is_documentation(
        self, tmp_path: pathlib.Path
    ) -> None:
        # about.qmd line 102 of the real catalog: "Cite inline with `[@citekey]`".
        # Counting that as a citation reports "citekey" as missing from a
        # bibliography it was never meant to be in.
        note = _write(
            tmp_path,
            "note.qmd",
            "Cite inline with `[@citekey]`, render a list with `::: {#refs}`.\n\nReal [@real2020].\n",
        )
        assert list(md.scan_markdown([note]).citekeys) == ["real2020"]

    def test_double_backtick_span_containing_a_backtick(
        self, tmp_path: pathlib.Path
    ) -> None:
        # A span written with two backticks because it contains one. Trying the
        # single-backtick alternative first would stop at the inner backtick
        # and leave "@leaked2020" exposed to the citekey scanner.
        note = _write(tmp_path, "note.md", "Like `` `[@leaked2020]` `` here. Real [@real2020].\n")
        assert list(md.scan_markdown([note]).citekeys) == ["real2020"]


# ---------------------------------------------------------------------------
# Obsidian-specific syntax
# ---------------------------------------------------------------------------


class TestObsidianWikilinkCitations:
    """The Citations plugin's ``[[@key]]`` form, and what it must exclude."""

    def test_bare_wikilink_citation(self, tmp_path: pathlib.Path) -> None:
        note = _write(tmp_path, "note.md", "As shown in [[@smith2020]].\n")
        scan = md.scan_markdown([note])
        assert list(scan.citekeys) == ["smith2020"]

    def test_wikilink_citation_with_display_text(self, tmp_path: pathlib.Path) -> None:
        # The display text after "|" is what a reader sees in Obsidian; only
        # the citekey before it identifies the bibliography entry.
        note = _write(tmp_path, "note.md", "As shown in [[@smith2020|Smith et al. 2020]].\n")
        scan = md.scan_markdown([note])
        assert list(scan.citekeys) == ["smith2020"]
        assert "smith" not in scan.citekeys  # the display text was not parsed as a key

    def test_ordinary_wikilink_is_not_a_citation(self, tmp_path: pathlib.Path) -> None:
        note = _write(tmp_path, "note.md", "See [[Some Other Note]] for background.\n")
        scan = md.scan_markdown([note])
        assert scan.citekeys == {}

    def test_wikilink_with_display_text_is_not_a_citation(self, tmp_path: pathlib.Path) -> None:
        note = _write(tmp_path, "note.md", "See [[2026-01-01-log|log entry]] for background.\n")
        scan = md.scan_markdown([note])
        assert scan.citekeys == {}

    def test_embed_is_not_a_citation(self, tmp_path: pathlib.Path) -> None:
        note = _write(tmp_path, "note.md", "![[some-figure]]\n")
        scan = md.scan_markdown([note])
        assert scan.citekeys == {}

    def test_embed_of_a_citekey_named_note_is_still_not_a_citation(
        self, tmp_path: pathlib.Path
    ) -> None:
        # An embedded literature note is a transclusion of another note, not a
        # citation of the work: the "!" is the whole difference.
        note = _write(tmp_path, "note.md", "![[@smith2020]]\n")
        assert md.scan_markdown([note]).citekeys == {}

    def test_heading_link_is_not_a_citation(self, tmp_path: pathlib.Path) -> None:
        note = _write(tmp_path, "note.md", "See [[#Background]] above.\n")
        scan = md.scan_markdown([note])
        assert scan.citekeys == {}

    def test_at_mention_inside_an_ordinary_wikilink_is_not_a_citation(
        self, tmp_path: pathlib.Path
    ) -> None:
        # A personal vault's note titles can carry an "@name" mention (e.g. a
        # meeting note); the generic [@key] scanner accepts "@" right after a
        # literal "[", so this is exactly the false positive wikilink masking
        # exists to prevent — without it, "nuria" would be read as a citekey.
        note = _write(tmp_path, "note.md", "Discussed in [[Meeting with @nuria]].\n")
        scan = md.scan_markdown([note])
        assert scan.citekeys == {}

    def test_wikilink_citation_inside_backtick_is_a_documentation_example(
        self, tmp_path: pathlib.Path
    ) -> None:
        # Mirrors the existing rule for `[@citekey]`: a citation syntax shown
        # inside backticks is documentation, not an actual citation.
        note = _write(tmp_path, "note.md", "Cite it like `` `[[@smith2020]]` ``.\n")
        scan = md.scan_markdown([note])
        assert scan.citekeys == {}

    def test_mixed_forms_in_one_note(self, tmp_path: pathlib.Path) -> None:
        note = _write(
            tmp_path,
            "note.md",
            "Prior work [@jones2019] and [[@smith2020]] agree; see [[overview]] too.\n",
        )
        scan = md.scan_markdown([note])
        assert set(scan.citekeys) == {"jones2019", "smith2020"}


class TestObsidianBlockRefsAndTags:
    """Syntax that shares no character with a citekey and must stay inert."""

    def test_block_reference_produces_no_citekey(self, tmp_path: pathlib.Path) -> None:
        note = _write(tmp_path, "note.md", "A finding worth linking to. ^finding-1\n")
        scan = md.scan_markdown([note])
        assert scan.citekeys == {}
        assert scan.references == []

    def test_tag_produces_no_citekey(self, tmp_path: pathlib.Path) -> None:
        note = _write(tmp_path, "note.md", "Filed under #project/postdoc-cnio today.\n")
        scan = md.scan_markdown([note])
        assert scan.citekeys == {}

    def test_block_reference_after_a_real_citation_does_not_disturb_it(
        self, tmp_path: pathlib.Path
    ) -> None:
        note = _write(tmp_path, "note.md", "A finding [@smith2020]. ^finding-1\n")
        scan = md.scan_markdown([note])
        assert list(scan.citekeys) == ["smith2020"]


class TestObsidianNoteFixture:
    """A whole note in the real vault's shape, read end to end."""

    def test_every_prose_citation_is_found(self) -> None:
        scan = md.scan_markdown([DATA / "markdown_obsidian_note.md"])
        assert set(scan.citekeys) == {
            "dahabreh2011index",
            "furberg2022cessation",
            "kiebach2025smoking",
            "brown2018preventable",
            "garciaclosas2005nat2",
            "carrerastorres2017mr",
        }

    def test_nothing_structural_leaks_into_the_citekeys(self) -> None:
        # Everything this note contains that resembles a citation and is not
        # one: three wikilinks, one embed, one tag, one block reference, one
        # e-mail address, one `[@citekey]` documentation example, and a
        # mermaid fence full of "-->" arrows.
        scan = md.scan_markdown([DATA / "markdown_obsidian_note.md"])
        for phantom in (
            "glucose-drugs",
            "smoking-af",
            "portfolio-state-snapshot",
            "slate-diagram.png",
            "citekey",
            "project",
            "slate-decision",
            "example-institute.org",
        ):
            assert phantom not in scan.citekeys

    def test_prose_dois_are_extracted_in_both_written_forms(self) -> None:
        # A bare DOI in running text and one inside a doi.org autolink.
        scan = md.scan_markdown([DATA / "markdown_obsidian_note.md"])
        assert _dois(scan) == ["10.1101/2023.04.20.23288770", "10.1073/pnas.2408715121"]
        assert all(r.raw["context"] == "prose" for r in scan.references)

    def test_a_note_declares_its_own_bibliography(self, tmp_path: pathlib.Path) -> None:
        # Every one of the 28 vault notes that declares a bibliography writes
        # exactly this line, at every depth.
        (tmp_path / ".obsidian").mkdir()
        note = tmp_path / "work" / "postdoc-cnio" / "papers" / "overview.md"
        note.parent.mkdir(parents=True)
        note.write_text(
            (DATA / "markdown_obsidian_note.md").read_text(encoding="utf-8"), encoding="utf-8"
        )
        _write(tmp_path, "material/postdoc-papers.bib", "")
        assert md.scan_markdown([note]).bibliographies == [
            (tmp_path / "material" / "postdoc-papers.bib").resolve()
        ]


# ---------------------------------------------------------------------------
# Bibliography resolution
# ---------------------------------------------------------------------------


class TestBibliographyResolution:
    """Where ``bibliography:`` front matter resolves, Quarto vs. Obsidian."""

    def test_quarto_style_resolves_against_the_note_directory(
        self, tmp_path: pathlib.Path
    ) -> None:
        # No .obsidian marker anywhere above the note: Quarto's own rule
        # applies, unchanged from before this module supported Obsidian.
        note = _write(
            tmp_path,
            "papers/note.md",
            "---\nbibliography: refs.bib\n---\n\nBody [@a].\n",
        )
        _write(tmp_path, "papers/refs.bib", "")
        resolved = md.find_project_bibliography(note)
        assert resolved == [(tmp_path / "papers" / "refs.bib").resolve()]

    def test_obsidian_style_resolves_against_the_vault_root(
        self, tmp_path: pathlib.Path
    ) -> None:
        # A .obsidian directory at the vault root, with the note three levels
        # below it — the exact shape observed in a real vault, where every
        # note at every depth declares the identical vault-relative path.
        (tmp_path / ".obsidian").mkdir()
        note = _write(
            tmp_path,
            "work/project/notes/note.md",
            "---\nbibliography: material/refs.bib\n---\n\nBody [@a].\n",
        )
        _write(tmp_path, "material/refs.bib", "")
        resolved = md.find_project_bibliography(note)
        assert resolved == [(tmp_path / "material" / "refs.bib").resolve()]

    def test_obsidian_style_does_not_resolve_against_the_note_directory(
        self, tmp_path: pathlib.Path
    ) -> None:
        (tmp_path / ".obsidian").mkdir()
        note = _write(
            tmp_path,
            "work/project/notes/note.md",
            "---\nbibliography: material/refs.bib\n---\n\nBody [@a].\n",
        )
        _write(tmp_path, "material/refs.bib", "")
        resolved = md.find_project_bibliography(note)
        wrong_guess = tmp_path / "work" / "project" / "notes" / "material" / "refs.bib"
        assert resolved != [wrong_guess.resolve()]

    def test_project_config_is_walked_up_to(self, tmp_path: pathlib.Path) -> None:
        # The real Quarto catalog declares `bibliography: references.bib` once,
        # at the project root, and no source page repeats it. A scan that only
        # read each file's own front matter would call all 438 of its citekeys
        # unresolvable.
        _write(tmp_path, "_quarto.yml", "project:\n  type: website\n\nbibliography: references.bib\n")
        note = _write(tmp_path, "sources/epic.qmd", "---\ntitle: EPIC\n---\n\nBody [@a].\n")
        assert md.find_project_bibliography(note) == [(tmp_path / "references.bib").resolve()]

    def test_a_directory_argument_also_resolves_the_project_config(
        self, tmp_path: pathlib.Path
    ) -> None:
        _write(tmp_path, "_quarto.yml", "bibliography: references.bib\n")
        (tmp_path / "sources").mkdir()
        assert md.find_project_bibliography(tmp_path / "sources") == [
            (tmp_path / "references.bib").resolve()
        ]

    def test_project_config_without_the_key_stops_the_search(
        self, tmp_path: pathlib.Path
    ) -> None:
        # A _quarto.yml marks the project root. A project with no bibliography
        # of its own has none to inherit from whatever directory happens to sit
        # above it on this machine.
        _write(tmp_path, "outer.bib", "")
        _write(tmp_path, "_quarto.yml", "project:\n  type: website\n")
        note = _write(tmp_path, "sources/epic.qmd", "---\ntitle: EPIC\n---\n\nBody [@a].\n")
        assert md.find_project_bibliography(note) == []

    def test_front_matter_wins_over_the_project_config(self, tmp_path: pathlib.Path) -> None:
        _write(tmp_path, "_quarto.yml", "bibliography: project.bib\n")
        note = _write(tmp_path, "one.qmd", "---\nbibliography: own.bib\n---\n\n[@a]\n")
        assert md.find_project_bibliography(note) == [(tmp_path / "own.bib").resolve()]

    def test_bibliographies_are_deduplicated_across_files(self, tmp_path: pathlib.Path) -> None:
        _write(tmp_path, "_quarto.yml", "bibliography: references.bib\n")
        _write(tmp_path, "sources/a.qmd", "---\ntitle: A\n---\n\n[@a]\n")
        _write(tmp_path, "sources/b.qmd", "---\ntitle: B\n---\n\n[@b]\n")
        scan = md.scan_markdown([tmp_path / "sources"])
        assert scan.bibliographies == [(tmp_path / "references.bib").resolve()]


class TestBibliographyYamlShapes:
    """The three shapes ``bibliography:`` takes, and the ones it must not take."""

    @pytest.mark.parametrize(
        ("front_matter", "expected"),
        [
            ("bibliography: refs.bib", ["refs.bib"]),
            ('bibliography: "refs.bib"', ["refs.bib"]),
            ("bibliography: 'refs.bib'", ["refs.bib"]),
            ("bibliography: [a.bib, b.bib]", ["a.bib", "b.bib"]),
            ('bibliography: ["a.bib", "b.bib"]', ["a.bib", "b.bib"]),
            ("bibliography: [a.bib,\n  b.bib]", ["a.bib", "b.bib"]),
            ("bibliography:\n  - a.bib\n  - b.bib", ["a.bib", "b.bib"]),
            ("bibliography:\n- a.bib\n- b.bib", ["a.bib", "b.bib"]),
            ("bibliography:\n\n  - a.bib\n\n  - b.bib", ["a.bib", "b.bib"]),
        ],
    )
    def test_accepted_shapes(
        self, tmp_path: pathlib.Path, front_matter: str, expected: list[str]
    ) -> None:
        # The un-indented block sequence is valid YAML and identical in meaning
        # to the indented one. Rejecting it returned no bibliography at all,
        # which reports as "every citekey in this file is unresolvable" rather
        # than as "the bibliography could not be found".
        note = _write(tmp_path, "note.qmd", f"---\n{front_matter}\n---\n\n[@a]\n")
        assert [p.name for p in md.find_project_bibliography(note)] == expected

    def test_true_positive_an_unrelated_key_is_not_absorbed(
        self, tmp_path: pathlib.Path
    ) -> None:
        # Accepting an un-indented "- item" must not let `bibliography:` reach
        # forward past a sibling key and claim that key's sequence. `filters:`
        # here owns the lightbox entry, and "lightbox" is not a bibliography.
        note = _write(
            tmp_path,
            "note.qmd",
            "---\nbibliography:\nfilters:\n- lightbox\n---\n\n[@a]\n",
        )
        assert md.find_project_bibliography(note) == []

    def test_true_positive_a_document_marker_is_not_a_path(
        self, tmp_path: pathlib.Path
    ) -> None:
        # A "---" line satisfies "starts with a dash". Requiring whitespace
        # after the dash — which YAML requires too — is what stops it being
        # read as the bibliography path "--".
        _write(tmp_path, "_quarto.yml", "project:\n  type: website\nbibliography:\n---\n")
        note = _write(tmp_path, "sources/one.qmd", "---\ntitle: x\n---\n\n[@a]\n")
        assert md.find_project_bibliography(note) == []

    def test_true_positive_no_bibliography_key_at_all(self, tmp_path: pathlib.Path) -> None:
        note = _write(tmp_path, "note.qmd", "---\ntitle: x\n---\n\n[@a]\n")
        assert md.find_project_bibliography(note) == []


# ---------------------------------------------------------------------------
# YAML front matter: delimiters and nocite
# ---------------------------------------------------------------------------


class TestFrontMatterDelimiters:
    """What opens and closes a metadata block, and what only looks like it."""

    @pytest.mark.parametrize("closing", ["---", "...", "---   ", "---\t"])
    def test_accepted_closing_delimiters(
        self, tmp_path: pathlib.Path, closing: str
    ) -> None:
        # Pandoc allows blanks after the delimiter, and an editor that does not
        # strip trailing whitespace on save leaves them. Missing the closing
        # delimiter costs the file its `bibliography:` and its `nocite:` block
        # — silently, because the file still scans as prose.
        note = _write(
            tmp_path, "note.qmd", f"---\nbibliography: refs.bib\n{closing}\n\n[@a]\n"
        )
        assert md.find_project_bibliography(note) == [(tmp_path / "refs.bib").resolve()]

    def test_a_byte_order_mark_does_not_hide_the_front_matter(
        self, tmp_path: pathlib.Path
    ) -> None:
        # read_text(encoding="utf-8") keeps a BOM; only utf-8-sig drops it. The
        # opening delimiter then arrives as "﻿---" and the whole metadata
        # block, bibliography included, is invisible.
        note = _write(tmp_path, "note.qmd", "﻿---\nbibliography: refs.bib\n---\n\n[@a]\n")
        assert md.find_project_bibliography(note) == [(tmp_path / "refs.bib").resolve()]

    def test_true_positive_four_dashes_do_not_open_front_matter(
        self, tmp_path: pathlib.Path
    ) -> None:
        # Tolerating trailing whitespace must not become tolerating any run of
        # dashes: "----" is a Markdown horizontal rule, and treating the text
        # under it as metadata would silence real prose citations.
        note = _write(tmp_path, "note.qmd", "----\nbibliography: refs.bib\n----\n\n[@a]\n")
        assert md.find_project_bibliography(note) == []
        assert list(md.scan_markdown([note]).citekeys) == ["a"]

    def test_true_positive_an_indented_delimiter_is_content(
        self, tmp_path: pathlib.Path
    ) -> None:
        # Pandoc requires the delimiter at column 0. An indented "---" inside a
        # list is text, and the file has no front matter at all.
        note = _write(tmp_path, "note.qmd", "  ---\nbibliography: refs.bib\n  ---\n\n[@a]\n")
        assert md.find_project_bibliography(note) == []

    def test_true_positive_a_horizontal_rule_later_is_not_front_matter(
        self, tmp_path: pathlib.Path
    ) -> None:
        note = _write(tmp_path, "note.md", "Body [@a].\n\n---\n\nMore [@b].\n")
        scan = md.scan_markdown([note])
        assert set(scan.citekeys) == {"a", "b"}

    def test_true_positive_unterminated_front_matter_is_not_a_metadata_block(
        self, tmp_path: pathlib.Path
    ) -> None:
        # With no closing delimiter there is no block, and the rest of the file
        # must still be scanned as prose rather than swallowed as metadata.
        note = _write(tmp_path, "note.qmd", "---\ntitle: x\n\nBody [@a].\n")
        assert list(md.scan_markdown([note]).citekeys) == ["a"]

    def test_front_matter_is_masked_from_the_prose_scan(
        self, tmp_path: pathlib.Path
    ) -> None:
        # A YAML value can contain an "@" in a citation-shaped position, and
        # metadata is not prose.
        note = _write(
            tmp_path,
            "note.qmd",
            "---\ntitle: x\nnote: [@notacitation]\n---\n\nReal [@real2020].\n",
        )
        assert list(md.scan_markdown([note]).citekeys) == ["real2020"]


class TestNocite:
    """``nocite:`` names works that are in the bibliography but never cited."""

    def test_block_scalar_over_several_lines(self, tmp_path: pathlib.Path) -> None:
        note = _write(
            tmp_path,
            "note.qmd",
            "---\ntitle: x\nnocite: |\n  @a1, @a2\n  @a3\n---\n\nBody.\n",
        )
        scan = md.scan_markdown([note])
        assert scan.citekeys == {
            "a1": ["note.qmd:4"],
            "a2": ["note.qmd:4"],
            "a3": ["note.qmd:5"],
        }

    def test_block_scalar_survives_a_blank_line(self, tmp_path: pathlib.Path) -> None:
        # YAML lets a block literal contain blank lines; only a line starting
        # at column 0 ends it.
        note = _write(
            tmp_path,
            "note.qmd",
            "---\nnocite: |\n  @a1\n\n  @a2\ntitle: x\n---\n\nBody.\n",
        )
        scan = md.scan_markdown([note])
        assert set(scan.citekeys) == {"a1", "a2"}

    @pytest.mark.parametrize("indicator", ["|", "|-", "|+", ">", ">-", ">+"])
    def test_every_block_scalar_indicator(
        self, tmp_path: pathlib.Path, indicator: str
    ) -> None:
        note = _write(tmp_path, "note.qmd", f"---\nnocite: {indicator}\n  @a1\n---\n\nBody.\n")
        assert list(md.scan_markdown([note]).citekeys) == ["a1"]

    @pytest.mark.parametrize("quote", ['"', "'", ""])
    def test_inline_scalar(self, tmp_path: pathlib.Path, quote: str) -> None:
        # The key flush against the opening quote is the one at risk: a
        # citekey opens a match only after a delimiter or at the start of the
        # scanned string, so `"@a1, @a2"` read with its quotes still on lost
        # `@a1` and kept `@a2` — half a nocite block, silently.
        note = _write(tmp_path, "note.qmd", f"---\nnocite: {quote}@a1, @a2{quote}\n---\n\nBody.\n")
        scan = md.scan_markdown([note])
        assert scan.citekeys == {"a1": ["note.qmd:2"], "a2": ["note.qmd:2"]}

    def test_true_positive_an_empty_nocite_names_no_key(
        self, tmp_path: pathlib.Path
    ) -> None:
        # Stripping the quotes must not turn a quoted empty scalar, or a bare
        # `nocite:` with nothing under it, into a phantom citation.
        for value in ('""', "''", ""):
            note = _write(tmp_path, f"note{len(value)}.qmd", f"---\nnocite: {value}\ntitle: x\n---\n\nBody.\n")
            assert md.scan_markdown([note]).citekeys == {}

    def test_nocite_keys_are_counted_once_not_twice(self, tmp_path: pathlib.Path) -> None:
        # The front matter is masked before the prose scan runs, so the
        # comma-separated block cannot be read a second time by the generic
        # scanner — a key listed once must show one locator, not two.
        note = _write(tmp_path, "note.qmd", "---\nnocite: |\n  @a1, @a2\n---\n\nBody.\n")
        scan = md.scan_markdown([note])
        assert [len(v) for v in scan.citekeys.values()] == [1, 1]

    def test_a_nocite_key_also_cited_in_prose_keeps_both_sites(
        self, tmp_path: pathlib.Path
    ) -> None:
        note = _write(tmp_path, "note.qmd", "---\nnocite: |\n  @a1\n---\n\nBody [@a1].\n")
        assert md.scan_markdown([note]).citekeys == {"a1": ["note.qmd:3", "note.qmd:6"]}

    def test_wildcard_nocite_names_no_key(self, tmp_path: pathlib.Path) -> None:
        # "nocite: | @*" means "everything in the bibliography". There is no
        # specific key to check, and inventing one called "*" would be a
        # phantom citation in every report of every file that uses it.
        note = _write(tmp_path, "note.qmd", "---\nnocite: |\n  @*\n---\n\nBody.\n")
        assert md.scan_markdown([note]).citekeys == {}

    def test_only_the_first_nocite_key_is_read(self, tmp_path: pathlib.Path) -> None:
        # A YAML mapping has at most one `nocite:`; a second is a malformed
        # document, and guessing which one pandoc would honour is not this
        # module's job.
        note = _write(
            tmp_path, "note.qmd", "---\nnocite: |\n  @a1\nnocite: |\n  @a2\n---\n\nBody.\n"
        )
        assert list(md.scan_markdown([note]).citekeys) == ["a1"]

    def test_cross_references_are_excluded_from_nocite_too(
        self, tmp_path: pathlib.Path
    ) -> None:
        note = _write(tmp_path, "note.qmd", "---\nnocite: |\n  @a1, @fig-one\n---\n\nBody.\n")
        assert list(md.scan_markdown([note]).citekeys) == ["a1"]

    def test_a_file_without_nocite_is_unaffected(self, tmp_path: pathlib.Path) -> None:
        note = _write(tmp_path, "note.qmd", "---\ntitle: x\n---\n\nBody [@a].\n")
        assert list(md.scan_markdown([note]).citekeys) == ["a"]


# ---------------------------------------------------------------------------
# Fenced code blocks
# ---------------------------------------------------------------------------


class TestFences:
    """Fenced code is stripped for citekeys but not for DOIs."""

    def test_a_citekey_inside_a_fence_is_not_a_citation(
        self, tmp_path: pathlib.Path
    ) -> None:
        # Observed in the vault: an annotated DAG edge list inside a plain
        # fence carries "@carrerastorres2017mr" as a note to the author.
        # Pandoc renders that literally — it is not a citation, and neither
        # this tool nor the renderer should treat it as one.
        note = _write(
            tmp_path,
            "note.md",
            "```\nU -> Y  (fasting insulin causal, @carrerastorres2017mr)\n```\n\nReal [@real2020].\n",
        )
        assert list(md.scan_markdown([note]).citekeys) == ["real2020"]

    def test_a_doi_inside_a_fence_is_still_extracted(self, tmp_path: pathlib.Path) -> None:
        # The asymmetry that matters: the publications tables live inside R
        # chunks, so masking fences for the DOI pass would hide every DOI in
        # the entire Quarto catalog.
        note = _write(tmp_path, "note.qmd", _chunk('doi <- "10.1234/inside"'))
        scan = md.scan_markdown([note])
        assert _dois(scan) == ["10.1234/inside"]
        assert scan.references[0].raw == {"context": "code-chunk"}

    def test_a_doi_flush_against_its_closing_backtick(self, tmp_path: pathlib.Path) -> None:
        # normalize.DOI_PATTERN's exclusion class does not name the backtick,
        # so `10.1234/x` with no intervening space would otherwise swallow it.
        note = _write(tmp_path, "note.md", "See `10.1234/flush` for the record.\n")
        assert _dois(md.scan_markdown([note])) == ["10.1234/flush"]

    def test_a_parenthesised_lancet_doi_survives_prose(self, tmp_path: pathlib.Path) -> None:
        note = _write(tmp_path, "note.md", "The trial (10.1016/S0140-6736(03)14065-2) reported.\n")
        assert _dois(md.scan_markdown([note])) == ["10.1016/s0140-6736(03)14065-2"]

    def test_tilde_fence(self, tmp_path: pathlib.Path) -> None:
        note = _write(tmp_path, "note.md", "~~~\n@notacitekey\n~~~\n\nReal [@real2020].\n")
        assert list(md.scan_markdown([note]).citekeys) == ["real2020"]

    def test_a_longer_fence_encloses_a_shorter_one(self, tmp_path: pathlib.Path) -> None:
        # Four backticks wrapping a three-backtick example: the inner fence
        # must not be read as the closing one, or the "@notacitekey" after it
        # leaks out as a citation.
        note = _write(
            tmp_path,
            "note.md",
            "````\n```{r}\nx <- 1\n```\n@notacitekey\n````\n\nReal [@real2020].\n",
        )
        assert list(md.scan_markdown([note]).citekeys) == ["real2020"]

    def test_a_closing_fence_may_carry_trailing_whitespace(
        self, tmp_path: pathlib.Path
    ) -> None:
        note = _write(tmp_path, "note.qmd", "```{r}\nx <- 1\n```   \n\nReal [@real2020].\n")
        assert md.scan_markdown([note]).citekeys == {"real2020": ["note.qmd:5"]}

    def test_an_indented_fence_inside_a_list(self, tmp_path: pathlib.Path) -> None:
        note = _write(
            tmp_path,
            "note.qmd",
            "- item:\n\n  ```{r}\n  doi <- \"10.1234/indented\"\n  ```\n\nReal [@real2020].\n",
        )
        scan = md.scan_markdown([note])
        assert list(scan.citekeys) == ["real2020"]
        assert _dois(scan) == ["10.1234/indented"]

    def test_an_unterminated_fence_masks_to_the_end_of_the_file(
        self, tmp_path: pathlib.Path
    ) -> None:
        # A broken document must not leak its code into plain-prose scanning;
        # its DOIs are still read, because that pass never masked fences.
        note = _write(tmp_path, "note.qmd", 'Real [@real2020].\n\n```{r}\nx <- "10.1234/z"\n@notakey\n')
        scan = md.scan_markdown([note])
        assert list(scan.citekeys) == ["real2020"]
        assert _dois(scan) == ["10.1234/z"]

    def test_consecutive_fences_are_each_scanned(self, tmp_path: pathlib.Path) -> None:
        note = _write(
            tmp_path,
            "note.qmd",
            _chunk('a <- "10.1234/one"') + "\nProse.\n\n" + _chunk('b <- "10.1234/two"'),
        )
        assert _dois(md.scan_markdown([note])) == ["10.1234/one", "10.1234/two"]

    def test_an_empty_fence_body_is_skipped(self, tmp_path: pathlib.Path) -> None:
        note = _write(tmp_path, "note.qmd", "```{r}\n```\n\nReal [@real2020].\n")
        scan = md.scan_markdown([note])
        assert scan.references == []
        assert list(scan.citekeys) == ["real2020"]


# ---------------------------------------------------------------------------
# read.delim(text = "...") publication tables
# ---------------------------------------------------------------------------


class TestReadDelimTables:
    """The pipe-table shape, as EPIC and Million Women Study write it."""

    def test_epic_fixture_rows(self) -> None:
        scan = md.scan_markdown([DATA / "markdown_epic_read_delim.qmd"])
        assert _dois(scan) == [
            "10.1093/ije/26.suppl_1.s6",
            "10.1079/phn2002394",
            "10.1158/1055-9965.epi-05-0800",
            "10.1093/ije/dyab115",
            "10.1002/ijc.70581",
        ]

    def test_epic_fixture_fields_come_from_the_named_columns(self) -> None:
        # Year is column 0, Journal column 6 and DOI column 7 in this table.
        scan = md.scan_markdown([DATA / "markdown_epic_read_delim.qmd"])
        ref = _by_doi(scan, "10.1093/ije/dyab115")
        assert (ref.year, ref.container, ref.kind) == (2022, "Int J Epidemiol", "article")
        assert ref.raw["Theme"] == "Biomarkers"
        assert ref.raw["N"] == "513 / 1,020"

    def test_epic_fixture_locators_point_at_the_row(self) -> None:
        # One locator per row, not one per table: a human fixing a wrong year
        # has to be sent to the line carrying it.
        scan = md.scan_markdown([DATA / "markdown_epic_read_delim.qmd"])
        assert [r.locator for r in scan.references] == [
            "markdown_epic_read_delim.qmd:24",
            "markdown_epic_read_delim.qmd:25",
            "markdown_epic_read_delim.qmd:26",
            "markdown_epic_read_delim.qmd:27",
            "markdown_epic_read_delim.qmd:28",
        ]

    def test_a_different_column_set_is_read_from_its_own_header(self) -> None:
        # Million Women Study has no Data or N column, so Journal sits at index
        # 4 and DOI at index 5 — against EPIC's 6 and 7. Any hardcoded index
        # would put the Topic text into `container` for one of the two files.
        scan = md.scan_markdown([DATA / "markdown_mws_read_delim.qmd"])
        ref = _by_doi(scan, "10.1002/mds.27933")
        assert (ref.year, ref.container) == (2020, "Mov Disord")
        assert "N" not in ref.raw

    def test_a_lancet_doi_with_parentheses_survives_the_table(self) -> None:
        # 10.1016/S0140-6736(03)14065-2 is real and common in epidemiology;
        # counting its parens as call nesting would close read.delim() early
        # and cut off every row after it.
        scan = md.scan_markdown([DATA / "markdown_mws_read_delim.qmd"])
        assert "10.1016/s0140-6736(03)14065-2" in _dois(scan)
        assert len(scan.references) == 5  # nothing was truncated

    def test_an_apostrophe_in_a_cell_does_not_truncate_the_table(self) -> None:
        # "Alcohol intake & Parkinson's disease risk" sits inside the
        # double-quoted literal; its apostrophe must not be read as a quote.
        scan = md.scan_markdown([DATA / "markdown_mws_read_delim.qmd"])
        assert _by_doi(scan, "10.1002/mds.27933").raw["Topic"].endswith("disease risk")
        assert "10.1016/s2468-2667(20)30284-x" in _dois(scan)  # the row after it

    def test_the_separator_may_be_declared_before_the_text(
        self, tmp_path: pathlib.Path
    ) -> None:
        # `sep` is an ordinary named argument; R does not care about order.
        # Read only after the literal, a leading `sep = "|"` fell back to the
        # tab default, the one-column header had no DOI role, and the table's
        # Year/Journal/Author were dropped without a word.
        note = _write(
            tmp_path,
            "note.qmd",
            _chunk('pubs <- read.delim(sep = "|", text =\n"Year|Journal|DOI\n2020|Int J Cancer|10.1234/a\n")'),
        )
        ref = _by_doi(md.scan_markdown([note]), "10.1234/a")
        assert (ref.kind, ref.year, ref.container) == ("article", 2020, "Int J Cancer")

    def test_true_positive_an_undeclared_separator_stays_a_tab(
        self, tmp_path: pathlib.Path
    ) -> None:
        # read.delim's own default. Searching both sides of the literal for
        # `sep` must not start inventing one: a genuinely tab-separated table
        # is parsed as tabs, and a pipe-separated table that forgot to say so
        # is one column wide — which is what R would do too.
        tabbed = _chunk('read.delim(text = "Year\tJournal\tDOI\n2020\tInt J Cancer\t10.1234/tabbed\n")')
        ref = _by_doi(md.scan_markdown([_write(tmp_path, "t.qmd", tabbed)]), "10.1234/tabbed")
        assert (ref.kind, ref.year, ref.container) == ("article", 2020, "Int J Cancer")

        piped = _chunk('read.delim(text = "Year|Journal|DOI\n2020|Int J Cancer|10.1234/piped\n")')
        ref = _by_doi(md.scan_markdown([_write(tmp_path, "p.qmd", piped)]), "10.1234/piped")
        assert (ref.kind, ref.year, ref.container) == ("other", None, None)

    def test_a_sep_written_inside_a_cell_is_text_not_an_argument(
        self, tmp_path: pathlib.Path
    ) -> None:
        note = _write(
            tmp_path,
            "note.qmd",
            _chunk('read.delim(text = "Year|Topic|Journal|DOI\n2020|sep = \\";\\" handling|Int J Cancer|10.1234/a\n", sep = "|")'),
        )
        ref = _by_doi(md.scan_markdown([note]), "10.1234/a")
        assert (ref.year, ref.container) == (2020, "Int J Cancer")

    def test_escaped_newlines_and_tabs_build_the_table(
        self, tmp_path: pathlib.Path
    ) -> None:
        # The idiomatic one-line spelling. Appending the escaped character
        # itself turned the whole literal into the single nonsense header
        # "YeartJournaltDOIn2020t..." — no DOI column, so the table silently
        # became bare DOIs with nothing to check against the registry.
        note = _write(
            tmp_path,
            "note.qmd",
            _chunk(r'read.delim(text = "Year\tJournal\tDOI\n2020\tInt J Cancer\t10.1234/escaped\n")'),
        )
        ref = _by_doi(md.scan_markdown([note]), "10.1234/escaped")
        assert (ref.kind, ref.year, ref.container) == ("article", 2020, "Int J Cancer")

    def test_a_literal_opening_on_its_own_line(self, tmp_path: pathlib.Path) -> None:
        # Ordinary R style, and `read.delim` skips the blank first line itself
        # (blank.lines.skip defaults to TRUE). Reading it as the header gave
        # one unnamed column and dropped the table.
        note = _write(
            tmp_path,
            "note.qmd",
            _chunk('read.delim(text = "\nYear|Journal|DOI\n2020|Int J Cancer|10.1234/a\n", sep = "|")'),
        )
        ref = _by_doi(md.scan_markdown([note]), "10.1234/a")
        assert (ref.kind, ref.year, ref.container) == ("article", 2020, "Int J Cancer")
        # The skipped blank row still counts towards the row-to-line mapping:
        # the fence opens on line 1, the literal on line 2, the header on
        # line 3, so this row is on line 4.
        assert ref.locator == "note.qmd:4"

    def test_true_positive_an_entirely_blank_literal_yields_nothing(
        self, tmp_path: pathlib.Path
    ) -> None:
        note = _write(tmp_path, "note.qmd", _chunk('read.delim(text = "\n\n", sep = "|")'))
        assert md.scan_markdown([note]).references == []

    def test_true_positive_a_table_without_a_doi_column_is_not_a_publications_table(
        self, tmp_path: pathlib.Path
    ) -> None:
        # A cohort's variable dictionary is also a read.delim table. Emitting
        # its rows as references would put unverifiable rows into the report.
        note = _write(
            tmp_path,
            "note.qmd",
            _chunk('read.delim(text = "Variable|Label|Year\nage|Age at recruitment|2020\n", sep = "|")'),
        )
        assert md.scan_markdown([note]).references == []

    def test_a_bare_doi_in_a_non_publications_table_is_still_swept_up(
        self, tmp_path: pathlib.Path
    ) -> None:
        # Dropping the table must not drop the DOI: it falls back to an
        # identifier-only reference rather than disappearing.
        note = _write(
            tmp_path,
            "note.qmd",
            _chunk('read.delim(text = "Variable|Source\nage|10.1234/loose\n", sep = "|")'),
        )
        scan = md.scan_markdown([note])
        assert _dois(scan) == ["10.1234/loose"]
        assert scan.references[0].kind == "other"

    def test_a_short_row_does_not_crash_or_invent_fields(
        self, tmp_path: pathlib.Path
    ) -> None:
        note = _write(
            tmp_path,
            "note.qmd",
            _chunk('read.delim(text = "Year|Journal|DOI\n2020|Int J Cancer|10.1234/full\n2021|10.1234/short\n", sep = "|")'),
        )
        scan = md.scan_markdown([note])
        assert _dois(scan) == ["10.1234/full", "10.1234/short"]

    def test_a_row_with_no_doi_value_keeps_a_locator_as_its_key(
        self, tmp_path: pathlib.Path
    ) -> None:
        # A blank DOI cell is a real gap in the table, not a reason to drop the
        # row: the reference still has to appear in the report so somebody sees
        # that this paper has no identifier.
        note = _write(
            tmp_path,
            "note.qmd",
            _chunk('read.delim(text = "Year|Journal|DOI\n2020|Int J Cancer|\n", sep = "|")'),
        )
        (ref,) = md.scan_markdown([note]).references
        assert ref.doi is None
        assert ref.key == f"row:{ref.locator}"

    def test_a_comment_before_the_call_does_not_disturb_it(
        self, tmp_path: pathlib.Path
    ) -> None:
        note = _write(
            tmp_path,
            "note.qmd",
            _chunk('# won\'t reorder these\nread.delim(text = "Year|Journal|DOI\n2020|Int J Cancer|10.1234/a\n", sep = "|")'),
        )
        ref = _by_doi(md.scan_markdown([note]), "10.1234/a")
        assert (ref.year, ref.container) == (2020, "Int J Cancer")


# ---------------------------------------------------------------------------
# data.frame(DOI = c(...)) publication tables
# ---------------------------------------------------------------------------


class TestDataFrameVectors:
    """The vector shape, as PanGenEU and eleven other sources write it."""

    def test_pangeneu_fixture_rows(self) -> None:
        scan = md.scan_markdown([DATA / "markdown_pangeneu_data_frame.qmd"])
        assert _dois(scan) == [
            "10.1093/annonc/mdx167",
            "10.1136/gutjnl-2015-310442",
            "10.1093/ije/dyx269",
            "10.1158/1055-9965.epi-20-0378",
            "10.3389/fgene.2021.693933",
            "10.1158/1055-9965.epi-25-1601",
        ]

    def test_pangeneu_fixture_pairs_siblings_positionally(self) -> None:
        scan = md.scan_markdown([DATA / "markdown_pangeneu_data_frame.qmd"])
        paired = [(r.doi, r.year, r.container) for r in scan.references]
        assert paired[0] == ("10.1093/annonc/mdx167", 2017, "Ann Oncol")
        assert paired[1] == ("10.1136/gutjnl-2015-310442", 2017, "Gut")
        assert paired[-1] == ("10.1158/1055-9965.epi-25-1601", 2026, "CEBP")

    def test_pangeneu_fixture_locators_follow_the_vector_across_lines(self) -> None:
        # The DOI vector wraps over three source lines; a row must carry the
        # line its own DOI is written on, not the vector's opening line.
        scan = md.scan_markdown([DATA / "markdown_pangeneu_data_frame.qmd"])
        assert [r.locator.rsplit(":", 1)[1] for r in scan.references] == [
            "28", "28", "28", "29", "29", "30",
        ]

    def test_columns_with_no_reference_role_stay_in_raw(self) -> None:
        # Theme, Topic and Role are this project's own bookkeeping. They are
        # not registry-checkable fields and must not be invented into any.
        scan = md.scan_markdown([DATA / "markdown_pangeneu_data_frame.qmd"])
        ref = _by_doi(scan, "10.3389/fgene.2021.693933")
        assert ref.raw["Role"] == "Replication"
        assert ref.raw["Theme"] == "Genetics & omics"

    def test_true_positive_a_short_sibling_stops_positional_pairing(
        self, tmp_path: pathlib.Path
    ) -> None:
        # Somebody added a DOI without adding its journal. Position i in
        # Journal no longer describes position i in DOI, so pairing anyway
        # would attach the first paper's journal to the second paper — a
        # field mismatch reported against a bibliography that is right.
        note = _write(
            tmp_path,
            "note.qmd",
            _chunk(
                'd <- data.frame(\n'
                '  Year = c(2020, 2021),\n'
                '  Journal = c("Int J Cancer"),\n'
                '  DOI = c("10.1234/a", "10.1234/b")\n'
                ')'
            ),
        )
        scan = md.scan_markdown([note])
        assert len(scan.references) == 2
        for ref in scan.references:
            assert (ref.year, ref.container, ref.authors, ref.kind) == (None, None, [], "other")
            assert "Journal (length 1 != DOI's 2)" in ref.raw["warning"]

    def test_true_positive_a_long_sibling_stops_positional_pairing(
        self, tmp_path: pathlib.Path
    ) -> None:
        note = _write(
            tmp_path,
            "note.qmd",
            _chunk(
                'd <- data.frame(\n'
                '  Year = c(2020, 2021, 2022),\n'
                '  DOI = c("10.1234/a", "10.1234/b")\n'
                ')'
            ),
        )
        scan = md.scan_markdown([note])
        assert all(r.year is None for r in scan.references)
        assert "Year (length 3 != DOI's 2)" in scan.references[0].raw["warning"]

    def test_equal_length_siblings_do_pair(self, tmp_path: pathlib.Path) -> None:
        # The benign case the check above must not swallow: this is how every
        # one of the twelve real data.frame tables is written.
        note = _write(
            tmp_path,
            "note.qmd",
            _chunk(
                'd <- data.frame(\n'
                '  Year = c(2020, 2021),\n'
                '  Authors = c("Gomez-Rubio et al.", "Lu et al."),\n'
                '  Journal = c("Gut", "Front Genet"),\n'
                '  DOI = c("10.1234/a", "10.1234/b")\n'
                ')'
            ),
        )
        scan = md.scan_markdown([note])
        assert [(r.year, r.container) for r in scan.references] == [
            (2020, "Gut"),
            (2021, "Front Genet"),
        ]
        assert all("warning" not in r.raw for r in scan.references)

    def test_a_data_frame_without_a_doi_vector_yields_no_rows(
        self, tmp_path: pathlib.Path
    ) -> None:
        note = _write(
            tmp_path,
            "note.qmd",
            _chunk('d <- data.frame(Year = c(2020), Journal = c("Gut"))'),
        )
        assert md.scan_markdown([note]).references == []

    def test_an_apostrophe_in_a_comment_does_not_merge_two_calls(
        self, tmp_path: pathlib.Path
    ) -> None:
        # "# don't reorder these" opens a single-quoted string that never
        # closes. The first call then ran to the end of the chunk and absorbed
        # the second call's vectors: the second table's row was emitted twice
        # and the first table lost its Year, Journal and Authors entirely.
        note = _write(
            tmp_path,
            "note.qmd",
            _chunk(
                'first <- data.frame(\n'
                "  # don't reorder these\n"
                '  Year = c(2001),\n'
                '  Journal = c("First J"),\n'
                '  DOI = c("10.1234/first")\n'
                ')\n'
                'second <- data.frame(\n'
                '  Year = c(2002),\n'
                '  Journal = c("Second J"),\n'
                '  DOI = c("10.1234/second")\n'
                ')'
            ),
        )
        scan = md.scan_markdown([note])
        assert [(r.doi, r.year, r.container) for r in scan.references] == [
            ("10.1234/first", 2001, "First J"),
            ("10.1234/second", 2002, "Second J"),
        ]

    def test_a_trailing_comment_inside_a_vector_is_not_an_element(
        self, tmp_path: pathlib.Path
    ) -> None:
        note = _write(
            tmp_path,
            "note.qmd",
            _chunk(
                'd <- data.frame(\n'
                '  Year = c(2020, # provisional\n'
                '           2021),\n'
                '  DOI = c("10.1234/a", "10.1234/b")\n'
                ')'
            ),
        )
        scan = md.scan_markdown([note])
        assert [(r.doi, r.year) for r in scan.references] == [
            ("10.1234/a", 2020),
            ("10.1234/b", 2021),
        ]

    def test_a_doi_vector_element_with_an_embedded_paren(
        self, tmp_path: pathlib.Path
    ) -> None:
        note = _write(
            tmp_path,
            "note.qmd",
            _chunk(
                'd <- data.frame(\n'
                '  Journal = c("Lancet"),\n'
                '  DOI = c("10.1016/S0140-6736(03)14065-2")\n'
                ')'
            ),
        )
        scan = md.scan_markdown([note])
        assert _dois(scan) == ["10.1016/s0140-6736(03)14065-2"]
        assert scan.references[0].container == "Lancet"

    def test_the_authors_column_is_read_under_either_spelling(
        self, tmp_path: pathlib.Path
    ) -> None:
        for name in ("Author", "Authors"):
            note = _write(
                tmp_path,
                f"note-{name}.qmd",
                _chunk(f'd <- data.frame({name} = c("Riboli et al."), DOI = c("10.1234/a"))'),
            )
            (ref,) = md.scan_markdown([note]).references
            assert ref.authors, f"{name} column was not read"


class TestMalformedRSource:
    """A chunk this adapter cannot parse must degrade, never crash or invent.

    Every case here loses the table's Year/Journal/Author, which is the right
    trade: an identifier-only reference is still checked against the registry
    and still reported, whereas a guessed pairing would be a field mismatch
    attributed to a bibliography that is correct.
    """

    def test_read_delim_reading_a_file_is_not_a_text_table(
        self, tmp_path: pathlib.Path
    ) -> None:
        # `read.delim("pubs.tsv")` has no inline table at all: the rows live in
        # a file this adapter deliberately does not open.
        note = _write(
            tmp_path,
            "note.qmd",
            _chunk('d <- read.delim("pubs.tsv", sep = "|")\nextra <- "10.1234/loose"'),
        )
        scan = md.scan_markdown([note])
        assert [(r.doi, r.kind) for r in scan.references] == [("10.1234/loose", "other")]

    def test_a_comment_between_the_keyword_and_its_literal(
        self, tmp_path: pathlib.Path
    ) -> None:
        note = _write(
            tmp_path,
            "note.qmd",
            _chunk('read.delim(text = # the table follows\n"Year|Journal|DOI\n2020|Gut|10.1234/a\n", sep = "|")'),
        )
        ref = _by_doi(md.scan_markdown([note]), "10.1234/a")
        assert (ref.year, ref.container) == (2020, "Gut")

    def test_an_unterminated_call_still_yields_its_rows(
        self, tmp_path: pathlib.Path
    ) -> None:
        note = _write(
            tmp_path,
            "note.qmd",
            _chunk('d <- data.frame(\n  Year = c(2020),\n  DOI = c("10.1234/a")'),
        )
        (ref,) = md.scan_markdown([note]).references
        assert (ref.doi, ref.year) == ("10.1234/a", 2020)

    def test_an_unterminated_string_falls_back_to_the_doi_sweep(
        self, tmp_path: pathlib.Path
    ) -> None:
        note = _write(
            tmp_path,
            "note.qmd",
            _chunk('read.delim(text = "Year|Journal|DOI\n2020|Gut|10.1234/a\n, sep = "|")'),
        )
        scan = md.scan_markdown([note])
        assert [(r.doi, r.kind) for r in scan.references] == [("10.1234/a", "other")]

    def test_a_literal_that_never_closes(self, tmp_path: pathlib.Path) -> None:
        note = _write(
            tmp_path, "note.qmd", _chunk('read.delim(text = "Year|Journal|DOI\n2020|Gut|10.1234/a')
        )
        scan = md.scan_markdown([note])
        assert [(r.doi, r.kind) for r in scan.references] == [("10.1234/a", "other")]

    def test_a_table_passed_as_a_variable_is_not_inlined(
        self, tmp_path: pathlib.Path
    ) -> None:
        # `read.delim(text = tbl)` builds the table from a value this adapter
        # cannot see. Guessing at it is exactly the kind of inference the
        # no-model rule forbids.
        note = _write(
            tmp_path,
            "note.qmd",
            _chunk('read.delim(text = tbl)\nextra <- "10.1234/loose"'),
        )
        scan = md.scan_markdown([note])
        assert [(r.doi, r.kind) for r in scan.references] == [("10.1234/loose", "other")]

    def test_an_escaped_quote_inside_a_vector_element(
        self, tmp_path: pathlib.Path
    ) -> None:
        note = _write(
            tmp_path,
            "note.qmd",
            _chunk('d <- data.frame(Journal = c("The \\"Lancet\\""), DOI = c("10.1234/a"))'),
        )
        (ref,) = md.scan_markdown([note]).references
        assert ref.container == 'The "Lancet"'

    def test_a_vector_element_and_a_table_cell_decode_escapes_alike(
        self, tmp_path: pathlib.Path
    ) -> None:
        # Two readers of the same R string grammar must not disagree, or the
        # same journal reads as "Int JnCancer" from a vector and
        # "Int J Cancer" from a read.delim cell — and one of the two then
        # reports a container mismatch the registry never caused.
        vector = _write(
            tmp_path,
            "vec.qmd",
            _chunk(r'd <- data.frame(Journal = c("Int J\nCancer"), DOI = c("10.1234/a"))'),
        )
        table = _write(
            tmp_path,
            "tab.qmd",
            _chunk(r'read.delim(text = "Journal|DOI\nInt J Cancer|10.1234/a\n", sep = "|")'),
        )
        assert md.scan_markdown([vector]).references[0].container == "Int J Cancer"
        assert md.scan_markdown([table]).references[0].container == "Int J Cancer"


class TestCodeChunkLeftovers:
    """DOIs typed into a chunk outside either recognised table shape."""

    def test_a_bare_assignment_is_still_a_reference(self, tmp_path: pathlib.Path) -> None:
        note = _write(tmp_path, "note.qmd", _chunk('doi <- "10.1234/loose"'))
        (ref,) = md.scan_markdown([note]).references
        assert (ref.doi, ref.kind, ref.raw) == ("10.1234/loose", "other", {"context": "code-chunk"})

    def test_a_doi_in_an_r_comment_is_still_a_reference(
        self, tmp_path: pathlib.Path
    ) -> None:
        # Comment-skipping exists to fix call-boundary scanning; it must not
        # start hiding DOIs, which are found by a separate sweep over the raw
        # chunk text.
        note = _write(tmp_path, "note.qmd", _chunk("# source: 10.1234/commented"))
        assert _dois(md.scan_markdown([note])) == ["10.1234/commented"]

    def test_a_table_doi_is_not_emitted_twice(self, tmp_path: pathlib.Path) -> None:
        # The structured pass records what it consumed so the leftover sweep
        # cannot re-emit the same DOI stripped of its Year and Journal.
        note = _write(
            tmp_path,
            "note.qmd",
            _chunk('read.delim(text = "Year|Journal|DOI\n2020|Gut|10.1234/a\n", sep = "|")'),
        )
        scan = md.scan_markdown([note])
        assert _dois(scan) == ["10.1234/a"]
        assert scan.references[0].kind == "article"

    def test_a_repeated_loose_doi_is_emitted_once_per_chunk(
        self, tmp_path: pathlib.Path
    ) -> None:
        note = _write(tmp_path, "note.qmd", _chunk('a <- "10.1234/x"\nb <- "10.1234/x"'))
        assert _dois(md.scan_markdown([note])) == ["10.1234/x"]


# ---------------------------------------------------------------------------
# Path collection, locators, and the whole-corpus contract
# ---------------------------------------------------------------------------


class TestPathCollection:
    """Which files are read, in what order, and how they are named in a report."""

    def test_a_directory_is_walked_for_both_extensions(self, tmp_path: pathlib.Path) -> None:
        _write(tmp_path, "src/b.qmd", "[@b]\n")
        _write(tmp_path, "src/a.md", "[@a]\n")
        _write(tmp_path, "src/notes.txt", "[@ignored]\n")
        scan = md.scan_markdown([tmp_path / "src"])
        assert set(scan.citekeys) == {"a", "b"}

    def test_directory_contents_are_walked_in_sorted_order(
        self, tmp_path: pathlib.Path
    ) -> None:
        # A filesystem walk order is not deterministic and this tool's output
        # must be, or two runs over an unchanged corpus disagree.
        for name in ("z.qmd", "a.qmd", "m.qmd"):
            _write(tmp_path, f"src/{name}", f"[@key-{name[0]}]\n")
        scan = md.scan_markdown([tmp_path / "src"])
        assert list(scan.citekeys) == ["key-a", "key-m", "key-z"]

    def test_a_file_named_twice_is_read_once(self, tmp_path: pathlib.Path) -> None:
        note = _write(tmp_path, "src/a.qmd", "[@a]\n")
        scan = md.scan_markdown([note, tmp_path / "src", note])
        assert scan.citekeys == {"a": ["a.qmd:1"]}

    def test_locators_are_relative_to_the_common_root_with_forward_slashes(
        self, tmp_path: pathlib.Path
    ) -> None:
        _write(tmp_path, "src/one/a.qmd", "[@a]\n")
        _write(tmp_path, "src/two/b.qmd", "[@b]\n")
        scan = md.scan_markdown([tmp_path / "src"])
        assert scan.citekeys == {"a": ["one/a.qmd:1"], "b": ["two/b.qmd:1"]}

    def test_an_empty_file_contributes_nothing(self, tmp_path: pathlib.Path) -> None:
        note = _write(tmp_path, "empty.qmd", "")
        scan = md.scan_markdown([note])
        assert (scan.citekeys, scan.references, scan.bibliographies) == ({}, [], [])

    def test_scanning_no_paths_at_all(self) -> None:
        scan = md.scan_markdown([])
        assert (scan.citekeys, scan.references, scan.bibliographies) == ({}, [], [])


class TestCorpusContract:
    """The three R-table fixtures read together, as a corpus scan reads them."""

    def test_totals_across_the_fixtures(self) -> None:
        scan = md.scan_markdown(
            [
                DATA / "markdown_epic_read_delim.qmd",
                DATA / "markdown_mws_read_delim.qmd",
                DATA / "markdown_pangeneu_data_frame.qmd",
            ]
        )
        assert len(scan.references) == 16
        assert len({r.doi for r in scan.references}) == 16
        # Every row of a publications table is a journal article with a year,
        # an author list and a journal — the state the real 442-row scan of the
        # Quarto catalog reaches for every single one of its rows.
        assert all(r.kind == "article" for r in scan.references)
        assert all(r.year and r.authors and r.container for r in scan.references)

    def test_no_cross_reference_reaches_the_citekeys(self) -> None:
        scan = md.scan_markdown(
            [
                DATA / "markdown_epic_read_delim.qmd",
                DATA / "markdown_mws_read_delim.qmd",
                DATA / "markdown_pangeneu_data_frame.qmd",
            ]
        )
        assert set(scan.citekeys) == {"riboli1997epic", "slimani2002calibration"}


class TestDeprecatedAliases:
    """The old Quarto-only names must keep working after the rename."""

    def test_scan_quarto_is_scan_markdown(self) -> None:
        assert md.scan_quarto is md.scan_markdown

    def test_quarto_scan_is_markdown_scan(self) -> None:
        assert md.QuartoScan is md.MarkdownScan

    def test_scan_quarto_still_callable(self, tmp_path: pathlib.Path) -> None:
        note = _write(tmp_path, "note.md", "Body [@a].\n")
        scan = md.scan_quarto([note])
        assert isinstance(scan, md.QuartoScan)
        assert list(scan.citekeys) == ["a"]
