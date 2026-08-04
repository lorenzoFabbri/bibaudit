"""Field-by-field comparison of a stored reference against registry records.

This is the module that decides whether a citation is trustworthy, and it does
so with string comparisons and documented thresholds only. No model is
consulted, so two runs over the same inputs produce identical verdicts and any
verdict can be re-derived by hand from the cached registry response.

The design commitment that shapes everything here: **a disagreement is reported,
never resolved.** The tool has no authority to decide that Crossref is right and
the bibliography is wrong. It says what each side holds and who to ask.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field

from . import benign
from .model import (
    ANSWERED,
    NOT_ASKED,
    REGISTRIES,
    UNREACHABLE,
    Consultation,
    Issue,
    Record,
    Reference,
    Result,
    is_registry_artifact,
)
from .names import compare_author_lists
from .normalize import clean, first_page, fold, normalize_doi, normalize_kind, similarity

__all__ = ["CHECKED_FIELDS", "Thresholds", "compare", "confirm_without_id", "verdict_for"]

#: Every stored field the tool checks. Anything absent from this tuple is
#: carried through to the report but never adjudicated — being explicit about
#: the boundary is part of being honest about what "verified" means.
#:
#: ``doi`` is deliberately not here. It is the lookup *key*, not a field with
#: two independent opinions to weigh: the record came back because the stored
#: DOI resolved to it. When the record's own DOI differs anyway the cause is a
#: redirect or an alias, which :func:`_check_doi` states as a note and which can
#: never be a failure — so it is checked, but never adjudicated, and putting it
#: in this tuple would promise otherwise.
CHECKED_FIELDS = (
    "title", "authors", "year", "container", "volume", "issue", "pages", "publisher",
)

#: Type pairs treated as the same shape of work rather than a contradiction.
#: A preprint's DOI legitimately resolves to a journal-article type once
#: published; a report is deposited as a preprint on some servers; and a
#: chapter is deposited as a book-part inconsistently — including by Open
#: Library, which has no ``chapter`` type at all and reports every ISBN as a
#: ``book`` (see ``registries/openlibrary.py``). Shared between
#: :func:`_check_kind` (a *found* record whose type disagrees with the
#: entry's) and :func:`confirm_without_id` (a *candidate*, from search,
#: being screened before it is accepted at all) so the two checks cannot
#: drift into disagreeing about which pairs are the same work catalogued
#: differently. Before this was shared, ``confirm_without_id``'s own
#: same-type-only test rejected every Open Library candidate for a
#: ``@incollection`` entry outright — the exact case ``audit.py`` added
#: Open Library search for.
_COMPATIBLE_KINDS = frozenset(
    {
        frozenset({"article", "preprint"}),
        frozenset({"chapter", "book"}),
        frozenset({"report", "preprint"}),
    }
)


def _in_registry_order(names: Iterable[str]) -> list[str]:
    """*names* sorted into :data:`~bibaudit.model.REGISTRIES` order.

    Every message that names more than one registry goes through this, so two
    runs over the same evidence produce byte-identical reports whatever order
    the records happened to be inserted in. A verdict that can be re-derived
    but whose wording shuffles is a diff nobody can review.
    """
    rank = {name: index for index, name in enumerate(REGISTRIES)}
    return sorted(names, key=lambda name: (rank.get(name, len(rank)), name))


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Similarity cut-offs, all on :func:`~bibaudit.normalize.similarity`.

    The title bands are deliberately wide at the bottom. ``wrong_work`` can sit
    as low as 0.55 only because a title that low is cross-checked against the
    author list before ``WRONG-WORK`` is declared: a single fuzzy score is not
    enough evidence to accuse a bibliography of pointing at the wrong paper.

    Books get a lower bar than articles because book titles are recorded with
    far more variation — subtitles, edition statements and series names come and
    go between registries.
    """

    #: Below this, the DOI may be pointing at a different work entirely.
    wrong_work: float = 0.55
    #: Below this (and above ``wrong_work``) the titles genuinely disagree.
    title_mismatch: float = 0.85
    #: Below this (and above ``title_mismatch``) the difference is drift worth
    #: reporting but not failing: markup, a lost subtitle, an en-dash.
    title_ok: float = 0.97
    #: Same bands, relaxed, for books and chapters.
    wrong_work_book: float = 0.45
    title_mismatch_book: float = 0.75
    #: Confirming an entry that carries no identifier at all needs a high bar
    #: plus corroborating author and year — see :func:`confirm_without_id`.
    search_confirm: float = 0.90

    def title_bands(self, kind: str) -> tuple[float, float]:
        """Return ``(wrong_work, mismatch)`` cut-offs for a work of *kind*."""
        if kind in {"book", "chapter"}:
            return self.wrong_work_book, self.title_mismatch_book
        return self.wrong_work, self.title_mismatch


@dataclass(slots=True)
class _Context:
    """Working state for one comparison, so the checks stay small."""

    ref: Reference
    primary: Record
    corroborator: Record | None
    thresholds: Thresholds
    issues: list[Issue] = dc_field(default_factory=list)
    suppressed: list[Issue] = dc_field(default_factory=list)

    def add(
        self,
        field: str,
        kind: str,
        severity: str,
        stored: object = "",
        registry: object = "",
        *,
        source: str = "",
        note: str = "",
    ) -> None:
        self.issues.append(
            Issue(
                field=field,
                kind=kind,
                severity=severity,  # type: ignore[arg-type]
                stored=clean(stored),
                registry=clean(registry),
                source=source or self.primary.source,
                note=note,
            )
        )

    def add_artifact(
        self, field: str, stored: object, registry: object, reason: str
    ) -> None:
        """Record a difference attributable to a known registry defect."""
        self.suppressed.append(
            Issue(
                field=field,
                kind="registry-artifact",
                severity="info",
                stored=clean(stored),
                registry=clean(registry),
                source=self.primary.source,
                note=reason,
            )
        )


