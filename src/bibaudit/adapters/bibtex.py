"""Read a BibTeX (``.bib``) file into :class:`~bibaudit.model.Reference` objects.

Three public functions:

``read_bibtex(path)``
    Parse *path* with :mod:`bibtexparser` (v2) and return one Reference per
    successfully parsed entry.

``entry_locator(path, key)``
    Map a citekey back to a ``"filename:line"`` string, independent of any
    particular parse — a report can call this for any key it already knows
    about without holding a parsed library around.

``duplicate_report(refs)``
    Find references that are probably the same work cited more than once:
    a shared DOI, a reused citekey, or a near-identical title.

BibTeX itself does not require one entry per line: v2's block parser handles
that correctly (confirmed against a real 438-entry file with only 250 lines
starting in column 1 — the rest are entries opening mid-indentation or
following another entry's closing brace on the same line), so nothing here
splits the file on newlines or on ``^@``.
"""

from __future__ import annotations

import pathlib
import re
import warnings
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

import bibtexparser
from bibtexparser.model import DuplicateBlockKeyBlock, Entry, ParsingFailedBlock
from rapidfuzz import fuzz

from ..model import Name, Reference
from ..names import parse_name_list
from ..normalize import clean, extract_dois, fold, normalize_doi, normalize_kind, parse_year

__all__ = ["duplicate_report", "entry_locator", "read_bibtex"]


def read_bibtex(path: pathlib.Path) -> list[Reference]:
    """Parse *path* as BibTeX and return one Reference per parsed entry.

    Uses bibtexparser's default middleware stack (string resolution + one
    layer of enclosing-brace removal per field) rather than a bespoke one:
    that default is what strips the outer ``{...}``/``"..."`` BibTeX wraps
    every field in without touching braces *inside* a value, which is what
    keeps an inner ``{GWAS}`` (capitalisation protection) or a fully braced
    single author (``{World Health Organization}``) intact for
    :func:`~bibaudit.names.parse_name_list` to see.

    A block bibtexparser could not attach to the library — most often two
    entries sharing one citekey, where the second is dropped from
    ``library.entries`` entirely — is never silently swallowed: each one
    raises a :class:`RuntimeWarning` naming the file, line and reason, so the
    defect is visible even though the reference it belongs to cannot be
    built.
    """
    path = pathlib.Path(path)
    # bibtexparser re-exports parse_file from .entrypoint without listing it
    # in __init__.py's own __all__, which mypy --strict's implicit-reexport
    # check flags even though it is documented top-level API.
    library = bibtexparser.parse_file(str(path))  # type: ignore[attr-defined]
    _warn_failed_blocks(path, library.failed_blocks)
    return [_entry_to_reference(path, entry) for entry in library.entries]


# ---------------------------------------------------------------------------
# Entry -> Reference
# ---------------------------------------------------------------------------


def _select_creators(fields: dict[str, str]) -> tuple[list[Name], str]:
    """Creators for one entry, and the role marker if they came from ``editor``.

    An edited volume ("Methods in Biobanking", cited by its editors) has no
    ``author`` field at all — that is correct BibTeX, not a defect — so
    falling back to ``editor`` when ``author`` is absent is the right read of
    the entry rather than reporting zero authors. The returned role is
    empty for the ordinary case, matching
    ``adapters.zotero._select_creator_role``'s convention, so a comparison
    downstream sees the same ``raw["creator_role"] == "editor"`` marker
    regardless of which adapter produced the Reference.
    """
    author_field = fields.get("author")
    if author_field and author_field.strip():
        return parse_name_list(author_field), ""
    editor_field = fields.get("editor")
    if editor_field and editor_field.strip():
        return parse_name_list(editor_field), "editor"
    return [], ""


