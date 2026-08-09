# Known registry defects

Cases where the registry record is wrong and the stored reference is right.
Most are implemented as a check in `src/bibaudit/benign.py`, matches are reported
as `REGISTRY-ARTIFACT` rather than as defects, and none of them ever causes a
value to be adopted.

A few sections below describe defects handled **outside** `benign.py` — in
`names.py`, in `normalize.py`, in `registries/crossref.py`, in
`registries/pubmed.py` or in `compare.py` — because they are about the shape of
a list, the direction of a relation or the spelling of a number rather than
about two strings disagreeing. Each says where it lives. *Online-first versus
print year* and *Container titles* are not suppressions at all: nothing there
is defective, and the entry stays `OK` with an `info` note saying which of the
registry's own values matched.

The list is deliberately short. A "tolerance" that is really just "this check is
noisy" belongs in a threshold or in a project's own `.bibaudit.toml`, not here.

Most of what follows is a reproducible defect in the registry's own data, but
not all of it. Some sections describe a filing convention, an encoding choice or
a representation both sides are entitled to, where nobody's record is wrong at
all. They are here because they are reported as `REGISTRY-ARTIFACT` and so owe
the reader the same explanation — read the section before concluding that a
registry got something wrong.

**Where a section carries an `Observed` line, that line is the record of what
was actually seen.** Five of them say *nothing was*, and each says so in its
first sentence, so a reader can tell a witnessed defect from a guarded
possibility without reading to the end. Anyone who catches an instance should
record it there.

*One DOI, more than one PubMed citation* and *An identifier a registry answered
around* guard against something the registries' responses make representable
but that no probe has caught one doing. They are kept because the two outcomes
are not symmetrical: adopting another work's record hands an entry that work's
metadata, which no later check recovers from, while declining one costs a
lookup. Post-publication status is the one thing carried across a work's
citations regardless, and the first of those two sections says why.

*Doubled tokens from mangled MathML*, *Bracketed parent title on comments and
replies* and *Deposit timestamps recorded as publication years* are the other
three, and they are not kept on that argument. Each is a suppression with no
instance behind it — a hole in a check with nothing to justify it — and
`benign.py` marks all three **NO WITNESSED INSTANCE** and names them candidates
for deletion. What each searched, and what the search found instead, is in its
own section. One author escape, *One-character spelling variants*, is in
the same position and says so in its own words.

`tests/test_benign.py::TestRuleScoping` fails the build when a rule in
`benign.CHECKS` **or** an escape in `names.py` is marked **NO WITNESSED
INSTANCE** in the source and its section here is not, so the two cannot drift.
The author escape names the reason it belongs to on the same line as the
marker, which is how the scan finds the section a reader would look it up in.

Not every section documents a defect, and the ones that do not say so in their
own opening lines: *Registries disagreeing with each other* and *Open Library*
describe how a difference nobody's record caused is kept from reading as one.
*The PMID check is one-sided, and warns rather than fails* documents no
difference at all — it records why a comparison carries the severity it does,
and it is here because [why field-level](why.md) sends a reader looking for
exactly that.

---

## Mojibake surnames

**What happens.** A deposit encoded in UTF-8 is decoded as Latin-1 somewhere in
the publisher's pipeline, and the registry stores the result. *Gómez* becomes
`GÃ³mez`, *Aragonés* becomes `AragonÃ©s`, *Dierssen* loses its initial letter.

**Observed.** Crossref record for **`10.5271/sjweh.3626`** (Papantoniou et al.,
*Shift work and colorectal cancer risk in the MCC-Spain case–control study*,
*Scand J Work Environ Health* 2017, stored as `papantoniou2017colorectal`)
returns mojibake for several Spanish surnames. The recorded response is
`tests/data/names_crossref_mojibake_author_list.json`.

This section previously cited `10.1093/aje/kwx137`, which is a real DOI but is
Kim et al., *Alcohol Consumption and Breast Cancer Risk in Younger Women*, with
a clean American byline. Anyone auditing the rule through the documented DOI
would have found no mojibake and concluded it was invented.

**Reported as.** `registry mojibake`.

**Detection.** Round-trip repair: `text.encode("latin-1").decode("utf-8")`. If
the result decodes cleanly and matches the stored name, the registry value was
mis-decoded. Implemented in `names.demojibake`, which is deterministic and does
not guess — an ordinary name containing `Ã` is left alone.

`clean()` runs an NFKC pass, and NFKC maps `Ã³` to `ó` **before** `demojibake`
ever sees the bytes it needs — which silently defeated the repair on every
surname containing ó, ², ³, ª or º, `GÃ³mez` among them.
`names._NFKC_CONTINUATION_INVERSE` undoes that one substitution first; that is
the only reason the rule works on the witnessed record.

**Why it matters.** Reducing a surname to its last token turns `aragona s` into
`s`, and a checker then reports a one-letter surname mismatch on a correct
bibliography. `names.family_key` compares the whole family name for this reason.

---

## Mojibake that also truncated the surname

**What happens.** The same mis-decode, but the first character of the surname is
lost outright: Crossref holds `ierssen` for *Dierssen*. A round trip cannot
repair this — mis-decoding never deletes a byte, so there is nothing to undo.

**Observed.** `10.5271/sjweh.3626` again, position 15 of 24 creators.

**Reported as.** `registry mojibake truncated the surname`.

**Detection.** `names._surname_truncated_by_mojibake`, and it is deliberately
narrow because "a surname missing one leading character" is far too wide on its
own — it would silence `Ash` against `Nash`, `Rice` against `Price`, `Ellis`
against `Kellis`. Three conditions must hold together: the byline must already
carry *proven* mojibake at some other position (`_list_carries_registry_mojibake`
— a round trip that lands on a surname the bibliography holds); the forename
initials must agree; and the registry surname must be **anomalously
uncapitalised** for that deposit, which is what actually separates byte damage
from a different family. Crossref writes `"family": "ierssen"` in lower case
among twenty-three capitalised surnames; a registry naming a different person
writes `Rice`.

---

## Consortia credited between people in the author array

**What happens.** Crossref credits a consortium as an `<organization>` creator
positioned *inside* the personal byline — `{"name": "for the Pancreatic Cancer
Cohort Consortium (PanScan)"}` at position 5 of 9. BibTeX has no slot for a
corporate creator sitting between two people, and Crossref's own
content-negotiated BibTeX emits none of them, so any bibliography exported that
way holds only the people. Compared position against position, the consortium
reads as a person substitution and every position after it is shifted by one.

**Observed.** `10.1158/1055-9965.epi-23-0009` (Kim et al., stored as
`kim2023abo`), recorded in `tests/data/audit_crossref_interleaved_collective.json`.
Also `10.1093/jnci/djad043` (7 consortia), `10.1038/s41467-020-16483-3` (16) and
`10.1038/s41586-025-09272-9`. On a 438-entry corpus this single shape accounted
for 27 of 30 `FIELD-MISMATCH` verdicts and all 15 remaining `authors`-only
`INCOMPLETE` ones — 42 entries, 9.6% of the file, **not one of which disagreed
with Crossref about a single person**.

**Reported as.** `registry interleaves collective creator(s) the byline omits`,
followed by the organisations in full. The whole-byline case is a different rule
with different evidence — see *A consortium standing for the whole byline* below.

**Detection.** `names._interleaved_collectives`. The collectives are dropped
from the registry side and what remains must align **exactly**: same length as
the stored list, every position agreeing informatively. That is the evidence the
suppression rests on, not a tolerance it grants — one substituted person, or one
person missing from the stored list, and the shape does not hold and the
ordinary comparison runs. Which creators count is Crossref's own answer (the
`<organization>` slot), not a vocabulary of marker words: the corpus credits
`DiscovEHR`, `GSK`, `AstraZeneca` and `UK Biobank`, none of which contains one.

**Accepted residual.** A *person* misfiled into the organisation slot, whom the
bibliography also omits, is not reported. Any Crossref-derived export drops that
creator too, so the omission is the publisher's defect and not one the user can
act on.

---

## Consortia the byline credits and MEDLINE files apart

**What happens.** The mirror of the section above, and the same defect. Crossref
credits a consortium as an `<organization>` inside the `author` array; MEDLINE
files it under `CN` and lists only people under `FAU`/`AU`. So a bibliography
exported from Crossref — which keeps the organisation, unlike Crossref's own
BibTeX — carries a creator the MEDLINE byline does not, and every position after
it is shifted by one.

**Observed.** PMID 42552006, `10.1136/bmjopen-2025-107667`, recorded verbatim in
`tests/data/pubmed_collective_creator.txt`. Crossref's byline is nine creators
with `for the ITC Project Collaborators` at position 6; MEDLINE's `FAU` list is
the same eight people, with `CN - ITC Project Collaborators` beside them. The
entry reported `#6 for the ITC Project Collaborators` against `#6 Kress, Alissa
C` and a substitution at every position after. Four of 386 entries in a 396-entry
live sample took this shape, one of them shifting 25 consecutive positions.

**Reported as.** `byline carries collective creator(s) the registry files apart`,
followed by the organisations in full. Which side carried them is in the
sentence, because a reader given the other one goes looking in the wrong record.

**Detection.** `names._byline_collectives`, on the evidence and the bound its
mirror uses: the collectives are dropped from the **stored** side and what
remains must be the same length as the registry's list with every position
agreeing informatively.

**Why `CN` is not simply read onto the record instead.** MEDLINE does write it
in byline position — but not in *Crossref's* byline position. PMID 42521817
(`10.1038/s41591-026-04492-6`) groups five consortia together after the 42nd
author where Crossref interleaves two of them among people at positions 44 and
46, so reading `CN` would replace one misalignment with another. This escape
does not depend on the position at all.

---

## A consortium standing for the whole byline

**What happens.** One side credits the consortium and the other credits its
members: the bibliography stores `{The Endogenous Hormones and Breast Cancer
Collaborative Group}` as a single creator while the registry lists the people, or
the reverse. Neither is wrong. It is the same work described at two levels, and
it is a representation difference rather than a defect in anyone's record.

