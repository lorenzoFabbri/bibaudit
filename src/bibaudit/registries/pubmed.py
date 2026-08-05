"""NCBI E-utilities client for PubMed — independent corroboration.

OpenAlex, Semantic Scholar and Unpaywall all re-crawl Crossref's own
deposits, so agreement between them and Crossref is the same metadata read
twice, not two opinions. PubMed's citations are curated by NLM separately
from the publisher's Crossref deposit, which is why this is the registry
consulted for a genuine second view rather than a third look at Crossref's
data.

PubMed has no bulk DOI lookup, so resolving a batch of DOIs is a three-step
pipeline:

1. ``esearch`` — DOI -> candidate PMIDs, searching the ``[aid]`` (Article ID)
   field. One query ORs several DOIs together to keep the request count down,
   which means the PMIDs it returns are not yet attributable to any one DOI.
2. ``esummary`` — PMID -> its own DOI, read back from ``articleids``. This is
   what actually resolves step 1's ambiguity: the result order of an ``OR``
   query need not follow the order the DOIs were listed in, so pairing
   candidates positionally would silently misattribute a hit the moment a
   batch dropped even one DOI.
3. ``efetch`` (MEDLINE text) — the full citation for every PMID recovered.

A caller that already holds the PMID needs none of that. ``efetch`` answers
for a PMID directly, so :meth:`PubMed.by_pmids` issues step 3 alone: steps 1
and 2 exist to *obtain* a PMID and to attribute the answer back to the DOI
that asked for it, and a reference storing its own PMID has both already.
NCBI's three-requests-a-second ceiling is the whole client's rather than each
endpoint's, so the two steps not taken are two requests not spent.
"""

from __future__ import annotations

import re
import threading
import time
import urllib.parse
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from ..model import Name, Record
from ..names import parse_name
from ..normalize import clean, fold, normalize_doi, normalize_pmid, parse_year
from .http import Client

__all__ = ["PmidAnswers", "PubMed"]

_EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_ESEARCH_URL = f"{_EUTILS}/esearch.fcgi"
_ESUMMARY_URL = f"{_EUTILS}/esummary.fcgi"
_EFETCH_URL = f"{_EUTILS}/efetch.fcgi"

#: DOIs per esearch OR-query. Large enough to make a real dent in a big
#: bibliography's request count, small enough that the query string and the
#: retmax it implies stay comfortably inside normal GET URL limits.
_ESEARCH_BATCH = 20
#: esummary responses are small (a few hundred bytes each); batching more of
#: them per request is cheap and keeps the request count down.
_ESUMMARY_BATCH = 200
#: efetch in MEDLINE format returns full records — title, abstract, author
#: list — so the same URL-length headroom only buys a much smaller batch
#: than esummary's.
_EFETCH_BATCH = 50

#: NCBI's documented ceiling for unauthenticated E-utilities traffic.
#: Client's own per-host throttle exists to be polite to Crossref's pool, not
#: to enforce NCBI's stricter cap, and nothing stops it from being configured
#: looser than 3 req/s for other registries' sake. This class enforces its
#: own floor on top so PubMed traffic is safe regardless of that setting.
_MAX_REQUESTS_PER_SECOND = 3.0
_MIN_REQUEST_GAP = 1.0 / _MAX_REQUESTS_PER_SECOND

#: A MEDLINE field line: a 2-4 letter tag, padding spaces, then "- ", then
#: the value. A line that does *not* match this — in particular an indented
#: one — is a continuation of the field just opened, not a new field.
_TAG_LINE_RE = re.compile(r"^([A-Z]{2,4})\s*- (.*)$")

#: PubMed wraps a machine-translated title in brackets with a trailing
#: period, e.g. "[Effet du traitement sur la survie].": the article's own
#: title is not English and TI carries NLM's English gloss instead of it.
_TRANSLATED_TITLE_RE = re.compile(r"^\[(.*)\]\.?$")

#: A title whose last word is an abbreviation — "... in the U.S.", "... e.g."
#: NLM does not double the period there: the one period ends both the
#: abbreviation and the citation, so the house-style strip below must not
#: take it. Stripping it yields "... in the U.S", which `compare._check_title`
#: reports as a cosmetic title difference on every entry of such a paper
#: (folded titles agree, display strings do not), and which `--suggest` would
#: then offer as a replacement for the bibliography's correct spelling. Two or
#: more letter-period pairs are required so an ordinary final word ("...
#: disorder in children.") is still stripped.
_ABBREVIATION_TAIL_RE = re.compile(r"(?:[A-Za-z]\.){2,}$")

