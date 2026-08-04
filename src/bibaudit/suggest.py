"""``--suggest``: propose a filled-in copy of a bibliography, never write to it.

This module exists to be the one place a corrected value is ever produced,
and to be the one place that is trivial to audit for the tool's second rule
("report, never rewrite"): it reads a ``.bib`` file, and every write it makes
lands in a *new* file next to it — ``<name>.suggested.bib`` and
``<name>.suggested.diff`` — with the original bytes on disk never opened for
writing.

What may be proposed (see ``docs/registry-artifacts.md`` for why the rest is
excluded):

* a field the entry has **no value for at all**, where a consulted registry
  supplied one — an :class:`~bibaudit.model.Issue` of kind ``"missing"``.
  This is filling a gap, never adjudicating a disagreement.
* a DOI for an entry that had none and was confirmed by title/author/year
  corroboration — kind ``"proposed"`` (see ``audit._audit_unidentified``).

Excluded by construction, not by a second filter bolted on afterwards:

* any field where the stored and registry values **disagree** — those are
  ``"mismatch"``/``"cosmetic"``/``"drift"`` issues, never ``"missing"`` or
  ``"proposed"``, so they are simply never in the set this module reads from.
* anything suppressed (``.bibaudit.toml``) or explained as a known registry
  defect (:mod:`bibaudit.benign`) — both move an issue out of
  ``Result.issues`` into ``Result.suppressed`` before this module ever sees
  the result, so there is no separate "is this suppressed" check to get
  wrong here.
* the author list. ``compare._check_authors`` reports a missing author list
  with only the first three creators, semicolon-joined, in display form
  (``Issue.registry``) — correct for a report, but not a complete, correctly
  delimited BibTeX ``author`` field. Writing that truncated string into
  ``author = {...}`` would silently drop every author past the third and
  hand the user an incomplete list disguised as a complete one, which is a
  worse outcome than proposing nothing. Fixing this would need the full
  :class:`~bibaudit.model.Record` author list, which ``Result`` does not
  carry; until it does, authors are not suggested.
"""

from __future__ import annotations

import difflib
import pathlib
import re
from collections.abc import Sequence
from dataclasses import dataclass

import bibtexparser

from .model import Result
from .normalize import normalize_kind

__all__ = ["Suggestion", "SuggestionOutcome", "build_suggestion", "write_suggestions"]

#: Internal (compare.CHECKED_FIELDS) name -> BibTeX field name, for the
#: fields that map one-to-one regardless of entry type. "container" is not
#: here: it needs the entry's kind to pick "journal" vs "booktitle", so
#: _bibtex_field_name handles it separately. "authors" is deliberately
#: absent — see the module docstring.
_FIELD_TO_BIBTEX = {
    "title": "title",
    "year": "year",
    "volume": "volume",
    # BibTeX has no "issue" field; "number" is the field an @article's issue
    # number is conventionally stored in (see adapters.bibtex._entry_to_reference,
    # which reads a Reference's `issue` from BibTeX's `number`).
    "issue": "number",
    "pages": "pages",
    "publisher": "publisher",
}


def _bibtex_field_name(field_name: str, ref_kind: str) -> str | None:
    """The BibTeX field *field_name* (a compare.py field) should fill, or ``None``."""
    if field_name == "container":
        # Mirrors adapters.bibtex._entry_to_reference's own read: "journal"
        # for an article, "booktitle" for a chapter. An entry has at most
        # one of the two, never both, so this is not a judgement call.
        return "booktitle" if ref_kind == "chapter" else "journal"
    return _FIELD_TO_BIBTEX.get(field_name)


def _suggested_fields(result: Result) -> dict[str, tuple[str, str]]:
    """BibTeX field name -> (value, registry) to fill, for one Result.

    Reads only ``result.issues`` — suppressed and registry-artifact
    differences already live in ``result.suppressed`` by the time a Result
    reaches this function (see :mod:`bibaudit.suppress` and
    :mod:`bibaudit.compare`), so there is nothing further to exclude here.
    """
    fields: dict[str, tuple[str, str]] = {}
    for issue in result.issues:
        if issue.kind == "missing" and issue.field != "authors":
            bibtex_field = _bibtex_field_name(issue.field, result.ref.kind)
            if bibtex_field is not None and issue.registry:
                fields[bibtex_field] = (issue.registry, issue.source)
        elif issue.kind == "proposed" and issue.field == "doi" and issue.registry:
            fields["doi"] = (issue.registry, issue.source)
    return fields


# ---------------------------------------------------------------------------
# Locating and rewriting one entry's exact source text
# ---------------------------------------------------------------------------

#: A field line's leading whitespace, used to match the indentation already
#: in use around it rather than inventing a new style for the fields this
#: module adds.
_FIELD_LINE_RE = re.compile(r"^([ \t]+)[A-Za-z][\w-]*\s*=", re.MULTILINE)

