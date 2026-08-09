"""Known registry defects, and the true positives that look like them.

``CLAUDE.md`` asks for two tests per rule in :mod:`bibaudit.benign`: one proving
the documented false positive is suppressed as ``REGISTRY-ARTIFACT``, and one
proving that a true positive which superficially resembles it still fires. The
second is the one that earns its keep — a suppression with no counter-test is
indistinguishable from a check somebody switched off, and this module is the
only thing standing between the user and a report full of false alarms.

Every case is taken from ``docs/registry-artifacts.md`` or from the private
438-entry corpus described in ``tests/test_audit_corpus.py``. Several strings
below are *damaged on purpose*; the comments say which, because they are the
point of the test and cleaning them up would delete it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bibaudit import benign, names
from bibaudit.compare import compare
from bibaudit.model import Name, Record, Reference, Result
from bibaudit.names import parse_name_list
from bibaudit.normalize import clean
from bibaudit.registries import pubmed as pubmed_client

TITLE = "Shift work and colorectal cancer risk in the MCC-Spain case-control study"

#: The prose that has to name every rule in :mod:`bibaudit.benign`. Located from
#: this file rather than the working directory so the test does not depend on
#: where pytest was invoked from.
ARTIFACT_DOCS = Path(__file__).resolve().parents[1] / "docs" / "registry-artifacts.md"


def _documented(reason: str) -> str:
    """The form a reason is written in ``docs/registry-artifacts.md``.

    One reason is a prefix that goes on to name the organisations it found, so
    the trailing separator is not part of what gets documented.
    """
    return reason.rstrip().rstrip(":").rstrip()


#: The note ``benign.py`` puts on a rule that describes a shape nothing was
#: found doing. It is the one claim in a suppression's write-up a reader cannot
#: check for themselves, so it has to appear where they are reading.
_UNWITNESSED = "NO WITNESSED INSTANCE"

_DATA = Path(__file__).parent / "data"


def crossref_authors(case: str) -> list[Name]:
    """Creators from a verbatim Crossref response recorded in ``tests/data``.

    Recording the response is what makes a suppression auditable: the mojibake
    and the missing creator below are the registry's own bytes, fetched from
    ``api.crossref.org`` and kept, not a plausible reconstruction of them.
    Reading them from disk keeps the test offline, which is the only kind of
    test that actually runs.
    """
    with (_DATA / f"names_crossref_{case}.json").open(encoding="utf-8") as handle:
        work = json.load(handle)["message"]
    return [
        Name(family=clean(person.get("family", "")), given=clean(person.get("given", "")))
        for person in work["author"]
    ]


def medline_journal(fixture: str) -> tuple[str, list[str]]:
    """``JT`` and ``TA`` as ``registries.pubmed`` reads them from a saved ``efetch`` body.

    Parsing NCBI's own bytes rather than retyping the two lines is what keeps a
    container suppression auditable: the qualifier, its inner punctuation and
    its spacing are the whole of what these rules turn on, and a transcription
    of them proves only that the test agrees with itself.
    """
    text = (_DATA / fixture).read_text(encoding="utf-8")
    [fields] = pubmed_client._parse_medline_records(text)
    record = pubmed_client._record_from_medline(fields)
    return record.container, list(record.container_alternates)


def make_ref(**overrides: object) -> Reference:
    """A correct entry from the real corpus, which tests then damage one field at a time.

    Every value is the one the corpus and Crossref actually hold for
    ``papantoniou2017colorectal`` / 10.5271/sjweh.3626, so a reader who doubts a
    suppression below can fetch that DOI and check. It previously carried
    10.1093/aje/kwx137, which is a real DOI belonging to a *different* paper
    (Kim et al., alcohol and breast cancer, Am J Epidemiol) with a clean,
    unaccented author list — an auditor following it would have concluded the
    mojibake rule had no instance behind it at all.

    The title is written with an ASCII hyphen where the registry has an en dash;
    both sides of the comparison use this constant, so the difference is not
    what any test here is about.
    """
    base: dict[str, object] = {
        "key": "papantoniou2017colorectal",
        "locator": "references.bib:1",
        "kind": "article",
        "doi": "10.5271/sjweh.3626",
        "title": TITLE,
        "authors": [
            Name(family="Papantoniou", given="Kyriaki"),
            Name(family="Aragonés", given="Nuria"),
            Name(family="Pérez-Gómez", given="Beatriz"),
        ],
        "year": 2017,
        "container": "Scandinavian Journal of Work, Environment & Health",
        "volume": "43",
        "issue": "3",
        "pages": "250-259",
    }
    base.update(overrides)
    return Reference(**base)  # type: ignore[arg-type]


def make_record(**overrides: object) -> Record:
    base: dict[str, object] = {
        "source": "crossref",
        "doi": "10.5271/sjweh.3626",
        "title": TITLE,
        "authors": [
            Name(family="Papantoniou", given="Kyriaki"),
            Name(family="Aragonés", given="Nuria"),
            Name(family="Pérez-Gómez", given="Beatriz"),
        ],
        "years": {"print": 2017},
        "container": "Scandinavian Journal of Work, Environment & Health",
        "volume": "43",
        "issue": "3",
        "pages": "250-259",
        "kind": "journal-article",
    }
    base.update(overrides)
    return Record(**base)  # type: ignore[arg-type]


def classify(field: str, stored: object, registry: object, **record_kwargs: object) -> str | None:
    """Call the rule set directly, with a record built from *record_kwargs*."""
    return benign.classify(
        field, stored, registry, make_ref(), make_record(**record_kwargs)
    )


def artifacts(result: Result, field: str) -> list[str]:
    """Reasons recorded for *field* as registry artifacts."""
    return [i.note for i in result.suppressed if i.field == field]


def errors(result: Result, field: str) -> list[str]:
    return [i.kind for i in result.issues if i.field == field and i.severity == "error"]


class TestShortenedRegistryTitle:
    def test_a_truncated_registry_title_is_a_known_artifact(self) -> None:
        """Rubin 1986 is registered with the bare title "Comment"."""
        stored = "Comment on 'Statistics and Causal Inference'"
        assert classify("title", stored, "Comment") == "registry stores a shortened title"

    def test_a_truncated_registry_title_does_not_fail_the_build(self) -> None:
        result = compare(
            make_ref(title="Comment on 'Statistics and Causal Inference'"),
            {"crossref": make_record(title="Comment")},
        )
        assert result.verdict == "REGISTRY-ARTIFACT"
        assert not result.fails
        assert artifacts(result, "title")

    def test_an_entry_missing_its_own_subtitle_still_fires(self) -> None:
        """The reverse direction is a real incompleteness the user may want to fix.

        Accepting it too would mean any entry whose title is a fragment of the
        registered one passes, which is most of a badly abbreviated bibliography.
        """
        full = "Comment on 'Statistics and Causal Inference'"
        assert classify("title", "Comment", full) is None
        result = compare(make_ref(title="Comment"), {"crossref": make_record(title=full)})
        assert not artifacts(result, "title")
        assert result.fails

    def test_an_unrelated_registry_title_is_not_explained_away(self) -> None:
        assert classify("title", TITLE, "An entirely unrelated paper about marine biology") is None

    @pytest.mark.parametrize(
        "stored",
        [
            f"Corrigendum to '{TITLE}' [Scand J Work Environ Health 43(3) 250-259]",
            f"Comment on '{TITLE}': the exposure assessment is not credible",
            f"Reply to Kogevinas et al: {TITLE}",
            f"Erratum: {TITLE}",
        ],
    )
    def test_an_entry_wrapping_the_registry_title_is_a_wrong_work_not_a_lost_subtitle(
        self, stored: str
    ) -> None:
        """The rule accepted the registry title appearing *anywhere* in the stored one.

        That is not the shape of a dropped subtitle, it is the shape of a
        citation pointing at the work it responds to. Each title here stores the
        corrigendum, comment, reply or erratum against 10.5271/sjweh.3626 — the
        *original* paper's DOI. The first two score 0.72 and 0.74 on the title
        comparison, well under the 0.85 mismatch band, so without the
        suppression they are reported as errors and the build fails, which is
        the whole point of checking titles.
        """
        assert classify("title", stored, TITLE) is None
        result = compare(make_ref(title=stored), {"crossref": make_record()})
        assert not artifacts(result, "title")

    def test_a_leading_fragment_must_end_on_a_word_boundary(self) -> None:
        """`Comment` is a prefix of `Commentary`, and they are not the same word.

        Without the boundary a registry title of `Comment` explains away a
        stored title about something else entirely, which is the failure the
        length comparison alone never caught.
        """
        assert classify("title", "Commentary on shift work and cancer", "Comment") is None


class TestDoubledMathML:
    # The doubled "do(x)do(x)" below is a genuine Crossref deposit defect: a
    # title carrying mathematical notation came through a broken MathML
    # conversion and the operator token was repeated. Do not "correct" it — the
    # doubling is what the rule detects.
    STORED = "Estimating the effect of do(x) interventions on survival"
    DOUBLED = "Estimating the effect of do(x)do(x) interventions on survival"

    def test_a_doubled_operator_token_is_repaired_and_matched(self) -> None:
        assert classify("title", self.STORED, self.DOUBLED) == (
            "registry title mangled by a MathML deposit"
        )

    def test_the_doubling_does_not_excuse_a_different_title(self) -> None:
        """Only an exact match after repair is accepted.

        Otherwise a mangled deposit would become a licence to ignore whatever
        else the registry's title says.
        """
        different = "Estimating the effect of do(x)do(x) interventions on relapse"
        assert classify("title", self.STORED, different) is None

    def test_a_parenthesised_token_appearing_once_is_not_stripped(self) -> None:
        """Guards against loosening the rule into "ignore parenthesised tokens"."""
        assert classify("title", self.STORED, self.STORED.replace("do(x)", "do(y)")) is None

    def test_the_registrys_markup_is_removed_before_the_rule_reads_it(self) -> None:
        """Crossref ships real HTML inside titles, and the doubling straddles it.

        A deposit that mangles MathML emits the operand wrapped in ``<i>``, so
        the raw registry string is ``do(<i>x</i>)do(<i>x</i>)`` and no pattern
        written against ``do(x)do(x)`` sees it. `classify` runs `clean()` over
        both sides first; without that, this entry is reported as a plain title
        mismatch — a false alarm on a title the bibliography has exactly right.
        """
        # Markup left in deliberately: it is the registry's own bytes.
        marked_up = (
            "Estimating the effect of do(<i>x</i>)do(<i>x</i>) interventions on survival"
        )
        assert classify("title", self.STORED, marked_up) == (
            "registry title mangled by a MathML deposit"
        )


class TestBracketedParentTitle:
    PARENT = "[Night shift work and colorectal cancer risk in the MCC-Spain study]"

    def test_the_parent_article_title_prefix_is_stripped(self) -> None:
        """PubMed registers a reply with the parent article's title ahead of its own."""
        stored = "Reply to Sorensen and colleagues"
        assert classify("title", stored, f"{self.PARENT} {stored}") == (
            "registry prefixes the parent article title"
        )

    def test_a_reply_to_a_different_letter_still_fires(self) -> None:
        assert classify("title", "Reply to Sorensen and colleagues",
                        f"{self.PARENT} Reply to Blanco and colleagues") is None

    def test_a_wholly_bracketed_translated_title_is_not_stripped_into_a_match(self) -> None:
        """MEDLINE brackets the whole title of a non-English article.

        Stripping the brackets there leaves nothing, and "nothing" must never
        be allowed to match the stored title.
        """
        translated = "[Trabajo a turnos y riesgo de cancer colorrectal en el estudio MCC-Spain]"
        assert classify("title", TITLE, translated) is None

    def test_a_short_bracketed_prefix_is_not_treated_as_a_parent_title(self) -> None:
        """The rule strips a bracketed span of ten characters or more, and no less.

        Titles in imaging and tracer work legitimately *begin* with a short
        bracketed label — ``[18F]FDG``, ``[11C]raclopride``. Without the length
        floor the label is stripped, the remainder matches an entry that dropped
        it, and a real title difference is filed as a registry defect.
        """
        stored = "FDG PET imaging of shift-work-related inflammation"
        assert classify("title", stored, f"[18F]{stored}") is None