#: The MEDLINE ``PT`` (publication type) value NLM puts on an article that has
#: been retracted, folded for comparison.
#:
#: The neighbouring value is ``Retraction of Publication``, and it means the
#: opposite: that record *is* the notice announcing a retraction, which is a
#: perfectly citable document and not itself a defect. The two differ by one
#: word, so any check looser than an exact match — a substring test for
#: "retract", say — clears the retracted paper and flags the notice that
#: retracted it. That is the worst mistake this tool can make, which is why
#: this is an equality test against a single controlled-vocabulary value.
#: PMID 9500320 (Wakefield et al., Lancet 1998) carries ``PT  - Retracted
#: Publication``; PMID 20137807, the Lancet notice that retracted it, carries
#: ``PT  - Retraction of Publication``.
_PT_RETRACTED = "retracted publication"


def _chunk(items: Sequence[str], size: int) -> Iterator[list[str]]:
    """Split *items* into consecutive lists of at most *size* elements."""
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def _clean_title(raw: str | None) -> tuple[str | None, bool]:
    """MEDLINE ``TI`` -> ``(display title, was this a translated-title gloss)``."""
    if raw is None:
        return None, False
    text = clean(raw)
    if not text:
        return None, False
    match = _TRANSLATED_TITLE_RE.match(text)
    if match:
        return match.group(1).strip(), True
    if text.endswith(".") and not _ABBREVIATION_TAIL_RE.search(text):
        # MEDLINE always terminates TI with a period that is house style, not
        # part of the title; every other registry in this tool omits it, and
        # keeping it here would report a cosmetic mismatch on every entry.
        # The abbreviation guard is the exception — see _ABBREVIATION_TAIL_RE.
        text = text[:-1].rstrip()
    # Stripping that period can leave nothing (a TI of just "."). Every other
    # scalar builder in this tool (e.g. crossref._text) returns None rather
    # than "" for an empty field, because Record's callers use `if
    # record.title:` to mean "the registry had something to say" -- an empty
    # string there would read as "present but blank" instead of "absent".
    return text or None, False


def _parse_au_fallback(raw: str) -> Name:
    """Parse one MEDLINE ``AU`` (abbreviated author) entry, e.g. "van Eijck CHJ".

    ``AU`` has no comma and orders surname before initials — the opposite of
    the "Given Family" order :func:`~bibaudit.names.parse_name` assumes for a
    comma-less string, which is BibTeX's convention, not MEDLINE's. Passing
    "Smith JA" to it unmodified would swap surname and given name and fail
    the author comparison on the surname alone. ``FAU`` (which does carry a
    comma, "Smith, John A") is used whenever present; this fallback exists
    only for the older citations where ``FAU`` was never backfilled.
    """
    text = clean(raw).strip()
    if not text:
        return Name()
    # A pre-2002 MEDLINE citation can carry a collective author's name
    # directly in AU with no FAU ever backfilled and no CN tag either (e.g.
    # "Multiple Risk Factor Intervention Trial Research Group"). parse_name's
    # own collective check only fires on a comma-less string, so it has to
    # see this text before the synthetic "surname, initials" comma is
    # introduced below -- inserting that comma first defeats the check and
    # turns the whole group name into a bogus family/given split (family=
    # every word but the last, given=the last word).
    whole = parse_name(text)
    if whole.collective:
        return whole
    tokens = text.split()
    if len(tokens) < 2:
        return Name(family=text)
    # MEDLINE's abbreviated form is always "<surname tokens...> <initials>";
    # the last token is the initials block regardless of how many words the
    # surname itself has ("van Eijck CHJ").
    surname, initials = " ".join(tokens[:-1]), tokens[-1]
    return parse_name(f"{surname}, {initials}")


def _authors_from(fields: dict[str, list[str]]) -> list[Name]:
    """``FAU`` when present, else ``AU`` — see :func:`_parse_au_fallback`."""
    full = fields.get("FAU")
    if full:
        return [parse_name(value) for value in full if value]
    abbreviated = fields.get("AU", [])
    return [_parse_au_fallback(value) for value in abbreviated if value]


