# Known registry defects

Cases where the registry record is wrong and the stored reference is right.
Most are implemented as a check in `src/bibaudit/benign.py`, matches are reported
as `REGISTRY-ARTIFACT` rather than as defects, and none of them ever causes a
value to be adopted.

A few sections below describe defects handled **outside** `benign.py` — in
`names.py`, in `registries/crossref.py` or in `compare.py` — because they are
about the shape of a list or the direction of a relation rather than about two
strings disagreeing. Each says where it lives. Two of them
(*Online-first versus print year*, *Container titles*) are not suppressions at
all: nothing there is defective, and the entry stays `OK` with an `info` note
saying which of the registry's own values matched.

The list is deliberately short. A "tolerance" that is really just "this check is
noisy" belongs in a threshold or in a project's own `.bibaudit.toml`, not here.

Most of what follows is a reproducible defect in the registry's own data, but
not all of it. Some sections describe a filing convention, an encoding choice or
a representation both sides are entitled to, where nobody's record is wrong at
all. They are here because they are reported as `REGISTRY-ARTIFACT` and so owe
the reader the same explanation — read the section before concluding that a
registry got something wrong.

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

## A consortium standing for the whole byline

**What happens.** One side credits the consortium and the other credits its
members: the bibliography stores `{The Endogenous Hormones and Breast Cancer
Collaborative Group}` as a single creator while the registry lists the people, or
the reverse. Neither is wrong. It is the same work described at two levels, and
it is a representation difference rather than a defect in anyone's record.

**Observed.** `key2002hormones` in the 438-entry corpus — *The Endogenous
Hormones and Breast Cancer Collaborative Group*, stored as one creator against a
registry byline of its members. It is one of the eight author flags that corpus
recorded as false positives, and it is pinned at
`tests/test_benign.py::TestAuthorArtifacts`. Neither the entry's DOI nor a
registry response for it is recorded in this repository, so unlike the
interleaved case above there is nothing here to read the suppression against —
the citekey and the test are the whole of the evidence trail.

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

**No witnessed instance.** No entry in the 438-entry corpus this tool was
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

**Detection.** `names.compare_author_lists`. A position is only excused when the
stored surname key appears somewhere in the registry list *and* the registry
surname key appears somewhere in the stored list — a genuine exchange, not a name
that merely went missing.

**Why it is narrow.** Read together with *Consortia credited between people in
the author array*: when a consortium sits inside the byline, every position after
it is shifted by one, and each shifted creator does appear on both sides. Before
`_interleaved_collectives` recognised that shape, this escape absorbed the whole
tail and reported 1446 differences as a reordering that never happened. A rule
that explains a shift it has no evidence for hides the substitution underneath.

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

## Zero-padded article numbers

**What happens.** Journals that number articles rather than paginating them
deposit the number with leading zeros. *Environmental Health Perspectives*
records `027004`; the citing entry carries `27004`.

**Detection.** `normalize.first_page` strips leading zeros after an optional
letter prefix, so both forms compare equal.

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
with a non-digit prefix (`e0123456`, `A102`) needs no floor — no journal
paginates that way.

---

## Doubled tokens from mangled MathML

**What happens.** A title containing mathematical notation is deposited through
a broken MathML conversion and an operator token is repeated, producing
fragments like `do(x)do(x)` in the registry's title.

**Detection.** `benign._title_mathml` removes the doubling and re-compares. Only
an exact match after repair is accepted.

---

## Bracketed parent title on comments and replies

**What happens.** A comment, reply or erratum is registered with the parent
article's title in square brackets ahead of its own. The bibliography carries
only the comment's title, correctly.

**Detection.** `benign._title_bracketed_parent` strips a leading bracketed span
of ten characters or more and re-compares.

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

## Deposit timestamps recorded as publication years

**What happens.** A working-paper series re-deposits an old item and the
`issued` date becomes the deposit date — a 2020 paper carrying 2026.

**Detection.** `benign._year_deposit_artifact` applies only when there is no
print date to corroborate the registry's year *and* the registry year is later
than the stored one. A registry year *earlier* than the stored year is not
covered: that is a real discrepancy worth a human's attention.

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
is an in-order prefix of a token in the full name *and* the last of them lands
on the full name's last token. That final condition is what separates an
abbreviation from a sibling journal: `Nature` is not an abbreviation of `Nature
Genetics`, nor `The Lancet` of `The Lancet Oncology`, and citing the parent
title for a paper that appeared in the offshoot is a real error the tool must
still report. A dropped leading article (`Lancet` for `The Lancet`) still
passes.

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
