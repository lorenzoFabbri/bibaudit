"""Author parsing and comparison.

Every case here comes from a real bibliography where a naive comparison reported
a defect that was not there. These are the tests that keep the tool usable: an
author check that fires on correct entries is worse than no author check.

Two of those rules — an omitted leading author, and a surname truncated inside a
byline that is provably mojibake — make the tool complain *less*, and a
suppression that is a little too wide produces a clean report, which is what
everybody wants to see and nobody questions. Each therefore comes in a pair:
the recorded registry response that motivated it, read from ``tests/data`` so
the test runs offline, and a case of the same superficial shape *without* the
corroborating evidence, which must still be reported.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bibaudit.model import Name
from bibaudit.names import (
    compare_author_lists,
    demojibake,
    family_key,
    names_agree,
    parse_name,
    parse_name_list,
)
from bibaudit.normalize import clean

_DATA = Path(__file__).parent / "data"


def crossref_authors(case: str) -> list[Name]:
    """Creators from a verbatim Crossref response recorded in ``tests/data``.

    The conversion mirrors ``registries.crossref._parse_creators`` — ``family``
    and ``given``, put through ``clean``, order preserved — rather than calling
    it, so a test about :mod:`bibaudit.names` fails only when
    :mod:`bibaudit.names` is wrong. What the fixture supplies is the part no
    hand-written literal can: the registry's own bytes, mojibake included.
    """
    with (_DATA / f"names_crossref_{case}.json").open(encoding="utf-8") as handle:
        work = json.load(handle)["message"]
    return [
        Name(family=clean(person.get("family", "")), given=clean(person.get("given", "")))
        for person in work["author"]
    ]


class TestCollectiveAuthors:
    def test_collaboration_name_is_one_author_not_two(self) -> None:
        """"The Endogenous Hormones and Breast Cancer Collaborative Group".

        Splitting on " and " turns this into two people and produces a
        two-versus-one author-count defect on a correct entry.
        """
        names = parse_name_list("The Endogenous Hormones and Breast Cancer Collaborative Group")
        assert len(names) == 1
        assert names[0].collective
        assert "Collaborative Group" in names[0].literal

    def test_brace_protected_name_is_collective(self) -> None:
        names = parse_name_list("{World Health Organization}")
        assert len(names) == 1
        assert names[0].collective

    def test_a_real_two_author_field_still_splits(self) -> None:
        names = parse_name_list("Malats, Núria and Real, Francisco X")
        assert [n.family for n in names] == ["Malats", "Real"]

    def test_person_at_an_institute_is_not_collective(self) -> None:
        """The marker word alone must not trigger it; the comma says it is a person."""
        names = parse_name_list("Smith, John")
        assert not names[0].collective


class TestEtAl:
    def test_and_others_is_a_truncation_marker(self) -> None:
        """BibTeX's `and others` is et al., not a person named "others"."""
        names = parse_name_list("Gil, Miguel and Huerta, Consuelo and others")
        assert len(names) == 3
        assert names[-1].et_al

    def test_truncated_lists_do_not_report_a_count_difference(self) -> None:
        stored = parse_name_list("Gil, Miguel and others")
        registry = [Name(family="Gil"), Name(family="Huerta"), Name(family="Montero")]
        diff = compare_author_lists(stored, registry)
        assert not diff.count_differs
        assert not diff.mismatches

    @pytest.mark.parametrize(
        ("cell", "surname", "marker"),
        [
            ("Gomez-Rubio et al.", "Gomez-Rubio", "et al."),
            ("Smith et al", "Smith", "et al"),
            ("Smith, A., et al.", "Smith", "et al."),
        ],
    )
    def test_a_trailing_et_al_is_the_same_marker_as_and_others(
        self, cell: str, surname: str, marker: str
    ) -> None:
        """A display byline writes the marker joined; BibTeX writes it split.

        ``Gomez-Rubio et al.`` is how the Quarto tables in
        ``sources/pangeneu.qmd`` (10.1093/annonc/mdx167) record a byline, and
        442 rows across that corpus are written this way. Parsed as one
        creator, the *surname* compared against Crossref was literally ``al.``
        and the list length was compared as though it were complete — an author
        FIELD-MISMATCH on every correct row.
        """
        names = parse_name_list(cell)

        assert [n.family for n in names[:-1]] == [surname]
        assert names[-1].et_al
        assert names[-1].literal == marker

    def test_a_surname_that_merely_contains_those_letters_is_untouched(self) -> None:
        """The true positive: ``et`` and ``al`` have to be whole tokens.

        Stripping on the letters alone would eat a real surname, and the entry
        would then be compared as a truncated list — a count difference nobody
        would ever be told about, which is the silent direction.
        """
        assert [n.family for n in parse_name_list("Etal, John")] == ["Etal"]
        assert [n.family for n in parse_name_list("Alal and Betal")] == ["Alal", "Betal"]
        assert not any(n.et_al for n in parse_name_list("Alal and Betal"))

    def test_a_cell_holding_only_the_marker_is_still_one_marker(self) -> None:
        names = parse_name_list("et al.")
        assert len(names) == 1
        assert names[0].et_al