def _retraction(fields: dict[str, list[str]]) -> tuple[bool, str | None]:
    """Whether ``PT`` says *this record's own article* was retracted.

    Returns the flag and the publication type verbatim, for the report. See
    :data:`_PT_RETRACTED` for why the direction matters and why the match is
    an equality test on the folded value rather than anything looser: a
    record whose only retraction-related ``PT`` is ``Retraction of
    Publication`` is a retraction *notice*, and reporting it as retracted
    would flag the correction while clearing the paper it corrects.

    PubMed is consulted precisely because NLM curates this independently of
    the publisher's Crossref deposit, so a paper Crossref never received a
    retraction notice for can still be caught here.
    """
    for value in fields.get("PT", []):
        if fold(value) == _PT_RETRACTED:
            return True, clean(value)
    return False, None


def _first(values: list[str] | None) -> str | None:
    if not values:
        return None
    return values[0] or None


def _parse_medline_records(text: str) -> list[dict[str, list[str]]]:
    """Split MEDLINE plain text into records, tags into repeatable field lists.

    Records are separated by a blank line. Within a record, a line matching
    :data:`_TAG_LINE_RE` starts a new field; anything else is a continuation
    of the field just opened and is *appended*, not discarded — efetch wraps
    long values (titles, abstracts) onto indented follow-on lines, and
    dropping those silently truncates exactly the fields most worth checking.
    Repeatable tags (``FAU``, ``AU``, ``AD``, ...) keep every occurrence, in
    order, as a list.
    """
    records: list[dict[str, list[str]]] = []
    current: dict[str, list[str]] = {}
    tag: str | None = None
    buffer: list[str] = []

    def flush_field() -> None:
        nonlocal tag, buffer
        if tag is not None:
            current.setdefault(tag, []).append(" ".join(buffer).strip())
        tag, buffer = None, []

    def flush_record() -> None:
        nonlocal current
        flush_field()
        if current:
            records.append(current)
        current = {}

    for line in text.splitlines():
        if not line.strip():
            flush_record()
            continue
        match = _TAG_LINE_RE.match(line)
        if match:
            flush_field()
            tag = match.group(1)
            buffer = [match.group(2).strip()]
        elif tag is not None:
            buffer.append(line.strip())
        # A non-blank line before any tag has been seen would not be valid
        # MEDLINE output; dropping it is safer than guessing which field it
        # belongs to.

    flush_record()
    return records


def _record_from_medline(fields: dict[str, list[str]]) -> Record:
    """Build a :class:`Record` from one MEDLINE block, ``doi`` unset.

    The block's own ``PMID`` line *is* carried: it is the citation's identity
    as NLM states it, and the only PMID this module ever has grounds to put on
    a record. The DOI is left to the caller, which knows the key the record was
    fetched under; MEDLINE's own ``AID`` list reaches ``raw`` and no further.

    ``TA`` is carried as a container *alternate* as well as as the short title,
    for the reason :attr:`bibaudit.model.Record.container_alternates` gives:
    both are titles PubMed itself names for the same journal, and an entry
    citing either is right.
    """
    title, translated = _clean_title(_first(fields.get("TI")))

    years: dict[str, int] = {}
    year = parse_year(_first(fields.get("DP")))
    if year is not None:
        years["issued"] = year
    # ``DP`` is the issue the citation is filed under; ``DEP`` is the day the
    # work actually went online, and for anything published ahead of its issue
    # the two carry different years. PMID 41474069 (*Int J Cancer*, recorded in
    # ``tests/data/pubmed_epub_ahead_of_issue.txt``) is ``DP - 2026 May 1``
    # against ``DEP - 20251231``: a bibliography populated at e-pub time stores
    # 2025, which is a year this very record carries, and reading ``DP`` alone
    # reported it as wrong. On the PMID path PubMed is the only registry there
    # is, so nothing else supplies the second date the way Crossref's
    # ``published-online`` does on the DOI path. ``PHST`` is not read instead:
    # that record's history stamps are ``[received]``, ``[accepted]``,
    # ``[revised]``, ``[pubmed]``, ``[entrez]``, ``[medline]``, ``[pmc-release]``
    # and no ``[epublish]`` at all, so the stamps that exist are dates the work
    # was *not* published on.
    online = parse_year(_first(fields.get("DEP")))
    if online is not None:
        years["online"] = online

    raw: dict[str, Any] = dict(fields)
    if translated:
        raw["translated"] = True

    retracted, retraction_kind = _retraction(fields)

    # NLM files a serial under a title of its own making: the leading article
    # dropped, a place-of-publication qualifier appended wherever the base title
    # would otherwise be ambiguous, and the sponsoring society spelled out after
    # a spaced colon. ``JT`` is "Lancet (London, England)", "BMJ (Clinical
    # research ed.)", "Science (New York, N.Y.)", "Cancer epidemiology,
    # biomarkers & prevention : a publication of the American Association for
    # Cancer Research, cosponsored by ..."; ``TA`` is the abbreviation, which
    # for the first three is the journal's plain name and for the fourth is not
    # (``benign._container_society_subtitle`` is what reaches that one). On the
    # PMID path PubMed is the only registry there is, so ``JT`` would be the
    # only container an entry could match, and no bibliography stores it.
    journal = _first(fields.get("JT"))
    abbreviation = _first(fields.get("TA"))
    alternates = (
        [abbreviation] if abbreviation and fold(abbreviation) != fold(journal) else []
    )

    return Record(
        source="pubmed",
        pmid=_first(fields.get("PMID")),
        title=title,
        authors=_authors_from(fields),
        years=years,
        container=journal,
        container_short=abbreviation,
        container_alternates=alternates,
        volume=_first(fields.get("VI")),
        issue=_first(fields.get("IP")),
        pages=_first(fields.get("PG")),
        retracted=retracted,
        retraction_kind=retraction_kind,
        raw=raw,
    )


