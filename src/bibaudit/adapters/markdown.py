"""Read a Quarto or Obsidian Markdown source tree into citekeys and inline references.

Two public functions and one result type:

``scan_markdown(paths)``
    Walk *paths* (files and/or directories) and return a :class:`MarkdownScan`:
    every citekey used in the prose — bracketed (``[@a; @b]``), bare
    (``@a`` outside brackets, as used inside this project's markdown-table
    cells), an Obsidian Citations-plugin wikilink (``[[@a]]``,
    ``[[@a|display text]]``), or listed in a front-matter ``nocite:`` block —
    mapped to where it was cited, plus one :class:`~bibaudit.model.Reference`
    per DOI typed directly into the text or into an R ``reactable``
    publications table, plus every bibliography path declared for the files
    scanned.

``find_project_bibliography(start)``
    The bibliography path(s) that apply to *start*: from its own YAML front
    matter if it has one, else from a ``_quarto.yml`` walked upward from it.
    A front-matter ``bibliography:`` value is resolved relative to *start*'s
    own directory (Quarto's rule) unless *start* sits inside an Obsidian
    vault (a directory carrying ``.obsidian`` somewhere above it), in which
    case it is resolved relative to the vault root instead — Obsidian
    citation plugins such as obsidian-pandoc-reference-list take the
    bibliography path as vault-relative, not note-relative, so a note three
    folders deep still finds a bibliography declared as ``material/refs.bib``.
    Exposed separately because a caller that already has a citekey (from
    ``scan_markdown`` or elsewhere) needs to resolve it against the
    *project's* bibliography, not repeat the search itself.

Everything here is regex- and string-scan-based, not a Markdown or YAML
parser: the grammar this module targets — Quarto's ``[@key]``, ``nocite:``
block scalars and R code chunks, plus Obsidian's ``[[@key]]`` citation
wikilinks and plain ``[[note]]``/``![[note]]`` links — is small and stable
enough that pulling in a general parser would add a dependency outside the
stdlib + bibtexparser + rapidfuzz allowlist for functionality this module
does not otherwise need.
"""

from __future__ import annotations

import os
import pathlib
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from ..model import Reference
from ..names import parse_name_list
from ..normalize import DOI_PATTERN, clean, extract_dois, normalize_doi, parse_year

#: "QuartoScan" and "scan_quarto" are deprecated aliases for "MarkdownScan"
#: and "scan_markdown", kept so existing imports do not break.
__all__ = [
    "MarkdownScan",
    "QuartoScan",
    "find_project_bibliography",
    "scan_markdown",
    "scan_quarto",
]


@dataclass
class MarkdownScan:
    """Everything :func:`scan_markdown` found across the files it walked."""

    #: citekey -> every "path:line" it was cited from, one entry per citation
    #: (a key cited three times keeps three locators, not one).
    citekeys: dict[str, list[str]]
    #: One Reference per DOI found in prose or in an R publications table.
    references: list[Reference]
    #: Resolved, deduplicated bibliography paths declared by the files scanned.
    bibliographies: list[pathlib.Path]


# ---------------------------------------------------------------------------
# YAML front matter: span, nocite block, bibliography key
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _FrontMatter:
    """Span and body of a file's YAML front matter, 0-based line indices."""

    open_index: int
    close_index: int
    #: Lines strictly between the opening and closing delimiters.
    body: list[str]


#: A UTF-8 byte-order mark. ``Path.read_text(encoding="utf-8")`` keeps it
#: (only ``utf-8-sig`` drops it), so a note saved by an editor that writes one
#: arrives with ``﻿`` glued to its first character — which is exactly the
#: opening ``---`` of the front matter.
_BOM = "﻿"


def _delimiter_token(line: str) -> str:
    """*line* reduced to the token that decides whether it delimits front matter.

    Two things are removed and nothing else. A leading BOM, because a file
    written with one would otherwise present its opening delimiter as
    ``"\\ufeff---"`` and lose its whole front matter. And *trailing*
    whitespace, because pandoc's own metadata-block parser accepts blanks
    after the ``---``/``...`` — an editor that strips nothing on save leaves
    ``"--- "`` behind, and treating that as ordinary prose costs the file its
    ``bibliography:`` (every citekey in it then reports as unresolvable) and
    its ``nocite:`` block (every key in it is never checked at all).

    Leading whitespace is deliberately *not* stripped: pandoc requires the
    delimiter at column 0, and an indented ``---`` inside a list is content.
    """
    return line.lstrip(_BOM).rstrip()


def _extract_front_matter(lines: Sequence[str]) -> _FrontMatter | None:
    """*lines*' YAML front matter, if line 1 opens one.

    Pandoc/Quarto only recognise front matter that starts on the file's very
    first line — a bare ``---`` appearing later is an ordinary Markdown
    horizontal rule, not a metadata block, so this never looks for an
    opening delimiter past line 1.
    """
    if not lines or _delimiter_token(lines[0]) != "---":
        return None
    for idx in range(1, len(lines)):
        if _delimiter_token(lines[idx]) in ("---", "..."):
            return _FrontMatter(open_index=0, close_index=idx, body=list(lines[1:idx]))
    return None


#: YAML block-scalar indicators ``nocite:``'s value can carry. This corpus
#: only ever uses the plain ``|``, but the chomping/indentation modifiers
#: (``|-``, ``>+``, ...) are valid pandoc metadata YAML and cost nothing
#: extra to recognise.
_BLOCK_SCALAR_INDICATORS = frozenset({"|", "|-", "|+", ">", ">-", ">+"})

