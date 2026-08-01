"""Normalisation primitives shared by every comparison.

Two levels, and keeping them apart is what makes the report readable:

``clean()``
    Presentation level. Undoes markup and encoding damage but preserves the
    text a human would recognise. Report output uses this.

``fold()``
    Comparison level. Aggressively strips case, accents and punctuation to
    produce a key that answers "is this the same string". Never shown to a user
    — printing a folded value hides the very glyph that caused the mismatch.

Every rule here was put in because a real bibliography broke without it; the
comments say which.
"""

from __future__ import annotations

import html
import re
import unicodedata
from difflib import SequenceMatcher

__all__ = [
    "DOI_PATTERN",
    "clean",
    "extract_dois",
    "first_page",
    "fold",
    "is_article_number",
    "normalize_doi",
    "normalize_kind",
    "parse_year",
    "similarity",
]

#: DOIs must be allowed to contain parentheses. Elsevier and Lancet DOIs look
#: like ``10.1016/S0140-6736(03)14065-2``; a character class that omits ``()``
#: truncates them at the bracket and reports a live DOI as unresolvable. That
#: mistake produced 12 false "does not exist" hits on a clean bibliography.
#: Trailing punctuation is stripped afterwards rather than excluded here, so a
#: DOI ending in a bracket is not cut short.
DOI_PATTERN = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)

#: Closing punctuation that is nearly always sentence furniture rather than part
#: of the DOI. A DOI legitimately ending in ``)`` keeps it, because we only strip
#: an unbalanced trailing bracket.
_DOI_TRAILING = ".,;:'\"]}>"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_BRACE_RE = re.compile(r"[{}]")
_NONWORD_RE = re.compile(r"[^a-z0-9]+")
_YEAR_RE = re.compile(r"(1[5-9]\d\d|20\d\d)")
_PAGE_RE = re.compile(r"\s*([A-Za-z]?)0*(\d+)")

#: LaTeX accent commands, as they appear in hand-written or exported .bib files:
#: ``{\'e}``, ``\"{o}``, ``\c{c}``. Mapping them to the bare letter is enough for
#: a comparison key — ``fold()`` would strip the diacritic anyway — and it avoids
#: a dependency on a full LaTeX decoder.
_LATEX_ACCENT_RE = re.compile(r"\\[`'^\"~=.uvHtcdbkr]\s*\{?([A-Za-z])\}?|\{\\[a-zA-Z]+\s+([A-Za-z])\}")

#: Remaining LaTeX control words, e.g. ``\emph``, ``\&``, ``\ldots``.
_LATEX_CMD_RE = re.compile(r"\\([a-zA-Z]+)\s*")

#: Unicode dashes and quotes that registries and BibTeX exports mix freely.
#: Folding them prevents an entry differing from Crossref only by a curly
#: apostrophe from being reported as a title mismatch.
_PUNCT_MAP = {
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "-", "\u2015": "-", "\u2212": "-",
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"',
    "\u00a0": " ", "\u2009": " ", "\u202f": " ",
}
_PUNCT_TABLE = str.maketrans(_PUNCT_MAP)


def clean(value: object) -> str:
    """Return *value* as display text: no markup, no entities, no brace armour.

    Crossref ships titles containing real HTML (``<i>``, ``<sub>``, ``&amp;``)
    and BibTeX uses ``{...}`` to protect capitalisation. Both are encoding, not
    content, and neither should reach a report or a comparison.
    """
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = _TAG_RE.sub("", text)
    text = _LATEX_ACCENT_RE.sub(lambda m: m.group(1) or m.group(2) or "", text)
    text = _LATEX_CMD_RE.sub(lambda m: "&" if m.group(1) == "amp" else " ", text)
    text = _BRACE_RE.sub("", text)
    text = text.translate(_PUNCT_TABLE)
    # NFKC folds ligatures and full-width forms, which registries use inconsistently.
    text = unicodedata.normalize("NFKC", text)
    return _WS_RE.sub(" ", text).strip()