@dataclass(frozen=True)
class PmidAnswers:
    """What ``efetch`` said about a batch of PMIDs — three states, not two.

    :attr:`records` holds the citations attributed to the number they were
    asked under. :attr:`inconclusive` holds the numbers ``efetch`` answered
    *around*. A PMID in neither is one nothing came back under at all, and that
    one is the only one of the three that is evidence about a bibliography —
    see :meth:`PubMed.by_pmids` for how narrow that evidence is.

    The distinction is CLAUDE.md's "404 is a fact, a timeout is ignorance" at
    an edge with a third state: a request that came back with a record nobody
    asked for has neither timed out nor said no. One dict would make those two
    the same empty entry, and :mod:`~bibaudit.compare` turns an empty entry
    into ``BAD-ID``.
    """

    #: PMID as asked for -> the MEDLINE record whose own ``PMID`` line is that
    #: number.
    records: dict[str, Record] = field(default_factory=dict)
    #: PMID as asked for -> the PMIDs its ``efetch`` batch came back under
    #: instead, empty when the batch was not answered at all. Attribution
    #: within a batch is impossible — ``efetch`` may return the records in any
    #: order and simply omits the ones it has nothing for — so every
    #: unanswered number in a batch that produced a stray record carries the
    #: whole stray list. Guessing which one it belongs to is the positional
    #: pairing the module docstring rules out for ``esearch``.
    inconclusive: dict[str, tuple[str, ...]] = field(default_factory=dict)