_NOCITE_KEY_RE = re.compile(r"^nocite:\s*(.*)$")


def _iter_nocite_citekeys(front_matter: _FrontMatter) -> Iterator[tuple[str, int]]:
    """(citekey, 1-based line number) for every key in a ``nocite:`` field.

    Deliberately its own pass rather than letting the generic prose scan see
    front matter too: a comma-separated ``@a, @b, ...`` block satisfies the
    same "preceded by a delimiter" rule ``_iter_citekeys`` uses for ordinary
    prose, so scanning both would report every nocite key twice.
    """
    body = front_matter.body
    for i, raw_line in enumerate(body):
        match = _NOCITE_KEY_RE.match(raw_line.rstrip("\r\n"))
        if not match:
            continue
        rest = match.group(1).strip()
        if rest == "" or rest in _BLOCK_SCALAR_INDICATORS:
            # Block scalar: gather subsequent indented lines. YAML lets a
            # block literal contain blank lines; only a line that starts at
            # column 0 ends it, since that is the only way a *following*
            # front-matter key could begin.
            j = i + 1
            while j < len(body):
                block_line = body[j].rstrip("\r\n")
                if block_line.strip() == "":
                    j += 1
                    continue
                if not block_line[:1].isspace():
                    break
                # body[j] is file line (j + 1); +1 again for the opening "---".
                line_no = j + 2
                for key in _iter_citekeys(block_line):
                    yield key, line_no
                j += 1
        else:
            # Rare inline scalar, e.g. `nocite: "@a, @b"`. The surrounding
            # quotes are YAML syntax, not text, and have to come off before
            # the citekey scan: a citekey opens a match only after a
            # delimiter or at the start of the string, so `"@a, @b"` scanned
            # as written silently loses `@a` — the one flush against the
            # opening quote — and keeps every key after it.
            yield from ((key, i + 2) for key in _iter_citekeys(_dequote(rest)))
        return  # A YAML mapping has at most one `nocite:` key.


def _dequote(value: str) -> str:
    """Strip one matching pair of surrounding quotes, if present."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


#: One entry of a YAML block sequence. Indentation is optional because YAML
#: allows a sequence under a mapping key to sit at the *same* column as the
#: key::
#:
#:     bibliography:
#:     - one.bib
#:     - two.bib
#:
#: is the identical document to the indented form, and both are written by
#: hand and by every YAML emitter in circulation. Requiring indentation made
#: the un-indented form yield no bibliography at all — the silent version of
#: the failure, since the scan then reports every citekey in the file as
#: unresolvable rather than saying it could not find the bibliography.
#:
#: The space after the ``-`` is required, not optional, and that is what keeps
#: a Markdown/YAML document-end marker out: ``---`` at column 0 would satisfy
#: ``^-\s*(.+)$`` and be read as a bibliography path ``--``.
_BLOCK_SEQUENCE_ITEM_RE = re.compile(r"^[ \t]*-[ \t]+(.+)$")


def _yaml_scalar_or_list(lines: Sequence[str], key: str) -> list[str]:
    """Values of a top-level (column-0) YAML *key*.

    Handles exactly the three shapes ``bibliography:`` can take in Quarto
    metadata — a bare scalar, a flow list ``[a, b]``, or a block list of
    ``- item`` lines — and nothing more general: a full YAML reader is out
    of scope for a project restricted to the stdlib plus bibtexparser and
    rapidfuzz, and Quarto's own ``bibliography:`` key never needs more than
    these three forms.
    """
    pattern = re.compile(rf"^{re.escape(key)}:\s*(.*)$")
    for i, raw in enumerate(lines):
        match = pattern.match(raw.rstrip("\r\n"))
        if not match:
            continue
        rest = match.group(1).strip()

        if not rest:
            values: list[str] = []
            j = i + 1
            while j < len(lines):
                item_line = lines[j].rstrip("\r\n")
                if item_line.strip() == "":
                    j += 1
                    continue
                item_match = _BLOCK_SEQUENCE_ITEM_RE.match(item_line)
                if not item_match:
                    break
                values.append(_dequote(item_match.group(1)))
                j += 1
            return values

        if rest.startswith("["):
            flow = rest
            j = i
            while "]" not in flow and j + 1 < len(lines):
                j += 1
                flow += " " + lines[j].rstrip("\r\n").strip()
            end = flow.find("]")
            inner = flow[1:end] if end != -1 else flow[1:]
            return [_dequote(item) for item in inner.split(",") if item.strip()]

        return [_dequote(rest)]
    return []


def _walk_upward(directory: pathlib.Path) -> Iterator[pathlib.Path]:
    """*directory*, then each ancestor up to and including the filesystem root."""
    current = directory
    while True:
        yield current
        parent = current.parent
        if parent == current:
            return
        current = parent


def _find_vault_root(directory: pathlib.Path) -> pathlib.Path | None:
    """The Obsidian vault root at or above *directory*, or ``None``.

    Obsidian marks a vault's root with a hidden ``.obsidian`` directory, the
    same way Quarto marks a project root with ``_quarto.yml``. This matters
    because a note's front-matter ``bibliography:`` path is resolved
    differently under each tool: Quarto resolves it against the citing
    file's own directory, but Obsidian citation plugins (e.g.
    obsidian-pandoc-reference-list) resolve it against the *vault* root. A
    vault observed in practice declares ``bibliography: material/refs.bib``
    identically in a note two levels deep and one three levels deep — the
    only resolution rule consistent with both is vault-relative; resolving
    against the note's own directory would send the deeper note looking for
    a bibliography that is not there.
    """
    for candidate in _walk_upward(directory):
        if (candidate / ".obsidian").is_dir():
            return candidate
    return None


def find_project_bibliography(start: pathlib.Path) -> list[pathlib.Path]:
    """Resolved bibliography path(s) that apply to *start*.

    Tries *start*'s own YAML front matter first, then walks upward for a
    ``_quarto.yml`` project config. In a Quarto corpus every source page
    typically relies on the second path — ``bibliography: references.bib``
    is declared once, project-wide, in ``_quarto.yml``, never per page — so a
    caller that only checked each file's own front matter would report every
    one of that project's real citekeys as unresolvable, a per-file `pandoc`
    run without an explicit ``--bibliography`` has exactly this failure mode.
    An Obsidian vault instead declares ``bibliography:`` per note; see
    :func:`_find_vault_root` for how that value's *base* directory differs
    from Quarto's.
    """
    start = pathlib.Path(start)
    if start.is_file():
        lines = start.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        front_matter = _extract_front_matter(lines)
        if front_matter is not None:
            values = _yaml_scalar_or_list(front_matter.body, "bibliography")
            if values:
                note_dir = start.resolve().parent
                vault_root = _find_vault_root(note_dir)
                base = vault_root if vault_root is not None else note_dir
                return [(base / value).resolve() for value in values]
        search_from = start.resolve().parent
    else:
        search_from = start.resolve()

    for directory in _walk_upward(search_from):
        candidate = directory / "_quarto.yml"
        if candidate.is_file():
            yml_lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            # `_quarto.yml` marks the project root: a project without its own
            # `bibliography:` key has none to inherit from a parent directory,
            # so the search stops here regardless of whether one was found.
            return [
                (directory / value).resolve()
                for value in _yaml_scalar_or_list(yml_lines, "bibliography")
            ]
    return []


# ---------------------------------------------------------------------------
# Fenced code blocks and inline code spans
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Fence:
    """One fenced code block's line span and body, 0-based line indices."""

    #: Line carrying the opening fence (`` ```{r} `` or ``~~~``).
    open_index: int
    #: Last line this block owns for masking: the closing fence line if one
    #: was found, else the file's last line — an unterminated fence still
    #: removes everything after it from the citekey pass rather than leaking
    #: a broken document into plain-prose scanning.
    mask_end_index: int
    #: Original, unmodified lines strictly between the fences.
    body: list[str]