class TestYearArtifacts:
    def test_citing_the_online_first_date_is_explained(self) -> None:
        """A work online in 2016 and printed in 2017 has two correct years."""
        reason = classify("year", "2016", "2017", years={"print": 2017, "online": 2016})
        assert reason is not None
        assert "online" in reason

    def test_a_year_no_registry_date_supports_is_not_explained(self) -> None:
        assert classify("year", "2014", "2017", years={"print": 2017, "online": 2016}) is None

    def test_an_accepted_year_is_not_counted_as_something_taken_on_trust(self) -> None:
        """Comparison accepts any date the registry carries, so nothing is suppressed.

        The summary prints how many differences were suppressed; padding that
        count with non-differences would misrepresent how much of the
        bibliography is being taken on trust.
        """
        result = compare(
            make_ref(year=2016), {"crossref": make_record(years={"print": 2017, "online": 2016})}
        )
        assert result.verdict == "OK"
        assert not result.suppressed

    def test_a_deposit_timestamp_later_than_the_entry_is_an_artifact(self) -> None:
        """A working-paper series re-deposits an old item and `issued` becomes today."""
        result = compare(make_ref(year=2020), {"crossref": make_record(years={"issued": 2026})})
        assert artifacts(result, "year") == ["registry year looks like a deposit timestamp"]
        assert not result.fails

    def test_a_registry_year_earlier_than_the_entry_still_fires(self) -> None:
        """Documented exclusion: that direction is a real discrepancy for a human."""
        assert classify("year", "2020", "2015", years={"issued": 2015}) is None
        result = compare(make_ref(year=2020), {"crossref": make_record(years={"issued": 2015})})
        assert errors(result, "year") == ["mismatch"]

    @pytest.mark.parametrize(("stored", "registry"), [(2020, 2021), (2019, 2021)])
    def test_a_near_miss_year_is_not_a_deposit_stamp(self, stored: int, registry: int) -> None:
        """The rule had no lower bound: *any* later registry year was excused.

        A one-year gap is a mistyped last digit; a two-year gap is citing the
        preprint's year for the published version — the mirror of
        10.1136/gutjnl-2019-319990, which `_year_deposit_artifact`'s own
        docstring records as a real corpus shape. Neither is a working-paper
        series re-depositing an old item, which is what this rule describes and
        which lands many years out.
        """
        assert classify("year", str(stored), str(registry), years={"issued": registry}) is None
        result = compare(
            make_ref(year=stored), {"crossref": make_record(years={"issued": registry})}
        )
        assert errors(result, "year") == ["mismatch"]

    def test_a_print_date_corroborating_the_registry_year_still_fires(self) -> None:
        """With a print date, the registry's year is not an unattended deposit stamp."""
        assert classify("year", "2020", "2026", years={"print": 2026}) is None
        result = compare(make_ref(year=2020), {"crossref": make_record(years={"print": 2026})})
        assert errors(result, "year") == ["mismatch"]

    def test_a_registry_with_no_print_slot_at_all_still_fires(self) -> None:
        """MEDLINE has no print field, so its silence corroborates nothing.

        `DP` is the issue a citation is filed under and reaches `years` as
        `issued`; `DEP` reaches it as `online`. Neither is a `print` key, so
        the guard above could never fire on a PubMed record and every
        PMID-resolved entry three or more years early was excused. The record
        here is `tests/data/pubmed_epub_ahead_of_issue.txt`'s shape — `DP -
        2026 May 1`, `DEP - 20251231` — against which a stored 2019 is a wrong
        year, not a deposit stamp.
        """
        years = {"issued": 2026, "online": 2025}
        assert classify("year", "2019", "2026", source="pubmed", years=years) is None
        result = compare(
            make_ref(year=2019), {"pubmed": make_record(source="pubmed", years=years)}
        )
        assert errors(result, "year") == ["mismatch"]


class TestPages:
    def test_a_zero_padded_article_number_is_not_a_difference_at_all(self) -> None:
        """Environmental Health Perspectives deposits 027004 for article 27004."""
        result = compare(make_ref(pages="27004"), {"crossref": make_record(pages="027004")})
        assert result.verdict == "OK"
        assert not result.suppressed

    def test_an_article_number_against_a_page_range_is_an_artifact(self) -> None:
        result = compare(make_ref(pages="e0123456"), {"crossref": make_record(pages="1211-1221")})
        assert artifacts(result, "pages") == ["article number recorded against a page range"]
        assert not result.fails

    def test_two_different_article_numbers_still_fire(self) -> None:
        """Both sides number their articles, so there is nothing to excuse."""
        assert classify("pages", "e0123456", "e0999999") is None
        result = compare(make_ref(pages="e0123456"), {"crossref": make_record(pages="e0999999")})
        assert errors(result, "pages") == ["mismatch"]

    def test_two_different_page_ranges_still_fire(self) -> None:
        assert classify("pages", "1211-1221", "990-1001") is None
        result = compare(make_ref(pages="1211-1221"), {"crossref": make_record(pages="990-1001")})
        assert errors(result, "pages") == ["mismatch"]


class TestZeroPaddedVolumeAndIssue:
    """Two registries write one issue number two ways, and neither is wrong.

    Crossref deposits ``"issue": "05"`` for 10.1055/a-2760-7307 (*Clinics in
    Colon and Rectal Surgery*); MEDLINE writes ``IP - 5`` on the same work,
    PMID 42553907. On the PMID path the other registry's spelling is the only
    value there is, so a correct entry reported ``issue/mismatch``.
    """

    def test_a_padded_issue_against_a_bare_one_is_an_artifact(self) -> None:
        result = compare(make_ref(issue="05"), {"crossref": make_record(issue="5")})

        assert artifacts(result, "issue") == [
            "one side writes the number with a leading zero"
        ]
        assert not result.fails

    def test_the_other_direction_is_the_same_shape(self) -> None:
        result = compare(make_ref(volume="18"), {"crossref": make_record(volume="018")})

        assert artifacts(result, "volume") == [
            "one side writes the number with a leading zero"
        ]

    def test_two_different_numbers_still_fire(self) -> None:
        assert classify("issue", "05", "6") is None
        result = compare(make_ref(issue="05"), {"crossref": make_record(issue="6")})

        assert errors(result, "issue") == ["mismatch"]

    def test_a_label_copied_into_the_field_is_still_reported(self) -> None:
        """``Volume 18`` is the entry being wrong about what the field holds.

        Five entries in the same live sample carry a publisher's own rendering
        of its volume line. That is a defect the user can fix, and suppressing
        it would hide exactly what this tool is for.
        """
        assert classify("volume", "Volume 18", "18") is None
        result = compare(make_ref(volume="Volume 18"), {"crossref": make_record(volume="18")})

        assert errors(result, "volume") == ["mismatch"]