def _registry_value(ctx: _Context, attr: str) -> tuple[str, str]:
    """Value for *attr* and the registry it came from.

    The primary registry wins; the corroborator fills gaps. Filling a gap is not
    arbitration — it only happens when the primary has nothing to say.
    """
    value = getattr(ctx.primary, attr, None)
    if value:
        return clean(value), ctx.primary.source
    if ctx.corroborator:
        alt = getattr(ctx.corroborator, attr, None)
        if alt:
            return clean(alt), ctx.corroborator.source
    return "", ""


def _alternate_containers(ctx: _Context) -> list[str]:
    """Every *other* container title the registries carry for this work.

    Kept beside the primary value rather than folded into it because the
    principle is CLAUDE.md's own, one field over: *any year the registry itself
    carries is acceptable*, print or online-first. A container is the same
    shape of fact. Crossref deposits ``["Methods in Molecular Biology",
    "Methods in Biobanking"]`` for 10.1007/978-1-59745-423-0_7 — the series and
    the volume — and a chapter citing either is citing a container Crossref
    named. See :attr:`~bibaudit.model.Record.container_alternates`.
    """
    values = list(ctx.primary.container_alternates)
    if ctx.corroborator:
        values.extend(ctx.corroborator.container_alternates)
    return [clean(value) for value in values if clean(value)]


def _check_scalar(
    ctx: _Context,
    field: str,
    attr: str,
    stored: object,
    *,
    also_accepted: Sequence[str] = (),
    optional_for_kinds: Collection[str] = (),
) -> None:
    """Compare one plain string field.

    Order of judgement, and it matters:
    1. If the registries disagree with *each other*, say so and stop — the tool
       has no basis for choosing between them.
    2. If the stored value matches either registry, it is correct.
    3. If a documented registry defect explains the difference, record it as an
       artifact rather than a defect.
    4. Otherwise it is a mismatch.

    *also_accepted* are further values the registry itself carries for the same
    field, and matching one of them is step 2 by another route — not a
    tolerance. Only the container check passes any; see
    :func:`_alternate_containers`.

    *optional_for_kinds* names entry kinds for which a registry value the
    bibliography omits is not worth a ``missing`` warning at all. A book has
    no volume-in-a-journal or issue-in-a-volume to be missing; see the
    ``compare()`` call sites for the concrete false alarm this prevents, and
    :func:`_check_pages` for the identical guard on ``pages``, which is not a
    plain scalar field and cannot go through this function.
    """
    stored_text = clean(stored)
    registry_text, source = _registry_value(ctx, attr)

    if not registry_text:
        return

    if not stored_text:
        if normalize_kind(ctx.ref.kind) in optional_for_kinds:
            return
        # The registry knows something the bibliography omits. Incompleteness is
        # worth surfacing, but it is not evidence of fabrication.
        ctx.add(field, "missing", "warning", "", registry_text, source=source)
        return

    alt_text = clean(getattr(ctx.corroborator, attr, "")) if ctx.corroborator else ""

    if fold(stored_text) == fold(registry_text):
        if stored_text != registry_text:
            ctx.add(field, "cosmetic", "info", stored_text, registry_text, source=source)
        return

    for accepted in also_accepted:
        if fold(stored_text) != fold(accepted):
            continue
        # Stated rather than passed over in silence. The registry's *first*
        # value is what a reader sees on the landing page, so an entry whose
        # booktitle is "Methods in Biobanking" beside a page headed "Methods in
        # Molecular Biology" looks wrong until the report says the tool saw both
        # and that Crossref supplies both. ``info``, and its kind is not one
        # ``verdict_for`` reads, so the entry stays OK — the same treatment
        # ``year/alternate-date`` gets, for the same reason.
        ctx.add(
            field, "alternate-title", "info", stored_text, registry_text,
            source=source,
            note=f"{source} also carries {accepted!r} for this work",
        )
        return

    if alt_text and fold(stored_text) == fold(alt_text):
        ctx.add(
            field,
            "disputed",
            "info",
            stored_text,
            f"{ctx.primary.source}={registry_text!r} vs {ctx.corroborator.source}={alt_text!r}",  # type: ignore[union-attr]
            source="both",
            note="stored value matches the corroborating registry",
        )
        return

    if alt_text and fold(alt_text) != fold(registry_text):
        ctx.add(
            field,
            "disputed",
            "info",
            stored_text,
            f"{ctx.primary.source}={registry_text!r} vs {ctx.corroborator.source}={alt_text!r}",  # type: ignore[union-attr]
            source="both",
            note="registries disagree with each other",
        )
        return

    reason = benign.classify(field, stored_text, registry_text, ctx.ref, ctx.primary)
    if reason:
        ctx.add_artifact(field, stored_text, registry_text, reason)
        return

    ctx.add(field, "mismatch", "error", stored_text, registry_text, source=source)


