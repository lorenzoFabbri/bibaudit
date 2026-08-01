"""Documented cases where the registry is wrong and the stored record is right.

A field-level checker that cannot express "the publisher's own metadata is
defective here" reports those cases as defects, the report fills with noise, and
people stop reading it. That failure mode is more dangerous than not checking at
all, because a report nobody reads still looks like assurance.

Every pattern below is a *documented, reproducible* registry defect, not a
tolerance. Each returns a reason string when it recognises the situation and
``None`` otherwise. Matches downgrade an issue to informational and are listed
in the report under REGISTRY-ARTIFACT — they are never silently dropped, and
they never cause a value to be adopted.

Sources for these are recorded in ``docs/registry-artifacts.md``.

**Every rule must name an instance somebody can look up** — a DOI anyone can
resolve — and not merely
describe the shape of the defect. A suppression whose instance nobody can fetch
cannot be challenged, and an unchallengeable suppression is how a check quietly
stops being a check. Three of the rules below currently carry a **NO WITNESSED
INSTANCE** note: they describe a shape that a search of the corpus and of the
live registries did not find. Each says what was searched. They are candidates
for deletion, not for widening.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from .model import Record, Reference
from .normalize import clean, fold, is_article_number

__all__ = ["ArtifactCheck", "classify"]

#: A check receives (field, stored value, registry value, reference, record) and
#: returns a human-readable reason if the difference is a known registry defect.
ArtifactCheck = Callable[[str, str, str, Reference, Record], "str | None"]

#: Crossref deposits that came through a mangled MathML pipeline repeat an
#: operator token, e.g. a title containing "do(x)do(x)". The doubling is an
#: artefact of the deposit, not of the citing bibliography.
#:
#: NO WITNESSED INSTANCE — see :func:`_title_mathml`.
_MATHML_DOUBLING = re.compile(r"(\b\w{1,4}\([a-z]\))\1", re.IGNORECASE)

#: PubMed and some publishers prefix a comment or reply with the parent
#: article's title in square brackets. The stored record legitimately carries
#: only the comment's own title.
#:
#: NO WITNESSED INSTANCE of that description — see :func:`_title_bracketed_parent`.
_BRACKETED_PARENT = re.compile(r"^\[[^\]]{10,}\]\s*[:.]?\s*")

#: DOI prefixes that redirect to a different registrant's DOI for the same work.
#: JSTOR is the common one: 10.2307/2669548 (Greenland, *Causal Analysis in the
#: Health Sciences*, JASA 2000) is registered to JSTOR, and doi.org answers a
#: request for it with ``301 -> https://doi.org/10.1080/01621459.2000.10473924``,
#: the Taylor & Francis DOI for the same article. Same work, different
#: identifier, not an error. One ``curl -I`` reproduces it.
_REDIRECTING_PREFIXES = ("10.2307/",)

#: Smallest gap, in years, between an entry's year and a registry ``issued``
#: date that :func:`_year_deposit_artifact` will read as a deposit timestamp
#: rather than as a wrong year. A re-deposited working paper lands many years
#: out (the rule's own example is a 2020 paper carrying 2026); a one- or
#: two-year gap is what citing a preprint year, or mistyping the last digit,
#: looks like, and a reader can settle either in one click.
_MIN_DEPOSIT_STAMP_GAP = 3


def _title_shortened(field: str, stored: str, registry: str, ref: Reference, rec: Record) -> str | None:
    """Registry stores a truncated title where the bibliography has the full one.

    Instance: ``sinha2009meatmortality``, 10.1001/archinternmed.2009.6. Crossref
    holds *Meat Intake and Mortality*; the entry, and the paper, are *Meat
    Intake and Mortality: A Prospective Study of Over Half a Million People*.
    JAMA-network deposits drop the subtitle routinely — this rule fires on ten
    of the 438 corpus entries, more than any other rule here, including
    10.1001/archinte.167.22.2461 and 10.1212/WNL.0000000000004856.

    Accepted only when the registry title is a **leading fragment** of the
    stored one, ending on a word boundary: that is a lost subtitle, not a
    different paper. The reverse (stored shorter than registry) is *not*
    accepted here — an entry missing its subtitle is a real incompleteness the
    user may want to fix.

    The rule used to accept the registry title appearing *anywhere* inside the
    stored one, and that is the shape of a wrong-work citation, not of a lost
    subtitle. An entry whose title is ``Corrigendum to 'Shift work and
    colorectal cancer risk in the MCC-Spain case-control study' [Scand J Work
    Environ Health 43(3) 250-259]`` stored against 10.5271/sjweh.3626 — the
    *original* paper's DOI, not the corrigendum's — contains Crossref's title
    verbatim, scores 0.72 on the title comparison, and would otherwise be
    reported as ``mismatch`` and fail the build. It was instead filed as a
    registry defect and cleared. So were ``Reply to Kogevinas et al: <title>``,
    ``Erratum: <title>`` and ``Comment on '<title>': the exposure assessment is
    not credible`` — every one of them an entry pointing at the work it
    responds to rather than at itself, which is one of the commonest real
    citation errors there is.

    The word boundary matters on its own: without it ``Comment`` is a leading
    fragment of ``Commentary on shift work``, and a registry title of
    ``Comment`` would explain away a stored title about a different thing.
    Both witnessed shapes survive — ``Meat Intake and Mortality`` opens
    ``Meat Intake and Mortality: A Prospective Study of Over Half a Million
    People``, and ``Comment`` opens ``Comment on 'Statistics and Causal
    Inference'``.
    """
    if field != "title":
        return None
    a, b = fold(stored), fold(registry)
    if b and b != a and a.startswith(f"{b} "):
        return "registry stores a shortened title"
    return None


def _title_mathml(field: str, stored: str, registry: str, ref: Reference, rec: Record) -> str | None:
    """Registry title carries doubled tokens from a mangled MathML deposit.

    **NO WITNESSED INSTANCE. Candidate for deletion.** Searched: all 438 corpus
    entries, and 17,144 Crossref titles from the journals where mathematical
    notation in a title is routine (*Journal of Causal Inference*, *Biometrika*,
    *Statistics in Medicine*, *Biometrics*, *PLOS ONE*). The pattern
    ``do(x)do(x)`` matched nothing anywhere.

    Doubling from mangled markup *is* real — 10.1002/(SICI)1097-0258(19980730)
    17:14<1601::AID-SIM870>3.0.CO;2-2 is deposited as "...uterine receptivity
    inin vitro fertilization", the word doubled across a lost ``<i>`` — but
    :data:`_MATHML_DOUBLING` does not match that shape either, so the rule
    neither has an instance nor catches the instance that exists. Widening it to
    "collapse any immediately repeated token" would suppress far more than one
    deposit defect and has no evidence behind it. Either replace it with a rule
    written against a recorded response, or delete it; a suppression with no
    known instance is a hole with no reason to exist.
    """
    if field != "title" or not _MATHML_DOUBLING.search(registry):
        return None
    repaired = _MATHML_DOUBLING.sub(r"\1", registry)
    if fold(repaired) == fold(stored):
        return "registry title mangled by a MathML deposit"
    return None


def _title_bracketed_parent(field: str, stored: str, registry: str, ref: Reference, rec: Record) -> str | None:
    """Registry prefixes a comment/reply with its parent article's title.

    **NO WITNESSED INSTANCE of that description. Candidate for narrowing or
    deletion.** Searched: all 438 corpus entries (Crossref and PubMed), and
    PubMed's ``comment[pt]``, ``"published erratum"[pt]`` and author-reply
    title searches. Not one registry title was a *parent article's title* in
    brackets followed by the item's own.

    What the search did find is a bracketed *label*: PMID 42535368,
    10.3892/mmr.2026.13976, whose title is "[Corrigendum] Identification of key
    differentially expressed genes associated with non-small cell lung cancer by
    bioinformatics analyses". The rule strips that label — the bracketed span is
    eleven characters, over the ten-character floor — and then accepts an entry
    that stores the *parent* article's title against the *corrigendum's* DOI.
    That is arguably a real citation error being suppressed, not a registry
    defect, which is the opposite of what this module is for.

    The bracketed form that PubMed really does use in bulk is a *wholly*
    bracketed translated title — 10.1016/j.medcli.2012.01.020 is registered as
    "[SIDIAP database: electronic clinical records in primary care as a source
    of information for epidemiologic research]" for a Spanish-language article —
    and this rule correctly leaves those alone, because stripping them leaves
    nothing to compare. That behaviour is worth keeping; the rest of the rule
    needs an instance or it needs to go.
    """
    if field != "title":
        return None
    stripped = _BRACKETED_PARENT.sub("", registry)
    if stripped != registry and fold(stripped) == fold(stored):
        return "registry prefixes the parent article title"
    return None


def _year_online_first(field: str, stored: str, registry: str, ref: Reference, rec: Record) -> str | None:
    """Entry cites one of the registry's own dates, just not the preferred one.

    Instance: ``gentiluomo2024ipmngwas``, 10.1002/cncr.35678. Crossref carries
    ``published-online`` 2024 and ``published-print`` 2025; the entry says 2024
    and is right. Also ``xiang2025reproductive``, 10.1097/CEJ.0000000000000987,
    online 2025 and print 2026. Both are correct citations that a "compare the
    year to the registry's preferred year" check reports.

    A work posted online in December and printed the following February has two
    correct years. Comparison already accepts any year the registry itself
    carries; this check exists so the *reason* is stated when it happens.
    """
    if field != "year" or not rec.years:
        return None
    try:
        value = int(stored)
    except (TypeError, ValueError):
        return None
    if value in rec.years.values():
        slot = next(k for k, v in rec.years.items() if v == value)
        return f"cites the {slot} date; registry prefers {rec.year}"
    return None


def _year_deposit_artifact(field: str, stored: str, registry: str, ref: Reference, rec: Record) -> str | None:
    """Registry year is a deposit timestamp, later than the work itself.

    **NO WITNESSED INSTANCE. Candidate for deletion.** Searched: all 438 corpus
    entries — four disagree with Crossref on the year and *all four run the
    other way* — and NBER, the working-paper series the description points at.
    NBER's ``issued`` dates are correct: 10.3386/w0001 carries ``issued``
    1973-06 and keeps the 2007-10-23 deposit stamp in ``created``, which this
    tool never reads. Crossref's ``created`` field is where deposit timestamps
    live, and it does not reach :class:`~bibaudit.model.Record.years` at all.

    Worth recording while this rule is being reconsidered: the *opposite*
    direction is a real, repeatable false positive and nothing here covers it.
    ``molinamontes2021diabetes``, 10.1136/gutjnl-2019-319990, is an online-first
    BMJ-group paper — Crossref has ``online`` 2020 and ``issued`` 2020 and **no
    print date**, while the entry cites the 2021 issue year, correctly. Three
    more corpus entries do the same (10.1093/aje/kwj364, 10.1093/aje/kwm361,
    10.1007/s10549-007-9523-x). Whoever replaces this rule should write that one
    instead, and should note it needs a defensible bound on the gap, because
    "the entry's year is later than anything the registry knows" is also what a
    wrong year looks like.

    That bound is now applied in *this* direction too, because it was missing
    and the rule had no lower limit at all: with no print date, **any** registry
    year later than the stored one was called a deposit timestamp. An entry
    citing 2019 against a registry that issues the work in 2021 is the ordinary
    preprint-year-for-the-published-version error — the exact shape of
    10.1136/gutjnl-2019-319990 read backwards — and it was excused as a deposit
    stamp on a one-line arithmetic. So was a one-year gap, which is what a typo
    in the last digit looks like. :data:`_MIN_DEPOSIT_STAMP_GAP` keeps the
    described scenario (a series re-depositing an old item, which lands many
    years out — the 2020 paper carrying 2026) and reports the near misses,
    which is the direction that costs a reader nothing to check.
    """
    if field != "year" or "print" in rec.years:
        return None
    try:
        stored_year, registry_year = int(stored), int(registry)
    except (TypeError, ValueError):
        return None
    if registry_year - stored_year >= _MIN_DEPOSIT_STAMP_GAP:
        return "registry year looks like a deposit timestamp"
    return None


def _pages_article_number(field: str, stored: str, registry: str, ref: Reference, rec: Record) -> str | None:
    """One side records an article number where the other records a page range.

    Instance: ``arslan2009vitamindovary``, 10.1155/2009/672492 (*Journal of
    Oncology*). Crossref deposits ``page`` as ``1-8``, the article's own
    pagination; PubMed records ``672492``, the article number. The entry stores
    ``1-8``. The two registries describe one article in two notations, and
    whichever of them the tool happens to read first decides whether the entry
    looks wrong.

    Journals that number rather than paginate articles deposit inconsistently;
    both forms identify the same item.
    """
    if field != "pages":
        return None
    if is_article_number(stored) != is_article_number(registry):
        return "article number recorded against a page range"
    return None


def _container_abbreviation(field: str, stored: str, registry: str, ref: Reference, rec: Record) -> str | None:
    """Stored journal name is the ISO abbreviation of the registry's full name.

    Instance: ``gomezrubio2019autoimmune``, 10.1002/ijc.31866. PubMed's
    ``source`` is ``Int J Cancer``; Crossref's ``container-title`` is
    ``International Journal of Cancer``. Any bibliography exported from PubMed,
    EndNote or a journal's own MEDLINE-style "cite this" carries the former.
    Across the 438-entry corpus, 366 entries have PubMed abbreviating a name
    Crossref spells out — this is the single largest source of container
    disagreement there is.

    The same record is why the ``short-container-title`` shortcut below is not
    enough on its own: Crossref's short title for 10.1002/ijc.31866 is ``Intl
    Journal of Cancer``, which is not the ISO abbreviation and does not equal
    the stored value, and for 10.1158/1055-9965.EPI-20-0378
    (``michaud2020methylation``, PubMed ``Cancer Epidemiol Biomarkers Prev``)
    the field is empty altogether. The token-prefix path is what actually
    carries both cases.
    """
    if field != "container":
        return None
    short = fold(rec.container_short or "")
    if short and fold(stored) == short:
        return "stored name is the journal's ISO abbreviation"
    # "Int J Cancer" vs "International Journal of Cancer": every abbreviated word
    # is a prefix of the corresponding full word, in order.
    stored_tokens = fold(stored).split()
    registry_tokens = fold(registry).split()
    if stored_tokens and len(stored_tokens) <= len(registry_tokens):
        cursor = 0
        last_matched = -1
        for token in stored_tokens:
            while cursor < len(registry_tokens) and not registry_tokens[cursor].startswith(token):
                cursor += 1
            if cursor == len(registry_tokens):
                return None
            last_matched = cursor
            cursor += 1
        # The abbreviation has to reach the *end* of the registry's name. Without
        # this, "Nature" passes as an abbreviation of "Nature Genetics", "The
        # Lancet" of "The Lancet Oncology" and "JAMA" of "JAMA Network Open" —
        # different journals in one family, and citing the parent title for a
        # paper that appeared in the offshoot is one of the commonest real
        # citation errors there is. Every ISO abbreviation covers the whole
        # title ("Int J Cancer" ends on "Cancer"), so the requirement costs
        # nothing, and it still accepts a merely dropped leading article
        # ("Lancet" for "The Lancet").
        if last_matched == len(registry_tokens) - 1:
            return "stored name abbreviates the registry name"
    return None


def _doi_redirecting_prefix(field: str, stored: str, registry: str, ref: Reference, rec: Record) -> str | None:
    """Stored DOI belongs to an aggregator that redirects to the publisher's.

    Instance: 10.2307/2669548 — Greenland, *Causal Analysis in the Health
    Sciences*, JASA 2000 — registered to JSTOR, redirected by doi.org to
    10.1080/01621459.2000.10473924 at Taylor & Francis. Both identifiers resolve
    to the same article, so an entry citing the JSTOR one is not wrong.
    """
    if field != "doi":
        return None
    if stored.startswith(_REDIRECTING_PREFIXES):
        return "aggregator DOI redirects to the publisher's own"
    return None


#: Order matters only for which reason is reported first; the checks are
#: independent and none of them consumes another's input.
CHECKS: tuple[ArtifactCheck, ...] = (
    _title_shortened,
    _title_mathml,
    _title_bracketed_parent,
    _year_online_first,
    _year_deposit_artifact,
    _pages_article_number,
    _container_abbreviation,
    _doi_redirecting_prefix,
)


def classify(
    field: str,
    stored: object,
    registry: object,
    ref: Reference,
    record: Record,
) -> str | None:
    """Return why a disagreement is a known registry defect, or ``None``.

    ``None`` means the disagreement stands and should be reported as a defect.
    """
    stored_text, registry_text = clean(stored), clean(registry)
    if not stored_text or not registry_text:
        return None
    for check in CHECKS:
        reason = check(field, stored_text, registry_text, ref, record)
        if reason:
            return reason
    return None
