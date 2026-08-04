"""Author-name parsing and comparison.

Comparing author lists is where a field-level checker earns its keep and where
it most easily becomes a false-alarm machine. Every rule below exists because a
real bibliography produced a spurious mismatch without it:

* ``The Endogenous Hormones and Breast Cancer Collaborative Group`` is one
  collective author. Splitting BibTeX on ``" and "`` turns it into two people
  and reports a two-versus-one author-count defect.
* ``and others`` is BibTeX's *et al.*, not a person named "others".
* ``Clavel-Chapelon`` and ``van Eijck`` must survive as single surnames.
* Crossref sometimes returns UTF-8 that was decoded as Latin-1 somewhere in its
  pipeline: *Gómez* arrives as ``GÃ³mez``. That is a registry defect, and the
  right response is to repair it and note it, not to accuse the bibliography.
* A registry byline can be short an author outright. Crossref's record for
  ``10.1097/00008469-199710000-00007`` begins at the paper's *second* author,
  so a position-against-position comparison reports every one of ten correct
  creators as the wrong person.

The two list-level rules at the bottom of this module — an omitted leading
author, and a surname truncated inside a byline that is provably mojibake —
each make the tool complain *less*, which is the direction in which a mistake
hides. Both are therefore written to the narrowest shape that covers the
witnessed instance, both name that instance by DOI, and both are paired in
``tests/test_names.py`` with a test proving that the same shape without the
corroborating evidence is still reported.

Three of the pair-level rules in :func:`names_agree` are held to the same
standard, because a suppression that clears a *pair* silently supplies the
evidence a list-level rule then counts:

* a surname neither side can express as a comparison key is *unknown*, not
  *equal*. ``fold`` keeps only ``[a-z0-9]``, so ``王``, ``Παπαδόπουλος`` and
  ``الحسن`` all fold to the empty string — and "one side has no surname" then
  cleared ``Smith`` against ``李`` and, worse, cleared three of them in a row,
  which is exactly the arithmetic :func:`_registry_omits_first_author` treats as
  proof;
* a lost *leading* character is only a registry defect when the deposit itself
  shows the damage. A four-character floor does not separate ``Rice`` from
  *Price* or ``Ross`` from *Gross*, and no floor separates ``Handler`` from
  *Chandler*; what does is that the witnessed instance arrives in lower case
  (``"family":"ierssen"``) in a byline that capitalises every other surname;
* "spelling variant" once accepted any two surnames sharing three leading
  characters, which is *Chan* against *Chang*, *Wan* against *Wang* and *Martin*
  against *Martinez* — three of the commonest surnames in the literature, each
  cleared against a different family.
"""

from __future__ import annotations

import re

from .model import Name
from .normalize import clean, fold

__all__ = [
    "ARTIFACT_REASONS",
    "AuthorDiff",
    "compare_author_lists",
    "demojibake",
    "family_key",
    "names_agree",
    "parse_name",
    "parse_name_list",
]


#: Words that mark a corporate or collaborative author. A name containing one of
#: these and no comma is treated as a single literal creator rather than being
#: split on " and ".
_COLLECTIVE_MARKERS = frozenset(
    {
        "group", "consortium", "collaboration", "collaborators", "collaborative",
        "committee", "network", "team", "investigators", "study", "trial",
        "society", "association", "institute", "organization", "organisation",
        "council", "panel", "workgroup", "initiative", "project", "centre",
        "center", "department", "ministry", "agency", "task", "force",
    }
)

#: Nobiliary particles and prefixes that belong to the surname. Kept attached for
#: the primary comparison and stripped for a secondary one, because registries
#: disagree about whether "van Eijck" files under V or E.
_PARTICLES = frozenset(
    {
        "van", "von", "der", "den", "de", "del", "della", "di", "da", "dos",
        "das", "du", "la", "le", "les", "el", "al", "bin", "ibn", "ter", "ten",
        "af", "av", "zu", "y", "i", "mac", "mc", "st",
    }
)

#: BibTeX's truncation marker.
_ET_AL_TOKENS = frozenset({"others", "et al", "et al.", "and others"})

#: Creator separators. ``and`` is BibTeX's; ``&`` is what a human-written table
#: cell uses, and this parser reads both because :mod:`bibaudit.adapters.markdown`
#: hands it Quarto table cells as well as ``.bib`` fields. ``Riboli & Kaaks``
#: (``sources/epic.qmd``, 10.1093/ije/26.suppl_1.s6) parsed as *one* creator
#: named "Kaaks" with the forename "Riboli &", so a correct two-author row was
#: compared against Crossref as a single mis-spelled person.
_SPLIT_CREATORS = re.compile(r"\s+and\s+|\s*&\s*", re.IGNORECASE)

#: A truncation marker written at the *end* of a byline rather than as its own
#: element: ``Gomez-Rubio et al.`` (``sources/pangeneu.qmd``,
#: 10.1093/annonc/mdx167). BibTeX writes ``Gomez-Rubio and others``, which the
#: separator above already splits into a name and a marker; a display string
#: writes it joined, and that parsed as a creator whose *surname* was ``al.``
#: with :attr:`~bibaudit.model.Name.et_al` unset — so the list was then compared
#: against the registry as though it were complete. 442 rows across the Quarto
#: corpus are written this way.
#:
#: ``et`` and ``al`` must both be whole tokens, so a surname such as *Etal* or
#: *Alal* cannot be eaten; the leading separator is optional because
#: ``Smith, A., et al.`` is as common as ``Smith et al.``
_TRAILING_ET_AL = re.compile(r"[,;]?\s+et\.?\s+al\.?\s*$", re.IGNORECASE)

_SPACE_RE = re.compile(r"\s+")

#: How many creators must still align, in order, before a registry byline that
#: is missing its first author is accepted as a registry defect rather than
#: reported. Two surnames agreeing in sequence is something a companion paper by
#: an overlapping team produces routinely; three in exact order does not happen
#: by accident. See :func:`_registry_omits_first_author`.
_MIN_ALIGNED_AFTER_OMISSION = 3