**Observed.** `key2002hormones` in the 438-entry corpus — *The Endogenous
Hormones and Breast Cancer Collaborative Group*, stored as one creator against a
registry byline of its members. It is one of the eight author differences that
run flagged, all of them false positives, and it is pinned at
`tests/test_benign.py::TestAuthorArtifacts`. The corpus is private; neither the
entry's DOI, nor a registry response for it, nor the baseline that recorded the
eight is in this repository, so unlike the interleaved case above there is
nothing here to read the suppression against — the citekey and the test are the
whole of the evidence trail.

**Reported as.** `collective author` when the *bibliography* holds the single
collective, `registry lists a collective author` when the registry does.

**Detection.** `names.compare_author_lists`, before the positional walk begins.
The test is only on the side that holds one creator and finds it collective; the
other side is not inspected, because there is nothing position-against-position
to compare once the two lists describe the byline at different granularities.

**What it gives up, stated plainly.** Unlike the interleaved case above, no
alignment is available as evidence, so this escape suppresses the author-count
difference on the strength of "one side is a single collective creator" alone. It
therefore cannot tell a consortium byline from a bibliography that replaced a
real author list with a group name. Both values are printed under
`REGISTRY-ARTIFACT` at the first position, so the report shows the collective
against the registry's *first* creator rather than the two bylines in full. That
is enough to see which shape it is, and the tool does not decide between them.

---

## Registry bylines missing their first author

**What happens.** The deposit opens one creator short. The paper's byline begins
with Clavel-Chapelon; Crossref's record holds nine creators beginning with
`van Liere`, marked `"sequence": "first"`.

**Observed.** `10.1097/00008469-199710000-00007` (Clavel-Chapelon et al., *E3N,
a French cohort study on cancer risk factors*, Eur J Cancer Prev 1997, stored as
`clavelchapelon1997e3n`), deposited by Ovid/Wolters Kluwer. Recorded in
`tests/data/names_crossref_first_author_omitted.json`.

**Reported as.** `registry omits the first author`.

**Detection.** `names._registry_omits_first_author`. Alignment, not a special
case for one citekey: exactly one creator missing and it must be the first; at
least three remaining creators, all agreeing in order and *informatively*; the
dropped surname absent from the registry list; no et-al marker on either side.

**What this cannot do, stated plainly.** A bibliography that *prepends* an
author who was never on the paper produces the identical shape, and no author
list can separate the two. The difference is still printed with both names under
`REGISTRY-ARTIFACT` — suppressed here means stated-and-not-failed, never
hidden — it simply does not break the build. The evidence that would separate
them (the citekey, the title) lives outside `names.py`.

---

## Particle filing

**What happens.** A nobiliary particle is part of the filing surname in one
source and not in the other: the bibliography stores *van Eijck* and the deposit
files the creator under *Eijck*, or the reverse. Neither is wrong — the two
conventions are both in use, and the choice is the cataloguer's.

**Observed.** `van Eijck` is a real surname in the 438-entry corpus and is one
of the two the module is written around (the other being `Clavel-Chapelon`), but
no deposit was recorded in which the two sides file it differently, so there is
no DOI to cite here. The rule is pinned in `tests/test_names.py`.

**Reported as.** `particle filing`.

**Detection.** `names.names_agree`, via `family_key(..., drop_particles=True)` on
both sides. The particle is dropped from *both* names and what remains must
match exactly; nothing is inferred about which convention is correct, and the
stored value is never adopted. `names._PARTICLES` is the vocabulary.

---

## Compound surnames shortened to their final element

**What happens.** A registry keeps only the last element of a compound surname —
`Chapelon` for *Clavel-Chapelon* — or sweeps a forename into the family field, so
the deposit holds `Cristina-Marianini-Rios` where the bibliography holds
`Marianini-Rios` with `Cristina` as the given name. Hyphen and space are already
interchangeable here, because `fold()` turns both into a space, so what remains
is a genuine difference in how many elements the surname has.

This one is a defect in the registry's data. A compound surname is the name the
person has, and a deposit holding part of it is holding the name wrong — which
is why the rule requires the shorter form to *end* the longer one rather than
treating the two as equally valid filings.

**Reported as.** `compound surname shortened`.

**Detection.** `names.names_agree`. The stored surname must be a **token-level
suffix** of the registry's, or the registry's of the stored one — whole elements,
never a character prefix, so `Martin` is not accepted against `Martinez`.

Suffix, not "shares a final token", and the difference is the whole rule.
Requiring the *shorter* name to end the longer one separates people who merely
share a surname: `Krebs-Smith` is not `Davey Smith`, and `González-González` is
not `Martínez-González`. Both pairs are real, both appear in the 438-entry corpus
under different DOIs, and a final-token test clears both. `Clavel-Chapelon`
against `Clavel` is rejected for the same reason — a leading element is not a
shortening this rule recognises. Pinned in `tests/test_names.py`.

---

## One-character spelling variants

**What happens.** Transliteration produces two spellings of one surname:
*Ivanov* and *Ivanova*, *Papantoniou* and *Papantoniu*.

Nobody's record is wrong. Romanisation from Cyrillic, Greek or Han has no single
standard, so two sources transliterating the same name honestly reach two
spellings, and neither is the correction of the other.

**Reported as.** `spelling variant with matching initials`.

**Detection.** `names.names_agree`, behind three conditions that must hold
together, each of which names what it excludes:

- **one edit, not a shared prefix** — so `Martinez` (two edits from `Martin`) and
  `Smithers` (three from `Smith`) are reported, not suppressed;
- **the same first character** — so a leading-character difference is never waved
  through, leaving `Herman`/`Sherman` and `Rossman`/`Grossman` reported. That
  damage is `_surname_truncated_by_mojibake`'s business, and only under evidence;
- **at least six characters on both sides** — so the short surnames where one
  edit is a different family entirely are reported: `Chan`/`Chang`, `Wan`/`Wang`,
  `Lin`/`Liu`, `Kim`/`Kum`.

The forename initials must also agree, which is what defeats `Kowalski, Anna`
against `Kowalska, Piotr`.

**NO WITNESSED INSTANCE.** No entry in the 438-entry corpus this tool was
developed against reaches this branch. It is kept, narrowly, for the
transliteration variants a multilingual bibliography does produce, and it is a
candidate for deletion rather than for widening. An earlier form of it accepted
any two surnames sharing three leading characters on a matching initial, which
cleared `Chan` against `Chang`, `Wan` against `Wang`, `Martin` against
`Martinez`, `Smith` against `Smithers`, `Gonzalez` against `Gonzalo` and `Sancho`
against `Sanchez` — pairs of different families, each cleared against the other
on an initial that thousands of researchers share.

---

## Author lists in a different order

**What happens.** The same people appear in both lists in a different order, so a
position-against-position comparison reports every moved creator as a
substitution. Neither record is wrong: an author order is a fact about the paper
that both sides may serialise differently.

**Reported as.** `reordered`.

**Detection.** `names.compare_author_lists`, on two conditions together. The two
bylines must hold **the same creators counted** — every surname key, with its
multiplicity, on both sides — and at the disagreeing position the stored surname
key must appear somewhere in the registry list *and* the registry surname key
somewhere in the stored list.

**Counted, because presence alone is not a reordering.** Set membership was the
whole test, and it excused a byline that had *lost* an author: a one-position
shift puts every stored creator somewhere in the registry list and every
registry creator somewhere in the stored one, so each shifted position was
excused and what survived was a count *warning*, which does not fail. Position 1
is what should have stopped it — the dropped surname has nowhere to be found in
the shortened list — and does not when that surname repeats later in the byline,
which is routine in Chinese, Korean and Japanese author lists. PMID 38213033's
thirteen creators are Lee, Jung, Kim, Lee, Lee, Baek, Kwon, Shin, Kim, Shin,
Park, Park, Kim, and with the first removed every one of the twelve remaining
positions read as a reordering. **104 of 2,551 live entries** took that shape.

Counting also makes a length difference impossible, which is what *not a name
that merely went missing* means. Measured over 3,040 MEDLINE/Crossref pairs of
the same work, 9 entries carry a `reordered` position; 6 hold the same creators
counted and keep it, and the 3 that lose it are the three whose bylines differ in
length — 13 against 6, 3 against 2, 14 against 13. Dropping the first creator
from each of 824 live entries now fails **every one**, against 104 of 2,551
absorbed before; the same 824 unmutated fail none.

**What it now reports that it used to excuse, stated plainly.** A reordering in a
byline that *also* carries a difference of spelling — a mojibake surname, a
particle one side files and the other does not — no longer holds the same keys
counted, so its moved positions are reported. That compound shape has no
witnessed instance, and the direction of the mistake is the one that shows: the
tool complains where it might have stayed quiet, rather than the reverse.

**Why it is narrow.** Read together with *Consortia credited between people in
the author array*: when a consortium sits inside the byline, every position after
it is shifted by one, and each shifted creator does appear on both sides. Before
`_interleaved_collectives` recognised that shape, this escape absorbed the whole
tail and reported 1446 differences as a reordering that never happened. A rule
that explains a shift it has no evidence for hides the substitution underneath.

---

## Creators the entry names past the registry's last

**What this is, and why it is not a suppression.** The two sections here are the
*exceptions* to a check that fails the build, which is the reverse of everything
else in this file. `names.compare_author_lists` walks two bylines in step and
stops at the shorter one, so a creator appended to the end of an entry's byline
was compared against nothing, and the only trace was an author-*count* warning,
which does not fail. Appending a fabricated name to an otherwise correct byline
produced no failing verdict on **4,255 of 4,255** live entries.

That is the documented failure mode of a generated bibliography, and the reason
`compare._check_authors` compares the whole list rather than the first author,
so the tie breaks towards the finding here: a creator past the registry's last
position is reported as `authors/uncorroborated`, at error severity, one line
per creator, naming the creator. A count line names nobody, and the reader's
question is *which* name no record carries.

**It is a tie.** A registry whose byline is short at the tail produces the
identical shape, exactly as *Registry bylines missing their first author*
describes for the other end, and no author list can separate the two. Measured
over 3,040 works whose MEDLINE and Crossref records were both fetched — five
windows spanning 1992-2026 — the two registries disagree about byline length on
40 (1.3%). After the exceptions below, an entry written from the publisher's
deposit and checked against MEDLINE alone reports a creator on **7 of them
(0.23%)**: that is the PMID path, where there is no second witness. On the DOI
path, where there is one, it reports **none**. Against a shape that was never
reported at all.