class TestAmpersandSeparator:
    """A table cell separates creators with ``&``; a ``.bib`` field with ``and``.

    ``adapters/markdown`` hands this parser both, so both are read.
    """

    def test_an_ampersand_separates_two_creators(self) -> None:
        """``Riboli & Kaaks`` — ``sources/epic.qmd``, 10.1093/ije/26.suppl_1.s6.

        Parsed as one creator it became family ``Kaaks``, given ``Riboli &``:
        a correct two-author row reported as a single mis-spelled person.
        """
        names = parse_name_list("Riboli & Kaaks")

        assert [n.family for n in names] == ["Riboli", "Kaaks"]

    def test_an_ampersand_between_inverted_names_splits_the_same_way(self) -> None:
        names = parse_name_list("Smith, A. & Jones, B.")

        assert [(n.family, n.given) for n in names] == [("Smith", "A."), ("Jones", "B.")]

    def test_a_collective_containing_an_ampersand_is_still_one_creator(self) -> None:
        """The true positive for the new separator.

        An organisation whose own name contains ``&`` must not be split into
        two people, exactly as one containing ``and`` must not be — that is the
        rule CLAUDE.md states, arriving through a second separator.
        """
        names = parse_name_list("Ministry of Health & Social Care")

        assert len(names) == 1
        assert names[0].collective


class TestSurnameForms:
    def test_particles_stay_with_the_surname(self) -> None:
        name = parse_name("Casper H. J. van Eijck")
        assert name.family == "van Eijck"
        assert name.given == "Casper H. J."

    def test_particle_filing_difference_is_accepted(self) -> None:
        """Registries disagree about whether "van Eijck" files under V or E."""
        agreed, reason = names_agree(Name(family="van Eijck"), Name(family="Eijck"))
        assert agreed
        assert reason

    def test_hyphenated_surname_is_one_unit(self) -> None:
        name = parse_name("Clavel-Chapelon, F")
        assert family_key(name) == "clavel chapelon"

    def test_compound_surname_shortened_by_the_registry_is_accepted(self) -> None:
        agreed, _ = names_agree(Name(family="Clavel-Chapelon"), Name(family="Chapelon"))
        assert agreed

    def test_a_registry_that_glued_the_forename_into_the_surname_is_accepted(self) -> None:
        """Crossref's defect on 10.1007/s10689-024-00397-w (marianinirios2024risk).

        The deposit puts the whole name in `family`: `Cristina-Marianini-Rios`.
        The stored surname is a token-level suffix of it.
        """
        agreed, reason = names_agree(
            Name(family="Marianini-Rios", given="Cristina"),
            Name(family="Cristina-Marianini-Rios", given="Cristina"),
        )
        assert agreed
        assert reason == "compound surname shortened"

    @pytest.mark.parametrize(
        ("stored", "registry"),
        [
            # Both people appear in the 438-entry corpus, under different DOIs.
            ("Krebs-Smith", "Davey Smith"),
            ("González-González", "Martínez-González"),
            ("Gómez-Rubio", "Fernández-Rubio"),
        ],
    )
    def test_two_people_sharing_a_final_surname_are_different_people(
        self, stored: str, registry: str
    ) -> None:
        """A shared maternal surname is not evidence of the same person.

        The escape used to accept any two surnames whose *final tokens* matched
        so long as either was compound — which is the "reduce a surname to its
        last token" rule `CLAUDE.md` forbids, wearing a different name.
        `tests/test_audit_corpus.py` counts 255 such pairs in the corpus.
        Requiring a token-level suffix separates them without costing
        `Clavel-Chapelon`/`Chapelon` above. Initials are made to agree here
        because a registry giving a single initial is the normal case and must
        not be what rescues the comparison.
        """
        agreed, _ = names_agree(
            Name(family=stored, given="S"), Name(family=registry, given="S")
        )
        assert not agreed

    def test_forenames_are_compared_only_by_initial(self) -> None:
        """"E", "Esther" and "Esther M." are one person in three registries."""
        agreed, _ = names_agree(
            Name(family="Molina-Montes", given="Esther"),
            Name(family="Molina-Montes", given="E"),
        )
        assert agreed

    def test_genuinely_different_surnames_disagree(self) -> None:
        agreed, _ = names_agree(Name(family="Malats"), Name(family="Thompson"))
        assert not agreed


