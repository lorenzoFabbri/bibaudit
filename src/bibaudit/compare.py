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
    STATUS_SOURCES,
    UNREACHABLE,
    Consultation,
    Issue,
    Record,
    Reference,
    Result,
    is_registry_artifact,
)
from .names import compare_author_lists
from .normalize import (
    clean,
    first_page,
    fold,
    normalize_doi,
    normalize_kind,
    normalize_pmid,
    similarity,
)

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
#:
#: ``pmid`` *is* here, and the difference between the two is the whole of
#: :func:`_check_pmid`: a PMID stored beside a DOI fetched nothing, so it is a
#: second claim about which work is cited rather than the key that produced the
#: record, and it can be wrong in a way the DOI structurally cannot.
CHECKED_FIELDS = (
    "title", "authors", "year", "container", "volume", "issue", "pages", "publisher",
    "pmid",
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

#: Everything a record can offer this module to compare an entry against.
#: Every check below returns in silence when the registry's value is empty —
#: correctly, since a registry omitting a field is not evidence about a
#: bibliography — so a record holding *none* of these produces no issues at
#: all, and ``verdict_for`` reports ``OK``: "every checked field agrees", over
#: zero checked fields.
#:
#: MEDLINE's whole-book records are how that was found. PMID 20301295 files its
#: title under ``BTI`` and its byline under ``FED``, neither of which was read,
#: so an entry with a fabricated title, a fabricated byline, a fabricated
#: container and a fabricated publisher was compared against nothing and passed
#: — the clean bill of health from a run that checked nobody that
#: ``status/not-asked`` exists to forbid, reached instead by a run that asked,
#: got an answer, and compared nothing. Reading those tags closed that
#: instance; this closes the shape, for the next registry that answers with a
#: record this module can make no use of.
#:
#: Identifier fields are deliberately absent. ``_check_doi`` and ``_check_pmid``
#: compare an entry's identifiers against each other, which says nothing about
#: whether the *work* is the one cited.
_COMPARABLE_FIELDS = (
    "title",
    "authors",
    "years",
    "container",
    "volume",
    "issue",
    "pages",
    "publisher",
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
        self,
        field: str,
        stored: object,
        registry: object,
        reason: str,
        *,
        source: str = "",
    ) -> None:
        """Record a difference attributable to a known registry defect.

        *source* is the registry the right-hand value came from, which is not
        always the primary one — :func:`_registry_value` lets the corroborator
        fill a field the primary left empty. "Registry files the journal
        without its leading article" printed beside the name of a registry
        that supplied no journal name names the wrong witness for a claim the
        reader is being invited to check.
        """
        self.suppressed.append(
            Issue(
                field=field,
                kind="registry-artifact",
                severity="info",
                stored=clean(stored),
                registry=clean(registry),
                source=source or self.primary.source,
                note=reason,
            )
        )


def _registry_source_record(ctx: _Context, attr: str) -> Record | None:
    """The record the comparison reads *attr* from, or ``None`` if neither has it.

    The primary registry wins; the corroborator fills gaps. Filling a gap is not
    arbitration — it only happens when the primary has nothing to say.

    A record rather than a registry name, because the *rest* of it decides
    things. :func:`benign.classify` judges a difference partly on the record
    the right-hand value came from — ``_container_leading_article`` reads
    MEDLINE's ``TA``, ``_pmid_pmc_accession`` its ``PMC`` line,
    ``_container_abbreviation`` Crossref's ``short-container-title`` — and
    every DOI-resolved reference has Crossref or DataCite as its primary with
    PubMed corroborating. So a Crossref deposit carrying no ``container-title``
    leaves PubMed's ``JT`` on the right-hand side while a Crossref record that
    holds neither ``JT`` nor ``TA`` is asked to explain it, and a correct entry
    citing *The Lancet* fails the build.
    """
    if getattr(ctx.primary, attr, None):
        return ctx.primary
    if ctx.corroborator and getattr(ctx.corroborator, attr, None):
        return ctx.corroborator
    return None


def _registry_value(ctx: _Context, attr: str) -> tuple[str, Record | None]:
    """Value for *attr* and the record it came from, by the precedence above."""
    holder = _registry_source_record(ctx, attr)
    if holder is None:
        return "", None
    return clean(getattr(holder, attr)), holder


def _alternate_containers(ctx: _Context) -> list[tuple[str, str]]:
    """Every *other* container title the registries carry, each with its source.

    Kept beside the primary value rather than folded into it because the
    principle is CLAUDE.md's own, one field over: *any year the registry itself
    carries is acceptable*, print or online-first. A container is the same
    shape of fact. Crossref deposits ``["Methods in Molecular Biology",
    "Methods in Biobanking"]`` for 10.1007/978-1-59745-423-0_7 — the series and
    the volume — and a chapter citing either is citing a container Crossref
    named. See :attr:`~bibaudit.model.Record.container_alternates`.

    Each value carries the registry that holds it because the two do not supply
    the same alternates: PubMed's is MEDLINE's ``TA``, and a report telling the
    reader that *Crossref* also carries ``Int J Cancer`` names the wrong witness
    for a claim it is inviting them to check.
    """
    records = [ctx.primary] + ([ctx.corroborator] if ctx.corroborator else [])
    return [
        (text, record.source)
        for record in records
        for value in record.container_alternates
        if (text := clean(value))
    ]


def _check_scalar(
    ctx: _Context,
    field: str,
    attr: str,
    stored: object,
    *,
    also_accepted: Sequence[tuple[str, str]] = (),
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

    *also_accepted* pairs further values a registry itself carries for the same
    field with the registry that carries them; matching one is step 2 by another
    route — not a tolerance. Only the container check passes any; see
    :func:`_alternate_containers`.

    *optional_for_kinds* names entry kinds for which a registry value the
    bibliography omits is not worth a ``missing`` warning at all. A book has
    no volume-in-a-journal or issue-in-a-volume to be missing; see the
    ``compare()`` call sites for the concrete false alarm this prevents, and
    :func:`_check_pages` for the identical guard on ``pages``, which is not a
    plain scalar field and cannot go through this function.
    """
    stored_text = clean(stored)
    registry_text, holder = _registry_value(ctx, attr)

    if holder is None or not registry_text:
        return
    source = holder.source

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

    for accepted, carrier in also_accepted:
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
            note=f"{carrier} also carries {accepted!r} for this work",
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

    reason = benign.classify(field, stored_text, registry_text, ctx.ref, holder)
    if reason:
        ctx.add_artifact(field, stored_text, registry_text, reason, source=source)
        return

    ctx.add(field, "mismatch", "error", stored_text, registry_text, source=source)


def _check_title(ctx: _Context) -> float | None:
    """Compare titles and return the similarity actually achieved.

    The best score against *any* consulted registry is used. PubMed and Crossref
    differ systematically in case and markup, and an entry that matches either
    of them is describing the right paper.
    """
    stored = clean(ctx.ref.title)
    candidates = [(clean(ctx.primary.title), ctx.primary)]
    if ctx.corroborator and ctx.corroborator.title:
        candidates.append((clean(ctx.corroborator.title), ctx.corroborator))
    candidates = [(text, rec) for text, rec in candidates if text]

    if not stored:
        if candidates:
            ctx.add(
                "title", "missing", "error", "",
                candidates[0][0], source=candidates[0][1].source,
            )
        return None
    if not candidates:
        return None

    best_text, best_record = max(candidates, key=lambda c: similarity(stored, c[0]))
    best_source = best_record.source
    score = similarity(stored, best_text)
    wrong_work, mismatch = ctx.thresholds.title_bands(normalize_kind(ctx.ref.kind))

    if fold(stored) == fold(best_text):
        if stored != best_text:
            # Same words, different glyphs: a curly apostrophe, an en-dash, a
            # capital. Worth showing, never worth failing a build over.
            ctx.add("title", "cosmetic", "info", stored, best_text, source=best_source)
        return score

    reason = benign.classify("title", stored, best_text, ctx.ref, best_record)
    if reason:
        ctx.add_artifact("title", stored, best_text, reason, source=best_source)
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

    "Full" reaches past the registry's last creator, which is where the
    documented failure mode actually puts the invented name: a byline compared
    only as far as the shorter list is a byline whose appended creator is
    compared against nothing. Those come back as ``authors/uncorroborated``,
    one line per creator, because a count line names nobody and the reader's
    question is *which* name no record carries.
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
            source=source,
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

    for position, name in diff.uncorroborated:
        ctx.add(
            "authors", "uncorroborated", "error", f"#{position} {name}", "",
            source=source,
            note=f"the registry's byline ends at #{diff.registry_count}",
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
        # The year on the right-hand side is the primary registry's preferred
        # one, so that is the record ``benign`` is shown, and it cannot explain
        # a year only the *corroborating* registry carries — MEDLINE's ``DP``
        # against Crossref's ``issued`` is a routine disagreement. Name the
        # slot from `accepted`, whose corroborator keys are already qualified
        # ("pubmed:issued"), rather than asserting the primary holds a date it
        # does not.
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


def _check_pmid(ctx: _Context) -> None:
    """Report a stored PMID that is not the one the stored DOI resolved to.

    A PMID stored *beside* a DOI fetched nothing: the record in hand came back
    under the DOI, so the two identifiers are two independent claims the entry
    makes about which work it cites. A registry answering for the DOI under a
    different PMID means they name different citations, which is the shape a
    mis-transcribed — or an invented — reference takes.

    **It is a warning, not an error, and the note says why.** One side of this
    comparison was looked up and the other was not: nothing here asks PubMed
    what the *stored* number names, so "these two identifiers name two works"
    is an inference from one lookup, not a finding from two. The case that
    decides it is a bibliography carrying a PMID that has since stopped
    answering — ``efetch`` for 20000157 or 35000082 returns an empty body
    today — beside the right DOI. Whether such a number was the work's own
    when the entry was written cannot be seen from here: what came back is the
    citation the DOI resolves to, and it says nothing about a number nobody
    put to PubMed. Failing a build on that is the false alarm this tool's
    third rule is about. An entry reaching this check prints
    under ``INCOMPLETE``, whose group heading reads "the registry holds fields
    the entry omits" and is a poor fit; the issue line beside it states
    exactly what disagrees with what, and ``--fail-on INCOMPLETE`` is there
    for a project that wants the finding to bite. Making it an error again
    means looking the stored PMID up first — see ``docs/registry-artifacts.md``.

    Three situations it is structurally unable to fire on, each a refusal
    rather than a suppression:

    *Nothing to compare against.* A registry nobody asked, one that answered
    and held nothing, and one whose answer was ambiguous all leave
    :attr:`~bibaudit.model.Record.pmid` unset, and an absent value is read as
    silence. The ambiguous case is PubMed answering for one DOI under more
    than one PMID: ``registries/pubmed.py`` withholds the value there rather
    than pick one, because an entry storing either of two PMIDs PubMed itself
    named is not wrong. "No PMID from PubMed" must never read as "PubMed says
    your PMID is wrong".

    *The PMID was the lookup key.* With no DOI stored the reference is
    resolved by its PMID (``audit._pmid_key``) and the record is here because
    that PMID resolved — the ``doi`` case exactly, so it gets the ``doi``
    treatment: not compared at all.

    *A PMID naming a retraction notice.* Nothing here reads ``updated-by``,
    ``update-to``, MEDLINE's ``RIN``/``ROF`` or any other relation, so there is
    no direction to get backwards and no way for this check to accuse somebody
    who cites a notice. An entry citing the Lancet's retraction of Wakefield et
    al. stores that notice's own pair — 10.1016/S0140-6736(10)60175-4 and PMID
    20137807 — which agree, and is silently correct. One storing the retracted
    paper's DOI, 10.1016/S0140-6736(97)11096-0, beside the notice's PMID is
    reported, and should be: those are two documents with two titles, twelve
    years apart, and a reader following one identifier lands somewhere the
    other does not.

    Comparison is on :func:`~bibaudit.normalize.normalize_pmid`, so a stored
    value that is not PMID-shaped at all — the ``PMCID: PMC5860629`` sitting
    one line below the PMID in a Zotero ``Extra`` block — is left alone rather
    than accused of disagreeing with a number it was never a candidate for.

    The one suppression is ``benign._pmid_pmc_accession``: NLM issues a PMID
    and a PMC accession for one deposited article and MEDLINE carries both on
    the same record, so a ``pmid`` field holding the PMC number names the very
    record it is being compared against. The record :func:`benign.classify`
    is handed is therefore the one the registry PMID came from, not
    ``ctx.primary`` — MEDLINE's ``PMC`` line is only on PubMed's record, and
    Crossref's would explain nothing.
    """
    stored = normalize_pmid(ctx.ref.pmid)
    if not stored or not ctx.ref.doi:
        return
    holder = _registry_source_record(ctx, "pmid")
    if holder is None:
        return
    registry = normalize_pmid(holder.pmid)
    if not registry or registry == stored:
        return
    reason = benign.classify("pmid", stored, registry, ctx.ref, holder)
    if reason:
        ctx.add_artifact("pmid", stored, registry, reason)
        return
    ctx.add(
        "pmid", "mismatch", "warning", stored, registry, source=holder.source,
        note=(
            "the stored DOI resolved to a different PubMed citation; what the "
            "stored PMID names was not itself looked up"
        ),
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
    registry, holder = _registry_value(ctx, "pages")
    if holder is None or not registry:
        return
    source = holder.source
    if not stored:
        if normalize_kind(ctx.ref.kind) == "book":
            return
        ctx.add("pages", "missing", "warning", "", registry, source=source)
        return
    # An inability to read a value is not evidence that two values match.
    # ``first_page`` has nothing to return for a locator carrying no
    # alphanumeric at all, and two of those agree only where they are the same
    # text — otherwise every such value agreed with every other one.
    opening = first_page(stored)
    if opening == first_page(registry) and (opening or stored == registry):
        return

    alt = clean(ctx.corroborator.pages) if ctx.corroborator else ""
    if alt and opening and opening == first_page(alt):
        ctx.add(
            "pages", "disputed", "info", stored,
            f"{ctx.primary.source}={registry!r} vs {ctx.corroborator.source}={alt!r}",  # type: ignore[union-attr]
            source="both", note="stored value matches the corroborating registry",
        )
        return

    reason = benign.classify("pages", stored, registry, ctx.ref, holder)
    if reason:
        ctx.add_artifact("pages", stored, registry, reason, source=source)
        return

    ctx.add("pages", "mismatch", "error", stored, registry, source=source)


def _registry_kinds(record: Record) -> list[str]:
    """Every type *record*'s registry files this work under, normalised, in order.

    A registry may file one work under several types at once and mean all of
    them, which is why this reads
    :attr:`~bibaudit.model.Record.kind_alternates` beside
    :attr:`~bibaudit.model.Record.kind`. Any type the registry itself carries
    is acceptable, on the same terms as any year and any container title it
    carries.

    ``"other"`` is dropped rather than returned. It is this vocabulary's word
    for "no opinion", so a record whose types are all unrecognised offers
    nothing to disagree with, and an empty list is what says so.
    """
    kinds: list[str] = []
    for value in (record.kind, *record.kind_alternates):
        kind = normalize_kind(value)
        if kind != "other" and kind not in kinds:
            kinds.append(kind)
    return kinds


def _kind_disagrees(stored_kind: str, registry_kinds: Sequence[str]) -> bool:
    """Whether *stored_kind* contradicts every type the registry carries.

    Shared by :func:`_check_kind` and :func:`confirm_without_id` for the same
    reason :data:`_COMPATIBLE_KINDS` is: the check that judges a *found*
    record and the one that screens a *candidate* must not drift into
    disagreeing about which pairs are the same work catalogued differently.
    """
    if stored_kind == "other" or not registry_kinds:
        return False
    return not any(
        stored_kind == kind or frozenset({stored_kind, kind}) in _COMPATIBLE_KINDS
        for kind in registry_kinds
    )


def _check_kind(ctx: _Context) -> None:
    """Flag an entry whose type is incompatible with the resolved work's.

    A ``@book`` whose DOI resolves to a journal article is nearly always citing a
    *review* of the book rather than the book. Type incompatibility rejected
    four of nine identifier proposals in earlier manual work, which is why it is
    checked rather than assumed.

    Every type the record carries is printed, because a reader deciding
    whether the entry or the registry is wrong needs to see what the registry
    actually said, and a record naming two of them said both.
    """
    stored_kind = normalize_kind(ctx.ref.kind)
    registry_kinds = _registry_kinds(ctx.primary)
    if not _kind_disagrees(stored_kind, registry_kinds):
        return
    ctx.add(
        "kind", "incompatible", "warning", stored_kind, ", ".join(registry_kinds),
        note="entry type disagrees with the resolved work's type",
    )


#: Sources this tool reads no retraction signal from, so neither their silence
#: nor their outage says anything about retraction. The test is what the client
#: in ``registries/`` does, not what the source's API could be made to answer:
#: only a client that sets :attr:`~bibaudit.model.Record.retracted` can carry
#: the signal, and one that does not would have carried nothing had it been
#: reachable.
#:
#: DataCite's schema has no retraction, withdrawal or concern element and
#: ``registries/datacite.py`` accordingly never sets the field; naming it in
#: the ``retraction-unverified`` note below would manufacture a doubt that a
#: reachable DataCite could not have resolved, on every dataset, preprint and
#: Zenodo deposit in the file. Open Library qualifies the same way: a book
#: catalogue with nothing in its data model to carry a notice, and ``audit.py``
#: does add it to *unreachable* when the ISBN leg times out.
#:
#: Europe PMC and OpenAlex qualify for the narrower reason, and it is the one
#: that matters here. Europe PMC's own API *does* carry retraction linkage in
#: ``commentCorrectionList``; ``registries/search.py`` reads neither that nor
#: anything like it for either source, and builds both records with
#: ``retracted`` left at its default. So a reachable Europe PMC would have
#: resolved nothing either, and an entry with no identifier — whose whole
#: candidate pool comes from these two plus Crossref — carried the doubt on
#: all three names. That this is a limit of the client rather than of the
#: source is a fact for ``docs/limits.md``; what it is not is evidence about a
#: particular work.
#:
#: Written as an exclusion rather than as the list of sources that *do* carry
#: the signal, because the two fail in opposite directions: a registry missing
#: from an inclusion list would be silently dropped from the notice, and
#: understating ignorance about retraction is the exact failure this check
#: exists to prevent. Whoever adds the next registry gets counted by default
#: and has to come here to opt out — which Europe PMC and OpenAlex did not, so
#: ``tests/test_compare.py`` now derives the partition from the registry
#: clients themselves and fails when a new one skips this comment.
#: ``"retraction-watch"`` is deliberately *not* here: it is the one source in
#: this set that exists only to carry the signal.
_NO_RETRACTION_SIGNAL = frozenset({"datacite", "openlibrary", "europepmc", "openalex"})

#: The mirror image, and the sharper of the two. These sources hold no
#: bibliographic record and can never resolve an identifier, so their being
#: unreachable is not ignorance about whether a work *exists* — Retraction
#: Watch is consulted for post-publication status alone. They share the
#: run-wide *unreachable* set with registries that can answer that question,
#: and the "nothing answered" branch below must exclude them: otherwise one
#: stale side-channel turns every fabricated DOI in a file into ``UNCHECKED``
#: and the exit code to 0, while ``consulted`` reports that Crossref, DataCite
#: and PubMed all answered.
#:
#: Open Library is deliberately absent: it *can* resolve an ISBN, so its outage
#: is genuine ignorance about a book's existence.
_NO_RESOLUTION_SIGNAL = frozenset({"retraction-watch"})

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
#: Anything not listed here or in :data:`_CORRECTION_KINDS`,
#: ``retraction_kind=None`` included, counts as a retraction: a registry this
#: module has never heard of cannot talk it out of the finding.
_CONCERN_KINDS = frozenset({"expression of concern"})

#: The other kind that is not a retraction, and the milder of the two: the work
#: stands, unamended in nothing but the part the notice corrects.
#:
#: ``retractions._RW_KIND_MAP`` mints this value from Retraction Watch's
#: ``RetractionNature`` column, so it is a kind this project produces rather
#: than one an unfamiliar registry might send — the "cannot talk it out of the
#: finding" default above is for the second, and reading a correction as a
#: retraction was that default catching one of the first. Retraction Watch
#: logs a 2004 ``Correction`` for 10.3390/nano14090769 (*Nanomaterials*), and
#: an entry citing it printed ``RETRACTED — the cited work has itself been
#: retracted`` with the word ``correction`` in the registry column and exited
#: 1: a false statement about a named work, made with the tool's full
#: authority, which is the output :data:`_CONCERN_KINDS` was introduced to
#: prevent and which arrived a second time by this route.
#:
#: Same exact-fold membership rule, for the same reason: a notice titled
#: "Retraction and correction" is a retraction.
_CORRECTION_KINDS = frozenset({"correction"})

#: What a concern and a correction say instead of "the work stands" when a
#: retraction is recorded for the same work by another source.
#:
#: Both notes otherwise close on the work standing and the citation being
#: legitimate, which beside ``status/retracted`` tells a reader in one report
#: that the work has been withdrawn and that it stands. Neither finding is
#: dropped for it — they are separate statements, all of them true at once, and
#: a source recording the milder one has not contradicted the retraction — but
#: the clause that reads as a second opinion against the finding goes.
#:
#: The case that reaches this is two sources disagreeing, since one source
#: recording two kinds already resolves to the stronger before it gets here
#: (``retractions._KIND_PRIORITY``, ``audit._with_pubmed_concern``). Crossref's
#: ``updated-by`` linkage and Retraction Watch's own export disagree on real
#: DOIs: of the 650 whose strongest Retraction Watch row is a correction, 19
#: carry a Crossref ``updated-by`` retraction — every one of them asked, not a
#: sample, live on 2026-08-09, with one more DOI Crossref does not hold at all.
#: 10.1111/anec.12955 is one of the 19 — Retraction Watch logs a 2023
#: correction, Crossref links the retraction — and 10.1080/09513590600604673
#: is the concern's counterpart.
_BESIDE_A_RETRACTION = "It does not undo the retraction recorded for this work"


def _detail(kinds: Mapping[str, str], names: Sequence[str]) -> str:
    """The registry column for one status issue, qualified only when it must be.

    One asserting registry: the source column already names it, and
    "pubmed  pubmed=Retracted Publication" is the kind of doubled label that
    makes a report look machine-generated rather than read.
    """
    if len(names) == 1:
        return kinds[names[0]]
    return "; ".join(f"{name}={kinds[name]}" for name in names)


#: Identifiers **this tool** looks each retraction source up by. A source
#: missing from the map is looked up by a DOI, which is the common case and the
#: reason the map is written as the exception: Crossref's ``updated-by`` is on a
#: DOI record and this module reads Retraction Watch's export through its DOI
#: column, while PubMed answers to a PMID as readily.
#:
#: It is the lookup key and not a property of the source, and the difference
#: shows on Retraction Watch: 33,403 of the 71,641 rows in the 2026-08-09
#: export carry an ``OriginalPaperPubMedID``, and 715 retraction rows carry one
#: with no DOI beside it, so those works are reachable by a PMID and by nothing
#: this tool asks with. Written as the source's own limit, the note told a
#: reader no rerun could ever close a gap that a second index would.
#:
#: Read only to say *why* a source went unasked, never to decide whether it
#: did. The two reasons need different words because the reader's next move
#: differs: a source with no key for this reference will not be asked by any
#: rerun of this tool, and one that had a key and was left out was left out by
#: a flag the reader chose. "Were never asked" covered both and told them apart
#: for neither.
_STATUS_SOURCE_KEYS: dict[str, tuple[str, ...]] = {"pubmed": ("doi", "pmid")}


def _lookup_keys(name: str) -> tuple[str, ...]:
    return _STATUS_SOURCE_KEYS.get(name, ("doi",))


def _why_unasked(unasked: Sequence[str], ref: Reference) -> str:
    """The *unasked* sources, grouped by whether this reference had a key.

    Every clause names its sources, so the sentence stays checkable against the
    ``consulted`` map beside it however the groups fall out — and the keyless
    ones are grouped by the keys *they* take rather than pooled. Pooled, a book
    carrying neither identifier read ``crossref, pubmed, retraction-watch take a
    DOI or a PMID this reference does not carry``, which is true of PubMed and
    loose about the other two: neither is ever asked with a PMID, so a reader
    adding one to that entry would find two of the three still unasked.
    """
    keyless: dict[tuple[str, ...], list[str]] = {}
    skipped = []
    for name in unasked:
        keys = _lookup_keys(name)
        if any(getattr(ref, attr, None) for attr in keys):
            skipped.append(name)
        else:
            keyless.setdefault(keys, []).append(name)

    # Insertion order, and *unasked* arrives in registry order, so two runs over
    # the same evidence produce the same sentence.
    clauses = [
        f"{', '.join(names)} {'take' if len(names) > 1 else 'takes'} "
        f"{' or '.join(f'a {key.upper()}' for key in keys)} "
        "this reference does not carry"
        for keys, names in keyless.items()
    ]
    if skipped:
        clauses.append(
            f"{', '.join(skipped)} {'were' if len(skipped) > 1 else 'was'} "
            "not queried on this run"
        )
    return "; ".join(clauses)


def _status_issues(
    records: Mapping[str, Record],
    consulted: Mapping[str, Consultation],
    ref: Reference,
    *,
    asked_stated: bool,
    gaps: bool = True,
) -> tuple[list[Issue], bool]:
    """Every ``status`` finding for one work, and whether it is *retracted*.

    Five statements can be true at once about the same work and none of them is
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
        A registry records a concern. Reported with the same weight — an author
        who cites a paper under an expression of concern needs to know before
        submission — but never under the word "retracted". See
        :data:`_CONCERN_KINDS` for the paper this was found on. It stays an
        error, so an entry that failed before this distinction existed still
        fails: the finding is re-labelled, never relaxed. Where another source
        records a retraction as well, the note says what the concern does not
        do rather than that the work stands — see :data:`_BESIDE_A_RETRACTION`.

    ``status/correction`` (info)
        A registry records a correction. The work stands and citing it is
        correct; what a reader may want is the corrected version's numbers
        rather than the original's — or, beside a retraction, that the
        correction does not undo it. See :data:`_CORRECTION_KINDS` for the
        notice that forced the distinction.

        ``info``, and so the verdict does not move — a corrected paper is
        perfectly citable, and failing a build over one is the false alarm
        this project's third rule is about. It is stated rather than dropped
        because Retraction Watch supplied it and this module's job is to say
        what the sources said: the same treatment ``year/alternate-date``
        gets, and it reaches the reader through the JSON report and
        ``--verbose``.

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

    ``status/not-asked`` (info)
        No registry that answered records a retraction, and a source that
        could have recorded one was never queried. A reference resolved by its
        PMID is the case this exists for: Retraction Watch's export and
        Crossref's ``updated-by`` linkage are both keyed on a DOI it does not
        have, so two of the four sources go unconsulted and PubMed's own two —
        MEDLINE's ``PT`` flag and its ``ECI`` cross-reference, neither of which
        needs a key to ask about (see ``audit._with_pubmed_concern``) — are the
        whole of the evidence. That entry rendered as ``verdict: OK, issues:
        []`` — a clean bill of health issued by a run that asked almost nobody,
        which is the one output this rule forbids. ``--no-retraction-check``
        and a book resolved by its ISBN reach it the same way.

        Kept apart from ``retraction-unverified`` rather than folded into it
        for the reason :data:`~bibaudit.model.Consultation` keeps three states
        and not two: "could not be reached" is ignorance that a rerun may
        settle, "was not asked" is not, and the reader's next move differs.
        The note goes further and says which of *two* reasons applied — see
        :func:`_why_unasked` — because "not asked" covers a source that takes
        an identifier this reference does not carry, which no rerun changes,
        and one that had a key and was left out by a flag, which dropping the
        flag fixes. Also ``info``, for the same reason as its neighbour:
        coverage a reference's own identifier denies it is not a defect in
        anybody's bibliography.

        Raised only when *asked_stated*, i.e. when :func:`compare`'s caller
        named the registries it queried. Without that, ``not-asked`` in
        *consulted* is :func:`_consultations`' documented under-statement — the
        caller did not say — and asserting from it that nobody asked would be a
        finding printed on evidence nobody produced. Exactly the line
        :func:`compare`'s ``identifier/not-asked`` branch already draws between
        an empty *asked* and ``None``. The map still shows ``not-asked``
        either way, so nothing that was visible stops being visible.

    *gaps* is False where the entry's identifier resolved in no registry. The
    four findings above are still made — a notice Retraction Watch answered
    with is evidence about the work whether or not any registry could name it,
    and it is the finding this tool exists for — but the last two are not:
    nothing about that entry was corroborated, its verdict already says so, and
    "retraction status not corroborated" beneath a failing identifier is a
    caveat about a check that never ran.

    Direction — whether a record *is* a retraction notice or *was* retracted —
    is decided upstream in the registry clients and read here as a plain flag.
    ``retraction_kind`` is inspected only to separate a concern and a
    correction from a retraction, by exact equality on two closed sets that
    contain no retraction-shaped string; nothing here can turn a retracted
    paper into a clean one, and no title, type or notice wording is sniffed at
    all.
    """
    asserting = _in_registry_order(
        name for name, record in records.items() if record.retracted
    )
    kinds = {name: records[name].retraction_kind or "retracted" for name in asserting}
    concerned = [name for name in asserting if fold(kinds[name]) in _CONCERN_KINDS]
    corrected = [name for name in asserting if fold(kinds[name]) in _CORRECTION_KINDS]
    milder = set(concerned) | set(corrected)
    retracting = [name for name in asserting if name not in milder]

    issues: list[Issue] = []

    if retracting:
        # Only a registry that answered, could have recorded a retraction, and
        # recorded *nothing* dissents. One that recorded a concern or a
        # correction has not contradicted the retraction, and listing it as
        # carrying "no retraction linkage" would read as a second opinion
        # against the finding when it is corroboration of a weaker one. One
        # this tool reads no retraction signal from has not contradicted it
        # either, and there the sentence is false outright: a DataCite or
        # Europe PMC record carries no retraction linkage for any work
        # whatsoever, so "and not by datacite, which answered for this work and
        # carries no retraction linkage" invents a dissent — on the finding
        # where weakening one is worst.
        silent = _in_registry_order(
            name for name in set(records) - set(asserting)
            if name not in _NO_RETRACTION_SIGNAL
        )
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
                    f"work; recorded by {', '.join(concerned)}. "
                    + (
                        _BESIDE_A_RETRACTION
                        if retracting
                        else "That is a stated doubt, not a retraction: the work "
                        "stands, and citing it is legitimate once the notice has "
                        "been read"
                    )
                ),
            )
        )

    if corrected:
        issues.append(
            Issue(
                field="status",
                kind="correction",
                severity="info",
                stored="",
                registry=_detail(kinds, corrected),
                source=",".join(corrected),
                note=(
                    "a correction has been published for the cited work; "
                    f"recorded by {', '.join(corrected)}. "
                    + (
                        _BESIDE_A_RETRACTION
                        if retracting
                        else "The work stands and citing it is correct; the "
                        "corrected version is the one to read the numbers off"
                    )
                ),
            )
        )

    if not retracting and gaps:
        # Both halves are read off the one consultation map rather than off two
        # sets that could disagree with it: it is what the report prints beside
        # the verdict, so a gap stated here and a gap shown there cannot differ.
        blind = _in_registry_order(
            name
            for name, state in consulted.items()
            if state == UNREACHABLE and name not in _NO_RETRACTION_SIGNAL
        )
        unasked = (
            _in_registry_order(
                name
                for name, state in consulted.items()
                if state == NOT_ASKED and name not in _NO_RETRACTION_SIGNAL
            )
            if asked_stated
            else []
        )
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
        if unasked:
            issues.append(
                Issue(
                    field="status",
                    kind="not-asked",
                    severity="info",
                    stored="",
                    registry="",
                    source=",".join(unasked),
                    note=(
                        f"retraction status not corroborated: {_why_unasked(unasked, ref)}"
                        ", and no registry that did answer records a retraction "
                        "— which is not the same as there being none"
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

    :data:`~bibaudit.model.REGISTRIES` and
    :data:`~bibaudit.model.STATUS_SOURCES` are stated on every reference, even
    where nothing reached them; any other name appears only once something did.
    The two rosters are the sources whose silence a reader would otherwise have
    to infer from a missing key, and a retraction source in particular has to
    be nameable as ``not-asked``: a reference resolved by its PMID asks none of
    them, and a key that is simply not there reads as nothing to report.

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
    always = (*REGISTRIES, *STATUS_SOURCES)
    names = [*always, *sorted(participating - set(always))]

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
    # Ranked below everything above it on purpose: anything actually found is a
    # better description of the entry than "nothing was comparable", and this
    # only ever displaces a verdict reached over an empty comparison. See
    # :data:`_COMPARABLE_FIELDS`.
    if any(i.kind == "uncompared" for i in issues):
        return "UNCHECKED"
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


def _inconclusive_note(inconclusive: Mapping[str, Sequence[str]]) -> str:
    """Say what each registry answered with instead, for an ``UNCHECKED`` line.

    The identifiers are named because a reader cannot otherwise tell this
    apart from an outage: "pubmed answered with 20137807 instead" is something
    they can look up, and looking it up is how a merged or mis-filed citation
    gets found. The wording is here rather than at the call site in ``audit``
    so that every note this module prints is written in this module.
    """
    parts = []
    for name in sorted(inconclusive):
        others = [text for value in inconclusive[name] if (text := clean(value))]
        parts.append(
            f"{name} answered with {', '.join(others)} instead"
            if others
            else f"{name} did not answer for it"
        )
    return f"{'; '.join(parts)}; an answer about another record is not one about this identifier"


def compare(
    ref: Reference,
    records: dict[str, Record],
    *,
    thresholds: Thresholds | None = None,
    unreachable: set[str] | None = None,
    asked: Collection[str] | None = None,
    inconclusive: Mapping[str, Sequence[str]] | None = None,
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
    inconclusive:
        Registries that answered on this reference's behalf without settling
        whether they hold its identifier, each mapped to the identifiers they
        answered with *instead* (empty when the request came back with
        nothing at all). Read only when no record resolved, where it is the
        difference between "nothing came back under this identifier" and "the
        registry answered with something else": the first is the evidence
        ``BAD-ID`` rests on, the second is ignorance and reports
        ``UNCHECKED``. ``registries/pubmed.py`` populates it — see
        :class:`~bibaudit.registries.pubmed.PmidAnswers`.

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

    # A source that carries post-publication status and no bibliographic record
    # cannot be the record an entry is compared against: it holds no title, no
    # byline and no year, and reading it as one would give a DOI nothing can
    # resolve a fieldless stub for a primary and take its ``BAD-ID`` away. That
    # is the "a notice never promotes a DOI to resolved" rule, enforced where
    # the choice is made rather than by refusing to record the notice at all.
    resolving = {
        name: record for name, record in records.items()
        if name not in _NO_RESOLUTION_SIGNAL
    }
    primary = resolving.get("crossref") or resolving.get("datacite")
    corroborator = resolving.get("pubmed")
    if primary is None and corroborator is not None:
        primary, corroborator = corroborator, None
    if primary is None and resolving:
        # A record from a registry this function does not name. Selecting by
        # explicit key alone meant such a record was ignored *and* the entry
        # then fell through to "resolves in no consulted registry" — a BAD-ID
        # reported on a DOI that a registry had, in the same run, resolved.
        # Whoever adds the next registry to REGISTRIES gets a sane default
        # instead of a fabrication warning.
        primary = resolving[_in_registry_order(resolving)[0]]

    if primary is None:
        # Whatever a status source did answer with is still reported. Retraction
        # Watch answers about DOIs no bibliographic registry carries — 3 of a
        # random 400 of its retraction DOIs resolve in none of Crossref,
        # DataCite and PubMed — and dropping its answer because of that renders
        # a logged retraction as an identifier problem and nothing else. The
        # entry keeps the verdict the branches below reach: an identifier that
        # resolved nowhere is what a reader has to act on first, and calling
        # the entry ``RETRACTED`` would assert that the work Retraction Watch
        # logged is the work this reference cites, which is exactly what no
        # registry could confirm. Both statements are on the report; only one
        # of them is provable from the evidence in hand.
        status, _ = _status_issues(
            records, result.consulted, ref, asked_stated=asked is not None, gaps=False
        )
        result.issues.extend(status)
        # Only registries that could have *held* the work count here. A
        # retraction side-channel going down says nothing about whether the
        # work exists, and letting it answer that question turns a fabricated
        # DOI into a network problem.
        blind_to_existence = set(unreachable) - _NO_RESOLUTION_SIGNAL
        if blind_to_existence and not resolving:
            # Nothing that could hold the work answered. Silence from an
            # unreachable registry is not evidence of anything.
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
        if ref.identifier and asked is not None and not asked:
            # ``BAD-ID`` requires a registry that answered. With nothing
            # consulted, "resolves in no consulted registry" is vacuously true
            # and reads as an accusation the run has no evidence for: it would
            # turn the caller's decision not to look into a failing verdict on
            # a work that may well exist. ``--no-isbn`` is the caller that does
            # this, on books whose ISBN is perfectly valid.
            #
            # An empty *asked* asserts that nothing was consulted; ``None``
            # means the caller did not say, which every direct ``compare`` call
            # relies on and which must keep its BAD-ID.
            result.verdict = "UNCHECKED"
            result.issues.append(
                Issue(
                    field="doi" if ref.doi else "identifier",
                    kind="not-asked",
                    severity="info",
                    stored=ref.identifier,
                    note="no registry was asked about this identifier; not checked",
                )
            )
            return result
        if ref.identifier and inconclusive:
            # A registry that answered *around* the identifier has settled
            # nothing about it. PubMed's ``efetch`` returning a citation under
            # a number nobody asked for is the case this exists for: declining
            # to adopt that record is right, but what came back is a record,
            # not the empty response an absent number produces, and only the
            # second is evidence a stored PMID is wrong. Collapsing them makes
            # a verdict of ``BAD-ID`` out of an answer.
            result.verdict = "UNCHECKED"
            result.issues.append(
                Issue(
                    field="doi" if ref.doi else "identifier",
                    kind="inconclusive",
                    severity="info",
                    stored=ref.identifier,
                    source=",".join(sorted(inconclusive)),
                    note=_inconclusive_note(inconclusive),
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
    _check_pmid(ctx)

    # Every record that answered, not just the primary — and, through the
    # consultation map, every source that could not answer and every source
    # nobody asked, because ignorance about retraction is not the same fact as
    # an absence of one. See _status_issues.
    status, retracted = _status_issues(
        records, result.consulted, ref, asked_stated=asked is not None
    )
    ctx.issues.extend(status)

    if not any(
        getattr(record, attr)
        for record in (primary, corroborator)
        if record is not None
        for attr in _COMPARABLE_FIELDS
    ):
        ctx.add(
            "doi" if ref.doi else "identifier",
            "uncompared",
            "info",
            ref.identifier or "",
            note=(
                "the identifier resolved, and the record it resolved to holds "
                "no field this entry could be compared against"
            ),
        )

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

        candidate_kinds = _registry_kinds(candidate)
        if _kind_disagrees(stored_kind, candidate_kinds):
            # Searching for a book by title reliably turns up reviews of it.
            rejections.append(
                f"{_candidate_label(candidate)}: type {', '.join(candidate_kinds)} "
                f"!= {stored_kind}"
            )
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