#: Shortest registry surname that may be accepted as a first-character-truncated
#: form of the stored one. ``Dierssen`` -> ``ierssen`` is seven; below four the
#: remaining stem carries too little information to distinguish damage from a
#: different person (``Ash`` against ``Nash``). See
#: :func:`_surname_truncated_by_mojibake`.
#:
#: The floor is a sanity bound and **nothing more**: it does not, and cannot,
#: separate damage from a different family. Real surname pairs one leading
#: character apart exist at every length — ``Rice``/*Price*, ``Ross``/*Gross*,
#: ``Lake``/*Blake*, ``Hang``/*Zhang* at four; ``Ellis``/*Kellis*,
#: ``Rooks``/*Brooks* at five; ``Herman``/*Sherman* at six;
#: ``Rossman``/*Grossman*, ``Handler``/*Chandler* at seven. Raising the number
#: only moves the collision. What actually does the separating is the case
#: evidence in :func:`_registry_surname_is_anomalously_lowercase`.
_MIN_TRUNCATED_SURNAME = 4

#: How many *other* creators in the same registry byline must carry a
#: capitalised surname before an uncapitalised one is read as byte damage rather
#: than as house style. One would be a coincidence; two is a convention. In the
#: witnessed deposit (10.5271/sjweh.3626) twenty-three of the twenty-four
#: creators are capitalised and only ``ierssen`` is not. See
#: :func:`_registry_surname_is_anomalously_lowercase`.
_MIN_CAPITALISED_WITNESSES = 2

#: Shortest surname on which a single-character difference still reads as one
#: spelling of one name rather than as two names. *Chan* and *Chang*, *Wan* and
#: *Wang*, *Lin* and *Liu* are one edit apart and are different families; they
#: are also, at three and four characters, among the commonest surnames there
#: are. See :func:`names_agree`.
_MIN_SPELLING_VARIANT_SURNAME = 6

#: Leads of a mis-decoded two-byte UTF-8 sequence whose *second* character may be
#: restored by :func:`_restore_nfkc_continuations`. Only the Latin-1 Supplement
#: leads (UTF-8 ``C2``/``C3``) are listed. The Cyrillic ones (``D0``/``D1``,
#: seen as ``Ð``/``Ñ``) are deliberately excluded: ``Ñ`` is an ordinary Spanish
#: letter, and substituting after it would turn a real surname beginning
#: ``Ño`` into a byte pair that happens to decode, inventing a repair where the
#: unmodified round trip correctly refuses one.
_NFKC_INVERSE_LEADS = "ÃÂ"

#: NFKC images of the Latin-1 characters that can stand as the *second*
#: character of a mis-decoded two-byte UTF-8 sequence.
#:
#: This exists because of an interaction with :func:`~bibaudit.normalize.clean`,
#: which every creator string has already been through by the time it reaches
#: this module: ``clean`` applies NFKC, and NFKC rewrites nine Latin-1 code
#: points to ASCII. Superscript three is one of them. So *Gómez* mis-decoded is
#: ``GÃ³mez``, but what arrives here is ``GÃ3mez`` — and that no longer encodes
#: to valid UTF-8, so the round trip below fails and a correct bibliography is
#: accused of a surname mismatch. Auditable instance: Crossref's record for
#: 10.5271/sjweh.3626 (``papantoniou2017colorectal``) gives ``PÃ©rez-GÃ³mez``
#: for *Pérez-Gómez* at position 8; ``fold`` turns the stored form into
#: ``perez gomez`` and the registry form into ``parez ga3mez``, which agree
#: nowhere. The fixture is ``tests/data/names_crossref_mojibake_author_list.json``.
#:
#: Restoring the code point is the deterministic inverse of a known lossy step,
#: not a guess, and it is applied *only* to a character standing immediately
#: after a mojibake lead — a position where a bare ASCII digit cannot arise any
#: other way. The other NFKC-altered Latin-1 points are left out on purpose:
#: U+00A0 has already been collapsed to a space by ``clean``'s whitespace pass
#: and cannot be told from a real one, and the vulgar fractions expand to three
#: characters, so neither can be inverted by a one-for-one substitution.
_NFKC_CONTINUATION_INVERSE = {
    "1": "¹",  # SUPERSCRIPT ONE
    "2": "²",  # SUPERSCRIPT TWO
    "3": "³",  # SUPERSCRIPT THREE
    "a": "ª",  # FEMININE ORDINAL INDICATOR
    "o": "º",  # MASCULINE ORDINAL INDICATOR
}


def _restore_nfkc_continuations(text: str) -> str:
    """Undo NFKC's rewriting of the second character of a mojibake pair.

    Only a character directly following one of :data:`_NFKC_INVERSE_LEADS` is
    touched, and only if NFKC could have produced it, so ordinary text is
    returned unchanged.
    """
    out = list(text)
    for index in range(1, len(out)):
        if out[index - 1] in _NFKC_INVERSE_LEADS:
            out[index] = _NFKC_CONTINUATION_INVERSE.get(out[index], out[index])
    return "".join(out)


def demojibake(text: str) -> tuple[str, bool]:
    """Repair UTF-8 that was decoded as Latin-1, if that is what *text* is.

    ``Gómez`` mis-decoded becomes ``GÃ³mez``; round-tripping through Latin-1
    recovers the original exactly. The round trip is attempted only when the
    string contains one of the tell-tale sequences, and the result is accepted
    only if it decodes cleanly, so ordinary text with a legitimate ``Ã`` is left
    alone.

    If the direct round trip fails, one further candidate is tried, in which
    NFKC's rewriting of the pair's second character is undone first — see
    :data:`_NFKC_CONTINUATION_INVERSE`. Nothing else is attempted: a string that
    does not decode as UTF-8 after that is not mojibake, and guessing further
    would be exactly the kind of unfalsifiable repair this tool refuses to make.

    Returns the (possibly repaired) string and whether a repair happened.
    """
    if not any(marker in text for marker in ("Ã", "Â", "â€", "Ð", "Ñ")):
        return text, False
    for candidate in (text, _restore_nfkc_continuations(text)):
        try:
            repaired = candidate.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        # A successful repair removes the marker; if it survives, this was not
        # mojibake.
        if repaired == text or "Ã" in repaired or "Â" in repaired:
            continue
        return repaired, True
    return text, False


def _is_collective(text: str) -> bool:
    """True if *text* reads as an organisation rather than a person."""
    tokens = set(fold(text).split())
    if not tokens & _COLLECTIVE_MARKERS:
        return False
    # "Smith, John" has a comma and is a person even if he works at an Institute;
    # collective names in author fields are given whole.
    return "," not in text