class TestSpellingVariants:
    """"Spelling variant with matching initials" is a suppression like any other.

    It carries **no witnessed instance** — no entry in the 438-entry corpus
    reaches it, and `TODO.md`'s eight author flags are all accounted for by the
    collective, et-al, first-author-omission and mojibake rules. It survives
    only for the transliteration variants a multilingual bibliography does
    produce, and it is held to the same bar as every other rule that makes the
    tool complain less: the shape without the evidence has to still fire.
    """

    @pytest.mark.parametrize(
        ("stored", "registry"),
        [
            # One edit apart and four characters long: two of the commonest
            # surnames in the literature, and two different families.
            ("Chang", "Chan"),
            ("Wang", "Wan"),
            ("Liu", "Lin"),
            # Two edits, and a shared three-character prefix is all the old rule
            # asked for. `Martin` and `Martinez` are not one name misspelled.
            ("Martinez", "Martin"),
            ("Smith", "Smithers"),
            ("Gonzalez", "Gonzalo"),
            ("Sanchez", "Sancho"),
        ],
    )
    def test_a_shared_prefix_is_not_a_spelling_variant(self, stored: str, registry: str) -> None:
        agreed, reason = names_agree(
            Name(family=stored, given="J"), Name(family=registry, given="J")
        )
        assert not agreed, reason

    @pytest.mark.parametrize(
        ("stored", "registry"),
        [
            # A leading-character difference is the damage
            # `_surname_truncated_by_mojibake` handles under evidence. Admitting
            # it here, for free, would clear these with no evidence at all.
            ("Sherman", "Herman"),
            ("Grossman", "Rossman"),
            ("Chandler", "Handler"),
        ],
    )
    def test_a_lost_leading_character_is_never_a_spelling_variant(
        self, stored: str, registry: str
    ) -> None:
        agreed, reason = names_agree(
            Name(family=stored, given="R"), Name(family=registry, given="R")
        )
        assert not agreed, reason

    @pytest.mark.parametrize(
        ("stored", "registry"),
        [
            # Slavic gendered endings and a dropped vowel in a transliteration:
            # one edit, the same first letter, long enough that one edit is not
            # another family.
            ("Kowalski", "Kowalska"),
            ("Ivanova", "Ivanov"),
            ("Papantoniou", "Papantoniu"),
        ],
    )
    def test_a_one_character_variant_of_a_long_surname_is_still_accepted(
        self, stored: str, registry: str
    ) -> None:
        agreed, reason = names_agree(
            Name(family=stored, given="A"), Name(family=registry, given="A")
        )
        assert agreed
        assert reason == "spelling variant with matching initials"

    def test_disagreeing_initials_defeat_it(self) -> None:
        agreed, _ = names_agree(
            Name(family="Kowalski", given="Anna"), Name(family="Kowalska", given="Piotr")
        )
        assert not agreed


class TestSurnamesOutsideTheComparisonAlphabet:
    """`fold()` keeps only `[a-z0-9]`, so a non-Latin surname folds to nothing.

    "Nothing" was then read as "this creator has no surname", which
    `names_agree` accepts — so every surname written in Han, Greek, Cyrillic,
    Hangul, Kana or Arabic agreed with every other name in the world, and three
    of them in a row were enough to satisfy the alignment arithmetic in
    `_registry_omits_first_author`.
    """

    @pytest.mark.parametrize("surname", ["王", "李", "Παπαδόπουλος", "Иванов", "الحسن", "김"])
    def test_the_comparison_key_really_is_empty(self, surname: str) -> None:
        """Pins the premise: without it every test in this class proves nothing."""
        assert family_key(Name(family=surname)) == ""

    def test_two_different_han_surnames_are_reported(self) -> None:
        """王 is not 李, and a checker that says otherwise is not checking."""
        agreed, _ = names_agree(Name(family="王"), Name(family="李"))
        assert not agreed

    def test_the_same_han_surname_agrees(self) -> None:
        agreed, reason = names_agree(Name(family="王", given="L"), Name(family="王", given="L"))
        assert agreed
        assert reason == ""

    def test_a_native_form_against_a_romanised_one_is_accepted_and_stated(self) -> None:
        """No key can bridge 山田 and Yamada, so the pair is accepted — out loud.

        Reporting it would fail every entry in a Japanese, Korean or Russian
        bibliography whose registry deposit keeps the original script, which is
        a false-alarm machine. The reason string is what keeps it honest: the
        difference is printed under REGISTRY-ARTIFACT rather than passed over.
        """
        agreed, reason = names_agree(
            Name(family="Yamada", given="Taro"), Name(family="山田", given="Taro")
        )
        assert agreed
        assert reason == "surname outside the comparison alphabet"

    def test_a_middle_initial_the_registry_drops_is_not_a_disagreement(self) -> None:
        """Only the *first* initial is compared, or the escape becomes a check."""
        agreed, _ = names_agree(
            Name(family="Yamada", given="Taro K"), Name(family="山田", given="Taro")
        )
        assert agreed

    def test_an_unrelated_creator_behind_an_unreadable_surname_is_reported(self) -> None:
        """The forename initial survives romanisation and is the evidence left.

        Without it, `Smith, John` compared clean against `王, Lei` and a
        completely different byline was suppressed as a registry artifact.
        """
        agreed, _ = names_agree(
            Name(family="Smith", given="John"), Name(family="王", given="Lei")
        )
        assert not agreed

    def test_a_wholly_unrelated_byline_in_another_script_is_reported(self) -> None:
        stored = parse_name_list("Smith, John and Jones, Alice and Brown, Bob")
        registry = [
            Name(family="王", given="Lei"),
            Name(family="李", given="Ming"),
            Name(family="张", given="Na"),
        ]
        diff = compare_author_lists(stored, registry)
        assert len(diff.mismatches) == 3
        assert not diff.clean


