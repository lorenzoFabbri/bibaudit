"""Known registry defects, and the true positives that look like them.

``CLAUDE.md`` asks for two tests per rule in :mod:`bibaudit.benign`: one proving
the documented false positive is suppressed as ``REGISTRY-ARTIFACT``, and one
proving that a true positive which superficially resembles it still fires. The
second is the one that earns its keep — a suppression with no counter-test is
indistinguishable from a check somebody switched off, and this module is the
only thing standing between the user and a report full of false alarms.

Every case is taken from ``docs/registry-artifacts.md`` or from the 438-entry
corpus whose expected baseline ``TODO.md`` records. Several strings below are
*damaged on purpose*; the comments say which, because they are the point of the
test and cleaning them up would delete it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bibaudit import benign
from bibaudit.compare import compare
from bibaudit.model import Name, Record, Reference, Result
from bibaudit.names import parse_name_list
from bibaudit.normalize import clean

TITLE = "Shift work and colorectal cancer risk in the MCC-Spain case-control study"

#: The prose that has to name every rule in :mod:`bibaudit.benign`. Located from
#: this file rather than the working directory so the test does not depend on
#: where pytest was invoked from.
ARTIFACT_DOCS = Path(__file__).resolve().parents[1] / "docs" / "registry-artifacts.md"

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

    def test_a_dropped_leading_article_is_accepted(self) -> None:
        assert classify("container", "Lancet", "The Lancet", container_short=None) == (
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
        to ``CHECKS`` without a section in ``docs/registry-artifacts.md`` naming
        it turns this test red, which is the only automatic reminder there is.
        """
        prose = ARTIFACT_DOCS.read_text(encoding="utf-8")
        missing = [c.__name__ for c in benign.CHECKS if c.__name__ not in prose]
        assert missing == []


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
    """The eight author flags TODO.md records as false positives on the real corpus.

    Author differences are adjudicated in :mod:`bibaudit.names` rather than by a
    rule in :mod:`bibaudit.benign`, but they surface through the same
    REGISTRY-ARTIFACT channel, and they are the cases that decide whether the
    report is readable.
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