def parse_name(raw: str) -> Name:
    """Parse a single creator string in either BibTeX order.

    Handles ``Family, Given``, ``Given Family``, brace-protected literals
    (``{World Health Organization}``) and particle-carrying surnames.
    """
    text = clean(raw).strip()
    if not text:
        return Name()

    if fold(text) in {fold(t) for t in _ET_AL_TOKENS}:
        return Name(literal=text, et_al=True)

    # Brace protection in the source is an explicit "this is one unit" marker;
    # clean() has already removed the braces, so check the raw string.
    braced = raw.strip().startswith("{") and raw.strip().endswith("}")
    if braced or _is_collective(text):
        return Name(literal=text, collective=True)

    if "," in text:
        family, _, given = text.partition(",")
        return Name(family=family.strip(), given=given.strip())

    tokens = text.split()
    if len(tokens) == 1:
        return Name(family=tokens[0])

    # Scan from the right for the start of the surname, absorbing particles:
    # "Casper H. J. van Eijck" -> family "van Eijck".
    start = len(tokens) - 1
    while start > 0 and fold(tokens[start - 1]) in _PARTICLES:
        start -= 1
    return Name(family=" ".join(tokens[start:]), given=" ".join(tokens[:start]))


def parse_name_list(raw: str) -> list[Name]:
    """Parse a creator field — a BibTeX ``author``/``editor``, or a table cell.

    The field is split on ``" and "`` and on ``"&"`` (see
    :data:`_SPLIT_CREATORS`), but a collective author containing either is
    reassembled: ``The Endogenous Hormones and Breast Cancer Collaborative
    Group`` is one creator, not two.

    A trailing ``et al.`` becomes its own :class:`~bibaudit.model.Name` with
    :attr:`~bibaudit.model.Name.et_al` set, exactly as BibTeX's ``and others``
    already did — that flag is what tells :func:`compare_author_lists` the list
    is truncated and its length states nothing. Left in place it was parsed as a
    creator surnamed ``al.``, and the byline was then compared as though it were
    complete.
    """
    text = clean(raw)
    if not text:
        return []

    truncated = _TRAILING_ET_AL.search(text)
    if truncated and text[: truncated.start()].strip():
        # Only when a name remains in front of it. A cell holding nothing but
        # "et al." is already a bare marker and ``parse_name`` handles it.
        marker = text[truncated.start() :].strip(" ,;")
        text = text[: truncated.start()].strip()
    else:
        marker = ""

    parts = [p.strip() for p in _SPLIT_CREATORS.split(text) if p.strip()]
    if len(parts) > 1 and _is_collective(text):
        # The whole field reads as one organisation. Splitting it produced
        # fragments ("The Endogenous Hormones") that are not names.
        names = [Name(literal=text, collective=True)]
    else:
        names = [parse_name(p) for p in parts if p]

    if marker:
        names.append(Name(literal=marker, et_al=True))
    return names


def family_key(name: Name, *, drop_particles: bool = False) -> str:
    """Folded surname used for comparison.

    The whole family name is used, never merely its last token: reducing
    ``Aragonés`` to its final token is how a mojibake surname (``AragonÃ©s``,
    which folds to ``aragona s``) silently becomes the single letter ``s``.
    """
    if name.literal:
        return fold(name.literal)
    key = fold(name.family)
    if drop_particles:
        tokens = [t for t in key.split() if t not in _PARTICLES]
        key = " ".join(tokens) or key
    return key


def _given_initials(name: Name) -> str:
    """First letter of each forename token, folded — ``"J. M."`` -> ``"jm"``."""
    return "".join(t[0] for t in fold(name.given).split() if t)


def _surname_text(name: Name) -> str:
    """The surname as a report would print it, before :func:`fold` touches it.

    Mirrors :func:`family_key`'s choice of field so the two can never disagree
    about *which* string a creator's surname is.
    """
    return clean(name.literal or name.family)


#: Reasons :func:`names_agree` returns when it accepted a pair **without any
#: evidence that the two creators are the same person**. Each is an honest "this
#: comparison had nothing to work with", and each is printed under
#: REGISTRY-ARTIFACT — but none of them may be counted as a creator that
#: *aligned*, which is what :func:`_registry_omits_first_author` does with its
#: three-in-a-row arithmetic. Three agreements of this kind are three pieces of
#: nothing, and before :func:`_agrees_informatively` existed they were enough to
#: clear a whole byline: a registry list of ``[王, 李, 张]`` "aligned" against
#: ``[Smith, Jones, Brown]`` and suppressed the author-count difference too.
_NO_SURNAME = "one side has no surname"
_UNREPRESENTABLE_SCRIPT = "surname outside the comparison alphabet"
_REGISTRY_INITIAL_ONLY = "registry surname truncated"
_ET_AL_REASON = "et-al marker"

_UNINFORMATIVE_AGREEMENTS = frozenset(
    {_NO_SURNAME, _UNREPRESENTABLE_SCRIPT, _REGISTRY_INITIAL_ONLY, _ET_AL_REASON}
)

#: Every reason this module attaches to an author position, each of which
#: ``compare._check_authors`` turns into a ``REGISTRY-ARTIFACT`` suppression.
#:
#: The author half of the contract ``benign.CHECKS`` carries: a suppression a
#: reader cannot look up is one nobody can challenge, so each string here has a
#: section in ``docs/registry-artifacts.md`` and ``tests/test_benign.py`` fails
#: the build when one does not. These escapes live here rather than in
#: ``benign.py`` because they are decided while walking two bylines in step,
#: where a field-level check has nothing to compare.
#:
#: Built from the constants above rather than retyping them, so a reworded
#: reason cannot keep a stale entry here and quietly satisfy that test. The
#: interleaved-collectives entry is a *prefix*: it goes on to name the
#: organisations in full, because "these particular creators are organisations
#: the bibliography left out" is not a claim a reader can check against a count.
ARTIFACT_REASONS: tuple[str, ...] = (
    _NO_SURNAME,
    _UNREPRESENTABLE_SCRIPT,
    _REGISTRY_INITIAL_ONLY,
    _ET_AL_REASON,
    "registry mojibake",
    "particle filing",
    "compound surname shortened",
    "spelling variant with matching initials",
    "collective author",
    "registry lists a collective author",
    "registry interleaves collective creator(s) the byline omits: ",
    "registry omits the first author",
    "registry mojibake truncated the surname",
    "reordered",
)