_FENCE_OPEN_RE = re.compile(r"^\s*(`{3,}|~{3,})")


def _find_fences(lines: Sequence[str]) -> list[_Fence]:
    """Every top-level fenced code block in *lines*."""
    fences: list[_Fence] = []
    i, n = 0, len(lines)
    while i < n:
        opening = _FENCE_OPEN_RE.match(lines[i])
        if not opening:
            i += 1
            continue
        fence_char = opening.group(1)[0]
        fence_len = len(opening.group(1))
        open_index = i
        close_index: int | None = None
        j = i + 1
        while j < n:
            stripped = lines[j].strip()
            # A closing fence is a run of the *same* character, at least as
            # long as the opener, with nothing else on the line.
            if stripped and set(stripped) == {fence_char} and len(stripped) >= fence_len:
                close_index = j
                break
            j += 1
        if close_index is not None:
            fences.append(
                _Fence(open_index=open_index, mask_end_index=close_index, body=list(lines[open_index + 1 : close_index]))
            )
            i = close_index + 1
        else:
            fences.append(_Fence(open_index=open_index, mask_end_index=n - 1, body=list(lines[open_index + 1 :])))
            i = n
    return fences


#: Double-backtick spans are tried first: a code span that itself contains a
#: literal backtick (`` ``code with ` backtick`` ``) is the only realistic
#: reason a Markdown author reaches for more than one, and trying the
#: single-backtick alternative first would stop at that inner backtick and
#: leave the rest of the span unmasked.
_INLINE_CODE_RE = re.compile(r"``[^`]*?``|`[^`]*?`")


# ---------------------------------------------------------------------------
# Citekeys: [@key], bare @key, and Quarto cross-reference exclusion
# ---------------------------------------------------------------------------

#: Pandoc's citekey grammar. The lookbehind alternatives are exactly the
#: contexts a real citation can open in: start of line (a bare ``@key``
#: opening a markdown table cell, as used throughout this corpus'
#: publication-year tables), whitespace, an opening ``[``/``{``, a
#: citation-list ``;``/``,`` separator, ``(``, or ``-`` (pandoc's
#: suppress-author ``[-@key]``). Nothing else precedes ``@`` in a real
#: citation — a preceding word character means an email address
#: (``user@example.com``) or, inside a stripped-out code chunk, an R S4 slot
#: access (``obj@field``), never a citekey.
#:
#: Characters after the first are split into plain runs (letters, digits,
#: underscore) and "internal punctuation" (``:.#$%&+?<>~/-``, pandoc's own
#: term) that is only consumed when *another* plain character follows it —
#: mirroring pandoc's own lookahead so a trailing mark that is really
#: sentence punctuation ("...cited in @riboli1997epic.", "as shown by
#: @smith2020: the results follow", "did @smith2020 find this?") is never
#: pulled into the key, while a key that legitimately contains one of these
#: internally (rare in this corpus, but valid pandoc syntax) still matches
#: in full.
_CITEKEY_RE = re.compile(r"(?:^|(?<=[\s\[{;,(-]))@([a-zA-Z][a-zA-Z0-9_]*(?:[:.#$%&+?<>~/-]+[a-zA-Z0-9_]+)*)")