class TestContainerAbbreviation:
    def test_the_registrys_own_short_title_is_accepted(self) -> None:
        reason = classify(
            "container", "Am J Epidemiol", "American Journal of Epidemiology",
            container_short="Am J Epidemiol",
        )
        assert reason == "stored name is the journal's ISO abbreviation"

    def test_an_abbreviation_the_registry_does_not_supply_is_still_recognised(self) -> None:
        """Crossref omits `short-container-title` for a large minority of works."""
        reason = classify(
            "container", "Int J Cancer", "International Journal of Cancer", container_short=None
        )
        assert reason == "stored name abbreviates the registry name"

    @pytest.mark.parametrize(
        ("stored", "registry"),
        [
            # Every one of these is a pair of serials NLM lists separately, and
            # every one was cleared as an abbreviation and printed an
            # exoneration: REGISTRY-ARTIFACT is not a failing verdict, so the
            # run exited 0 on a bibliography naming the wrong journal.
            ("Annals of Oncology", "Annals of surgical oncology"),
            ("Surgical Oncology", "Annals of surgical oncology"),
            ("Journal of Cancer", "Journal of gastrointestinal cancer"),
            ("Cancer", "Pediatric blood & cancer"),
            ("Cancer", "International journal of cancer"),
            ("American Journal of Nutrition", "The American journal of clinical nutrition"),
            ("Pathology", "American journal of clinical pathology"),
        ],
    )
    def test_a_word_the_stored_name_skips_is_not_an_abbreviation(
        self, stored: str, registry: str
    ) -> None:
        """The end anchor bounds one direction only; a skipped word is the other.

        Every token being an in-order prefix reaching the registry's last one
        is satisfied by any name whose words are a subsequence of a longer
        one, so the rule accepted 27,954 ordered pairs of distinct serials in
        NLM's own list as abbreviations of one another.
        """
        assert classify("container", stored, registry, container_short=None) is None

    def test_a_wrong_journal_of_that_shape_fails_the_build(self) -> None:
        """The suppression is on the verdict path: it decides the exit code."""
        result = compare(
            make_ref(container="Annals of Oncology"),
            {"crossref": make_record(container="Annals of surgical oncology", container_short=None)},
        )

        assert result.verdict == "FIELD-MISMATCH"
        assert result.fails
        assert errors(result, "container") == ["mismatch"]

    @pytest.mark.parametrize(
        ("stored", "registry"),
        [
            ("Int J Cancer", "International Journal of Cancer"),
            ("Am J Epidemiol", "American Journal of Epidemiology"),
            (
                "Cancer Epidemiol Biomarkers Prev",
                "Cancer epidemiology, biomarkers & prevention",
            ),
            # The words an abbreviation deletes are grammatical in every
            # language it is written in, which is why the bound is on their
            # length and not on a vocabulary: *und*, *der*, *voor*, *de*.
            ("Z Gastroenterol", "Zeitschrift fur Gastroenterologie"),
            ("Rev Esp Cardiol", "Revista Espanola de Cardiologia"),
        ],
    )
    def test_a_grammatical_word_may_still_be_skipped(
        self, stored: str, registry: str
    ) -> None:
        """The bound must not cost the abbreviations the rule exists for.

        25,756 of the 26,497 words skipped across the 25,639 abbreviated
        titles in NLM's serial list are three characters or fewer.
        """
        assert classify("container", stored, registry, container_short=None) == (
            "stored name abbreviates the registry name"
        )

    def test_a_sibling_journal_is_not_an_abbreviation(self) -> None:
        """"Nature" is not short for "Nature Genetics" — it is a different journal.

        Every token of the stored name being an in-order prefix is not enough on
        its own: citing the parent title for a paper that appeared in the
        offshoot is a common, real error, and swallowing it here would make the
        container check useless for the whole Nature/Lancet/JAMA family.
        """
        assert classify("container", "Nature", "Nature Genetics", container_short=None) is None
        result = compare(
            make_ref(container="Nature"),
            {"crossref": make_record(container="Nature Genetics", container_short=None)},
        )
        assert errors(result, "container") == ["mismatch"]

    def test_a_parent_title_for_a_specialist_journal_still_fires(self) -> None:
        assert classify("container", "The Lancet", "The Lancet Oncology", container_short=None) is None

    def test_a_registry_name_carrying_an_extra_trailing_word_still_fires(self) -> None:
        assert classify(
            "container", "Int J Cancer", "International Journal of Cancer Prevention",
            container_short=None,
        ) is None

    def test_an_unrelated_journal_still_fires(self) -> None:
        assert classify(
            "container", "Journal of Clinical Oncology", "International Journal of Cancer",
            container_short=None,
        ) is None


class TestContainerLeadingArticle:
    """MEDLINE's ``JT`` is NLM's filing title, not the journal's own name.

    ``Lancet (London, England)``, ``BMJ (Clinical research ed.)``, ``Science
    (New York, N.Y.)``: the leading article dropped, a place qualifier appended
    wherever the bare title would be ambiguous. PMID 9500320's block, in
    ``tests/data/compare_pubmed_wakefield_retracted.txt``, is the instance.
    """

    def test_the_masthead_name_against_medlines_filing_title_is_an_artifact(self) -> None:
        assert classify(
            "container", "The Lancet", "Lancet (London, England)",
            container_alternates=["Lancet"],
        ) == "registry files the journal without its leading article"

    def test_the_qualifier_alone_is_not_enough_to_explain_a_difference(self) -> None:
        """With no ``TA`` on the record there is no title to match, and none is invented.

        This rule reads the titles the registry supplies and strips one opening
        article from the *stored* value; it never edits the registry's. What
        may come off the registry side is decided next door, in
        :class:`TestContainerMedlineSubtitle` and
        :class:`TestContainerMedlineQualifier` — and neither of those reaches
        this pairing either, because both strip an article from the registry's
        side alone and here it is the stored value that carries one.
        """
        assert classify(
            "container", "The Lancet", "Lancet (London, England)", container_alternates=[]
        ) is None

    def test_a_journal_filed_without_a_qualifier_is_covered_too(self) -> None:
        assert classify("container", "The BMJ", "BMJ", container_alternates=[]) == (
            "registry files the journal without its leading article"
        )

    def test_a_sibling_journal_is_not_a_dropped_article(self) -> None:
        """The pairing, and the reason the rule compares whole titles.

        *The Lancet Oncology* is where a Lancet-family citation actually goes
        wrong, and it differs from every title the record carries by a word
        that is not an article.
        """
        assert classify(
            "container", "The Lancet Oncology", "Lancet (London, England)",
            container_alternates=["Lancet"],
        ) is None

    def test_a_title_the_comparison_alphabet_cannot_represent_matches_nothing(
        self,
    ) -> None:
        """Both sides fold to nothing, and nothing is not a match.

        MEDLINE files a Chinese-language serial under a ``TA`` written in Han,
        which ``fold`` discards entirely. Without the emptiness guard the
        stored name — discarded the same way — equals that empty alternate, and
        a journal nobody compared is suppressed under a sentence claiming the
        registry dropped an article it never had.
        """
        assert classify(
            "container", "中华肿瘤杂志", "Chinese Journal of Oncology",
            container_alternates=["中华肿瘤杂志"],
        ) is None

    def test_the_article_the_registry_keeps_is_the_same_shape(self) -> None:
        """NLM keeps a serial's article on some titles and drops it on most.

        ``The Alaska nurse``, ``Der Anaesthesist``, ``L'Auxiliaire`` are ``JT``
        values whose ``MedAbbr`` is the name without it, and *The Lancet*
        against a stored *Lancet* is the shape a bibliography exported from
        PubMed or EndNote carries. Nothing is abbreviated in any of them.
        """
        assert classify("container", "Lancet", "The Lancet", container_alternates=[]) == (
            "registry files the journal under a leading article"
        )

    def test_a_parallel_title_half_that_opens_with_an_article_is_reachable(self) -> None:
        """PMID 42559441: ``JT`` joins two names and the English one keeps its article.

        Crossref deposits the journal as *Canadian Journal of Statistics*, so
        the article-free masthead form is the exported spelling and the one
        that failed. 63 of the 607 parallel titles in NLM's own list open a
        half with an article.
        """
        journal, alternates = medline_journal("pubmed_parallel_title_article.txt")

        assert journal == (
            "The Canadian journal of statistics = Revue canadienne de statistique"
        )
        assert classify(
            "container", "Canadian Journal of Statistics", journal,
            container_alternates=alternates,
        ) == "registry files the journal under a leading article"

    def test_the_other_half_is_still_matched_whole(self) -> None:
        """The French half carries no article and needs no editing at all."""
        _, alternates = medline_journal("pubmed_parallel_title_article.txt")

        assert "Revue canadienne de statistique" in alternates

    def test_a_sibling_journal_is_not_a_kept_article_either(self) -> None:
        """The pairing for the new direction, and the reason it is a whole-title test."""
        assert classify(
            "container", "Canadian Journal of Surgery",
            "The Canadian journal of statistics = Revue canadienne de statistique",
            container_alternates=["Can J Stat", "The Canadian journal of statistics"],
        ) is None

    def test_only_one_side_loses_an_article_in_any_comparison(self) -> None:
        """``A X`` against ``The X`` is two articles, and neither rule may join them.

        The stored branch strips the entry's, the registry branch strips the
        record's, and running both would suppress a difference the registry
        dropped nothing to produce under a sentence false of it.
        """
        assert classify(
            "container", "A Journal of Cancer", "The Journal of Cancer",
            container_alternates=[],
        ) is None

    def test_an_unrelated_journal_is_not_a_dropped_article(self) -> None:
        assert classify(
            "container", "BMJ", "Lancet (London, England)", container_alternates=["Lancet"]
        ) is None

    def test_two_different_articles_are_not_one_dropped_article(self) -> None:
        """The reason has to be true of what it suppresses.

        Stripping the article from the registry's value as well made this pair
        an artifact reading "registry files the journal without its leading
        article", when the registry dropped nothing: the two names open with
        different words, which is a difference like any other.
        """
        assert classify(
            "container", "A Journal of Cancer", "The Journal of Cancer",
            container_alternates=[],
        ) is None

    def test_an_article_in_the_middle_of_a_title_is_not_stripped(self) -> None:
        """Only the *opening* word may go, which is what ``^`` in the pattern is.

        Unanchored, ``re.sub`` takes every internal "the ", and *Journal of the
        National Cancer Institute* — a real journal — becomes a documented
        registry artifact against a name nobody publishes under, disappearing
        from the report entirely.
        """
        assert classify(
            "container", "Journal of the National Cancer Institute",
            "Journal of National Cancer Institute", container_alternates=[],
        ) is None