def _check_title(ctx: _Context) -> float | None:
    """Compare titles and return the similarity actually achieved.

    The best score against *any* consulted registry is used. PubMed and Crossref
    differ systematically in case and markup, and an entry that matches either
    of them is describing the right paper.
    """
    stored = clean(ctx.ref.title)
    candidates = [(clean(ctx.primary.title), ctx.primary.source)]
    if ctx.corroborator and ctx.corroborator.title:
        candidates.append((clean(ctx.corroborator.title), ctx.corroborator.source))
    candidates = [(text, src) for text, src in candidates if text]

    if not stored:
        if candidates:
            ctx.add("title", "missing", "error", "", candidates[0][0], source=candidates[0][1])
        return None
    if not candidates:
        return None

    best_text, best_source = max(candidates, key=lambda c: similarity(stored, c[0]))
    score = similarity(stored, best_text)
    wrong_work, mismatch = ctx.thresholds.title_bands(normalize_kind(ctx.ref.kind))

    if fold(stored) == fold(best_text):
        if stored != best_text:
            # Same words, different glyphs: a curly apostrophe, an en-dash, a
            # capital. Worth showing, never worth failing a build over.
            ctx.add("title", "cosmetic", "info", stored, best_text, source=best_source)
        return score

    reason = benign.classify("title", stored, best_text, ctx.ref, ctx.primary)
    if reason:
        ctx.add_artifact("title", stored, best_text, reason)
        return score

    if score >= ctx.thresholds.title_ok:
        ctx.add("title", "drift", "info", stored, best_text, source=best_source)
    elif score >= mismatch:
        ctx.add(
            "title", "drift", "warning", stored, best_text, source=best_source,
            note=f"similarity {score:.2f}",
        )
    elif score >= wrong_work:
        ctx.add(
            "title", "mismatch", "error", stored, best_text, source=best_source,
            note=f"similarity {score:.2f}",
        )
    else:
        ctx.add(
            "title", "wrong-work", "error", stored, best_text, source=best_source,
            note=f"similarity {score:.2f}",
        )
    return score


def _check_authors(ctx: _Context) -> bool:
    """Compare the full author list. Returns whether the lists corroborate.

    Comparing only the first author is the common shortcut and it cannot see an
    invented co-author, which is a documented failure mode of generated
    bibliographies. The cost of the full comparison is a longer list of benign
    differences, which is why :mod:`~bibaudit.names` carries explicit handling
    for collective authors, et-al markers, particles and registry mojibake.
    """
    stored = ctx.ref.authors
    registry = ctx.primary.authors or (
        ctx.corroborator.authors if ctx.corroborator else []
    )
    source = ctx.primary.source if ctx.primary.authors else (
        ctx.corroborator.source if ctx.corroborator else ""
    )

    if not stored:
        if registry:
            ctx.add(
                "authors", "missing", "error", "",
                "; ".join(str(n) for n in registry[:3]), source=source,
            )
        return False
    if not registry:
        return False

    diff = compare_author_lists(stored, registry)

    for position, reason in sorted(diff.reasons.items()):
        ctx.add_artifact(
            "authors",
            str(stored[position - 1]) if position <= len(stored) else "",
            str(registry[position - 1]) if position <= len(registry) else "",
            reason,
        )

    if diff.count_differs:
        ctx.add(
            "authors", "count", "warning",
            f"{diff.stored_count} authors", f"{diff.registry_count} authors",
            source=source,
        )

    for position, left, right in diff.mismatches:
        ctx.add(
            "authors", "mismatch", "error", f"#{position} {left}", f"#{position} {right}",
            source=source,
        )

    return diff.clean


def _check_year(ctx: _Context) -> None:
    """Compare the year against every date the registries hold.

    Any date the registry itself carries is accepted: an entry citing the
    online-first year of a work printed the following February is not wrong.
    """
    stored = ctx.ref.year
    accepted = dict(ctx.primary.years)
    if ctx.corroborator:
        accepted.update(
            {f"{ctx.corroborator.source}:{k}": v for k, v in ctx.corroborator.years.items()}
        )

    if stored is None:
        if accepted:
            ctx.add("year", "missing", "warning", "", str(ctx.primary.year or ""))
        return
    if not accepted:
        return

    detail = ", ".join(f"{k}={v}" for k, v in sorted(accepted.items()))

    if stored in accepted.values():
        _note_alternate_date(ctx, stored, accepted, detail)
        return

    registry_year = ctx.primary.year
    reason = benign.classify("year", stored, registry_year, ctx.ref, ctx.primary)
    if reason:
        ctx.add_artifact("year", stored, registry_year, reason)
        return

    ctx.add("year", "mismatch", "error", str(stored), detail)