class PubMed:
    """NCBI PubMed lookup, by DOI or by PMID.

    A DOI takes the ``esearch`` -> ``esummary`` -> ``efetch`` pipeline the
    module docstring sets out; a PMID takes ``efetch`` alone.

    Every request goes through ``client``, but see :data:`_MAX_REQUESTS_PER_SECOND`
    for why this class also paces its own calls independently of ``client``'s
    per-host throttle.
    """

    name = "pubmed"

    def __init__(self, client: Client) -> None:
        self._client = client
        self._lock = threading.Lock()
        self._next_allowed_at = 0.0

    def _throttle(self) -> None:
        """Block until :data:`_MIN_REQUEST_GAP` seconds have passed since this
        instance's last request, counting ``esearch``, ``esummary`` and
        ``efetch`` together — the 3 req/s NCBI asks for is a ceiling on all
        E-utilities traffic from one caller, not a per-endpoint allowance.
        """
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed_at - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed_at = now + _MIN_REQUEST_GAP

    def _get_json(self, url: str, params: dict[str, str]) -> dict[str, Any] | None:
        self._throttle()
        return self._client.get_json(f"{url}?{urllib.parse.urlencode(params)}")

    def _get_text(self, url: str, params: dict[str, str]) -> str | None:
        self._throttle()
        return self._client.get_text(f"{url}?{urllib.parse.urlencode(params)}")

    def _pmids_by_doi(self, dois: Sequence[str]) -> dict[str, list[str]]:
        """Every PMID PubMed attributes to each of *dois*, in the order found.

        A DOI missing from the result means PubMed has no PMID for it — the
        normal case for anything outside biomedicine, not an error. A registry
        outage during ``esearch``/``esummary`` instead raises
        :class:`~bibaudit.registries.http.Transient`, so it is never confused
        with a confirmed absence.

        The list is kept whole because *how many* PMIDs came back for one DOI
        is evidence in its own right, and collapsing to one throws it away.
        Two mean either that PubMed holds two citations for the work or that
        one record lists another's identifier among its own article ids — the
        case the ``doi in wanted`` filter below drops, in the residual form
        where both records claim a DOI that *was* asked for. Neither can be
        settled from this side, which is why :meth:`by_dois` withholds the
        PMID rather than picking one.
        """
        normalized = list(dict.fromkeys(doi for raw in dois if (doi := normalize_doi(raw))))
        if not normalized:
            return {}

        candidate_pmids: set[str] = set()
        for batch in _chunk(normalized, _ESEARCH_BATCH):
            candidate_pmids.update(self._esearch(batch))
        if not candidate_pmids:
            return {}

        wanted = set(normalized)
        result: dict[str, list[str]] = {}
        for batch in _chunk(sorted(candidate_pmids), _ESUMMARY_BATCH):
            for pmid, doi in self._esummary(batch).items():
                if doi in wanted:
                    result.setdefault(doi, []).append(pmid)
        return result

    def by_dois(self, dois: Sequence[str]) -> dict[str, Record]:
        """Fetch full MEDLINE records for every DOI PubMed has a PMID for.

        Keyed by normalized DOI. A registry outage during any of the three
        E-utilities calls propagates as
        :class:`~bibaudit.registries.http.Transient` rather than being caught
        here: this method answers for the whole batch at once, and a partial
        outage must not be reported as some of the batch's DOIs being
        confirmed absent from PubMed.

        Each record carries the PMID it was fetched under, so that a reference
        storing a PMID beside its DOI can be checked against it — unless
        PubMed answered for that DOI under more than one PMID, in which case
        it carries none. See the comment on ``ambiguous`` below.
        """
        candidates = self._pmids_by_doi(dois)
        if not candidates:
            return {}

        # A PMID could in principle be the target of more than one requested
        # DOI (a duplicate deposit); keep every DOI it should map back to
        # rather than dropping all but the last.
        pmid_to_dois: dict[str, list[str]] = {}
        for doi, pmids in candidates.items():
            pmid_to_dois.setdefault(pmids[-1], []).append(doi)

        # The other direction, and it is not the same fact. Any of the PMIDs a
        # DOI came back under fetches a usable citation, so the record still
        # goes out; *which* one it is, is arbitrary. The record therefore
        # carries no PMID, because the only thing that reads one —
        # `compare._check_pmid` — would otherwise report a bibliography storing
        # the PMID this line happened not to pick as disagreeing with PubMed,
        # when PubMed named both.
        ambiguous = {doi for doi, pmids in candidates.items() if len(pmids) > 1}

        out: dict[str, Record] = {}
        for batch in _chunk(list(pmid_to_dois), _EFETCH_BATCH):
            text = self._efetch_medline(batch)
            if text is None:
                continue
            for fields in _parse_medline_records(text):
                record_pmid = _first(fields.get("PMID"))
                dois_for_pmid = pmid_to_dois.get(record_pmid) if record_pmid else None
                if not dois_for_pmid:
                    continue
                record = _record_from_medline(fields)
                for doi in dois_for_pmid:
                    # A fresh copy per DOI: Record is mutable, and two DOIs
                    # sharing one Record instance would make the second
                    # assignment's `.doi` silently override the first's.
                    out[doi] = replace(
                        record, doi=doi, pmid=None if doi in ambiguous else record.pmid
                    )
        return out

    def by_pmids(self, pmids: Sequence[str]) -> PmidAnswers:
        """Fetch full MEDLINE records for *pmids*. See :class:`PmidAnswers`.

        A PMID in neither half of the answer came back with no citation under
        it, which is the whole of the evidence a ``BAD-ID`` on a PMID rests on.
        It is an omission, not a sentence: ``efetch`` returns the citations it
        holds for the numbers in the request and says nothing whatever about
        the rest. Witnessed: 20000157 and 35000082 (deleted citations) and
        99999999 (above the highest number NLM has assigned) all answer HTTP
        200 with an empty body — no error, no substitute record, nothing. So a
        withdrawal and an absence are indistinguishable here, and that is the
        limit ``docs/verdicts.md`` states. An answer of any other shape is not
        this fact at all, and is reported as ignorance.

        An outage raises :class:`~bibaudit.registries.http.Transient`, for the
        reason :meth:`by_dois` gives: this answers for a whole batch at once,
        and half a batch must never be reported as the other half being
        confirmed absent.

        Only ``efetch`` is issued — see the module docstring on why the
        ``esearch``/``esummary`` pair that opens :meth:`by_dois` is not a step
        this path skips but one it never needed.
        """
        wanted = list(dict.fromkeys(p for raw in pmids if (p := normalize_pmid(raw))))
        if not wanted:
            return PmidAnswers()

        out: dict[str, Record] = {}
        inconclusive: dict[str, tuple[str, ...]] = {}
        for batch in _chunk(wanted, _EFETCH_BATCH):
            requested = set(batch)
            text = self._efetch_medline(batch)
            if text is None:
                # A 404 on ``efetch.fcgi`` itself is a fact about the request,
                # never about the numbers in it: NCBI answers 200 for a PMID
                # it does not hold. Read as an absence it would condemn every
                # entry in a batch of fifty at once, so the batch is ignorance
                # — and only this batch, because the others' answers are still
                # good and ``Transient`` would throw them away.
                inconclusive.update(dict.fromkeys(batch, ()))
                continue
            others: list[str] = []
            for fields in _parse_medline_records(text):
                # Attributed by each record's *own* ``PMID`` line, exactly as
                # in ``by_dois``: a record answering under a number nobody
                # asked for is not the citation this reference names, and
                # adopting it would hand the entry another paper's metadata
                # and another paper's retraction status. NO WITNESSED INSTANCE
                # of ``efetch`` doing so — the two deleted PMIDs above come
                # back empty rather than redirected — so the guard is a
                # precaution, and what it declines is reported as such.
                record_pmid = _first(fields.get("PMID"))
                if record_pmid is None:
                    continue
                if record_pmid not in requested:
                    others.append(record_pmid)
                    continue
                out[record_pmid] = _record_from_medline(fields)
            if others:
                strays = tuple(dict.fromkeys(others))
                inconclusive.update({p: strays for p in batch if p not in out})
        return PmidAnswers(records=out, inconclusive=inconclusive)

    def _esearch(self, dois: list[str]) -> list[str]:
        """PMIDs matching any of *dois* by Article ID.

        Result order carries no meaning and no attribution back to a
        specific DOI — see the module docstring on why ``esummary`` is a
        separate, required step rather than an optimisation.
        """
        term = " OR ".join(f'("{doi}"[aid])' for doi in dois)
        params = {
            "db": "pubmed",
            "term": term,
            "retmode": "json",
            # A generous margin over the batch size: a single [aid] match is
            # normally one PMID, but nothing guarantees a corrected or
            # reprinted citation cannot match twice, and retmax must not
            # silently truncate real hits.
            "retmax": str(max(100, len(dois) * 5)),
        }
        payload = self._get_json(_ESEARCH_URL, params)
        if not isinstance(payload, dict):
            return []
        esearchresult = payload.get("esearchresult")
        if not isinstance(esearchresult, dict):
            return []
        idlist = esearchresult.get("idlist")
        return list(idlist) if isinstance(idlist, list) else []

    def _esummary(self, pmids: list[str]) -> dict[str, str]:
        """PMID -> its own DOI, read from each summary's ``articleids``."""
        params = {"db": "pubmed", "id": ",".join(pmids), "retmode": "json"}
        payload = self._get_json(_ESUMMARY_URL, params)
        if not isinstance(payload, dict):
            return {}
        result = payload.get("result")
        if not isinstance(result, dict):
            return {}

        out: dict[str, str] = {}
        for uid in result.get("uids", []):
            entry = result.get(uid)
            if not isinstance(entry, dict):
                continue
            for article_id in entry.get("articleids", []):
                if not isinstance(article_id, dict):
                    continue
                if article_id.get("idtype") == "doi":
                    doi = normalize_doi(article_id.get("value") or "")
                    if doi:
                        out[uid] = doi
                    break
        return out

    def _efetch_medline(self, pmids: list[str]) -> str | None:
        params = {
            "db": "pubmed",
            "id": ",".join(pmids),
            "rettype": "medline",
            "retmode": "text",
        }
        return self._get_text(_EFETCH_URL, params)
