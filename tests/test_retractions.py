"""``registries.retractions``: Retraction Watch's own data, and PubMed's ``ECI``.

Offline only. :class:`_StubClient` stands in for
:class:`~bibaudit.registries.http.Client`, so nothing here reaches the
network — including the Retraction Watch fetch, which a naive test would
otherwise turn into a ~66 MB download on every run.

Two groups of fixtures matter more than the rest:

* ``tests/data/retraction_watch_sample.csv`` is **fourteen rows copied
  verbatim from the live export, and five this project wrote**. The copied
  rows cover five DOIs RW logged more than once, which is what proves
  :func:`~bibaudit.registries.retractions._parse_rw_csv` picks the strongest
  notice rather than the newest, the first or the last: the Wakefield paper
  (a 2004 correction and a later 2010 retraction), 10.1002/ana.24658 (a 2016
  retraction and a later 2019 correction), 10.1371/journal.pone.0058088 (a
  2022 concern and a later 2024 correction), 10.1308/rcsann.2020.0038 (a
  2021 reinstatement and a concern raised after it) and 10.1093/ageing/28.3.265
  (a 2019 concern upgraded to a retraction later the same year). The last of
  those, and the single ``Correction`` row for 10.1016/j.ymthe.2023.01.020,
  are the two DOIs the sources disagree about — one in each direction; see
  :class:`TestSourcesThatDisagree`.

  The written rows carry ``Record ID`` 90001-90004 and 90006 and say so in
  their own ``Title`` column. Each stands for a shape the export holds
  thousands of but never beside a DOI these tests already use: a
  reinstatement withdrawing an earlier retraction of the same DOI
  (90001/90002), a blank ``OriginalPaperDOI``, RW's ``Unavailable`` sentinel
  in that column -- for both of those,
  :func:`~bibaudit.registries.retractions._looks_like_doi`'s docstring holds
  the live counts and is the only place that states them -- and a
  blank ``RetractionNature`` (241 rows). Copying a real row for each would
  have pulled in a second DOI per case with its own notice history, which is
  what the DOIs above are for; these carry ``10.9999/`` identifiers precisely
  so that nothing here can be mistaken for a citation.
* ``tests/data/pubmed_eci_concern*.txt`` are MEDLINE ``efetch`` output for
  PMID 23741377 (the affected paper) and PMID 34710116 (the notice), fetched
  live. The pair is what proves ``ECI`` ("Expression of Concern In:") is read
  on the affected paper and its mirror-image ``ECF`` ("Expression of Concern
  For:") on the notice is not — the identical direction discipline
  ``registries.pubmed`` already applies to ``PT``.
"""

from __future__ import annotations

import json
import shutil
import urllib.parse
import warnings
from pathlib import Path
from typing import Any

import pytest

from bibaudit.model import Record
from bibaudit.normalize import normalize_doi
from bibaudit.registries import retractions
from bibaudit.registries.http import Cache, Client, Transient
from bibaudit.registries.retractions import (
    RetractionNotice,
    RetractionOutage,
    Retractions,
    concern_in,
)

DATA = Path(__file__).parent / "data"

WAKEFIELD_DOI = "10.1016/S0140-6736(97)11096-0"
WAKEFIELD_PMID = "9500320"
RETRACTION_NOTICE_DOI = "10.1016/S0140-6736(10)60175-4"
RETRACTION_NOTICE_PMID = "20137807"
NEIRINCKX_DOI = "10.1371/journal.pone.0064723"
NEIRINCKX_PMID = "23741377"
CONCERN_NOTICE_DOI = "10.1371/journal.pone.0256488"
CONCERN_NOTICE_PMID = "34710116"
#: The paper no source has a notice for: PMID 37726507, recorded verbatim in
#: ``tests/data/pubmed_wrapped_title.txt``, and absent from the Retraction
#: Watch extract beside it.
CLEAN_DOI = "10.1038/s41370-023-00600-7"
CLEAN_PMID = "37726507"

#: The two sources disagree about this DOI, with Retraction Watch the milder of
#: them. Its only row in the 2026-08-09 export is a ``Correction``, filed — as RW
#: files a correction — under the correcting article's own DOI, and that
#: article's MEDLINE citation carries ``PT - Retracted Publication``: the
#: erratum was itself retracted. Recorded in
#: ``tests/data/pubmed_retracted_erratum.txt``.
RW_CORRECTION_PUBMED_RETRACTION_DOI = "10.1016/j.ymthe.2023.01.020"
RW_CORRECTION_PUBMED_RETRACTION_PMID = "36736314"
#: RW files this correction under the corrected article's own DOI, so the
#: notice DOI and the DOI it is about are one string. Named rather than
#: repeated, because a reader meeting it twice in an assertion would take it
#: for a copy-paste slip.
RW_CORRECTION_NOTICE_DOI = RW_CORRECTION_PUBMED_RETRACTION_DOI

#: The same disagreement the other way up: RW logs an expression of concern
#: (2019-02-05) and then a ``Retraction`` (2019-11-20) against Sato et al.,
#: *Age and Ageing* 1999, while NLM has never given that citation a retraction
#: ``PT`` — its only post-publication signal is the ``ECI`` naming the 2019
#: concern. Recorded in ``tests/data/pubmed_concern_only.txt``.
RW_RETRACTION_PUBMED_CONCERN_DOI = "10.1093/ageing/28.3.265"
RW_RETRACTION_PUBMED_CONCERN_PMID = "10475862"
RW_RETRACTION_NOTICE_DOI = "10.1093/ageing/afz156"


#: A Retraction Watch export that downloaded intact and simply does not mention
#: the DOI under test. It is the stub's default because an *empty* body is no
#: longer that: since :meth:`Retractions._load_index` reads an export with no
#: usable row as an outage, a test wanting "Retraction Watch answered and had
#: nothing for this DOI" has to hand it an export with a row in it. The DOI is
#: one no fixture in this file uses.
RW_EXPORT_WITHOUT_THIS_DOI = (
    "Record ID,Title,Subject,Institution,Journal,Publisher,Country,Author,"
    "URLS,ArticleType,RetractionDate,RetractionDOI,RetractionPubMedID,"
    "OriginalPaperDate,OriginalPaperDOI,OriginalPaperPubMedID,"
    "RetractionNature,Reason,Paywalled,Notes,\n"
    "1,T,,,J,P,,A,,,1/2/2020 0:00,10.1000/notice,0,1/1/2019 0:00,"
    "10.1000/unrelated,0,Retraction,,No,,\n"
)


#: The columns :func:`~bibaudit.registries.retractions._parse_rw_csv` reads,
#: in the live export's own order.
_RW_COLUMNS = RW_EXPORT_WITHOUT_THIS_DOI.splitlines()[0]


def _rw_rows(*rows: tuple[str, str, str, str]) -> str:
    """An export holding exactly *rows*, each ``(doi, date, nature, notice)``.

    Written out rather than extracted from the live file because the shapes
    these exercise have no live instance: no DOI in the 2026-08-09 export
    carries an undated notice beside a dated reinstatement, two reinstatements,
    or a reinstatement dated the same day as the notice it withdraws. 241 rows
    of that export carry no date at all, so the first of those is one export
    away, and a rule with no live instance is exactly the one nothing else
    holds in place.
    """
    body = "".join(
        f"1,T,,,J,P,,A,,,{date},{notice},0,1/1/2019 0:00,{doi},0,{nature},,No,,\n"
        for doi, date, nature, notice in rows
    )
    return f"{_RW_COLUMNS}\n{body}"


def _pubmed_fixture(name: str) -> str:
    return (DATA / f"pubmed_{name}.txt").read_text(encoding="utf-8")


def _rw_sample() -> str:
    return (DATA / "retraction_watch_sample.csv").read_text(encoding="utf-8")


def _rewrite_cached_kind(cache_dir: Path, kind: str) -> None:
    """Set every notice kind in the on-disk Retraction Watch index to *kind*.

    How a value outside this module's own vocabulary reaches the merge:
    ``_index_from_payload`` reads that file back with no vocabulary check on
    ``kind``, over a file its docstring calls stale or hand-edited, and the
    index sits there for the whole seven-day TTL. The file is written by the
    real :class:`~bibaudit.registries.http.Cache` first and edited in place, so
    a change to its layout fails these tests rather than leaving them editing
    something nothing reads.
    """
    [path] = list(cache_dir.rglob("*.json"))
    envelope = json.loads(path.read_text(encoding="utf-8"))
    for notice in envelope["payload"]["notices"].values():
        notice["kind"] = kind
    path.write_text(json.dumps(envelope), encoding="utf-8")