def _note_alternate_date(
    ctx: _Context, stored: int, accepted: dict[str, int], detail: str
) -> None:
    """State *which* of the registry's own dates an accepted year is.

    Reached only when the stored year is one the registry itself carries, so
    nothing here is ever a disagreement — a work posted online in December 2020
    and printed in February 2021 has two correct years and citing either is
    right. What was missing is the *reason*: the report simply said nothing, so
    a reader comparing an entry's ``year = {2020}`` against a Crossref landing
    page showing 2021 had no way to learn that the tool had seen both dates and
    accepted the earlier one.

    ``benign._year_online_first`` was written to supply exactly that sentence
    and could never fire, because ``_check_year`` returned before consulting it
    on precisely the inputs it recognises — dead code in a suppression list,
    which reads to anyone auditing ``benign.py`` like a guarantee that is being
    honoured.

    Emitted at ``info`` severity and *not* as an artifact. An artifact means
    "the registry's value is defective", and here neither value is: promoting
    an online-first citation to ``REGISTRY-ARTIFACT`` would relabel a large
    slice of an ordinary epidemiology bibliography — online-first is the norm
    at most journals — as though the publisher's metadata were broken, and
    would move those entries out of ``OK`` for no reader-visible benefit.
    """
    preferred = ctx.primary.year
    if preferred is None or stored == preferred:
        # Either the primary registry has no opinion on the date at all, or the
        # entry cites the one it prefers. Nothing to explain.
        return
    reason = benign.classify("year", stored, preferred, ctx.ref, ctx.primary)
    if not reason:
        # ``benign`` is only ever shown the primary record, so it cannot explain
        # a year that only the *corroborating* registry carries — MEDLINE's
        # ``DP`` against Crossref's ``issued`` is a routine disagreement. Name
        # the slot from `accepted`, whose corroborator keys are already
        # qualified ("pubmed:issued"), rather than asserting the primary holds a
        # date it does not.
        holders = [key for key, value in sorted(accepted.items()) if value == stored]
        reason = (
            f"cites the {', '.join(holders)} date; "
            f"{ctx.primary.source} prefers {preferred}"
        )
    ctx.add("year", "alternate-date", "info", str(stored), detail, note=reason)


def _check_doi(ctx: _Context) -> None:
    """Note when the registry's own DOI is not the one that was looked up.

    The DOI is the lookup key: this record is here *because* the stored DOI
    resolved to it. So the registry disagreeing with itself is not a defect in
    the bibliography and can never be a failure — the only ways it happens are
    a redirect (``doi.org`` content-negotiates the JSTOR DOI 10.2307/2669548 to
    the publisher's own) and an alias (a deposit re-registered under a new
    prefix). Both are worth one line in the report, because a reader who later
    looks the entry up by hand will land on a different identifier than the one
    in their ``.bib`` and needs to know that is expected.

    This also makes ``benign._doi_redirecting_prefix`` reachable. ``classify``
    was never called with ``field="doi"`` by any caller, so the rule was
    documentation shaped like code: ``docs/registry-artifacts.md`` promised
    JSTOR redirects were "reported as a note", and nothing reported anything.

    Comparison is on :func:`~bibaudit.normalize.normalize_doi`, never on the raw
    strings: DOIs are case-insensitive by specification and Crossref echoes them
    back as deposited, so ``10.1158/1055-9965.EPI-20-0378`` against
    ``10.1158/1055-9965.epi-20-0378`` would otherwise print an alias note on an
    entry that is character-for-character correct.
    """
    stored = normalize_doi(ctx.ref.doi)
    registry = normalize_doi(ctx.primary.doi)
    if not stored or not registry or stored == registry:
        return
    reason = benign.classify("doi", ctx.ref.doi, ctx.primary.doi, ctx.ref, ctx.primary)
    ctx.add(
        "doi",
        "alias",
        "info",
        clean(ctx.ref.doi),
        clean(ctx.primary.doi),
        note=reason
        or "the stored DOI resolved to a record registered under a different DOI",
    )


def _check_pages(ctx: _Context) -> None:
    """Compare the opening page or article number only.

    Closing pages disagree constantly and harmlessly between registries, so
    comparing them produces noise without evidence.

    A book has no opening page of anything to be missing — see the
    ``optional_for_kinds`` argument :func:`_check_scalar` takes for the
    ``volume``/``issue`` version of the same guard, and the comment on
    ``compare()``'s own call to this function for the concrete false alarm
    both exist to prevent. Once :mod:`~bibaudit.registries.openlibrary`
    started supplying ``number_of_pages`` — the book's total length, not a
    citation locator — every correctly complete ``@book`` entry with no
    ``pages`` field of its own (nearly all of them; BibTeX has no
    convention for one) gained a ``pages/missing`` warning it could neither
    fix nor should. A stored ``pages`` value is still compared normally
    below: this guard only silences the "you never said anything" case, not
    a real disagreement.
    """
    stored = clean(ctx.ref.pages)
    registry, source = _registry_value(ctx, "pages")
    if not registry:
        return
    if not stored:
        if normalize_kind(ctx.ref.kind) == "book":
            return
        ctx.add("pages", "missing", "warning", "", registry, source=source)
        return
    if first_page(stored) == first_page(registry):
        return

    alt = clean(ctx.corroborator.pages) if ctx.corroborator else ""
    if alt and first_page(stored) == first_page(alt):
        ctx.add(
            "pages", "disputed", "info", stored,
            f"{ctx.primary.source}={registry!r} vs {ctx.corroborator.source}={alt!r}",  # type: ignore[union-attr]
            source="both", note="stored value matches the corroborating registry",
        )
        return

    reason = benign.classify("pages", stored, registry, ctx.ref, ctx.primary)
    if reason:
        ctx.add_artifact("pages", stored, registry, reason)
        return

    ctx.add("pages", "mismatch", "error", stored, registry, source=source)