def _extract_doi(fields: dict[str, str]) -> str | None:
    """The entry's DOI, preferring the dedicated field over one buried in a URL.

    Crossref's BibTeX export writes the same DOI twice on many entries —
    once in ``DOI``, once as a resolver link in ``url`` — and the two
    commonly disagree only in case
    (``10.1158/1055-9965.EPI-20-0378`` vs. the url's
    ``...epi-20-0378``). Preferring the dedicated field means that
    disagreement is never even evaluated; falling back to ``url`` only when
    ``DOI`` is absent covers entries (mostly older ones) that carry a
    doi.org link but no separate DOI field.

    ``extract_dois`` (not a bare ``normalize_doi``) is used on *both* fields
    so a placeholder some reference managers write into ``doi`` — ``N/A``,
    ``TBD``, an empty pair of braces that survives as whitespace — is
    recognised as not-a-DOI and falls through to ``url`` instead of being
    normalised into a confident-looking fake like ``"n/a"`` that a registry
    lookup would then report as a bad identifier on an otherwise-fine entry.
    """
    doi_field = fields.get("doi")
    if doi_field and doi_field.strip():
        found = extract_dois(doi_field)
        if found:
            return found[0]
    url_field = fields.get("url")
    if url_field:
        found = extract_dois(url_field)
        if found:
            return found[0]
    return None


def _entry_to_reference(path: pathlib.Path, entry: Entry) -> Reference:
    """Build one Reference from a parsed bibtexparser Entry.

    Field names arrive from bibtexparser in whatever case the source file
    used (``DOI``, ``ISSN``, ``url`` all appear in the same real file); every
    lookup here goes through *fields*, a lowercase-keyed copy, while *raw*
    keeps the original casing intact as "untouched adapter output".
    """
    raw: dict[str, Any] = {name: field.value for name, field in entry.fields_dict.items()}
    fields = {name.lower(): value for name, value in raw.items()}

    authors, role = _select_creators(fields)
    if role:
        raw["creator_role"] = role

    # "journal" covers @article; "booktitle" covers @inbook/@incollection —
    # an entry has at most one of the two, never both, so this is not a
    # judgement call about which field wins.
    container = fields.get("journal") or fields.get("booktitle")

    return Reference(
        key=entry.key,
        locator=entry_locator(path, entry.key),
        kind=normalize_kind(entry.entry_type),
        doi=_extract_doi(fields),
        isbn=clean(fields["isbn"]) if fields.get("isbn") else None,
        url=clean(fields["url"]) if fields.get("url") else None,
        title=clean(fields["title"]) if fields.get("title") else None,
        authors=authors,
        # BibTeX conventionally uses "year"; a "date" field (Zotero's Better
        # BibTeX export, some CSL-flavoured .bib files) is the fallback
        # parse_year handles on its own once handed the right string.
        year=parse_year(fields.get("year")) or parse_year(fields.get("date")),
        container=clean(container) if container else None,
        volume=clean(fields["volume"]) if fields.get("volume") else None,
        issue=clean(fields["number"]) if fields.get("number") else None,
        pages=clean(fields["pages"]) if fields.get("pages") else None,
        publisher=clean(fields["publisher"]) if fields.get("publisher") else None,
        raw=raw,
    )


def _warn_failed_blocks(path: pathlib.Path, failed_blocks: list[ParsingFailedBlock]) -> None:
    """Raise a RuntimeWarning per block bibtexparser could not parse.

    A duplicate-citekey block is bibtexparser's own way of catching a real
    defect (two entries claiming the same key): it does not raise, it just
    quietly excludes the second entry from ``library.entries`` and records a
    :class:`DuplicateBlockKeyBlock` here instead. Treating that as nothing to
    report would make ``read_bibtex`` return 437 references from a 438-entry
    file with no indication why — the exact "not something to swallow"
    failure this function exists to prevent. ``warnings.warn`` matches
    ``adapters.zotero``'s convention for a non-fatal defect the caller must
    still see.
    """
    # stacklevel=3: frame 1 is this function's own warnings.warn() call,
    # frame 2 is read_bibtex() (the only caller), so frame 3 is whoever
    # called read_bibtex() — the actual "user" adapters.zotero's convention
    # means to point at. Leaving this at 2 (right for a warning raised
    # directly in a public function, which is zotero's case) would blame the
    # line inside read_bibtex that calls this helper instead of the caller's
    # own code, which is not something to swallow either.
    stacklevel = 3
    for block in failed_blocks:
        line = block.start_line
        location = f"{path.name}:{line + 1}" if line is not None else path.name
        if isinstance(block, DuplicateBlockKeyBlock):
            warnings.warn(
                f"{location}: duplicate citekey {block.key!r} — the second "
                "entry using this key was discarded and is not in the audit",
                RuntimeWarning,
                stacklevel=stacklevel,
            )
            continue
        # BlockAbortedException (bibtexparser's usual syntax-error exception)
        # carries its message in .abort_reason, not in str(exc) — the base
        # Exception is constructed without one, so str() alone is blank.
        reason = getattr(block.error, "abort_reason", None) or str(block.error) or repr(block.error)
        warnings.warn(
            f"{location}: block failed to parse ({reason})", RuntimeWarning, stacklevel=stacklevel
        )