def _script_key(text: str) -> str:
    """Comparison key for a surname :func:`fold` cannot represent.

    ``fold`` keeps only ``[a-z0-9]``, so every surname written in Han, Greek,
    Cyrillic, Hebrew, Arabic, Hangul or Kana folds to the empty string. That is
    not "this creator has no surname"; it is "this creator's surname is outside
    the comparison alphabet", and the two must not be conflated — ``fold``
    returning nothing for both ``王`` and ``李`` is why they compared equal.

    ``clean`` has already applied NFKC and collapsed whitespace, so all that is
    left is to drop the spacing a registry may or may not put between name
    elements and to case-fold for the scripts that have case. Simplified and
    traditional Han forms (``张``/``張``) are *not* unified — no deterministic
    mapping between them exists in the standard library — so an entry holding
    one and a registry holding the other is reported. That is the safe
    direction: reporting a difference a reader can adjudicate costs less than
    clearing two different families.
    """
    return _SPACE_RE.sub("", clean(text)).casefold()


def _agree_without_a_comparison_key(stored: Name, registry: Name) -> tuple[bool, str]:
    """Decide a pair where at least one side folds to an empty key.

    Two situations arrive here and they are not the same fact:

    * one side genuinely carries no surname — a Crossref stub deposit, a
      MEDLINE ``AU`` entry with nothing in it. There is nothing to compare and
      nothing to report;
    * both sides carry a surname, but at least one is written in a script
      :func:`fold` discards. If both are, the raw glyphs are compared directly
      by :func:`_script_key`, because ``王`` and ``李`` differ and a checker that
      says otherwise is not checking. If only one is, the two are most likely a
      native and a romanised form of one name — a Crossref or DataCite deposit
      from a Japanese, Korean or Russian publisher against a ``.bib`` exported
      in Latin script — and no key can show it, so the pair is accepted and the
      reason is *stated* rather than left silent.

    Before accepting for want of a key, the one piece of evidence that survives
    romanisation is used: the **first forename initial**. It is script-independent
    when the registry romanises forenames (which most do even when the surname
    is not), it is cheap, and without it a byline of ``[Smith J, Jones A,
    Brown B]`` compared clean against ``[王 L, 李 M, 张 N]`` — three surnames
    that fold to nothing and three forenames that agree with nothing. Only the
    *first* initial is compared: a stored "T. K." against a registry "T." is one
    person recorded twice, and demanding the whole initial string would invent a
    mismatch on every entry that carries a middle name the registry drops.
    """
    stored_text, registry_text = _surname_text(stored), _surname_text(registry)
    if stored_text and registry_text and not fold(stored_text) and not fold(registry_text):
        if _script_key(stored_text) == _script_key(registry_text):
            return True, ""
        return False, ""

    stored_initial = _given_initials(stored)[:1]
    registry_initial = _given_initials(registry)[:1]
    if stored_initial and registry_initial and stored_initial != registry_initial:
        return False, ""

    if not stored_text or not registry_text:
        return True, _NO_SURNAME
    return True, _UNREPRESENTABLE_SCRIPT


def _differs_by_one_edit(left: str, right: str) -> bool:
    """True if one substitution, insertion or deletion turns *left* into *right*.

    Written out rather than taken from ``rapidfuzz`` because only the answer
    "at most one" is ever wanted and the bounded form is four lines; a distance
    function invites a caller to raise the bound later, which is the direction
    in which this rule stops meaning anything.
    """
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) > len(right):
        left, right = right, left
    edits = 0
    i = j = 0
    while i < len(left) and j < len(right):
        if left[i] == right[j]:
            i += 1
            j += 1
            continue
        edits += 1
        if edits > 1:
            return False
        # Equal lengths means the one edit is a substitution and both cursors
        # advance; otherwise it is an insertion into the longer string.
        if len(left) == len(right):
            i += 1
        j += 1
    return edits + (len(right) - j) + (len(left) - i) == 1