def _check_kind(ctx: _Context) -> None:
    """Flag an entry whose type is incompatible with the resolved work's.

    A ``@book`` whose DOI resolves to a journal article is nearly always citing a
    *review* of the book rather than the book. Type incompatibility rejected
    four of nine identifier proposals in earlier manual work, which is why it is
    checked rather than assumed.
    """
    stored_kind = normalize_kind(ctx.ref.kind)
    registry_kind = normalize_kind(ctx.primary.kind)
    if stored_kind == "other" or registry_kind == "other":
        return
    if stored_kind == registry_kind:
        return
    if frozenset({stored_kind, registry_kind}) in _COMPATIBLE_KINDS:
        return
    ctx.add(
        "kind", "incompatible", "warning", stored_kind, registry_kind,
        note="entry type disagrees with the resolved work's type",
    )


#: Registries whose data model carries no retraction signal at all, so an outage
#: at one of them is not ignorance *about retraction*. DataCite's schema has no
#: retraction, withdrawal or concern element and ``registries/datacite.py``
#: accordingly never sets ``Record.retracted``; naming it in the
#: ``retraction-unverified`` note below would manufacture a doubt that a
#: reachable DataCite could not have resolved, on every dataset, preprint and
#: Zenodo deposit in the file.
#:
#: Open Library is here for the same reason and was found the same way, one
#: audit later: ``registries/openlibrary.py`` contains no retraction handling of
#: any kind — the word does not appear in it — because it is a book catalogue
#: with nothing in its data model to carry a notice. But ``audit.py`` does add
#: ``"openlibrary"`` to *unreachable* when the ISBN leg times out, so without
#: this entry an Open Library outage rendered "retraction status not
#: corroborated: openlibrary could not be reached" against every book in the
#: file — manufacturing precisely the doubt the DataCite paragraph above forbids,
#: and manufacturing it about the one registry least able to resolve it.
#:
#: Written as an exclusion rather than as the list of registries that *do* carry
#: the signal, because the two fail in opposite directions: a registry missing
#: from an inclusion list would be silently dropped from the notice, and
#: understating ignorance about retraction is the exact failure this check
#: exists to prevent. Whoever adds the next registry gets counted by default and
#: has to come here to opt out. ``"retraction-watch"`` is deliberately *not*
#: here: it is the one source in this set that exists only to carry the signal.
_NO_RETRACTION_SIGNAL = frozenset({"datacite", "openlibrary"})

#: Folded ``retraction_kind`` values that are **not** a retraction: the work has
#: not been pulled back at all. An expression of concern is an editor recording
#: a doubt; the paper stands, stays in the literature, and citing it is
#: legitimate provided the citing author has read the notice.
#:
#: 10.1371/journal.pone.0064723 is the auditable instance. Its Crossref record
#: (fetched 2026-08-01, kept verbatim in
#: ``tests/data/compare_crossref_expression_of_concern.json``) carries two
#: ``expression_of_concern`` entries in ``updated-by`` and no retraction,
#: withdrawal or removal of any kind — and ``registries/crossref.py``, whose
#: ``_RETRACTION_PRIORITY`` ranks a concern in the same tuple as a retraction,
#: therefore hands this module ``retracted=True``. The report then printed
#: ``RETRACTED — the cited work has itself been retracted`` about a paper nobody
#: has retracted: a false statement about a named work, made with the tool's
#: full authority, which is the one output this project may never produce.
#:
#: Membership is an exact match on :func:`~bibaudit.normalize.fold`, never a
#: substring or a prefix. The two vocabularies do overlap in real data — the
#: Lancet Neurology notice 10.1016/S1474-4422(26)00052-9 is titled "Resolution
#: of expression of concern", and resolving a concern is as often a retraction
#: as an exoneration — so a substring test would let a registry that reports a
#: *retraction* in wording that mentions a concern be downgraded to a doubt.
#: Anything not listed here, ``retraction_kind=None`` included, counts as a
#: retraction: a registry this module has never heard of cannot talk it out of
#: the finding.
_CONCERN_KINDS = frozenset({"expression of concern"})


def _detail(kinds: Mapping[str, str], names: Sequence[str]) -> str:
    """The registry column for one status issue, qualified only when it must be.

    One asserting registry: the source column already names it, and
    "pubmed  pubmed=Retracted Publication" is the kind of doubled label that
    makes a report look machine-generated rather than read.
    """
    if len(names) == 1:
        return kinds[names[0]]
    return "; ".join(f"{name}={kinds[name]}" for name in names)