class TestContainerMedlineSubtitle:
    """MEDLINE's ``JT`` also carries a subtitle, after a spaced colon.

    PMID 32430337's own block, in ``tests/data/pubmed_society_expansion.txt``:
    ``TA - Cancer Epidemiol Biomarkers Prev`` beside the ``JT`` below. Where
    ``TA`` is a real abbreviation rather than the journal's plain name, neither
    the alternate-title route nor the leading-article rule reaches the entry
    that stores the masthead name, and 26 of 173 journals in a 300-record
    sample are filed this way.
    """

    JT = (
        "Cancer epidemiology, biomarkers & prevention : a publication of the American "
        "Association for Cancer Research, cosponsored by the American Society of "
        "Preventive Oncology"
    )
    TA = ("Cancer Epidemiol Biomarkers Prev",)

    def test_the_masthead_name_against_the_medline_subtitle_is_an_artifact(self) -> None:
        assert classify(
            "container", "Cancer Epidemiology, Biomarkers & Prevention", self.JT,
            container_alternates=list(self.TA),
        ) == "registry appends its own subtitle to the journal name"

    @pytest.mark.parametrize(
        ("stored", "jt"),
        [
            # The title's own acronym, not a society.
            ("Archives of Medical Science", "Archives of medical science : AMS"),
            (
                "The Malaysian Journal of Medical Sciences",
                "The Malaysian journal of medical sciences : MJMS",
            ),
            # A descriptive phrase naming no organisation at all.
            (
                "The British Journal of Psychiatry",
                "The British journal of psychiatry : the journal of mental science",
            ),
        ],
    )
    def test_a_subtitle_naming_no_society_is_the_same_defect(
        self, stored: str, jt: str
    ) -> None:
        """PMIDs 42540560, 42534732 and 42552687, all live ``efetch`` output.

        The rule reads NLM's separator, never what follows it, so all three are
        suppressed under the one reason — which is why that reason says
        *subtitle* and not *society*. A reader challenging one of these
        suppressions is owed a sentence that is true of the entry it is printed
        beside.
        """
        assert classify("container", stored, jt) == (
            "registry appends its own subtitle to the journal name"
        )

    def test_a_leading_article_on_the_registrys_side_is_the_same_defect(self) -> None:
        """PMID 42547188, live ``efetch``: NLM keeps *The*, the masthead has none.

        ``JT - The Journal of adolescent health : official publication of the
        Society for Adolescent Medicine`` against ``TA - J Adolesc Health``.
        Crossref and the masthead both say *Journal of Adolescent Health*, so
        neither the whole-``JT`` leading-article rule nor the abbreviation rule
        reaches the entry and a correct citation reported `container/mismatch`.
        """
        assert classify(
            "container",
            "Journal of Adolescent Health",
            "The Journal of adolescent health : official publication of the Society "
            "for Adolescent Medicine",
            container_alternates=["J Adolesc Health"],
        ) == "registry files the journal under a leading article and a subtitle"

    def test_the_first_spaced_colon_is_the_separator_not_the_last(self) -> None:
        """NLM writes a second one, and everything before it is not the name.

        ``Arthroscopy : the journal of arthroscopic & related surgery :
        official publication of the Arthroscopy Association of North America
        and the International Arthroscopy Association`` is NlmId 8506498's own
        filing title, verbatim from NLM's serial list
        (``ftp.ncbi.nlm.nih.gov/pubmed/J_Medline.txt``), where 401 of 37,987
        serials carry two spaced colons. The masthead is *Arthroscopy*.
        Splitting on the last colon would compare the stored name against
        ``Arthroscopy : the journal of arthroscopic & related surgery`` and
        report a correct entry as a mismatch.
        """
        jt = (
            "Arthroscopy : the journal of arthroscopic & related surgery : "
            "official publication of the Arthroscopy Association of North America "
            "and the International Arthroscopy Association"
        )

        assert classify("container", "Arthroscopy", jt) == (
            "registry appends its own subtitle to the journal name"
        )

    def test_the_text_between_two_separators_is_not_the_journals_name(self) -> None:
        """The pairing, and what a last-colon split would have accepted."""
        jt = (
            "Arthroscopy : the journal of arthroscopic & related surgery : "
            "official publication of the Arthroscopy Association of North America "
            "and the International Arthroscopy Association"
        )

        assert classify(
            "container", "Arthroscopy : the journal of arthroscopic & related surgery", jt
        ) is None

    def test_a_different_article_on_the_stored_side_still_fires(self) -> None:
        """The pairing: only one side is ever stripped, so *A* is not *The*.

        Taking the article off both would suppress two journals whose names
        differ by exactly the word that distinguishes them, under a sentence
        blaming the registry for a difference it did not make.
        """
        assert classify(
            "container",
            "A Journal of Cancer",
            "The Journal of Cancer : official journal of the Cancer Society",
        ) is None

    def test_a_titles_own_unspaced_colon_is_not_the_separator(self) -> None:
        """`Circulation: Cardiovascular Quality and Outcomes` is another journal.

        Crossref's literal `container-title` for
        10.1161/circoutcomes.5.suppl_1.a180, and an AHA title distinct from
        *Circulation*. NLM spaces its own separator on both sides; a masthead
        writes `Title: Subtitle` closed up. Matching on a bare colon merges two
        journals in one family — the error `_container_abbreviation`'s
        final-token rule exists to prevent, arriving through the rule written
        beside it.
        """
        assert classify(
            "container", "Circulation", "Circulation: Cardiovascular Quality and Outcomes"
        ) is None

    def test_a_colon_inside_a_parenthetical_qualifier_is_not_the_separator(self) -> None:
        """PMID 42552576: ``ASAIO journal (American Society for Artificial
        Internal Organs : 1992)``.

        NLM writes its spaced colon inside the qualifier where the body named
        there needs a date to be unambiguous, so the first one in ``JT`` is not
        always the separator. Splitting on it leaves a base ending
        mid-qualifier, which is not a name to compare a stored value against.
        A stored value that *is* that fragment is the only thing the guard has
        to hold off, and it holds; the journal's own name reaches
        :class:`TestContainerMedlineQualifier`, which takes the qualifier off
        whole.
        """
        jt, _ = medline_journal("pubmed_qualifier_inner_colon.txt")

        assert classify("container", jt.partition(" : ")[0], jt) is None

    def test_a_journal_sharing_the_opening_words_still_fires(self) -> None:
        """The pairing. *Cancer Epidemiology* is Elsevier's, a different journal.

        The stored name has to equal the whole of the base title, not open it:
        a prefix test would explain away exactly the sibling-journal error the
        container check exists to report.
        """
        assert classify(
            "container", "Cancer Epidemiology", self.JT, container_alternates=list(self.TA)
        ) is None

    def test_an_unrelated_journal_still_fires(self) -> None:
        assert classify(
            "container", "Clinical Cancer Research", self.JT, container_alternates=list(self.TA)
        ) is None


