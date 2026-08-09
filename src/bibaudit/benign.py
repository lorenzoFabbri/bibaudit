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
from .normalize import clean, fold, is_article_number, pmc_number

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

#: Opening word a catalogue may file a serial without. Applied to a folded
#: value, where punctuation is already gone and the separator is one space.
_LEADING_ARTICLE = re.compile(r"^(?:the|a|an) ")

#: How NLM separates a serial's title from the rest of the title it files that
#: serial under, in ``JT``: ``Journal of clinical oncology : official journal
#: of the American Society of Clinical Oncology``. Spaced on both sides, which
#: is NLM's own convention and not a title's ordinary "Title: subtitle" colon.
#: The spacing is the whole of the separator's safety: Crossref's literal
#: ``container-title`` for 10.1161/circoutcomes.5.suppl_1.a180 is ``Circulation:
#: Cardiovascular Quality and Outcomes``, a different AHA journal from
#: *Circulation*, and a bare colon would suppress the difference between the
#: two — the sibling-journal merge :func:`_container_abbreviation`'s final-token
#: rule exists to prevent. Matched before :func:`~bibaudit.normalize.fold`,
#: which deletes the punctuation this depends on — see
#: :func:`_container_medline_subtitle`.
_MEDLINE_SUBTITLE = " : "

