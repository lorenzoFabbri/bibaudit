"""``registries.retractions``: Retraction Watch's own data, and PubMed's ``ECI``.

Offline only. :class:`_StubClient` stands in for
:class:`~bibaudit.registries.http.Client`, so nothing here reaches the
network — including the Retraction Watch fetch, which a naive test would
otherwise turn into a ~66 MB download on every run.

Two groups of fixtures matter more than the rest, and both are real, recorded
data rather than invented for the test:

* ``tests/data/retraction_watch_sample.csv`` is a small extract of the live
  export (fetched 2026-08-01) covering the Wakefield paper's *two* RW
  entries — a 2004 correction and a later 2010 retraction — which is what
  proves :func:`~bibaudit.registries.retractions._parse_rw_csv` picks the
  more recent row rather than the first or last one in the file.
* ``tests/data/pubmed_eci_concern*.txt`` are MEDLINE ``efetch`` output for
  PMID 23741377 (the affected paper) and PMID 34710116 (the notice), fetched
  live. The pair is what proves ``ECI`` ("Expression of Concern In:") is read
  on the affected paper and its mirror-image ``ECF`` ("Expression of Concern
  For:") on the notice is not — the identical direction discipline
  ``registries.pubmed`` already applies to ``PT``.
"""

from __future__ import annotations

import urllib.parse
import warnings
from pathlib import Path
from typing import Any

import pytest

from bibaudit.normalize import normalize_doi
from bibaudit.registries.http import Transient
from bibaudit.registries.retractions import Retractions

DATA = Path(__file__).parent / "data"

WAKEFIELD_DOI = "10.1016/S0140-6736(97)11096-0"
WAKEFIELD_PMID = "9500320"
RETRACTION_NOTICE_DOI = "10.1016/S0140-6736(10)60175-4"
RETRACTION_NOTICE_PMID = "20137807"
NEIRINCKX_DOI = "10.1371/journal.pone.0064723"
NEIRINCKX_PMID = "23741377"
CONCERN_NOTICE_DOI = "10.1371/journal.pone.0256488"
CONCERN_NOTICE_PMID = "34710116"
CLEAN_DOI = "10.1093/aje/kwx137"
CLEAN_PMID = "28338828"


def _pubmed_fixture(name: str) -> str:
    return (DATA / f"pubmed_{name}.txt").read_text(encoding="utf-8")


def _rw_sample() -> str:
    return (DATA / "retraction_watch_sample.csv").read_text(encoding="utf-8")


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
        rw_csv: str | None = "",
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
        self.urls: list[str] = []

    def get_text(
        self, url: str, *, cache_key: str | None = None, headers: dict[str, str] | None = None
    ) -> str | None:
        self.urls.append(url)
        if "retractionwatch" in url:
            self.rw_fetch_count += 1
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
        result = Retractions(stub, cache_dir=tmp_path).status_for([])
        assert result == {}
        assert stub.urls == []

    def test_a_doi_neither_source_has_ever_heard_of_is_simply_absent(self, tmp_path: Path) -> None:
        stub = _client(rw_csv=_rw_sample())
        result = Retractions(stub, cache_dir=tmp_path).status_for(["10.1000/nothing-to-see-here"])
        assert result == {}


class TestRetractionWatchCsv:
    """:func:`~bibaudit.registries.retractions._parse_rw_csv`, exercised through
    :meth:`Retractions.status_for` end to end rather than called directly --
    a private helper under test would keep passing if ``status_for`` stopped
    wiring it in at all.
    """

    def test_a_retraction_is_reported(self, tmp_path: Path) -> None:
        stub = _client(rw_csv=_rw_sample())
        result = Retractions(stub, cache_dir=tmp_path).status_for([WAKEFIELD_DOI])
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
        result = Retractions(stub, cache_dir=tmp_path).status_for([WAKEFIELD_DOI])
        assert result[WAKEFIELD_DOI.lower()].kind != "correction"

    def test_a_standalone_correction_is_reported_as_a_correction(self, tmp_path: Path) -> None:
        doi = "10.3390/nano14090769"
        stub = _client(rw_csv=_rw_sample())
        result = Retractions(stub, cache_dir=tmp_path).status_for([doi])
        notice = result[doi]
        assert notice.kind == "correction"
        assert notice.notice_doi == "10.3390/nano15181429"

    def test_an_expression_of_concern_is_reported(self, tmp_path: Path) -> None:
        stub = _client(rw_csv=_rw_sample())
        result = Retractions(stub, cache_dir=tmp_path).status_for([NEIRINCKX_DOI])
        notice = result[NEIRINCKX_DOI]
        assert notice.kind == "expression-of-concern"
        assert notice.notice_doi == CONCERN_NOTICE_DOI
        assert notice.date == "2021-10-28"

    def test_a_reinstated_retraction_is_not_reported(self, tmp_path: Path) -> None:
        """RW's sole row for this DOI is a ``Reinstatement`` -- the retraction
        was reversed, and reporting it anyway is the false alarm CLAUDE.md's
        third rule forbids.
        """
        doi = "10.1016/j.heliyon.2023.e18637"
        stub = _client(rw_csv=_rw_sample())
        result = Retractions(stub, cache_dir=tmp_path).status_for([doi])
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
        result = Retractions(stub, cache_dir=tmp_path).status_for([doi])
        assert doi not in result

    def test_a_blank_original_paper_doi_is_skipped(self, tmp_path: Path) -> None:
        """The row exists (RetractionDOI 10.9999/blank-orig-notice) but names no
        original paper; there is no DOI to index it under.
        """
        stub = _client(rw_csv=_rw_sample())
        result = Retractions(stub, cache_dir=tmp_path).status_for(
            ["10.9999/blank-orig-notice"]
        )
        assert result == {}

    def test_the_unavailable_sentinel_is_not_treated_as_a_doi(self, tmp_path: Path) -> None:
        """RW records ``Unavailable`` literally in ``OriginalPaperDOI`` for rows
        with no known original-paper DOI (3,419 of 71,496 rows in the live
        2026-08-01 export) -- it must never be indexed as though it were one.
        """
        stub = _client(rw_csv=_rw_sample())
        index_probe = Retractions(stub, cache_dir=tmp_path).status_for(
            ["10.9999/unavailable-orig-notice"]
        )
        assert index_probe == {}

    def test_an_unrecognised_retraction_nature_is_skipped_not_guessed(
        self, tmp_path: Path
    ) -> None:
        """A future RW category this module has never been taught about is
        left out rather than reported as a guessed "retraction" -- the
        conservative side of CLAUDE.md's third rule.
        """
        doi = "10.9999/unknown-nature-paper"
        stub = _client(rw_csv=_rw_sample())
        result = Retractions(stub, cache_dir=tmp_path).status_for([doi])
        assert doi not in result

    def test_a_blank_nature_defaults_to_retraction(self, tmp_path: Path) -> None:
        """190 of 71,496 live rows carry no ``RetractionNature`` tag at all; the
        whole database's subject is retractions, so an untagged row is read
        as one rather than silently dropped.
        """
        doi = "10.9999/blank-nature-paper"
        stub = _client(rw_csv=_rw_sample())
        result = Retractions(stub, cache_dir=tmp_path).status_for([doi])
        assert result[doi].kind == "retraction"
        assert result[doi].notice_doi == "10.9999/blank-nature-notice"