class TestContainerMedlineQualifier:
    """``JT`` also carries a parenthetical qualifier, and it comes off.

    NLM appends one — a place, a founding year, the issuing body, or several
    at once — wherever a bare title would be ambiguous in its catalogue, on
    2,698 of the 37,987 serials in ``J_Medline.txt``. In a catalogue that
    qualifier is exactly what tells two serials of the same base name apart,
    which is the objection this rule has to answer; in *this* comparison
    nothing is being looked up. ``compare`` reaches ``container`` only after
    the work has been pinned — by its identifier, or by the title, author and
    year ``confirm_without_id`` demanded — and a work appears in exactly one
    serial, so the ``JT`` in hand is by construction that serial's.

    The residual exposure is a citation naming a journal that shares a base
    title with the right one *and* being wrong about it: ``The neurologist``
    (NlmId 9503763) beside ``The neurologist (Hyderabad, India)`` (NlmId
    101719078) is a real such pair. It requires the entry to be wrong in the
    one field the identifier already settled.
    """

    def test_a_founding_year_qualifier_is_an_artifact(self) -> None:
        """PMID 42555391, ``tests/data/pubmed_qualifier_year.txt``.

        ``TA`` is a real abbreviation carrying a qualifier of its own, so the
        alternate-title route does not reach the entry either, and without
        this rule a correct citation of the journal's own name fails the
        build.
        """
        jt, ta = medline_journal("pubmed_qualifier_year.txt")

        assert (jt, ta) == ("Annals of medicine and surgery (2012)", ["Ann Med Surg (Lond)"])
        assert classify(
            "container", "Annals of Medicine and Surgery", jt, container_alternates=ta
        ) == "registry appends a parenthetical qualifier to the journal name"

    def test_a_qualifier_carrying_its_own_colon_comes_off_whole(self) -> None:
        """PMID 42552576, ``tests/data/pubmed_qualifier_inner_colon.txt``.

        ``ASAIO journal (American Society for Artificial Internal Organs :
        1992)``. The parenthetical is matched from its closing bracket back to
        the one that opens it, so NLM's inner colon never splits it and the
        base left behind is the journal's whole name rather than the head of a
        qualifier.
        """
        jt, ta = medline_journal("pubmed_qualifier_inner_colon.txt")

        assert jt == "ASAIO journal (American Society for Artificial Internal Organs : 1992)"
        assert classify("container", "ASAIO Journal", jt, container_alternates=ta) == (
            "registry appends a parenthetical qualifier to the journal name"
        )

    def test_a_nested_qualifier_comes_off_in_one_piece(self) -> None:
        """PMID 42546669: ``Clinical oncology (Royal College of Radiologists
        (Great Britain))``.

        Forty-nine serials name a body that itself needs a country, and the
        outer bracket is the one that closes the qualifier. A pattern matching
        only bracket-free contents would leave ``Clinical oncology (Royal
        College of Radiologists`` and suppress nothing.
        """
        jt = "Clinical oncology (Royal College of Radiologists (Great Britain))"

        assert classify("container", "Clinical Oncology", jt) == (
            "registry appends a parenthetical qualifier to the journal name"
        )

    def test_a_leading_article_before_the_qualifier_is_the_same_defect(self) -> None:
        """PMID 30151503, ``tests/data/pubmed_qualifier_leading_article.txt``.

        NLM keeps the article on ten of the qualified serials and drops it on
        the rest, so it comes off the registry's remainder exactly as it does
        after the spaced colon next door. Which of the two happened is what the
        returned reason says, because a suppression whose stated cause is not
        the actual one cannot be argued with.
        """
        jt, ta = medline_journal("pubmed_qualifier_leading_article.txt")

        assert (jt, ta) == ("The neurologist (Hyderabad, India)", ["Neurologist (Hyderabad)"])
        assert classify("container", "Neurologist", jt, container_alternates=ta) == (
            "registry files the journal under a leading article and a qualifier"
        )

    def test_a_qualifier_left_unclosed_is_refused(self) -> None:
        """NlmId 101745449: ``Interventional radiology (Higashimatsuyama-shi (Japan)``.

        One of four titles in NLM's serial list whose final bracket does not
        close the qualifier. Stripping from it leaves ``Interventional
        radiology (Higashimatsuyama-shi``, a fragment of a name, and comparing
        a stored value against a fragment is what the balance check exists to
        stop.

        The same walk answers the question from the other side. A bracket that
        nothing opens is not in NLM's list, but it is what that same title
        looks like after an export has eaten the opening ones, and there is no
        qualifier there to remove.
        """
        jt = "Interventional radiology (Higashimatsuyama-shi (Japan)"

        assert classify("container", "Interventional Radiology", jt) is None
        assert classify("container", "Interventional radiology (Higashimatsuyama-shi", jt) is None
        assert classify(
            "container", "Interventional Radiology",
            "Interventional radiology Higashimatsuyama-shi Japan)",
        ) is None

    def test_a_volume_title_reaches_the_rule_too(self) -> None:
        """The one scope guard is the field, and a book's container is a volume.

        MEDLINE's ``BTI`` on every *GeneReviews* chapter is ``GeneReviews((R))``
        (PMID 20301425) and a bibliography writes ``booktitle = {GeneReviews}``;
        a book record carries no ``TA``, so ``container_alternates`` is empty
        and nothing else reaches the pairing. Crossref's deposit for
        10.1088/978-0-7503-3703-8ch5 is ``container-title: ["Photography
        (Second Edition)"]`` and an ``@incollection`` naming *Photography* is
        suppressed against it, which is the entry omitting the parenthetical on
        a work its own identifier has already pinned.
        """
        assert classify(
            "container", "GeneReviews", "GeneReviews((R))", container_alternates=[]
        ) == "registry appends a parenthetical qualifier to the journal name"
        assert classify(
            "container", "Photography", "Photography (Second Edition)",
            container_alternates=[],
        ) == "registry appends a parenthetical qualifier to the journal name"

    def test_a_volume_named_as_the_wrong_edition_still_fires(self) -> None:
        """The pairing for a book, where two editions really are two works.

        Only an *omitted* parenthetical is suppressed. The remainder has to
        equal the stored name outright, so an entry that names an edition and
        names the wrong one is a ``container/mismatch`` like any other.
        """
        assert classify(
            "container", "Photography (First Edition)", "Photography (Second Edition)",
            container_alternates=[],
        ) is None

    def test_a_journal_differing_by_a_word_still_fires(self) -> None:
        """The pairing. The remainder has to equal the stored name outright.

        *Annals of Surgery* is a different journal from *Annals of Medicine and
        Surgery*, and a prefix or substring test over the remainder would
        explain away exactly the sibling-journal error the container check
        exists to report.
        """
        jt, ta = medline_journal("pubmed_qualifier_year.txt")

        assert classify("container", "Annals of Surgery", jt, container_alternates=ta) is None
        result = compare(
            make_ref(container="Annals of Surgery"),
            {"pubmed": make_record(source="pubmed", container=jt, container_alternates=ta)},
        )
        assert errors(result, "container") == ["mismatch"]

    def test_an_unrelated_journal_still_fires(self) -> None:
        jt, ta = medline_journal("pubmed_qualifier_leading_article.txt")

        assert classify("container", "Neurology", jt, container_alternates=ta) is None

    def test_a_different_article_on_the_stored_side_still_fires(self) -> None:
        """Only one side is ever stripped, so *A* is not *The*.

        Taking the article off both would suppress two journals whose names
        differ by exactly the word that distinguishes them, under a sentence
        blaming the registry for a difference it did not make.
        """
        assert classify(
            "container", "A Journal of Cancer", "The Journal of Cancer (Basel, Switzerland)"
        ) is None

    def test_the_stored_side_keeps_its_own_qualifier(self) -> None:
        """The registry's value is the only one edited.

        An entry exported from PubMed stores the qualifier, and the reason
        printed says the registry appended it; a rule stripping the stored side
        too would say that of an entry the registry appended nothing to, and
        would drop a qualifier from each of two serials in the one comparison.
        """
        assert classify(
            "container", "Annals of Medicine and Surgery (2012)", "Annals of medicine and surgery"
        ) is None


class TestContainerAcronymPrefix:
    """A publisher's masthead sets the acronym ahead of the name; NLM does not.

    ``JNCI: Journal of the National Cancer Institute`` against ``JT - Journal
    of the National Cancer Institute`` (PMID 42550479), and ``JACCP: JOURNAL OF
    THE AMERICAN COLLEGE OF CLINICAL PHARMACY`` against ``JT - Journal of the
    American College of Clinical Pharmacy : JACCP`` (PMID 42522049). Seven of
    386 entries in a live sample failed on the shape.
    """

    def test_the_acronym_ahead_of_the_registrys_own_name_is_an_artifact(self) -> None:
        assert classify(
            "container",
            "JNCI: Journal of the National Cancer Institute",
            "Journal of the National Cancer Institute",
            container_alternates=["J Natl Cancer Inst"],
        ) == "stored name prefixes the journal's own acronym"

    def test_it_reaches_a_name_nlm_files_behind_its_own_subtitle(self) -> None:
        """Both sides carry the acronym, in the two places their houses put it."""
        assert classify(
            "container",
            "JACCP: JOURNAL OF THE AMERICAN COLLEGE OF CLINICAL PHARMACY",
            "Journal of the American College of Clinical Pharmacy : JACCP",
            container_alternates=["J Am Coll Clin Pharm"],
        ) == "stored name prefixes the journal's own acronym"

    def test_an_acronym_that_is_not_the_names_own_still_fires(self) -> None:
        """The whole of what makes removing the prefix information-free."""
        assert classify(
            "container",
            "NEJM: Journal of the National Cancer Institute",
            "Journal of the National Cancer Institute",
        ) is None

    def test_a_different_journal_behind_the_acronym_still_fires(self) -> None:
        assert classify(
            "container",
            "JNCI: Journal of the National Cancer Institute",
            "Journal of Clinical Oncology",
        ) is None

    def test_a_titles_own_opening_clause_is_not_an_acronym(self) -> None:
        """*Circulation: Cardiovascular Quality and Outcomes* is another journal.

        The case class is what refuses it: a lowercase word before a colon is a
        title's own clause, and reading it as an acronym would merge two AHA
        journals.
        """
        assert classify(
            "container", "Circulation: Cardiovascular Quality and Outcomes", "Circulation"
        ) is None

    def test_the_remainder_has_to_match_outright(self) -> None:
        """Never a prefix test, for the reason every container rule says so."""
        assert classify(
            "container",
            "CEBP: Cancer Epidemiology",
            "Cancer Epidemiology, Biomarkers & Prevention",
        ) is None


class TestRedirectingAggregatorDoi:
    def test_a_jstor_doi_redirecting_to_the_publisher_is_not_a_defect(self) -> None:
        assert classify("doi", "10.2307/2669548", "10.1111/j.1540-5907.2000.tb00000.x") == (
            "aggregator DOI redirects to the publisher's own"
        )

    def test_an_ordinary_doi_disagreement_is_not_excused(self) -> None:
        assert classify("doi", "10.1093/aje/kwx137", "10.1111/j.1540-5907.2000.tb00000.x") is None

    def test_the_rule_reads_only_the_stored_side(self) -> None:
        """A stored DOI that is simply wrong must not be excused by the registry's.

        Making this symmetric would silence every mismatch against a JSTOR
        record, which is the opposite of what the redirect explains.
        """
        assert classify("doi", "10.1111/j.1540-5907.2000.tb00000.x", "10.2307/2669548") is None