def _status_issues(
    records: Mapping[str, Record], unreachable: Collection[str]
) -> tuple[list[Issue], bool]:
    """Every ``status`` finding for one work, and whether it is *retracted*.

    Three statements can be true at once about the same work and none of them is
    a paraphrase of another, so each gets its own issue:

    ``status/retracted`` (error)
        At least one registry that answered records a retraction. This is a
        union over every registry, never the primary registry's opinion alone.
        The tool used to read the flag off Crossref (or, failing that, DataCite)
        and drop PubMed's, so a paper NLM records as ``PT - Retracted
        Publication`` but whose publisher never deposited the Crossref
        ``updated-by`` linkage passed as clean. That is the worst possible miss,
        and it defeats the stated reason PubMed is consulted at all: it is
        curated separately, so it is the source that can know something Crossref
        does not. The note names *which* registry asserted it, because "both
        curated sources agree" and "only PubMed knows about this; the publisher
        never deposited the linkage" are different things to hand a reader — the
        second is also a bug report for the publisher.

    ``status/expression-of-concern`` (error)
        A registry records a concern and *nobody* records a retraction. Reported
        with the same weight — an author who cites a paper under an expression
        of concern needs to know before submission — but never under the word
        "retracted". See :data:`_CONCERN_KINDS` for the paper this was found on.
        It stays an error, so an entry that failed before this distinction
        existed still fails: the finding is re-labelled, never relaxed.

    ``status/retraction-unverified`` (info)
        No registry that answered records a retraction, but a registry that
        could have recorded one was unreachable. A timeout is ignorance, and
        silence from a registry nobody could reach is not a clean bill of
        health — least of all on the one field where a miss puts a retracted
        paper into a manuscript. Fires only on a *proved* outage, never on a
        registry that simply had nothing for this DOI: PubMed holds no PMID for
        most non-biomedical work, and treating that as unverified would put the
        line on nearly every entry of an ordinary bibliography and teach readers
        to skip it.

        Deliberately ``info``, so the verdict does not move: an outage is not a
        defect in anybody's bibliography, which is why ``UNCHECKED`` does not
        fail either, and a run-wide PubMed outage must not relabel 400 correct
        entries as ``INCOMPLETE`` ("the registry holds fields the entry omits"),
        which is not what happened. It reaches the reader through the JSON
        report's ``issues`` list, through ``--verbose``, and beside
        ``consulted["pubmed"] == "unreachable"``.

    Direction — whether a record *is* a retraction notice or *was* retracted —
    is decided upstream in the registry clients and read here as a plain flag.
    ``retraction_kind`` is inspected only to separate a concern from a
    retraction, by exact equality on a closed set that contains no
    retraction-shaped string; nothing here can turn a retracted paper into a
    clean one, and no title, type or notice wording is sniffed at all.
    """
    asserting = _in_registry_order(
        name for name, record in records.items() if record.retracted
    )
    kinds = {name: records[name].retraction_kind or "retracted" for name in asserting}
    concerned = [name for name in asserting if fold(kinds[name]) in _CONCERN_KINDS]
    retracting = [name for name in asserting if name not in set(concerned)]

    issues: list[Issue] = []

    if retracting:
        # Only a registry that answered and recorded *nothing* dissents. One
        # that recorded a concern has not contradicted the retraction, and
        # listing it as carrying "no retraction linkage" would read as a second
        # opinion against the finding when it is corroboration of a weaker one.
        silent = _in_registry_order(set(records) - set(asserting))
        note = f"the cited work has been retracted; recorded by {', '.join(retracting)}"
        if silent:
            note += (
                f" and not by {', '.join(silent)}, which answered for this work "
                "and carries no retraction linkage"
            )
        issues.append(
            Issue(
                field="status",
                kind="retracted",
                severity="error",
                stored="",
                registry=_detail(kinds, retracting),
                source=",".join(retracting),
                note=note,
            )
        )

    if concerned:
        issues.append(
            Issue(
                field="status",
                kind="expression-of-concern",
                severity="error",
                stored="",
                registry=_detail(kinds, concerned),
                source=",".join(concerned),
                note=(
                    "an expression of concern has been published about the cited "
                    f"work; recorded by {', '.join(concerned)}. That is a stated "
                    "doubt, not a retraction: the work stands, and citing it is "
                    "legitimate once the notice has been read"
                ),
            )
        )

    if not retracting:
        blind = _in_registry_order(set(unreachable) - _NO_RETRACTION_SIGNAL)
        if blind:
            issues.append(
                Issue(
                    field="status",
                    kind="retraction-unverified",
                    severity="info",
                    stored="",
                    registry="",
                    source=",".join(blind),
                    note=(
                        f"retraction status not corroborated: {', '.join(blind)} "
                        "could not be reached, and no registry that did answer "
                        "records a retraction — which is not the same as there "
                        "being none"
                    ),
                )
            )

    return issues, bool(retracting)


def _consultations(
    records: Mapping[str, Record],
    unreachable: Collection[str],
    asked: Collection[str] | None,
) -> dict[str, Consultation]:
    """What each registry contributed — see :data:`~bibaudit.model.Consultation`.

    *asked* is the set of registries actually queried on this reference's
    behalf. It is separate from *records* because a registry that answers "I do
    not hold this DOI" contributes real evidence and leaves no record behind,
    and separate from *unreachable* because a registry nobody queried is not
    an outage.

    When *asked* is ``None`` the caller has not said, and the only registries
    that can be *proved* to have participated are those that answered or timed
    out. That understates the evidence — a registry that answered "not mine"
    then reads as ``not-asked`` — but it errs in the safe direction: it can only
    make a verdict look less well supported than it is, never more. It must not
    be papered over by calling an unasked registry ``unreachable`` instead:
    ``compare`` takes its "nothing answered" branch off the unreachable set, so
    that would silently turn every genuine ``BAD-ID`` into ``UNCHECKED`` — a
    fabricated DOI reported as a network problem.

    *unreachable* wins over *asked*, deliberately. It is a run-wide set today,
    so a registry that fell over while another reference was being resolved is
    reported ``unreachable`` here even if this particular DOI never reached it.
    That is the same set ``compare`` derives ``UNCHECKED`` from, and the point of
    this mapping is to explain the verdict that was actually reached: a report
    saying "everything answered" beside a verdict of "nothing could be reached"
    would be worse than a slightly pessimistic one.
    """
    proven = set(records) | set(unreachable)
    participating = proven if asked is None else proven | set(asked)
    names = [*REGISTRIES, *sorted(participating - set(REGISTRIES))]

    out: dict[str, Consultation] = {}
    for name in names:
        if name in unreachable:
            out[name] = UNREACHABLE
        elif name in participating:
            out[name] = ANSWERED
        else:
            out[name] = NOT_ASKED
    return out