class TestMojibake:
    @pytest.mark.parametrize(
        ("broken", "expected"),
        [("GÃ³mez", "Gómez"), ("AragonÃ©s", "Aragonés"), ("MuÃ±oz", "Muñoz")],
    )
    def test_latin1_misdecoding_round_trips(self, broken: str, expected: str) -> None:
        """Crossref returns UTF-8 decoded as Latin-1 for some deposits."""
        repaired, was_mojibake = demojibake(broken)
        assert was_mojibake
        assert repaired == expected

    def test_ordinary_text_is_left_alone(self) -> None:
        repaired, was_mojibake = demojibake("Gómez")
        assert not was_mojibake
        assert repaired == "Gómez"

    def test_mojibake_surname_is_not_reported_as_a_mismatch(self) -> None:
        agreed, reason = names_agree(Name(family="Gómez"), Name(family="GÃ³mez"))
        assert agreed
        assert reason == "registry mojibake"

    def test_surname_is_never_reduced_to_its_last_token(self) -> None:
        """`AragonÃ©s` folds to "aragona s"; taking the last token yields "s".

        That is how a mojibake surname silently becomes a one-letter phantom
        mismatch, so the whole family name is compared.
        """
        assert family_key(Name(family="AragonÃ©s")) != "s"

    def test_mojibake_flattened_by_nfkc_is_still_repaired(self) -> None:
        """`clean()` runs NFKC, which destroys the second half of some pairs.

        *Gómez* mis-decoded is `GÃ³mez`, but superscript three is one of the
        nine Latin-1 characters NFKC rewrites to ASCII, so what reaches this
        module is `GÃ3mez` — which no longer encodes to valid UTF-8. Crossref's
        record for 10.5271/sjweh.3626 carries exactly this at position 8, and
        without the inverse the entry `papantoniou2017colorectal` is reported
        for a surname it has right.
        """
        damaged = clean("PÃ©rez-GÃ³mez")
        # clean() has already flattened the pair; the test would prove nothing
        # if it had not.
        assert "3" in damaged
        repaired, was_mojibake = demojibake(damaged)
        assert was_mojibake
        assert repaired == "Pérez-Gómez"

    @pytest.mark.parametrize("surname", ["Ñoriega", "Ñuñez", "Ñíguez"])
    def test_an_enye_surname_is_not_rewritten_by_the_inverse(self, surname: str) -> None:
        """The NFKC inverse must not reach past the Latin-1 Supplement leads.

        `Ñ` is an ordinary Spanish letter as well as the first character of a
        mis-decoded Cyrillic pair. Substituting after it turns `Ñoriega` into a
        byte pair that happens to decode, which would invent a repair where the
        plain round trip correctly refuses one.
        """
        repaired, was_mojibake = demojibake(surname)
        assert not was_mojibake
        assert repaired == surname


class TestAuthorListComparison:
    def test_identical_lists_are_clean(self) -> None:
        stored = parse_name_list("Malats, Núria and Real, Francisco X")
        registry = [Name(family="Malats", given="Núria"), Name(family="Real", given="Francisco X")]
        assert compare_author_lists(stored, registry).clean

    def test_an_invented_coauthor_is_caught(self) -> None:
        """The failure mode a first-author-only check cannot see."""
        stored = parse_name_list("Malats, N and Invented, Person and Real, F")
        registry = [Name(family="Malats"), Name(family="Fabbri"), Name(family="Real")]
        diff = compare_author_lists(stored, registry)
        assert diff.mismatches
        assert diff.mismatches[0][0] == 2

    def test_reordering_is_distinguished_from_substitution(self) -> None:
        stored = [Name(family="Real"), Name(family="Malats")]
        registry = [Name(family="Malats"), Name(family="Real")]
        diff = compare_author_lists(stored, registry)
        assert not diff.mismatches
        assert set(diff.reasons.values()) == {"reordered"}

    def test_count_difference_is_reported(self) -> None:
        stored = [Name(family="Malats"), Name(family="Real")]
        registry = [Name(family="Malats")]
        assert compare_author_lists(stored, registry).count_differs

    def test_registry_collective_against_a_member_list_is_not_a_defect(self) -> None:
        """Crossref splits some consortium bylines; the bibliography keeps the group."""
        stored = [Name(family="Smith"), Name(family="Jones")]
        registry = [Name(literal="The Study Group", collective=True)]
        diff = compare_author_lists(stored, registry)
        assert not diff.mismatches
        assert not diff.count_differs


#: The byline of `clavelchapelon1997e3n` exactly as the corpus stores it. Ten
#: creators; Crossref's deposit for the same DOI holds the last nine.
E3N_STORED = (
    "Clavel-Chapelon, F and van Liere, M J and Giubout, C and Niravong, M Y "
    "and Goulard, H and Corre, C Le and Hoang, L A and Amoyel, J "
    "and Auquier, A and Duquesnel, E"
)