def names_agree(stored: Name, registry: Name) -> tuple[bool, str]:
    """Whether two creators denote the same person.

    Returns the decision and a short reason, which the report uses to explain
    why an apparent mismatch was accepted. Forenames are compared only by
    initial: registries record "E", "Esther" and "Esther M." for one person, and
    demanding equality there invents mismatches on nearly every entry.

    A reason listed in :data:`_UNINFORMATIVE_AGREEMENTS` means "accepted for
    want of anything to compare", not "shown to be the same person". Callers
    that count agreements as evidence must use :func:`_agrees_informatively`.
    """
    if stored.et_al or registry.et_al:
        return True, _ET_AL_REASON

    stored_key = family_key(stored)
    registry_key = family_key(registry)

    if not stored_key or not registry_key:
        return _agree_without_a_comparison_key(stored, registry)

    if stored_key == registry_key:
        return True, ""

    # A registry value that only differs by mojibake is a registry defect.
    repaired, was_mojibake = demojibake(
        registry.literal or registry.family or ""
    )
    if was_mojibake and fold(repaired) == stored_key:
        return True, "registry mojibake"

    # Particle filing differences: "van Eijck" vs "Eijck".
    if family_key(stored, drop_particles=True) == family_key(registry, drop_particles=True):
        return True, "particle filing"

    # Hyphen and space are interchangeable in compound surnames, and fold()
    # already turns both into a space, so a remaining difference is real —
    # except when one side kept only the final element ("Chapelon" for
    # "Clavel-Chapelon"), which some registries do.
    #
    # One side must be strictly shorter and must be a **token-level suffix** of
    # the other. Comparing final tokens alone is the rule CLAUDE.md forbids
    # under another name: Spanish and Portuguese bylines carry two surnames and
    # the second is inherited from the mother, so very large numbers of
    # unrelated people share it. ``tests/test_audit_corpus.py`` counts 255
    # distinct pairs in the 438-entry corpus that a final-token test merges,
    # among them `Krebs-Smith, Susan M.` (reedy2014dietquality,
    # 10.3945/jn.113.189407) against `Davey Smith, George` (carrerastorres2017mr,
    # 10.1093/jnci/djx012), and `González-González, Rocío` (gil2019colorectal,
    # 10.1002/pds.4686) against `Martínez-González, M` (willame2023access,
    # 10.1016/j.vaccine.2022.11.031) — two people apiece, merged into one.
    # Requiring a suffix separates 123 of the 255 and costs none of the
    # witnessed benign cases: `chapelon` closes `clavel chapelon`, and Crossref's
    # own defect on 10.1007/s10689-024-00397-w (marianinirios2024risk), which
    # deposits the forename inside the surname as `Cristina-Marianini-Rios`,
    # still ends on the stored `marianini rios`.
    #
    # The strict inequality also does the work the old length guard did: the
    # longer list has at least two tokens, so it really is compound, and two
    # unrelated single-token surnames can never be accepted this way.
    stored_parts = stored_key.split()
    registry_parts = registry_key.split()
    shorter, longer = sorted((stored_parts, registry_parts), key=len)
    if shorter and len(shorter) < len(longer) and longer[-len(shorter):] == shorter:
        return True, "compound surname shortened"

    # A single-letter registry surname carries no information; treat it as the
    # registry being incomplete rather than as a contradiction.
    if len(registry_key) <= 1:
        return True, _REGISTRY_INITIAL_ONLY

    # **NO WITNESSED INSTANCE.** No entry in the 438-entry corpus reaches this
    # branch: `TODO.md`'s baseline lists eight author flags and every one of them
    # is a collective author, an et-al marker, the E3N omission or Latin-1
    # mojibake. It is kept, narrowly, for the transliteration variants a
    # multilingual bibliography does produce — `Kowalski`/`Kowalska`,
    # `Ivanov`/`Ivanova`, `Papantoniou`/`Papantoniu` — and it is a candidate for
    # deletion, not for widening.
    #
    # It used to accept *any* two surnames sharing three leading characters when
    # the forename initials matched, which is not a spelling variant, it is a
    # prefix: `Chan`/`Chang`, `Wan`/`Wang`, `Martin`/`Martinez`,
    # `Smith`/`Smithers`, `Gonzalez`/`Gonzalo`, `Sancho`/`Sanchez` — pairs of
    # different families, each cleared against the other on a shared initial
    # that thousands of researchers share. Three conditions now stand in the way,
    # and each names what it excludes:
    #
    # * one edit, not a shared prefix, so `Martinez` (two edits from `Martin`)
    #   and `Smithers` (four from `Smith`) are reported;
    # * the same first character, so a *leading* character difference is never
    #   waved through here — that is the damage
    #   :func:`_surname_truncated_by_mojibake` handles under evidence, and
    #   letting it in for free would leave `Herman`/`Sherman` and
    #   `Rossman`/`Grossman` cleared with no evidence at all;
    # * at least `_MIN_SPELLING_VARIANT_SURNAME` characters on both sides, so
    #   the short surnames where one edit is a whole other family — `Chan`,
    #   `Chang`, `Wan`, `Wang`, `Lin`, `Liu`, `Kim`, `Kum` — are reported.
    initials_match = (
        _given_initials(stored)
        and _given_initials(stored) == _given_initials(registry)
    )
    if (
        initials_match
        and len(stored_key) >= _MIN_SPELLING_VARIANT_SURNAME
        and len(registry_key) >= _MIN_SPELLING_VARIANT_SURNAME
        and stored_key[0] == registry_key[0]
        and _differs_by_one_edit(stored_key, registry_key)
    ):
        return True, "spelling variant with matching initials"

    return False, ""


def _agrees_informatively(stored: Name, registry: Name) -> bool:
    """True if the pair agrees **and** something was actually compared.

    The distinction exists because :func:`_registry_omits_first_author` treats a
    run of agreements as proof, and :func:`names_agree` returns agreement in
    several situations that prove nothing at all — see
    :data:`_UNINFORMATIVE_AGREEMENTS`.
    """
    agreed, reason = names_agree(stored, registry)
    return agreed and reason not in _UNINFORMATIVE_AGREEMENTS


def _registry_omits_first_author(stored: list[Name], registry: list[Name]) -> bool:
    """True if *registry* is *stored* with exactly its first creator removed.

    The witnessed instance is Crossref's record for
    ``10.1097/00008469-199710000-00007`` — Clavel-Chapelon et al., *E3N, a
    French cohort study on cancer risk factors*, Eur J Cancer Prev 1997, stored
    in the corpus as ``clavelchapelon1997e3n``. The paper's byline opens with
    Clavel-Chapelon; Crossref's deposit holds nine creators opening with
    ``van Liere``, which it even marks ``"sequence": "first"``. Ovid/Wolters
    Kluwer deposited the byline one creator short. Compared position against
    position that is ten consecutive "different person" findings on an entry
    whose author list is exactly right — and one FIELD-MISMATCH verdict, which
    fails a build. The recorded response is in
    ``tests/data/names_crossref_first_author_omitted.json``.

    Alignment, not a special case for one citekey: the registry list has to be a
    contiguous subsequence of the stored one, missing a run from the front. The
    conditions below are narrow on purpose, because the *same shape* is produced
    by a bibliography that prepends an author who was never on the paper — a
    documented failure mode of generated reference lists — and no registry
    record can tell the two apart:

    * **Exactly one creator missing, and it is the first.** One is the only
      omission length with a witnessed instance. A registry that dropped a run
      of leading authors has not been observed here, and tolerating one would be
      a suppression with nothing behind it.
    * **At least** ``_MIN_ALIGNED_AFTER_OMISSION`` **creators remain, and every
      one agrees in order — informatively.** A single disagreement anywhere in
      the tail means this is not an off-by-one deposit and the ordinary
      positional comparison must run instead. "Informatively" is
      :func:`_agrees_informatively` and it is load-bearing: ``names_agree``
      returns agreement in several situations where nothing was compared, and
      this arithmetic counted them. A registry byline of ``[王, 李, 张]``
      "aligned" against ``[Smith, Jones, Brown]`` — three surnames ``fold``
      reduces to the empty key — and the entry came back clean with its
      author-count difference suppressed as well; so did a byline of
      initials-for-surnames, which agrees with anything by the same route.
    * **The dropped surname appears nowhere in the registry list.** Otherwise a
      bibliography that repeats an author at the head of its own list — the
      commonest way a hand-edited ``.bib`` entry goes wrong — would be silently
      realigned instead of reported.
    * **Neither side carries an et-al marker.** Past such a marker a list is
      truncated and its length states nothing, so the arithmetic here would be
      comparing a fact against a placeholder.

    Deliberately *not* covered, and each is still reported: a run missing from
    the middle; a registry list sharing only a minority of the names; and the
    mirror case where the *bibliography* is the short list, which is a citation
    that has lost its first author and is exactly the sort of attribution error
    a reader wants to see.

    **What this rule cannot do, and does not claim to.** The comment above that
    "three in exact order does not happen by accident" is false as stated, and
    the arithmetic must not be read as though it were true. Two papers from one
    laboratory routinely share every author but one, in the same order, because
    that is what a research group is; run the recorded E3N byline through this
    predicate with any plausible senior name prepended — ``Riboli, E`` ahead of
    the nine real creators — and it is accepted, because the shape is
    *identical* to the deposit defect and no author list can tell them apart.
    The distinguishing evidence exists but is outside this module: the citekey
    (``clavelchapelon1997e3n`` names the creator the registry dropped) and the
    title comparison. Anyone tightening this further should gate it there, in
    ``compare``, rather than by raising the count here, which buys nothing.
    """
    if len(stored) - len(registry) != 1:
        return False
    if len(registry) < _MIN_ALIGNED_AFTER_OMISSION:
        return False
    if any(n.et_al for n in stored) or any(n.et_al for n in registry):
        return False
    # ``strict=True`` because the length guard above already makes the two
    # sequences equal: if that ever stops holding, a silently truncated zip
    # would let a shorter registry list "align" against a longer stored one and
    # suppress the difference. Better to raise than to under-report.
    if not all(
        _agrees_informatively(left, right)
        for left, right in zip(stored[1:], registry, strict=True)
    ):
        return False
    dropped = family_key(stored[0])
    return bool(dropped) and dropped not in {family_key(n) for n in registry}