def verdict_for(
    issues: Sequence[Issue],
    suppressed: Sequence[Issue],
    *,
    retracted: bool = False,
    authors_ok: bool = True,
) -> str:
    """Derive an overall verdict from a set of issues.

    Exposed rather than kept private so that a caller which removes issues —
    :mod:`~bibaudit.suppress` does exactly that — can re-derive the verdict by
    the same rule instead of inventing a second, divergent one.
    """
    if retracted:
        return "RETRACTED"

    error_kinds = {issue.kind for issue in issues if issue.severity == "error"}

    if "unresolved" in error_kinds:
        return "BAD-ID"
    if "absent" in error_kinds:
        return "UNCONFIRMED"

    if "wrong-work" in error_kinds:
        # A low title score alone is not enough. If the author lists agree, the
        # likelier explanation is a registry title defect, and calling that a
        # wrong paper is an accusation the evidence does not support.
        return "WRONG-WORK" if not authors_ok else "FIELD-MISMATCH"

    if error_kinds:
        # ``status/expression-of-concern`` lands here too, which is a compromise
        # and is documented as one: the entry is failing and its issue line says
        # exactly what is wrong with it, but the group heading it prints under
        # reads "right work, but stored metadata disagrees". The honest label is
        # a verdict of its own, and adding one means touching
        # ``model.VERDICTS``/``FAILING_VERDICTS``, ``report._VERDICT_HELP`` and
        # the README table together — never this line alone, because a verdict
        # absent from ``model.VERDICTS`` is silently dropped from the terminal
        # report by ``report.render_text``.
        return "FIELD-MISMATCH"

    if any(i.kind == "disputed" for i in issues):
        return "DISPUTED"
    if any(i.severity == "warning" for i in issues):
        return "INCOMPLETE"
    # Two different claims, and mapping both onto REGISTRY-ARTIFACT meant a
    # reader could not tell "the registry is known to be wrong here, and here is
    # the documented defect" from "someone on this project decided not to care".
    # README defines REGISTRY-ARTIFACT as the first; a .bibaudit.toml
    # adjudication is the second. Both are non-failing, both stay visible, and
    # the human's say-so is the one that ranks higher, because it can go stale
    # and is the one worth re-reading.
    if any(not is_registry_artifact(i) for i in suppressed):
        return "ADJUDICATED"
    if suppressed:
        return "REGISTRY-ARTIFACT"
    if any(i.kind == "drift" for i in issues):
        return "TITLE-DRIFT"
    if any(i.kind == "cosmetic" for i in issues):
        return "COSMETIC"
    return "OK"


def compare(
    ref: Reference,
    records: dict[str, Record],
    *,
    thresholds: Thresholds | None = None,
    unreachable: set[str] | None = None,
    asked: Collection[str] | None = None,
) -> Result:
    """Compare one stored reference against the registry records found for it.

    Parameters
    ----------
    ref:
        The citation as stored.
    records:
        Registry name to record, e.g. ``{"crossref": ..., "pubmed": ...}``.
        Absent means the registry answered and had nothing.
    unreachable:
        Registries that could not be reached at all. A registry that timed out
        is *unknown*, not *empty*, and must never contribute to a "does not
        exist" conclusion.
    asked:
        Registries actually queried on *this* reference's behalf. Callers
        should always pass it: it is the only way ``Result.consulted`` can tell
        "PubMed answered and had nothing" from "PubMed was never asked because
        ``--no-corroborate`` was given". See :func:`_consultations` for what is
        assumed when it is omitted, and why the assumption errs low.

    Returns
    -------
    Result
        Carrying the verdict, the per-field issues, and the differences that
        were suppressed as known registry defects.
    """
    thresholds = thresholds or Thresholds()
    unreachable = unreachable or set()
    result = Result(ref=ref)
    result.consulted = _consultations(records, unreachable, asked)

    primary = records.get("crossref") or records.get("datacite")
    corroborator = records.get("pubmed")
    if primary is None and corroborator is not None:
        primary, corroborator = corroborator, None
    if primary is None and records:
        # A record from a registry this function does not name. Selecting by
        # explicit key alone meant such a record was ignored *and* the entry
        # then fell through to "resolves in no consulted registry" — a BAD-ID
        # reported on a DOI that a registry had, in the same run, resolved.
        # Whoever adds the next registry to REGISTRIES gets a sane default
        # instead of a fabrication warning.
        primary = records[_in_registry_order(records)[0]]

    if primary is None:
        if unreachable and not records:
            # Nothing answered. Silence from an unreachable registry is not
            # evidence of anything.
            result.verdict = "UNCHECKED"
            result.issues.append(
                Issue(
                    field="doi" if ref.doi else "identifier",
                    kind="unreachable",
                    severity="info",
                    stored=ref.identifier or "",
                    note="no registry could be reached; not checked",
                )
            )
            return result
        if ref.identifier:
            result.verdict = "BAD-ID"
            result.issues.append(
                Issue(
                    field="doi" if ref.doi else "identifier",
                    kind="unresolved",
                    severity="error",
                    stored=ref.identifier,
                    note="resolves in no consulted registry",
                )
            )
        else:
            result.verdict = "UNCONFIRMED"
            result.issues.append(
                Issue(
                    field="identifier",
                    kind="absent",
                    severity="error",
                    note="no identifier stored and no confident registry match",
                )
            )
        return result

    ctx = _Context(ref=ref, primary=primary, corroborator=corroborator, thresholds=thresholds)

    title_score = _check_title(ctx)
    authors_ok = _check_authors(ctx)
    _check_year(ctx)
    _check_scalar(
        ctx, "container", "container", ref.container,
        also_accepted=_alternate_containers(ctx),
    )
    # A monograph series volume ("Lecture Notes in ...", vol. 12) is a real,
    # occasional exception, but the ordinary book has no volume-in-a-journal
    # or issue-in-a-volume to be missing, and a bibliography almost never
    # states one for a book that lacks it. `optional_for_kinds` only silences
    # the "the entry never said anything" case — a stored value that
    # disagrees with the registry's is still reported below, series volumes
    # included.
    _check_scalar(ctx, "volume", "volume", ref.volume, optional_for_kinds={"book"})
    _check_scalar(ctx, "issue", "issue", ref.issue, optional_for_kinds={"book"})
    _check_pages(ctx)
    _check_scalar(ctx, "publisher", "publisher", ref.publisher)
    _check_kind(ctx)
    _check_doi(ctx)

    # Every record that answered, not just the primary — and every registry that
    # could not answer at all, because ignorance about retraction is not the
    # same fact as an absence of one. See _status_issues.
    status, retracted = _status_issues(records, unreachable)
    ctx.issues.extend(status)

    result.issues = ctx.issues
    result.suppressed = ctx.suppressed
    result.title_similarity = title_score
    result.verdict = verdict_for(
        ctx.issues, ctx.suppressed, retracted=retracted, authors_ok=authors_ok
    )
    return result