class TestRegistryOmittingTheFirstAuthor:
    """10.1097/00008469-199710000-00007 — Crossref's byline starts one name late.

    The paper is Clavel-Chapelon et al., *E3N, a French cohort study on cancer
    risk factors*, Eur J Cancer Prev 1997. Ovid/Wolters Kluwer deposited nine
    creators beginning with `van Liere`, whom the deposit even marks
    `"sequence": "first"`. Compared position against position, a correct
    ten-author entry produces ten "different person" errors and fails as
    FIELD-MISMATCH. `TODO.md`'s acceptance baseline lists it among the eight
    author flags that are all false positives.
    """

    def test_the_recorded_deposit_still_carries_the_omission(self) -> None:
        """Pins the fixture: without the defect the tests below prove nothing.

        A future pass that "corrects" `tests/data` would leave every assertion
        in this class passing for the wrong reason.
        """
        registry = crossref_authors("first_author_omitted")
        assert len(registry) == 9
        assert registry[0].family == "van Liere"
        assert not any(name.family == "Clavel-Chapelon" for name in registry)

    def test_the_omission_is_recognised_and_the_rest_aligns(self) -> None:
        diff = compare_author_lists(
            parse_name_list(E3N_STORED), crossref_authors("first_author_omitted")
        )
        assert diff.clean
        assert not diff.mismatches
        assert not diff.count_differs
        assert diff.reasons == {1: "registry omits the first author"}

    def test_an_author_missing_from_the_middle_is_still_reported(self) -> None:
        """Only a run from the front is alignment; a hole is a different animal.

        A registry list that skips an interior creator cannot be told from a
        bibliography that inserted one, and every creator after the hole is a
        genuine positional disagreement.
        """
        stored = parse_name_list(
            "Alpha, A and Bravo, B and Charlie, C and Delta, D and Echo, E"
        )
        registry = [
            Name(family="Alpha"), Name(family="Bravo"),
            Name(family="Delta"), Name(family="Echo"),
        ]
        diff = compare_author_lists(stored, registry)
        assert diff.mismatches
        assert not diff.clean

    def test_two_omitted_leading_authors_are_still_reported(self) -> None:
        """One is the only omission length with a witnessed instance.

        Tolerating a longer run would be a suppression with nothing behind it,
        and it is the same shape as a bibliography that prepended two authors
        who were never on the paper.
        """
        stored = parse_name_list(
            "Alpha, A and Bravo, B and Charlie, C and Delta, D and Echo, E"
        )
        registry = [Name(family="Charlie"), Name(family="Delta"), Name(family="Echo")]
        diff = compare_author_lists(stored, registry)
        assert diff.mismatches
        assert diff.count_differs

    def test_a_registry_sharing_only_a_minority_of_the_names_is_still_reported(self) -> None:
        stored = parse_name_list(
            "Alpha, A and Bravo, B and Charlie, C and Delta, D and Echo, E and Foxtrot, F"
        )
        registry = [Name(family="Echo"), Name(family="Foxtrot")]
        diff = compare_author_lists(stored, registry)
        assert diff.mismatches
        assert diff.count_differs

    def test_two_corroborating_names_are_not_enough(self) -> None:
        """Two surnames agreeing in sequence is what a companion paper produces.

        With only two names left after the omission there is too little
        evidence to prefer "the registry is short" over "the bibliography
        gained a first author", so the tool says so instead of choosing.
        """
        stored = parse_name_list("Alpha, A and Bravo, B and Charlie, C")
        registry = [Name(family="Bravo"), Name(family="Charlie")]
        diff = compare_author_lists(stored, registry)
        assert diff.mismatches
        assert not diff.clean

    def test_a_bibliography_missing_the_first_author_is_still_reported(self) -> None:
        """The mirror direction is a citation that has lost its first author.

        That is an attribution error a reader wants to see, not a registry
        defect, so the rule reads the registry side only.
        """
        stored = parse_name_list("Bravo, B and Charlie, C and Delta, D")
        registry = [
            Name(family="Alpha"), Name(family="Bravo"),
            Name(family="Charlie"), Name(family="Delta"),
        ]
        diff = compare_author_lists(stored, registry)
        assert diff.mismatches
        assert not diff.clean

    def test_a_repeated_leading_author_is_still_reported(self) -> None:
        """A duplicated name at the head of a hand-edited entry is a real error.

        Without the "the dropped surname appears nowhere in the registry list"
        condition, the duplicate aligns away and the entry reports clean.
        """
        stored = parse_name_list("Alpha, A and Alpha, A and Bravo, B and Charlie, C")
        registry = [Name(family="Alpha"), Name(family="Bravo"), Name(family="Charlie")]
        diff = compare_author_lists(stored, registry)
        assert not diff.clean

    @pytest.mark.parametrize(
        ("case", "registry"),
        [
            # A creator with no surname at all: a Crossref stub deposit, a
            # DataCite creator given only a forename.
            ("no surname", [Name(given="B"), Name(given="C"), Name(given="D")]),
            # A one-character surname, which `names_agree` accepts as "the
            # registry is incomplete" rather than as a contradiction.
            ("initials for surnames", [Name(family="B"), Name(family="C"), Name(family="D")]),
            # And surnames `fold()` cannot represent at all.
            (
                "another script",
                [
                    Name(family="王", given="B"),
                    Name(family="李", given="C"),
                    Name(family="张", given="D"),
                ],
            ),
        ],
    )
    def test_an_alignment_of_creators_that_compare_nothing_is_not_evidence(
        self, case: str, registry: list[Name]
    ) -> None:
        """Three agreements that compared nothing are three pieces of nothing.

        The rule counts `_MIN_ALIGNED_AFTER_OMISSION` creators agreeing in order
        as proof that the registry dropped its first author. `names_agree`
        returns agreement in several situations where nothing was compared, and
        the count used to include them: a registry byline of `[王, 李, 张]`
        "aligned" against `[Alpha, Bravo, Charlie]`, and the author-count
        difference was suppressed along with it.
        """
        stored = parse_name_list("Alpha, A and Bravo, B and Charlie, C and Delta, D")
        diff = compare_author_lists(stored, registry)
        assert diff.count_differs, case
        assert diff.reasons.get(1) != "registry omits the first author", case

    def test_a_prepended_senior_author_is_accepted_and_that_is_a_known_limit(self) -> None:
        """The hole this rule cannot close, pinned so it cannot widen unnoticed.

        Prepending a plausible senior name — `Riboli, E`, who ran EPIC and
        co-authored across these French cohorts — to the *recorded* E3N byline
        produces a list byte-identical in shape to the real deposit defect. Nine
        creators still align in exact order, because that is what an overlapping
        team looks like, and no author list can separate the two readings. The
        evidence that could lives outside this module: the citekey
        (`clavelchapelon1997e3n` names the creator the registry dropped) and the
        title comparison. Raising `_MIN_ALIGNED_AFTER_OMISSION` does not help —
        the real instance has nine.
        """
        registry = crossref_authors("first_author_omitted")
        diff = compare_author_lists([Name(family="Riboli", given="E"), *registry], registry)
        assert diff.reasons == {1: "registry omits the first author"}
        assert diff.clean

    def test_an_et_al_marker_voids_the_length_arithmetic(self) -> None:
        """Past `and others` the stored list is truncated and its length is void.

        Aligning against a placeholder would let the marker stand in for the
        registry's real creator and hide a genuine first-author difference.
        """
        stored = parse_name_list("Alpha, A and Bravo, B and Charlie, C and others")
        registry = [Name(family="Bravo"), Name(family="Charlie"), Name(family="Delta")]
        diff = compare_author_lists(stored, registry)
        assert diff.mismatches
        assert diff.mismatches[0][0] == 1