def _interleaved_collectives(stored: list[Name], registry: list[Name]) -> list[Name]:
    """Registry creators that are consortia the stored byline simply omits.

    Returns them in registry order, or an empty list when the shape does not
    hold — so the caller can both branch on it and name what it excused.

    The witnessed instance is Crossref's record for
    ``10.1158/1055-9965.epi-23-0009`` (Kim et al., *Cancer Epidemiol Biomarkers
    Prev* 2023, stored as ``kim2023abo``). Its ``author`` array has nine
    creators: seven people and, at **positions 5 and 7**, ``{"name": "for the
    Pancreatic Cancer Cohort Consortium (PanScan)"}`` and ``{"name": "for the
    Pancreatic Cancer Case-Control Consortium (PanC4)"}``. Crossref's own
    content-negotiated BibTeX — which generated this bibliography — emits
    neither, because BibTeX has no slot for a corporate creator sitting *between*
    two people. The recorded response is
    ``tests/data/audit_crossref_interleaved_collective.json``.

    Compared position against position, registry #5 is a consortium and stored
    #5 is Alison P. Klein, and every position after each insertion is shifted by
    one. On the 438-entry corpus this single shape accounted for 27 of 30
    ``FIELD-MISMATCH`` verdicts and all 15 remaining ``authors``-only
    ``INCOMPLETE`` ones — 42 entries, 9.6% of the file, **not one of which
    disagrees with Crossref about a single person**. It also produced all 1446
    differences suppressed as ``reordered``: with the lists offset, every stored
    name appears somewhere in the registry list and vice versa, so that escape
    absorbed the whole tail and called it a reordering that never happened. A
    reader who meets 27 false alarms in the first screen stops reading, and the
    three genuine defects underneath go into the manuscript anyway.

    **What remains must align exactly**: the people left after the collectives
    are dropped must be the same length as the stored list, and every position
    must agree *informatively* — :func:`_agrees_informatively`, so a run of "one
    side has no surname" cannot stand in for a comparison. That is the evidence
    the suppression rests on, not a tolerance it grants. One substituted person,
    or one person missing from the stored list, and the shape does not hold and
    the ordinary positional comparison runs. Measured over the corpus, all 44
    works whose creator array carries a collective align exactly this way — zero
    mismatches, zero count difference — so the rule is never asked to absorb a
    residue.

    A stored byline that carries the consortium *too* needs nothing from this
    rule: those positions match on both sides and the ordinary comparison
    already passes them.

    **Which creators count as collectives is Crossref's own answer, not a
    guess about the string.** Crossref's deposit schema has two creator shapes,
    ``<person_name>`` (surfacing as ``family``/``given``) and
    ``<organization>`` (surfacing as ``name``), and ``crossref._parse_creators``
    marks the second ``collective=True``. An earlier version of this rule also
    required the literal to carry one of :data:`_COLLECTIVE_MARKERS`, as a guard
    against a deposit that files a *person* under ``name``. It was wrong twice
    over. It does not work — the real corpus credits ``PanScan and PanC4
    consortia`` (plural, 10.1093/jnci/djy155), ``DiscovEHR``, ``GSK``,
    ``AstraZeneca``, ``Bristol Myers Squibb`` and ``UK Biobank``, none of which
    carries a marker word, and 8 correct entries were still failed. And it
    guards nothing worth guarding: BibTeX has no slot for an ``<organization>``
    creator, so the exporter that produced these entries drops every one of
    them whether the publisher filed a company or a person there. Reporting a
    person misfiled into the organisation slot would accuse the bibliography of
    a defect that is the publisher's and that no user can act on.

    The residual is accepted and stated: a person deposited under ``name``
    rather than ``family``, whom the bibliography also omits, is not reported.
    It is bounded by the alignment requirement — every *other* creator has to
    match — and by the fact that the same omission is what any Crossref-derived
    export produces.
    """
    collectives = [n for n in registry if n.collective]
    if not collectives:
        return []
    people = [n for n in registry if not n.collective]
    if len(people) != len(stored) or not people:
        return []
    if any(n.et_al for n in stored) or any(n.et_al for n in registry):
        # Past an et-al marker a list is truncated and its length states
        # nothing, so "what remains aligns exactly" is not a fact that can be
        # established here.
        return []
    if not all(
        _agrees_informatively(left, right)
        for left, right in zip(stored, people, strict=True)
    ):
        return []
    return collectives