#: Quarto cross-references share ``@key`` syntax with citations but are never
#: bibliography entries — ``@tbl-pangeneu-pubs`` names a table, not a work.
#: A citekey never legitimately starts with one of these.
_XREF_PREFIXES = (
    "fig-", "tbl-", "sec-", "eq-", "lst-", "thm-", "def-",
    "exm-", "exr-", "cor-", "lem-", "prp-", "cnj-",
)


def _iter_citekeys(text: str) -> Iterator[str]:
    """Every citekey in *text*, cross-references and trailing punctuation removed.

    ``_CITEKEY_RE``'s own lookahead already keeps a trailing period, colon or
    other internal-punctuation character out of the match when it is really
    sentence punctuation rather than part of the key. The ``rstrip`` here is
    defence in depth, not the primary mechanism: it costs nothing and guards
    against a future edit to the regex reintroducing the failure mode.
    """
    for match in _CITEKEY_RE.finditer(text):
        key = match.group(1).rstrip(".,")
        if key and not key.startswith(_XREF_PREFIXES):
            yield key


# ---------------------------------------------------------------------------
# Obsidian wikilinks: [[@key]] citations vs. [[note]] / ![[note]] navigation
# ---------------------------------------------------------------------------

#: A wikilink or embed span: ``[[target]]``, ``[[target|display]]``, or an
#: embed ``![[target]]``. Obsidian's own syntax never lets a wikilink span a
#: line break, so matching within one line (this module already processes
#: line by line) is exact, not an approximation. Group 1 is the leading
#: ``!`` if this is an embed; group 2 is the target, which decides whether
#: this is a citation.
_WIKILINK_RE = re.compile(r"(!?)\[\[([^\]|\n]*)(?:\|[^\]\n]*)?\]\]")

#: Obsidian block references (``^block-id``, only valid at the end of a
#: line) and tags (``#tag``, ``#nested/tag``) share no syntax with a citekey
#: — neither one can ever contain the literal ``@`` that ``_CITEKEY_RE``
#: requires — so unlike wikilinks they need no masking pass here: there is
#: no failure mode to prevent. They are named in this comment, not in code,
#: so a future reader does not mistake the omission for an oversight.


def _mask_wikilinks(
    line: str, line_no: int, locator_path: str, citekeys: dict[str, list[str]]
) -> str:
    """Blank every wikilink/embed span on *line*, extracting citations first.

    The Citations plugin's ``[[@key]]`` / ``[[@key|display text]]`` is the
    one wikilink shape that names a bibliography entry; every other wikilink
    (``[[Some Note]]``, ``[[Some Note|display]]``, a same-note heading link
    ``[[#Heading]]``) and every embed (``![[Some Note]]``) points at another
    vault note, not a reference. Left unmasked, a note title that happens to
    contain an ``@``-prefixed mention — plausible in a personal vault that
    also uses ``@name`` for people — would otherwise be picked up as a
    citekey by the generic scanner in :func:`_iter_citekeys`, which accepts
    ``@`` right after a literal ``[``. Every span is blanked here, citation
    or not, so that scanner never re-processes what this function already
    resolved (which would otherwise double-count the citation ones).
    """

    def _replace(match: re.Match[str]) -> str:
        is_embed, target = match.group(1), match.group(2)
        if not is_embed and target.startswith("@"):
            for key in _iter_citekeys(target):
                citekeys.setdefault(key, []).append(f"{locator_path}:{line_no}")
        return " " * len(match.group(0))

    return _WIKILINK_RE.sub(_replace, line)


# ---------------------------------------------------------------------------
# R publication tables: read.delim(text = "...") and data.frame(... DOI = c(...))
# ---------------------------------------------------------------------------

#: Header/vector-name -> Reference field, case-insensitive. Column sets vary
#: file to file (EPIC's read.delim table carries Data/N/Theme columns
#: PanGenEU's data.frame does not); only these four are ever pulled into
#: structured Reference fields, everything else stays in ``raw`` untouched.
_COLUMN_ROLES = {
    "year": "year",
    "author": "authors",
    "authors": "authors",
    "journal": "container",
    "doi": "doi",
}


def _column_role(name: str) -> str | None:
    """The :class:`~bibaudit.model.Reference` field *name* maps to, if any.

    Matching is whitespace- and case-insensitive so ``" DOI "``, ``"doi"``
    and ``"Doi"`` — all seen across this corpus' tables — resolve to the
    same role.
    """
    return _COLUMN_ROLES.get(name.strip().lower())