def _params(url: str) -> dict[str, str]:
    query = urllib.parse.urlsplit(url).query
    return {k: v[0] for k, v in urllib.parse.parse_qs(query, keep_blank_values=True).items()}


class _StubClient:
    """Stands in for :class:`~bibaudit.registries.http.Client`.

    ``retractions.py`` reaches the network in two, unrelated ways: one
    ``get_text`` call for the Retraction Watch CSV, and — through a real
    ``PubMed`` instance it constructs internally — the ordinary
    esearch/esummary/efetch pipeline ``test_pubmed.py`` already stubs the
    same way. Both are handled by this one fake so a test can exercise
    :meth:`Retractions.status_for` end to end without knowing which internal
    path a DOI happens to take.
    """

    def __init__(
        self,
        *,
        rw_csv: str | None = RW_EXPORT_WITHOUT_THIS_DOI,
        rw_transient: bool = False,
        pmid_by_doi: dict[str, str] | None = None,
        medline_by_pmid: dict[str, str] | None = None,
        pubmed_transient: bool = False,
    ) -> None:
        self.rw_csv = rw_csv
        self.rw_transient = rw_transient
        self.pmid_by_doi = dict(pmid_by_doi or {})
        self.medline_by_pmid = dict(medline_by_pmid or {})
        self.pubmed_transient = pubmed_transient
        #: How many times the Retraction Watch URL was actually fetched --
        #: the on-disk/in-process caching tests assert this stays at 1.
        self.rw_fetch_count = 0
        #: Whether the export was asked for past the shared registry cache.
        #: Recorded rather than ignored because that flag is the whole of the
        #: seven-day bound: stored in a cache whose TTL is ``--cache-ttl``,
        #: the body outlives the index built from it and answers the refetch
        #: its expiry asked for.
        self.rw_bypassed_cache: bool | None = None
        self.urls: list[str] = []

    def get_text(
        self,
        url: str,
        *,
        cache_key: str | None = None,
        headers: dict[str, str] | None = None,
        bypass_cache: bool = False,
    ) -> str | None:
        self.urls.append(url)
        if "retractionwatch" in url:
            self.rw_fetch_count += 1
            self.rw_bypassed_cache = bypass_cache
            if self.rw_transient:
                raise Transient(f"{url}: registry unreachable after 5 attempts")
            return self.rw_csv
        if "efetch.fcgi" in url:
            if self.pubmed_transient:
                raise Transient(f"{url}: registry unreachable after 5 attempts")
            requested = _params(url)["id"].split(",")
            blocks = [self.medline_by_pmid[p] for p in requested if p in self.medline_by_pmid]
            return "\n".join(blocks)
        raise AssertionError(f"unexpected text request: {url}")

    def get_json(
        self, url: str, *, cache_key: str | None = None, headers: dict[str, str] | None = None
    ) -> dict[str, Any] | None:
        self.urls.append(url)
        if "esearch.fcgi" in url:
            if self.pubmed_transient:
                raise Transient(f"{url}: registry unreachable after 5 attempts")
            term = _params(url)["term"]
            # `pubmed.py` builds the esearch term from the *normalised* DOI;
            # matching on the raw key here would silently find nothing for a
            # DOI supplied in its natural, mixed-case form (Elsevier and
            # Lancet DOIs, `10.1016/S0140-6736(97)11096-0` among them).
            idlist = [
                pmid
                for doi, pmid in self.pmid_by_doi.items()
                if f'"{normalize_doi(doi)}"[aid]' in term
            ]
            return {
                "header": {"type": "esearch", "version": "0.3"},
                "esearchresult": {"count": str(len(idlist)), "retmax": "100", "idlist": idlist},
            }
        if "esummary.fcgi" in url:
            requested = _params(url)["id"].split(",")
            doi_by_pmid = {pmid: doi for doi, pmid in self.pmid_by_doi.items()}
            result: dict[str, Any] = {"uids": list(requested)}
            for uid in requested:
                article_ids: list[dict[str, Any]] = [{"idtype": "pubmed", "idtypen": 1, "value": uid}]
                doi = doi_by_pmid.get(uid)
                if doi is not None:
                    article_ids.insert(0, {"idtype": "doi", "idtypen": 3, "value": doi})
                result[uid] = {"uid": uid, "source": "test", "articleids": article_ids}
            return {"header": {"type": "esummary"}, "result": result}
        raise AssertionError(f"unexpected JSON request: {url}")


class _CountingClient(Client):
    """The real :class:`~bibaudit.registries.http.Client`, wired to a body.

    :class:`_StubClient` replaces ``get_text`` outright, which is exactly the
    method whose shared cache-then-network path is under test here — a stub
    cannot see that path at all. This one replaces only the socket, so
    ``_fetch_cached`` runs for real and the registry cache is the real one.
    """

    def __init__(self, cache: Cache, export: str) -> None:
        super().__init__(cache=cache, min_interval=0.0)
        self.export = export
        #: Fetches of the export alone. PubMed's leg goes through this client
        #: too and must not be counted as one.
        self.export_fetches = 0

    def _request(self, url: str, headers: dict[str, str]) -> bytes | None:
        if "retractionwatch" in url:
            self.export_fetches += 1
            return self.export.encode("utf-8")
        # PubMed holds no PMID for anything here, so the answer under test is
        # Retraction Watch's alone.
        return b'{"esearchresult": {"count": "0", "retmax": "100", "idlist": []}}'


def _cached_urls(root: Path) -> list[str]:
    """Every URL the shared registry cache holds a body for."""
    return [
        json.loads(path.read_text(encoding="utf-8")).get("url", "")
        for path in root.rglob("*.json")
        if not path.name.startswith(".tmp-")
    ]


def _client(**kwargs: Any) -> _StubClient:
    """A stub with no Retraction Watch data and no PubMed hits, by default.

    Individual tests layer in only the fixture(s) their scenario needs, so a
    test asserting on the RW-sourced answer cannot accidentally pass because
    a stray PubMed hit supplied the same finding, and vice versa.
    """
    return _StubClient(**kwargs)


class TestPublicSurface:
    def test_the_name_attribute_matches_every_other_registry_client(self, tmp_path: Path) -> None:
        retractions = Retractions(_client(), cache_dir=tmp_path)
        assert retractions.name == "retractions"

    def test_no_dois_touches_neither_source(self, tmp_path: Path) -> None:
        """An empty request must not download a 66 MB file to answer nothing."""
        stub = _client()
        result = Retractions(stub, cache_dir=tmp_path).status_for([]).notices
        assert result == {}
        assert stub.urls == []

    def test_a_doi_neither_source_has_ever_heard_of_is_simply_absent(self, tmp_path: Path) -> None:
        stub = _client(rw_csv=_rw_sample())
        result = Retractions(stub, cache_dir=tmp_path).status_for(["10.1000/nothing-to-see-here"]).notices
        assert result == {}