def _candidate_label(candidate: Record) -> str:
    """A human-readable stand-in for *candidate* in a rejection message.

    Most candidates carry a DOI and it is the obvious label. Open Library's
    do not — see ``registries/openlibrary.py``, which mints no DOI of its
    own — and falling back to ``None`` there would print ``"None: type book
    != article"``, which reads as a bug report against this function rather
    than as an explanation of what was rejected and why.
    """
    return candidate.doi or (repr(candidate.title) if candidate.title else "<candidate>")


def confirm_without_id(
    ref: Reference,
    candidates: list[Record],
    *,
    thresholds: Thresholds | None = None,
) -> tuple[Record | None, str]:
    """Pick the registry record that confirms an identifier-less reference.

    Requires three independent signals to agree — title, author surname and year
    — because a title-only match is exactly how a plausible-but-wrong work gets
    adopted. Returns the record and an explanation, or ``(None, reason)``.

    That bar has to be enforced on the *candidate*, not merely attempted: the
    author and year checks below are both written as "skip the comparison if
    the candidate has nothing to compare", which is correct when the
    candidate is simply a different shape of *complete* record (Crossref
    omits a year field it never had) but was silently exploitable by a
    candidate that has *no* author or year data at all — Open Library's
    crowd-sourced catalogue is "noticeably patchier than Crossref's" (see
    ``registries/openlibrary.py``'s module docstring) and a great many of its
    records carry a title and nothing else. Without the explicit guard below,
    such a record confirmed on the title match alone, both checks having
    silently done nothing.
    """
    thresholds = thresholds or Thresholds()
    stored_kind = normalize_kind(ref.kind)
    best: tuple[float, Record] | None = None
    rejections: list[str] = []

    for candidate in candidates:
        score = similarity(ref.title, candidate.title)
        if score < thresholds.search_confirm:
            continue

        candidate_kind = normalize_kind(candidate.kind)
        if (
            stored_kind != "other"
            and candidate_kind != "other"
            and stored_kind != candidate_kind
            and frozenset({stored_kind, candidate_kind}) not in _COMPATIBLE_KINDS
        ):
            # Searching for a book by title reliably turns up reviews of it.
            rejections.append(f"{_candidate_label(candidate)}: type {candidate_kind} != {stored_kind}")
            continue

        if not candidate.authors and not candidate.years:
            # A title match and nothing else to check it against — see the
            # docstring above. Refused outright, rather than left to the two
            # guards below: both are written as "no comparable data, so
            # nothing to disagree about", which is the right read when a
            # *rich* candidate happens to omit one field, and the wrong read
            # when the candidate has no corroborating data whatsoever.
            rejections.append(
                f"{_candidate_label(candidate)}: title match only, "
                "no author or year on the candidate to corroborate it"
            )
            continue

        if ref.authors and candidate.authors:
            diff = compare_author_lists(ref.authors[:1], candidate.authors[:1])
            if diff.mismatches:
                rejections.append(f"{_candidate_label(candidate)}: first author disagrees")
                continue

        # ``candidate.years`` empty means the registry offered no year at all,
        # which is silence rather than disagreement, so the candidate survives.
        if (
            ref.year
            and candidate.years
            and not any(abs(ref.year - y) <= 1 for y in candidate.years.values())
        ):
            rejections.append(
                f"{_candidate_label(candidate)}: year {ref.year} not among "
                f"{sorted(candidate.years.values())}"
            )
            continue

        if best is None or score > best[0]:
            best = (score, candidate)

    if best is None:
        detail = "; ".join(rejections[:3]) if rejections else "no candidate above the title threshold"
        return None, detail
    return best[1], f"title {best[0]:.2f} with author and year corroboration"