def _skip_r_comment(text: str, index: int) -> int:
    """Index of the newline ending the ``#`` comment at *index* (or EOF).

    Outside a string literal, ``#`` always starts a comment in R — there is
    no other use for it — so everything to the end of the line is inert. This
    exists because the apostrophe in an ordinary English comment
    (``# don't reorder these``) otherwise reads as an opening single-quoted
    string: :func:`_scan_balanced` then never finds the call's closing
    ``)``, runs to the end of the chunk, and swallows the *next*
    ``data.frame()`` call's vectors into the current one. On a two-table
    chunk that produced the second table's rows twice and dropped the first
    table's Year/Journal/Author entirely; with vectors of unequal length it
    would instead have paired one paper's DOI with another paper's year — a
    field mismatch reported against a bibliography that was right.
    """
    newline = text.find("\n", index)
    return len(text) if newline == -1 else newline


def _scan_balanced(text: str, open_index: int) -> int:
    """Index just past the ``)`` matching the ``(`` at *open_index*.

    Quote-aware: an R string literal between the parens can itself contain
    unbalanced-looking parens — a DOI like ``10.1016/S0140-6736(03)14065-2``
    is exactly the case ``normalize.DOI_PATTERN``'s own docstring warns
    about — and counting those as call-nesting would close the call early
    and cut the rest of a publications table off. Comment-aware for the
    reason :func:`_skip_r_comment` gives.
    """
    assert text[open_index] == "("
    depth = 0
    quote: str | None = None
    i, n = open_index, len(text)
    while i < n:
        ch = text[i]
        if quote is not None:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch == "#":
            i = _skip_r_comment(text, i)
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n  # Unterminated call (malformed source); treat as running to EOF.


#: The R escape sequences that change a *table's shape* rather than one cell's
#: text. ``read.delim(text = "Year\\tDOI\\n2020\\t10.1234/x\\n")`` is the
#: idiomatic one-line spelling of the same table this corpus writes across
#: real newlines, and R sees a two-row tab-separated table in it. Appending
#: the bare escaped character instead — the previous behaviour — turned that
#: into the single header ``YeartDOIn2020t10.1234/x``, whose column names
#: match nothing, so the table was silently demoted to bare DOIs with no
#: Year, Author or Journal to check against the registry.
#:
#: Everything not listed (``\\\\``, ``\\"``, ``\\'``, ``\\``` and, leniently,
#: any unrecognised escape) still yields the escaped character itself, which
#: is what R does for the quoting ones and close enough for the rest.
_R_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "f": "\f", "v": "\v"}


def _extract_quoted_string(text: str, index: int) -> tuple[str, int]:
    """The quoted string opening at *index*, unescaped, and the index just past it."""
    quote = text[index]
    i, n = index + 1, len(text)
    out: list[str] = []
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            out.append(_R_ESCAPES.get(text[i + 1], text[i + 1]))
            i += 2
            continue
        if ch == quote:
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    return "".join(out), n  # Unterminated string; treat as running to EOF.


def _split_r_vector_elements(text: str, start: int, end: int) -> list[tuple[str, int]]:
    """Comma-separated, quote- and comment-aware elements of an R vector literal.

    Returns ``(value, absolute_offset)`` pairs rather than bare strings: a
    DOI vector such as PanGenEU's ``DOI = c(...)`` spans one value per source
    line, and the offset is how each row's Reference later gets the line it
    actually appears on instead of all of them collapsing onto the vector's
    opening line.

    An interleaved ``# comment`` line is skipped whole, both because its text
    is not an element and because an apostrophe in it would otherwise open a
    string that runs to the end of the vector — see :func:`_skip_r_comment`.
    """
    elements: list[tuple[str, int]] = []
    buf: list[str] = []
    elem_start = -1
    quote: str | None = None
    i = start
    while i < end:
        ch = text[i]
        if quote is not None:
            if ch == "\\" and i + 1 < end:
                # Same escape table as :func:`_extract_quoted_string`: the two
                # readers of an R string literal have to agree, or the same
                # journal name reads as "Int J\nCancer" in a `data.frame`
                # vector and "Int J Cancer" in a `read.delim` cell.
                buf.append(_R_ESCAPES.get(text[i + 1], text[i + 1]))
                i += 2
                continue
            if ch == quote:
                quote = None
                i += 1
                continue
            buf.append(ch)
            i += 1
            continue
        if ch == "#":
            i = min(_skip_r_comment(text, i), end)
            continue
        if ch in "\"'":
            if elem_start == -1:
                elem_start = i
            quote = ch
            i += 1
            continue
        if ch == ",":
            value = "".join(buf).strip()
            if value:
                elements.append((value, elem_start if elem_start != -1 else i))
            buf, elem_start = [], -1
            i += 1
            continue
        if not ch.isspace() and elem_start == -1:
            elem_start = i
        buf.append(ch)
        i += 1
    value = "".join(buf).strip()
    if value:
        elements.append((value, elem_start if elem_start != -1 else i))
    return elements


_READ_DELIM_RE = re.compile(r"read\.delim\s*\(")
_TEXT_KW_RE = re.compile(r"text\s*=\s*")
_SEP_KW_RE = re.compile(r'sep\s*=\s*"([^"]*)"')