#: The byline of `papantoniou2017colorectal` exactly as the corpus stores it,
#: in the same order as Crossref's deposit for 10.5271/sjweh.3626.
MCC_SPAIN_STORED = (
    "Papantoniou, Kyriaki and Castaño-Vinyals, Gemma and Espinosa, Ana "
    "and Turner, Michelle C and Alonso-Aguado, Maria Henar and Martin, Vicente "
    "and Aragonés, Nuria and Pérez-Gómez, Beatriz and Pozo, Benito Mirón "
    "and Gómez-Acebo, Inés and Ardanaz, Eva and Altzibar, Jone M "
    "and Peiro, Rosana and Tardon, Adonina and Lorca, José Andrés "
    "and Chirlaque, Maria Dolores and García-Palomo, Andrés "
    "and Jimenez-Moleon, Jose Juan and Dierssen, Trinidad and Ederra, Maria "
    "and Amiano, Pilar and Pollan, Marina and Moreno, Victor and Kogevinas, Manolis"
)


class TestSurnameTruncatedInsideAMojibakeByline:
    """10.5271/sjweh.3626 — a byline mis-decoded as Latin-1, one name past repair.

    Crossref's deposit for Papantoniou et al., *Shift work and colorectal cancer
    risk in the MCC-Spain case-control study*, returns UTF-8 decoded as Latin-1
    throughout. Round-trip repair recovers `AragonÃ©s`, `PÃ©rez-GÃ³mez` and
    `GarcÃ­a-Palomo`; it cannot recover position 19, where *Dierssen* arrives as
    `ierssen`, having lost its first character outright — there is no byte left
    to repair.

    Accepting "a surname missing one leading character" on its own would silence
    `ash` against *Nash* and `reid` against *Freid*. What licenses it here is the
    evidence in the same byline: this deposit is provably mis-decoded, and its
    other surnames still match the bibliography. The final test in this class is
    the one that matters — the identical shape in a clean byline is still
    reported.
    """

    def test_the_recorded_deposit_still_carries_the_damage(self) -> None:
        """Pins the fixture. Repairing `tests/data` would empty this class."""
        registry = crossref_authors("mojibake_author_list")
        assert len(registry) == 24
        assert registry[18].family == "ierssen"
        assert registry[6].family == "AragonÃ©s"

    def test_the_whole_recorded_byline_compares_clean(self) -> None:
        diff = compare_author_lists(
            parse_name_list(MCC_SPAIN_STORED), crossref_authors("mojibake_author_list")
        )
        assert not diff.mismatches
        assert not diff.count_differs
        assert diff.clean

    def test_the_truncated_surname_is_named_as_a_registry_artifact(self) -> None:
        """Suppressed, never dropped: the report has to be able to say why."""
        diff = compare_author_lists(
            parse_name_list(MCC_SPAIN_STORED), crossref_authors("mojibake_author_list")
        )
        assert diff.reasons[19] == "registry mojibake truncated the surname"
        # The rest of the damage is ordinary round-trip repair, and saying so
        # keeps this test from passing if the truncation rule quietly widened
        # to cover names the round trip already handles.
        assert sorted(diff.reasons) == [2, 7, 8, 10, 17, 19]

    def test_the_same_shape_in_a_clean_byline_is_still_reported(self) -> None:
        """The counter-test the whole rule rests on.

        No name in this byline is mojibake, so nothing corroborates the missing
        letter and `ash` is simply not *Nash*. If this ever passes, the rule has
        become a global "accept a surname missing its first character" and the
        author check is worthless.
        """
        stored = parse_name_list("Nash, John and Bravo, B and Charlie, C")
        registry = [
            Name(family="ash", given="John"),
            Name(family="Bravo"), Name(family="Charlie"),
        ]
        diff = compare_author_lists(stored, registry)
        assert diff.mismatches
        assert diff.mismatches[0][0] == 1

    def test_mojibake_that_does_not_match_the_bibliography_is_not_evidence(self) -> None:
        """The damage has to be *proven*, not merely plausible.

        A registry surname that round-trips to a name the bibliography does not
        hold shows nothing about this deposit's encoding, so it cannot license
        the truncation rule for a different position.
        """
        stored = parse_name_list("Nash, John and Bravo, B and Charlie, C")
        registry = [
            Name(family="ash", given="John"),
            # Repairs cleanly to "Muñoz", which nobody in the entry is called.
            Name(family="MuÃ±oz"),
            Name(family="Charlie"),
        ]
        diff = compare_author_lists(stored, registry)
        assert any(position == 1 for position, _, _ in diff.mismatches)

    def test_two_lost_characters_are_still_reported(self) -> None:
        """One character is the observed damage; two is an assumption."""
        stored = parse_name_list("Dierssen, Trinidad and Aragonés, Nuria")
        registry = [
            Name(family="erssen", given="Trinidad"),
            Name(family="AragonÃ©s", given="Nuria"),
        ]
        diff = compare_author_lists(stored, registry)
        assert diff.mismatches
        assert diff.mismatches[0][0] == 1

    def test_a_stored_surname_missing_its_first_letter_is_still_reported(self) -> None:
        """The registry is the damaged side; a short *stored* surname is a typo."""
        stored = parse_name_list("ierssen, Trinidad and Aragonés, Nuria")
        registry = [
            Name(family="Dierssen", given="Trinidad"),
            Name(family="AragonÃ©s", given="Nuria"),
        ]
        diff = compare_author_lists(stored, registry)
        assert diff.mismatches
        assert diff.mismatches[0][0] == 1

    def test_disagreeing_forename_initials_are_still_reported(self) -> None:
        """The forename is the cheap corroboration; without it there is no case."""
        stored = parse_name_list("Dierssen, Trinidad and Aragonés, Nuria")
        registry = [
            Name(family="ierssen", given="Manuel"),
            Name(family="AragonÃ©s", given="Nuria"),
        ]
        diff = compare_author_lists(stored, registry)
        assert diff.mismatches
        assert diff.mismatches[0][0] == 1

    def test_a_registry_forename_that_is_absent_supplies_no_corroboration(self) -> None:
        stored = parse_name_list("Dierssen, Trinidad and Aragonés, Nuria")
        registry = [Name(family="ierssen"), Name(family="AragonÃ©s", given="Nuria")]
        diff = compare_author_lists(stored, registry)
        assert diff.mismatches

    def test_a_stem_too_short_to_identify_anyone_is_still_reported(self) -> None:
        """`sh` against *Ash* is two characters of evidence, which is none."""
        stored = parse_name_list("Ash, Alan and Aragonés, Nuria")
        registry = [
            Name(family="sh", given="Alan"),
            Name(family="AragonÃ©s", given="Nuria"),
        ]
        diff = compare_author_lists(stored, registry)
        assert diff.mismatches
        assert diff.mismatches[0][0] == 1

    def test_the_recorded_damage_is_the_one_uncapitalised_surname(self) -> None:
        """Pins the evidence the rule actually rests on.

        Crossref writes `"family":"ierssen"` in lower case in an array where it
        writes `Papantoniou`, `Espinosa` and `Ederra`. That anomaly is what
        separates byte damage from a different family; if a future pass
        "tidies" the fixture to `Ierssen`, every suppression below stops firing
        and this test says why.
        """
        registry = crossref_authors("mojibake_author_list")
        lowercase = [n.family for n in registry if n.family[:1].islower()]
        assert lowercase == ["ierssen"]

    @pytest.mark.parametrize(
        ("stored", "registry"),
        [
            # Real surname pairs exactly one leading character apart. Every one
            # of them satisfied the old rule — a four-character floor, a
            # one-character length difference, a suffix match and agreeing
            # initials — and was cleared inside any byline carrying a single
            # mojibake name. A registry naming a different person capitalises
            # that person's surname; only the damaged one arrives in lower case.
            ("Price", "Rice"),
            ("Gross", "Ross"),
            ("Blake", "Lake"),
            ("Zhang", "Hang"),
            ("Bland", "Land"),
            ("Frank", "Rank"),
            ("Kellis", "Ellis"),
            ("Brooks", "Rooks"),
            ("Sherman", "Herman"),
            ("Grossman", "Rossman"),
            ("Chandler", "Handler"),
        ],
    )
    def test_a_capitalised_registry_surname_is_a_different_family(
        self, stored: str, registry: str
    ) -> None:
        """No floor separates these; the capitalisation does.

        Mis-decoding UTF-8 as Latin-1 never *deletes* a character, so proof that
        a deposit was mis-decoded is not proof that a byte was lost. Without a
        second signal the rule cleared `Rice` against *Price* and `Handler`
        against *Chandler* — different people, reported as the same one.
        """
        diff = compare_author_lists(
            [
                Name(family=stored, given="Robert"),
                Name(family="Aragonés", given="Nuria"),
                Name(family="Espinosa", given="Ana"),
            ],
            [
                Name(family=registry, given="Robert"),
                Name(family="AragonÃ©s", given="Nuria"),  # mojibake, deliberately
                Name(family="Espinosa", given="Ana"),
            ],
        )
        assert diff.mismatches
        assert diff.mismatches[0][0] == 1

    def test_a_byline_that_lowercases_every_surname_supplies_no_anomaly(self) -> None:
        """Some publishers deposit whole bylines in lower case.

        There the case of one surname says nothing about it, so there is no
        evidence and the difference is reported. Without this the rule would be
        strongest exactly where its signal is weakest.
        """
        stored = parse_name_list("Dierssen, Trinidad and Aragonés, Nuria and Espinosa, Ana")
        registry = [
            Name(family="ierssen", given="Trinidad"),
            Name(family="aragonÃ©s", given="Nuria"),  # mojibake, deliberately
            Name(family="espinosa", given="Ana"),
        ]
        diff = compare_author_lists(stored, registry)
        assert diff.mismatches
        assert diff.mismatches[0][0] == 1

    def test_one_capitalised_neighbour_is_not_a_convention(self) -> None:
        """`_MIN_CAPITALISED_WITNESSES`: one sample is a coincidence, two a habit."""
        stored = parse_name_list("Dierssen, Trinidad and Aragonés, Nuria")
        registry = [
            Name(family="ierssen", given="Trinidad"),
            Name(family="AragonÃ©s", given="Nuria"),  # mojibake, deliberately
        ]
        diff = compare_author_lists(stored, registry)
        assert diff.mismatches

    def test_a_particle_surname_does_not_disarm_the_capitalisation_evidence(self) -> None:
        """`van Eijck` and `de Sousa` are filed lower case by house style.

        Counting them as evidence that the deposit does not capitalise surnames
        would switch the rule off for every Dutch, German or Portuguese byline —
        which is a large slice of European epidemiology — and put the witnessed
        Dierssen defect back into the report.
        """
        stored = parse_name_list(
            "Dierssen, Trinidad and Aragonés, Nuria and Espinosa, Ana and van Eijck, Casper"
        )
        registry = [
            Name(family="ierssen", given="Trinidad"),
            Name(family="AragonÃ©s", given="Nuria"),  # mojibake, deliberately
            Name(family="Espinosa", given="Ana"),
            Name(family="van Eijck", given="Casper"),
        ]
        diff = compare_author_lists(stored, registry)
        assert not diff.mismatches
        assert diff.reasons[1] == "registry mojibake truncated the surname"

    def test_a_stored_surname_filed_in_lower_case_is_not_repaired_against(self) -> None:
        """The stored side has to be capitalised too, or the anomaly is not one."""
        stored = parse_name_list("ierssen, Trinidad and Aragonés, Nuria and Espinosa, Ana")
        registry = [
            Name(family="erssen", given="Trinidad"),
            Name(family="AragonÃ©s", given="Nuria"),  # mojibake, deliberately
            Name(family="Espinosa", given="Ana"),
        ]
        diff = compare_author_lists(stored, registry)
        assert diff.mismatches