_DEFAULT_INDENT = "  "


def _insert_fields(raw: str, new_fields: dict[str, str]) -> str:
    """Return *raw* (one BibTeX entry's exact source text) with *new_fields* added.

    Every other byte of *raw* is preserved: the new fields are spliced in
    immediately before the entry's closing ``}``, with a comma added after
    the previous field only if it did not already have one. This is a text
    edit, not a reparse-and-reserialise, because bibtexparser's writer does
    not promise to reproduce a file's original formatting byte for byte, and
    a --suggest diff that reformats fields the user never asked to change is
    exactly the kind of noise this tool's own "false alarm" rule is against.
    """
    stripped = raw.rstrip()
    if not stripped.endswith("}"):
        # An entry bibtexparser accepted but that does not end in "}" is not
        # a shape this function knows how to edit; leave it untouched rather
        # than guess.
        return raw
    before_close = stripped[:-1]
    trailer = raw[len(stripped) :]  # whatever followed the "}" verbatim, if anything

    before_rstripped = before_close.rstrip()
    between = before_close[len(before_rstripped) :]  # whitespace between last field and "}"
    needs_comma = bool(before_rstripped) and not before_rstripped.endswith(",")
    comma = "," if needs_comma else ""
    if not between:
        between = "\n"

    # That whitespace runs from the last field to the "}", so its final line is
    # the closing brace's own indentation and belongs after the added fields,
    # not before them. Splicing it in front instead pushes the first new field
    # one level too deep and leaves the "}" in column 1 — a reformatted line the
    # user did not ask to change, in a file whose whole purpose is to be read as
    # a diff.
    blank_lines, _, closing_indent = between.rpartition("\n")

    indent_match = _FIELD_LINE_RE.search(raw)
    indent = indent_match.group(1) if indent_match else _DEFAULT_INDENT

    added = "".join(f"{indent}{name} = {{{value}}},\n" for name, value in new_fields.items())
    return f"{before_rstripped}{comma}{blank_lines}\n{added}{closing_indent}}}{trailer}"


@dataclass(slots=True)
class _EntrySpan:
    """Where one BibTeX entry sits in its file's original text."""

    offset: int
    raw: str
    kind: str


def _entry_spans(path: pathlib.Path, text: str) -> dict[str, _EntrySpan]:
    """citekey -> its exact source span, located by verbatim substring search.

    bibtexparser gives each entry a ``start_line`` (0-based) and a ``raw``
    (its exact source text) but not a character offset, so the offset is
    derived from ``start_line`` and then confirmed with an exact substring
    check — belt and braces, since splicing text at a wrong offset is
    exactly the kind of mistake this module exists to never make. An entry
    whose offset cannot be confirmed is dropped from the map rather than
    guessed at; :func:`build_suggestion` then simply has nothing to suggest
    for it.

    ``raw`` begins at the ``@``, while ``start_line`` names the line that
    holds it, so the two meet at the start of the line only for an entry
    written flush left. The entry's own leading whitespace is skipped to
    bring them together, and nothing beyond it: an offset that may move
    forward by a known amount is still confirmed against ``raw`` before it
    is used, and a file whose entries are indented is a formatting choice,
    not a reason to have nothing to suggest.
    """
    # bibtexparser re-exports parse_file from .entrypoint without listing it
    # in __init__.py's own __all__ (see adapters.bibtex.read_bibtex, which
    # hits the same mypy --strict implicit-reexport complaint).
    library = bibtexparser.parse_file(str(path))  # type: ignore[attr-defined]
    lines = text.splitlines(keepends=True)
    line_starts: list[int] = []
    pos = 0
    for line in lines:
        line_starts.append(pos)
        pos += len(line)

    spans: dict[str, _EntrySpan] = {}
    for entry in library.entries:
        raw = entry.raw
        if raw is None or entry.start_line is None or entry.start_line >= len(line_starts):
            continue
        line = lines[entry.start_line]
        offset = line_starts[entry.start_line] + (len(line) - len(line.lstrip()))
        if text[offset : offset + len(raw)] != raw:
            continue
        spans[entry.key] = _EntrySpan(offset=offset, raw=raw, kind=normalize_kind(entry.entry_type))
    return spans


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Suggestion:
    """A proposed, ready-to-write replacement for one bibliography file."""

    source: pathlib.Path
    suggested_path: pathlib.Path
    diff_path: pathlib.Path
    new_text: str
    diff_text: str
    entries_changed: int
    fields_filled: int
    registries: tuple[str, ...]


@dataclass(slots=True)
class SuggestionOutcome:
    """What actually happened for one bibliography after writing to disk."""

    source: pathlib.Path
    suggested_path: pathlib.Path
    diff_path: pathlib.Path
    entries_changed: int
    fields_filled: int