def _list_carries_registry_mojibake(stored: list[Name], registry: list[Name]) -> bool:
    """True if some registry creator is provably a Latin-1 mis-decode of the stored one.

    "Provably" is doing the work: the registry string has to round-trip through
    :func:`demojibake` *and* the repaired form has to equal the surname the
    bibliography holds at the same position. That pair of facts localises the
    damage to the registry's own bytes, which is what licenses the narrower
    rule in :func:`_surname_truncated_by_mojibake` for the rest of the list.
    A registry surname that merely looks unusual proves nothing and is ignored.
    """
    # ``strict=False``: the lists may legitimately differ in length here (a
    # truncated byline, an et-al marker), and evidence from the positions they
    # do share is all this predicate needs.
    for left, right in zip(stored, registry, strict=False):
        repaired, was_mojibake = demojibake(right.literal or right.family or "")
        if was_mojibake and fold(repaired) == family_key(left):
            return True
    return False


def _leading_letter(text: str) -> str:
    """First alphabetic character of *text*, or ``""`` if it has none."""
    return next((ch for ch in text if ch.isalpha()), "")


def _files_under_a_particle(name: Name) -> bool:
    """True if the surname opens with a nobiliary particle.

    ``van Eijck``, ``de Sousa`` and ``von Behring`` are filed with a lower-case
    first letter by publishers that follow Dutch and German house style, and
    that is orthography rather than damage. They must not be allowed to make a
    byline look as though it does not capitalise surnames, which would disarm
    :func:`_registry_surname_is_anomalously_lowercase` for every other creator
    in the same deposit.
    """
    tokens = fold(name.literal or name.family).split()
    return bool(tokens) and tokens[0] in _PARTICLES


def _registry_surname_is_anomalously_lowercase(
    stored: Name, registry: Name, byline: list[Name], position: int
) -> bool:
    """True if this registry surname is uncapitalised in a byline that capitalises.

    This is the evidence that separates *byte damage* from *a different family*,
    and it is the only condition in :func:`_surname_truncated_by_mojibake` that
    can. Crossref's deposit for 10.5271/sjweh.3626 gives ``"family":"ierssen"``
    — lower-case ``i`` — while all twenty-three other creators are capitalised
    (``Papantoniou``, ``Espinosa``, ``Ederra``, ...). A registry naming a
    *different* person does not do that: ``Rice``, ``Ross``, ``Lake``,
    ``Ellis``, ``Handler`` and ``Rossman`` arrive capitalised like every other
    surname in their deposits, so each of them is now reported against *Price*,
    *Gross*, *Blake*, *Kellis*, *Chandler* and *Grossman* instead of being
    cleared by a length floor that never distinguished them.

    Three guards keep the signal honest:

    * the stored surname must itself be capitalised, so a bibliography that
      genuinely files a name in lower case is not "repaired" against it;
    * particle-initial surnames are ignored on both sides — see
      :func:`_files_under_a_particle`;
    * at least :data:`_MIN_CAPITALISED_WITNESSES` other creators must be
      capitalised, so a deposit that lower-cases its whole byline (some
      publishers do) supplies no anomaly and every one of its surnames is
      compared normally.
    """
    if not _leading_letter(_surname_text(stored)).isupper():
        return False
    if not _leading_letter(_surname_text(registry)).islower():
        return False
    if _files_under_a_particle(registry):
        return False

    witnesses = 0
    for index, other in enumerate(byline):
        if index == position:
            continue
        letter = _leading_letter(_surname_text(other))
        # A surname in a caseless script (Han, Arabic, Hangul) is neither
        # evidence for the convention nor evidence against it.
        if not letter or _files_under_a_particle(other):
            continue
        if letter.islower():
            return False
        witnesses += 1
    return witnesses >= _MIN_CAPITALISED_WITNESSES


def _surname_truncated_by_mojibake(
    stored: Name, registry: Name, byline: list[Name], position: int
) -> bool:
    """True if the registry surname is the stored one minus its first character.

    Crossref's record for ``10.5271/sjweh.3626`` (Papantoniou et al., *Shift
    work and colorectal cancer risk in the MCC-Spain case-control study*, stored
    as ``papantoniou2017colorectal``) is UTF-8 decoded as Latin-1 throughout:
    ``AragonÃ©s`` for *Aragonés*, ``PÃ©rez-GÃ³mez`` for *Pérez-Gómez*,
    ``GarcÃ\\xada-Palomo`` for *García-Palomo* — and, at position 19,
    ``ierssen`` for *Dierssen*, a surname that lost its first character
    outright. Round-trip repair recovers the first three and cannot touch the
    fourth: there is no byte left to repair. The recorded response is in
    ``tests/data/names_crossref_mojibake_author_list.json``.

    **This predicate is meaningless on its own and must never be called on its
    own.** "Accept a surname missing one leading character" would silence
    ``ash`` against *Nash* and ``reid`` against *Freid*. What makes it
    defensible here is the evidence sitting beside it in the same byline:
    :func:`_list_carries_registry_mojibake` has already shown that *this
    deposit* mis-decoded UTF-8 and that its other surnames still match the
    bibliography. A byline that is demonstrably byte-damaged and agrees
    everywhere else is a registry defect; the identical shape in a clean byline
    is a different surname, and ``tests/test_names.py`` pins that distinction.

    Three further conditions narrow it:

    * exactly one character lost, and the registry form is a suffix of the
      stored one — the observed damage, and nothing wider;
    * at least ``_MIN_TRUNCATED_SURNAME`` characters remain and the forename
      initials agree. A registry that gives no forename supplies no
      corroboration, so the difference is reported rather than excused;
    * the registry surname is **anomalously uncapitalised** inside a byline that
      capitalises the rest — :func:`_registry_surname_is_anomalously_lowercase`.

    That last condition is not decoration, it is the whole rule. Mis-decoding
    UTF-8 as Latin-1 never *deletes* a character, so "this deposit was
    mis-decoded" is evidence of a careless pipeline and not, by itself, evidence
    of a lost byte; without a second signal the rule cleared every real surname
    pair that happens to be one leading character apart, and there are many at
    every length — see :data:`_MIN_TRUNCATED_SURNAME`. Crossref writes
    ``"family":"ierssen"`` in lower case where it writes ``"Ederra"`` and
    ``"Espinosa"`` in the same array; a registry naming *Rice* rather than
    *Price* writes ``"Rice"``.
    """
    stored_key, registry_key = family_key(stored), family_key(registry)
    if len(registry_key) < _MIN_TRUNCATED_SURNAME:
        return False
    if len(stored_key) - len(registry_key) != 1 or not stored_key.endswith(registry_key):
        return False
    initials = _given_initials(stored)
    if not initials or initials != _given_initials(registry):
        return False
    return _registry_surname_is_anomalously_lowercase(stored, registry, byline, position)