A live sweep of 1,177 entries written from the MEDLINE record — correct by
construction — fails none of them and reports no uncorroborated creator at all.
Append one fabricated name to each of the same 1,177 and **every one fails**,
against 0 of 4,255 before.

Three kinds of tail position are *not* that claim, and each is kept out of it
rather than reported.

### A creator the corroborating registry does name

**What happens.** Only one byline is ever compared — the primary registry's, or
the corroborator's where the primary deposited none — and *uncorroborated* is a
claim about every record that answered. A Crossref deposit that stops short of
the paper's real byline had the entry's remaining creators reported against it
while PubMed, already fetched and already parsed, named every one of them.

**Observed.** 2 of the 3,040 works above, and they are the whole of the DOI
path's exposure to this check.

**Reported as.** `authors/count`, the length difference, at warning severity —
the report that was already right for this shape. The primary's byline *is*
short, and an entry naming creators a second registry corroborates is not a
fabrication. Nothing is suppressed here: the finding is withdrawn, not hidden.

**Detection.** `compare._drop_creators_the_corroborator_names`. Surnames, on the
same terms as the rest of the author comparison; empty keys excluded, because a
corroborator holding one creator whose surname `fold` discards would otherwise
vouch for every stored creator whose surname it also discards. The PMID path
consults no second registry and keeps its findings.

**The residual, stated.** A fabricated name sharing a surname with a real
creator on the corroborator's byline is dropped.

### A collective creator past the registry's last

**What happens.** An organisation is not an invented co-author. MEDLINE files
consortia under `CN` and lists only people under `FAU`/`AU`, so a byline
exported from Crossref carries a creator MEDLINE's list does not — the defect
*Consortia the byline credits and MEDLINE files apart* covers where the
remaining people align exactly. Where they do not, that escape declines the
byline and the consortium arrives here.

**Observed.** PMID 38236418 (`10.1007/s00392-023-02363-5`): the entry built from
Crossref's deposit carries 21 creators including `on behalf of the STAAB
consortium`, MEDLINE's byline is 11 people with a `CN` line beside them, and the
consortium sits past MEDLINE's last position. 1 of the 11 tails in the live
sample above.

**Reported as.** `collective creator past the registry's last`.

**Detection.** `names._tail_past_the_registry`, on `Name.collective` — which is
the registry's own answer where the creator came from a registry (Crossref's
`<organization>` slot) and the adapter's collective-author parse where it came
from the bibliography.

**The residual, stated.** A fabricated *organisation* appended to a byline is
excused. The failure mode this check exists for invents people.

### A surname the registry's byline carries somewhere else

**What happens.** "The registry does not name this person" would be false
against the very record being quoted. Two shapes reach it: a bibliography that
repeats a creator, and a misalignment this tool declined to explain — run a
byline with a plausible senior author prepended through
`names._registry_omits_first_author` below its alignment floor and the entry's
*last* creator lands past the registry's end, present in the registry list all
along.

**Observed.** PMID 42552006, the ITC Project entry in
`tests/data/pubmed_collective_creator.txt`, with one of its people substituted:
the shape no longer holds for *Consortia the byline credits and MEDLINE files
apart*, the positional comparison runs, and `Ahluwalia, Indu B` ends up one
position past MEDLINE's last — carried by MEDLINE all the same.

**Reported as.** `credited elsewhere in the registry byline`.

**Detection.** `names._tail_past_the_registry`. The creator's `family_key` must
be non-empty and appear among the registry byline's keys.

**The residual, stated.** A fabricated name that happens to share a surname with
a real co-author is excused rather than reported. That is a real hole, and the
alternative is printing a claim the registry's own record contradicts.

---

## Author comparisons with nothing to compare

**What happens.** Four situations let a position pass without either side being
evidence that the two creators are the same person: one side has no surname at
all; a surname is written outside the comparison alphabet, so `family_key`
returns nothing usable; the registry truncated a surname to a single character;
or the list carries an et-al marker rather than a creator.

**Reported as.** `one side has no surname`, `registry surname truncated`,
`et-al marker`, and `surname outside the comparison alphabet`.

**Observed.** No single deposit is cited: these arise from the shape of a byline
rather than from one registry's defect. The case that motivated separating them
out is recorded in `names.py` — a registry list of `[王, 李, 张]` "aligning"
against `[Smith, Jones, Brown]` — and is pinned in `tests/test_names.py`.

**Detection.** `names.names_agree` returns each as an honest "this comparison had
nothing to work with", and every one is printed under `REGISTRY-ARTIFACT` so the
reader can see it was reached.

**Why it matters.** None of them may be counted as a creator that *aligned*.
`names._agrees_informatively` excludes all four from the alignment arithmetic
`_registry_omits_first_author` and `_interleaved_collectives` rest on. Three
agreements of this kind are three pieces of nothing, and counting them let a
registry list of `[王, 李, 张]` "align" against `[Smith, Jones, Brown]` and
suppress the author-count difference on top.

---

## A full-author tag written without its comma

**What happens.** MEDLINE's `FAU` is `Surname, Initials` and the comma is what
says which half is which. A few citations carry the tag without it, and a
comma-less creator string is BibTeX's *Given Family* — so `FAU - Okano J` was
read as a person surnamed `J`.

That is not a cosmetic misreading. A registry surname of one character is
exactly what *Author comparisons with nothing to compare* accepts **any** stored
surname against, under `registry surname truncated`. The parser was
manufacturing the evidence for an escape that then cleared whatever name the
bibliography had at that position, so a fabricated co-author passed there and
nowhere else.

**Observed.** 10 of 16,511 `FAU` values, in 9 of 3,500 citations sampled from
five windows spanning 1992–2026: `Okano J` (PMID 11278851, recorded verbatim in
`tests/data/pubmed_fau_without_comma.txt` beside `FAU - Rustgi, A K` in the same
byline), `Chung H`, `Watanabe Yi`, `Liu Cj`, `van der Schaaf A`,
`Meijer Drees R`, `van Veenendaal MA`, `Van Siclen CD`, `K Sikorska`, and the
single-token `Desriani`. Every one of them is written character for character as
that record's own `AU` line: NLM never backfilled the comma.

**Reported as.** Nothing. This is a parsing rule, not a suppression — the
creator is read the way the record means it and then compared normally.

**Detection.** `registries/pubmed._parse_fau`. A value with a comma goes to
`names.parse_name` as before; one without goes to `_parse_au_fallback`, the
parser for the abbreviated `AU` tag, which is not an approximation here but the
same string's own reading. `FED`, the editor tag with `FAU`'s convention, is
read by the same function.

**The one citation that writes it the other way round.** PMID 31128948 carries
`K Sikorska` — an initial in front of the surname — in both `FAU` and `AU`,
beside fifteen colleagues written `Koole, S N`-fashion. `"Sikorska K"[au]`
answers 215 citations and `"K Sikorska"[au]` exactly that one, so NLM's own
order is what the record breaks. `pubmed._initials_ahead_of_the_surname`
recognises it on two conditions, and 27 of the 33,026 `FAU`/`AU` values in the
sample open with a single letter, so both are load-bearing:

- **exactly two tokens, the first one character long.** Eight surnames in the
  sample genuinely begin with a lone letter — `A Richmond, Jacqueline`,
  `T Rahma, Azhar`, `E Albuquerque, Rodrigo Pires`, `W Y Chan, Stella` — and the
  abbreviated form of each carries its initials as a further token, so none is
  two tokens long. One character rather than a short one, because two-letter
  surnames are among the commonest in this literature and take a transliterated
  initials block: `"Ho Yi"[au]` answers 14 citations and is Ho, Y. I.;
- **the second token is not written in capitals.** An initials block is, and a
  surname of one or two letters is real: `S DMTS`, `A LK`, `N AK` and `T T` are
  surnames `S`, `A`, `N` and `T` with their initials after them, which is NLM's
  order already. A length test cannot separate those from `Sikorska`; the
  capitals can.

---

## Zero-padded article numbers

**What happens.** Journals that number articles rather than paginating them
deposit the number with leading zeros. *Environmental Health Perspectives*
records `027004`; the citing entry carries `27004`.

**Detection.** `normalize.first_page` strips leading zeros after an optional
letter prefix, so both forms compare equal.

---

## Page locators nothing can read as a number

**What happens.** `normalize.first_page` reads an optional letter, any zero
padding and the digits. Two live shapes are neither: SAGE paginates its
online-only articles `NP580-NP599`, and front matter runs `i-xv`. The pattern
did not match, the function returned `""`, and `""` compares equal to `""` — so
every unreadable locator agreed with every other one. A bibliography storing
`NP585-NP599` against a record holding `NP580-NP599` came back `OK`.

That is the same collapse *404 is a fact, a timeout is ignorance* names
elsewhere, in the pages field: an inability to read a value was being rendered
as the values agreeing.

**Observed.** 24 of 3,250 MEDLINE `PG` values (0.74%) on a live sample of 3,500
citations drawn from five windows spanning 1992-2026 — 22 `NP…`, `i-xv` on PMID
38284210, and `suppl 4 p.` on PMID 10118706.

**Reported as.** Nothing where the openings agree; `pages/mismatch` where they
do not. This is a normalisation rule, not a suppression.

**Detection.** `normalize.first_page`. A locator the pattern cannot read falls
back to the folded text ahead of the range separator — still the opening
locator, which is what the function is for, and not the empty string.

**Only the opening, on this path as on the other one.** MEDLINE writes
`PG - NP2661-76` where Crossref deposits `NP2661-NP2676` for
10.1177/1010539511421194, and both open at `np2661`. Of the 24 values above,
the 21 whose publisher deposited a page at all agree with MEDLINE on the
opening locator, character for character — so the rule that makes the miss fire
reports nothing new on the shape it was measured against.

**What is left, and where it is guarded.** A value carrying no alphanumeric at
all still yields nothing, because there is nothing in it to read.
`compare._check_pages` therefore accepts an empty opening only against the
identical text: two such values are not evidence of a match, and reporting one
against itself would be a finding about nothing.

---

## A volume or issue number with a leading zero

**What happens.** One registry writes the number padded and the other does not.
Crossref deposits `"issue": "05"` for `10.1055/a-2760-7307` (*Clinics in Colon
and Rectal Surgery*); MEDLINE writes `IP - 5` on the same work, PMID 42553907.
A bibliography exported from either carries that registry's spelling, and on the
PMID path the other one is the only value there is to compare against — so a
correct entry reported `issue/mismatch` and the run exited 1.