def _find_read_delim_tables(chunk_text: str) -> list[tuple[str, str, int]]:
    """``(content, sep, content_offset)`` for each ``read.delim(text = "...")`` call.

    *content_offset* is where the quoted literal's first character sits in
    *chunk_text*, needed to turn a row's position inside *content* back into
    a source line number.
    """
    results: list[tuple[str, str, int]] = []
    for call_match in _READ_DELIM_RE.finditer(chunk_text):
        open_paren = call_match.end() - 1
        call_end = _scan_balanced(chunk_text, open_paren) - 1  # index of the call's ")"
        text_kw = _TEXT_KW_RE.search(chunk_text, open_paren + 1, call_end)
        if text_kw is None:
            continue
        q = text_kw.end()
        while q < call_end and chunk_text[q] not in "\"'":
            # `#` here is a comment between `text =` and its literal; its
            # prose can hold both an apostrophe and a stray quote.
            q = _skip_r_comment(chunk_text, q) if chunk_text[q] == "#" else q + 1
        if q >= call_end:
            continue
        content, after = _extract_quoted_string(chunk_text, q)
        # `sep` is an ordinary named argument and R does not care where it
        # sits: `read.delim(sep = "|", text = "...")` is as valid as this
        # corpus' `read.delim(text = "...", sep = "|")`. Searching only after
        # the literal made the first spelling fall back to read.delim's
        # tab default, at which point a pipe-delimited table is one column
        # wide, has no DOI header, and is dropped — its rows surviving only
        # as bare DOIs with no Year, Journal or Author to check.
        #
        # Both searches deliberately stop at the literal's own span rather
        # than running through it: a table cell reading `sep = ";"` is text,
        # not an argument.
        sep_match = _SEP_KW_RE.search(chunk_text, open_paren + 1, q) or _SEP_KW_RE.search(
            chunk_text, after, call_end
        )
        sep = sep_match.group(1) if sep_match else "\t"  # read.delim's own default
        results.append((content, sep, q + 1))
    return results


def _references_from_delim_table(
    content: str,
    sep: str,
    content_offset: int,
    chunk_text: str,
    chunk_first_line: int,
    locator_path: str,
) -> list[Reference]:
    """Reference rows from one ``read.delim`` literal's header + data lines."""
    rows = content.split("\n")
    # The header is the literal's first *non-blank* row, not its first row.
    # Opening the literal on its own line —
    #
    #     pubs <- read.delim(text = "
    #     Year|Journal|DOI
    #     ...
    #
    # — is ordinary R style and keeps the header aligned with the data in the
    # source, but it makes row 0 the empty string. Reading that as the header
    # produced a single unnamed column, no DOI role, and the whole table
    # silently demoted to bare DOIs. `read.delim` itself skips it (blank.lines
    # .skip defaults to TRUE), so this only matches R's own behaviour.
    header_index = next((i for i, row in enumerate(rows) if row.strip()), None)
    if header_index is None:
        return []
    header = [h.strip() for h in rows[header_index].split(sep)]
    roles = [_column_role(h) for h in header]
    if "doi" not in roles:
        # Not a publications table this adapter recognises; the generic
        # leftover DOI sweep in _references_from_code_chunk still catches any
        # bare DOI text inside it.
        return []

    doi_col = roles.index("doi")
    year_col = roles.index("year") if "year" in roles else None
    authors_col = roles.index("authors") if "authors" in roles else None
    journal_col = roles.index("container") if "container" in roles else None

    refs: list[Reference] = []
    row_offset = content_offset
    for row_idx, row_text in enumerate(rows):
        if row_idx > header_index and row_text.strip():
            fields = [f.strip() for f in row_text.split(sep)]
            if len(fields) > doi_col:
                line_no = chunk_first_line + chunk_text.count("\n", 0, row_offset)
                locator = f"{locator_path}:{line_no}"
                raw: dict[str, Any] = {header[i]: fields[i] for i in range(min(len(header), len(fields)))}
                doi = normalize_doi(fields[doi_col]) or None
                year_raw = fields[year_col] if year_col is not None and len(fields) > year_col else None
                authors_raw = fields[authors_col] if authors_col is not None and len(fields) > authors_col else None
                journal_raw = fields[journal_col] if journal_col is not None and len(fields) > journal_col else None
                refs.append(
                    Reference(
                        key=doi or f"row:{locator}",
                        locator=locator,
                        # Every observed read.delim/data.frame table in this
                        # corpus is a "Key publications" listing of journal
                        # papers; a Journal-column table is never anything else.
                        kind="article",
                        doi=doi,
                        year=parse_year(year_raw) if year_raw else None,
                        authors=parse_name_list(authors_raw) if authors_raw else [],
                        container=clean(journal_raw) if journal_raw else None,
                        raw=raw,
                    )
                )
        # +1 accounts for the "\n" str.split("\n") consumed between rows.
        row_offset += len(row_text) + 1
    return refs