#: Registries whose :attr:`~bibaudit.model.Record.years` vocabulary has a
#: ``"print"`` slot at all, so one of their records carrying none is a fact
#: about the work rather than about the registry's schema. Crossref is the only
#: one: ``crossref._years`` reads ``published-print`` beside
#: ``published-online`` and ``issued``, while MEDLINE has no print field for
#: ``pubmed._record_from_medline`` to read — ``DP`` *is* the issue the citation
#: is filed under — and DataCite, Open Library and the search clients write
#: ``issued`` alone. See :func:`_year_deposit_artifact`, whose guard this is.
_PRINT_DATE_REGISTRIES = frozenset({"crossref"})

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

    And only for a registry that *has* a print date to be missing. The guard
    reads an absent ``"print"`` key as evidence there was no print issue, which
    holds only where the registry models one — see
    :data:`_PRINT_DATE_REGISTRIES`. On a MEDLINE record it could never fire, so
    every PMID-resolved entry three or more years early was excused instead:
    ``tests/data/pubmed_epub_ahead_of_issue.txt`` is ``DP - 2026 May 1``, and a
    stored 2019 against it was filed as a deposit stamp and dropped out of the
    default report. A missing key that means "this registry does not record
    that" is ignorance, not a fact, which is the distinction the rest of this
    tool is built on.
    """
    if field != "year" or rec.source not in _PRINT_DATE_REGISTRIES:
        return None
    if "print" in rec.years:
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


def _number_zero_padded(field: str, stored: str, registry: str, ref: Reference, rec: Record) -> str | None:
    """Volume or issue written with a leading zero on one side and not the other.

    Instance: 10.1055/a-2760-7307 (*Clinics in Colon and Rectal Surgery*, PMID
    42553907). Crossref deposits ``"issue": "05"``; MEDLINE writes ``IP - 5``.
    A bibliography exported from either carries that registry's spelling, and
    on the PMID path the other one is the only value there is to compare
    against, so a correct entry reported ``issue/mismatch`` and failed the
    build.

    ``05`` and ``5`` are the same issue and neither side is wrong, which is why
    the reason blames nobody — the same shape as
    :func:`_pages_article_number`, where two registries record one article in
    two notations. It is a suppression rather than a normalisation because a
    difference this tool passes over in silence is one nobody can audit.

    Only where both sides are ASCII digits and differ by leading zeros alone,
    so ``18`` against ``Volume 18`` is untouched: that one is a label a
    reference manager copied off a publisher's page into a numeric field, the
    entry is wrong about the field's contents, and the fix is one the user can
    make.
    """
    if field not in {"volume", "issue"}:
        return None
    if not (stored.isascii() and stored.isdigit()):
        return None
    if not (registry.isascii() and registry.isdigit()):
        return None
    if stored.lstrip("0") != registry.lstrip("0"):
        return None
    return "one side writes the number with a leading zero"


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


def _container_leading_article(field: str, stored: str, registry: str, ref: Reference, rec: Record) -> str | None:
    """Stored name differs from a title the registry carries only by a leading article.

    Instance: PMID 9500320 — Wakefield et al., *The Lancet* 1998, the retracted
    MMR paper, recorded verbatim in
    ``tests/data/compare_pubmed_wakefield_retracted.txt``. MEDLINE files it
    under ``JT`` ``Lancet (London, England)`` with ``TA`` ``Lancet``; the
    bibliography, and Crossref, call the journal *The Lancet*. NLM drops the
    leading article from every serial title and appends a place qualifier
    wherever the bare title would be ambiguous — ``BMJ (Clinical research
    ed.)``, ``Science (New York, N.Y.)`` are the same shape. A reference
    resolved by its PMID has PubMed as its only registry and no corroborator to
    supply another spelling, so without this every correct Lancet, BMJ or
    Science entry is a ``FIELD-MISMATCH``.

    Only an opening ``The``/``A``/``An`` may differ, and only against a title
    **the record itself carries** — ``TA`` reaches
    :attr:`~bibaudit.model.Record.container_alternates` for this. This rule
    reads whole titles and edits neither side beyond that one word; the
    parenthetical qualifier comes off in
    :func:`_container_medline_qualifier`, which is where the case for removing
    it is made.

    The article comes off the **stored** value only, because that is what the
    reason printed beside the suppression says happened. Taking it off both
    sides suppressed ``A Journal of Cancer`` against ``The Journal of Cancer``
    — two different articles, and a difference the registry did not drop
    anything to produce — under a sentence that is false of it. The reverse
    direction, an entry storing ``Lancet`` against a registry's ``The
    Lancet``, is :func:`_container_abbreviation`'s and stays there.
    """
    if field != "container":
        return None
    stored_bare = _LEADING_ARTICLE.sub("", fold(stored))
    for candidate in (registry, *rec.container_alternates):
        folded = fold(candidate)
        if folded and folded == stored_bare:
            return "registry files the journal without its leading article"
    return None


def _container_medline_subtitle(field: str, stored: str, registry: str, ref: Reference, rec: Record) -> str | None:
    """Stored name is the journal's; the registry adds NLM's own subtitle to it.

    Instance: PMID 32430337 — Michaud et al., 10.1158/1055-9965.EPI-20-0378,
    recorded verbatim in ``tests/data/pubmed_society_expansion.txt``. MEDLINE's
    ``JT`` is ``Cancer epidemiology, biomarkers & prevention : a publication of
    the American Association for Cancer Research, cosponsored by the American
    Society of Preventive Oncology``; the masthead, the bibliography and
    Crossref all say *Cancer Epidemiology, Biomarkers & Prevention*. NLM writes
    a subtitle after its spaced colon on 26 of the 173 journals in a 300-record
    sample of live ``efetch`` output — *J Clin Oncol*, *Clin Cancer Res*, *Ann
    Oncol*, *Toxicol Sci* and *Am J Transplant* among them.

    The reason says *subtitle* rather than *society* because the society is
    only the commonest of the things NLM puts there, and the reason printed
    beside a suppression is the whole of the account a reader gets of it.
    ``Archives of medical science : AMS`` (PMID 42540560) and ``The Malaysian
    journal of medical sciences : MJMS`` (PMID 42534732) carry the title's own
    acronym; ``The British journal of psychiatry : the journal of mental
    science`` (PMID 42552687) carries a descriptive phrase naming no
    organisation at all. All three are one defect, all three are suppressed,
    and a reason naming a society would be false of most of the entries it
    appears beside.

    Nothing else covers it. ``TA`` reaches
    :attr:`~bibaudit.model.Record.container_alternates`, but where it is a real
    abbreviation (``Cancer Epidemiol Biomarkers Prev``) it equals neither the
    stored value nor anything :func:`_container_abbreviation` will accept: that
    rule requires the stored tokens to reach the *end* of the registry's name,
    and the subtitle is what they cannot reach. On the PMID path PubMed is the
    only registry there is and ``JT`` the only container, so every correct
    entry citing one of those journals was a ``FIELD-MISMATCH``.

    Only the text before NLM's own :data:`_MEDLINE_SUBTITLE` separator is
    compared, and it has to equal the stored name outright: *Cancer
    Epidemiology* — a different journal — differs from the base title by a
    word, so it still fires. A **parenthetical** qualifier is the other shape
    NLM's filing titles take, and it is removed by
    :func:`_container_medline_qualifier`; this rule reads the colon and
    nothing else.

    The *first* spaced colon in ``JT`` is not always NLM's own separator: it
    writes one **inside** a parenthetical qualifier where the body named there
    needs a date of its own to be unambiguous — ``ASAIO journal (American
    Society for Artificial Internal Organs : 1992)``, PMID 42552576. Splitting
    on that colon leaves a base ending mid-qualifier, and half a qualifier is
    not a name any stored value should be compared against. An unclosed
    parenthesis in the base is what that looks like, and it is refused — the
    rule next door then takes the qualifier off whole.

    NLM drops a serial's leading article on most titles and keeps it on some,
    so the article has to come off the **registry's** base for the second
    shape: ``JT - The Journal of adolescent health : official publication of
    the Society for Adolescent Medicine`` beside ``TA - J Adolesc Health``
    (PMID 42547188), where the masthead and Crossref both say *Journal of
    Adolescent Health* with no article at all. Nothing else reaches that one
    either — :func:`_container_leading_article` compares against the whole of
    ``JT``, :func:`_container_abbreviation` needs the stored tokens to reach
    ``JT``'s last one, and ``TA`` is a real abbreviation — so a correct entry
    failed the build. Which of the two happened is what the returned reason
    says, because a suppression whose stated cause is not the actual one
    cannot be argued with.

    The **stored** side is never stripped, so at most one article is dropped
    in any comparison and ``A Journal of Cancer`` against ``The Journal of
    Cancer : ...`` stays a difference — two journals, and nothing the registry
    did produced it. The opposite pairing, an entry storing ``The X`` against a
    ``JT`` of ``X : subtitle``, has no witnessed instance and is not accepted.
    """
    if field != "container":
        return None
    base, separator, _ = registry.partition(_MEDLINE_SUBTITLE)
    if not separator or base.count("(") != base.count(")"):
        return None
    folded_base, folded_stored = fold(base), fold(stored)
    if folded_base == folded_stored:
        return "registry appends its own subtitle to the journal name"
    if _LEADING_ARTICLE.sub("", folded_base) == folded_stored:
        return "registry files the journal under a leading article and a subtitle"
    return None


def _without_trailing_qualifier(title: str) -> str | None:
    """*title* with one trailing balanced parenthetical removed, or ``None``.

    Matched from the closing parenthesis back to the ``(`` that balances it,
    rather than by a pattern over the qualifier's contents, because NLM writes
    both a spaced colon and a nested parenthetical inside one: ``ASAIO journal
    (American Society for Artificial Internal Organs : 1992)`` (PMID 42552576)
    and ``Clinical oncology (Royal College of Radiologists (Great Britain))``
    (PMID 42546669) each lose their qualifier whole.

    ``None`` when there is no trailing parenthetical, when nothing opens it, or
    when what is left still holds an unclosed ``(``: four titles in NLM's own
    serial list end on a parenthesis that does not close the qualifier —
    ``Interventional radiology (Higashimatsuyama-shi (Japan)``, NlmId
    101745449, is one — and the remainder there is a fragment of a name rather
    than a name. Callers must treat ``None`` as "no comparison to make".
    """
    if not title.endswith(")"):
        return None
    depth = 0
    for index in range(len(title) - 1, -1, -1):
        char = title[index]
        if char == ")":
            depth += 1
        elif char == "(":
            depth -= 1
            if depth:
                continue
            remainder = title[:index].rstrip()
            if not remainder or remainder.count("(") != remainder.count(")"):
                return None
            return remainder
    return None


def _container_medline_qualifier(field: str, stored: str, registry: str, ref: Reference, rec: Record) -> str | None:
    """Stored name is the journal's; the registry adds a parenthetical qualifier to it.

    Instances: PMID 42555391, ``JT - Annals of medicine and surgery (2012)``
    beside ``TA - Ann Med Surg (Lond)``, recorded verbatim in
    ``tests/data/pubmed_qualifier_year.txt``, and PMID 42552576, ``JT - ASAIO
    journal (American Society for Artificial Internal Organs : 1992)`` beside
    ``TA - ASAIO J``, in ``tests/data/pubmed_qualifier_inner_colon.txt``. NLM
    appends a qualifier — a place, a founding year, the issuing body, or
    several at once — wherever a bare title would be ambiguous in its
    catalogue, on 2,697 of the 37,979 serials in its own list,
    ``ftp.ncbi.nlm.nih.gov/pubmed/J_Medline.txt``. The masthead, Crossref and
    the bibliography carry the bare title, and where ``TA`` is a real
    abbreviation rather than the plain name nothing else reaches the entry:
    :func:`_container_abbreviation` needs the stored tokens to reach ``JT``'s
    last one, which is inside the qualifier, and
    :func:`_container_leading_article` compares against the whole of ``JT``.

    **Why the qualifier may be dropped**, when in a catalogue it is exactly
    what tells two serials sharing a base title apart: that is a fact about
    looking a journal up, and nothing here looks one up. By the time
    :func:`~bibaudit.compare.compare` reaches ``container`` the work is already
    pinned — by its DOI or PMID, or, for an entry carrying neither, by the
    title, first author and year :func:`~bibaudit.compare.confirm_without_id`
    demanded before any record was adopted — and a work appears in exactly one
    serial. The registry's ``JT`` is by construction the serial *this* work
    appeared in, so there is one serial in play and nothing to merge. What the
    rule accepts is ``Lancet`` for a work published in ``Lancet (London,
    England)``, which is correct.

    **What it costs**, stated so it can be argued with: for this to be a miss,
    a citation would have to name a journal sharing a base title with the right
    one *and* be wrong about it. Such pairs exist — ``The neurologist`` (NlmId
    9503763) beside ``The neurologist (Hyderabad, India)`` (NlmId 101719078),
    and 316 qualified titles whose base is some other serial's full name — so
    an entry resolved by PMID 30151503 and naming the journal *The Neurologist*
    is accepted. That is the whole of the exposure: it needs the citation to be
    wrong in the one field the identifier has already settled, and every other
    field of the entry is still compared against the record.

    NLM drops a serial's leading article on most titles and keeps it on some,
    so one opening ``The``/``A``/``An`` comes off the **registry's** remainder
    too, exactly as :func:`_container_medline_subtitle` does after its colon.
    ``The neurologist (Hyderabad, India)``, ``TA - Neurologist (Hyderabad)``
    (PMID 30151503, ``tests/data/pubmed_qualifier_leading_article.txt``) is the
    shape: ten of the qualified serials keep an article, and it is the only one
    whose remainder is a plain journal name. The **stored** side is never
    stripped, so at most one article is dropped in any comparison and ``A
    Journal of Cancer`` against ``The Journal of Cancer (Basel, Switzerland)``
    stays a difference. Which of the two happened is what the returned reason
    says.

    Never a prefix or a substring test: the remainder has to equal the stored
    name outright. *Cancer Epidemiology* against *Cancer Epidemiology,
    Biomarkers & Prevention* differs by a word rather than by a qualifier, and
    fires here for the same reason it fires next door.
    """
    if field != "container":
        return None
    base = _without_trailing_qualifier(registry)
    if base is None:
        return None
    folded_base, folded_stored = fold(base), fold(stored)
    if folded_base == folded_stored:
        return "registry appends a parenthetical qualifier to the journal name"
    if _LEADING_ARTICLE.sub("", folded_base) == folded_stored:
        return "registry files the journal under a leading article and a qualifier"
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


def _pmid_pmc_accession(field: str, stored: str, registry: str, ref: Reference, rec: Record) -> str | None:
    """Stored "PMID" is this very record's PMC accession, not another citation.

    Instance: PMID 28520842 (*Am J Epidemiol* 2017, 10.1093/aje/kwx137). Its
    MEDLINE record carries ``PMC  - PMC5860629`` beside ``PMID- 28520842``, so
    ``efetch id=28520842`` shows both numbers on one citation. NLM issues a
    PMID and a PMC accession for the same deposited article and Zotero keeps
    them on adjacent lines of one ``Extra`` box, which is where a ``pmid``
    field comes to hold ``5860629``.

    That number names *this* record under NLM's other accession scheme, so
    ``compare._check_pmid``'s claim — that the entry's two identifiers name two
    citations — is not true of it, and the entry is not evidence of anything to
    check. :func:`~bibaudit.normalize.normalize_pmid` already refuses the
    prefixed ``PMC5860629``; only the stripped form gets this far.
    """
    if field != "pmid":
        return None
    values = rec.raw.get("PMC")
    if not isinstance(values, list):
        return None
    for value in values:
        if pmc_number(value) == stored:
            return "stored number is this record's own PMC accession"
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
    _number_zero_padded,
    _container_abbreviation,
    _container_leading_article,
    _container_medline_subtitle,
    _container_medline_qualifier,
    _doi_redirecting_prefix,
    _pmid_pmc_accession,
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