**Observed.** `10.1055/a-2760-7307`, both records fetched live 2026-08-09. The
opposite pairing is the same shape and is accepted the same way.

**Reported as.** `one side writes the number with a leading zero`. It blames
nobody, because neither side is wrong: `05` and `5` are one issue, exactly as
an article number and a page range are one article in the section below.

**Detection.** `benign._number_zero_padded`, on `volume` and `issue` only, and
only where **both** sides are ASCII digits differing by leading zeros alone.

**What it deliberately does not cover.** `Volume 18` against `18` — five
entries in the same live sample, all from one publisher's own rendering of its
volume line. That is a label a reference manager copied into a numeric field:
the entry really is wrong about what the field holds, and the fix is one the
user can make. Suppressing it would hide a defect the tool exists to report.

---

## Article number recorded against a page range

**What happens.** One side records `e0123456` or `693933`, the other a span.
Both identify the same item; the journal simply changed how it deposits.

**Detection.** `benign._pages_article_number` — when exactly one side looks like
an article number, the difference is an encoding choice, not a defect.

`normalize.is_article_number` decides "looks like". An all-numeric value needs
**six** digits, not four: `2461` is the opening page of
`10.1001/archinte.167.22.2461` and nothing about it says article number, so the
four-digit floor let a stored `2461` against a registry `2450-2455` be excused
as "one article in two notations" when it is a plain page disagreement. A value
carrying a non-digit prefix gets a **four-character** floor rather than none at
all, so `e0123456` and `A102` are article numbers and `e12` and `A1` are not.
The prefix is good evidence and it is not proof: an `e12` is as easily a
mistyped page as a numbered article, and the floor keeps the difference
reported. That is the safe direction — a page disagreement nobody was told
about is the loss this predicate's threshold exists to prevent.

**Both floors count the value as the source writes it**, zero padding included,
which is the same padding the section above says both forms of. Measured on the
normalised value instead, `085001` was five digits and not an article number,
while the unpadded `85001` for the same article was — so the predicate answered
differently about one item depending on which registry deposited it, and the
suppression never fired. Three live instances in one 1,923-entry sweep, all
correct entries, all failing the build: PMIDs 42571480, 42571556 and 42571506
(*J Biomed Opt*), `PG` `085001`/`086003`/`086004` against a Crossref `page` of
`1-15`/`1-16`/`1-37`. `027004` — the literal `first_page`'s own docstring cites
— had the same problem. Padding is counted, never waived: `0246` is four
characters and still not an article number. On the prefixed floor the same rule
has **no witnessed instance**, since the journals that pad write a bare number
and `e0123456` clears four characters either way; it is written once rather than
twice because two floors counting differently is a second rule nobody could
state.

---

## Doubled tokens from mangled MathML

**NO WITNESSED INSTANCE. Candidate for deletion.**

**What it claims to happen.** A title containing mathematical notation is
deposited through a broken MathML conversion and an operator token is repeated,
producing fragments like `do(x)do(x)` in the registry's title.

**Observed. Nothing.** Searched: all 438 corpus entries, and 17,144 Crossref
titles from the journals where notation in a title is routine — *Journal of
Causal Inference*, *Biometrika*, *Statistics in Medicine*, *Biometrics*, *PLOS
ONE*. The pattern matched nothing anywhere.

Doubling from mangled markup *is* real, and this rule does not catch it.
`10.1002/(SICI)1097-0258(19980730)17:14<1601::AID-SIM870>3.0.CO;2-2` is
deposited as "…uterine receptivity inin vitro fertilization", the word doubled
across a lost `<i>`, and `benign._MATHML_DOUBLING` does not match that shape
either. So the rule has neither an instance nor the instance that exists.

**Detection.** `benign._title_mathml` removes the doubling and re-compares. Only
an exact match after repair is accepted.

**What to do with it.** Replace it with a rule written against a recorded
response, or delete it. Widening it to "collapse any immediately repeated
token" would suppress far more than one deposit defect and has no evidence
behind it.

---

## Bracketed parent title on comments and replies

**NO WITNESSED INSTANCE of that description. Candidate for narrowing or
deletion.**

**What it claims to happen.** A comment, reply or erratum is registered with the
parent article's title in square brackets ahead of its own. The bibliography
carries only the comment's title, correctly.

**Observed. Not that.** Searched: all 438 corpus entries against both Crossref
and PubMed, and PubMed's `comment[pt]`, `"published erratum"[pt]` and
author-reply title searches. Not one registry title was a *parent article's*
title in brackets followed by the item's own.

What the search did find is a bracketed **label**: PMID 42535368,
`10.3892/mmr.2026.13976`, titled "[Corrigendum] Identification of key
differentially expressed genes associated with non-small cell lung cancer by
bioinformatics analyses".

**Detection.** `benign._title_bracketed_parent` strips a leading bracketed span
of ten characters or more and re-compares. That is wider than the description
above, and the difference matters: the span on PMID 42535368 is eleven
characters, so the rule strips `[Corrigendum]` and then accepts an entry
storing the **parent** article's title against the **corrigendum's** DOI. That
is arguably a real citation error being suppressed, which is the opposite of
what this file is for.

**What it gets right, and is worth keeping.** A *wholly* bracketed translated
title — `10.1016/j.medcli.2012.01.020` is registered as "[SIDIAP database:
electronic clinical records in primary care as a source of information for
epidemiologic research]" for a Spanish-language article — is left alone,
because stripping it leaves nothing to compare. The rest of the rule needs an
instance or it needs to go.

---

## Shortened registry titles

**What happens.** The registry holds a truncated title where the bibliography
holds the full one — Rubin 1986 is registered as bare `Comment`.

**Detection.** `benign._title_shortened` accepts the difference only when the
registry title is a **leading fragment** of the stored one, ending on a word
boundary. "Fully contained anywhere" was too wide: that is not the shape of a
dropped subtitle, it is the shape of a citation pointing at the work it responds
to — a stored `Corrigendum to 'Shift work and colorectal cancer risk in the
MCC-Spain case-control study' [...]` against the *original* paper's DOI
(`10.5271/sjweh.3626`) was cleared by it, and that is a real citation error. The
reverse direction is not accepted either: an entry missing its own subtitle is a
real incompleteness.

---

## A journal's own acronym written ahead of its name

**What happens.** Several publishers set the masthead as the acronym, a colon,
then the title — `JNCI: Journal of the National Cancer Institute` — and a
reference manager copies it whole into `journal`. NLM never files a serial that
way, so the stored name carries a token the registry's does not have at all.

**Observed.** PMID 42550479, `10.1093/jnci/djag268`, `JT - Journal of the
National Cancer Institute` with `TA - J Natl Cancer Inst`, in
`tests/data/pubmed_acronym_prefix.txt`; and PMID 42522049,
`10.1002/jac5.70263`, `JT - Journal of the American College of Clinical
Pharmacy : JACCP` against a stored `JACCP: JOURNAL OF THE AMERICAN COLLEGE OF
CLINICAL PHARMACY`, in `pubmed_acronym_prefix_subtitle.txt`. Four entries in
a 386-entry live sample take this shape: PMIDs 42550479, 42544784 and 42528271,
all *JNCI*, and 42522049.

Two more were first counted with them and are not this shape. PMID 42550905
stores *Proceedings of the National Academy of Sciences* against a `JT` that
continues `of the United States of America`; PMID 42546077 stores *Revista da
Escola de Enfermagem da USP* against `JT - Revista da Escola de Enfermagem da U
S P`. Neither has an acronym before a colon, neither is reached by the rule,
and both still report `container/mismatch`. `benign._container_abbreviation`
reaches neither: it needs the stored tokens to be in-order prefixes of the
registry's, and the acronym is a token the registry's name does not contain.

**Reported as.** `stored name prefixes the journal's own acronym`, and where
the registry's value had to be reduced to reach the match, the clause that says
so: `, and the registry adds its own subtitle` or `, and the registry adds a
parenthetical qualifier`. Two edits in one comparison, and the sentence names
both — PMID 42522049 lost an acronym from the left-hand side and ` : JACCP`
from the right, and said so about the acronym only.

**Detection.** `benign._container_acronym_prefix`. The prefix must be two to
ten characters with no lowercase — a lowercase word before a colon is a title's
own opening clause, and `Circulation: Cardiovascular Quality and Outcomes` is a
different journal from *Circulation* — and its letters must be word-initials of
what follows, **in order**. That is what makes removing it information-free:
`JNCI` is derivable from *Journal of the National Cancer Institute* and cannot
stand for another journal, while `NEJM:` before the same name fails on `E`.

The subsequence test is used rather than a reduction because which words an
acronym skips is a fact about a language, not about a title: `JNCI` skips *of*
and *the*, and a Portuguese or German serial skips different ones. A stop-word
vocabulary would put one language's function words in the verdict path.

What is left has to equal a name the record itself carries — `JT`, a
`container_alternates` entry, the text before NLM's spaced colon, or the text
before a trailing parenthetical — **outright**. Never a prefix and never a
substring: `CE: Cancer Epidemiology`, whose prefix *is* an initialism of what
follows it, leaves *Cancer Epidemiology*, which differs from *Cancer
Epidemiology, Biomarkers & Prevention* by a word and is no name that record
carries, so the difference still fires. `CEBP:` before the same two words never
reaches that test at all — it fails the initialism check on `B`. A leading
article is never taken off the registry side here: that would be a third edit
in one comparison, and no reason printed here claims it.

---

## Aggregator DOIs that redirect

**What happens.** `doi.org` content negotiation redirects a JSTOR DOI
(`10.2307/2669548`) to the publisher's own DOI. Same work, different registrant.

**Detection.** `benign._doi_redirecting_prefix` recognises the prefix, reached
through `compare._check_doi`, and the difference is printed as a `doi/alias`
issue at `info` severity — a note, never a mismatch, and it can never fail a
build. The DOI is the lookup *key*: the record is here precisely because the
stored DOI resolved to it, so the registry disagreeing with itself is not
evidence against the bibliography. `doi` is deliberately absent from
`compare.CHECKED_FIELDS` for that reason.