_DATA_FRAME_RE = re.compile(r"data\.frame\s*\(")
_VECTOR_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*c\(")


def _find_data_frame_doi_vectors(chunk_text: str) -> list[dict[str, list[tuple[str, int]]]]:
    """``{vector_name: [(value, offset), ...]}`` for each ``data.frame(...)`` call
    in *chunk_text* that defines a DOI-role vector.

    Every ``name = c(...)`` assignment inside a matching call is returned,
    not only the DOI one, so :func:`_references_from_data_frame_vectors` can
    look for Year/Author/Journal siblings to pair positionally.
    """
    results: list[dict[str, list[tuple[str, int]]]] = []
    for call_match in _DATA_FRAME_RE.finditer(chunk_text):
        open_paren = call_match.end() - 1
        call_end = _scan_balanced(chunk_text, open_paren) - 1  # index of the call's ")"
        vectors: dict[str, list[tuple[str, int]]] = {}
        for vec_match in _VECTOR_RE.finditer(chunk_text, open_paren + 1, call_end):
            vec_open = vec_match.end() - 1
            if vec_open >= call_end:
                continue
            vec_close = _scan_balanced(chunk_text, vec_open)
            vectors[vec_match.group(1)] = _split_r_vector_elements(chunk_text, vec_open + 1, vec_close - 1)
        if any(_column_role(name) == "doi" for name in vectors):
            results.append(vectors)
    return results


def _references_from_data_frame_vectors(
    vectors: dict[str, list[tuple[str, int]]],
    chunk_text: str,
    chunk_first_line: int,
    locator_path: str,
) -> list[Reference]:
    """Reference rows from one ``data.frame(... DOI = c(...) ...)`` call.

    Sibling vectors (Year/Author(s)/Journal) are paired positionally only
    when *every* one of them has the same length as the DOI vector. A single
    short or long sibling — someone added a DOI without updating every other
    column — means position ``i`` in that vector no longer corresponds to
    position ``i`` in DOI, so the whole call falls back to DOI-only
    References with a warning rather than silently misattributing a year or
    author to the wrong paper.
    """
    doi_name = next(name for name in vectors if _column_role(name) == "doi")
    doi_elems = vectors[doi_name]
    n = len(doi_elems)

    siblings: dict[str, list[tuple[str, int]]] = {}
    mismatched: list[str] = []
    for name, elems in vectors.items():
        role = _column_role(name)
        if role is None or role == "doi":
            continue
        if len(elems) == n:
            siblings[role] = elems
        else:
            mismatched.append(f"{name} (length {len(elems)} != DOI's {n})")
    warning = (
        "sibling vector length mismatch, positional pairing skipped: " + "; ".join(sorted(mismatched))
        if mismatched
        else ""
    )

    refs: list[Reference] = []
    for i, (raw_doi, offset) in enumerate(doi_elems):
        line_no = chunk_first_line + chunk_text.count("\n", 0, offset)
        locator = f"{locator_path}:{line_no}"
        doi = normalize_doi(raw_doi) or None
        raw: dict[str, Any] = {name: elems[i][0] for name, elems in vectors.items() if i < len(elems)}

        if warning:
            raw["warning"] = warning
            refs.append(Reference(key=doi or f"row:{locator}", locator=locator, kind="other", doi=doi, raw=raw))
            continue

        year_raw = siblings["year"][i][0] if "year" in siblings else None
        authors_raw = siblings["authors"][i][0] if "authors" in siblings else None
        journal_raw = siblings["container"][i][0] if "container" in siblings else None
        refs.append(
            Reference(
                key=doi or f"row:{locator}",
                locator=locator,
                kind="article",
                doi=doi,
                year=parse_year(year_raw) if year_raw else None,
                authors=parse_name_list(authors_raw) if authors_raw else [],
                container=clean(journal_raw) if journal_raw else None,
                raw=raw,
            )
        )
    return refs


def _references_from_code_chunk(chunk_text: str, chunk_first_line: int, locator_path: str) -> list[Reference]:
    """Every Reference in one fenced code block: structured tables, then leftovers.

    The structured passes run first and record which DOIs they already
    accounted for; a final sweep with ``normalize.DOI_PATTERN`` then catches
    any DOI typed into the chunk outside of either recognised table shape —
    a bare ``doi <- "10.x/y"`` assignment, a comment — without re-emitting
    the ones already captured with real Year/Author/Journal fields attached.
    """
    refs: list[Reference] = []
    consumed: set[str] = set()

    for content, sep, content_offset in _find_read_delim_tables(chunk_text):
        table_refs = _references_from_delim_table(content, sep, content_offset, chunk_text, chunk_first_line, locator_path)
        refs.extend(table_refs)
        consumed.update(r.doi for r in table_refs if r.doi)

    for vectors in _find_data_frame_doi_vectors(chunk_text):
        vector_refs = _references_from_data_frame_vectors(vectors, chunk_text, chunk_first_line, locator_path)
        refs.extend(vector_refs)
        consumed.update(r.doi for r in vector_refs if r.doi)

    for match in DOI_PATTERN.finditer(chunk_text):
        doi = normalize_doi(match.group(0))
        if not doi or doi in consumed:
            continue
        consumed.add(doi)
        line_no = chunk_first_line + chunk_text.count("\n", 0, match.start())
        refs.append(
            Reference(
                key=doi,
                locator=f"{locator_path}:{line_no}",
                kind="other",
                doi=doi,
                raw={"context": "code-chunk"},
            )
        )
    return refs


# ---------------------------------------------------------------------------
# Per-file orchestration
# ---------------------------------------------------------------------------


def _scan_one_file(
    text: str,
    locator_path: str,
    citekeys: dict[str, list[str]],
    references: list[Reference],
) -> None:
    """Scan one file's *text* and fold its citekeys and References into the
    caller's accumulators.

    *citekeys* and *references* are mutated in place rather than returned so
    :func:`scan_markdown` can accumulate across every file in a project
    without re-merging per-file results; a file that raises never gets this
    far, so a partial mutation on failure is not a concern here.
    """
    lines = text.splitlines(keepends=True)
    if not lines:
        return

    front_matter = _extract_front_matter(lines)
    fences = _find_fences(lines)

    # `masked_base`: front matter and fenced code blanked to empty lines, but
    # inline code spans left intact. A DOI inside a `single-backtick span` in
    # running prose is still real text to check; a citekey inside one is a
    # documentation example (about.qmd's `` `[@citekey]` ``) that must not be
    # counted as an actual citation — hence two different maskings below,
    # not one.
    masked_base = list(lines)
    if front_matter is not None:
        for idx in range(front_matter.open_index, front_matter.close_index + 1):
            masked_base[idx] = "\n"
        for key, line_no in _iter_nocite_citekeys(front_matter):
            citekeys.setdefault(key, []).append(f"{locator_path}:{line_no}")
    for fence in fences:
        for idx in range(fence.open_index, fence.mask_end_index + 1):
            masked_base[idx] = "\n"

    for i, base_line in enumerate(masked_base):
        line_no = i + 1
        # A backtick delimiting an inline code span is never part of a DOI,
        # but normalize.DOI_PATTERN's exclusion class (whitespace, quotes,
        # angle brackets) does not name backtick either — a DOI sitting
        # flush against its closing backtick (`` `10.1234/x` `` with no
        # intervening space) would otherwise swallow the backtick into the
        # extracted value. Blanking backticks to spaces removes only the
        # delimiter; the DOI text itself stays visible, per the masking
        # note above, unlike the citekey pass below which blanks the whole
        # span.
        for doi in extract_dois(base_line.replace("`", " ")):
            references.append(
                Reference(key=doi, locator=f"{locator_path}:{line_no}", kind="other", doi=doi, raw={"context": "prose"})
            )
        citekey_line = _INLINE_CODE_RE.sub(" ", base_line)
        # Wikilinks are masked (and their citations extracted) on the
        # inline-code-blanked line, not on `base_line`: a documentation
        # example written as `` `[[@citekey]]` `` must be excluded from real
        # citations for the same reason `` `[@citekey]` `` already is, above.
        citekey_line = _mask_wikilinks(citekey_line, line_no, locator_path, citekeys)
        for key in _iter_citekeys(citekey_line):
            citekeys.setdefault(key, []).append(f"{locator_path}:{line_no}")

    for fence in fences:
        chunk_text = "".join(fence.body)
        if not chunk_text:
            continue
        chunk_first_line = fence.open_index + 2  # 1-based line number of the body's first line
        references.extend(_references_from_code_chunk(chunk_text, chunk_first_line, locator_path))


# ---------------------------------------------------------------------------
# Path collection and locator formatting
# ---------------------------------------------------------------------------


def _collect_files(paths: Sequence[pathlib.Path]) -> list[pathlib.Path]:
    """Expand *paths* (files and/or directories) into a deduplicated file list.

    Input order is preserved for files given directly; a directory's
    contents are appended sorted by path, since a filesystem walk order is
    not itself deterministic and this tool's output must be.
    """
    seen: set[pathlib.Path] = set()
    files: list[pathlib.Path] = []
    for given in paths:
        given = pathlib.Path(given)
        candidates = (
            sorted({*given.rglob("*.qmd"), *given.rglob("*.md")}, key=lambda c: c.as_posix())
            if given.is_dir()
            else [given]
        )
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                files.append(candidate)
    return files


def _common_root(files: Sequence[pathlib.Path]) -> pathlib.Path | None:
    """The deepest directory common to every path in *files*, or ``None``."""
    if not files:
        return None
    resolved = [f.resolve() for f in files]
    if len(resolved) == 1:
        return resolved[0].parent
    try:
        return pathlib.Path(os.path.commonpath([str(p) for p in resolved]))
    except ValueError:
        # Different drives on Windows, or another unresolvable mix: fall back
        # to "no common root" rather than let one bad pair abort the scan.
        return None


def _relative_locator(file_path: pathlib.Path, root: pathlib.Path | None) -> str:
    """The path half of a "path:line" locator: relative to *root* when possible.

    Forward slashes always, regardless of platform, so a locator string is
    stable and directly usable in a report or a clickable link.
    """
    if root is not None:
        try:
            return file_path.resolve().relative_to(root).as_posix()
        except ValueError:
            pass
    return file_path.as_posix()


def scan_markdown(paths: Sequence[pathlib.Path]) -> MarkdownScan:
    """Scan *paths* (Quarto/Obsidian Markdown files and/or directories) for citekeys and References."""
    files = _collect_files(paths)
    root = _common_root(files)

    citekeys: dict[str, list[str]] = {}
    references: list[Reference] = []
    bibliographies: list[pathlib.Path] = []
    seen_bibliographies: set[pathlib.Path] = set()

    for file_path in files:
        locator_path = _relative_locator(file_path, root)
        text = file_path.read_text(encoding="utf-8", errors="replace")
        _scan_one_file(text, locator_path, citekeys, references)
        for bib in find_project_bibliography(file_path):
            if bib not in seen_bibliographies:
                seen_bibliographies.add(bib)
                bibliographies.append(bib)

    return MarkdownScan(citekeys=citekeys, references=references, bibliographies=bibliographies)


#: Deprecated aliases. This module was ``adapters/quarto.py`` (``QuartoScan``,
#: ``scan_quarto``) before Obsidian support made "Quarto" too narrow a name;
#: kept so an import written against the old names keeps working.
QuartoScan = MarkdownScan
scan_quarto = scan_markdown