_HEADER = """\
% ---------------------------------------------------------------------
% GENERATED by `bibaudit check --suggest`. Review before use.
%
% This is a proposed copy of {name}. A value was added here only where
%   (a) the field was completely absent from the original entry and a
%       consulted registry supplied one, or
%   (b) a DOI was proposed for an entry that had none, confirmed by title,
%       author and year corroboration.
% No existing value in {name} was ever changed, removed, or judged against
% another. See {diff_name} for the exact, minimal set of lines added.
%
% Registries the proposed values in this file came from: {registries}
%
% bibaudit never writes to {name} itself. Verify each proposed value
% against the registry before applying any of it by hand.
% ---------------------------------------------------------------------

"""


def build_suggestion(bib_path: pathlib.Path, results: Sequence[Result]) -> Suggestion | None:
    """Build the suggested copy and diff for *bib_path*, without writing anything.

    Returns ``None`` when nothing in *results* has a fillable field for an
    entry actually present in *bib_path* — a bibliography with nothing to
    propose gets no ``.suggested.*`` files, matching this tool's own rule
    against noise that looks like assurance nobody asked for.
    """
    bib_path = pathlib.Path(bib_path)
    original_text = bib_path.read_text(encoding="utf-8")
    spans = _entry_spans(bib_path, original_text)
    if not spans:
        return None

    per_entry: dict[str, dict[str, str]] = {}
    registries: set[str] = set()
    for result in results:
        span = spans.get(result.ref.key)
        if span is None:
            continue
        suggested = _suggested_fields(result)
        if not suggested:
            continue
        per_entry[result.ref.key] = {name: value for name, (value, _source) in suggested.items()}
        registries.update(source for _value, source in suggested.values() if source)

    if not per_entry:
        return None

    # Splice from the last entry in the file backward, so a not-yet-processed
    # entry's offset (computed once, from the pristine original text) is
    # never disturbed by an earlier splice — see _entry_spans's own docstring
    # on why offsets are trusted only after an exact-match confirmation.
    ordered_keys = sorted(per_entry, key=lambda k: spans[k].offset, reverse=True)
    new_text = original_text
    entries_changed = 0
    fields_filled = 0
    for key in ordered_keys:
        span = spans[key]
        new_raw = _insert_fields(span.raw, per_entry[key])
        if new_raw == span.raw:
            # _insert_fields only refuses when the entry's own text does not
            # end in "}" (see its docstring) — essentially unreachable for
            # anything bibtexparser accepted, but counted as unchanged rather
            # than assumed, since entries_changed/fields_filled must describe
            # what the text actually gained, not what was attempted.
            continue
        new_text = new_text[: span.offset] + new_raw + new_text[span.offset + len(span.raw) :]
        entries_changed += 1
        fields_filled += len(per_entry[key])

    if new_text == original_text:
        return None

    new_text = _HEADER.format(
        name=bib_path.name,
        diff_name=bib_path.stem + ".suggested.diff",
        registries=", ".join(sorted(registries)) or "unknown",
    ) + new_text

    suggested_path = bib_path.with_name(bib_path.stem + ".suggested.bib")
    diff_path = bib_path.with_name(bib_path.stem + ".suggested.diff")
    diff_text = "".join(
        difflib.unified_diff(
            original_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=str(bib_path),
            tofile=str(suggested_path),
        )
    )

    return Suggestion(
        source=bib_path,
        suggested_path=suggested_path,
        diff_path=diff_path,
        new_text=new_text,
        diff_text=diff_text,
        entries_changed=entries_changed,
        fields_filled=fields_filled,
        registries=tuple(sorted(registries)),
    )


def write_suggestions(
    results: Sequence[Result], bibliography_paths: Sequence[pathlib.Path]
) -> list[SuggestionOutcome]:
    """Write ``<name>.suggested.bib``/``.diff`` beside every bibliography with a proposal.

    Never opens *any* path in *bibliography_paths* for writing — each is only
    ever ``read_text``'d, in :func:`build_suggestion`. Returns one outcome per
    file actually written, in the order given.
    """
    outcomes: list[SuggestionOutcome] = []
    for bib_path in bibliography_paths:
        bib_path = pathlib.Path(bib_path)
        if not bib_path.is_file():
            continue
        suggestion = build_suggestion(bib_path, results)
        if suggestion is None:
            continue
        suggestion.suggested_path.write_text(suggestion.new_text, encoding="utf-8")
        suggestion.diff_path.write_text(suggestion.diff_text, encoding="utf-8")
        outcomes.append(
            SuggestionOutcome(
                source=suggestion.source,
                suggested_path=suggestion.suggested_path,
                diff_path=suggestion.diff_path,
                entries_changed=suggestion.entries_changed,
                fields_filled=suggestion.fields_filled,
            )
        )
    return outcomes