class TestPubMedEci:
    """``ECI`` on the affected paper, ``ECF`` on the notice -- and only one of
    the two may ever be read as "this DOI has a concern about it".
    """

    def test_a_retracted_publication_is_reported(self, tmp_path: Path) -> None:
        stub = _client(
            pmid_by_doi={WAKEFIELD_DOI: WAKEFIELD_PMID},
            medline_by_pmid={WAKEFIELD_PMID: _pubmed_fixture("retracted")},
        )
        result = Retractions(stub, cache_dir=tmp_path).status_for([WAKEFIELD_DOI])
        notice = result[WAKEFIELD_DOI.lower()]
        assert notice.kind == "retraction"
        assert notice.source == "pubmed"

    def test_a_retraction_notice_is_not_reported(self, tmp_path: Path) -> None:
        """``PT - Retraction of Publication`` means *this record is the
        notice*; ``pubmed.py`` already reads ``retracted=False`` for it, and
        it carries no ``ECI`` of its own either.
        """
        stub = _client(
            pmid_by_doi={RETRACTION_NOTICE_DOI: RETRACTION_NOTICE_PMID},
            medline_by_pmid={RETRACTION_NOTICE_PMID: _pubmed_fixture("retraction_notice")},
        )
        result = Retractions(stub, cache_dir=tmp_path).status_for([RETRACTION_NOTICE_DOI])
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
        result = Retractions(stub, cache_dir=tmp_path).status_for([NEIRINCKX_DOI])
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
        result = Retractions(stub, cache_dir=tmp_path).status_for([CONCERN_NOTICE_DOI])
        assert result == {}

    def test_a_clean_paper_has_no_signal(self, tmp_path: Path) -> None:
        stub = _client(
            rw_csv=_rw_sample(),
            pmid_by_doi={CLEAN_DOI: CLEAN_PMID},
            medline_by_pmid={CLEAN_PMID: _pubmed_fixture("wrapped_title")},
        )
        result = Retractions(stub, cache_dir=tmp_path).status_for([CLEAN_DOI])
        assert CLEAN_DOI not in result


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
        result = Retractions(stub, cache_dir=tmp_path).status_for([NEIRINCKX_DOI])
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
        result = Retractions(stub, cache_dir=tmp_path).status_for([WAKEFIELD_DOI])
        assert result[WAKEFIELD_DOI.lower()].kind == "retraction"

    def test_several_dois_at_once_are_each_answered_independently(self, tmp_path: Path) -> None:
        stub = _client(
            rw_csv=_rw_sample(),
            pmid_by_doi={CLEAN_DOI: CLEAN_PMID},
            medline_by_pmid={CLEAN_PMID: _pubmed_fixture("wrapped_title")},
        )
        result = Retractions(stub, cache_dir=tmp_path).status_for([WAKEFIELD_DOI, CLEAN_DOI])
        assert result[WAKEFIELD_DOI.lower()].kind == "retraction"
        assert CLEAN_DOI not in result


class TestOutageHandling:
    """Per the brief: raise Transient for that source only and let the others answer."""

    def test_a_retraction_watch_outage_still_lets_pubmed_answer(self, tmp_path: Path) -> None:
        stub = _client(
            rw_transient=True,
            pmid_by_doi={WAKEFIELD_DOI: WAKEFIELD_PMID},
            medline_by_pmid={WAKEFIELD_PMID: _pubmed_fixture("retracted")},
        )
        with pytest.warns(RuntimeWarning, match="Retraction Watch"):
            result = Retractions(stub, cache_dir=tmp_path).status_for([WAKEFIELD_DOI])
        notice = result[WAKEFIELD_DOI.lower()]
        assert notice.kind == "retraction"
        assert notice.source == "pubmed"

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
            result = Retractions(stub, cache_dir=tmp_path).status_for([WAKEFIELD_DOI])
        assert result == {}


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
            result = second.status_for([WAKEFIELD_DOI])
        assert result[WAKEFIELD_DOI.lower()].kind == "retraction"
        assert second_stub.rw_fetch_count == 0