Until `_check_doi` existed, `benign.classify` was never called with
`field="doi"` by any caller, so this rule was documentation shaped like code and
the promise above was false.

---

## One DOI, more than one PubMed citation

**A precaution, not a recorded defect.** Nothing was observed doing this; see
the `Observed` line below for what was looked for.

**What happens.** `esummary` answers for a queried DOI with two records that
both carry it among their own `articleids`. Either PubMed holds two citations
for the work, or one record lists another's identifier as its own — the case
the DOI filter in `PubMed._pmids_by_doi` already drops candidates for, in the
residual form where both records claim a DOI that *was* asked for.

**Observed.** Nothing. Every DOI checked against `"<doi>"[aid]` while this was
written came back with exactly one PMID, including `10.1093/ije/dyx269`
(Molina-Montes et al., *Int J Epidemiol* 2018 — PMID 29329392), the entry the
comparison tests are built on. This section exists because the ambiguity is
representable in what `esummary` returns and because `compare._check_pmid` is
the first thing that would turn an arbitrary choice into an accusation — not
because a defective payload was recorded. Anyone who finds one should record it
here.

**Reported as.** Nothing at all. This is a refusal rather than a suppression:
no `REGISTRY-ARTIFACT` line is printed, because there is no difference to
explain. The comparison never runs.

**Detection.** In `registries/pubmed.py`, not in `benign.py`.
`PubMed._pmids_by_doi` keeps every PMID a DOI came back under, `by_dois`
**fetches all of them**, and the record it hands on carries no `Record.pmid`
whenever there was more than one. The DOI still resolves — two citations of one
work carry the same title, byline and year, so which of them supplies those is
arbitrary and nothing is lost but the one comparison that had no basis.

Post-publication status is the part that is not arbitrary, and it is read off
every citation rather than off the one that supplied the fields. Fetching one
of the two took retraction status off whichever number sorted last, so a work
PubMed records as retracted under one citation and not the other reported clean
— the worst miss available here, and the one that defeats the stated reason
PubMed is consulted at all. `_merged_citation` carries the `PT` retraction flag
across, and MEDLINE's `ECI` cross-reference with it: a concern is filed on the
citation it was raised against, and reading it off one record left whether it
was reported at all to `efetch`'s ordering. The tie breaks towards the finding,
on the rule `crossref._reciprocal_updates` states: naming a status a second
citation does not carry costs a line a reader can check, and missing one puts a
retracted paper in a manuscript.

**The fields do not cross over, and that is the half this section turns on.**
Handing the retracted citation on whole would resolve the second case above —
one record listing another work's identifier as its own — towards adopting that
work's record, chosen *because* it carries the accusation, and give the entry
its title, byline, container and year. Carrying the status alone is the one
asymmetry kept deliberately: in that residual case a status this DOI's other
citation does not carry becomes a line a reader can check against two named
PMIDs, and dropping it would put a retracted paper in a manuscript. Ignorance
about retraction may never render as a clean bill of health; nothing else in a
citation is carried this way.

**Why it matters.** A work with two PubMed citations makes either number a
correct thing to store. Keeping the arbitrary one would fail a bibliography for
a choice the tool made, on a check whose whole purpose is to catch a citation
whose two identifiers name two different works.