class TestPmidPmcAccession:
    """One MEDLINE record, two accession schemes.

    PMID 28520842 (*Am J Epidemiol* 2017, 10.1093/aje/kwx137) carries ``PMC  -
    PMC5860629`` beside its own ``PMID- 28520842``, and ``efetch
    id=28520842`` shows both on the one citation. A ``pmid`` field holding
    ``5860629`` names that record, so ``compare._check_pmid``'s claim — two
    identifiers, two citations — is not true of it.
    """

    def test_the_records_own_pmc_accession_is_not_another_citation(self) -> None:
        assert classify("pmid", "5860629", "28520842", raw={"PMC": ["PMC5860629"]}) == (
            "stored number is this record's own PMC accession"
        )

    def test_the_prefix_is_read_however_it_was_pasted(self) -> None:
        """A bibliography writes ``pmc5860629`` as readily as ``PMC5860629``."""
        assert classify("pmid", "5860629", "28520842", raw={"PMC": ["pmc5860629"]}) is not None

    def test_a_different_number_is_not_excused_by_the_pmc_line(self) -> None:
        """The pairing: a record with a ``PMC`` line is not a record beyond question.

        PMID 9500320 is Wakefield et al. — a different paper entirely — and
        the rule must read the accession rather than the presence of one.
        """
        assert classify("pmid", "9500320", "28520842", raw={"PMC": ["PMC5860629"]}) is None

    def test_a_record_with_no_pmc_line_explains_nothing(self) -> None:
        """Crossref records have no ``PMC`` key, and none is invented for them."""
        assert classify("pmid", "5860629", "28520842") is None

    def test_a_pmc_field_that_is_not_a_list_is_not_read(self) -> None:
        """``raw`` is whatever the registry sent; only MEDLINE's shape is parsed."""
        assert classify("pmid", "5860629", "28520842", raw={"PMC": "PMC5860629"}) is None


class TestRuleScoping:
    def test_a_title_rule_does_not_leak_into_another_field(self) -> None:
        """Each rule guards on its own field; without that, one rule silences all."""
        assert classify("volume", "Comment on 'Statistics and Causal Inference'", "Comment") is None

    @pytest.mark.parametrize(
        ("field", "stored", "registry", "record_kwargs", "rule"),
        [
            # _year_deposit_artifact reads both sides with int() and accepts any
            # registry value larger than the stored one. Unscoped, that excuses
            # *every* numeric field where the registry holds the bigger number:
            # volume 185 against the registry's 186 — the single commonest real
            # volume error — would be filed as a deposit timestamp and never
            # reported.
            ("volume", "185", "186", {"years": {"issued": 2017}}, "_year_deposit_artifact"),
            # _year_online_first accepts any value the record carries as a date.
            # Unscoped, an entry whose volume happens to equal the publication
            # year is "explained" against a completely different volume.
            ("volume", "2017", "185", {}, "_year_online_first"),
            # _pages_article_number fires when exactly one side looks like an
            # article number. Unscoped, a year typed into the volume field
            # (four digits, no dash) is excused against the real volume.
            ("volume", "2019", "185", {"years": {"issued": 2017}}, "_pages_article_number"),
            # _container_abbreviation accepts in-order token prefixes. Unscoped,
            # a title abbreviated to initials is excused against the full title,
            # which is exactly the drift the title check exists to show.
            (
                "title", "Am J Epidemiol", "American Journal of Epidemiology",
                {}, "_container_abbreviation",
            ),
            # _container_leading_article accepts a difference of one opening
            # article. Unscoped, "The Lancet" as a *title* is explained against
            # a paper called "Lancet", and a publisher whose article titles
            # begin with "The" gets a free pass on the field that identifies
            # the work.
            (
                "title", "The Lancet", "Lancet",
                {"container_alternates": []}, "_container_leading_article",
            ),
            # _container_medline_subtitle accepts a stored value equal to
            # everything before NLM's spaced colon. Unscoped, an entry titled
            # "Vitamin D" is explained against a registry title of "Vitamin D :
            # a review of the evidence" — a different document, reached through
            # the one field that says which work is cited.
            (
                "title", "Vitamin D", "Vitamin D : a review of the evidence",
                {}, "_container_medline_subtitle",
            ),
            # _container_medline_qualifier accepts a stored value equal to the
            # registry's minus a trailing parenthetical. Unscoped, an entry
            # titled "Vitamin D" is explained against a registry title of
            # "Vitamin D (second edition)" — a different document, and the
            # parenthesis in a title is the publisher's, not a catalogue's
            # disambiguator.
            (
                "title", "Vitamin D", "Vitamin D (second edition)",
                {}, "_container_medline_qualifier",
            ),
            # _pmid_pmc_accession compares a stored number against the record's
            # own PMC line. Unscoped, any field whose value happens to equal
            # those digits is excused against the registry's — a PMC accession
            # explains a wrong PMID and nothing else about the entry.
            ("volume", "5860629", "47", {"raw": {"PMC": ["PMC5860629"]}}, "_pmid_pmc_accession"),
            # _number_zero_padded accepts two ASCII digit strings differing by
            # leading zeros. Unscoped, a stored page of "07" is excused against
            # an opening page of 7 on a work that starts at 7 in one registry
            # and 07 in neither — pages have their own first-page rule, and a
            # DOI's suffix or a PMID would be excused the same way.
            ("pages", "027004", "27004", {}, "_number_zero_padded"),
            # _container_acronym_prefix strips an all-caps prefix whose letters
            # are word-initials of what follows. Unscoped, a paper titled
            # "MCCS: Melanoma, Cohort, Case and Survival" is explained against
            # a registry title of the words alone — the field that says which
            # work is cited, cleared by an editing rule written for a masthead.
            (
                "title", "MCCS: Melanoma Cohort Case Survival",
                "Melanoma Cohort Case Survival", {}, "_container_acronym_prefix",
            ),
        ],
    )
    def test_no_rule_leaks_into_a_field_it_was_not_written_for(
        self, field: str, stored: str, registry: str, record_kwargs: dict[str, object], rule: str
    ) -> None:
        """Every rule guards on ``field`` first; each case names the rule it pins."""
        assert classify(field, stored, registry, **record_kwargs) is None, rule

    @pytest.mark.parametrize(
        ("field", "stored", "registry"),
        [
            # Without the empty-value guard, stripping the bracketed prefix
            # leaves "", which equals an absent stored title, and a title the
            # entry does not have at all is "explained".
            ("title", "", "[Night shift work and colorectal cancer risk in the MCC-Spain study]"),
            # And here exactly one side looks like an article number — because
            # the other side is empty.
            ("pages", "e0123456", ""),
        ],
    )
    def test_a_missing_value_is_never_explained_away(
        self, field: str, stored: str, registry: str
    ) -> None:
        """An absent value is incompleteness, and INCOMPLETE is what must be reported.

        A rule that fired on an empty side would turn a gap in the bibliography,
        or a gap in the registry, into a registry defect nobody looks at again.
        """
        assert classify(field, stored, registry) is None

    def test_every_check_names_the_case_that_motivated_it(self) -> None:
        """CLAUDE.md: an undocumented suppression is a check nobody can audit."""
        undocumented = [c.__name__ for c in benign.CHECKS if not (c.__doc__ or "").strip()]
        assert undocumented == []

    def test_every_check_is_written_up_in_the_registry_defect_docs(self) -> None:
        """A suppression a reader cannot look up is one nobody can challenge.

        A docstring alone is not enough: it is invisible to the person deciding
        whether to trust a ``REGISTRY-ARTIFACT`` line in a report. Adding a rule
        to ``CHECKS`` without ``docs/registry-artifacts.md`` naming it turns this
        test red, which is the only automatic reminder there is.

        Matched as the backticked ``benign.``-qualified form the documentation
        actually uses, for the reason the author test below spells out: these
        names are prefix-shaped — ``_title_shortened``, ``_year_online_first``,
        ``_pages_article_number`` — so a bare substring would let the first
        ``_title_shortened_by_publisher``-style addition be satisfied by the
        older rule's section.
        """
        prose = ARTIFACT_DOCS.read_text(encoding="utf-8")
        missing = [c.__name__ for c in benign.CHECKS if f"`benign.{c.__name__}`" not in prose]
        assert missing == []

    def test_a_rule_with_no_instance_says_so_where_the_reader_looks(self) -> None:
        """``benign.py``'s note is invisible to whoever is challenging a report.

        Three rules describe a shape a search of the corpus and of the live
        registries did not find, and the docstring saying so is read by nobody
        deciding whether to trust a ``REGISTRY-ARTIFACT`` line. The file that
        *is* read described all three as ordinary documented defects, which
        reads as a guarantee being honoured. This keeps the two in step in the
        direction that matters: a rule marked in the source must be marked in
        the section a reader can reach.
        """
        prose = ARTIFACT_DOCS.read_text(encoding="utf-8")
        sections = {
            check.__name__: section
            for section in prose.split("\n## ")
            for check in benign.CHECKS
            if f"`benign.{check.__name__}`" in section
        }
        unmarked = [
            name
            for check in benign.CHECKS
            if _UNWITNESSED in (check.__doc__ or "")
            and (name := check.__name__)
            and _UNWITNESSED not in sections.get(name, "")
        ]
        assert unmarked == [], (
            f"{unmarked} carry a NO WITNESSED INSTANCE note in benign.py and "
            "their section in docs/registry-artifacts.md does not."
        )

    def test_an_author_escape_with_no_instance_says_so_where_the_reader_looks(
        self,
    ) -> None:
        """The author half of the marker contract, which only ``CHECKS`` had.

        ``names.py`` marks one escape **NO WITNESSED INSTANCE** and the file a
        reader actually reaches marked it in a different case, so the scan
        above could never have matched it even had it looked. Editing the
        marker out of the section left the suite green.

        The source names the reason on the marker's own line, because an
        escape is a branch inside a byline walk rather than a function in a
        registry, and nothing else ties the two together mechanically.
        """
        prose = ARTIFACT_DOCS.read_text(encoding="utf-8")
        source = Path(str(names.__file__)).read_text(encoding="utf-8")
        marked = [
            reason
            for reason in names.Reason
            for line in source.splitlines()
            if _UNWITNESSED in line and f"Reason.{reason.name}" in line
        ]

        assert marked, (
            f"no line of names.py carries {_UNWITNESSED} beside a Reason member. "
            "Either the marker moved or it stopped naming its reason, and "
            "either way this test now proves nothing."
        )
        unmarked = [
            reason.name
            for reason in marked
            for section in [
                next(
                    (s for s in prose.split("\n## ") if f"`{_documented(reason)}`" in s),
                    "",
                )
            ]
            if _UNWITNESSED not in section
        ]
        assert unmarked == [], (
            f"{unmarked} carry a {_UNWITNESSED} note in names.py and their "
            "section in docs/registry-artifacts.md does not."
        )

    def test_every_author_escape_is_written_up_too(self) -> None:
        """The author half of the same contract.

        ``names.compare_author_lists`` decides its escapes while walking two
        bylines in step, which is why they are not in ``CHECKS`` — but
        ``compare._check_authors`` turns each one into a ``REGISTRY-ARTIFACT``
        suppression exactly like a check in this module, and a reader has the
        same claim to look it up. Only ``CHECKS`` was covered here, so the
        author escapes could be added, changed or reworded with nothing
        noticing.
        """
        prose = ARTIFACT_DOCS.read_text(encoding="utf-8")
        # Matched as a backticked literal, not as a bare substring. Two reasons
        # contain another ("registry mojibake" sits inside "registry mojibake
        # truncated the surname", "collective author" inside "registry lists a
        # collective author"), so a substring test passes for a reason whose own
        # section was deleted — it finds the longer one and reports success.
        missing = [r for r in names.ARTIFACT_REASONS if f"`{_documented(r)}`" not in prose]
        assert missing == []

    def test_no_reason_is_an_alias_for_another(self) -> None:
        """Documenting the list proves nothing unless the list is complete.

        Most of completeness is structural: ``ARTIFACT_REASONS`` is derived from
        ``names.Reason``, so a reason cannot exist outside the tuple the test
        above walks, and ``mypy`` covers the rest — ``note`` takes a ``Reason``,
        ``reasons`` is read-only, ``names_agree`` returns ``Reason | None`` — so
        a bare string fails CI at the emission site.

        Neither reaches a member whose value repeats another's. ``Enum`` makes
        that an *alias*: it vanishes from iteration, so every derived collection
        still looks right, while it answers to the older member's name and emits
        the older member's reason. ``@unique`` on ``Reason`` turns it into an
        ``ImportError``; this pins the property over ``__members__``, the one
        view that shows aliases, so the guarantee survives the decorator.
        """
        members = names.Reason.__members__
        assert len({member.value for member in members.values()}) == len(members)