def fold(value: object) -> str:
    """Return a comparison key: lowercase, unaccented, alphanumeric-only.

    ``&`` becomes ``and`` before punctuation is dropped, so "Cancer Epidemiology
    & Prevention" and "Cancer Epidemiology and Prevention" agree rather than
    differing by a token.
    """
    text = clean(value).lower().replace("&", " and ")
    # NFKD splits a letter from its diacritic so the combining marks can go.
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return _WS_RE.sub(" ", _NONWORD_RE.sub(" ", text)).strip()


def similarity(left: object, right: object) -> float:
    """Similarity of two strings on their folded forms, in ``[0, 1]``.

    ``SequenceMatcher`` is used rather than a token-set ratio because word order
    carries meaning in titles: "Effect of A on B" and "Effect of B on A" are
    different papers, and a set-based measure scores them identically.
    """
    a, b = fold(left), fold(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def normalize_doi(value: object) -> str:
    """Lowercase a DOI and strip resolver prefixes and trailing punctuation.

    DOIs are case-insensitive by specification, and the same work turns up as
    ``10.1158/1055-9965.EPI-20-0378`` in a URL field and
    ``10.1158/1055-9965.epi-20-0378`` in a DOI field of the very same entry.
    """
    text = clean(value).strip()
    # A DOI quoted in prose arrives wrapped: "(10.1234/abc)", "[10.1234/abc]".
    text = text.lstrip("([{<\"'")
    text = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", text, flags=re.IGNORECASE)
    text = text.strip().rstrip(_DOI_TRAILING)
    # Only an *unbalanced* closing bracket is punctuation; 10.1016/S0140-6736(03)14065-2
    # ends in a digit, but a DOI cited as "(10.1234/x)" would not.
    while text.endswith(")") and text.count(")") > text.count("("):
        text = text[:-1]
    return text.lower()


def extract_dois(text: str) -> list[str]:
    """Every DOI in *text*, normalised, in order of appearance, deduplicated."""
    seen: dict[str, None] = {}
    for match in DOI_PATTERN.finditer(text):
        doi = normalize_doi(match.group(0))
        if doi:
            seen.setdefault(doi, None)
    return list(seen)


def parse_year(value: object) -> int | None:
    """First four-digit year in *value*, or ``None``.

    Accepts anything a date field might hold — ``2021``, ``2021-05``,
    ``May 2021``, ``2021 Aug 12``, ``{2021}`` — because BibTeX, CSL and MEDLINE
    each format dates differently.
    """
    match = _YEAR_RE.search(clean(value))
    return int(match.group(1)) if match else None


def first_page(value: object) -> str:
    """Comparable form of the first page or article number.

    Only the opening page is compared. Closing pages disagree constantly and
    harmlessly: registries record ``1009-1018``, ``1009-18`` and ``1009+`` for
    the same article, and some drop the range entirely.

    Leading zeros are stripped because *Environmental Health Perspectives* and
    similar journals zero-pad article numbers (``027004``) while the citing
    entry carries ``27004``. Both denote the same article.
    """
    text = clean(value).replace("--", "-")
    match = _PAGE_RE.match(text)
    if not match:
        return ""
    prefix, digits = match.group(1).lower(), match.group(2)
    return f"{prefix}{digits}"


#: Fewest digits in an **all-numeric** value before it reads as an article
#: number rather than as an opening page. Four was too few: ``2461`` is the
#: first page of 10.1001/archinte.167.22.2461 (*Arch Intern Med* 167(22)) and
#: nothing about it says "article number" — so a stored ``2461`` against a
#: registry ``2450-2455`` was excused by ``benign._pages_article_number`` as
#: "article number recorded against a page range" when it is a plain four-digit
#: page disagreement. Six keeps both witnessed article numbers (``693933``,
#: ``672492`` on 10.1155/2009/672492) and excludes every page a volume-relative
#: numbering scheme plausibly reaches.
#:
#: A value carrying a non-digit prefix — ``e0123456``, ``A102`` — needs no such
#: floor: no journal paginates that way, so the prefix alone identifies it.
_MIN_NUMERIC_ARTICLE_NUMBER = 6


def is_article_number(value: object) -> bool:
    """True if *value* looks like an article number rather than a page range.

    Journals that number articles instead of paginating them (``e0123456``,
    ``693933``) legitimately have no volume-relative page span, so a missing
    closing page is expected rather than a defect.

    The threshold differs by shape — see :data:`_MIN_NUMERIC_ARTICLE_NUMBER`.
    Getting it wrong is not cosmetic: this predicate is what
    ``benign._pages_article_number`` uses to decide that two page values are one
    article in two notations, and every value it wrongly accepts is a page
    disagreement nobody is told about.
    """
    text = clean(value)
    if not text or "-" in text:
        return False
    page = first_page(text)
    if not page:
        return False
    if page.isdigit():
        return len(page) >= _MIN_NUMERIC_ARTICLE_NUMBER
    return len(page) >= 4


#: Registry type strings mapped onto the tool's own vocabulary. The mapping is
#: deliberately coarse: it exists to reject *incompatible* pairings (a book
#: entry whose DOI resolves to a journal article is usually a book review, not
#: the book) rather than to model bibliographic taxonomy.
#:
#: Keys are the output of ``fold(value).replace(" ", "-")``, so they are already
#: lowercase and hyphen-joined; ``bookSection`` arrives as ``booksection`` and
#: ``BookChapter`` as ``bookchapter``. The blocks are grouped by the vocabulary
#: that first needed each key, and a spelling shared by several vocabularies is
#: listed once — ``report`` is spelled identically by Crossref, BibLaTeX, Zotero
#: and CSL. Repeating it in a later block does nothing (the second literal wins
#: and the first is dead), and one such repetition had in fact displaced the CSL
#: spelling of a blog post, so ``post-weblog`` silently mapped to ``other``.
#: :func:`test_no_input_type_is_shadowed_by_a_repeated_key` guards against that
#: happening again.
_KIND_MAP = {
    # Crossref
    "journal-article": "article",
    "proceedings-article": "article",
    "posted-content": "preprint",
    "book": "book",
    "monograph": "book",
    "reference-book": "book",
    "edited-book": "book",
    "book-chapter": "chapter",
    "book-section": "chapter",
    "book-part": "chapter",
    "dissertation": "thesis",
    "report": "report",
    "report-component": "report",
    "dataset": "dataset",
    "component": "other",
    "peer-review": "other",
    # BibTeX
    "article": "article",
    "inproceedings": "article",
    "conference": "article",
    "inbook": "chapter",
    "incollection": "chapter",
    "booklet": "book",
    "phdthesis": "thesis",
    "mastersthesis": "thesis",
    "techreport": "report",
    "manual": "report",
    "unpublished": "preprint",
    "misc": "other",
    "online": "webpage",
    "electronic": "webpage",
    # Zotero / CSL. Each Zotero itemType is followed by the CSL type it exports
    # to; "report", "thesis", "preprint" and "webpage" are spelled the same in
    # both and are already listed above.
    "journalarticle": "article",
    "article-journal": "article",
    "conferencepaper": "article",
    "paper-conference": "article",
    "booksection": "chapter",
    "chapter": "chapter",
    "thesis": "thesis",
    "preprint": "preprint",
    "webpage": "webpage",
    "blogpost": "webpage",
    "post-weblog": "webpage",
    "manuscript": "preprint",
    # DataCite resourceTypeGeneral. Every other value DataCite emits for a
    # citable work already folds onto a key above (JournalArticle,
    # ConferencePaper, Dissertation, Preprint, Report, Book, Dataset);
    # BookChapter was the one spelling with no equivalent, so a chapter
    # deposited with DataCite came back as "other" and its type was never
    # checked against the entry's.
    "bookchapter": "chapter",
}


def normalize_kind(value: object) -> str:
    """Map a registry or adapter type string onto the internal vocabulary."""
    key = fold(value).replace(" ", "-")
    return _KIND_MAP.get(key, "other")