# ---------------------------------------------------------------------------
# entry_locator
# ---------------------------------------------------------------------------

#: An entry opens with "@type{key," (or, rarely, "@type{key}" for a
#: field-less entry). The key class excludes comma/whitespace/close-brace
#: rather than allowlisting characters, because real citekeys use ':', '.'
#: and '/' (DOI-derived keys, Better BibTeX's "auto-export" keys) that an
#: allowlist would have to keep growing to cover.
_ENTRY_OPEN_RE = re.compile(r"@([A-Za-z]+)\s*\{\s*([^,\s}]+)\s*[,}]")

#: "@word{" also opens a @string macro, @comment or @preamble block. None of
#: those has a citekey worth indexing, and skipping them means a macro name
#: that happens to collide with a real citekey can never shadow the entry's
#: own line.
_NON_ENTRY_BLOCK_TYPES = frozenset({"string", "comment", "preamble"})

#: (resolved path, mtime_ns) -> {citekey: 1-based line number}. entry_locator
#: is called once per reference — hundreds of times in one read_bibtex() run
#: — so the file is regex-scanned once per (path, mtime) rather than once per
#: call; without this, locating N references would cost O(N * file size)
#: instead of O(file size + N).
_LINE_CACHE: dict[tuple[str, int], dict[str, int]] = {}


def _citekey_lines(path: pathlib.Path) -> dict[str, int]:
    """citekey -> 1-based line number for every entry in *path*, cached by mtime."""
    resolved = path.resolve()
    cache_key = (str(resolved), resolved.stat().st_mtime_ns)
    cached = _LINE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    text = resolved.read_text(encoding="utf-8")
    n = len(text)
    lines: dict[str, int] = {}
    line_no = 1
    pos = 0  # last position line_no has been advanced to
    scan_from = 0
    # Repeated search-and-skip rather than a flat finditer(): a bare
    # _ENTRY_OPEN_RE.finditer(text) matches "@type{key," anywhere in the raw
    # text, including *inside* another entry's own title — "A study
    # mentioning @article{other-key, as an example}" — and when that fake
    # occurrence comes first in the file, lines.setdefault keeps it and the
    # real "other-key" entry is reported at the wrong line. So once a match
    # is found, its own body is skipped by counting braces from *that
    # match's* opening brace only — never across the free text between
    # blocks, which BibTeX's implicit-comment rule leaves unbalanced by
    # design (a stray "{" in a note is not a defect) and which a whole-file
    # depth counter would misread as "still inside a block" forever after.
    while True:
        match = _ENTRY_OPEN_RE.search(text, scan_from)
        if match is None:
            break
        start = match.start()
        if match.group(1).lower() not in _NON_ENTRY_BLOCK_TYPES:
            line_no += text.count("\n", pos, start)
            pos = start
            # First occurrence wins. A duplicate citekey's second block never
            # reaches library.entries (see _warn_failed_blocks), so its line
            # must not overwrite the line of the entry actually in the audit.
            lines.setdefault(match.group(2), line_no)

        # The regex's own "\s*\{\s*" already consumed the block's opening
        # brace, so the first "{" at or after `start` is exactly that one.
        depth = 1
        i = text.index("{", start) + 1
        while i < n and depth > 0:
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            i += 1
        # depth == 0 means i now sits just past this block's real closing
        # brace. A block whose braces never balance is a syntax error
        # bibtexparser will already have reported via _warn_failed_blocks;
        # resuming right after its opener (rather than scanning to EOF as
        # "still inside") keeps that one bad block from hiding every entry
        # that follows it, at the cost of possibly re-scanning its own
        # (already broken) interior — the same fallback a flat scan would
        # give.
        scan_from = i if depth == 0 else start + 1

    # Drop any earlier snapshot of this same path so a long-lived process
    # that re-reads a file across edits does not accumulate one cache entry
    # per edit forever.
    for stale_key in [k for k in _LINE_CACHE if k[0] == cache_key[0] and k != cache_key]:
        del _LINE_CACHE[stale_key]
    _LINE_CACHE[cache_key] = lines
    return lines