class TestArtifactsAreReportedNotResolved:
    def test_an_artifact_carries_both_values_and_a_reason(self) -> None:
        """Artifacts are listed under REGISTRY-ARTIFACT, never silently dropped."""
        result = compare(
            make_ref(title="Comment on 'Statistics and Causal Inference'"),
            {"crossref": make_record(title="Comment")},
        )
        issue = result.suppressed[0]
        assert issue.kind == "registry-artifact"
        assert issue.severity == "info"
        assert issue.stored and issue.registry and issue.note

    def test_an_artifact_never_rewrites_the_entry_or_the_record(self) -> None:
        """Recognising a registry defect must not cause a value to be adopted.

        `Scand J Work Environ Health` is the abbreviation Crossref's own
        `short-container-title` gives for 10.5271/sjweh.3626; the record here
        leaves that field unset so the difference has to be carried by the
        token-prefix path instead.
        """
        ref = make_ref(container="Scand J Work Environ Health")
        record = make_record()
        result = compare(ref, {"crossref": record})
        # Without this the test would still pass if the container rule never
        # fired at all, and it would then be guarding nothing.
        assert artifacts(result, "container") == ["stored name abbreviates the registry name"]
        assert ref.container == "Scand J Work Environ Health"
        assert record.container == "Scandinavian Journal of Work, Environment & Health"