class AuthorDiff:
    """Outcome of comparing two author lists.

    Attributes
    ----------
    mismatches:
        ``(position, stored, registry, )`` triples, 1-based, for creators that do
        not denote the same person.
    stored_count, registry_count:
        List lengths. Compared only when neither list is et-al truncated.
    truncated:
        A length difference is already accounted for and must not be reported
        again as a count: either side ended in an et-al marker, one side is a
        single collective author standing for the whole byline, or the registry
        omitted the first author (:func:`_registry_omits_first_author`). In
        every case the corresponding entry in ``reasons`` says which.
    reasons:
        Accepted-difference explanations keyed by position, for the report.
    """

    __slots__ = ("mismatches", "reasons", "registry_count", "stored_count", "truncated")

    def __init__(self) -> None:
        self.mismatches: list[tuple[int, str, str]] = []
        self.stored_count: int = 0
        self.registry_count: int = 0
        self.truncated: bool = False
        self.reasons: dict[int, str] = {}

    @property
    def count_differs(self) -> bool:
        """True if the lists differ in length in a way that is not explained."""
        if self.truncated:
            return False
        return self.stored_count != self.registry_count

    @property
    def clean(self) -> bool:
        return not self.mismatches and not self.count_differs


def compare_author_lists(stored: list[Name], registry: list[Name]) -> AuthorDiff:
    """Compare two author lists position by position.

    Positional comparison is deliberate: an invented co-author and a reordered
    list are different defects, and a set-based comparison cannot tell them
    apart. Where a positional pair disagrees but both names appear elsewhere in
    the other list, the difference is recorded as a reordering rather than a
    substitution.

    Four escapes exist, in the order they are tried, and every one of them makes
    the tool report *less*, so each is written to the narrowest shape that
    covers a witnessed registry defect and each is paired with a test proving
    the same shape without the corroborating evidence still fires:

    1. a single collective creator on either side, standing for a whole byline;
    2. consortia the registry credits *between* people and BibTeX cannot
       represent, where every remaining person aligns exactly
       (:func:`_interleaved_collectives`, 10.1158/1055-9965.epi-23-0009);
    3. a registry byline missing exactly its first author
       (:func:`_registry_omits_first_author`, 10.1097/00008469-199710000-00007);
    4. within a byline proven to be mis-decoded, a surname that lost its first
       character *and* is the one uncapitalised surname in the deposit
       (:func:`_surname_truncated_by_mojibake`, 10.5271/sjweh.3626);
    5. a pair that disagrees where both names appear in the other list, which is
       a reordering.

    Nothing here is dropped: every escape records a reason, and
    ``compare._check_authors`` prints it with both values under
    REGISTRY-ARTIFACT. Suppressed means "stated and not failed", never "hidden".
    """
    diff = AuthorDiff()
    diff.stored_count = len(stored)
    diff.registry_count = len(registry)
    diff.truncated = any(n.et_al for n in stored) or any(n.et_al for n in registry)

    if not stored or not registry:
        return diff

    # A collective author on one side and a person list on the other is a
    # representation difference, not a defect: Crossref splits some consortium
    # bylines into members while the bibliography keeps the group name.
    if len(stored) == 1 and stored[0].collective:
        diff.reasons[1] = "collective author"
        diff.truncated = True
        return diff
    if len(registry) == 1 and registry[0].collective:
        diff.reasons[1] = "registry lists a collective author"
        diff.truncated = True
        return diff

    interleaved = _interleaved_collectives(stored, registry)
    if interleaved:
        # The consortia are named in full rather than counted. A suppression a
        # reader cannot look up is a check nobody can audit, and here the whole
        # claim is "these particular creators are organisations the bibliography
        # left out" — so the report has to print which ones.
        diff.reasons[1] = (
            "registry interleaves collective creator(s) the byline omits: "
            + "; ".join(str(n) for n in interleaved)
        )
        diff.truncated = True
        return diff

    if _registry_omits_first_author(stored, registry):
        # Returning here rather than re-running the loop at an offset is not a
        # shortcut: the predicate only holds when every remaining creator has
        # already been shown to agree, so there is nothing left to compare. It
        # also keeps ``reasons`` indexable against *both* lists by the same
        # position, which is the contract ``compare._check_authors`` relies on
        # when it prints the stored and registry values side by side.
        diff.reasons[1] = "registry omits the first author"
        diff.truncated = True
        return diff

    # Empty keys are excluded from both sets. ``family_key`` returns "" for a
    # creator with no surname *and* for every surname written outside the
    # comparison alphabet, so keeping them would put one shared "" in both sets
    # and let the reordering escape below fire on any two such creators — a
    # substitution reported as a harmless reordering.
    stored_keys = {key for key in (family_key(n) for n in stored) if key}
    registry_keys = {key for key in (family_key(n) for n in registry) if key}
    # Evidence gathered once for the whole byline, because the truncation rule
    # below is only safe in the presence of proven damage to *this* deposit.
    mojibake_byline = _list_carries_registry_mojibake(stored, registry)

    for index in range(min(len(stored), len(registry))):
        left, right = stored[index], registry[index]
        agreed, reason = names_agree(left, right)
        if agreed:
            if reason:
                diff.reasons[index + 1] = reason
            continue
        if mojibake_byline and _surname_truncated_by_mojibake(left, right, registry, index):
            diff.reasons[index + 1] = "registry mojibake truncated the surname"
            continue
        left_key, right_key = family_key(left), family_key(right)
        if left_key in registry_keys and right_key in stored_keys:
            diff.reasons[index + 1] = "reordered"
            continue
        diff.mismatches.append((index + 1, str(left), str(right)))

    return diff
