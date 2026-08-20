"""Core data contracts.

Three types cross every module boundary:

    Reference  what an input adapter produces — one citation as it is *stored*
    Record     what a registry client produces — the same work as *published*
    Issue      one field-level disagreement between the two

Adapters never talk to registries; registries never see a Reference. Everything
they have in common lives here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .normalize import normalize_pmid

Severity = Literal["error", "warning", "info"]

#: Every registry this tool can consult, in the order it consults them and in
#: the order any message that names more than one lists them. Defined here
#: rather than in :mod:`~bibaudit.compare` because :class:`Result` promises a
#: verdict rests on a stated set of registries, and that promise is only
#: checkable against one canonical roster.
REGISTRIES = ("crossref", "datacite", "pubmed")

#: Sources consulted for post-publication status alone. They hold no
#: bibliographic record and can never resolve an identifier, so they are not
#: registries in :data:`REGISTRIES`' sense — but :attr:`Result.consulted` states
#: one for every reference, asked or not, and that is what this roster is for.
#: A source named only where something happened to reach it leaves the reader of
#: two entries side by side with a key present on one and absent on the other,
#: and absence is the one thing ignorance about retraction may never look like.
#: A reference resolved by its PMID asks none of these, so on that path the
#: whole of the difference from a DOI-resolved entry would be a missing key.
STATUS_SOURCES = ("retraction-watch",)

#: What one registry contributed to one reference's verdict. Three states,
#: because three things actually happen and only two of them used to be
#: representable:
#:
#: ``answered``
#:     The registry was queried and replied. It may have held the work or not:
#:     an authoritative "I do not have this" is evidence, and it is what makes
#:     ``BAD-ID`` a fact rather than a guess.
#: ``unreachable``
#:     The registry was queried and could not reply — a timeout, a run of 5xx.
#:     Ignorance, never absence. See ``Transient``.
#: ``not-asked``
#:     The registry was never queried at all: ``--no-corroborate`` skips PubMed
#:     entirely, DataCite is only asked about DOIs Crossref did not answer for,
#:     and nothing keyed on a DOI is asked about a reference resolved by its
#:     PMID or its ISBN. Where the unasked source carries a retraction signal,
#:     ``compare`` states the gap as an issue as well — see
#:     :data:`STATUS_SOURCES`.
#:
#: The bool this replaced was computed as "not known to be unreachable", so a
#: run with ``--no-corroborate`` reported ``"pubmed": true`` on every single
#: reference — the JSON report, which exists to record what evidence a verdict
#: rests on, claimed a curated second opinion that was never sought.
Consultation = Literal["answered", "unreachable", "not-asked"]

ANSWERED: Consultation = "answered"
UNREACHABLE: Consultation = "unreachable"
NOT_ASKED: Consultation = "not-asked"

#: Verdicts, ordered from most to least severe. Whether a verdict fails a build
#: is decided by :data:`FAILING_VERDICTS`, not by the order here.
#:
#: This is also the order :mod:`~bibaudit.report` prints groups in, worst first,
#: so the last thing on a reader's screen is the part that needs no action.
#: ``report`` imports this tuple rather than keeping a second copy: it did keep
#: one, and the two drifted — ``INCOMPLETE`` and ``REGISTRY-ARTIFACT`` had ended
#: up in opposite relative positions, so the module that claims to define
#: severity and the module that shows it to a human disagreed. ``INCOMPLETE``
#: outranks ``REGISTRY-ARTIFACT`` because a gap in the entry is something the
#: reader can fix, whereas a known registry defect is explicitly nothing to do.
#:
#: ``ADJUDICATED`` sits between them. It and ``REGISTRY-ARTIFACT`` were one
#: verdict, which made two incompatible claims indistinguishable in a report:
#: "the registry is known to be wrong here, reproducibly, and it is documented
#: in docs/registry-artifacts.md" versus "somebody on this project wrote a rule
#: in .bibaudit.toml saying not to care". The first needs no reader; the second
#: rests on a person's say-so, can go stale when a citekey is renamed or a
#: bibliography is re-exported, and is the one worth re-reading — so it prints
#: first of the two.
VERDICTS = (
    "RETRACTED",
    "BAD-ID",
    "WRONG-WORK",
    "FIELD-MISMATCH",
    "UNCONFIRMED",
    "DISPUTED",
    "INCOMPLETE",
    "ADJUDICATED",
    "REGISTRY-ARTIFACT",
    "TITLE-DRIFT",
    "COSMETIC",
    "UNCHECKED",
    "OK",
)

#: A citation to a retracted paper fails even when every field is correct.
#: ``UNCONFIRMED`` fails because a reference nothing can confirm is exactly the
#: shape a fabricated one takes — but the report says "needs review", never
#: "fake". ``UNCHECKED`` (registry unreachable) never fails: a network outage is
#: not a defect in the bibliography.
FAILING_VERDICTS = frozenset(
    {"RETRACTED", "BAD-ID", "WRONG-WORK", "FIELD-MISMATCH", "UNCONFIRMED"}
)


@dataclass(frozen=True, slots=True)
class Name:
    """One creator.

    ``literal`` carries collective authors — "The Endogenous Hormones and Breast
    Cancer Collaborative Group" is one author, not two, and splitting it on
    " and " is the single most common way to invent an author-list mismatch.
    """

    family: str = ""
    given: str = ""
    literal: str = ""
    collective: bool = False
    #: BibTeX's ``and others`` / CSL's et-al marker. Truncates the list rather
    #: than naming a person, so a length comparison past this point is void.
    et_al: bool = False
    #: The source put the whole creator in one slot, so where the family name
    #: ends is not something it states. ``family`` carries the slot verbatim,
    #: which is the only value a report may show, and
    #: :func:`~bibaudit.names.names_agree` accepts the readings that split a
    #: forename off it rather than choosing one of them.
    unsplit: bool = False

    def __str__(self) -> str:
        if self.literal:
            return self.literal
        return f"{self.family}, {self.given}".strip(", ")


@dataclass(slots=True)
class Reference:
    """A citation exactly as stored, before any normalisation."""

    key: str
    #: Where a human goes to fix it: "references.bib:412", "sources/epic.qmd:88".
    locator: str
    #: Normalised across adapters: article, chapter, book, preprint, thesis,
    #: report, dataset, webpage, other.
    kind: str = "other"

    doi: str | None = None
    pmid: str | None = None
    isbn: str | None = None
    url: str | None = None

    title: str | None = None
    authors: list[Name] = field(default_factory=list)
    year: int | None = None
    container: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    publisher: str | None = None

    #: Untouched adapter output, for the report and for --suggest.
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def identifier(self) -> str | None:
        """The strongest identifier present, for cache keys and dedup.

        Only these three, because these are the three ``audit`` builds a
        request from: Crossref and DataCite take a DOI, PubMed a DOI or a PMID,
        Open Library an ISBN. Ranking a fourth would not widen coverage — it
        would narrow it, because :mod:`~bibaudit.compare` reads this property
        to decide an entry has an identifier at all and reports a truthy one
        that resolved nowhere as ``BAD-ID``, "resolves in no consulted
        registry". An identifier nothing looks up resolves nowhere by
        construction, so every entry carrying one would be accused about a
        value no registry was ever asked.

        The stored ``pmid`` counts only when :func:`~bibaudit.normalize.
        normalize_pmid` accepts it, for that same reason one step further in: a
        ``PMC5860629`` copied into the ``pmid`` field from the line under it in
        a Zotero ``Extra`` was reported as resolving "in no consulted registry"
        — an authoritative absence about a number no registry was asked for.
        Refused here, the entry falls through to the identifier-less path and
        is checked the way any other entry without one is.
        """
        for value in (self.doi, normalize_pmid(self.pmid), self.isbn):
            if value:
                return value
        return None


@dataclass(slots=True)
class Record:
    """A work as some registry describes it.

    ``years`` is a mapping rather than a scalar because the distinction matters:
    an entry citing the print year when Crossref also carries an earlier
    online-first year is correct, and flagging it is a false alarm.
    """

    source: str
    doi: str | None = None
    #: The PMID of the MEDLINE citation this record was built from, where the
    #: registry has one. Only PubMed sets it, and only when it is unambiguous —
    #: see ``registries/pubmed.py``. It exists so that a reference storing a
    #: PMID *beside* a DOI can be checked: the DOI fetched the record, so the
    #: PMID is a second, independent claim about which work is cited, and a
    #: pair naming two different records is what a mis-transcribed citation
    #: looks like. ``compare._check_pmid`` is the only check that reads it.
    pmid: str | None = None
    title: str | None = None
    authors: list[Name] = field(default_factory=list)
    years: dict[str, int] = field(default_factory=dict)
    container: str | None = None
    container_short: str | None = None
    #: Further container titles the registry carries for this same work, in the
    #: registry's own order after :attr:`container`. A book chapter has two —
    #: the series and the volume — and an entry citing either is right, exactly
    #: as an entry citing either the print or the online-first year is right.
    #:
    #: Crossref's record for 10.1007/978-1-59745-423-0_7 (Hainaut et al.,
    #: stored as ``hainaut2011biobank``) deposits ``"container-title":
    #: ["Methods in Molecular Biology", "Methods in Biobanking"]``. The client
    #: kept element 0 only, so the ``@inbook``'s ``booktitle`` — Crossref's
    #: *second* element, character for character — had no registry value left to
    #: match against, and the corpus's one ``DISPUTED`` verdict was Crossref
    #: reported as disagreeing with PubMed about a title Crossref itself supplies.
    container_alternates: list[str] = field(default_factory=list)
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    publisher: str | None = None
    #: The registry's own type string, e.g. "journal-article", "book-chapter".
    kind: str | None = None
    #: Further type strings the registry files this same work under, in the
    #: registry's own order after :attr:`kind`. A registry may mean all of
    #: them at once: MEDLINE's ``PT`` list for PMID 42557261 is ``Dataset``
    #: then ``Journal Article``, and a data descriptor in *Scientific Data*
    #: really is both. An entry matching any of them is right, exactly as an
    #: entry citing either the print or the online-first year is right.
    kind_alternates: list[str] = field(default_factory=list)
    retracted: bool = False
    retraction_kind: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def year(self) -> int | None:
        """Print year if known, else whatever else the registry gave."""
        for slot in ("print", "issued", "online"):
            if slot in self.years:
                return self.years[slot]
        return next(iter(self.years.values()), None)


@dataclass(slots=True)
class Issue:
    """One field-level disagreement.

    ``stored`` and ``registry`` are display strings, already cleaned but never
    normalised — a report that shows the comparison key instead of the actual
    value is unreadable and hides exactly the sort of defect (a stray glyph, a
    mojibake surname) that matters.
    """

    field: str
    kind: str
    severity: Severity
    stored: str = ""
    registry: str = ""
    source: str = ""
    note: str = ""


#: The ``kind`` :mod:`~bibaudit.compare` stamps on a difference explained by
#: :mod:`~bibaudit.benign` — a registry defect that is true for everybody.
ARTIFACT_KIND = "registry-artifact"

#: The prefix :mod:`~bibaudit.suppress` stamps on an issue a project-local
#: ``.bibaudit.toml`` adjudicated away (``mismatch`` becomes
#: ``suppressed:mismatch``). Recorded here so the two producers of
#: ``Result.suppressed`` can be told apart by their output rather than by the
#: caller remembering which one it used.
SUPPRESSED_PREFIX = "suppressed:"


def is_registry_artifact(issue: Issue) -> bool:
    """True when *issue* was suppressed because the **registry** is wrong.

    Only meaningful for an issue that lives in :attr:`Result.suppressed`; that
    list has exactly two producers and this distinguishes them. Anything else
    in it got there because a human wrote a rule in ``.bibaudit.toml``, which
    is a different claim entirely — see :data:`VERDICTS`.

    The test is an equality, not a substring or a prefix: ``suppress`` rewrites
    a suppressed issue's kind to ``suppressed:<original>``, which can never
    equal :data:`ARTIFACT_KIND`, and it only ever reads ``Result.issues`` —
    artifacts are placed straight into ``Result.suppressed`` and are never
    offered to it, so the two namespaces cannot collide.
    """
    return issue.kind == ARTIFACT_KIND


@dataclass(slots=True)
class Result:
    """Everything known about one reference after checking."""

    ref: Reference
    verdict: str = "UNCHECKED"
    issues: list[Issue] = field(default_factory=list)
    #: What each registry contributed, e.g.
    #: ``{"crossref": "answered", "datacite": "not-asked", "pubmed": "unreachable"}``.
    #: See :data:`Consultation`: "not asked" and "asked and could not answer"
    #: are different facts about the evidence, and a bool could hold only one
    #: of them.
    consulted: dict[str, Consultation] = field(default_factory=dict)
    title_similarity: float | None = None
    suppressed: list[Issue] = field(default_factory=list)

    @property
    def fails(self) -> bool:
        return self.verdict in FAILING_VERDICTS

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    def answered(self, registry: str) -> bool:
        """True only if *registry* was queried **and** replied.

        Deliberately not "was not unreachable": a registry nobody asked has
        said nothing, and treating its silence as an answer is how a run with
        ``--no-corroborate`` came to claim PubMed corroboration on every
        reference in the file.
        """
        return self.consulted.get(registry) == ANSWERED

    @property
    def artifacts(self) -> list[Issue]:
        """Suppressed differences explained by a documented registry defect."""
        return [i for i in self.suppressed if is_registry_artifact(i)]

    @property
    def adjudicated(self) -> list[Issue]:
        """Suppressed differences a project-local ``.bibaudit.toml`` silenced."""
        return [i for i in self.suppressed if not is_registry_artifact(i)]