def entry_locator(path: pathlib.Path, key: str) -> str:
    """A clickable ``"filename:line"`` locator for citekey *key* in *path*.

    Only the basename is used, matching the exact form ``Reference.locator``
    itself documents for a bibliography ("references.bib:412"): unlike a
    per-page ``.qmd`` locator, where the directory disambiguates one page
    among hundreds, a bibliography is normally one shared file, and a full
    path would just repeat the same prefix on every line of a report.
    """
    path = pathlib.Path(path)
    line = _citekey_lines(path).get(key)
    if line is None:
        # A key that came from read_bibtex() on this same file always has a
        # line; this only fires for a key looked up against a file that
        # changed (or never contained it), and "filename:?" is a better
        # failure than a KeyError aborting an otherwise-successful report.
        return f"{path.name}:?"
    return f"{path.name}:{line}"


# ---------------------------------------------------------------------------
# duplicate_report
# ---------------------------------------------------------------------------

#: rapidfuzz's fuzz.ratio() is on a 0-100 scale; this is the contract's 0.95
#: threshold expressed on that scale.
_TITLE_SIMILARITY_THRESHOLD = 95.0


def duplicate_report(refs: Sequence[Reference]) -> dict[str, list[str]]:
    """Group *refs* into three kinds of probable accidental duplication.

    Always returns exactly the keys ``"doi"``, ``"key"`` and ``"title"``,
    each a list of one human-readable line per colliding group (empty when
    nothing collided in that category). A duplicate DOI under two different
    citekeys is a common, real defect (a paper pasted into the bibliography
    twice under two different keys, once by hand and once from an export) —
    distinct from a duplicate *citekey*, which bibtexparser itself already
    prevents from reaching a single ``read_bibtex()`` call's output (see
    ``read_bibtex``'s failed-block handling); the "key" category exists for
    callers that merge references from more than one source, where the same
    key can legitimately reappear.
    """
    report: dict[str, list[str]] = {"doi": [], "key": [], "title": []}

    by_doi: dict[str, list[Reference]] = defaultdict(list)
    for ref in refs:
        if ref.doi:
            by_doi[normalize_doi(ref.doi)].append(ref)
    for doi, group in sorted(by_doi.items()):
        if len(group) > 1:
            where = ", ".join(f"{r.key} ({r.locator})" for r in group)
            report["doi"].append(f"{doi}: {where}")

    by_key: dict[str, list[Reference]] = defaultdict(list)
    for ref in refs:
        by_key[ref.key].append(ref)
    for key, group in sorted(by_key.items()):
        if len(group) > 1:
            where = ", ".join(r.locator for r in group)
            report["key"].append(f"{key}: {where}")

    # Folded once per reference up front so an O(n^2) comparison pays for
    # fold() O(n) times, not O(n) times per candidate pair.
    titled = [(ref, fold(ref.title)) for ref in refs if ref.title and fold(ref.title)]
    for i, (left, left_folded) in enumerate(titled):
        for right, right_folded in titled[i + 1 :]:
            if right.key == left.key:
                continue
            score = fuzz.ratio(left_folded, right_folded)
            if score >= _TITLE_SIMILARITY_THRESHOLD:
                report["title"].append(
                    f"{left.key} ({left.locator}) ~ {right.key} ({right.locator}) "
                    f"[{score:.0f}%]: {clean(left.title)}"
                )

    return report