`compare._check_pmid`'s other two refusals are not registry defects either, and
each is stated where it lives — a reference with no DOI is *resolved* by its
PMID and so is never compared against it, and no relation (Crossref's
`updated-by`, MEDLINE's `RIN`/`ROF`) is read anywhere in the check, so an entry
citing a retraction notice by that notice's own DOI and PMID is silently
correct. A project that meets a case none of this covers adjudicates it in its
own `.bibaudit.toml`, where the claim reads as somebody's say-so and can be
re-read.

---

## A record's PMC accession stored as its PMID

**What happens.** NLM issues a PMID and a PubMed Central accession for one
deposited article and MEDLINE carries both on the same record. A `pmid` field
comes to hold the accession with its `PMC` prefix dropped, and the two numbers
then disagree while naming one citation.

**Observed.** PMID 28520842 (*Am J Epidemiol* 2017, 10.1093/aje/kwx137).
`efetch id=28520842` returns a block with `PMID- 28520842` and `PMC  -
PMC5860629` in it; Zotero writes the pair on adjacent lines of one `Extra` box,
which is where a stripped-prefix copy comes from. The record side is what is
recorded here — anyone can fetch it — rather than a bibliography that made the
mistake.

**Detection.** `benign._pmid_pmc_accession`, reached through
`compare._check_pmid`, which hands `classify` the record the registry PMID came
from rather than `ctx.primary`: MEDLINE's `PMC` line exists only on PubMed's
record, and Crossref's would explain nothing.
`normalize.pmc_number` owns the parsing, and it requires the prefix — a PMC
accession and a PMID are both bare ascending integers, so a rule that accepted
a naked number would read every PMID as an accession.

**Reported as.** `REGISTRY-ARTIFACT`, with both numbers printed.

**Why it matters.** The check's whole claim is that the entry's two identifiers
name two citations. A number that names the record in hand is the one case
where that claim is false by construction.

---

## The PMID check is one-sided, and warns rather than fails

**No defect here, registry or otherwise.** This section records why a
comparison carries the severity it does, and it is in this file because [why
field-level](why.md) sends a reader looking for exactly that.

**What happens.** Nothing looks the *stored* PMID up. The record in hand came
back under the stored DOI, so "these two identifiers name two works" is an
inference from one lookup, not a finding from two. The case that decides it: a
bibliography carrying a PMID that has since stopped answering — `efetch` for
20000157 or 35000082 returns an empty body today — beside the right DOI.
Whether such a number was the work's own when the entry was written cannot be
seen from this side, because what came back is the citation the DOI resolves to
and it says nothing about a number nobody put to PubMed.

**Reported as.** A `pmid/mismatch` issue at `warning` severity, so the entry's
verdict is `INCOMPLETE` and the run's exit code is 0. Two costs, both real. The
group heading `INCOMPLETE` prints under reads "the registry holds fields the
entry omits", which this is not; the issue line beside it names both numbers and
says the stored one was not looked up. And the default report prints the failing
groups plus `DISPUTED` and nothing else, so on a default run the finding needs
`--verbose` to be seen. `--fail-on INCOMPLETE` is the other way to reach it, for
a project that wants it to bite: naming a verdict there prints its group as well
as failing on it, so the run that exits 1 is the run that names the entry.

**What would earn `error` back.** Looking the stored PMID up — `PubMed.by_pmids`
already answers for a batch of numbers, so it is a second efetch per fifty
dual-identifier entries — and adjudicating what comes back: a citation
describing another work is a two-sided finding, a citation carrying the stored
DOI is PubMed holding the work twice, and no citation at all is a stored number
naming nothing. Until then the severity states what the evidence supports.

---

## An identifier a registry answered *around*

**A precaution, not a recorded defect.** Nothing was observed doing this; see
the `Observed` line below for what was looked for.

**What happens.** `efetch` returns a MEDLINE record whose own `PMID` line is not
the number requested. `PubMed.by_pmids` declines to adopt it — that record's
metadata and retraction status belong to another paper — and the requested
number is then left with no record.

**Observed.** Nothing, for the substitution itself. `efetch` for the deleted
PMIDs 20000157 and 35000082 answers HTTP 200 with an empty body: no error, no
redirect, no surviving record put in its place. `id=99999999`, far above the
highest number NLM has assigned, answers identically — so an absence and a
withdrawal are the same response, and neither is a substitution. The guard stays
because adopting another paper's citation is unrecoverable while declining one
costs a lookup, but it is a precaution and is written up as one.

**Reported as.** `UNCHECKED`, with an `identifier/inconclusive` issue naming
what came back instead. Never `BAD-ID`, which rests on a narrower fact than the
phrase "no such record" suggests: `efetch` returns the citations it holds and
says nothing at all about the rest, so the evidence is an omission — the empty
body above, reaching `compare` as a plain missing key. That omission covers a
number NLM never assigned and a deleted citation alike, as the three probes
above show. A whole-batch failure — `efetch` answering 404 for the request
rather than 200 for its contents — is reported as `UNCHECKED` too, for the same
reason and at batch granularity, because read as absence it would condemn fifty
entries at once.

**Detection.** In `registries/pubmed.py` and `compare.compare`, not in
`benign.py`. `PubMed.by_pmids` returns a `PmidAnswers` whose two dicts keep
"answered with this record" and "answered with something else" apart, `resolve`
carries the second to `audit`, and `compare`'s `inconclusive` argument is what
stops the `BAD-ID` branch.

**Why it matters.** It is CLAUDE.md's "404 is a fact, a timeout is ignorance" at
an edge with a third state. An answer about another record is not an answer
about this identifier, and only one of the three is evidence about a
bibliography.

---

## Deposit timestamps recorded as publication years

**NO WITNESSED INSTANCE. Candidate for deletion.**

**What it claims to happen.** A working-paper series re-deposits an old item and
the `issued` date becomes the deposit date — a 2020 paper carrying 2026.

**Observed. Nothing, and the named series does the opposite.** Searched: all 438
corpus entries — four disagree with Crossref on the year and *all four run the
other way* — and NBER, the working-paper series the description points at.
NBER's `issued` dates are correct: `10.3386/w0001` carries `issued` 1973-06 and
keeps its 2007-10-23 deposit stamp in `created`, a field this tool never reads.
Crossref's `created` is where deposit timestamps live, and it does not reach
`Record.years` at all.

The *opposite* direction is a real, repeatable false positive and nothing here
covers it. `10.1136/gutjnl-2019-319990` is an online-first BMJ-group paper with
`online` 2020, `issued` 2020 and no print date, while the entry cites the 2021
issue year, correctly; three more corpus entries do the same
(`10.1093/aje/kwj364`, `10.1093/aje/kwm361`, `10.1007/s10549-007-9523-x`).
Whoever replaces this rule should write that one instead, and will need a
defensible bound on the gap: "the entry's year is later than anything the
registry knows" is also what a wrong year looks like.

**Detection.** `benign._year_deposit_artifact` applies only when there is no
print date to corroborate the registry's year *and* the registry year is at
least three years later than the stored one — `benign._MIN_DEPOSIT_STAMP_GAP`.
A series re-depositing an old item lands many years out; a one- or two-year gap
is what citing a preprint's year, or mistyping the last digit, looks like, and
both of those are reported. A registry year *earlier* than the stored year is
not covered either: that is a real discrepancy worth a human's attention.

**Crossref records only.** "No print date" is a fact about the work where the
registry has a print date to report and did not, and Crossref is the only one
of them that does: `crossref._years` writes `print`, `online` and `issued`,
while MEDLINE has no print field at all — `DP` is the issue a citation is filed
under, and it reaches `Record.years` as `issued` — and DataCite, Open Library
and the search clients write `issued` alone. Read as an absent print date, the
guard could never fire off Crossref, and every entry resolved by its PMID whose
year was three or more years early was filed here instead of reported. A
missing key meaning "this registry does not record that" is ignorance, not a
fact.

---

## Online-first versus print year

**What happens.** A work posted online in one year and printed in the next has
two correct years, and different tools cite different ones.

**Handling.** Not a defect at all — `compare._check_year` accepts any year the
registry itself carries, and the verdict stays `OK`. `benign._year_online_first`
exists only so the report can state *which* date the entry is citing, and it is
reached through `compare._note_alternate_date`, which surfaces it as a
`year/alternate-date` issue at `info` severity — **not** as a
`REGISTRY-ARTIFACT`. Neither value is defective, and calling one so would
relabel a large slice of an ordinary epidemiology bibliography, where
online-first is the norm.

`info` is filtered out of the default terminal report, so this reaches a reader
through `--verbose` or the JSON report's `issues` list.

**Both registries carry both years.** Crossref deposits `published-print` beside
`published-online`; MEDLINE files `DP`, the issue the citation belongs to,
beside `DEP`, the day the work went online. Both pairs reach `Record.years`, so
a reference resolved by its PMID gets the same latitude as one resolved by its
DOI — which matters more there, because PubMed is then the only registry
consulted and no second source is present to supply the other date. `Record.year`,
the single value a report prints when it needs one, still prefers the issue.

---

## Container titles: a chapter has two

**What happens.** Crossref's `container-title` is an array, and for a book
chapter it holds both the series and the volume. An entry citing either is
right; keeping only element 0 threw away the one that matched.

**Observed.** `10.1007/978-1-59745-423-0_7` (Hainaut et al., stored as
`hainaut2011biobank`) deposits `["Methods in Molecular Biology", "Methods in
Biobanking"]`. The `@inbook`'s `booktitle` is the second element, character for
character. On the live corpus run this produced the file's only `DISPUTED`
verdict — reported as Crossref and PubMed disagreeing about a title Crossref
itself supplies. Recorded in
`tests/data/audit_crossref_series_and_book_container.json`.

**Handling.** Not a suppression: `Record.container_alternates` carries the rest
of the array and `compare._check_scalar` accepts a stored value matching any of
them, which is the same principle as "any year the registry itself carries is
acceptable". A `container/alternate-title` note at `info` severity states which
one was matched, because the reader is looking at a landing page headed with the
*other* title. A container matching neither is still reported.

---

## Journal names versus ISO abbreviations

**What happens.** `Int J Cancer` versus `International Journal of Cancer`.

**Detection.** `benign._container_abbreviation` accepts the difference when
Crossref's own `short-container-title` matches, or when every abbreviated token
is an in-order prefix of a token in the full name, the last of them lands on
the full name's last token, and no word longer than **three characters** was
passed over on the way.

Two conditions, because each bounds one direction and the sibling-journal test
rests on both. The end anchor stops the prefix direction: `Nature` is not an
abbreviation of `Nature Genetics`, nor `The Lancet` of `The Lancet Oncology`,
and citing the parent title for a paper that appeared in the offshoot is a real
error the tool must still report. The skip bound stops the other one. Without
it, any name whose words are a subsequence of a longer name reaching the same
last word was cleared — `Annals of Oncology` against `Annals of surgical
oncology`, `Journal of Cancer` against `Journal of gastrointestinal cancer`,
`Cancer` against `Pediatric blood & cancer`, all pairs of serials NLM lists
separately. Over the 37,989 serials in the 2026-08-09 fetch of
`J_Medline.txt` the unbounded rule accepts **27,851** ordered pairs of serials
with different titles as abbreviations of one another; bounded, **903**.
`REGISTRY-ARTIFACT` is not a failing verdict, so each of those was an
exoneration printed on a wrong bibliography that exited 0.

**What the residue is.** The two bounds narrow the rule; they do not close it,
and a section reporting only the count leaves a reader unable to tell whether
their own journal is inside it. Two shapes account for most of it, both
measured over the same 37,989-serial list:

* **A short word in front of the whole stored name** — the registry's name is
  one or more words of three characters or fewer followed by the stored name,
  token for token: 240 ordered pairs, 186 distinct stored names, counted on the
  same basis as the 903 above, which is after `_container_leading_article` has
  taken the 15 pairs differing only by `The`/`A`/`An` under its own reason. The
  skip bound passes over words of three characters or fewer, and a publisher's
  imprint is usually one: `Materials letters` is
  cleared against `ACS materials letters`, `Archives of dermatology` against
  `A.M.A. archives of dermatology`, `Precision oncology` against `AI in
  precision oncology`, `Clinics` against `PET clinics`, `Cancer` against `BMC
  cancer`. Every pair is two separate serials in NLM's own list.
* **A name that is the start of a longer one** — the final-token test is a
  prefix match, so a one-token title is cleared against any longer one-token
  title beginning with it: 373 ordered pairs, including `BioMedicine` against
  `Biomedicines`, `Biofilm` against `Biofilms`, `Cancer` against `Cancers`,
  `Bios` against `Bioscience`, and the journal `C` against `Cairo`.

Neither is closable by tightening the existing two bounds, and the obvious
narrowings are not free: requiring the stored name's first token to match the
registry's first — which would refuse the first shape — refuses **2,226 of the
27,334** real `MedAbbr` → `JournalTitle` pairs in that list, since NLM keeps a
leading article on the full title and the abbreviation drops it (`Der
Anaesthesist`, `L'Annee biologique`, 289 of them in a language whose article is
not `the`/`a`/`an`). Requiring the last stored token to be shorter than the
registry's by at least two characters — which would refuse the plural half of
the second — costs **75** of the same 27,334 (`Rev Med Chil` against `Revista
medica de Chile`, `Contin Chang` against `Continuity and change`). Both are
verdict-path changes, so both are the owner's to make; what is recorded here is
what the rule clears today.

**Why a length and not a word list.** ISO 4 shortens each significant word of a
title and deletes the articles, conjunctions and prepositions between them, so
the words a real abbreviation passes over are grammatical furniture: 25,758 of
the 26,500 words skipped across the 25,641 abbreviated titles in NLM's own list
are three characters or fewer — *of*, *and*, *the*, *in*, *de*, *on*, *for*,
*la*, *et*, *für*, *di*, *und*. Which words those are is a fact about a
language, and a stop-word vocabulary would put one language's function words in
the verdict path; a length admits every language on the same terms. It is the
argument `_is_initialism_of` makes, for the same reason.

**What it costs.** 432 of the 25,641 `MedAbbr`-against-`JournalTitle` pairs in
NLM's list are no longer accepted here: the Italian *della*, the Dutch *voor*,
the English *with* and *from*, and the `Part E` / `Series B` sub-series titles.
Most never reach this rule — `TA` is on `container_alternates` and
`compare._check_scalar` accepts a stored value matching one of those before any
suppression is consulted — and the pairs the bound newly refuses are the family
confusions the anchor was written for: `Advances in biology` against `Advances
in cell biology`, `Advances in research` against `Advances in drug research`.

A dropped leading article (`Lancet` for `The Lancet`) is
`_container_leading_article`'s, which runs first and says so in its reason.

---

## Journal names as MEDLINE files them

**What happens.** NLM does not record a journal under the name on its masthead.
It drops the leading article; it appends a place-of-publication qualifier in
parentheses wherever the bare title would be ambiguous; and it writes a subtitle
after a spaced colon. MEDLINE's `JT` is therefore
`Lancet (London, England)`, `BMJ (Clinical research ed.)`, `Science (New York,
N.Y.)`, `Cancer epidemiology, biomarkers & prevention : a publication of the
American Association for Cancer Research, cosponsored by the American Society
of Preventive Oncology`, while `TA` holds the abbreviation — which for the
first three is the journal's plain name and for the fourth is `Cancer Epidemiol
Biomarkers Prev`. A bibliography stores none of these: it stores `The Lancet`.

The subtitle is the sponsoring society on many journals, and it is not always
one. `Archives of medical science : AMS` (PMID 42540560) and `The Malaysian
journal of medical sciences : MJMS` (PMID 42534732) carry the title's own
acronym; `The British journal of psychiatry : the journal of mental science`
(PMID 42552687) carries a descriptive phrase naming no organisation. The rule
below reads NLM's separator, not what follows it, so it suppresses all three
and says *subtitle* rather than *society* in the reason it prints.

**Observed.** PMID 9500320, Wakefield et al. 1998, recorded verbatim in
`tests/data/compare_pubmed_wakefield_retracted.txt` (`TA - Lancet`, `JT -
Lancet (London, England)`), and PMID 32430337, Michaud et al.,
`10.1158/1055-9965.EPI-20-0378`, in `tests/data/pubmed_society_expansion.txt`.
The subtitle is on 26 of the 173 journals in a 300-record sample of
live `efetch` output — *J Clin Oncol*, *Clin Cancer Res*, *Ann Oncol*, *Toxicol
Sci* and *Am J Transplant* among them. It matters most on the PMID path, where
PubMed is the only registry consulted and its `JT` is the only container value
there is: every correct Lancet, BMJ, Science or Cancer Epidemiology, Biomarkers
& Prevention entry resolved by PMID was a `FIELD-MISMATCH`, and the run exited 1.

**Handling, in three places** — a fourth, for the parenthetical qualifier, is
the section after this one.

Every container rule reads `JT`, `TA` and `container_short` off **the record
the compared value came from** — the three below, the qualifier rule after
them, and `_container_abbreviation` above. `compare._check_scalar` takes the
container from the primary registry, or from the corroborator where the primary
deposited none, and hands `benign.classify` that same record. On a DOI-resolved
entry that is PubMed's record whenever Crossref deposited no `container-title`
at all, and judging the value against Crossref's instead asks Crossref to
account for a MEDLINE filing title, which it cannot. The
`container/alternate-title` note below does not go through that rule and does
not need to: it offers every alternate *either* record carries, each printed
with the registry that holds it.

`registries/pubmed._record_from_medline` puts `TA` on the record's
`container_alternates` as well as its `container_short`. Both are titles PubMed
itself names for the same journal, so this is the chapter case above by another
route, not a suppression: an entry storing `Lancet` gets the same
`container/alternate-title` note at `info` severity, naming PubMed as the
registry that carries it.

A **parallel title** joins the same list by the same argument. NLM writes one
serial's two names into `JT` separated by a spaced equals sign — `Journal of
preventive medicine and public health = Yebang Uihakhoe chi` (PMID 42526877,
`tests/data/pubmed_parallel_title.txt`), `Zhongguo yao li xue bao = Acta
pharmacologica Sinica` (NlmId 8100330) — and 607 of the 37,989 serials in NLM's
own list carry at least one. Both halves are the journal's own name and a
bibliography stores whichever its house style uses, so each is offered as an
alternate rather than substituted for `JT`. That is what keeps the **34**
serials whose half equals some other serial's whole title — 38 once `fold` has
run — from merging with it: `Dong wu xue yan jiu = Zoological research` beside
`Zoological research`, `Noshuyo byori = Brain tumor pathology` beside `Brain
tumor pathology`, `Neirofiziologiia = Neurophysiology` beside `Neurophysiology`.
The entry has to match a name, and `JT` stays what the record holds.

`benign._container_leading_article` covers the entry that stores the masthead
name. It accepts a stored value whose opening `The`/`A`/`An`, taken off, leaves
exactly `JT` or exactly one of the titles on `container_alternates`, and it
accepts the mirror: a `JT` or an alternate whose own opening article, taken
off, leaves exactly the stored name. One article comes off **one** side per
comparison, never both — that is what the printed reason claims happened, and
the two reasons differ — so `A Journal of Cancer` against `The Journal of
Cancer` is a difference and not an artifact. It has to be the opening word:
`Journal of the National Cancer Institute` against `Journal of National Cancer
Institute` still fires.

The registry direction is where NLM's parallel titles arrive. `JT - The
Canadian journal of statistics = Revue canadienne de statistique` (PMID
42559441, `tests/data/pubmed_parallel_title_article.txt`) puts each half on
`container_alternates` verbatim, article and all, while Crossref deposits the
journal as *Canadian Journal of Statistics* — the spelling a bibliography
exports. 63 of the 607 parallel titles in NLM's own list open a half with an
article, and this branch is what reaches 62 of them: strip the registry side
back out and an entry storing the article-free masthead form of those 62
reports `container/mismatch`, where today none does. It reaches a whole `JT`
filed under one as well: `The Alaska nurse`.

`_LEADING_ARTICLE` is English only — `the`, `a`, `an`. `Der Anaesthesist`,
`L'Auxiliaire` and `Die Naturwissenschaften` are filed with an article and
abbreviated without one, and this rule does **not** reach them: those articles
fold to three characters or fewer, so `_container_abbreviation` clears them one
rule later and prints `stored name abbreviates the registry name` — the reason
this rule was placed ahead of that one to stop printing, and the reason those
entries are still given. The verdict is the same either way; the sentence is
not. Widening the set would put one language's function words in the verdict
path, which is what the skip bound above chose a length over.

The qualifier is not stripped here at all — this rule compares whole titles,
and what may come off the registry's side beyond an article is decided by the
two rules below. `The Lancet Oncology` against `Lancet (London, England)`
therefore still fires under all three: strip an article from either side and
what is left matches no title that record carries, and take the qualifier off
and `Lancet` is still not `The Lancet Oncology`.

`_container_leading_article` is placed **ahead of** `_container_abbreviation`
in `benign.CHECKS`, which is the only thing that order decides. The wider rule
accepts the same pairing — a dropped article is a token subsequence reaching
the registry's last token — and calls it `stored name abbreviates the registry
name`, which is not true of `Lancet` against `The Lancet`: nothing was
abbreviated. The narrower reason is the one a reader can check.

The `BMJ` record is worth spelling out, because the two spellings a bibliography
uses reach the same destination by different routes. Against `JT` `BMJ (Clinical
research ed.)` with `TA` `BMJ`: a stored `The BMJ`, the masthead, is suppressed
here, its article coming off to leave `TA` exactly; a stored `BMJ` is not this
rule's business at all, because it matches `TA` outright and gets the
`container/alternate-title` note the paragraph above describes, at `info`, with
the entry reported `OK`. Neither is a mismatch, and neither should be.

`benign._container_medline_subtitle` covers the third shape. It compares the
stored name against the part of the registry's value **before NLM's own spaced
colon**, and accepts only an exact match there: `Cancer Epidemiology,
Biomarkers & Prevention` against the `JT` above is suppressed, while `Cancer
Epidemiology` — a different journal, published by Elsevier — differs from that
base title by a word and still fires. Nothing else reaches this case: `TA` is a
real abbreviation for these journals rather than the plain name, and
`_container_abbreviation` requires the stored tokens to reach the *end* of the
registry's name, which is the subtitle and not the journal.

NLM drops a serial's leading article on most titles and keeps it on some, so
that rule also takes one opening `The`/`A`/`An` off the **registry's** base
before comparing. `The Journal of adolescent health : official publication of
the Society for Adolescent Medicine`, `TA - J Adolesc Health` (PMID 42547188)
is the case: the masthead and Crossref both say *Journal of Adolescent Health*
with no article, and until the base was stripped that correct entry reported
`container/mismatch`. The reason printed names which of the two happened. The
stored side is never stripped, so at most one article is dropped in any
comparison and `A Journal of Cancer` against `The Journal of Cancer : …`
remains a `container/mismatch` — two journals differing by exactly the word
that distinguishes them.

That rule reads the colon and nothing else. NLM writes a spaced colon *inside*
a parenthetical qualifier where the body named there needs a date of its own —
`ASAIO journal (American Society for Artificial Internal Organs : 1992)`,
PMID 42552576 — so the first spaced colon in `JT` is not always the separator,
and a base left holding an unclosed parenthesis is refused rather than compared
against: half a qualifier is not a name. The qualifier itself is the fourth
shape, and it has its own rule below.

---

## Parenthetical qualifiers in a MEDLINE filing title

**What happens.** NLM appends a qualifier in parentheses wherever a bare title
would be ambiguous in its catalogue — a place (`Lancet (London, England)`), an
edition (`BMJ (Clinical research ed.)`), a founding year (`Annals of medicine
and surgery (2012)`), the issuing body, or several at once. It is on 2,698 of
the 37,989 serials in NLM's own list,
[`J_Medline.txt`](https://ftp.ncbi.nlm.nih.gov/pubmed/J_Medline.txt). The
masthead, Crossref and the bibliography all carry the bare title.

**Observed.** PMID 42555391, `JT - Annals of medicine and surgery (2012)` with
`TA - Ann Med Surg (Lond)`, recorded verbatim in
`tests/data/pubmed_qualifier_year.txt`; PMID 42552576, `JT - ASAIO journal
(American Society for Artificial Internal Organs : 1992)` with `TA - ASAIO J`,
in `tests/data/pubmed_qualifier_inner_colon.txt`; and PMID 30151503, `JT - The
neurologist (Hyderabad, India)` with `TA - Neurologist (Hyderabad)`, in
`tests/data/pubmed_qualifier_leading_article.txt`. Where `TA` is not the
journal's plain name — and for none of the three is it — nothing else reaches
the entry: `_container_abbreviation` needs the stored tokens to reach `JT`'s
last one, which is inside the qualifier, and `_container_leading_article`
compares against the whole of `JT`. On the PMID path `JT` is the only container
there is, so an entry citing the journal's own name has nothing else to be
compared against, and without the rule below it reports `container/mismatch`.

**Detection.** `benign._container_medline_qualifier` removes one trailing
balanced parenthetical from **every name the registry's record carries for the
container** — its `container`, and each entry of `container_alternates` — and
requires what is left to equal the stored name outright. Never a prefix and
never a substring: *Annals of Surgery* against `Annals of medicine and surgery
(2012)` differs by more than a qualifier and still reports
`container/mismatch`, as *Cancer Epidemiology* does against *Cancer
Epidemiology, Biomarkers & Prevention* next door.

Two things happen on the record's primary value and nowhere else. One opening
`The`/`A`/`An` may come off its remainder, and the remainder may be a single
word. Both are answered below.

**Why the qualifier may be dropped, when in a catalogue it is exactly what
tells two serials apart.** That objection is about looking a journal up, and
nothing in this comparison looks one up. By the time `compare` reaches
`container`, the work has already been pinned — by its DOI or PMID, or, for an
entry carrying neither, by the title, first author and year that
`compare.confirm_without_id` demanded before any record was adopted — and a
work appears in exactly one serial. The registry's `JT` is therefore, by
construction, the serial *this* work appeared in: there is one serial in play
and nothing to merge. What the rule accepts is `Lancet` for a work published in
`Lancet (London, England)`, which is correct.

**What it costs.** For this to be a miss, a citation would have to name a
journal that shares a base title with the right one *and* be wrong about it.
Such pairs are real: `The neurologist` (NlmId 9503763) beside `The neurologist
(Hyderabad, India)` (NlmId 101719078), and 316 qualified titles whose base is
some other serial's full name. So an entry resolved by PMID 30151503 and naming
the journal *The Neurologist* is accepted. That is the whole of the exposure:
it needs the entry to be wrong in the one field the identifier has already
settled, and every other field of that entry is still compared against the
record.

**NLM writes the qualifier onto the abbreviation too, and the rule follows it
there.** `MedAbbr` carries the same parenthetical as `JournalTitle`, and
`registries/pubmed._container_alternates` puts it on the record verbatim: 2,695
of the 37,989 serials carry a qualified `MedAbbr`, against 2,698 with a
qualified `JournalTitle`, and 1,213 carry one on both. A bibliography storing
the abbreviation the way ISO 4, Web of Science and Scopus write it — the
journal's own, with no catalogue disambiguator — matches neither the `JT` it is
compared against nor the `TA` beside it, and no other rule reaches the pairing:
the stored value abbreviates nothing the record holds whole. Measured through
the real `compare()` on a PMID-shaped record built by the shipped client,
**1,075 of those 2,695 serials reported `container/mismatch` [error]** while
the reduction was scoped to `JT`. `Acta Hepatogastroenterol` against `JT - Acta
hepato-gastroenterologica` and `TA - Acta Hepatogastroenterol (Stuttg)` (NlmId
0340734) is the shape.

Because the printed pair is then the stored value beside `JT`, which are *not*
related by a qualifier, the reason says which name was reduced: `registry
appends a parenthetical qualifier to another name it carries for the journal`.

**The floor: two tokens, and what it costs.** On any name but the primary one
the remainder must keep at least two tokens. `JT` is the serial's name in full
and one word of it is still that name — `Lancet` for `Lancet (London,
England)`; `TA` is already a reduction, and one word of it is one truncated
word. `Proc (Bayl Univ Med Cent)` (NlmId 9302033) reduces to `Proc`, which
opens 442 serials' abbreviations in NLM's list — its own among them — and is
the whole of none of them.

Measured: 825 of the 2,695 qualified abbreviations reduce to a single token,
and **208** of those are literally some other serial's whole `MedAbbr` —
`Aging (Milano)` (NlmId 9102503) leaving `Aging`, which is NlmId 0050677's
abbreviation entire. The floor refuses all 825, and the price is **67** of the
1,075 rescues above: `Rehabilitation` against `TA - Rehabilitation (Bonn)`
(NlmId 1302716), `Biochemistry` against `TA - Biochemistry (Mosc)` (NlmId
0376536) and 65 more still report `container/mismatch`. That is the whole of
the change's residual noise, and it is the direction this project errs in.

**What the floor does not remove, named.** 611 pairs remain where a
two-token-or-longer remainder is some other serial's whole `MedAbbr`, and this
rule newly clears **600** of them (the other 11 already agreed on case or as an
alternate title). `Acta Oncol (Madr)` (NlmId 0370346) reduces to `Acta Oncol`,
which is the whole `MedAbbr` of *Acta oncologica (Stockholm, Sweden)* (NlmId
8709065); `Acta Ophthalmol (Copenh)` (NlmId 0370347) reduces to `Acta
Ophthalmol`, which is *Acta ophthalmologica*'s (NlmId 101468102); `Ann Immunol
(Paris)` (NlmId 0353045) reduces to `Ann Immunol`, which is *Annals of
immunology*'s (NlmId 7611917). A reader can look up either half of any of them
in [`J_Medline.txt`](https://ftp.ncbi.nlm.nih.gov/pubmed/J_Medline.txt).

It is the same exposure as the 316 on the `JournalTitle` side and it is bounded
the same way: reaching it needs an entry to name the *other* serial for a work
whose identifier has already settled which serial it appeared in, and every
other field of that entry is still compared against the record.

**No article comes off an abbreviation.** The leading-article branch stays on
the record's primary value. `_LEADING_ARTICLE` matches `An `, and on an
abbreviated title that is *Anales* or *Anais*: all 18 qualified `MedAbbr`
values whose remainder matches the pattern are that shape — `An Pediatr (Barc)`
(NlmId 101162596), `An R Acad Nac Med (Madr)` (NlmId 7505188) — so stripping
there would compare a stored name against a title with its first word deleted,
and clear an entry naming a journal the record does not.

**Two details of the stripping.** The parenthetical is matched from its closing
bracket back to the one that balances it, not by a pattern over its contents,
so NLM's inner colon never splits it and a nested qualifier comes off in one
piece — `Clinical oncology (Royal College of Radiologists (Great Britain))`
(PMID 42546669) is one of forty-nine serials naming a body that itself needs a
country. And a remainder still holding an unclosed `(` is refused: four titles
in NLM's list end on a bracket that does not close the qualifier —
`Interventional radiology (Higashimatsuyama-shi (Japan)`, NlmId 101745449 — and
what is left of one of those is a fragment of a name rather than a name.

The **stored** side is never stripped. At most one article is dropped in any
comparison, so `A Journal of Cancer` against `The Journal of Cancer (Basel,
Switzerland)` stays a `container/mismatch`, and an entry that stores the
qualifier itself is compared as it was written.

**The rule is not scoped to a serial**, and the reason it prints says *journal*
because that is what the container is on all but a handful of entries. Its one
scope guard is the field: it runs on any `container`, from any registry, and a
book's is a volume title. Two real containers reach it that way.

`GeneReviews((R))` is MEDLINE's `BTI` on every chapter of it — PMID 20301425,
`tests/data/pubmed_book_chapter.txt` — and a `booktitle = {GeneReviews}` is
what a bibliography carries. Nothing else covers that pairing: `TA` is absent
on a book record, so `container_alternates` is empty. Crossref's deposit for
`10.1088/978-0-7503-3703-8ch5` (IOP, `type: book-chapter`) is
`container-title: ["Photography (Second Edition)"]`, and an `@incollection`
whose `booktitle` is *Photography* is suppressed against it.

That second one is worth being precise about, because a book differs from a
serial in the way the argument above turns on: a chapter can appear in two
editions of one volume, and those are two works. What the rule accepts there is
an entry that **omits** the parenthetical, on a work whose own identifier has
already settled which edition it is — incompleteness, stated as a
`REGISTRY-ARTIFACT` line rather than passed over. An entry naming the **wrong**
edition still fires: the remainder has to equal the stored name outright, so
`Photography (First Edition)` against `Photography (Second Edition)` is a
`container/mismatch`, exactly as *Annals of Surgery* is against `Annals of
medicine and surgery (2012)`.

---

## Retraction relations deposited in both directions

**What happens.** Crossref's model makes `update-to` and `updated-by` opposites:
a record carrying `update-to: retraction -> X` **is** the notice that retracted
X, and one carrying `updated-by: retraction <- X` **was** retracted by X. Both,
about the same X and the same type, cannot be true. Elsevier deposits exactly
that.

**Observed.** `10.1016/S0140-6736(20)31324-6`, the Lancet's retraction notice
for the Surgisphere hydroxychloroquine paper, carries
`update-to: [retraction -> 31180-6, source retraction-watch]` **and**
`updated-by: [retraction <- 31180-6, source publisher]`. Read naively it has
itself been retracted, so any manuscript about the scandal that cites the notice
was failed `RETRACTED` — a false factual claim about a named work, made with the
tool's full authority.

**Detection.** `registries/crossref._reciprocal_updates`, and the obvious version
of it is catastrophic. The retracted paper `10.1016/S0140-6736(20)31180-6`
carries the mirror image — `updated-by: retraction <- 31324-6` *and*
`update-to: retraction -> 31324-6` — so a rule that discounted every reciprocated
`updated-by` would clear the retracted paper itself, which is the worst miss
this tool can make. What separates the two records is that **Retraction Watch**
recorded the notice side on the notice and the retracted side on the paper. A
relation is therefore discounted only when Retraction Watch names this record as
the notice and does *not* also name it as retracted; every other case, including
a tie, keeps the finding. `10.29328/journal.jcmhs.1001023` is the tie — its
genuine `partial_retraction` points at itself in both arrays, both sourced
`publisher` — and it is still reported. All three payloads are recorded under
`tests/data/`.

---

## Registries disagreeing with each other

Not an artifact and not suppressed. When Crossref and PubMed hold different
values for the same field, the result is `DISPUTED` and both values are printed.
The tool has no basis for choosing between two curated sources, and pretending
otherwise would be the same overreach as rewriting the bibliography.

---

## Open Library

Two entries below, and both are **not artifacts**, in the same sense as
*Online-first versus print year* and *Container titles* above: nothing here is
a case of the registry holding a wrong value. Open Library is instead simply
*thinner* than Crossref — crowd-sourced, and a great many records carry a
title and nothing else — and two design decisions in `compare.py` and
`registries/openlibrary.py` exist specifically to keep that thinness from
reading as a defect in the bibliography. Neither is a `benign.py` suppression:
CLAUDE.md and this file's own introduction require a suppression to name a
witnessed instance — a specific ISBN, fetched and checked — and this project
has no network access to Open Library to record one. What follows is
documentation of the design, not a claim that a specific record was seen to be
wrong.

**`number_of_pages` is a book's total length, not a citation locator.** Every
other registry's `pages` means "the opening page of a citation inside a larger
work" (see *Zero-padded article numbers* and *Article number recorded against
a page range*, above). Open Library's Books API has no such field for a book —
there is no larger work — and the closest it offers is `number_of_pages`, the
book's own extent. `registries/openlibrary.py` maps it into
`Record.pages` anyway, because `Record` has no separate "extent" slot and the
alternative is to drop a value Open Library actually supplies. The
compensating fix lives in `compare.py`: `_check_scalar`'s `optional_for_kinds`
parameter and the identical guard in `_check_pages` stop a `@book` entry with
no `pages` field of its own — nearly every one; BibTeX has no convention for
writing a book's total length there — from picking up a `pages/missing`
warning it can neither fix nor should. The same guard covers `volume` and
`issue`: an ordinary book has no volume-in-a-journal or issue-in-a-volume to
be missing either. A **stored** `pages`/`volume`/`issue` value that disagrees
with the registry's is still reported exactly as before; only the "the entry
never said anything" case is silenced, and only for `kind == "book"` — a
chapter's `pages` is a real page range within its parent book and keeps the
ordinary check.

**A thin record cannot confirm a book on title alone.** `confirm_without_id`
requires title similarity *plus* corroboration from the author list or the
year before accepting a search candidate for an entry with no identifier —
see that function's own docstring for why a title-only match is exactly how a
plausible-but-wrong work gets adopted. Before Open Library existed, every
candidate the tool ever considered there came from Crossref, Europe PMC or
OpenAlex, and all three reliably carry an author list, a year, or both — so
the corroboration checks, each written as "skip if the candidate has nothing
to compare", were never actually exercised on a candidate with *nothing at
all*. Open Library's `search.json` routinely returns exactly that: a title,
and no `author_name`, no `first_publish_year`. Without an explicit guard, such
a record would confirm on the title match alone, both corroboration checks
having silently done nothing — which is precisely the failure mode
`confirm_without_id` exists to prevent, arriving through a source the
original code never had to defend against. `compare.confirm_without_id` now
refuses a candidate outright when it carries neither authors nor a year,
before either corroboration check runs.