class TestAuthorArtifacts:
    """The eight author differences the corpus run flagged, all false positives.

    Author differences are adjudicated in :mod:`bibaudit.names` rather than by a
    rule in :mod:`bibaudit.benign`, but they surface through the same
    REGISTRY-ARTIFACT channel, and they are the cases that decide whether the
    report is readable. The corpus is private and the baseline that recorded the
    eight is not in this repository; what is here is one test per shape.
    """

    def test_a_collective_author_is_not_an_author_count_defect(self) -> None:
        """key2002hormones: "The Endogenous Hormones and Breast Cancer Collaborative Group".

        Split on " and " it becomes two people and the entry reports a
        two-versus-five author-count defect on a correct bibliography.
        """
        ref = make_ref(
            key="key2002hormones",
            authors=parse_name_list(
                "The Endogenous Hormones and Breast Cancer Collaborative Group"
            ),
        )
        record = make_record(
            authors=[Name(family="Key"), Name(family="Appleby"), Name(family="Barnes")]
        )
        result = compare(ref, {"crossref": record})
        assert errors(result, "authors") == []
        assert not result.fails
        assert artifacts(result, "authors") == ["collective author"]
        # A count warning would make the verdict INCOMPLETE and put the entry in
        # the report anyway, which is the false alarm this is here to prevent.
        assert not [i for i in result.issues if i.field == "authors"]

    def test_and_others_is_not_a_missing_author(self) -> None:
        """maciamartinez2020bifap: BibTeX's `and others` is et al., not a person."""
        ref = make_ref(
            key="maciamartinez2020bifap",
            authors=parse_name_list("Macía-Martínez, Miguel and others"),
        )
        record = make_record(
            authors=[Name(family="Macía-Martínez"), Name(family="Gil"), Name(family="Huerta")]
        )
        result = compare(ref, {"crossref": record})
        assert errors(result, "authors") == []
        assert not any(i.field == "authors" for i in result.issues)

    def test_registry_mojibake_surnames_are_artifacts_not_defects(self) -> None:
        """papantoniou2017colorectal: Crossref returns UTF-8 decoded as Latin-1.

        The registry surnames below are mangled on purpose — repairing them in
        this fixture would remove the only reason the test exists.
        """
        record = make_record(
            authors=[
                Name(family="Papantoniou", given="Kyriaki"),
                Name(family="AragonÃ©s", given="Nuria"),  # Aragonés
                Name(family="PÃ©rez-GÃ³mez", given="Beatriz"),  # Pérez-Gómez
            ]
        )
        result = compare(make_ref(), {"crossref": record})
        assert errors(result, "authors") == []
        assert artifacts(result, "authors") == ["registry mojibake"] * 2
        assert not result.fails

    def test_the_report_shows_the_damaged_glyphs_not_the_comparison_key(self) -> None:
        """Printing the folded value would hide the glyph that caused the finding.

        `AragonÃ©s` folds to "aragona s"; a report showing that instead of the
        registry's actual bytes tells the reader nothing about what is wrong.
        """
        record = make_record(
            authors=[
                Name(family="Papantoniou", given="Kyriaki"),
                Name(family="AragonÃ©s", given="Nuria"),  # mojibake, deliberately
                Name(family="Pérez-Gómez", given="Beatriz"),
            ]
        )
        result = compare(make_ref(), {"crossref": record})
        shown = [i.registry for i in result.suppressed if i.field == "authors"]
        assert any("AragonÃ©s" in value for value in shown)
        assert not any("aragona s" in value for value in shown)

    def test_an_invented_coauthor_is_still_a_defect(self) -> None:
        """The failure mode a first-author-only check cannot see."""
        ref = make_ref(
            authors=[
                Name(family="Papantoniou", given="Kyriaki"),
                Name(family="Fabricated", given="Person"),
                Name(family="Pérez-Gómez", given="Beatriz"),
            ]
        )
        result = compare(ref, {"crossref": make_record()})
        assert errors(result, "authors") == ["mismatch"]
        assert result.fails

    def test_a_genuinely_different_surname_is_still_a_defect(self) -> None:
        """Mojibake handling must not become "any unfamiliar surname agrees"."""
        record = make_record(
            authors=[
                Name(family="Papantoniou", given="Kyriaki"),
                Name(family="Gutiérrez", given="Nuria"),
                Name(family="Pérez-Gómez", given="Beatriz"),
            ]
        )
        result = compare(make_ref(), {"crossref": record})
        assert errors(result, "authors") == ["mismatch"]

    def test_a_registry_omitting_the_first_author_is_not_a_defect(self) -> None:
        """clavelchapelon1997e3n: Crossref's byline starts at the second author.

        The record is 10.1097/00008469-199710000-00007 (Eur J Cancer Prev 1997)
        and it is read here from the response recorded in ``tests/data``: nine
        creators opening with `van Liere`, whom the deposit itself marks
        `"sequence": "first"`, against the entry's correct ten opening with
        Clavel-Chapelon. Compared position against position that is ten
        consecutive "wrong person" errors and a failing build on an entry with
        nothing wrong with it.
        """
        ref = make_ref(
            key="clavelchapelon1997e3n",
            doi="10.1097/00008469-199710000-00007",
            title="E3N, a French cohort study on cancer risk factors",
            authors=parse_name_list(
                "Clavel-Chapelon, F and van Liere, M J and Giubout, C "
                "and Niravong, M Y and Goulard, H and Corre, C Le and Hoang, L A "
                "and Amoyel, J and Auquier, A and Duquesnel, E"
            ),
            year=1997,
            container="European Journal of Cancer Prevention",
            volume="6",
            issue="5",
            pages="473-478",
        )
        record = make_record(
            doi="10.1097/00008469-199710000-00007",
            title="E3N, a French cohort study on cancer risk factors",
            authors=crossref_authors("first_author_omitted"),
            years={"print": 1997},
            container="European Journal of Cancer Prevention",
            volume="6",
            issue="5",
            pages="473-478",
        )
        result = compare(ref, {"crossref": record})
        assert errors(result, "authors") == []
        assert not result.fails
        assert artifacts(result, "authors") == ["registry omits the first author"]
        # A count warning would make the verdict INCOMPLETE and put a correct
        # entry in the report anyway, which is the false alarm this prevents.
        assert not [i for i in result.issues if i.field == "authors"]
        assert result.verdict == "REGISTRY-ARTIFACT"
        # Suppressed is not silent, and that is what keeps this rule honest: the
        # difference is still printed, with both names, under REGISTRY-ARTIFACT.
        # A reader who thinks Crossref is right can see exactly which creator is
        # in dispute without re-running anything.
        omission = next(i for i in result.suppressed if i.field == "authors")
        assert "Clavel-Chapelon" in omission.stored
        assert "van Liere" in omission.registry

    def test_a_bibliography_that_gained_a_first_author_is_still_a_defect(self) -> None:
        """The counter-test for the rule above, and the reason it is narrow.

        A reference list that prepends a plausible senior author to an otherwise
        correct byline produces the *same shape* as the E3N deposit, and no
        registry record can tell them apart. What separates them here is
        arithmetic the tool can check: three creators must survive the
        alignment. With only two the tool reports and lets a human decide,
        rather than clearing an invented attribution.
        """
        ref = make_ref(
            authors=[
                Name(family="Invented", given="Senior"),
                Name(family="Papantoniou", given="Kyriaki"),
                Name(family="Aragonés", given="Nuria"),
            ]
        )
        record = make_record(
            authors=[
                Name(family="Papantoniou", given="Kyriaki"),
                Name(family="Aragonés", given="Nuria"),
            ]
        )
        result = compare(ref, {"crossref": record})
        assert errors(result, "authors") == ["mismatch"]
        assert result.fails

    def test_a_registry_missing_an_interior_author_is_still_a_defect(self) -> None:
        """Only a run from the front is alignment; a hole in the middle is not.

        Without this the rule would be "ignore any length difference of one",
        which silences an invented co-author — the documented failure mode of a
        generated bibliography, and the thing the full-list comparison exists
        for.
        """
        ref = make_ref(
            authors=parse_name_list(
                "Clavel-Chapelon, F and van Liere, M J and Giubout, C "
                "and Niravong, M Y and Goulard, H"
            )
        )
        registry = crossref_authors("first_author_omitted")
        record = make_record(
            # Clavel-Chapelon restored at the head, `Giubout` dropped from the
            # middle: same lengths as the accepted case, different defect.
            authors=[Name(family="Clavel-Chapelon", given="F"), *registry[:1], *registry[2:4]]
        )
        result = compare(ref, {"crossref": record})
        # The shift past the hole is partly absorbed by the reordering rule —
        # `Niravong` really does appear on both sides — so what survives is one
        # positional mismatch. One error is all it takes: the entry is reported
        # and the build fails, which is the outcome this test is about.
        assert errors(result, "authors") == ["mismatch"]
        assert result.fails

    def test_a_registry_surname_missing_its_first_letter_is_not_a_defect(self) -> None:
        """papantoniou2017colorectal: `ierssen` for Dierssen, past repairing.

        Crossref's record for 10.5271/sjweh.3626 is UTF-8 decoded as Latin-1
        throughout. Round-trip repair recovers `AragonÃ©s`, `PÃ©rez-GÃ³mez` and
        `GarcÃ­a-Palomo`; position 19 lost its first character outright and
        there is no byte left to repair. The whole recorded byline is used here
        rather than a two-name excerpt, because the corroborating evidence —
        proven mis-decoding elsewhere in the *same* deposit — is what makes the
        suppression defensible, and an excerpt would not carry it.
        """
        ref = make_ref(
            authors=parse_name_list(
                "Papantoniou, Kyriaki and Castaño-Vinyals, Gemma and Espinosa, Ana "
                "and Turner, Michelle C and Alonso-Aguado, Maria Henar "
                "and Martin, Vicente and Aragonés, Nuria and Pérez-Gómez, Beatriz "
                "and Pozo, Benito Mirón and Gómez-Acebo, Inés and Ardanaz, Eva "
                "and Altzibar, Jone M and Peiro, Rosana and Tardon, Adonina "
                "and Lorca, José Andrés and Chirlaque, Maria Dolores "
                "and García-Palomo, Andrés and Jimenez-Moleon, Jose Juan "
                "and Dierssen, Trinidad and Ederra, Maria and Amiano, Pilar "
                "and Pollan, Marina and Moreno, Victor and Kogevinas, Manolis"
            )
        )
        record = make_record(authors=crossref_authors("mojibake_author_list"))
        result = compare(ref, {"crossref": record})
        assert errors(result, "authors") == []
        assert not result.fails
        assert not [i for i in result.issues if i.field == "authors"]
        # Five surnames the round trip repairs, then the one it cannot. Spelling
        # the count out keeps this from passing if the truncation rule widened
        # to swallow names ordinary demojibake already handles.
        assert artifacts(result, "authors") == (
            ["registry mojibake"] * 5 + ["registry mojibake truncated the surname"]
        )
        # And the damaged bytes reach the report, not the comparison key.
        truncation = result.suppressed[-1]
        assert truncation.stored.startswith("Dierssen")
        assert truncation.registry.startswith("ierssen")

    def test_a_capitalised_surname_one_letter_shorter_is_still_a_defect(self) -> None:
        """`Rice` is not *Price*, however mangled the rest of the deposit is.

        The byline below is provably mis-decoded — `AragonÃ©s` round-trips to
        *Aragonés*, which the entry holds — and that used to be enough to accept
        any surname that was the stored one minus a leading character. Real
        surname pairs one leading character apart are everywhere:
        `Rice`/*Price*, `Ross`/*Gross*, `Handler`/*Chandler*. What tells them
        from the witnessed `ierssen`/*Dierssen* is that Crossref deposits a
        different person's surname capitalised, and deposited the damaged one in
        lower case.
        """
        ref = make_ref(
            authors=[
                Name(family="Price", given="Robert"),
                Name(family="Papantoniou", given="Kyriaki"),
                Name(family="Aragonés", given="Nuria"),
            ]
        )
        record = make_record(
            authors=[
                Name(family="Rice", given="Robert"),
                Name(family="Papantoniou", given="Kyriaki"),
                Name(family="AragonÃ©s", given="Nuria"),  # mojibake, deliberately
            ]
        )
        result = compare(ref, {"crossref": record})
        assert errors(result, "authors") == ["mismatch"]
        assert result.fails

    def test_a_byline_in_another_script_does_not_clear_a_different_byline(self) -> None:
        """`fold()` reduces 王, 李 and 张 to nothing, which is not "no surname".

        Read as "no surname", every one of them agreed with every Latin name
        there is, and the entry below — three creators who share nothing with
        the record — came back as a clean REGISTRY-ARTIFACT. The forename
        initial is what survives romanisation and is what now carries the
        finding.
        """
        ref = make_ref(
            authors=[
                Name(family="Smith", given="John"),
                Name(family="Jones", given="Alice"),
                Name(family="Brown", given="Bob"),
            ]
        )
        record = make_record(
            authors=[
                Name(family="王", given="Lei"),
                Name(family="李", given="Ming"),
                Name(family="张", given="Na"),
            ]
        )
        result = compare(ref, {"crossref": record})
        assert errors(result, "authors") == ["mismatch"] * 3
        assert result.fails

    def test_a_registry_byline_of_nameless_creators_does_not_align_away_a_count(self) -> None:
        """The first-author-omission rule counts agreements it never earned.

        Three creators the registry left without surnames agree with anything,
        and three agreements are exactly what the rule reads as proof that the
        deposit dropped its opening author — so a four-against-three count
        difference disappeared as well.
        """
        ref = make_ref(
            authors=parse_name_list("Alpha, A and Bravo, B and Charlie, C and Delta, D")
        )
        record = make_record(
            authors=[Name(given="B"), Name(given="C"), Name(given="D")]
        )
        result = compare(ref, {"crossref": record})
        assert "registry omits the first author" not in artifacts(result, "authors")
        # The count difference is back, and the positional comparison runs
        # against the real offsets instead of the aligned-away ones.
        assert "count" in [i.kind for i in result.issues if i.field == "authors"]
        assert result.fails

    def test_a_lost_first_letter_in_a_clean_byline_is_still_a_defect(self) -> None:
        """The counter-test the truncation rule rests on.

        Nothing in this byline is mis-decoded, so nothing corroborates the
        missing letter and `ash` is simply not *Nash*. If this stops firing, the
        rule has become a global "accept a surname missing its first character",
        which would clear `Reid` against *Freid* and every case like it.
        """
        ref = make_ref(
            authors=[
                Name(family="Nash", given="John"),
                Name(family="Papantoniou", given="Kyriaki"),
                Name(family="Aragonés", given="Nuria"),
            ]
        )
        record = make_record(
            authors=[
                Name(family="ash", given="John"),
                Name(family="Papantoniou", given="Kyriaki"),
                Name(family="Aragonés", given="Nuria"),
            ]
        )
        result = compare(ref, {"crossref": record})
        assert errors(result, "authors") == ["mismatch"]
        assert result.fails


class TestCosmeticGlyphs:
    def test_a_curly_apostrophe_is_never_labelled_a_registry_artifact(self) -> None:
        """clean() regularises the glyph, so there is no difference to explain.

        Reporting it as a suppressed registry defect would inflate the "taken on
        trust" count with three entries that agree perfectly.
        """
        # U+2019 is written as an escape so that no editor, and no future
        # "straighten the quotes" pass, can quietly turn it into the ASCII
        # apostrophe and leave the test comparing a string with itself.
        ref = make_ref(title="Alcohol intake and Parkinson\u2019s disease risk")
        record = make_record(title="Alcohol intake and Parkinson's disease risk")
        result = compare(ref, {"crossref": record})
        assert result.verdict == "OK"
        assert not result.suppressed