class TestRetractionWatchCsv:
    """:func:`~bibaudit.registries.retractions._parse_rw_csv`, exercised through
    :meth:`Retractions.status_for` end to end rather than called directly --
    a private helper under test would keep passing if ``status_for`` stopped
    wiring it in at all.
    """

    def test_a_retraction_is_reported(self, tmp_path: Path) -> None:
        stub = _client(rw_csv=_rw_sample())
        result = Retractions(stub, cache_dir=tmp_path).status_for([WAKEFIELD_DOI]).notices
        notice = result[WAKEFIELD_DOI.lower()]
        assert notice.kind == "retraction"
        assert notice.source == "retraction-watch"
        assert notice.notice_doi == RETRACTION_NOTICE_DOI.lower()
        assert notice.date == "2010-02-06"

    def test_the_earlier_correction_does_not_win_over_a_later_retraction(
        self, tmp_path: Path
    ) -> None:
        """RW logged the Wakefield paper twice: a 2004 correction ("Updated to
        Retraction") and the actual 2010 retraction. Reporting the 2004 row
        because it happens to sort first, or last, in the export would call a
        paper retracted for fabricated data merely "corrected".
        """
        stub = _client(rw_csv=_rw_sample())
        result = Retractions(stub, cache_dir=tmp_path).status_for([WAKEFIELD_DOI]).notices
        assert result[WAKEFIELD_DOI.lower()].kind != "correction"

    def test_a_standalone_correction_is_reported_as_a_correction(self, tmp_path: Path) -> None:
        doi = "10.3390/nano14090769"
        stub = _client(rw_csv=_rw_sample())
        result = Retractions(stub, cache_dir=tmp_path).status_for([doi]).notices
        notice = result[doi]
        assert notice.kind == "correction"
        assert notice.notice_doi == "10.3390/nano15181429"

    def test_an_expression_of_concern_is_reported(self, tmp_path: Path) -> None:
        stub = _client(rw_csv=_rw_sample())
        result = Retractions(stub, cache_dir=tmp_path).status_for([NEIRINCKX_DOI]).notices
        notice = result[NEIRINCKX_DOI]
        assert notice.kind == "expression-of-concern"
        assert notice.notice_doi == CONCERN_NOTICE_DOI
        assert notice.date == "2021-10-28"

    def test_a_later_correction_does_not_downgrade_an_earlier_retraction(
        self, tmp_path: Path
    ) -> None:
        """The pairing the Wakefield rows cannot make: the *strongest* row wins,
        not the newest.

        RW logs a 2016 ``Retraction`` for 10.1002/ana.24658 and a 2019
        ``Correction`` whose own ``Reason`` column reads ``Upgrade/Update of
        Prior Notice(s)``, and Crossref carries no ``updated-by`` for that DOI
        at all, so a rule keeping the newest row leaves nothing to contradict
        it. (That DOI resolves in no registry today, so the *entry* is
        ``BAD-ID`` rather than a clean pass; what is pinned here is the index,
        which is where the two rules differ.) The retraction row is placed
        *first* on purpose: keeping whichever row arrived last is the same
        defect wearing another hat.
        """
        doi = "10.1002/ana.24658"
        stub = _client(rw_csv=_rw_sample())
        result = Retractions(stub, cache_dir=tmp_path).status_for([doi]).notices
        notice = result[doi]
        assert notice.kind == "retraction"
        assert notice.notice_doi == "10.1002/ana.24676"
        assert notice.date == "2016-05-25"

    def test_a_later_correction_does_not_downgrade_an_earlier_concern(
        self, tmp_path: Path
    ) -> None:
        """The same rule one step down the priority order.

        RW logs a 2022 ``Expression of concern`` for 10.1371/journal.pone.0058088
        and a 2024 ``Correction``. A correction ranking above a concern would
        both pick the wrong row here and, where two sources answer, soften a
        concern one of them recorded into the other's correction.
        """
        doi = "10.1371/journal.pone.0058088"
        stub = _client(rw_csv=_rw_sample())
        result = Retractions(stub, cache_dir=tmp_path).status_for([doi]).notices
        assert result[doi].kind == "expression-of-concern"

    def test_a_reinstated_retraction_is_not_reported(self, tmp_path: Path) -> None:
        """RW's sole row for this DOI is a ``Reinstatement`` -- the retraction
        was reversed, and reporting it anyway is the false alarm CLAUDE.md's
        third rule forbids.
        """
        doi = "10.1016/j.heliyon.2023.e18637"
        stub = _client(rw_csv=_rw_sample())
        result = Retractions(stub, cache_dir=tmp_path).status_for([doi]).notices
        assert doi not in result

    def test_a_retraction_reversed_by_a_later_reinstatement_is_not_reported(
        self, tmp_path: Path
    ) -> None:
        """The pairing the test above cannot exercise on its own: an earlier
        retraction *and* a later reinstatement for the same DOI, proving the
        reversal is read by date and not merely by "a reinstatement row
        exists somewhere in the file".
        """
        doi = "10.9999/reinstated-paper"
        stub = _client(rw_csv=_rw_sample())
        result = Retractions(stub, cache_dir=tmp_path).status_for([doi]).notices
        assert doi not in result

    def test_a_notice_raised_after_a_reinstatement_still_stands(
        self, tmp_path: Path
    ) -> None:
        """A reinstatement withdraws what preceded it, not the file's whole DOI.

        10.1308/rcsann.2020.0038 was retracted, reinstated on 2021-09-07, and
        then had an expression of concern raised against it on 2022-03-23 —
        "eoc issued after retracted artice reinstated", in RW's own ``Notes``
        column. Reading a reinstatement row as "this DOI has nothing to report"
        loses the later notice outright.
        """
        doi = "10.1308/rcsann.2020.0038"
        stub = _client(rw_csv=_rw_sample())
        result = Retractions(stub, cache_dir=tmp_path).status_for([doi]).notices
        assert result[doi].kind == "expression-of-concern"

    @pytest.mark.parametrize("cell", ["", "Unavailable", "unavailable"])
    def test_a_row_with_no_original_paper_doi_is_read_as_no_row(
        self, cell: str, tmp_path: Path
    ) -> None:
        """The three values :func:`~bibaudit.registries.retractions._looks_like_doi`
        rejects, each as the only row of an export.

        Probed through the "no usable rows" outage rather than through a
        lookup, because that is the one place a skipped row is observable:
        ``status_for`` short-circuits an empty request, so a blank cell cannot
        be asked about at all, and asking by the row's *RetractionDOI* -- which
        is what these two tests did -- answers ``{}`` whatever the function
        returns, since the index keys on ``OriginalPaperDOI``. Accept any of
        the three and the export stops being empty and the outage disappears.

        Both spellings of the sentinel are here because both are live, and
        because the rejection is what makes the count in ``_looks_like_doi``'s
        own docstring the case-folded one.
        """
        csv = _rw_rows((cell, "1/1/2020 0:00", "Retraction", "10.9999/notice"))
        stub = _client(rw_csv=csv)
        with pytest.warns(RuntimeWarning, match="Retraction Watch"):
            status = Retractions(stub, cache_dir=tmp_path).status_for([WAKEFIELD_DOI])
        assert status.unreachable == frozenset({"retraction-watch"})

    def test_the_unavailable_sentinel_is_not_indexed_as_a_doi(self, tmp_path: Path) -> None:
        """The same rejection seen from the index side, on the whole extract:
        asking for the sentinel itself must not return row 90004's notice.
        """
        stub = _client(rw_csv=_rw_sample())
        index_probe = Retractions(stub, cache_dir=tmp_path).status_for(["Unavailable"]).notices
        assert index_probe == {}

    def test_an_unrecognised_retraction_nature_is_skipped_not_guessed(
        self, tmp_path: Path
    ) -> None:
        """A future RW category this module has never been taught about is
        left out rather than reported as a guessed "retraction" -- the
        conservative side of CLAUDE.md's third rule.
        """
        doi = "10.9999/unknown-nature-paper"
        csv = _rw_rows((doi, "1/1/2020 0:00", "Republication", "10.9999/notice"))
        stub = _client(rw_csv=csv)
        with pytest.warns(RuntimeWarning, match="does not recognise"):
            result = Retractions(stub, cache_dir=tmp_path).status_for([doi]).notices
        assert doi not in result

    def test_a_nature_this_build_cannot_rank_is_said_out_loud(
        self, tmp_path: Path
    ) -> None:
        """Skipping the row is a *missed* notice on the one field where a miss
        has no remedy, and nothing else in the run would ever mention it. The
        warning names the value and how many rows carried it, because the fix
        is an entry in this module's kind map and nobody can make it without
        knowing what to add.
        """
        doi = "10.9999/unknown-nature-paper"
        csv = _rw_rows(
            (doi, "1/1/2020 0:00", "Retract and replace", "10.9999/a"),
            ("10.9999/other", "1/1/2021 0:00", "Retract and replace", "10.9999/b"),
            ("10.9999/third", "1/1/2021 0:00", "Retraction", "10.9999/c"),
        )
        with pytest.warns(RuntimeWarning) as caught:
            Retractions(_client(rw_csv=csv), cache_dir=tmp_path).status_for([doi])
        [warning] = [w for w in caught if "does not recognise" in str(w.message)]
        assert "'retract and replace' (2 rows)" in str(warning.message)

    def test_the_warning_survives_the_index_it_was_parsed_into(
        self, tmp_path: Path
    ) -> None:
        """Warning on the parse warns on the cache *miss* and on nothing else.

        Every run after the first reads the index off disk, and for its whole
        seven-day life the skipped row was the silence the parser's own comment
        forbids: the DOI carries no notice, ``unreachable`` is empty,
        ``consulted`` reads ``retraction-watch: answered``, and the verdict is
        ``OK``. The parse that learned it is the only thing that can say it, so
        what it learned is cached beside what it read.
        """
        doi = "10.9999/unknown-nature-paper"
        csv = _rw_rows((doi, "1/1/2020 0:00", "Retract and replace", "10.9999/a"))
        with pytest.warns(RuntimeWarning, match="does not recognise"):
            Retractions(_client(rw_csv=csv), cache_dir=tmp_path).status_for([doi])

        # A second process, reading the index the first one wrote. Its own
        # fetch is broken, to prove the answer is the cached one.
        replayed = _client(rw_transient=True)
        with pytest.warns(RuntimeWarning, match="does not recognise") as caught:
            status = Retractions(replayed, cache_dir=tmp_path).status_for([doi])
        assert replayed.rw_fetch_count == 0
        [warning] = [w for w in caught if "does not recognise" in str(w.message)]
        assert "'retract and replace' (1 row)" in str(warning.message)
        # ...and the run is still not told the source failed, because it did
        # not: this is a source that answered and was not fully read.
        assert status.unreachable == frozenset()

    def test_the_warning_is_said_once_a_run_not_once_a_reference(
        self, tmp_path: Path
    ) -> None:
        """A bibliography is hundreds of ``status_for`` calls against one index.

        Repeating the caveat per reference is how a caveat stops being read,
        which is the same rule the false-alarm one comes from.
        """
        doi = "10.9999/unknown-nature-paper"
        csv = _rw_rows((doi, "1/1/2020 0:00", "Retract and replace", "10.9999/a"))
        retractions = Retractions(_client(rw_csv=csv), cache_dir=tmp_path)
        with pytest.warns(RuntimeWarning) as caught:
            retractions.status_for([doi])
            retractions.status_for(["10.9999/other"])
            retractions.status_for(["10.9999/third"])
        assert len([w for w in caught if "does not recognise" in str(w.message)]) == 1

    def test_an_index_written_by_an_older_build_is_not_read_as_an_empty_one(
        self, tmp_path: Path
    ) -> None:
        """The cached payload gained a shape, so the key gained a version.

        Left at the old one, a file written before this change is still found,
        and read by a reader that now looks for its notices under a key that
        file does not carry: every notice in it silently gone, for the seven
        days the index lives, while ``consulted`` reads ``retraction-watch:
        answered`` and nothing is unreachable. The suffix on
        ``_RW_INDEX_CACHE_KEY`` is what turns a shape change into a cache miss;
        the old key is written out here because it no longer exists in the
        source, and it is the file on disk that has to be told apart.
        """
        Cache(tmp_path, ttl_days=7).put(
            "index-v1",
            {
                "url": "https://api.labs.crossref.org/data/retractionwatch",
                "payload": {
                    WAKEFIELD_DOI.lower(): {
                        "doi": WAKEFIELD_DOI.lower(),
                        "kind": "retraction",
                        "source": "retraction-watch",
                        "notice_doi": None,
                        "date": "2010-02-02",
                    }
                },
            },
        )

        stub = _client(rw_csv=_rw_sample())
        status = Retractions(stub, cache_dir=tmp_path).status_for([WAKEFIELD_DOI])

        assert stub.rw_fetch_count == 1
        assert status.notices[WAKEFIELD_DOI.lower()].kind == "retraction"

    def test_a_replayed_index_that_read_everything_says_nothing(
        self, tmp_path: Path
    ) -> None:
        """The true-negative half of the replay: every value in the 2026-08-09
        export is one this module ranks, so a cached index built from it must
        be as quiet on the second run as on the first.
        """
        Retractions(_client(rw_csv=_rw_sample()), cache_dir=tmp_path).status_for(
            [WAKEFIELD_DOI]
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            status = Retractions(_client(rw_transient=True), cache_dir=tmp_path).status_for(
                [WAKEFIELD_DOI]
            )
        assert status.notices[WAKEFIELD_DOI.lower()].kind == "retraction"

    def test_a_vocabulary_this_build_knows_says_nothing(self, tmp_path: Path) -> None:
        """The true-negative half. Every value in the 2026-08-09 export is one
        this module ranks, so an ordinary run must be silent — a warning on
        every run is one nobody reads on the run that matters.
        """
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            status = Retractions(
                _client(rw_csv=_rw_sample()), cache_dir=tmp_path
            ).status_for([WAKEFIELD_DOI])
        assert status.notices[WAKEFIELD_DOI.lower()].kind == "retraction"

    def test_a_blank_nature_defaults_to_retraction(self, tmp_path: Path) -> None:
        """241 of 71,641 live rows carry no ``RetractionNature`` tag at all; the
        whole database's subject is retractions, so an untagged row is read
        as one rather than silently dropped.
        """
        doi = "10.9999/blank-nature-paper"
        stub = _client(rw_csv=_rw_sample())
        result = Retractions(stub, cache_dir=tmp_path).status_for([doi]).notices
        assert result[doi].kind == "retraction"
        assert result[doi].notice_doi == "10.9999/blank-nature-notice"


class TestWhichRowOfManyIsRead:
    """Two rows about one DOI, and which of them the index ends up holding.

    Both selections below are decided by a comparison rather than by the order
    the export happens to list the rows in, and both are observable: 104 DOIs
    in the 2026-08-09 export carry two or more rows of one kind.
    """

    DOI = "10.1000/twice"

    @pytest.mark.parametrize("oldest_first", [True, False])
    def test_within_one_kind_the_later_row_wins(
        self, tmp_path: Path, oldest_first: bool
    ) -> None:
        """Two rows of a kind are one status restated, and the later row is its
        current wording — the notice DOI and the date a reader would look up.
        Written both ways round, because a rule that keeps whichever row came
        last in the file agrees with this one on exactly one of them.
        """
        rows = [
            (self.DOI, "2/10/2017 0:00", "Expression of concern", "10.1000/first"),
            (self.DOI, "10/27/2025 0:00", "Expression of concern", "10.1000/latest"),
        ]
        csv = _rw_rows(*(rows if oldest_first else rows[::-1]))
        notice = Retractions(_client(rw_csv=csv), cache_dir=tmp_path).status_for(
            [self.DOI]
        ).notices[self.DOI]
        assert notice.notice_doi == "10.1000/latest"
        assert notice.date == "2025-10-27"

    def test_the_latest_reinstatement_is_the_cutoff(self, tmp_path: Path) -> None:
        """Not the last one in the file. A retraction logged between two
        reinstatements is withdrawn by the later of them and reported by the
        earlier, so which one is kept decides whether it is reported at all.
        """
        csv = _rw_rows(
            (self.DOI, "1/1/2024 0:00", "Reinstatement", ""),
            (self.DOI, "1/1/2020 0:00", "Reinstatement", ""),
            (self.DOI, "1/1/2022 0:00", "Retraction", "10.1000/notice"),
        )
        result = Retractions(_client(rw_csv=csv), cache_dir=tmp_path).status_for(
            [self.DOI]
        ).notices
        assert result == {}

    def test_a_notice_after_the_latest_reinstatement_still_stands(
        self, tmp_path: Path
    ) -> None:
        """The true-positive half, and the shape 10.1308/rcsann.2020.0038 has
        live: a concern raised six months after the paper was reinstated.
        """
        csv = _rw_rows(
            (self.DOI, "1/1/2020 0:00", "Reinstatement", ""),
            (self.DOI, "1/1/2024 0:00", "Reinstatement", ""),
            (self.DOI, "6/1/2025 0:00", "Retraction", "10.1000/notice"),
        )
        result = Retractions(_client(rw_csv=csv), cache_dir=tmp_path).status_for(
            [self.DOI]
        ).notices
        assert result[self.DOI].kind == "retraction"


class TestAnUnreadableDate:
    """Ignorance on either side of the reinstatement comparison.

    A reinstatement withdraws by date, so both halves of that comparison have
    to answer the same way when a date cannot be read: it is not a very old
    date, it is no date. Read as the earliest representable one on the notice's
    side, an undated retraction was withdrawn by any reinstatement in the file
    — the inverse of what the same rule promises on the reinstatement's side.
    """

    DOI = "10.1000/undated"

    def test_a_notice_with_no_date_survives_a_dated_reinstatement(
        self, tmp_path: Path
    ) -> None:
        csv = _rw_rows(
            (self.DOI, "", "Retraction", "10.1000/notice"),
            (self.DOI, "6/1/2021 0:00", "Reinstatement", ""),
        )
        result = Retractions(_client(rw_csv=csv), cache_dir=tmp_path).status_for(
            [self.DOI]
        ).notices
        assert result[self.DOI].kind == "retraction"

    def test_a_dated_notice_before_a_reinstatement_is_still_withdrawn(
        self, tmp_path: Path
    ) -> None:
        """The true-negative half: a retraction that really was reversed stays
        unreported, or the rule above would be a licence to report every one.
        """
        csv = _rw_rows(
            (self.DOI, "1/1/2020 0:00", "Retraction", "10.1000/notice"),
            (self.DOI, "6/1/2021 0:00", "Reinstatement", ""),
        )
        result = Retractions(_client(rw_csv=csv), cache_dir=tmp_path).status_for(
            [self.DOI]
        ).notices
        assert result == {}

    def test_a_reinstatement_removes_a_notice_of_its_own_date(
        self, tmp_path: Path
    ) -> None:
        """"Dated at or before it" includes the same day, which is the shape a
        record corrected on the day it was logged takes.
        """
        csv = _rw_rows(
            (self.DOI, "3/4/2021 0:00", "Retraction", "10.1000/notice"),
            (self.DOI, "3/4/2021 0:00", "Reinstatement", ""),
        )
        result = Retractions(_client(rw_csv=csv), cache_dir=tmp_path).status_for(
            [self.DOI]
        ).notices
        assert result == {}


class TestPubMedEci:
    """``ECI`` on the affected paper, ``ECF`` on the notice -- and only one of
    the two may ever be read as "this DOI has a concern about it".
    """

    def test_a_retracted_publication_is_reported(self, tmp_path: Path) -> None:
        stub = _client(
            pmid_by_doi={WAKEFIELD_DOI: WAKEFIELD_PMID},
            medline_by_pmid={WAKEFIELD_PMID: _pubmed_fixture("retracted")},
        )
        result = Retractions(stub, cache_dir=tmp_path).status_for([WAKEFIELD_DOI]).notices
        notice = result[WAKEFIELD_DOI.lower()]
        assert notice.kind == "retraction"
        assert notice.source == "pubmed"

    def test_a_retraction_notice_is_not_reported(self, tmp_path: Path) -> None:
        """``PT - Retraction Notice`` means *this record is the notice*;
        ``pubmed.py`` already reads ``retracted=False`` for it, and it carries
        no ``ECI`` of its own either.
        """
        stub = _client(
            pmid_by_doi={RETRACTION_NOTICE_DOI: RETRACTION_NOTICE_PMID},
            medline_by_pmid={RETRACTION_NOTICE_PMID: _pubmed_fixture("retraction_notice")},
        )
        result = Retractions(stub, cache_dir=tmp_path).status_for([RETRACTION_NOTICE_DOI]).notices
        assert result == {}

    def test_an_expression_of_concern_via_eci_is_reported(self, tmp_path: Path) -> None:
        """The gap this module closes: PMID 23741377's ``PT`` is only
        ``Journal Article`` / ``Research Support, Non-U.S. Gov't`` -- nothing
        ``pubmed._retraction`` would ever catch -- and the only signal is the
        ``ECI`` cross-reference this module reads instead.
        """
        stub = _client(
            pmid_by_doi={NEIRINCKX_DOI: NEIRINCKX_PMID},
            medline_by_pmid={NEIRINCKX_PMID: _pubmed_fixture("eci_concern")},
        )
        result = Retractions(stub, cache_dir=tmp_path).status_for([NEIRINCKX_DOI]).notices
        notice = result[NEIRINCKX_DOI]
        assert notice.kind == "expression-of-concern"
        assert notice.source == "pubmed"
        assert notice.notice_doi == CONCERN_NOTICE_DOI
        assert notice.date == "2021"

    def test_the_concern_notices_own_record_is_not_reported_as_concerned_about_itself(
        self, tmp_path: Path
    ) -> None:
        """The pairing the test above needs: PMID 34710116 carries ``PT -
        Expression of Concern`` (it *is* the notice) and ``ECF`` — "Expression
        of Concern For:" — pointing back at 23741377, not ``ECI``. Reading
        either as "this record has a concern about itself" would flag the
        notice and, symmetrically with the retraction-direction bug this
        project has already found once, silently under-report the paper the
        notice actually concerns.
        """
        stub = _client(
            pmid_by_doi={CONCERN_NOTICE_DOI: CONCERN_NOTICE_PMID},
            medline_by_pmid={CONCERN_NOTICE_PMID: _pubmed_fixture("eci_concern_notice")},
        )
        result = Retractions(stub, cache_dir=tmp_path).status_for([CONCERN_NOTICE_DOI]).notices
        assert result == {}

    def test_a_clean_paper_has_no_signal(self, tmp_path: Path) -> None:
        stub = _client(
            rw_csv=_rw_sample(),
            pmid_by_doi={CLEAN_DOI: CLEAN_PMID},
            medline_by_pmid={CLEAN_PMID: _pubmed_fixture("wrapped_title")},
        )
        result = Retractions(stub, cache_dir=tmp_path).status_for([CLEAN_DOI]).notices
        assert CLEAN_DOI not in result

    def test_an_eci_that_is_not_medlines_list_is_not_a_concern(self) -> None:
        """``Record.raw`` is free-form, and only MEDLINE's shape may be read.

        ``concern_in`` is public and takes any :class:`Record`, whose ``raw`` is
        typed ``dict[str, Any]`` and filled by whichever registry built it — a
        Crossref record's is that work's own JSON. Indexing a bare string would
        take its first character for a whole citation and report a concern about
        a named paper on the strength of one letter.
        """
        assert concern_in(Record(source="pubmed", raw={"ECI": "PLoS One. 2021 Oct 28"})) is None
        assert concern_in(Record(source="pubmed", raw={"ECI": []})) is None
        assert concern_in(Record(source="pubmed", raw={"ECI": ["PLoS One. 2021 Oct 28"]})) == (
            "expression-of-concern"
        )


class TestSourceCombination:
    """Retraction Watch and PubMed asked about the same DOI at once."""

    def test_agreement_across_both_sources_is_reported_once_with_both_named(
        self, tmp_path: Path
    ) -> None:
        stub = _client(
            rw_csv=_rw_sample(),
            pmid_by_doi={NEIRINCKX_DOI: NEIRINCKX_PMID},
            medline_by_pmid={NEIRINCKX_PMID: _pubmed_fixture("eci_concern")},
        )
        result = Retractions(stub, cache_dir=tmp_path).status_for([NEIRINCKX_DOI]).notices
        notice = result[NEIRINCKX_DOI]
        assert notice.kind == "expression-of-concern"
        assert set(notice.source.split(",")) == {"retraction-watch", "pubmed"}
        # The fuller-resolution date (Retraction Watch's own, day-precision)
        # is kept rather than discarded for PubMed's year-only one.
        assert notice.date == "2021-10-28"

    def test_a_retraction_from_one_source_is_not_softened_by_the_others_silence(
        self, tmp_path: Path
    ) -> None:
        """PubMed has no PMID at all for this DOI (the usual case for most of
        the RW database, which is not biomedical-only); Retraction Watch's
        answer must stand on its own.
        """
        stub = _client(rw_csv=_rw_sample())
        result = Retractions(stub, cache_dir=tmp_path).status_for([WAKEFIELD_DOI]).notices
        assert result[WAKEFIELD_DOI.lower()].kind == "retraction"

    def test_several_dois_at_once_are_each_answered_independently(self, tmp_path: Path) -> None:
        stub = _client(
            rw_csv=_rw_sample(),
            pmid_by_doi={CLEAN_DOI: CLEAN_PMID},
            medline_by_pmid={CLEAN_PMID: _pubmed_fixture("wrapped_title")},
        )
        result = Retractions(stub, cache_dir=tmp_path).status_for([WAKEFIELD_DOI, CLEAN_DOI]).notices
        assert result[WAKEFIELD_DOI.lower()].kind == "retraction"
        assert CLEAN_DOI not in result


class TestSourcesThatDisagree:
    """One DOI, two sources, two different kinds — and which one is reported.

    :func:`~bibaudit.registries.retractions._combine` is the only place that
    choice is made. Both directions are here because the merge must depend on
    the *kind*, not on the order the candidates happen to be built in: the
    Retraction Watch notice is always the first of the two, so a rule keeping
    the first, or the last, agrees with the strongest-wins rule on exactly one
    of these pairs.

    Both pairings are live, and neither is a shape invented to exercise the
    branch: Retraction Watch and NLM curate independently and reach different
    conclusions about the same work all the time. What the code may not do is
    let the milder conclusion take the stronger one away.
    """

    def test_pubmeds_retraction_survives_retraction_watchs_correction(
        self, tmp_path: Path
    ) -> None:
        """Retraction Watch's only row for this DOI is a ``Correction``.

        PMID 36736314 is the erratum for Peng et al., *Molecular Therapy*, and
        it carries ``PT - Published Erratum`` beside ``PT - Retracted
        Publication``: the erratum itself was retracted. Reported as the
        correction RW logged, the report tells a reader the work stands and
        that the corrected version is the one to read the numbers off — about a
        work NLM records as retracted, which is the worst sentence this tool
        can print.
        """
        stub = _client(
            rw_csv=_rw_sample(),
            pmid_by_doi={RW_CORRECTION_PUBMED_RETRACTION_DOI: RW_CORRECTION_PUBMED_RETRACTION_PMID},
            medline_by_pmid={
                RW_CORRECTION_PUBMED_RETRACTION_PMID: _pubmed_fixture("retracted_erratum")
            },
        )
        result = Retractions(stub, cache_dir=tmp_path).status_for(
            [RW_CORRECTION_PUBMED_RETRACTION_DOI]
        ).notices
        assert result[RW_CORRECTION_PUBMED_RETRACTION_DOI].kind == "retraction"

    def test_retraction_watchs_retraction_survives_pubmeds_concern(
        self, tmp_path: Path
    ) -> None:
        """The same disagreement with the sources swapped.

        RW upgraded its 2019 expression of concern about Sato et al. to a
        ``Retraction`` eight months later; NLM's citation still carries the
        ``ECI`` for the concern and no retraction ``PT`` at all. PubMed's
        milder answer must not take RW's retraction away — and this is the
        pairing that fails if the merge keeps whichever notice was built last
        rather than the strongest.
        """
        stub = _client(
            rw_csv=_rw_sample(),
            pmid_by_doi={RW_RETRACTION_PUBMED_CONCERN_DOI: RW_RETRACTION_PUBMED_CONCERN_PMID},
            medline_by_pmid={RW_RETRACTION_PUBMED_CONCERN_PMID: _pubmed_fixture("concern_only")},
        )
        result = Retractions(stub, cache_dir=tmp_path).status_for(
            [RW_RETRACTION_PUBMED_CONCERN_DOI]
        ).notices
        assert result[RW_RETRACTION_PUBMED_CONCERN_DOI].kind == "retraction"

    def test_each_source_keeps_the_kind_it_recorded(self, tmp_path: Path) -> None:
        """The merged kind answers "what is the status of this DOI"; it is not
        what each source said, and a caller that names its sources needs the
        second. Stamped on both, the strongest kind is reported under the name
        of a source whose export records something milder — and the registry
        column is the reader's only route to challenge a finding.
        """
        stub = _client(
            rw_csv=_rw_sample(),
            pmid_by_doi={RW_CORRECTION_PUBMED_RETRACTION_DOI: RW_CORRECTION_PUBMED_RETRACTION_PMID},
            medline_by_pmid={
                RW_CORRECTION_PUBMED_RETRACTION_PMID: _pubmed_fixture("retracted_erratum")
            },
        )
        status = Retractions(stub, cache_dir=tmp_path).status_for(
            [RW_CORRECTION_PUBMED_RETRACTION_DOI]
        )
        answers = status.by_source[RW_CORRECTION_PUBMED_RETRACTION_DOI]
        assert answers["retraction-watch"].kind == "correction"
        assert answers["pubmed"].kind == "retraction"
        assert status.notices[RW_CORRECTION_PUBMED_RETRACTION_DOI].kind == "retraction"

    def test_each_answer_is_keyed_by_the_name_its_own_notice_carries(self) -> None:
        """The key is read off the notice, never restated beside it.

        ``compare._status_issues`` prints whatever ``by_source`` calls a
        source, and every notice already carries its own name. Writing the two
        strings out here as well makes two places that have to be changed
        together, and the failure when they are not is one source appearing
        under two names in a report, which reads as two witnesses.
        """
        notice = RetractionNotice(
            doi=WAKEFIELD_DOI.lower(), kind="retraction", source="retraction-watch-mirror",
            notice_doi=None, date=None,
        )

        status = retractions._reconcile({notice.doi: notice}, {}, frozenset())

        assert list(status.by_source[notice.doi]) == ["retraction-watch-mirror"]

    def test_both_sources_are_named_even_though_only_one_kind_survives(
        self, tmp_path: Path
    ) -> None:
        """The registry column is how a reader challenges the finding.

        Naming only the source whose kind won would leave the reader with no
        way to see that the other source was asked and answered differently —
        and ``compare._status_issues`` prints this string verbatim.
        """
        stub = _client(
            rw_csv=_rw_sample(),
            pmid_by_doi={RW_CORRECTION_PUBMED_RETRACTION_DOI: RW_CORRECTION_PUBMED_RETRACTION_PMID},
            medline_by_pmid={
                RW_CORRECTION_PUBMED_RETRACTION_PMID: _pubmed_fixture("retracted_erratum")
            },
        )
        result = Retractions(stub, cache_dir=tmp_path).status_for(
            [RW_CORRECTION_PUBMED_RETRACTION_DOI]
        ).notices
        assert result[RW_CORRECTION_PUBMED_RETRACTION_DOI].source == "pubmed,retraction-watch"

    def test_the_evidence_the_losing_source_carried_is_not_discarded(
        self, tmp_path: Path
    ) -> None:
        """The kind is the winner's; the notice DOI and the date need not be.

        PubMed's ``PT`` flag carries neither — the notice is not named on the
        retracted citation and the only date on it is the article's own year —
        while the RW row this merge overrules carries both. Taking the whole
        winning notice would answer "2023, notice unknown" for a finding one of
        the two sources dated to the day and pointed at a document.
        """
        stub = _client(
            rw_csv=_rw_sample(),
            pmid_by_doi={RW_CORRECTION_PUBMED_RETRACTION_DOI: RW_CORRECTION_PUBMED_RETRACTION_PMID},
            medline_by_pmid={
                RW_CORRECTION_PUBMED_RETRACTION_PMID: _pubmed_fixture("retracted_erratum")
            },
        )
        notice = Retractions(stub, cache_dir=tmp_path).status_for(
            [RW_CORRECTION_PUBMED_RETRACTION_DOI]
        ).notices[RW_CORRECTION_PUBMED_RETRACTION_DOI]
        assert notice.notice_doi == RW_CORRECTION_NOTICE_DOI
        assert notice.date == "2023-02-03"

    def test_a_kind_this_module_was_never_taught_cannot_take_a_retraction_away(
        self, tmp_path: Path
    ) -> None:
        """An unranked kind sorts last, and the cache is where one gets in.

        ``_index_from_payload`` rebuilds notices from a file its own docstring
        calls stale or hand-edited, with no vocabulary check on ``kind``, so
        the value reaching the merge need not be one this module minted. Sorted
        *first* instead of last it beats every real notice — which is the one
        thing a kind nobody has taught this module must never be able to do.
        """
        first = Retractions(_client(rw_csv=_rw_sample()), cache_dir=tmp_path)
        first.status_for([WAKEFIELD_DOI])
        _rewrite_cached_kind(tmp_path, "a-category-invented-after-this-was-written")

        stub = _client(
            pmid_by_doi={WAKEFIELD_DOI: WAKEFIELD_PMID},
            medline_by_pmid={WAKEFIELD_PMID: _pubmed_fixture("retracted")},
        )
        result = Retractions(stub, cache_dir=tmp_path).status_for([WAKEFIELD_DOI]).notices
        assert result[WAKEFIELD_DOI.lower()].kind == "retraction"

    def test_a_kind_this_module_was_never_taught_is_still_reported_alone(
        self, tmp_path: Path
    ) -> None:
        """Sorting it last is not dropping it.

        With no second source to outrank it, the unfamiliar kind is the only
        notice this DOI has, and a notice this module cannot classify is still
        a source saying something happened to the work.
        """
        first = Retractions(_client(rw_csv=_rw_sample()), cache_dir=tmp_path)
        first.status_for([WAKEFIELD_DOI])
        _rewrite_cached_kind(tmp_path, "a-category-invented-after-this-was-written")

        result = Retractions(_client(), cache_dir=tmp_path).status_for([WAKEFIELD_DOI]).notices
        assert result[WAKEFIELD_DOI.lower()].kind == (
            "a-category-invented-after-this-was-written"
        )


class TestACacheFileNobodyCanRead:
    """``_index_from_payload`` reads back a file its docstring calls hand-edited.

    That is the contract :class:`~bibaudit.registries.http.Cache` itself keeps:
    anything that does not parse back is *dropped* and asked for again, never
    raised over. A cache file is on disk for the whole seven-day TTL, is not a
    registry response, and is the one input to this module a person can edit —
    so a `KeyError` or an `AttributeError` out of it would abort a run over a
    file the tool wrote itself, on the source whose whole job is to keep
    answering when the others cannot.

    Every shape here is a cache file, not an invented registry answer, and each
    is written by the real cache first and then damaged, so a change to the
    envelope's layout fails these tests rather than leaving them editing
    something nothing reads.
    """

    def _damage(self, cache_dir: Path, mutate: object) -> dict[str, object]:
        first = Retractions(_client(rw_csv=_rw_sample()), cache_dir=cache_dir)
        first.status_for([WAKEFIELD_DOI])
        [path] = list(cache_dir.rglob("*.json"))
        envelope = json.loads(path.read_text(encoding="utf-8"))
        mutate(envelope["payload"])  # type: ignore[operator]
        path.write_text(json.dumps(envelope), encoding="utf-8")
        payload: dict[str, object] = envelope["payload"]
        return payload

    def test_a_notices_table_that_is_not_a_table_yields_no_notices(
        self, tmp_path: Path
    ) -> None:
        """The outer half: ``notices`` holding a list, a string or nothing."""
        self._damage(tmp_path, lambda payload: payload.update(notices=["not", "a", "table"]))

        status = Retractions(_client(), cache_dir=tmp_path).status_for([WAKEFIELD_DOI])

        assert status.notices == {}
        assert not status.unreachable

    def test_one_unreadable_entry_does_not_take_the_others_with_it(
        self, tmp_path: Path
    ) -> None:
        """A row that is not a table at all is dropped and the rest kept."""

        def mutate(payload: dict[str, object]) -> None:
            notices = payload["notices"]
            assert isinstance(notices, dict)
            notices["10.1234/not-a-table"] = "a string where a record belongs"

        self._damage(tmp_path, mutate)

        status = Retractions(_client(), cache_dir=tmp_path).status_for([WAKEFIELD_DOI])

        assert "10.1234/not-a-table" not in status.notices
        assert status.notices[WAKEFIELD_DOI.lower()].kind == "retraction"

    def test_an_entry_missing_a_field_the_record_requires_is_dropped(
        self, tmp_path: Path
    ) -> None:
        """``kind`` gone: the field the merge ranks on, so half a record is none."""

        def mutate(payload: dict[str, object]) -> None:
            notices = payload["notices"]
            assert isinstance(notices, dict)
            for notice in notices.values():
                notice.pop("kind")

        self._damage(tmp_path, mutate)

        status = Retractions(_client(), cache_dir=tmp_path).status_for([WAKEFIELD_DOI])

        assert status.notices == {}

    def test_the_unranked_counts_are_read_back_with_the_same_tolerance(
        self, tmp_path: Path
    ) -> None:
        """They outlive the process that made them, so they are read back too.

        A count that is not a count is dropped rather than warned about: the
        warning names how many rows an export withheld, and a number nobody can
        read is not one.
        """
        self._damage(tmp_path, lambda payload: payload.update(unranked="not a table"))

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            status = Retractions(_client(), cache_dir=tmp_path).status_for([WAKEFIELD_DOI])

        assert status.notices[WAKEFIELD_DOI.lower()].kind == "retraction"


class TestOutageHandling:
    """Per the brief: raise Transient for that source only and let the others answer."""

    def test_a_retraction_watch_outage_still_lets_pubmed_answer(self, tmp_path: Path) -> None:
        stub = _client(
            rw_transient=True,
            pmid_by_doi={WAKEFIELD_DOI: WAKEFIELD_PMID},
            medline_by_pmid={WAKEFIELD_PMID: _pubmed_fixture("retracted")},
        )
        with pytest.warns(RuntimeWarning, match="Retraction Watch"):
            status = Retractions(stub, cache_dir=tmp_path).status_for([WAKEFIELD_DOI])
        notice = status.notices[WAKEFIELD_DOI.lower()]
        assert notice.kind == "retraction"
        assert notice.source == "pubmed"

    def test_a_retraction_watch_outage_is_named_in_the_returned_status(
        self, tmp_path: Path
    ) -> None:
        """Degrading to PubMed alone is only acceptable if the caller is told.

        Without it ``audit.py`` has nothing to add to its *unreachable* set,
        ``compare`` cannot raise ``retraction-unverified``, and a run whose
        cached export has aged past the seven-day TTL prints a green ``PASS``
        over a source nobody reached -- while ``consulted`` reports
        ``retraction-watch: answered``, because ``_asked_registries`` puts it in
        ``asked`` unconditionally. That is the clean bill of health from
        ignorance CLAUDE.md forbids.
        """
        stub = _client(rw_transient=True)
        with pytest.warns(RuntimeWarning, match="Retraction Watch"):
            status = Retractions(stub, cache_dir=tmp_path).status_for([WAKEFIELD_DOI])
        assert status.unreachable == frozenset({"retraction-watch"})

    def test_a_reachable_retraction_watch_reports_nothing_unreachable(
        self, tmp_path: Path
    ) -> None:
        """The true-negative half: a source that answered must never be named
        as unreachable, or every ordinary run grows a retraction caveat and the
        caveat stops meaning anything.
        """
        stub = _client(rw_csv=_rw_sample())
        status = Retractions(stub, cache_dir=tmp_path).status_for([WAKEFIELD_DOI])
        assert status.unreachable == frozenset()

    def test_an_empty_request_reports_no_outage(self, tmp_path: Path) -> None:
        """Nothing was asked, so nothing went unanswered. An empty DOI list
        short-circuits before either source is touched (see
        ``test_no_dois_touches_neither_source``); it must not report an outage
        it never attempted.
        """
        status = Retractions(_client(), cache_dir=tmp_path).status_for([])
        assert status.unreachable == frozenset()

    def test_a_pubmed_outage_is_not_swallowed(self, tmp_path: Path) -> None:
        """Unlike the Retraction Watch bulk file, a PubMed outage is a real
        outage of a registry this project relies on for corroboration and
        must propagate exactly as :meth:`PubMed.by_dois` already raises it --
        silencing it here would be the one failure this module exists to
        prevent: a registry that could have said "retracted" going unheard
        and reading as a clean citation.
        """
        stub = _client(rw_csv=_rw_sample(), pubmed_transient=True)
        with pytest.raises(Transient):
            Retractions(stub, cache_dir=tmp_path).status_for([WAKEFIELD_DOI])

    def test_a_pubmed_outage_does_not_delete_what_retraction_watch_already_said(
        self, tmp_path: Path
    ) -> None:
        """The raise above must not take the other source's answer with it.

        Retraction Watch has already been read by the time PubMed's leg fails,
        and its notices are in the frame that raises. Let them go and a caller
        catching the outage has nothing left to state a retraction from, so a
        work Retraction Watch records as retracted reports as carrying no
        notice at all — the finding is not lost to ignorance, it is deleted and
        then contradicted.
        """
        stub = _client(rw_csv=_rw_sample(), pubmed_transient=True)
        with pytest.raises(RetractionOutage) as caught:
            Retractions(stub, cache_dir=tmp_path).status_for([WAKEFIELD_DOI])

        status = caught.value.status
        assert status.notices[WAKEFIELD_DOI.lower()].kind == "retraction"
        assert status.notices[WAKEFIELD_DOI.lower()].source == "retraction-watch"
        assert status.by_source[WAKEFIELD_DOI.lower()]["retraction-watch"].kind == "retraction"
        assert caught.value.unreachable == frozenset({"pubmed"})
        assert status.unreachable == frozenset({"pubmed"})

    def test_both_sources_down_names_both_and_carries_no_finding(
        self, tmp_path: Path
    ) -> None:
        """The other half: nothing survives an outage that took both sources.

        Retraction Watch's own failure is absorbed on the way in, so it is only
        the union on the raise that keeps its name from being dropped by
        PubMed's — and an outage that recovered notices from a source that
        never answered would be fabricating them.
        """
        stub = _client(rw_transient=True, pubmed_transient=True)
        with (
            pytest.warns(RuntimeWarning, match="Retraction Watch"),
            pytest.raises(RetractionOutage) as caught,
        ):
            Retractions(stub, cache_dir=tmp_path).status_for([WAKEFIELD_DOI])

        assert caught.value.unreachable == frozenset({"pubmed", "retraction-watch"})
        assert caught.value.status.notices == {}

    def test_a_retraction_watch_outage_with_no_pubmed_hit_leaves_the_doi_absent(
        self, tmp_path: Path
    ) -> None:
        """Not a false "clean": see the module docstring for what silence
        from :meth:`Retractions.status_for` may and may not be read as. This
        test only pins down that a degraded call does not crash and does not
        fabricate a finding it has no evidence for.
        """
        stub = _client(rw_transient=True)
        with pytest.warns(RuntimeWarning):
            status = Retractions(stub, cache_dir=tmp_path).status_for([WAKEFIELD_DOI])
        assert status.notices == {}
        # ...and the absence is explicitly qualified rather than left to read as
        # a clean answer, which is the whole difference this class encodes.
        assert status.unreachable == frozenset({"retraction-watch"})


class TestAnExportThatCarriesNothing:
    """A whole-database fetch that yields no row is ignorance, not an answer.

    Every other confirmed absence in this project is a fact about one work:
    ``/works/10.x/y`` answering 404 settles that this registry does not hold
    that DOI. This request asks for the entire database, so the same 404 is a
    fact about the endpoint — one that has already moved once under this
    file — and read as "nothing found" it turns the only source that exists
    to carry this signal into a source that answered and had nothing.
    """

    def test_a_404_on_the_export_is_an_outage_not_an_empty_database(
        self, tmp_path: Path
    ) -> None:
        stub = _client(rw_csv=None)
        with pytest.warns(RuntimeWarning, match="Retraction Watch"):
            status = Retractions(stub, cache_dir=tmp_path).status_for([WAKEFIELD_DOI])
        assert status.unreachable == frozenset({"retraction-watch"})

    def test_a_body_that_is_not_the_export_is_an_outage_too(self, tmp_path: Path) -> None:
        """A maintenance page or a rate-limit notice served with a 200 parses
        to zero usable rows and is indistinguishable from the 404 by the time
        it reaches the index. Both are the endpoint failing to answer.
        """
        stub = _client(rw_csv="<html><body>Service unavailable</body></html>")
        with pytest.warns(RuntimeWarning, match="Retraction Watch"):
            status = Retractions(stub, cache_dir=tmp_path).status_for([WAKEFIELD_DOI])
        assert status.unreachable == frozenset({"retraction-watch"})

    def test_the_header_row_alone_is_an_outage(self, tmp_path: Path) -> None:
        """A truncated download that got the header and no rows. ``DictReader``
        reads it without complaint and yields nothing, which is the same
        emptiness by a third route.
        """
        header = RW_EXPORT_WITHOUT_THIS_DOI.splitlines()[0] + "\n"
        stub = _client(rw_csv=header)
        with pytest.warns(RuntimeWarning, match="Retraction Watch"):
            status = Retractions(stub, cache_dir=tmp_path).status_for([WAKEFIELD_DOI])
        assert status.unreachable == frozenset({"retraction-watch"})

    def test_pubmeds_own_answer_survives_an_unreadable_export(self, tmp_path: Path) -> None:
        """Degrade, never fail: the other source's finding still comes back,
        exactly as it does for a network outage.
        """
        stub = _client(
            rw_csv=None,
            pmid_by_doi={WAKEFIELD_DOI: WAKEFIELD_PMID},
            medline_by_pmid={WAKEFIELD_PMID: _pubmed_fixture("retracted")},
        )
        with pytest.warns(RuntimeWarning, match="Retraction Watch"):
            status = Retractions(stub, cache_dir=tmp_path).status_for([WAKEFIELD_DOI])
        assert status.notices[WAKEFIELD_DOI.lower()].kind == "retraction"

    def test_the_emptiness_is_not_cached_over_the_next_seven_days(
        self, tmp_path: Path
    ) -> None:
        """The cost of getting this wrong is not one run. This index has a
        seven-day TTL and ``--refresh`` does not reach it, so an empty index
        written to disk answers every healthy run for a week.
        """
        with pytest.warns(RuntimeWarning, match="Retraction Watch"):
            Retractions(_client(rw_csv=None), cache_dir=tmp_path).status_for([WAKEFIELD_DOI])
        assert list(tmp_path.rglob("*.json")) == []

        later = Retractions(_client(rw_csv=_rw_sample()), cache_dir=tmp_path)
        status = later.status_for([WAKEFIELD_DOI])
        assert status.notices[WAKEFIELD_DOI.lower()].kind == "retraction"
        assert status.unreachable == frozenset()

    def test_an_export_whose_only_notice_was_withdrawn_is_an_answer(
        self, tmp_path: Path
    ) -> None:
        """It is the rows read that say whether the export was downloaded, not
        the index built from them. An index can be legitimately empty — every
        notice in it reversed by a reinstatement — and calling that an outage
        would report a retraction gap on a file that answered in full.
        """
        csv = _rw_rows(
            ("10.1000/reversed", "1/1/2020 0:00", "Retraction", "10.1000/notice"),
            ("10.1000/reversed", "6/1/2021 0:00", "Reinstatement", ""),
        )
        status = Retractions(_client(rw_csv=csv), cache_dir=tmp_path).status_for(
            ["10.1000/reversed"]
        )
        assert status.notices == {}
        assert status.unreachable == frozenset()

    def test_an_export_with_one_row_is_an_answer(self, tmp_path: Path) -> None:
        """The true-negative half. An export that parsed is an answer even
        about a DOI it does not mention, or every ordinary run would report a
        retraction gap and the gap would stop meaning anything.
        """
        stub = _client(rw_csv=RW_EXPORT_WITHOUT_THIS_DOI)
        status = Retractions(stub, cache_dir=tmp_path).status_for([WAKEFIELD_DOI])
        assert status.notices == {}
        assert status.unreachable == frozenset()


class TestCaching:
    """The Retraction Watch CSV must not be refetched more than its 7-day TTL
    requires -- it is a ~66 MB download, and every DOI in a whole bibliography
    shares the one fetch.
    """

    def test_the_index_is_fetched_once_per_process_for_many_calls(self, tmp_path: Path) -> None:
        stub = _client(rw_csv=_rw_sample())
        retractions = Retractions(stub, cache_dir=tmp_path)
        retractions.status_for([WAKEFIELD_DOI])
        retractions.status_for([NEIRINCKX_DOI])
        retractions.status_for(["10.1000/still-nothing"])
        assert stub.rw_fetch_count == 1

    def test_the_export_is_fetched_past_the_shared_registry_cache(
        self, tmp_path: Path
    ) -> None:
        """Seven days is a bound only if nothing else keeps the same rows.

        The export went through the shared registry cache like any other
        request, and that cache holds a body for ``--cache-ttl`` days, 90 by
        default. So the index expiring bought nothing: the refetch was answered
        out of the longer-lived copy of the same body — same rows, no request
        made — and a retraction Retraction Watch logged up to 90 days ago read
        clean, on the one source that exists to catch what Crossref and NLM do
        not.
        """
        shared = Cache(tmp_path / "shared", ttl_days=90)
        index_dir = tmp_path / "index"
        old = _rw_rows(("10.1000/old", "1/1/2020 0:00", "Retraction", "10.1000/notice"))
        fresh = _rw_rows(
            ("10.1000/old", "1/1/2020 0:00", "Retraction", "10.1000/notice"),
            ("10.1000/fresh", "6/1/2026 0:00", "Retraction", "10.1000/notice2"),
        )

        first = _CountingClient(shared, old)
        Retractions(first, cache_dir=index_dir).status_for(["10.1000/old"])
        assert first.export_fetches == 1
        # A 66 MB body nothing downstream ever wants unparsed again, held for
        # 90 days beside the index it was parsed into. PubMed's own lookups
        # belong in there and are left alone, so the assertion names the one
        # URL that must not be.
        cached = _cached_urls(shared.path)
        assert [url for url in cached if "retractionwatch" in url] == []
        assert any("eutils" in url for url in cached)

        # Seven days pass and the index expires; Retraction Watch has logged a
        # new retraction meanwhile.
        shutil.rmtree(index_dir)
        second = _CountingClient(shared, fresh)
        status = Retractions(second, cache_dir=index_dir).status_for(
            ["10.1000/old", "10.1000/fresh"]
        )

        assert second.export_fetches == 1
        assert status.notices["10.1000/fresh"].kind == "retraction"

    def test_a_second_instance_replays_the_on_disk_cache_without_a_second_fetch(
        self, tmp_path: Path
    ) -> None:
        """The scenario the 7-day TTL exists for: a later run (a fresh
        process, a fresh ``Retractions`` instance) within the cache's
        lifetime must not re-download the file. Proven here by making the
        *second* instance's own fetch fail outright (``rw_transient=True``):
        if the cache were not being read, this test would see the outage
        warning and an empty result instead of the real finding.
        """
        first = Retractions(_client(rw_csv=_rw_sample()), cache_dir=tmp_path)
        first.status_for([WAKEFIELD_DOI])

        second_stub = _client(rw_transient=True)
        second = Retractions(second_stub, cache_dir=tmp_path)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            result = second.status_for([WAKEFIELD_DOI]).notices
        assert result[WAKEFIELD_DOI.lower()].kind == "retraction"
        assert second_stub.rw_fetch_count == 0
