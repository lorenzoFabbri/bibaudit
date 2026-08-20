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
import unicodedata
from pathlib import Path

import pytest

from bibaudit.model import Name
from bibaudit.names import (
    _NFKC_CONTINUATION_INVERSE,
    AuthorDiff,
    Reason,
    _forenames_are_incompatible,
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
    reaches it, and the eight author differences that run did flag are all
    accounted for by the collective, et-al, first-author-omission and mojibake
    rules. That corpus is private and its baseline is not in this
    repository, so the count is stated rather than checkable here. It survives
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
        """And carries no reason: `_script_key` compared the glyphs and they matched."""
        agreed, reason = names_agree(Name(family="王", given="L"), Name(family="王", given="L"))
        assert agreed
        assert reason is None

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


class TestARomanisedByline:
    """MEDLINE romanises a surname and Crossref deposits it as written.

    The two disagree systematically, not occasionally: Susanne Kjær is ``Kjaer,
    Susanne K`` on PMID 42550510 and ``Kjær`` in Crossref's record for the same
    DOI, 10.1001/jamanetworkopen.2026.26893, and five of her six most recent
    papers are the same way. ``fold`` replaced the letter with a *space*, so the
    two keys were ``kj r`` and ``kjaer`` and the entry failed on
    ``authors/mismatch``. Two of the seven such entries in a 396-entry live
    sample were one low title score away from being called a different paper.
    """

    @pytest.mark.parametrize(
        ("written", "romanised"),
        [
            ("Kjær", "Kjaer"),
            ("Heß-Busch", "Hess-Busch"),
            ("Dreßen", "Dressen"),
            ("Łapińska", "Lapinska"),
            # PMID 38747246, 10.1093/eurheartj/ehae331: MEDLINE writes
            # Gudmundsdottir where Crossref deposits Guðmundsdóttir.
            ("Guðmundsdóttir", "Gudmundsdottir"),
        ],
    )
    def test_the_two_registries_spellings_agree(
        self, written: str, romanised: str
    ) -> None:
        agreed, reason = names_agree(
            Name(family=romanised, given="S K"), Name(family=written, given="S K")
        )

        assert agreed
        # And on the strongest ground there is: the two keys are equal, not
        # excused by a documented escape.
        assert reason is None

    def test_the_registrys_second_romanisation_of_a_letter_is_not_reached(self) -> None:
        """NLM writes eth as ``d`` on nearly every byline, and ``eth`` on a few.

        PMID 42541912 is one of them — ``FAU - Friethriksdottir, Nanna``
        against Crossref's ``Friðriksdóttir`` for 10.1016/j.ejca.2026.116959 —
        and the length difference puts the pair past the one-substitution rule
        the spelling-variant escape allows, so a correct entry reports
        ``authors/mismatch``. A table holds one spelling per letter and a
        second is a second key, not a wider match: ``docs/limits.md`` says so
        where a reader of the report will find it.
        """
        agreed, _ = names_agree(
            Name(family="Friethriksdottir", given="N"),
            Name(family="Friðriksdóttir", given="N"),
        )

        assert not agreed

    def test_a_different_surname_is_still_reported(self) -> None:
        """Romanising must not make one Danish surname stand for another."""
        agreed, _ = names_agree(
            Name(family="Kjaer", given="S K"), Name(family="Kjærgaard", given="S K")
        )

        assert not agreed


class TestABylineCarryingALatinLookalike:
    """A creator with one letter drawn from Cyrillic or Greek.

    The publisher deposits it and both registries inherit it: Crossref's
    creator array for 10.26442/00403660.2024.07.202907 opens a forename on
    U+0422 CYRILLIC CAPITAL LETTER TE, and NLM's XML for the same paper
    (PMID 39106512) carries that code point as ``&#x422;``. 23 of the 91,226
    creators in a 32,000-work random Crossref sample carry one.

    Written with explicit escapes rather than the glyphs, because which code
    point is in the string is the whole of what these tests are about — a
    reader cannot tell the two ``T``\\s apart, which is why the damage survives
    proofreading in the first place.

    ``fold`` deleted the letter, leaving a key one letter short instead of
    empty, and both halves of the comparison then read the short key as
    knowledge. This class pins both directions: the correct entry that stopped
    being accused, and the substituted creator that stopped being cleared.
    """

    # U+0422 CYRILLIC CAPITAL LETTER TE, U+0410 CYRILLIC CAPITAL LETTER A and
    # U+0443 CYRILLIC SMALL LETTER U, each in the deposit named beside it.
    TATIANA = "\u0422atiana A."
    INITIAL = "\u0410. S."
    SOLOVYEV = "Solov\u0443ev"

    def test_a_correct_entry_is_not_accused_of_crediting_someone_else(self) -> None:
        """10.26442/00403660.2024.07.202907, creator four.

        A bibliography spelling the forename in Latin initials on ``t``; the
        deposit folded to ``atiana a`` and initialled on ``a``, so the entry
        was reported ``authors/forename`` — a correct citation, told it credits
        a different person.
        """
        agreed, reason = names_agree(
            Name(family="Rassovskaya", given="Tatiana A."),
            Name(family="Rassovskaya", given=self.TATIANA),
        )

        assert agreed
        assert reason is None

    def test_a_substituted_forename_under_that_creator_is_still_reported(self) -> None:
        """The other half, and the reason the repair replaces the reading.

        Deletion moved the forename's second letter into first place, so every
        substituted forename opening on that letter was cleared. *Anna* is not
        Tatiana, and offering the damaged reading *as well* would keep clearing
        her.
        """
        agreed, _ = names_agree(
            Name(family="Rassovskaya", given="Anna A."),
            Name(family="Rassovskaya", given=self.TATIANA),
        )

        assert not agreed

    def test_a_forename_that_is_only_an_initial_is_read_as_that_initial(self) -> None:
        """10.26442/00403660.2025.07.203267, creator one.

        Nothing survives deletion here but the *middle* initial, so the
        comparison read ``S`` as the first: ``Panferov, A. S.`` was accused and
        ``Panferov, Carl S.`` was cleared.
        """
        assert names_agree(
            Name(family="Panferov", given="A. S."),
            Name(family="Panferov", given=self.INITIAL),
        ) == (True, None)
        agreed, _ = names_agree(
            Name(family="Panferov", given="Carl S."),
            Name(family="Panferov", given=self.INITIAL),
        )
        assert not agreed

    def test_a_surname_carrying_one_is_not_a_phantom_mismatch(self) -> None:
        """10.33285/0132-2222-2019-9(554)-28-35, the surname rather than the forename.

        ``family_key`` compares the whole surname, so a deleted letter is
        CLAUDE.md's banned shape reached by a second route: the key is a
        fragment of the surname and the entry fails ``authors/mismatch``.
        """
        agreed, reason = names_agree(
            Name(family="Solovyev", given="A"), Name(family=self.SOLOVYEV, given="A")
        )

        assert agreed
        assert reason is None

    def test_a_different_surname_is_still_reported(self) -> None:
        """Repairing must not make one surname stand for another.

        Not *Solovyov*, which the repaired key is one substitution from and
        which `Reason.SPELLING_VARIANT` accepts as the same name transliterated
        twice — that escape is not what this test is about.
        """
        agreed, _ = names_agree(
            Name(family="Kuznetsov", given="A"), Name(family=self.SOLOVYEV, given="A")
        )

        assert not agreed

    def test_a_surname_written_in_cyrillic_is_still_unreadable_rather_than_repaired(
        self,
    ) -> None:
        """The guard, from the byline's side.

        A surname a registry deposits in its own script has no comparison key
        and is accepted out loud, under a documented reason. Repairing letter
        by letter would replace that honest gap with an invented Latin key —
        and every test in `TestSurnamesOutsideTheComparisonAlphabet` rests on
        the key being empty.
        """
        # U+0421 U+043e U+0440: a Russian word whose every letter has a Latin
        # twin, and therefore the worst case for the guard.
        assert family_key(Name(family="\u0421\u043e\u0440")) == ""


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

    @pytest.mark.parametrize(
        ("broken", "expected"),
        [
            # 10.1111/j.1574-6968.1992.tb05540.x, recorded as
            # tests/data/names_crossref_mojibake_latin_extended_a.json: two
            # Slovenian surnames whose lead bytes are C5 and C4, the Latin
            # Extended-A block.
            ("LaniÅ¡nik", "Lanišnik"),
            ("BeliÄ\x8d", "Belič"),
            # 10.1615/intjmedmushrooms.2024052864: *Téllez* deposited
            # decomposed, so what was mis-decoded is the combining acute's own
            # bytes, CC 81.
            ("TeÌ\x81llez-TeÌ\x81llez", "Téllez-Téllez"),
        ],
    )
    def test_a_lead_byte_outside_the_latin1_supplement_is_repaired(
        self, broken: str, expected: str
    ) -> None:
        """Mis-decoding is not confined to the characters Spanish needs.

        Latin Extended-A carries the Polish, Czech, Slovak, Croatian, Slovene,
        Turkish, Romanian, Hungarian, Latvian and Lithuanian diacritics, and
        `Lanišnik` mis-decoded folds to `lania nik` — the surname broken into
        tokens, which is the `AragonÃ©s` failure arriving by a second route.
        """
        repaired, was_mojibake = demojibake(broken)
        assert was_mojibake
        assert unicodedata.normalize("NFC", repaired) == expected

    def test_a_correct_bibliography_is_not_accused_over_a_slovene_surname(self) -> None:
        """The end of the same story: what the entry pays for the missed repair.

        Crossref's byline for `10.1111/j.1574-6968.1992.tb05540.x` is
        mis-decoded; an entry holding the surnames correctly is the one that
        gets reported unless the repair is attempted.
        """
        agreed, reason = names_agree(
            Name(family="Lanišnik", given="Tea"), Name(family="LaniÅ¡nik", given="Tea")
        )
        assert agreed
        assert reason == "registry mojibake"

    @pytest.mark.parametrize(
        ("broken", "expected"),
        [
            # 10.14748/adipo.v4.289, recorded as
            # tests/data/names_crossref_mojibake_cp1252.json: C5 9F, where 0x9F
            # is a C1 control in Latin-1 and `Ÿ` in cp1252.
            ("Ionescu-TÃ®rgoviÅŸte", "Ionescu-Tîrgovişte"),
            # 10.14419/ijet.v7i4.3.19550: D0 9A, one Cyrillic capital opening an
            # otherwise Latin surname, deposited that way by the publisher. The
            # expected value is written as an escape because U+041A and `K` are
            # indistinguishable by eye, which is the point of it.
            ("Ðšravets", "\u041aravets"),
        ],
    )
    def test_a_surname_mis_decoded_as_cp1252_is_repaired(
        self, broken: str, expected: str
    ) -> None:
        """Latin-1 cannot re-encode what cp1252 decoded, and vice versa.

        The two agree above 0x9F and disagree below it, so a value mis-decoded
        by one is unreachable through the other: `Ÿ` is not a Latin-1 character
        and cp1252 has nothing at 0x8D.
        """
        repaired, was_mojibake = demojibake(broken)
        assert was_mojibake
        assert repaired == expected

    @pytest.mark.parametrize(
        "surname",
        # U+2019 RIGHT SINGLE QUOTATION MARK, U+2013 EN DASH: written as escapes
        # because neither is distinguishable from its ASCII lookalike by eye.
        ["O\u2019Brien", "D\u2019Angelo", "H\u00e4kkinen\u2013Virtanen"],
    )
    def test_a_name_carrying_typographic_punctuation_is_not_repaired(
        self, surname: str
    ) -> None:
        """cp1252 is what makes a curly quote or an en dash encodable at all.

        U+2019 becomes byte 0x92, a continuation byte with no lead in front of
        it, so the round trip refuses the string rather than inventing a repair
        for a name that was never mis-decoded.
        """
        repaired, was_mojibake = demojibake(surname)
        assert not was_mojibake
        assert repaired == surname

    @pytest.mark.parametrize(
        "surname",
        ["Åström", "Ångström", "Öberg", "Ärlig", "Øst", "Ćurić", "Škoda", "Žitnik"],
    )
    def test_a_nordic_or_slavic_surname_is_not_repaired_into_something_else(
        self, surname: str
    ) -> None:
        """The round trip is the guard, so it has to hold with nothing ahead of it.

        `Å` before an ASCII letter is not a valid UTF-8 lead byte, and a
        surname written in Latin Extended-A does not encode to Latin-1 at all.
        """
        repaired, was_mojibake = demojibake(surname)
        assert not was_mojibake
        assert repaired == surname

    @pytest.mark.parametrize(
        ("broken", "expected"),
        [
            # 10.1111/j.1574-6968.1992.tb05540.x: `½`, which NFKC expands to
            # three characters, so no one-for-one substitution reaches it.
            ("Å½akelj-MavriÄ\x8d", "Žakelj-Mavrič"),
            # `¼`, the commonest of the three by a wide margin: every mis-decoded
            # `ü` in every German surname there is.
            ("MÃ¼ller", "Müller"),
            # The four spacing accents, which NFKC turns into a space and a
            # combining mark: `è` and `ø`.
            ("MichÃ¨le", "Michèle"),
            ("S\u00c3\u00b8rensen", "Sørensen"),
        ],
    )
    def test_an_nfkc_image_of_more_than_one_character_is_restored(
        self, broken: str, expected: str
    ) -> None:
        """A one-for-one table reaches five of the fourteen rewritings NFKC makes.

        The other nine expand a Latin-1 character into two or three, and a
        substitution written character-for-character cannot invert one.
        """
        damaged = clean(broken)
        # The test would prove nothing if `clean` had not already done the harm.
        assert damaged != broken
        repaired, was_mojibake = demojibake(damaged)
        assert was_mojibake
        assert unicodedata.normalize("NFC", repaired) == expected

    def test_a_rewritten_continuation_is_restored_after_any_lead(self) -> None:
        """`µ` is not an ASCII image, so it needs no positional guard.

        Crossref's byline for `10.14419/ijet.v7i4.3.19550` carries one Cyrillic
        letter inside a Latin surname, mis-decoded as `Ðµ` — whose lead is not
        one of the two after which an ASCII image is read as a continuation.
        """
        damaged = clean("YÐµvtushenko")
        assert "μ" in damaged
        repaired, was_mojibake = demojibake(damaged)
        assert was_mojibake
        assert repaired == "Y\u0435vtushenko"

    def test_the_inverse_is_read_out_of_unicodedata_rather_than_listed(self) -> None:
        """A written table is a claim about NFKC, and drifts from it unwatched.

        Every Latin-1 character that can stand as a continuation byte and that
        NFKC rewrites needs an entry, or the surname carrying it is
        irreparable — which is how `ü`, `è`, `ø`, `ô`, `õ`, `ï`, `ý` and `þ`
        came to be outside a table that named `¹²³ªº`.
        """
        rewritten = {
            chr(point)
            for point in range(0x80, 0xC0)
            if unicodedata.normalize("NFKC", chr(point)) != chr(point)
        }
        assert set(_NFKC_CONTINUATION_INVERSE.values()) == rewritten - {"\xa0"}
        # U+00A0 is the one exclusion, and `clean`'s whitespace pass rather than
        # NFKC is what destroyed it. See the test below for what it costs.
        assert "\xa0" in rewritten
        assert " " not in _NFKC_CONTINUATION_INVERSE

    def test_a_space_after_a_mojibake_lead_is_not_read_back_as_a_hard_one(self) -> None:
        """The one rewriting left uninverted, and what it costs.

        `clean` collapses U+00A0 to an ordinary space before this module runs,
        so a space standing after a mojibake lead cannot be told from one the
        deposit really had, and reading it back would repair `JosÃ©Â Antonio`
        and mangle any correct name shaped like it in equal measure. **NO
        WITNESSED INSTANCE** either way: no value in 399,450 from a random
        Crossref sample carries `Ã` or `Â` before a space.
        """
        damaged = clean("Jos\u00c3\u00a9\u00c2\u00a0Antonio")
        assert damaged == "Jos\u00c3\u00a9\u00c2 Antonio"
        repaired, was_mojibake = demojibake(damaged)
        assert not was_mojibake
        assert repaired == damaged

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

    def test_a_rotation_is_still_a_reordering(self) -> None:
        """Neither list is wrong and no creator moved in or out of the byline.

        The rule is the same creators counted, not a pairwise exchange: a
        registry that files the senior author first shifts everybody, and every
        shifted position is excused.
        """
        stored = [Name(family="Malats"), Name(family="Real"), Name(family="Kaaks")]
        registry = [Name(family="Kaaks"), Name(family="Malats"), Name(family="Real")]

        diff = compare_author_lists(stored, registry)

        assert not diff.mismatches
        assert set(diff.reasons.values()) == {"reordered"}

    def test_a_creator_the_registry_never_reaches_is_reported_by_name(self) -> None:
        """Not as a count: the reader's question is *which* name has no witness."""
        stored = [Name(family="Malats"), Name(family="Real")]
        registry = [Name(family="Malats")]

        diff = compare_author_lists(stored, registry)

        assert diff.uncorroborated == [(2, "Real")]
        assert not diff.count_differs
        assert not diff.clean

    def test_a_registry_byline_longer_than_the_entry_is_still_a_count(self) -> None:
        """The other direction is incompleteness, and stays a count difference."""
        stored = [Name(family="Malats")]
        registry = [Name(family="Malats"), Name(family="Real")]

        diff = compare_author_lists(stored, registry)

        assert diff.count_differs
        assert not diff.uncorroborated

    def test_a_truncated_byline_states_nothing_about_its_own_tail(self) -> None:
        """Past an et-al marker the list stopped; it did not gain a creator.

        Without the guard the marker itself is the creator past the registry's
        last, and every BibTeX byline written `and others` reports an invented
        co-author named *others*.
        """
        stored = parse_name_list("Malats, N and Real, F and others")
        registry = [Name(family="Malats"), Name(family="Real")]

        diff = compare_author_lists(stored, registry)

        assert not diff.uncorroborated
        assert diff.clean

    def test_a_tail_that_is_entirely_excused_is_not_a_count_either(self) -> None:
        """Every position past the registry's last is accounted for one by one.

        A count line beside them restates the same arithmetic and names nobody,
        so it is not printed whether the tail was reported or excused.
        """
        stored = [
            Name(family="Alpha", given="A"),
            Name(family="Bravo", given="B"),
            Name(literal="STAAB consortium", collective=True),
        ]
        registry = [Name(family="Alpha", given="A"), Name(family="Zulu", given="Z")]

        diff = compare_author_lists(stored, registry)

        assert not diff.uncorroborated
        assert not diff.count_differs
        assert diff.reasons[3] == "collective creator past the registry's last"

    def test_a_tail_surname_the_alphabet_cannot_express_is_reported(self) -> None:
        """Nothing was compared, and that is not the registry carrying it."""
        stored = [Name(family="Alpha", given="A"), Name(family="王", given="L")]
        registry = [Name(family="Alpha", given="A")]

        diff = compare_author_lists(stored, registry)

        assert [position for position, _ in diff.uncorroborated] == [2]

    def test_registry_collective_against_a_member_list_is_not_a_defect(self) -> None:
        """Crossref splits some consortium bylines; the bibliography keeps the group."""
        stored = [Name(family="Smith"), Name(family="Jones")]
        registry = [Name(literal="The Study Group", collective=True)]
        diff = compare_author_lists(stored, registry)
        assert not diff.mismatches
        assert not diff.count_differs
        assert diff.reasons == {1: "registry lists a collective author"}

    def test_one_group_name_against_the_same_group_name_is_simply_correct(self) -> None:
        """Nothing was suppressed, so nothing may be reported as suppressed.

        The escape above is for a group name standing against a *list of
        people*. Both sides naming the organisation is the entry being right,
        and printing "collective author" over the pair puts a correct entry in
        the report under a reason its reader cannot act on.
        """
        stored = [Name(literal="The Study Group", collective=True)]
        registry = [Name(literal="The Study Group", collective=True)]
        diff = compare_author_lists(stored, registry)
        assert diff.clean
        assert not diff.reasons

    def test_two_different_group_names_keep_the_escape(self) -> None:
        """Equality of the comparison keys is the whole of the new branch.

        One name against one name gives the positional comparison nothing to
        work with either way, so a pair that does *not* agree stays where it
        was — suppressed and stated — rather than becoming a mismatch on the
        strength of a rule about organisations nobody has written.
        """
        stored = [Name(literal="The Other Study Group", collective=True)]
        registry = [Name(literal="The Study Group", collective=True)]
        diff = compare_author_lists(stored, registry)
        assert not diff.mismatches
        assert diff.reasons == {1: "collective author"}


class TestABylineThatLostAnAuthorIsNotAReordering:
    """Set membership let a repeated surname supply its own alibi.

    A byline missing its first author is a one-position shift, and at every
    shifted position both surnames do appear in the other list — so each was
    excused as a reordering and the entry came back non-failing with a count
    warning. 104 of 2,551 live entries took that shape, while
    `docs/registry-artifacts.md` promised the opposite: "a genuine exchange,
    not a name that merely went missing".

    Position 1 is what should have stopped it, and does not when the dropped
    surname repeats later in the byline — routine in Chinese, Korean and
    Japanese author lists. PMID 38213033's real thirteen-creator byline is one:
    Lee, Jung, Kim, Lee, Lee, Baek, Kwon, Shin, Kim, Shin, Park, Park, Kim.
    """

    _BYLINE = (
        "Lee, A", "Jung, B", "Kim, C", "Lee, D", "Lee, E", "Baek, F", "Kwon, G",
        "Shin, H", "Kim, I", "Shin, J", "Park, K", "Park, L", "Kim, M",
    )

    def _registry(self) -> list[Name]:
        return [parse_name(value) for value in self._BYLINE]

    def test_every_shifted_position_is_reported(self) -> None:
        stored = [parse_name(value) for value in self._BYLINE[1:]]

        diff = compare_author_lists(stored, self._registry())

        assert diff.mismatches
        assert "reordered" not in set(diff.reasons.values())

    def test_the_same_byline_merely_reordered_is_still_excused(self) -> None:
        """The false positive this must not create.

        The identical thirteen creators with the first two exchanged: nobody
        went missing, so the difference is an order the two sources serialise
        differently and neither is wrong.
        """
        swapped = [self._BYLINE[1], self._BYLINE[0], *self._BYLINE[2:]]
        stored = [parse_name(value) for value in swapped]

        diff = compare_author_lists(stored, self._registry())

        assert not diff.mismatches
        assert set(diff.reasons.values()) == {"reordered"}

    def test_two_surnames_the_alphabet_cannot_hold_are_not_a_reordering(self) -> None:
        """Counted keys are equal for any two bylines the alphabet discards.

        `family_key` returns "" for every surname outside it, so a byline of
        those counts equal against any other, and the escape would then excuse
        the whole of it. 王 against 李 is a substitution — the initials that
        separate them are what `names_agree` already refused on.
        """
        stored = [Name(family="王", given="L"), Name(family="李", given="M")]
        registry = [Name(family="李", given="M"), Name(family="王", given="L")]

        diff = compare_author_lists(stored, registry)

        assert diff.mismatches
        assert "reordered" not in set(diff.reasons.values())

    def test_a_substitution_inside_a_reordering_is_reported(self) -> None:
        """Counted, so an exchanged pair cannot cover a replaced creator.

        The same length, and every other position still finds its own surname
        somewhere in the other list.
        """
        swapped = [self._BYLINE[1], self._BYLINE[0], "Fabricado, X", *self._BYLINE[3:]]
        stored = [parse_name(value) for value in swapped]

        diff = compare_author_lists(stored, self._registry())

        assert diff.mismatches
        assert "reordered" not in set(diff.reasons.values())


#: The byline of `clavelchapelon1997e3n` exactly as the corpus stores it. Ten
#: creators; Crossref's deposit for the same DOI holds the last nine.
E3N_STORED = (
    "Clavel-Chapelon, F and van Liere, M J and Giubout, C and Niravong, M Y "
    "and Goulard, H and Corre, C Le and Hoang, L A and Amoyel, J "
    "and Auquier, A and Duquesnel, E"
)


class TestABylineCollectiveTheRegistryFilesApart:
    """The escape's two refusals, neither of which the shape below can do without.

    Crossref credits a consortium in the byline and MEDLINE files it under
    ``CN``, so the byline carries a creator the registry's list does not and
    every position after it is shifted. What licenses the escape is that the
    people left after the collectives come off are the *same list* as the
    registry's — same length, agreeing position for position — and each guard
    below is a way for that evidence to be absent while the walk still runs.
    """

    def _registry(self) -> list[Name]:
        return [Name(family=f, given="X") for f in ("Alvarez", "Bianchi", "Costa")]

    def _byline(self, *extra: Name) -> list[Name]:
        people = self._registry()
        return [
            people[0],
            Name(literal="ITC Project Collaborators", collective=True),
            *people[1:],
            *extra,
        ]

    def test_the_aligned_shape_is_suppressed(self) -> None:
        diff = compare_author_lists(self._byline(), self._registry())

        assert diff.clean
        assert diff.reasons == {
            1: "byline carries collective creator(s) the registry files apart: "
               "ITC Project Collaborators"
        }

    def test_a_byline_longer_than_the_registrys_is_reported_not_suppressed(self) -> None:
        """The length bound, which is also what keeps the walk from crashing.

        Without it the people list and the registry's are zipped ``strict``,
        so an extra creator raises ``ValueError`` out of a comparison instead
        of returning a finding.
        """
        diff = compare_author_lists(
            self._byline(Name(family="Eriksen", given="X")), self._registry()
        )

        assert not diff.clean
        assert diff.mismatches

    def test_a_creator_that_is_a_collective_and_a_truncation_marker_is_refused(
        self,
    ) -> None:
        """Past an et-al marker a list is truncated and its length states nothing.

        The alignment this escape rests on is a claim about two complete
        lists, and a marker makes the byline's length meaningless — so the
        collectives are not reported as creators the registry filed apart,
        they are what the byline stopped at. No adapter builds a name that is
        both today; the guard runs before the alignment is computed and this
        pins it at the contract.
        """
        marker = Name(literal="others", collective=True, et_al=True)
        stored = [self._registry()[0], marker, *self._registry()[1:]]

        diff = compare_author_lists(stored, self._registry())

        assert not [r for r in diff.reasons.values() if r.startswith("byline carries")]


class TestRegistryOmittingTheFirstAuthor:
    """10.1097/00008469-199710000-00007 — Crossref's byline starts one name late.

    The paper is Clavel-Chapelon et al., *E3N, a French cohort study on cancer
    risk factors*, Eur J Cancer Prev 1997. Ovid/Wolters Kluwer deposited nine
    creators beginning with `van Liere`, whom the deposit even marks
    `"sequence": "first"`. Compared position against position, a correct
    ten-author entry produces ten "different person" errors and fails as
    FIELD-MISMATCH. It is one of the eight author differences the corpus run
    flagged, all of them false positives. Unlike that count, this one can be
    checked from here: the deposit itself is in `tests/data`.
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
        assert not diff.clean

    def test_a_registry_sharing_only_a_minority_of_the_names_is_still_reported(self) -> None:
        stored = parse_name_list(
            "Alpha, A and Bravo, B and Charlie, C and Delta, D and Echo, E and Foxtrot, F"
        )
        registry = [Name(family="Echo"), Name(family="Foxtrot")]
        diff = compare_author_lists(stored, registry)
        assert diff.mismatches
        assert not diff.clean

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

        `Delta` is the creator that carries the finding: it sits past the
        registry's third and last position, so what says the difference was not
        aligned away is that it is reported there.
        """
        stored = parse_name_list("Alpha, A and Bravo, B and Charlie, C and Delta, D")
        diff = compare_author_lists(stored, registry)
        assert not diff.clean, case
        assert [name for _, name in diff.uncorroborated] == ["Delta, D"], case
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


#: One byline pair per :class:`Reason`, and the position the reason lands at.
#: Every entry produces a *suppressed* difference, which is the shape that needs
#: watching: an escape records its reason and then returns a diff that is clean
#: by every other measure, so the recorded string is the only thing separating
#: "stated and not failed" from "hidden". Asserting the diff is clean asserts
#: nothing about it.
_WITNESSED_REASONS: tuple[tuple[Reason, int, list[Name], list[Name]], ...] = (
    # Nothing to compare: a creator deposited with no surname at all.
    (
        Reason.NO_SURNAME, 2,
        [Name(family="Alpha", given="A"), Name(given="B")],
        [Name(family="Alpha", given="A"), Name(family="Bravo", given="B")],
    ),
    # A native form against a romanised one: no key spans them.
    (
        Reason.UNREPRESENTABLE_SCRIPT, 2,
        [Name(family="Alpha", given="A"), Name(family="山田", given="T")],
        [Name(family="Alpha", given="A"), Name(family="Yamada", given="T")],
    ),
    # A registry surname cut to one character carries no information.
    (
        Reason.REGISTRY_INITIAL_ONLY, 2,
        [Name(family="Alpha", given="A"), Name(family="Bravo", given="B")],
        [Name(family="Alpha", given="A"), Name(family="B", given="B")],
    ),
    # MEDLINE deposits the whole creator in `<LastName>` and says nothing about
    # where the surname ends; `Okano, J.` is what the byline correctly carries.
    (
        Reason.UNSPLIT_REGISTRY_NAME, 2,
        [Name(family="Alpha", given="A"), Name(family="Okano", given="J.")],
        [Name(family="Alpha", given="A"), Name(family="Okano J", unsplit=True)],
    ),
    (
        Reason.ET_AL, 2,
        [Name(family="Alpha", given="A"), Name(et_al=True)],
        [Name(family="Alpha", given="A"), Name(family="Bravo", given="B")],
    ),
    (
        Reason.REGISTRY_MOJIBAKE, 1,
        [Name(family="Aragonés", given="Nuria")],
        [Name(family="AragonÃ©s", given="Nuria")],  # mojibake, deliberately
    ),
    (
        Reason.PARTICLE_FILING, 1,
        [Name(family="van Eijck", given="Casper")],
        [Name(family="Eijck", given="Casper")],
    ),
    (
        Reason.COMPOUND_SHORTENED, 1,
        [Name(family="Clavel-Chapelon", given="F")],
        [Name(family="Chapelon", given="F")],
    ),
    (
        Reason.SPELLING_VARIANT, 1,
        [Name(family="Papantoniou", given="K")],
        [Name(family="Papantoniu", given="K")],
    ),
    (
        Reason.STORED_COLLECTIVE, 1,
        [Name(literal="The Study Group", collective=True)],
        [Name(family="Smith"), Name(family="Jones")],
    ),
    (
        Reason.REGISTRY_COLLECTIVE, 1,
        [Name(family="Smith"), Name(family="Jones")],
        [Name(literal="The Study Group", collective=True)],
    ),
    (
        Reason.INTERLEAVED_COLLECTIVES, 1,
        [Name(family="Alpha", given="A"), Name(family="Bravo", given="B")],
        [
            Name(family="Alpha", given="A"),
            Name(literal="DiscovEHR", collective=True),
            Name(family="Bravo", given="B"),
        ],
    ),
    (
        Reason.BYLINE_COLLECTIVES, 1,
        [
            Name(family="Alpha", given="A"),
            Name(literal="ITC Project Collaborators", collective=True),
            Name(family="Bravo", given="B"),
        ],
        [Name(family="Alpha", given="A"), Name(family="Bravo", given="B")],
    ),
    (
        Reason.FIRST_AUTHOR_OMITTED, 1,
        [
            Name(family="Alpha", given="A"), Name(family="Bravo", given="B"),
            Name(family="Charlie", given="C"), Name(family="Delta", given="D"),
        ],
        [
            Name(family="Bravo", given="B"), Name(family="Charlie", given="C"),
            Name(family="Delta", given="D"),
        ],
    ),
    # The Dierssen defect: a surname that lost its first character inside a
    # byline two other creators prove is mis-decoded.
    (
        Reason.MOJIBAKE_TRUNCATED, 1,
        [
            Name(family="Dierssen", given="Trinidad"), Name(family="Aragonés", given="Nuria"),
            Name(family="Espinosa", given="Ana"),
        ],
        [
            Name(family="ierssen", given="Trinidad"),
            Name(family="AragonÃ©s", given="Nuria"),  # mojibake, deliberately
            Name(family="Espinosa", given="Ana"),
        ],
    ),
    (
        Reason.REORDERED, 1,
        [Name(family="Real"), Name(family="Malats")],
        [Name(family="Malats"), Name(family="Real")],
    ),
    # Past the registry's last creator. The people do not align, so
    # `_byline_collectives` declines the byline and the consortium arrives at
    # the tail instead.
    (
        Reason.TRAILING_COLLECTIVE, 3,
        [
            Name(family="Alpha", given="A"), Name(family="Bravo", given="B"),
            Name(literal="STAAB consortium", collective=True),
        ],
        [Name(family="Alpha", given="A"), Name(family="Zulu", given="Z")],
    ),
    (
        Reason.CREDITED_ELSEWHERE, 3,
        [
            Name(family="Alpha", given="A"), Name(family="Bravo", given="B"),
            Name(family="Alpha", given="A"),
        ],
        [Name(family="Alpha", given="A"), Name(family="Bravo", given="B")],
    ),
)


class TestEveryReasonIsActuallyRecorded:
    """The other half of the contract ``tests/test_benign.py`` enforces.

    That module checks every reason has a section in the documentation. This one
    checks the reason still reaches the report at all — a section explaining a
    string nothing emits documents nothing.
    """

    @pytest.mark.parametrize(
        ("reason", "position", "stored", "registry"),
        _WITNESSED_REASONS,
        ids=[reason.name for reason, _, _, _ in _WITNESSED_REASONS],
    )
    def test_the_witness_records_its_reason(
        self, reason: Reason, position: int, stored: list[Name], registry: list[Name]
    ) -> None:
        recorded = compare_author_lists(stored, registry).reasons.get(position, "")
        assert recorded.startswith(reason.value), recorded

    def test_every_reason_has_a_witness(self) -> None:
        """Complete by construction, like ``ARTIFACT_REASONS`` itself.

        A new member of ``Reason`` cannot be added without a byline that produces
        it, so "this escape is tested" stops being something to remember.
        """
        assert {reason for reason, _, _, _ in _WITNESSED_REASONS} == set(Reason)


class TestOnlyDocumentedReasonsReachAReport:
    """``AuthorDiff.note`` is the whole of the path from an escape to the report."""

    def test_the_reason_that_names_what_it_found_is_refused_by_note(self) -> None:
        """Otherwise it emits its own prefix and then names nothing.

        ``registry interleaves collective creator(s) the byline omits:`` with an
        empty tail is a claim about particular creators, printed without them.
        """
        with pytest.raises(ValueError, match="note_collectives"):
            AuthorDiff().note(1, Reason.INTERLEAVED_COLLECTIVES)

    def test_the_mirror_reason_is_refused_by_note_too(self) -> None:
        """Both prefixes name what they found, so both are excluded."""
        with pytest.raises(ValueError, match="note_collectives"):
            AuthorDiff().note(1, Reason.BYLINE_COLLECTIVES)

    def test_naming_no_collectives_at_all_is_refused(self) -> None:
        with pytest.raises(ValueError, match="named"):
            AuthorDiff().note_collectives(1, [], Reason.INTERLEAVED_COLLECTIVES)

    def test_the_organisations_are_printed_in_full(self) -> None:
        diff = AuthorDiff()
        diff.note_collectives(
            1,
            [Name(literal="DiscovEHR"), Name(literal="UK Biobank")],
            Reason.INTERLEAVED_COLLECTIVES,
        )
        assert diff.reasons[1] == (
            "registry interleaves collective creator(s) the byline omits: "
            "DiscovEHR; UK Biobank"
        )

    def test_the_side_that_carried_them_is_named(self) -> None:
        """A reader given the wrong side goes looking in the wrong record."""
        diff = AuthorDiff()
        diff.note_collectives(
            1, [Name(literal="FinnGen")], Reason.BYLINE_COLLECTIVES
        )
        assert diff.reasons[1] == (
            "byline carries collective creator(s) the registry files apart: FinnGen"
        )

    def test_reasons_cannot_be_written_through_the_public_name(self) -> None:
        """The mapping is the report's only source for why a difference was excused."""
        with pytest.raises(TypeError):
            AuthorDiff().reasons[1] = "a reason nobody documented"  # type: ignore[index]


class TestForenamesUnderAnAgreeingSurname:
    """One surname, two people.

    The comparison used to stop at the surname key, so `Wade, Nicholas` against
    `Wade, Zbigniew` was `(True, None)` — agreement, with nothing recorded at
    any verbosity, on a citation crediting somebody who did not write the paper.
    Measured over 3,963 live MEDLINE/Crossref pairs of the same work: replacing
    one creator's forename with an incompatible one was reported on none of
    3,693 entries before and on 3,652 after, while the two registries'
    unmutated bylines gained a finding on 3 of 3,876.

    Both directions are pinned here, and the exceptions matter more than the
    check: everything in `TestForenameDifferencesRegistriesProduce` is a
    disagreement two registries reach honestly about one person, and a rule
    that fires on any of them is worse than the miss it closes.
    """

    def test_a_different_forename_under_one_surname_is_reported(self) -> None:
        agreed, reason = names_agree(
            Name(family="Wade", given="Nicholas"), Name(family="Wade", given="Zbigniew")
        )
        assert not agreed, reason

    def test_a_different_initial_under_one_surname_is_reported(self) -> None:
        """PMID 414278's shape: `Shih, R M` where Crossref deposits `Tsung-Ming`."""
        agreed, reason = names_agree(
            Name(family="Orci", given="L"), Name(family="Orci", given="Q")
        )
        assert not agreed, reason

    def test_the_report_names_both_people(self) -> None:
        """A count line names nobody; the reader's question is *which* creator."""
        diff = compare_author_lists(
            parse_name_list("Wade, Nicholas and Malats, N"),
            [Name(family="Wade", given="Zbigniew"), Name(family="Malats", given="N")],
        )
        assert diff.miscredited == [(1, "Wade, Nicholas", "Wade, Zbigniew")]
        assert not diff.mismatches
        assert not diff.clean

    def test_a_reordering_cannot_absorb_a_substituted_forename(self) -> None:
        """The escape that would have swallowed the whole check.

        Substituting a forename leaves both bylines holding the same surnames
        counted, so `reordering` is true at every position. Routed through it,
        the difference came back suppressed as a creator who moved — while the
        surname sits exactly where the entry put it.
        """
        stored = parse_name_list("Wade, Nicholas and Malats, Nuria")
        registry = [Name(family="Wade", given="Zbigniew"), Name(family="Malats", given="Nuria")]
        diff = compare_author_lists(stored, registry)
        assert diff.miscredited
        assert Reason.REORDERED not in diff.reasons.values()

    def test_two_unreadable_surnames_are_a_substitution_and_not_a_forename(self) -> None:
        """`family_key` returns nothing for either, and nothing equals nothing.

        Without the emptiness guard, 王 against 李 — two surnames `fold`
        discards — files as one family with two members, which is neither what
        happened nor what a reader can act on.
        """
        diff = compare_author_lists(
            [Name(family="王", given="Lei")], [Name(family="李", given="Ming")]
        )
        assert diff.mismatches
        assert not diff.miscredited

    def test_a_byline_carrying_only_a_forename_difference_is_not_clean(self) -> None:
        diff = AuthorDiff()
        diff.stored_count = diff.registry_count = 1
        diff.miscredited.append((1, "Wade, Nicholas", "Wade, Zbigniew"))
        assert not diff.clean

    @pytest.mark.parametrize(
        ("surname_stored", "surname_registry"),
        [
            # Every surname rule in `names_agree` decides that two spellings name
            # one *family*, and not one of them looks at which member of it.
            ("Aragonés", "AragonÃ©s"),          # registry mojibake
            ("van Eijck", "Eijck"),             # particle filing
            ("Clavel-Chapelon", "Chapelon"),    # compound shortened
            ("Papantoniou", "Papantoniu"),      # spelling variant
        ],
    )
    def test_no_surname_escape_hands_back_agreement_over_two_people(
        self, surname_stored: str, surname_registry: str
    ) -> None:
        agreed, reason = names_agree(
            Name(family=surname_stored, given="Kenneth"),
            Name(family=surname_registry, given="Margaret"),
        )
        assert not agreed, reason


class TestForenameDifferencesRegistriesProduce:
    """The exceptions, which are the reason this was left alone for so long.

    Each is two registries recording one person, and each was live before the
    check went in. A rule that fires on any of them is a false-alarm machine.
    """

    @pytest.mark.parametrize(
        ("stored", "registry"),
        [
            ("Kenneth P", "K P"),        # the name against the initial
            ("K P", "Kenneth P"),        # and the other way round
            ("Frits H M", "Frits"),      # a middle initial one side omits
            ("Frits", "Frits H M"),
            ("KP", "K P"),               # initials run together, and separated
            ("K P", "KP"),
            ("Jean-Pierre", "Jean Pierre"),   # hyphenation
            ("Jean-Pierre", "J.-P."),
            ("José", "Jose"),                 # an accent `fold` flattens
            ("Esther", "E"),
        ],
    )
    def test_one_person_recorded_two_ways_still_agrees(
        self, stored: str, registry: str
    ) -> None:
        agreed, reason = names_agree(
            Name(family="Molina-Montes", given=stored),
            Name(family="Molina-Montes", given=registry),
        )
        assert agreed, reason

    def test_a_forename_the_registrys_own_bytes_destroyed_is_read_both_ways(self) -> None:
        """`Émile` mis-decoded arrives as `Ã\x89mile`, whose lead byte folds to `a`.

        The surname rules already repair mojibake — the deposit they are built
        on carries `InÃ©s` and `AndrÃ©s` beside the damaged surnames — so
        without the same reading on the forename, a byline that is *provably*
        byte-damaged fails on any creator whose damage reached the first letter.
        `Ángel` is not this test: `Ã` and `Á` both fold to `a`, so the raw
        reading already agrees and the repair proves nothing.
        """
        agreed, reason = names_agree(
            Name(family="Molina-Montes", given="Émile"),
            Name(family="Molina-Montes", given="Ã\x89mile"),
        )
        assert agreed, reason

    @pytest.mark.parametrize("given", ["健太", "Владимир", "الحسن"])
    def test_a_forename_outside_the_comparison_alphabet_is_not_a_disagreement(
        self, given: str
    ) -> None:
        """Absence of evidence, and the check stands down rather than guessing."""
        agreed, reason = names_agree(
            Name(family="Yamada", given=given), Name(family="Yamada", given="Taro")
        )
        assert agreed, reason

    @pytest.mark.parametrize("side", ["stored", "registry"])
    def test_a_registry_or_an_entry_that_gives_no_forename_supplies_no_evidence(
        self, side: str
    ) -> None:
        names = {"stored": "Esther", "registry": "Margaret"}
        names[side] = ""
        agreed, reason = names_agree(
            Name(family="Molina-Montes", given=names["stored"]),
            Name(family="Molina-Montes", given=names["registry"]),
        )
        assert agreed, reason

    def test_a_compound_surname_the_registry_divides_differently(self) -> None:
        """Crossref on 10.1016/0002-9378(83)90252-1 (PMID 6650633).

        The deposit files *A. López Bernal* as `"family":"Bernal"`,
        `"given":"López"`. The surnames still agree — a compound shortened to
        its final element — but the `given` field is then holding surname, and
        comparing it against `A` accuses a correct citation.
        """
        agreed, reason = names_agree(
            Name(family="Lopez Bernal", given="A"), Name(family="Bernal", given="López")
        )
        assert agreed, reason
        assert reason == "compound surname shortened"

    @pytest.mark.parametrize(
        ("surname", "forename"), [("Li", "Li"), ("Yang", "Yang"), ("Wei", "Wei")]
    )
    def test_a_creator_whose_forename_is_also_their_surname_is_still_compared(
        self, surname: str, forename: str
    ) -> None:
        """A person named *Li Li* is not a compound surname divided two ways.

        Both registries file `Yang, Yang` for creator five of PMID 39780408
        (10.1111/jog.16205), and the subset test then reads the `given` field as
        surname and stands the whole comparison down — so an entry crediting
        somebody else entirely came back agreed, with no reason attached and
        nothing for `--show-suppressed` to recover. The shape is 54 of 51,534
        compared creator positions in live MEDLINE/Crossref pairs.
        """
        agreed, reason = names_agree(
            Name(family=surname, given="Zbigniew"), Name(family=surname, given=forename)
        )
        assert not agreed, reason

    def test_a_creator_credited_to_somebody_else_reaches_the_report(self) -> None:
        """The end of the same story: a position that returned `OK` with no reason."""
        diff = compare_author_lists(
            [Name(family="Yang", given="Zbigniew")], [Name(family="Yang", given="Yang")]
        )
        assert diff.miscredited == [(1, "Yang, Zbigniew", "Yang, Yang")]
        assert not diff.clean

    def test_a_surname_element_in_the_given_field_is_not_read_as_a_forename(
        self,
    ) -> None:
        """What the stand-down is for, on a surname of one token.

        Crossref files *María Maitre Azcárate* as `"family":"Azcarate"`,
        `"given":"Maria Maitre"` against MEDLINE's `Maitre, Azcarate`
        (10.1007/bf00801918) — one surname element each, in opposite fields.
        The two sides disagree about the surname, so the position is reported
        whatever this predicate says; what would be wrong is *what* it says.
        Narrowing the stand-down to a compound surname on the other side instead
        makes this a forename disagreement at 15 of 51,534 live positions, every
        one of them a registry that filed `given` and `family` the other way
        round.
        """
        assert not _forenames_are_incompatible(
            Name(family="Maitre", given="Azcarate"),
            Name(family="Azcarate", given="Maria Maitre"),
        )
        # And it is the surname the report names.
        diff = compare_author_lists(
            [Name(family="Maitre", given="Azcarate")],
            [Name(family="Azcarate", given="Maria Maitre")],
        )
        assert diff.mismatches == [(1, "Maitre, Azcarate", "Azcarate, Maria Maitre")]

    def test_a_forename_that_merely_collides_with_a_surname_element_still_fires(self) -> None:
        """Every token, not any: `Lopez Miguel` is a forename that is not surname."""
        agreed, reason = names_agree(
            Name(family="Lopez Bernal", given="A"),
            Name(family="Bernal", given="Lopez Miguel"),
        )
        assert not agreed, reason
