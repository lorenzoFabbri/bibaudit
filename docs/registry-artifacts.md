# Known registry defects

This page lists the cases where the registry record is wrong and the stored reference is right. Most are checks in `src/bibaudit/benign.py`. An entry a check matches is reported as `REGISTRY-ARTIFACT` instead of as a defect, and no check ever adopts the registry's value.

A few sections describe defects handled outside `benign.py` (in `names.py`, `normalize.py`, `registries/crossref.py`, `registries/pubmed.py`, `registries/search.py` or `compare.py`), because they concern the shape of a list, the direction of a relation or the spelling of a number, where the other checks compare two strings. Each says where it lives. *Online-first versus print year* and *Container titles* are not suppressions: nothing there is defective, and the entry stays `OK` with an `info` note saying which of the registry's own values matched.

The list is kept short. A tolerance whose only justification is that a check is noisy belongs in a threshold or in a project's own `.bibaudit.toml`.

Most of what follows is a reproducible defect in the registry's own data, but not all of it. Some sections describe a filing convention, an encoding choice or a representation both sides are entitled to, where nobody's record is wrong. They are here because they are reported as `REGISTRY-ARTIFACT` and so owe the reader the same explanation. Read the section before concluding that a registry got something wrong.

Where a section has an `Observed` line, that line records what was seen. Five of those lines say nothing was, each in its first sentence, so a reader can tell a witnessed defect from a guarded possibility without reading to the end. Anyone who catches an instance should record it there.

*One DOI, more than one PubMed citation* and *An identifier a registry answered around* guard against a case the registries' responses can represent but that no probe has caught. They are kept because the two outcomes are not symmetrical: adopting another work's record gives an entry that work's metadata, which no later check recovers from, while declining one costs a lookup. Post-publication status is the one thing carried across a work's citations regardless, and the first of those two sections says why.

*Doubled tokens from mangled MathML*, *Bracketed parent title on comments and replies* and *Deposit timestamps recorded as publication years* are the other three, and that argument does not cover them. Each suppresses a difference with no instance found to justify it, so `benign.py` marks all three **NO WITNESSED INSTANCE** and names them candidates for deletion. Each section says what was searched and what the search found instead. One author escape, *One-character spelling variants*, is in the same position and says so.

`tests/test_benign.py::TestRuleScoping` fails the build when a rule in `benign.CHECKS` or an escape in `names.py` is marked **NO WITNESSED INSTANCE** in the source and its section here is not, so the two cannot drift. The source line carrying the author escape's marker also names the reason the escape emits, and the scan uses that reason to find the section a reader would look up.

Not every section documents a defect, and those that do not say so in their opening lines. *Registries disagreeing with each other* and *Open Library* describe how the tool keeps a difference that nobody's record caused from reading as a defect. *The PMID check is one-sided, and warns rather than fails* documents no difference: it records why a comparison carries the severity it does, and it is here because [why field-level](why.md) sends a reader looking for that.

## Mojibake surnames

**What happens.** A deposit encoded in UTF-8 is decoded as Latin-1 somewhere in the publisher's pipeline, and the registry stores the result. *Gómez* becomes `GÃ³mez`, *Aragonés* becomes `AragonÃ©s`, *Dierssen* loses its initial letter.

**Observed.** Crossref record for **`10.5271/sjweh.3626`** (Papantoniou et al., *Shift work and colorectal cancer risk in the MCC-Spain case–control study*, *Scand J Work Environ Health* 2017, stored as `papantoniou2017colorectal`) returns mojibake for several Spanish surnames. The recorded response is `tests/data/names_crossref_mojibake_author_list.json`.

`10.1093/aje/kwx137`, cited below for its PubMed Central accession, is not an instance of this defect: it is Kim et al., *Alcohol Consumption and Breast Cancer Risk in Younger Women*, with a clean American byline.

**Reported as.** `registry mojibake`.

**Detection.** `names.demojibake` attempts a round-trip repair, `text.encode("latin-1").decode("utf-8")`, on every value. If the result decodes cleanly and matches the stored name, the registry value was mis-decoded. The repair is deterministic and does not guess. An ordinary name containing `Ã` is left alone, because `Ã` before an ASCII letter is not a valid UTF-8 lead byte and the round trip refuses it.

Every value is tried, with no list of tell-tale characters choosing which ones. Such a list covers only the deposits its author had seen, and the round trip already refuses everything the list would exclude, so a list could only remove repairs. One covering the Latin-1 Supplement and Cyrillic (`Ã`, `Â`, `â€`, `Ð`, `Ñ`) leaves out all of Latin Extended-A: the Polish, Czech, Slovak, Croatian, Slovene, Turkish, Romanian, Hungarian, Latvian and Lithuanian diacritics, whose lead bytes are C4 and C5. `LaniÅ¡nik` folds to `lania nik`, the same surname broken into tokens as `AragonÃ©s`, and the same phantom mismatch.

The repair re-encodes as Latin-1 and as cp1252, because a pipeline that reads UTF-8 as text reads it as one or the other, and neither can stand in for the other. They agree on every byte above 0x9F and disagree below it, where Latin-1 has the C1 controls and cp1252 has 27 printable characters and 5 undefined slots. `BeliÄ\x8d` (C4 8D) re-encodes only as Latin-1, because cp1252 has nothing at 0x8D. `Ionescu-TÃ®rgoviÅŸte` (C5 9F, `10.14748/adipo.v4.289`, recorded as `tests/data/names_crossref_mojibake_cp1252.json`) re-encodes only as cp1252, because `Ÿ` is not a Latin-1 character. `Ðšravets` for *Кravets* (`10.14419/ijet.v7i4.3.19550`) has the same shape, one Cyrillic capital opening an otherwise Latin surname. Where both encodings apply they give identical bytes, so trying both changes only which values can be reached. A name carrying typographic punctuation, `O’Brien` among them, is still refused: `’` becomes byte 0x92, a continuation byte with no lead byte in front of it.

**Also observed.** Crossref's byline for **`10.1111/j.1574-6968.1992.tb05540.x`** (Lanišnik Rižner et al., *Fungal 17β-hydroxysteroid dehydrogenase*, *FEMS Microbiol Lett* 1992) is mis-decoded at all three positions (`LaniÅ¡nik`, `Å½akelj-MavriÄ\x8d`, `BeliÄ\x8d`), with lead bytes C5 and C4. The recorded response is `tests/data/names_crossref_mojibake_latin_extended_a.json`. `10.1615/intjmedmushrooms.2024052864` shows a second shape: `TeÌ\x81llez-TeÌ\x81llez` for *Téllez-Téllez*, where the name was deposited in decomposed form and the mis-decoded bytes are the combining acute accent's own two, CC 81.

`clean()` runs an NFKC pass, and NFKC rewrites fourteen of the sixty-four Latin-1 characters that can stand as the second byte of a mis-decoded pair: `Ã³` becomes `Ã3` before `demojibake` sees the bytes it needs. `names._nfkc_continuation_inverse` undoes that substitution first, and without it the rule would not repair the witnessed record.

The inverse table is computed from `unicodedata` instead of written by hand, because a hand-written table is a claim about what NFKC does and drifts from it wherever nobody checked. Five of the fourteen rewritings map one character to one (`¹²³ªº`), and a table naming only those five leaves the other nine irreparable. NFKC expands `¼`, `½` and `¾` to three characters each and turns the four spacing accents into a space and a combining mark, so `MÃ¼ller` for *Müller*, `MichÃ¨le` for *Michèle*, `SÃ¸rensen` for *Sørensen* and `Å½akelj-MavriÄ\x8d` for *Žakelj-Mavrič* all arrive longer than they left the registry, and none of them round-trips.

Where a rewriting produces a bare ASCII character (`a`, `o`, `1`, `2`, `3`), ordinary text produces that character too, so it is read as a continuation byte only directly after `Ã` or `Â` (`names._NFKC_INVERSE_LEADS`), where such a character cannot arise any other way. Admitting every UTF-8 lead byte there repairs 9 correct names into wrong ones among 399,450 creator values from a random Crossref sample: `Çavdar` to `Ǫvdar`, `Çolak` to `Ǻlak`, `Ñanculef` to `Ѫnculef`. Every other rewriting produces a combining mark, a fraction slash or a Greek letter, which a creator name does not contain, and needs no positional guard.

**The one rewriting left uninverted is a guard against a case nobody has witnessed.** U+00A0 is already a space by the time this runs, because `clean`'s whitespace pass collapsed it (NFKC did not), and nothing here can tell it from a space the deposit had. Reading it back would repair `JosÃ©Â Antonio` and mangle a correct name of the same shape equally often. No value in the 399,450 carries `Ã` or `Â` before a space, so the evidence decides the shape in neither direction.

**Why it matters.** Reducing a surname to its last token turns `aragona s` into `s`, and a checker then reports a one-letter surname mismatch on a correct bibliography. For this reason `names.family_key` compares the whole family name.

## Mojibake that also truncated the surname

**What happens.** The same mis-decode, except that the surname's first character is lost: Crossref holds `ierssen` for *Dierssen*. A round trip cannot repair this, because mis-decoding never deletes a byte and so there is nothing to undo.

**Observed.** `10.5271/sjweh.3626` again, position 19 of 24 creators. Position 15 is `Lorca`, undamaged, so a reader who checks the rule at that position finds a correctly spelled surname and wrongly concludes the rule has no instance.

**Reported as.** `registry mojibake truncated the surname`.

**Detection.** `names._surname_truncated_by_mojibake` is narrow, because "a surname missing one leading character" on its own would silence `Ash` against `Nash`, `Rice` against `Price` and `Ellis` against `Kellis`. Three conditions must hold together. The byline must already carry proven mojibake at another position (`_list_carries_registry_mojibake`: a round trip that lands on a surname the bibliography holds). The forename initials must agree. And the registry surname must be uncapitalised where the rest of that deposit is capitalised, which is what separates byte damage from a different family. Crossref writes `"family": "ierssen"` in lower case among twenty-three capitalised surnames; a registry naming a different person writes `Rice`.

## Consortia credited between people in the author array

**What happens.** Crossref credits a consortium as an `<organization>` creator positioned *inside* the personal byline — `{"name": "for the Pancreatic Cancer Cohort Consortium (PanScan)"}` at position 5 of 9. BibTeX has no slot for a corporate creator sitting between two people, and Crossref's own content-negotiated BibTeX emits none of them, so any bibliography exported that way holds only the people. Compared position against position, the consortium reads as a person substitution and every position after it is shifted by one.

**Observed.** `10.1158/1055-9965.epi-23-0009` (Kim et al., stored as `kim2023abo`), recorded in `tests/data/audit_crossref_interleaved_collective.json`. Also `10.1093/jnci/djad043` (7 consortia), `10.1038/s41467-020-16483-3` (16) and `10.1038/s41586-025-09272-9`. On a 438-entry corpus this shape accounted for 27 of 30 `FIELD-MISMATCH` verdicts and all 15 remaining `INCOMPLETE` verdicts that concerned `authors` alone: 42 entries, 9.6% of the file, none of which disagreed with Crossref about a single person.

**Reported as.** `registry interleaves collective creator(s) the byline omits`, followed by the organisations in full. The whole-byline case is a different rule with different evidence, described in *A consortium standing for the whole byline* below.

**Detection.** `names._interleaved_collectives` drops the collectives from the registry side, and what remains must align exactly: the same length as the stored list, with every position agreeing informatively. That alignment is the evidence the suppression rests on. If one person is substituted, or one person is missing from the stored list, the shape does not hold and the ordinary comparison runs. Crossref's own `<organization>` slot decides which creators are collectives; no list of marker words does, and the corpus credits `DiscovEHR`, `GSK`, `AstraZeneca` and `UK Biobank`, none of which contains one.

**Accepted residual.** A person misfiled into the organisation slot, whom the bibliography also omits, is not reported. Any export derived from Crossref drops that creator too, so the omission is the publisher's defect and not one the user can act on.

## Consortia the byline credits and MEDLINE files apart

**What happens.** This is the mirror of the section above, and the same defect. Crossref credits a consortium as an `<organization>` inside the `author` array; MEDLINE files it under `CN` and lists only people under `FAU`/`AU`. A bibliography exported from Crossref keeps the organisation (Crossref's own BibTeX does not), so it carries a creator the MEDLINE byline lacks, and every position after it is shifted by one.

**Observed.** PMID 42552006, `10.1136/bmjopen-2025-107667`, recorded verbatim in `tests/data/pubmed_collective_creator.txt`. Crossref's byline is nine creators with `for the ITC Project Collaborators` at position 6; MEDLINE's `FAU` list is the same eight people, with `CN - ITC Project Collaborators` beside them. The entry reported `#6 for the ITC Project Collaborators` against `#6 Kress, Alissa C` and a substitution at every position after. Four of 386 entries in a 396-entry live sample took this shape, one of them shifting 25 consecutive positions.

**Reported as.** `byline carries collective creator(s) the registry files apart`, followed by the organisations in full. The sentence names the side that carries them, because a reader told the wrong side goes looking in the wrong record.

**Detection.** `names._byline_collectives` uses the same evidence and bound as its mirror: it drops the collectives from the stored side, and what remains must be the same length as the registry's list, with every position agreeing informatively.

**Why `CN` is not read onto the record instead.** MEDLINE does write it in byline position, but not in Crossref's byline position. PMID 42521817 (`10.1038/s41591-026-04492-6`) groups five consortia together after the 42nd author, where Crossref interleaves two of them among people at positions 44 and 46, so reading `CN` would replace one misalignment with another. This escape does not depend on position.

## A consortium standing for the whole byline

**What happens.** One side credits the consortium and the other credits its members: the bibliography stores `{The Endogenous Hormones and Breast Cancer Collaborative Group}` as a single creator while the registry lists the people, or the reverse. Neither is wrong. The two sides describe the same work at two levels, and nobody's record is defective.

**Observed.** `key2002hormones` in the 438-entry corpus: *The Endogenous Hormones and Breast Cancer Collaborative Group*, stored as one creator against a registry byline of its members. It is one of the eight author differences that run flagged, all of them false positives, and `tests/test_benign.py::TestAuthorArtifacts` pins it. The corpus is private. Neither the entry's DOI, nor a registry response for it, nor the baseline that recorded the eight is in this repository, so unlike the interleaved case above there is nothing here to check the suppression against: the citekey and the test are all the evidence there is.

**Reported as.** `collective author` when the bibliography holds the single collective, `registry lists a collective author` when the registry does.

**Detection.** `names.compare_author_lists` applies it before the positional walk begins. It inspects only the side that holds one creator and checks that the creator is a collective. The other side is not inspected, because once the two lists describe the byline at different granularities there are no positions to compare.

**What it gives up.** Unlike the interleaved case above, this escape has no alignment as evidence, so it suppresses the author-count difference on the strength of one side holding a single collective creator. It therefore cannot tell a consortium byline from a bibliography that replaced a real author list with a group name. Both values are printed under `REGISTRY-ARTIFACT` at the first position, so the report shows the collective against the registry's first creator instead of the two bylines in full. That is enough for the reader to see which shape it is; the tool does not decide between them.

## Registry bylines missing their first author

**What happens.** The deposit opens one creator short. The paper's byline begins with Clavel-Chapelon; Crossref's record holds nine creators beginning with `van Liere`, marked `"sequence": "first"`.

**Observed.** `10.1097/00008469-199710000-00007` (Clavel-Chapelon et al., *E3N, a French cohort study on cancer risk factors*, Eur J Cancer Prev 1997, stored as `clavelchapelon1997e3n`), deposited by Ovid/Wolters Kluwer. Recorded in `tests/data/names_crossref_first_author_omitted.json`.

**Reported as.** `registry omits the first author`.

**Detection.** `names._registry_omits_first_author` tests the alignment and names no citekey. Exactly one creator is missing and it is the first; at least three creators remain, all agreeing in order and informatively; the dropped surname is absent from the registry list; and neither side carries an et-al marker.

**What this cannot do.** A bibliography that prepends an author who was never on the paper produces the identical shape, and no author list can separate the two. The difference is still printed with both names under `REGISTRY-ARTIFACT`, because a suppression here is stated and not failed, never hidden; it only does not break the build. The evidence that would separate the two (the citekey, the title) lives outside `names.py`.

## Particle filing

**What happens.** A nobiliary particle is part of the filing surname in one source and not in the other: the bibliography stores *van Eijck* and the deposit files the creator under *Eijck*, or the reverse. Neither is wrong. Both conventions are in use, and the choice is the cataloguer's.

**Observed.** No deposit was recorded in which the two sides file a particle differently, so there is no DOI to cite here. `van Eijck` is a real surname in the 438-entry corpus and one of the two surnames the module is written around (the other is `Clavel-Chapelon`). The rule is pinned in `tests/test_names.py`.

**Reported as.** `particle filing`.

**Detection.** `names.names_agree` calls `family_key(..., drop_particles=True)` on both sides. The particle is dropped from both names and what remains must match exactly. Nothing is inferred about which convention is correct, and the stored value is never adopted. `names._PARTICLES` is the vocabulary.

## Compound surnames shortened to their final element

**What happens.** A registry keeps only the last element of a compound surname (`Chapelon` for *Clavel-Chapelon*), or sweeps a forename into the family field, so the deposit holds `Cristina-Marianini-Rios` where the bibliography holds `Marianini-Rios` with `Cristina` as the given name. Hyphen and space are already interchangeable, because `fold()` turns both into a space, so what remains is a difference in how many elements the surname has.

This one is a defect in the registry's data. A compound surname is the name the person has, and a deposit holding part of it holds the name wrong. That is why the rule requires the shorter form to end the longer one, instead of treating the two as equally valid filings.

**Reported as.** `compound surname shortened`.

**Detection.** `names.names_agree` requires the stored surname to be a token-level suffix of the registry's, or the registry's of the stored one. The suffix is made of whole elements, never a character prefix, so `Martin` is not accepted against `Martinez`.

The rule depends on the suffix condition; a test that the two names share a final token would be wrong. Requiring the shorter name to end the longer one separates people who only share a surname: `Krebs-Smith` is not `Davey Smith`, and `González-González` is not `Martínez-González`. Both pairs are real, both appear in the 438-entry corpus under different DOIs, and a final-token test clears both. `Clavel-Chapelon` against `Clavel` is rejected for the same reason: dropping a final element is not a shortening this rule recognises. Pinned in `tests/test_names.py`.

## One-character spelling variants

**What happens.** Transliteration produces two spellings of one surname: *Ivanov* and *Ivanova*, *Papantoniou* and *Papantoniu*.

Nobody's record is wrong. Romanisation from Cyrillic, Greek or Han has no single standard, so two sources transliterating the same name correctly can reach two spellings, and neither corrects the other.

**Reported as.** `spelling variant with matching initials`.

**Detection.** `names.names_agree` accepts a variant only when three conditions hold together, each excluding a named set of pairs:

- One edit apart, where a shared prefix is not enough, so `Martinez` (two edits from `Martin`) and `Smithers` (three from `Smith`) are reported, not suppressed.
- The same first character, so a difference in the leading character is never accepted, and `Herman`/`Sherman` and `Rossman`/`Grossman` are reported. That damage is for `_surname_truncated_by_mojibake` to judge, and only with evidence.
- At least six characters on both sides, so the short surnames where one edit makes a different family are reported: `Chan`/`Chang`, `Wan`/`Wang`, `Lin`/`Liu`, `Kim`/`Kum`.

The forename initials must also agree, which rejects `Kowalski, Anna` against `Kowalska, Piotr`.

**NO WITNESSED INSTANCE.** No entry in the 438-entry corpus this tool was developed against reaches this branch. It is kept, narrowly, for the transliteration variants a multilingual bibliography does produce, and it is a candidate for deletion, not for widening. A rule accepting any two surnames that share three leading characters on a matching initial would clear `Chan` against `Chang`, `Wan` against `Wang`, `Martin` against `Martinez`, `Smith` against `Smithers`, `Gonzalez` against `Gonzalo` and `Sancho` against `Sanchez`: pairs of different families, each cleared against the other on an initial that thousands of researchers share.

## Author lists in a different order

**What happens.** The same people appear in both lists in a different order, so a position-against-position comparison reports every moved creator as a substitution. Neither record is wrong: an author order is a fact about the paper that both sides may serialise differently.

**Reported as.** `reordered`.

**Detection.** `names.compare_author_lists` requires two conditions together. The two bylines must hold the same creators counted, meaning every surname key with its multiplicity on both sides. And at the disagreeing position the stored surname key must appear somewhere in the registry list, and the registry surname key somewhere in the stored list.

**Counted, because presence alone is not a reordering.** A test of set membership alone excuses a byline that has lost an author. A one-position shift puts every stored creator somewhere in the registry list and every registry creator somewhere in the stored one, so each shifted position is excused and only a count warning survives, which does not fail. Position 1 would catch it, since the dropped surname is nowhere in the shortened list, except when that surname repeats later in the byline, which is routine in Chinese, Korean and Japanese author lists. PMID 38213033's thirteen creators are Lee, Jung, Kim, Lee, Lee, Baek, Kwon, Shin, Kim, Shin, Park, Park, Kim, and with the first removed, a membership test reads each of the twelve remaining positions as a reordering. 104 of 2,551 live entries took that shape under the membership test.

Counting also rules out a length difference, so a name that went missing is never read as a moved one. Over 3,040 MEDLINE/Crossref pairs of the same work, 9 entries carry a `reordered` position. Six hold the same creators counted and keep it, and the 3 that lose it are the three whose bylines differ in length: 13 against 6, 3 against 2, 14 against 13. Dropping the first creator from each of 824 live entries fails every one under the counted test, where the membership test absorbed 104 of 2,551; the same 824 entries unmutated fail none.

**What counting reports that membership excused.** A reordering in a byline that also carries a spelling difference (a mojibake surname, or a particle one side files and the other does not) does not hold the same keys counted, so its moved positions are reported. That compound shape has no witnessed instance, and the mistake runs in the direction a reader sees: the tool complains where it might have stayed quiet.

**Why it is narrow.** Read this together with *Consortia credited between people in the author array*. When a consortium sits inside the byline, every position after it is shifted by one, and each shifted creator does appear on both sides. Unless `_interleaved_collectives` recognises that shape first, this escape absorbs the whole tail; measured without it, the escape reported 1446 differences as a reordering that never happened. A rule that explains a shift it has no evidence for hides the substitution underneath.

## Creators the entry names past the registry's last

**What this is, and why it is not a suppression.** The two sections here are the exceptions to a check that fails the build, where everything else in this file suppresses one. `names.compare_author_lists` walks two bylines in step and stops at the shorter one, so a creator appended to the end of an entry's byline is compared against nothing, and on its own leaves only an author-count warning, which does not fail. Without the tail check, appending a fabricated name to an otherwise correct byline produced no failing verdict on 4,255 of 4,255 live entries.

An appended invented name is the documented failure mode of a generated bibliography, and it is why `compare._check_authors` compares the whole list and not only the first author. So the tie breaks towards the finding: a creator past the registry's last position is reported as `authors/uncorroborated`, at error severity, one line per creator, naming the creator. A count line names nobody, and the reader's question is which name no record carries.

**It is a tie.** A registry whose byline is short at the tail produces the identical shape, as *Registry bylines missing their first author* describes for the other end, and no author list can separate the two. Over 3,040 works whose MEDLINE and Crossref records were both fetched, in five windows spanning 1992-2026, the two registries disagree about byline length on 40 (1.3%). After the exceptions below, an entry written from the publisher's deposit and checked against MEDLINE alone reports a creator on 7 of them (0.23%). That is the PMID path, where there is no second witness. On the DOI path, where there is one, it reports none. Without the tail check, none of these shapes was reported.

A live sweep of 1,177 entries written from the MEDLINE record, and so correct by construction, fails none of them and reports no uncorroborated creator. Append one fabricated name to each of the same 1,177 and every one fails, against 0 of 4,255 without the tail check.

Four kinds of tail position do not support that claim, and each is excluded from it instead of reported.

### A creator the corroborating registry does name

**What happens.** Only one byline is ever compared, the primary registry's, or the corroborator's where the primary deposited none, and *uncorroborated* is a claim about every record that answered. Without this exception, a Crossref deposit that stops short of the paper's real byline has the entry's remaining creators reported against it, while PubMed, already fetched and parsed, names every one of them.

**Observed.** 2 of the 3,040 works above, and they are all of the DOI path's exposure to this check.

**Reported as.** `authors/count`, the length difference, at warning severity, which is the report that fits this shape. The primary's byline is short, and an entry naming creators a second registry corroborates is not a fabrication. Nothing is suppressed here: the finding is withdrawn, not hidden.

**Detection.** `compare._drop_creators_the_corroborator_names` matches on surnames, on the same terms as the rest of the author comparison. It excludes empty keys, because a corroborator holding one creator whose surname `fold` discards would otherwise vouch for every stored creator whose surname it also discards. The PMID path consults no second registry and keeps its findings.

**The residual.** A fabricated name sharing a surname with a real creator on the corroborator's byline is dropped.

### A creator Open Library's title-page transcription names

**What happens.** An Open Library edition carries two bylines. `authors` links author records and is routinely short; `by_statement` is the cataloguer's transcription of the title page. Without this exception, the creators the linked list leaves out are reported against a record that names them in its own statement.

**Observed.** ISBN 0412042312, *An Introduction to the Bootstrap*: `authors` links Bradley Efron alone, and `by_statement` reads "Bradley Efron and Robert J. Tibshirani." (`tests/data/openlibrary_by_statement_second_author.json`, fetched 2026-11-26). The work record, `/works/OL3905086W`, links Efron alone as well.

**Reported as.** `authors/count`, at warning severity, as in the section above.

**Detection.** `compare._drop_creators_the_title_page_names` withdraws a creator when their surname is a whole word of the folded `by_statement`, so "Day" is not found inside "Dayton", and never when the surname folds to nothing. The statement is free text ("edited by", "Vol. 1", "with a foreword by"), so it is searched and never parsed into a byline, and nothing else reads it. Only an Open Library primary is consulted this way.

**The residual.** A fabricated name whose surname happens to appear anywhere in the statement, as an editor's or a translator's say, is dropped.

### A collective creator past the registry's last

**What happens.** An organisation is not an invented co-author. MEDLINE files consortia under `CN` and lists only people under `FAU`/`AU`, so a byline exported from Crossref carries a creator MEDLINE's list does not. *Consortia the byline credits and MEDLINE files apart* covers that defect where the remaining people align exactly. Where they do not, that escape declines the byline and the consortium arrives here.

**Observed.** PMID 38236418 (`10.1007/s00392-023-02363-5`): the entry built from Crossref's deposit carries 21 creators including `on behalf of the STAAB consortium`, MEDLINE's byline is 11 people with a `CN` line beside them, and the consortium sits past MEDLINE's last position. It is 1 of the 11 tails in the live sample above.

**Reported as.** `collective creator past the registry's last`.

**Detection.** `names._tail_past_the_registry` reads `Name.collective`. For a creator that came from a registry, that flag is the registry's own answer (Crossref's `<organization>` slot); for one from the bibliography, it is the adapter's collective-author parse.

**The residual.** A fabricated organisation appended to a byline is excused. The failure mode this check exists for invents people.

### A surname the registry's byline carries somewhere else

**What happens.** Saying "the registry does not name this person" would be false against the record being quoted. Two shapes reach this case: a bibliography that repeats a creator, and a misalignment this tool declined to explain. Run a byline with a plausible senior author prepended, and below its alignment floor, through `names._registry_omits_first_author`, and the entry's last creator lands past the registry's end although the registry list carries it.

**Observed.** PMID 42552006, the ITC Project entry in `tests/data/pubmed_collective_creator.txt`, with one of its people substituted. The shape then no longer holds for *Consortia the byline credits and MEDLINE files apart*, the positional comparison runs, and `Ahluwalia, Indu B` ends up one position past MEDLINE's last, though MEDLINE carries the name.

**Reported as.** `credited elsewhere in the registry byline`.

**Detection.** `names._tail_past_the_registry`. The creator's `family_key` must be non-empty and appear among the registry byline's keys.

**The residual.** A fabricated name that happens to share a surname with a real co-author is excused, not reported. That is a real gap, and closing it would mean printing a claim the registry's own record contradicts.

## Forenames the two sides write differently

**What this is, and why it is not a suppression.** Like the section above, this is a check that fails the build. It is written up here because everything it excuses is a difference the registries produce constantly and legitimately, and a reader who meets one wants to know it was seen and passed over on purpose. The check itself is the narrower half: a creator whose surname the registry confirms and whose forename it contradicts is reported as `authors/forename`, at error severity, on one line naming both spellings.

**What fires.** An incompatible forename under an agreeing surname: `Kenneth` against `Margaret`, `K` against `M`. Nothing else fires. The check compares the first initial and only the first initial, on both sides, and only where both sides supply one.

**What does not fire, and why each is left alone.** Registries disagree about forenames far more often than they disagree about people, so the check leaves each of these alone; otherwise most of what it reported would be false alarms.

- An initial against the name: `K P` against `Kenneth P`, `E` against `Esther M.` A registry that gave a letter cannot be asked for more than the letter.
- A middle initial one side omits: `Frits H M` against `Frits`. Only the first initial is compared, so what follows it is free to differ.
- Initials run together against initials separated: `KP` against `K P`. `fold` leaves those as one token and as two, and both begin with `k`.
- Hyphenation and accents: `Jean-Pierre`, `Jean Pierre` and `J.-P.` all have the initial `j`, because `fold` turns a hyphen into a space and strips the diacritic.
- Mojibake. `names._forename_initials` offers both the raw reading and the `demojibake` repair, and one agreeing is enough, so a forename whose first letter the registry's own bytes destroyed is not read as a different person. *Émile* mis-decoded arrives as `Ã\x89mile`, and the lead byte `Ã` folds to `a`. **NO WITNESSED INSTANCE** of the first letter being the damaged one: the deposit this module's mojibake handling is built on (`10.5271/sjweh.3626`) carries four mis-decoded forenames (`InÃ©s`, `JosÃ© AndrÃ©s`, `AndrÃ©s`, `Benito MirÃ³n`), all four opening on an ASCII letter, and 3,963 live MEDLINE/Crossref pairs carried no mis-decoded forename. It is kept, not deleted, because it can only make the tool complain less about a string that is provably mis-decoded UTF-8, and the names it protects (*Ángel*, *Émile*, *Øystein*, *Ólafur*, *Åsa*) are ordinary in these bylines.
- Another script or another transliteration. A forename that `fold` discards entirely (`健太`, `Владимир`, `الحسن`) yields no initial on that side, and an absent initial is ignorance, not disagreement, so the check stands down.
- A compound surname the two sides divide differently. Crossref's deposit for `10.1016/0002-9378(83)90252-1` (PMID 6650633) files *A. López Bernal* as `"family":"Bernal","given":"López"` against MEDLINE's `FAU - Lopez Bernal, A`. The surnames still agree (see *Compound surnames shortened to their final element* above), but one `given` field then holds part of the surname, and comparing it against `A` accuses a correct citation. `names._forename_is_a_surname_element` stands the check down whenever every token of one side's forename is a token of the other side's surname and the two sides file different surnames, which they necessarily do when they divide a compound differently. Both conditions are needed. Without the second, the subset test on a single-token surname reduces to "the forename equals the surname", which describes a person named *Li Li*. 54 of 51,534 compared creator positions in live MEDLINE/Crossref pairs are `Li, Li`, `Yang, Yang` or `Wei, Wei`, with both registries agreeing, and the check would stand down at every one, so an entry crediting somebody else would come back agreed with no reason attached and nothing for `--show-suppressed` to recover. Narrowing the condition the other way, to a compound surname on the other side, breaks the case the exception exists for. Crossref files *María Maitre Azcárate* as `"family":"Azcarate","given":"Maria Maitre"` against MEDLINE's `Maitre, Azcarate` (`10.1007/bf00801918`), one element each in opposite fields, and that shape is commoner than the compound: 15 of the same 51,534 positions, every one a registry that filed `given` and `family` the other way round.

**Detection.** `names.names_agree` calls `names._forenames_are_incompatible` ahead of every surname rule, because a mojibake repair, a particle filing or a shortened compound decides only that two surnames name one family, and none of them looks at which member of it. `compare_author_lists` then routes the position to `AuthorDiff.miscredited` before the *Author lists in a different order* escape can reach it. Substituting a forename leaves both bylines holding the same surnames counted, so that escape would otherwise excuse every one of these as a creator who moved, although the surname sits where the entry put it.

**Measured over 3,963 MEDLINE/Crossref pairs of the same work**, fetched live (51 publication years, 1975–2025). Comparing the two registries' own bylines against each other, 3 of the 3,876 pairs with a byline on both sides gain a finding: 0.077%, and the same 3 in either direction. One is a false alarm: PMID 990943, `Edgerton, V R` against Crossref's `Edgerton, Reggie`, one person recorded by the middle name he went by. In the other two the registries disagree about who is credited, each confirmed against a third source: PMID 414278 (`Shih, R M` against `Shih, Tsung-Ming`, and Springer's own deposit says Tsung-Ming), and PMID 37689523 (`Perez Arqueros, Valeska` against `Arqueros, María`, whose ORCID 0000-0001-5649-3404 records *María Arqueros*). Replacing one creator's forename with an incompatible one is reported on 3,652 of 3,693 entries (98.89%), against none without the check.

**The residuals.** There are three.

A person recorded by a middle name they go by is reported, as PMID 990943 is. The alternative was measured: admitting the whole initial string of a side written entirely as initials, so that `Reggie` may answer the `R` in `V R`, loses 11 caught mutations to remove that 1 false alarm. Eleven misses per false alarm is the wrong trade for the one check whose miss credits the wrong person, so the narrow rule is the one implemented.

A forename substituted at a position the two bylines order differently is not seen: the surnames disagree there, so the pair is a reordering and never reaches this check. That is 41 of 3,693 in the same battery, 1.11%.

Two forenames sharing an initial are accepted (`Kenneth` against `Karl`), which is the direct cost of the first exception above.

## A creator the primary deposited malformed and PubMed did not

Handled in `compare._dispute_creators_the_corroborator_agrees_with`, not suppressed. The byline compared is the primary registry's, so a creator Crossref deposited malformed fails as `authors/mismatch` or `authors/forename` even when PubMed, fetched for the same DOI, names that creator exactly as the entry does. Crossref's deposit for 10.1093/ije/9.1.89 gives its third author the forename "and P G", the conjunction of a byline string split at the wrong place. Its deposit for 10.5705/ss.202015.0116 splits "Tchetgen Tchetgen, Eric" into family "Tchetgen" and given "Tchetgen, Eric". MEDLINE has `Smith, P G` and `Tchetgen Tchetgen, Eric J`. The cache's 33,520 Crossref creators on 2026-11-26 show further shapes of the same kind (a comma inside a family name, "Anne,Lyngholm" as a forename), so the handling reads PubMed rather than repairing one shape at a time.

At a position where the primary's creator disagrees with the entry and PubMed's creator at the same position agrees with it by the same name test the byline walk uses, the entry reports `authors/disputed` (info, verdict `DISPUTED`) with both registries' values, and does not fail. That is what *Registries disagreeing with each other* below describes for every text field. Same position only: an invented creator matches neither registry and still fails, and a name PubMed carries at some other position is left to the reordering rule. A DOI PubMed does not index gets no help.

Observed: `tests/data/names_crossref_conjunction_in_given_name.json` with `names_pubmed_conjunction_in_given_name.txt`, and `names_crossref_compound_surname_split_into_given.json` with `names_pubmed_compound_surname_split_into_given.txt`.

## Author comparisons with nothing to compare

**What happens.** In four situations a position passes without either side being evidence that the two creators are the same person: one side has no surname; a surname is written outside the comparison alphabet, so `family_key` returns nothing usable; the registry truncated a surname to a single character; or the list carries an et-al marker in place of a creator.

**Reported as.** `one side has no surname`, `registry surname truncated`, `et-al marker`, and `surname outside the comparison alphabet`.

**Observed.** No single deposit is cited, because these arise from the shape of a byline, not from one registry's defect. `names.py` records the case that motivated separating them out, a registry list of `[王, 李, 张]` "aligning" against `[Smith, Jones, Brown]`, and `tests/test_names.py` pins it.

**Detection.** `names.names_agree` returns each as a statement that the comparison had nothing to work with, and every one is printed under `REGISTRY-ARTIFACT` so the reader can see it was reached.

**Why it matters.** None of them may be counted as a creator that aligned. `names._agrees_informatively` excludes all four from the alignment counts that `_registry_omits_first_author` and `_interleaved_collectives` rest on. Three agreements of this kind carry no evidence, and counting them would let a registry list of `[王, 李, 张]` "align" against `[Smith, Jones, Brown]` and suppress the author-count difference as well.

## A full-author tag written without its comma

**What happens.** MEDLINE's `FAU` is `Surname, Initials`, and the comma says which half is which. A few citations carry the tag without it, because the publisher deposited no forename and no initials and NLM put the whole creator in the surname slot: the XML behind one of these carries `<LastName>` alone, with neither a `<ForeName>` nor an `<Initials>` beside it. The value is then a name whose two halves the registry never separated, and it is not the abbreviated `AU` form even though it looks like one.

Read as the abbreviated form, with the last token as the initials block, a value that was only `<LastName>` loses its last word to an invented forename. `Xiaodong Lv` becomes a creator surnamed `Xiaodong` with the forename `Lv`, and the bibliography that spells the name right is reported at error severity.

**Reported as.** `registry name deposited in one slot`, where the stored name agrees with one of the readings. Nothing is reported where it agrees with the slot taken whole, which is an ordinary surname match.

**Observed.** 23 of 36,568 `FAU` values carry no comma. Five have the whole-name shape (`de LAVERGNE`, `Xiaodong Lv`, `Hung Nguyen`, `Editorial Board Of Radiology` and `The Lancet Child Adolescent Health`), reproducing on `10.1016/j.rxeng.2025.101663`, `10.1016/s2352-4642(23)00169-4` and `10.1016/j.plaphy.2024.109034`. The rest have the initials shape, written character for character as that record's own `AU` line because NLM never backfilled the comma: `Okano J` (PMID 11278851), `Chung H`, `Watanabe Yi`, `Liu Cj`, `van der Schaaf A`, `Meijer Drees R`, `van Veenendaal MA`, `Van Siclen CD`, `K Sikorska` (PMID 31128948) and the single-token `Desriani`, 10 of 16,511 values in a sample of 3,500 citations spanning 1992–2026.

Nothing NLM exposes separates the two shapes. Fetched on 2026-08-20, `efetch` returned `<LastName>Okano J</LastName>`, `<LastName>K Sikorska</LastName>`, `<LastName>Xiaodong Lv</LastName>`, `<LastName>Editorial Board Of Radiology</LastName>` and `<LastName>The Lancet Child Adolescent Health</LastName>`, every one without a `<ForeName>` or an `<Initials>`. The structured form holds exactly what the MEDLINE line holds, so requesting it would add nothing. `Ho Yi` is Ho, Y. I. (`"Ho Yi"[au]` answers 14 citations), while `Xiaodong Lv` is Lv, Xiaodong, and both are two title-case tokens in the same element. A rule on length or capitalisation refuses one in order to accept the other.

**Detection.** `registries/pubmed._parse_fau` reads a value with a comma through `names.parse_name`, and returns one without a comma as the surname slot verbatim, marked `Name.unsplit`. The same function reads `FED`, the editor tag that follows `FAU`'s convention. `names.names_agree` then compares the stored name against the slot taken whole and, failing that, against the two readings `names._unsplit_readings` offers: the last token as the forename, and the first token as the forename.

Both readings go through the full comparison, and each has to agree informatively. A reading that leaves a one-character surname behind reaches `registry surname truncated`, which accepts any stored surname, so offering it would manufacture the evidence for an escape. So `Okano, J.` agrees with `Okano J` and `Lv, Xiaodong` agrees with `Xiaodong Lv`, while `Smith, J.` agrees with neither and is reported.

**Why it matters.** A stored name that matches no reading is still a finding, so a creator the bibliography invented is not cleared here. The escape gives up the case where a fabricated name happens to be the other reading of a real one, which is the cost of not choosing a reading the registry declined to state.

## An initial written in front of the surname

**What happens.** MEDLINE's abbreviated tags put the surname before the initials. One recorded citation writes them the other way round: PMID 31128948 carries `K Sikorska` in both `FAU` and `AU`, beside fifteen colleagues written in the form `Koole, S N`. `"Sikorska K"[au]` answers 215 citations and `"K Sikorska"[au]` only that one, so the record breaks NLM's own order.

**Reported as.** Nothing. This is a parsing rule, not a suppression.

**Detection.** `pubmed._initials_ahead_of_the_surname` requires two conditions, each excluding a shape the same live sample carries; 27 of 33,026 `FAU`/`AU` values open with a single letter.

- Exactly two tokens, the first one character long. Eight surnames in the sample begin with a lone letter (`A Richmond, Jacqueline`, `T Rahma, Azhar`, `E Albuquerque, Rodrigo Pires`, `W Y Chan, Stella`), and the abbreviated form of each carries its initials as a further token, so none is two tokens long. The test is for exactly one character, because two-letter surnames are among the commonest in this literature and take a transliterated initials block: `"Ho Yi"[au]` answers 14 citations and is Ho, Y. I.
- The second token is not written in capitals. An initials block is, and surnames of one or two letters exist: `S DMTS`, `A LK`, `N AK` and `T T` are the surnames `S`, `A`, `N` and `T` with their initials after them, already in NLM's order. A length test cannot separate those from `Sikorska`; the capitals can.

**A guard, not a repair.** What reaches this function is `ED`, the abbreviated editor tag, which `_parse_au_fallback` parses. The sighting above is on the author tags, and the sample counts `FAU` and `AU` values, not editor ones, so no `ED` value is known to carry the shape. The guard is kept because MEDLINE's editor tags follow its author tags' convention, which makes the shape possible there, and because the two conditions keep the guard from becoming a defect of its own. On the author side the question does not arise: a `FAU` without a comma is read as the surname slot verbatim, as the section above describes.

## Zero-padded article numbers

**What happens.** Journals that number articles instead of paginating them deposit the number with leading zeros. *Environmental Health Perspectives* records `027004`; the citing entry carries `27004`.

**Detection.** `normalize.first_page` strips leading zeros after an optional letter prefix, so both forms compare equal.

## Page locators nothing can read as a number

**What happens.** `normalize.first_page` reads an optional letter, any zero padding and the digits. Two live shapes are neither: SAGE paginates its online-only articles `NP580-NP599`, and front matter runs `i-xv`. If an unreadable locator yielded `""`, it would compare equal to every other unreadable locator, and a bibliography storing `NP585-NP599` against a record holding `NP580-NP599` would come back `OK`.

That is the same collapse *404 is a fact, a timeout is ignorance* names elsewhere, applied to the pages field: an inability to read a value rendered as the values agreeing.

**Observed.** 24 of 3,250 MEDLINE `PG` values (0.74%) in a live sample of 3,500 citations drawn from five windows spanning 1992-2026: 22 `NP…`, `i-xv` on PMID 38284210, and `suppl 4 p.` on PMID 10118706.

**Reported as.** Nothing where the openings agree; `pages/mismatch` where they do not. This is a normalisation rule, not a suppression.

**Detection.** `normalize.first_page` falls back, for a locator its pattern cannot read, to the folded text ahead of the range separator. That text is still the opening locator, which is what the function returns.

**Only the opening, on this path as on the other.** MEDLINE writes `PG - NP2661-76` where Crossref deposits `NP2661-NP2676` for 10.1177/1010539511421194, and both open at `np2661`. Of the 24 values above, the 21 whose publisher deposited a page agree with MEDLINE on the opening locator, character for character, so on the shape it was measured against the rule reports nothing new, while a differing opening does fire.

**What is left, and where it is guarded.** A value with no alphanumeric character still yields nothing, because there is nothing in it to read. `compare._check_pages` therefore accepts an empty opening only against the identical text: two such values are not evidence of a match, and reporting one against itself would be a finding about nothing.

## A volume or issue number with a leading zero

**What happens.** One registry writes the number padded and the other does not. Crossref deposits `"issue": "05"` for `10.1055/a-2760-7307` (*Clinics in Colon and Rectal Surgery*); MEDLINE writes `IP - 5` on the same work, PMID 42553907. A bibliography exported from either carries that registry's spelling, and on the PMID path the other spelling is the only value to compare against, so without this rule a correct entry reports `issue/mismatch` and the run exits 1.

**Observed.** `10.1055/a-2760-7307`, both records fetched live 2026-08-09. The opposite pairing has the same shape and is accepted the same way.

**Reported as.** `one side writes the number with a leading zero`. It blames nobody, because neither side is wrong: `05` and `5` are one issue, as an article number and a page range are one article in the section below.

**Detection.** `benign._number_zero_padded`, on `volume` and `issue` only, and only where both sides are ASCII digits differing by leading zeros alone.

**What it does not cover.** `Volume 18` against `18`: five entries in the same live sample, all from one publisher's own rendering of its volume line. That is a label a reference manager copied into a numeric field. The entry is wrong about what the field holds, and the user can fix it, so suppressing it would hide a defect the tool exists to report.

## Article number recorded against a page range

**What happens.** One side records `e0123456` or `693933`, the other a span. Both identify the same item; the journal changed how it deposits.

**Detection.** `benign._pages_article_number`: when exactly one side looks like an article number, the difference is an encoding choice, not a defect.

`normalize.is_article_number` decides what looks like one. An all-numeric value needs six digits. `2461` is the opening page of `10.1001/archinte.167.22.2461` and nothing about it says article number, so a four-digit floor would excuse a stored `2461` against a registry `2450-2455` as "one article in two notations" when it is a plain page disagreement. A value with a non-digit prefix has a four-character floor, so `e0123456` and `A102` are article numbers and `e12` and `A1` are not. The prefix is good evidence but not proof: an `e12` is as easily a mistyped page as a numbered article, and the floor keeps the difference reported. That is the safe direction, because a page disagreement nobody was told about is the loss this threshold exists to prevent.

**Both floors count the value as the source writes it**, zero padding included, because the padding is evidence in itself: no journal files a page number with leading zeros, so a padded numeric is fixed-width notation and not a page. Counting `normalize.first_page`'s output instead counts only the digits, and `085001` and `85001` both have five of those, so neither would count as an article number and the suppression would fire for neither. One 1,923-entry sweep found three correct entries that fail the build under that counting: PMIDs 42571480, 42571556 and 42571506 (*J Biomed Opt*), `PG` `085001`/`086003`/`086004` against a Crossref `page` of `1-15`/`1-16`/`1-37`, all four values re-fetched from `efetch` and `api.crossref.org` on 2026-08-09. `027004`, the literal cited in `first_page`'s own docstring, has the same shape. Padding is counted, never waived: `0246` is four characters and still not an article number. On the prefixed floor the same counting has **no witnessed instance**, since the journals that pad write a bare number and `e0123456` clears four characters either way. It is applied to both floors because two floors counting differently would be a second rule nobody could state.

**The residuals.** There are two, and they run in opposite directions.

*It reads notation, not the article.* The same article number written without its padding is five characters, stays under the floor, and is reported against a page range. There is no witnessed instance: a bare five-character numeric appears in none of 4,548 MEDLINE `PG` values or 258 bare-numeric Crossref `page` values, in a fresh 4,800-citation sample drawn from six windows spanning 1992-2026 and fetched 2026-08-09. Every bare numeric in it is one to four characters or six to seven. The four-character ones are the shape the floor exists to keep reported, `2461` among them, and lowering the floor to five would excuse them all to rescue a shape nothing has been seen writing.

*One side decides for both.* Once either value looks like an article number, the other is not examined, so any span on the other side is excused, including one naming a different article. The rule has no way to check a number against a range. Of the 3,416 works in that sample whose MEDLINE and Crossref records both carry a page, the two registries wrote the same notation for every one and the suppression fired on none. The exposure is the entries where they differ, of which *J Biomed Opt* above is the witnessed case.

## Doubled tokens from mangled MathML

**NO WITNESSED INSTANCE. Candidate for deletion.**

**What it claims to happen.** A title containing mathematical notation is deposited through a broken MathML conversion and an operator token is repeated, producing fragments like `do(x)do(x)` in the registry's title.

**Observed.** Nothing. The search covered all 438 corpus entries and 17,144 Crossref titles from journals where notation in a title is routine: *Journal of Causal Inference*, *Biometrika*, *Statistics in Medicine*, *Biometrics*, *PLOS ONE*. The pattern matched none of them.

Doubling from mangled markup does occur, and this rule does not catch it. `10.1002/(SICI)1097-0258(19980730)17:14<1601::AID-SIM870>3.0.CO;2-2` is deposited as "…uterine receptivity inin vitro fertilization", the word doubled across a lost `<i>`, and `benign._MATHML_DOUBLING` does not match that shape. So the rule has no instance of its own shape and misses the one instance of doubling that exists.

**Detection.** `benign._title_mathml` removes the doubling and re-compares. Only an exact match after repair is accepted.

**What to do with it.** Replace it with a rule written against a recorded response, or delete it. Widening it to collapse any immediately repeated token would suppress far more than one deposit defect, with no evidence behind it.

## Bracketed parent title on comments and replies

**NO WITNESSED INSTANCE of that description. Candidate for narrowing or deletion.**

**What it claims to happen.** A comment, reply or erratum is registered with the parent article's title in square brackets ahead of its own. The bibliography carries only the comment's title, correctly.

**Observed.** Not that shape. The search covered all 438 corpus entries against both Crossref and PubMed, and PubMed's `comment[pt]`, `"published erratum"[pt]` and author-reply title searches. No registry title was a parent article's title in brackets followed by the item's own.

The search did find a bracketed label: PMID 42535368, `10.3892/mmr.2026.13976`, titled "[Corrigendum] Identification of key differentially expressed genes associated with non-small cell lung cancer by bioinformatics analyses".

**Detection.** `benign._title_bracketed_parent` strips a leading bracketed span of ten characters or more and re-compares. That is wider than the description above, and the difference changes the outcome: the span on PMID 42535368 is eleven characters, so the rule strips `[Corrigendum]` and then accepts an entry storing the parent article's title against the corrigendum's DOI. That would suppress what is arguably a real citation error, the opposite of what this file is for.

**What it gets right.** A wholly bracketed translated title is left alone, because stripping it leaves nothing to compare: `10.1016/j.medcli.2012.01.020` is registered as "[SIDIAP database: electronic clinical records in primary care as a source of information for epidemiologic research]" for a Spanish-language article. That part is worth keeping. The rest of the rule needs an instance or should be deleted.

## Shortened registry titles

**What happens.** The registry holds a truncated title where the bibliography holds the full one: Rubin 1986 is registered as bare `Comment`.

**Detection.** `benign._title_shortened` accepts the difference only when the registry title is a leading fragment of the stored one, ending on a word boundary. A registry title contained anywhere in the stored one is not the shape of a dropped subtitle but of a citation pointing at the work it responds to. A stored `Corrigendum to 'Shift work and colorectal cancer risk in the MCC-Spain case-control study' [...]` against the original paper's DOI (`10.5271/sjweh.3626`) contains the registry title, and it is a real citation error. The reverse direction is not accepted either: an entry missing its own subtitle is incomplete.

## A journal's own acronym written ahead of its name

**What happens.** Several publishers set the masthead as the acronym, a colon, then the title (`JNCI: Journal of the National Cancer Institute`), and a reference manager copies it whole into `journal`. NLM never files a serial that way, so the stored name carries a token the registry's lacks.

**Observed.** PMID 42550479, `10.1093/jnci/djag268`, `JT - Journal of the National Cancer Institute` with `TA - J Natl Cancer Inst`, in `tests/data/pubmed_acronym_prefix.txt`; and PMID 42522049, `10.1002/jac5.70263`, `JT - Journal of the American College of Clinical Pharmacy : JACCP` against a stored `JACCP: JOURNAL OF THE AMERICAN COLLEGE OF CLINICAL PHARMACY`, in `pubmed_acronym_prefix_subtitle.txt`. Four entries in a 386-entry live sample take this shape: PMIDs 42550479, 42544784 and 42528271, all *JNCI*, and 42522049.

Two entries from the same sample look similar and have a different shape. PMID 42550905 stores *Proceedings of the National Academy of Sciences* against a `JT` that continues `of the United States of America`; PMID 42546077 stores *Revista da Escola de Enfermagem da USP* against `JT - Revista da Escola de Enfermagem da U S P`. Neither has an acronym before a colon, the rule reaches neither, and both still report `container/mismatch`. `benign._container_abbreviation` does not reach them either: it needs the stored tokens to be in-order prefixes of the registry's, and the acronym is a token the registry's name does not contain.

**Reported as.** `stored name prefixes the journal's own acronym`, followed, where the registry's value had to be reduced to reach the match, by the clause that says so: `, and the registry adds its own subtitle` or `, and the registry adds a parenthetical qualifier`. A comparison can make two edits, and the sentence names both: PMID 42522049 loses an acronym from the stored side and ` : JACCP` from the registry side.

**Detection.** `benign._container_acronym_prefix`. The prefix must be two to ten characters with no lowercase letter, because a lowercase word before a colon is a title's own opening clause, and `Circulation: Cardiovascular Quality and Outcomes` is a different journal from *Circulation*. Its letters must also be word-initials of what follows, in order. That is what makes removing it lose no information: `JNCI` is derivable from *Journal of the National Cancer Institute* and cannot stand for another journal, while `NEJM:` before the same name fails on `E`.

The rule tests for a subsequence of word-initials instead of reducing the name by a list of skippable words, because which words an acronym skips is a fact about a language, not about a title: `JNCI` skips *of* and *the*, and a Portuguese or German serial skips different ones. A stop-word vocabulary would put one language's function words in the verdict path.

What remains must equal, outright, a name the record itself carries: `JT`, a `container_alternates` entry, the text before NLM's spaced colon, or the text before a trailing parenthetical. A prefix or a substring is never enough. `CE: Cancer Epidemiology`, whose prefix is an initialism of what follows it, leaves *Cancer Epidemiology*, which differs from *Cancer Epidemiology, Biomarkers & Prevention* by a word and is no name that record carries, so the difference still fires. `CEBP:` before the same two words never reaches that test, because it fails the initialism check on `B`. A leading article is never removed from the registry side here: that would be a third edit in one comparison, and no reason this rule prints claims it.

## Aggregator DOIs that redirect

**What happens.** `doi.org` content negotiation redirects a JSTOR DOI (`10.2307/2669548`) to the publisher's own DOI. The work is the same; the registrant differs.

**Detection.** `compare._check_doi` calls `benign._doi_redirecting_prefix`, which recognises the prefix, and the difference is printed as a `doi/alias` issue at `info` severity: a note, never a mismatch, and never able to fail a build. The DOI is the lookup key. The record is here because the stored DOI resolved to it, so the registry disagreeing with itself is not evidence against the bibliography, and for that reason `doi` is absent from `compare.CHECKED_FIELDS`.

`compare._check_doi` is the only caller that passes `field="doi"` to `benign.classify`; without `_check_doi` this rule would never run.

## One DOI, more than one PubMed citation

**A precaution, not a recorded defect.** Nothing has been observed doing this; the `Observed` line below says what was looked for.

**What happens.** `esummary` answers a queried DOI with two records that both carry it among their own `articleids`. Either PubMed holds two citations for the work, or one record lists another's identifier as its own. The DOI filter in `PubMed._pmids_by_doi` already drops candidates of the second kind; what remains is the form where both records claim the DOI that was asked for.

**Observed.** Nothing. Every DOI checked against `"<doi>"[aid]` when this section was written came back with exactly one PMID, including `10.1093/ije/dyx269` (Molina-Montes et al., *Int J Epidemiol* 2018, PMID 29329392), the entry the comparison tests are built on. The section exists because `esummary`'s response can represent the ambiguity and because `compare._check_pmid` is the first thing that would turn an arbitrary choice into an accusation; no defective payload was recorded. Anyone who finds one should record it here.

**Reported as.** Nothing. This is a refusal, not a suppression: no `REGISTRY-ARTIFACT` line is printed, because there is no difference to explain. The comparison never runs.

**Detection.** In `registries/pubmed.py`, not in `benign.py`. `PubMed._pmids_by_doi` keeps every PMID a DOI came back under, `by_dois` fetches all of them, and the record it hands on carries no `Record.pmid` whenever there was more than one. The DOI still resolves: two citations of one work carry the same title, byline and year, so which of them supplies those is arbitrary, and only the one comparison that had no basis is lost.

Post-publication status is not arbitrary, so it is read from every citation, not only from the one that supplied the fields. Fetching only one of the two would take retraction status from whichever number sorted last, so a work PubMed records as retracted under one citation and not the other would report clean. That is the worst miss available here, and it defeats the reason PubMed is consulted. `_merged_citation` carries the `PT` retraction flag across, and MEDLINE's `ECI` cross-reference with it, because a concern is filed on the citation it was raised against, and reading it from one record would leave whether it is reported at all to `efetch`'s ordering. The tie breaks towards the finding, on the rule `crossref._reciprocal_updates` states: naming a status a second citation does not carry costs a line a reader can check, and missing one puts a retracted paper in a manuscript.

**The fields do not cross over, and this section depends on that.** Handing the retracted citation on whole would resolve the second case above, one record listing another work's identifier as its own, by adopting that work's record, chosen because it carries the accusation, and would give the entry that work's title, byline, container and year. Carrying the status alone is the one deliberate asymmetry: in that residual case a status this DOI's other citation does not carry becomes a line a reader can check against two named PMIDs, and dropping it would put a retracted paper in a manuscript. Ignorance about retraction may never render as a clean bill of health; nothing else in a citation is carried across this way.

**Why it matters.** When a work has two PubMed citations, either number is correct to store. Keeping the arbitrary one would fail a bibliography for a choice the tool made, on a check whose purpose is to catch a citation whose two identifiers name two different works.

`compare._check_pmid`'s other two refusals are not registry defects either, and each is stated where it lives. A reference with no DOI is resolved by its PMID and so is never compared against it. And the check reads no relation (Crossref's `updated-by`, MEDLINE's `RIN`/`ROF`), so an entry citing a retraction notice by that notice's own DOI and PMID passes without comment, correctly. A project that meets a case none of this covers adjudicates it in its own `.bibaudit.toml`, where the claim reads as somebody's decision and can be re-read.

## A record's PMC accession stored as its PMID

**What happens.** NLM issues a PMID and a PubMed Central accession for one deposited article, and MEDLINE carries both on the same record. A `pmid` field ends up holding the accession with its `PMC` prefix dropped, and the two numbers then disagree while naming one citation.

**Observed.** PMID 28520842 (*Am J Epidemiol* 2017, 10.1093/aje/kwx137). `efetch id=28520842` returns a block containing `PMID- 28520842` and `PMC  - PMC5860629`; Zotero writes the pair on adjacent lines of one `Extra` box, which is where a copy with the prefix stripped comes from. What is recorded here is the registry record, which anyone can fetch, not a bibliography that made the mistake.

**Detection.** `compare._check_pmid` calls `benign._pmid_pmc_accession` and hands `classify` the record the registry PMID came from, not `ctx.primary`, because MEDLINE's `PMC` line exists only on PubMed's record and Crossref's would explain nothing. `normalize.pmc_number` owns the parsing, and it requires the prefix: a PMC accession and a PMID are both bare ascending integers, so a rule that accepted a bare number would read every PMID as an accession.

**Reported as.** `REGISTRY-ARTIFACT`, with both numbers printed, under the name of the registry that carries them. That is PubMed on every entry reaching this check: only PubMed sets `Record.pmid`, so the primary registry of an entry resolved by DOI supplied neither number and has no `PMC` line for a reader to check the suppression against.

**Why it matters.** The check claims that the entry's two identifiers name two citations. A number that names the record in hand is the one case where that claim is false by construction.

## The PMID check is one-sided, and warns rather than fails

**No defect here, in a registry or anywhere else.** This section records why a comparison carries the severity it does, and it is in this file because [why field-level](why.md) sends a reader looking for that.

**What happens.** Nothing looks up the stored PMID. The record in hand came back under the stored DOI, so "these two identifiers name two works" is an inference from one lookup, not a finding from two. The case that decides the severity is a bibliography carrying, beside the right DOI, a PMID that has since stopped answering: `efetch` for 20000157 or 35000082 returns an empty body today. Whether such a number was the work's own when the entry was written cannot be seen from this side, because what came back is the citation the DOI resolves to, and it says nothing about a number nobody sent to PubMed.

**Reported as.** A `pmid/mismatch` issue at `warning` severity, so the entry's verdict is `INCOMPLETE` and the run's exit code is 0. This has two costs. The `INCOMPLETE` group heading reads "the registry holds fields the entry omits", which this is not; the issue line beside it names both numbers and says the stored one was not looked up. And the default report prints the failing groups plus `DISPUTED` and nothing else, so on a default run the finding is visible only with `--verbose`. A project that wants the finding to fail the run can use `--fail-on INCOMPLETE`: naming a verdict there prints its group as well as failing on it, so the run that exits 1 is the run that names the entry.

**What would justify `error`.** Looking up the stored PMID, and adjudicating what comes back. `PubMed.by_pmids` already answers for a batch of numbers, so the lookup costs a second efetch per fifty entries carrying both identifiers. A citation describing another work is then a two-sided finding, a citation carrying the stored DOI is PubMed holding the work twice, and no citation at all is a stored number naming nothing. Until the tool does that, the severity states what the evidence supports.

## An identifier a registry answered *around*

**A precaution, not a recorded defect.** Nothing has been observed doing this; the `Observed` line below says what was looked for.

**What happens.** `efetch` returns a MEDLINE record whose own `PMID` line is not the number requested. `PubMed.by_pmids` declines to adopt it, because that record's metadata and retraction status belong to another paper, and the requested number is then left with no record.

**Observed.** Nothing, for the substitution itself. `efetch` for the deleted PMIDs 20000157 and 35000082 answers HTTP 200 with an empty body: no error, no redirect, no surviving record put in its place. `id=99999999`, far above the highest number NLM has assigned, answers identically, so an absence and a withdrawal give the same response, and neither is a substitution. The guard stays because adopting another paper's citation is unrecoverable while declining one costs a lookup, but it is a precaution and is written up as one.

**Reported as.** `UNCHECKED`, with an `identifier/inconclusive` issue naming what came back instead. Never `BAD-ID`, which rests on a narrower fact than the phrase "no such record" suggests: `efetch` returns the citations it holds and says nothing about the rest, so the evidence is an omission (the empty body above, reaching `compare` as a missing key). As the three probes above show, that omission covers a number NLM never assigned and a deleted citation alike. A failure of the whole batch, where `efetch` answers 404 for the request instead of 200 for its contents, is also reported as `UNCHECKED`, for the same reason and per batch, because read as absence it would condemn fifty entries at once.

**Detection.** In `registries/pubmed.py` and `compare.compare`, not in `benign.py`. `PubMed.by_pmids` returns a `PmidAnswers` whose two dicts keep "answered with this record" and "answered with something else" apart, `resolve` carries the second to `audit`, and `compare`'s `inconclusive` argument stops the `BAD-ID` branch.

**Why it matters.** This is CLAUDE.md's "404 is a fact, a timeout is ignorance" at a boundary with a third state. An answer about another record is not an answer about this identifier, and only one of the three states is evidence about a bibliography.

## Deposit timestamps recorded as publication years

**NO WITNESSED INSTANCE. Candidate for deletion.**

**What it claims to happen.** A working-paper series re-deposits an old item and the `issued` date becomes the deposit date: a 2020 paper carrying 2026.

**Observed.** Nothing, and the named series does the opposite. The search covered all 438 corpus entries, of which four disagree with Crossref on the year and all four run the other way, and NBER, the working-paper series the description points at. NBER's `issued` dates are correct: `10.3386/w0001` carries `issued` 1973-06 and keeps its 2007-10-23 deposit stamp in `created`, a field this tool never reads. Crossref keeps deposit timestamps in `created`, and that field does not reach `Record.years`.

The opposite direction is a real, repeatable false positive, and nothing here covers it. `10.1136/gutjnl-2019-319990` is an online-first BMJ-group paper with `online` 2020, `issued` 2020 and no print date, while the entry correctly cites the 2021 issue year; three more corpus entries do the same (`10.1093/aje/kwj364`, `10.1093/aje/kwm361`, `10.1007/s10549-007-9523-x`). Whoever replaces this rule should write that one instead, and will need a defensible bound on the gap, because "the entry's year is later than anything the registry knows" is also what a wrong year looks like.

**Detection.** `benign._year_deposit_artifact` applies only when there is no print date to corroborate the registry's year and the registry year is at least three years later than the stored one (`benign._MIN_DEPOSIT_STAMP_GAP`). A series re-depositing an old item lands many years out; a gap of one or two years is what citing a preprint's year, or mistyping the last digit, looks like, and both of those are reported. A registry year earlier than the stored year is not covered either, because that is a real discrepancy for a person to look at.

**Crossref records only.** "No print date" is a fact about the work only where the registry has a print date to report and did not, and Crossref is the only registry that does. `crossref._years` writes `print`, `online` and `issued`. MEDLINE has no print field (`DP` is the issue a citation is filed under, and it reaches `Record.years` as `issued`), and DataCite, Open Library and the search clients write `issued` alone. If a missing print key counted as an absent print date, nothing would stop the guard firing outside Crossref, and every entry resolved by its PMID whose year is three or more years early would be filed here instead of reported. A missing key that means "this registry does not record that" is ignorance, not a fact.

## Online-first versus print year

**What happens.** A work posted online in one year and printed in the next has two correct years, and different tools cite different ones.

**Handling.** Not a defect: `compare._check_year` accepts any year the registry itself carries, and the verdict stays `OK`. `benign._year_online_first` exists only so the report can state which date the entry cites. `compare._note_alternate_date` reaches it and reports it as a `year/alternate-date` issue at `info` severity, not as a `REGISTRY-ARTIFACT`. Neither value is defective, and labelling one so would relabel a large share of an ordinary epidemiology bibliography, where online-first is the norm.

`info` is filtered out of the default terminal report, so this reaches a reader through `--verbose` or the JSON report's `issues` list.

**Both registries carry both years.** Crossref deposits `published-print` beside `published-online`; MEDLINE files `DP`, the issue the citation belongs to, beside `DEP`, the day the work went online. Both pairs reach `Record.years`, so a reference resolved by its PMID gets the same latitude as one resolved by its DOI. That matters more on the PMID path, because PubMed is then the only registry consulted and no second source can supply the other date. `Record.year`, the single value a report prints when it needs one, prefers the issue.

## Container titles: a chapter has two

**What happens.** Crossref's `container-title` is an array, and for a book chapter it holds both the series and the volume. An entry citing either is right, and keeping only element 0 discards the one that matches.

**Observed.** `10.1007/978-1-59745-423-0_7` (Hainaut et al., stored as `hainaut2011biobank`) deposits `["Methods in Molecular Biology", "Methods in Biobanking"]`. The `@inbook`'s `booktitle` is the second element, character for character. Compared against element 0 alone, it produced the live corpus run's only `DISPUTED` verdict, reported as Crossref and PubMed disagreeing about a title Crossref itself supplies. Recorded in `tests/data/audit_crossref_series_and_book_container.json`.

**Handling.** Not a suppression: `Record.container_alternates` carries the rest of the array, and `compare._check_scalar` accepts a stored value matching any of them, on the same principle as "any year the registry itself carries is acceptable". A `container/alternate-title` note at `info` severity states which one matched, because the reader is looking at a landing page headed with the other title. A container matching neither is still reported.

## Journal names versus ISO abbreviations

**What happens.** `Int J Cancer` versus `International Journal of Cancer`.

**Detection.** `benign._container_abbreviation` accepts the difference when Crossref's own `short-container-title` matches, or when every abbreviated token is an in-order prefix of a token in the full name, the last of them lands on the full name's last token, and no word longer than three characters was passed over on the way.

There are two conditions because each bounds one direction, and the test against sibling journals rests on both. The end anchor bounds the prefix direction: `Nature` is not an abbreviation of `Nature Genetics`, nor `The Lancet` of `The Lancet Oncology`, and citing the parent title for a paper that appeared in the offshoot is a real error the tool must still report. The skip bound limits the other direction. Without it, any name whose words are a subsequence of a longer name ending on the same last word would be cleared: `Annals of Oncology` against `Annals of surgical oncology`, `Journal of Cancer` against `Journal of gastrointestinal cancer`, `Cancer` against `Pediatric blood & cancer`, all pairs of serials NLM lists separately. Over the 37,989 serials in the 2026-08-09 fetch of `J_Medline.txt`, the unbounded rule accepts 27,851 ordered pairs of serials with different titles as abbreviations of one another; the bounded rule accepts 903. `REGISTRY-ARTIFACT` is not a failing verdict, so each of those pairs, when cleared, prints an exoneration on a wrong bibliography that exits 0.

!!! note "Every serial-list figure on this page is that one fetch"

    NLM rebuilds [`J_Medline.txt`](https://ftp.ncbi.nlm.nih.gov/pubmed/J_Medline.txt) daily (on 2026-08-10 its `Last-Modified` was that morning), so re-running any count below gives a slightly different answer, and the difference is the catalogue changing, not the rule. The 2026-08-10 file holds 37,990 records, 37,989 of them carrying a `MedAbbr`, which is what "37,989 serials" counts here and in `benign.py`. Where a figure names a date, that is the fetch it was measured on; where it does not, it is this one.

**What the residue is.** The two bounds narrow the rule without closing it, and a section reporting only the count leaves a reader unable to tell whether their own journal is among the 903. Two shapes account for most of them, both measured over the same 37,989-serial list:

* A short word in front of the whole stored name. The registry's name is one or more words of three characters or fewer followed by the stored name, token for token: 240 ordered pairs and 186 distinct stored names, counted on the same basis as the 903 above, which is after `_container_leading_article` has taken, under its own reason, the 15 pairs differing only by `The`/`A`/`An`. The skip bound passes over words of three characters or fewer, and a publisher's imprint is usually one: `Materials letters` is cleared against `ACS materials letters`, `Archives of dermatology` against `A.M.A. archives of dermatology`, `Precision oncology` against `AI in precision oncology`, `Clinics` against `PET clinics`, `Cancer` against `BMC cancer`. Every pair is two separate serials in NLM's own list.
* A name that is the start of a longer one. The final-token test is a prefix match, so a one-token title is cleared against any longer one-token title beginning with it: 373 ordered pairs, including `BioMedicine` against `Biomedicines`, `Biofilm` against `Biofilms`, `Cancer` against `Cancers`, `Bios` against `Bioscience`, and the journal `C` against `Cairo`.

Tightening the two existing bounds closes neither, and the obvious narrowings have costs. Requiring the stored name's first token to match the registry's first, which would refuse the first shape, refuses 2,226 of the 27,334 real `MedAbbr` → `JournalTitle` pairs in that list, because NLM keeps a leading article on the full title and the abbreviation drops it (`Der Anaesthesist`, `L'Annee biologique`, 289 of them in a language whose article is not `the`/`a`/`an`). Requiring the last stored token to be shorter than the registry's by at least two characters, which would refuse the plural half of the second shape, costs 75 of the same 27,334 (`Rev Med Chil` against `Revista medica de Chile`, `Contin Chang` against `Continuity and change`). Both would change the verdict path, so both are the maintainer's decision; this section records what the rule clears now.

**Why a length and not a word list.** ISO 4 shortens each significant word of a title and deletes the articles, conjunctions and prepositions between them, so the words a real abbreviation passes over are grammatical words: 25,758 of the 26,500 words skipped across the 25,641 abbreviated titles in NLM's own list are three characters or fewer (*of*, *and*, *the*, *in*, *de*, *on*, *for*, *la*, *et*, *für*, *di*, *und*). Which words those are is a fact about a language, and a stop-word vocabulary would put one language's function words in the verdict path; a length admits every language on the same terms. `_is_initialism_of` rests on the same argument.

**What it costs.** This rule does not accept 432 of the 25,641 `MedAbbr`-against-`JournalTitle` pairs in NLM's list: the Italian *della*, the Dutch *voor*, the English *with* and *from*, and the `Part E` / `Series B` sub-series titles. Most of them never reach this rule, because `TA` is on `container_alternates` and `compare._check_scalar` accepts a stored value matching one of those before any suppression is consulted. The pairs the bound refuses are the confusions between related journals that the anchor exists to catch: `Advances in biology` against `Advances in cell biology`, `Advances in research` against `Advances in drug research`.

A dropped leading article (`Lancet` for `The Lancet`) belongs to `_container_leading_article`, which runs first and says so in its reason.

## Journal names as MEDLINE files them

**What happens.** NLM does not record a journal under the name on its masthead. It drops the leading article; it appends a place-of-publication qualifier in parentheses wherever the bare title would be ambiguous; and it writes a subtitle after a spaced colon. MEDLINE's `JT` is therefore `Lancet (London, England)`, `BMJ (Clinical research ed.)`, `Science (New York, N.Y.)`, `Cancer epidemiology, biomarkers & prevention : a publication of the American Association for Cancer Research, cosponsored by the American Society of Preventive Oncology`, while `TA` holds the abbreviation, which for the first three is the journal's plain name and for the fourth is `Cancer Epidemiol Biomarkers Prev`. A bibliography stores none of these: it stores `The Lancet`.

On many journals the subtitle is the sponsoring society, but not on all. `Archives of medical science : AMS` (PMID 42540560) and `The Malaysian journal of medical sciences : MJMS` (PMID 42534732) carry the title's own acronym; `The British journal of psychiatry : the journal of mental science` (PMID 42552687) carries a descriptive phrase naming no organisation. The rule below reads NLM's separator, not what follows it, so it suppresses all three and says *subtitle*, not *society*, in the reason it prints.

**Observed.** PMID 9500320, Wakefield et al. 1998, recorded verbatim in `tests/data/compare_pubmed_wakefield_retracted.txt` (`TA - Lancet`, `JT - Lancet (London, England)`), and PMID 32430337, Michaud et al., `10.1158/1055-9965.EPI-20-0378`, in `tests/data/pubmed_society_expansion.txt`. The subtitle is on 26 of the 173 journals in a 300-record sample of live `efetch` output, *J Clin Oncol*, *Clin Cancer Res*, *Ann Oncol*, *Toxicol Sci* and *Am J Transplant* among them. It matters most on the PMID path, where PubMed is the only registry consulted and its `JT` is the only container value: without these rules, every correct Lancet, BMJ, Science or Cancer Epidemiology, Biomarkers & Prevention entry resolved by PMID is a `FIELD-MISMATCH`, and the run exits 1.

**Handling, in three places.** A fourth, for the parenthetical qualifier, is the section after this one.

Every container rule reads `JT`, `TA` and `container_short` from the record the compared value came from: the three below, the qualifier rule after them, and `_container_abbreviation` above. `compare._check_scalar` takes the container from the primary registry, or from the corroborator where the primary deposited none, and hands `benign.classify` that same record. On an entry resolved by DOI, that is PubMed's record whenever Crossref deposited no `container-title`, and judging the value against Crossref's record would ask Crossref to account for a MEDLINE filing title, which it cannot. The `container/alternate-title` note below does not go through that rule and does not need to: it offers every alternate that either record carries, each printed with the registry that holds it.

`registries/pubmed._record_from_medline` puts `TA` on the record's `container_alternates` as well as its `container_short`. Both are titles PubMed itself gives for the same journal, so this is the chapter case above by another route, not a suppression: an entry storing `Lancet` gets the same `container/alternate-title` note at `info` severity, naming PubMed as the registry that carries it.

A parallel title joins the same list on the same argument. NLM writes one serial's two names into `JT` separated by a spaced equals sign (`Journal of preventive medicine and public health = Yebang Uihakhoe chi`, PMID 42526877, `tests/data/pubmed_parallel_title.txt`; `Zhongguo yao li xue bao = Acta pharmacologica Sinica`, NlmId 8100330), and 607 of the 37,989 serials in NLM's own list carry at least one. Both halves are the journal's own name and a bibliography stores whichever its house style uses, so each is offered as an alternate instead of replacing `JT`. That keeps the 34 serials whose half equals some other serial's whole title (38 once `fold` has run) from merging with that serial: `Dong wu xue yan jiu = Zoological research` beside `Zoological research`, `Noshuyo byori = Brain tumor pathology` beside `Brain tumor pathology`, `Neirofiziologiia = Neurophysiology` beside `Neurophysiology`. The entry has to match a name, and `JT` stays what the record holds.

`benign._container_leading_article` covers the entry that stores the masthead name. It accepts a stored value whose opening `The`/`A`/`An`, once removed, leaves exactly `JT` or exactly one of the titles on `container_alternates`, and it accepts the mirror: a `JT` or an alternate whose own opening article, once removed, leaves exactly the stored name. One article comes off one side per comparison, never both, because that is what the printed reason claims happened and the two directions print different reasons. So `A Journal of Cancer` against `The Journal of Cancer` is a difference and not an artifact. The article has to be the opening word: `Journal of the National Cancer Institute` against `Journal of National Cancer Institute` still fires.

NLM's parallel titles arrive through the registry direction. `JT - The Canadian journal of statistics = Revue canadienne de statistique` (PMID 42559441, `tests/data/pubmed_parallel_title_article.txt`) puts each half on `container_alternates` verbatim, article included, while Crossref deposits the journal as *Canadian Journal of Statistics*, the spelling a bibliography exports. 63 of the 607 parallel titles in NLM's own list open a half with an article, and this branch reaches 62 of them: remove the registry direction and an entry storing the article-free masthead form of those 62 reports `container/mismatch`, where with it none does. It also reaches a whole `JT` filed with an article: `The Alaska nurse`.

`_LEADING_ARTICLE` is English only: `the`, `a`, `an`. `Der Anaesthesist`, `L'Auxiliaire` and `Die Naturwissenschaften` are filed with an article and abbreviated without one, and this rule does not reach them. Those articles fold to three characters or fewer, so `_container_abbreviation` clears them one rule later and prints `stored name abbreviates the registry name`. That is the sentence this rule runs ahead of `_container_abbreviation` to avoid printing for an English article, and those entries still get it. The verdict is the same either way; the sentence is not. Widening the set would put one language's function words in the verdict path, which is why the skip bound above uses a length.

This rule does not strip the qualifier: it compares whole titles, and the two rules below decide what else may come off the registry's side beyond an article. `The Lancet Oncology` against `Lancet (London, England)` therefore still fires under all three: strip an article from either side and what is left matches no title that record carries, and with the qualifier removed `Lancet` is still not `The Lancet Oncology`.

`_container_leading_article` runs ahead of `_container_abbreviation` in `benign.CHECKS`, and the order decides only which reason is printed. The wider rule accepts the same pairing, because a dropped article is a token subsequence reaching the registry's last token, and calls it `stored name abbreviates the registry name`, which is not true of `Lancet` against `The Lancet`: nothing was abbreviated. The narrower reason is the one a reader can check.

On the `BMJ` record, the two spellings a bibliography uses reach the same outcome by different routes. Against `JT` `BMJ (Clinical research ed.)` with `TA` `BMJ`, a stored `The BMJ` (the masthead) is suppressed here, its article coming off to leave `TA` exactly. A stored `BMJ` never reaches this rule, because it matches `TA` outright and gets the `container/alternate-title` note described above, at `info`, with the entry reported `OK`. Neither is a mismatch, and neither should be.

`benign._container_medline_subtitle` covers the third shape. It compares the stored name against the part of the registry's value before NLM's own spaced colon, and accepts only an exact match there. `Cancer Epidemiology, Biomarkers & Prevention` against the `JT` above is suppressed, while `Cancer Epidemiology`, a different journal published by Elsevier, differs from that base title by a word and still fires. No other rule reaches this case: `TA` is a real abbreviation for these journals, not the plain name, and `_container_abbreviation` requires the stored tokens to reach the end of the registry's name, which is the subtitle and not the journal.

NLM drops a serial's leading article on most titles and keeps it on some, so that rule also removes one opening `The`/`A`/`An` from the registry's base before comparing. `The Journal of adolescent health : official publication of the Society for Adolescent Medicine`, `TA - J Adolesc Health` (PMID 42547188) is the case: the masthead and Crossref both say *Journal of Adolescent Health* with no article, and without stripping the base, that correct entry would report `container/mismatch`. The printed reason says which of the two happened. The stored side is never stripped, so at most one article is dropped in any comparison, and `A Journal of Cancer` against `The Journal of Cancer : …` remains a `container/mismatch`: two journals differing by the word that distinguishes them.

That rule reads the colon and nothing else. NLM writes a spaced colon inside a parenthetical qualifier where the body named there needs a date of its own (`ASAIO journal (American Society for Artificial Internal Organs : 1992)`, PMID 42552576), so the first spaced colon in `JT` is not always the separator. A base left holding an unclosed parenthesis is refused instead of compared, because half a qualifier is not a name. The qualifier itself is the fourth shape, with its own rule below.

## Parenthetical qualifiers in a MEDLINE filing title

**What happens.** NLM appends a qualifier in parentheses wherever a bare title would be ambiguous in its catalogue: a place (`Lancet (London, England)`), an edition (`BMJ (Clinical research ed.)`), a founding year (`Annals of medicine and surgery (2012)`), the issuing body, or several at once. It is on 2,698 of the 37,989 serials in NLM's own list, [`J_Medline.txt`](https://ftp.ncbi.nlm.nih.gov/pubmed/J_Medline.txt). The masthead, Crossref and the bibliography all carry the bare title.

**Observed.** PMID 42555391, `JT - Annals of medicine and surgery (2012)` with `TA - Ann Med Surg (Lond)`, recorded verbatim in `tests/data/pubmed_qualifier_year.txt`; PMID 42552576, `JT - ASAIO journal (American Society for Artificial Internal Organs : 1992)` with `TA - ASAIO J`, in `tests/data/pubmed_qualifier_inner_colon.txt`; and PMID 30151503, `JT - The neurologist (Hyderabad, India)` with `TA - Neurologist (Hyderabad)`, in `tests/data/pubmed_qualifier_leading_article.txt`. Where `TA` is not the journal's plain name, as for all three, no other rule reaches the entry: `_container_abbreviation` needs the stored tokens to reach `JT`'s last token, which is inside the qualifier, and `_container_leading_article` compares against the whole of `JT`. On the PMID path `JT` is the only container value, so an entry citing the journal's own name has nothing else to be compared against, and without the rule below it reports `container/mismatch`.

**Detection.** `benign._container_medline_qualifier` removes one trailing balanced parenthetical from every name the registry's record carries for the container (its `container`, and each entry of `container_alternates`) and requires what is left to equal the stored name outright. A prefix or a substring is never enough: *Annals of Surgery* against `Annals of medicine and surgery (2012)` differs by more than a qualifier and still reports `container/mismatch`, as *Cancer Epidemiology* does against *Cancer Epidemiology, Biomarkers & Prevention* in the section above.

Two things are allowed on the record's primary value and nowhere else: one opening `The`/`A`/`An` may come off its remainder, and the remainder may be a single word. Both are explained below.

**Why the qualifier may be dropped, when in a catalogue it is what tells two serials apart.** That objection concerns looking a journal up, and nothing in this comparison looks one up. By the time `compare` reaches `container`, the work has already been identified, by its DOI or PMID or, for an entry carrying neither, by the title, first author and year that `compare.confirm_without_id` required before adopting any record. A work appears in exactly one serial, so the registry's `JT` is by construction the serial this work appeared in: there is one serial in play and nothing to merge. What the rule accepts is `Lancet` for a work published in `Lancet (London, England)`, which is correct.

**What it costs.** For this to be a miss, a citation would have to name a journal that shares a base title with the right one and be wrong about it. Such pairs are real: `The neurologist` (NlmId 9503763) beside `The neurologist (Hyderabad, India)` (NlmId 101719078), and 316 qualified titles whose base is some other serial's full name. So an entry resolved by PMID 30151503 and naming the journal *The Neurologist* is accepted. That is all of the exposure: it needs the entry to be wrong in the one field the identifier has already settled, and every other field of that entry is still compared against the record.

**NLM writes the qualifier onto the abbreviation too, and the rule follows it there.** `MedAbbr` carries the same parenthetical as `JournalTitle`, and `registries/pubmed._container_alternates` puts it on the record verbatim: 2,695 of the 37,989 serials carry a qualified `MedAbbr`, against 2,698 with a qualified `JournalTitle`, and 1,213 carry one on both. A bibliography storing the abbreviation the way ISO 4, Web of Science and Scopus write it (the journal's own, with no catalogue disambiguator) matches neither the `JT` it is compared against nor the `TA` beside it, and no other rule reaches the pairing, because the stored value abbreviates nothing the record holds whole. Measured through the real `compare()` on a PMID-shaped record built by the shipped client, with the reduction applied to `JT` alone, 1,075 of those 2,695 serials report `container/mismatch` [error]. `Acta Hepatogastroenterol` against `JT - Acta hepato-gastroenterologica` and `TA - Acta Hepatogastroenterol (Stuttg)` (NlmId 0340734) is the shape.

The printed pair is then the stored value beside `JT`, which a qualifier does not relate, so the reason says which name was reduced: `registry appends a parenthetical qualifier to another name it carries for the journal`.

**The floor: two tokens, and what it costs.** On any name but the primary one, the remainder must keep at least two tokens. `JT` is the serial's name in full, and one word of it is still that name (`Lancet` for `Lancet (London, England)`); `TA` is already a reduction, and one word of it is one truncated word. `Proc (Bayl Univ Med Cent)` (NlmId 9302033) reduces to `Proc`, which opens 442 serials' abbreviations in NLM's list, its own among them, and is the whole of none of them.

825 of the 2,695 qualified abbreviations reduce to a single token, and 208 of those are exactly some other serial's whole `MedAbbr`: `Aging (Milano)` (NlmId 9102503) leaves `Aging`, which is NlmId 0050677's entire abbreviation. The floor refuses all 825, at the cost of 67 of the 1,075 entries above: `Rehabilitation` against `TA - Rehabilitation (Bonn)` (NlmId 1302716), `Biochemistry` against `TA - Biochemistry (Mosc)` (NlmId 0376536) and 65 more still report `container/mismatch`. That is all of this rule's residual noise, and it errs in the direction this project chooses.

**What the floor does not remove.** 611 pairs remain where a remainder of two or more tokens is some other serial's whole `MedAbbr`, and this rule clears 600 of them (the other 11 already agree on case or as an alternate title). `Acta Oncol (Madr)` (NlmId 0370346) reduces to `Acta Oncol`, which is the whole `MedAbbr` of *Acta oncologica (Stockholm, Sweden)* (NlmId 8709065); `Acta Ophthalmol (Copenh)` (NlmId 0370347) reduces to `Acta Ophthalmol`, which is *Acta ophthalmologica*'s (NlmId 101468102); `Ann Immunol (Paris)` (NlmId 0353045) reduces to `Ann Immunol`, which is *Annals of immunology*'s (NlmId 7611917). A reader can look up either half of any of them in [`J_Medline.txt`](https://ftp.ncbi.nlm.nih.gov/pubmed/J_Medline.txt).

It is the same exposure as the 316 on the `JournalTitle` side, bounded the same way: reaching it needs an entry to name the other serial for a work whose identifier has already settled which serial it appeared in, and every other field of that entry is still compared against the record.

**No article comes off an abbreviation.** The leading-article branch applies only to the record's primary value. `_LEADING_ARTICLE` matches `An `, and on an abbreviated title that is *Anales* or *Anais*: all 18 qualified `MedAbbr` values whose remainder matches the pattern have that shape (`An Pediatr (Barc)`, NlmId 101162596; `An R Acad Nac Med (Madr)`, NlmId 7505188), so stripping there would compare a stored name against a title with its first word deleted, and clear an entry naming a journal the record does not name.

**Two details of the stripping.** The parenthetical is matched from its closing bracket back to the one that balances it, not by a pattern over its contents, so NLM's inner colon never splits it and a nested qualifier comes off in one piece: `Clinical oncology (Royal College of Radiologists (Great Britain))` (PMID 42546669) is one of forty-nine serials naming a body that itself needs a country. And a remainder still holding an unclosed `(` is refused. Four titles in NLM's list end on a bracket that does not close the qualifier (`Interventional radiology (Higashimatsuyama-shi (Japan)`, NlmId 101745449), and what is left of one of those is a fragment of a name, not a name.

The stored side is never stripped. At most one article is dropped in any comparison, so `A Journal of Cancer` against `The Journal of Cancer (Basel, Switzerland)` stays a `container/mismatch`, and an entry that stores the qualifier itself is compared as written.

**The rule is not limited to serials**, and the reason it prints says *journal* because that is what the container is on all but a handful of entries. Its one scope guard is the field: it runs on any `container`, from any registry, and a book's container is a volume title. Two real containers reach it that way.

`GeneReviews((R))` is MEDLINE's `BTI` on every chapter of it (PMID 20301425, `tests/data/pubmed_book_chapter.txt`), and a bibliography carries `booktitle = {GeneReviews}`. Nothing else covers that pairing: a book record has no `TA`, so `container_alternates` is empty. Crossref's deposit for `10.1088/978-0-7503-3703-8ch5` (IOP, `type: book-chapter`) is `container-title: ["Photography (Second Edition)"]`, and an `@incollection` whose `booktitle` is *Photography* is suppressed against it.

The second case needs care, because a book differs from a serial on the point the argument above depends on: a chapter can appear in two editions of one volume, and those are two works. What the rule accepts there is an entry that omits the parenthetical, on a work whose own identifier has already settled which edition it is. That is incompleteness, stated as a `REGISTRY-ARTIFACT` line instead of passed over. An entry naming the wrong edition still fires: the remainder has to equal the stored name outright, so `Photography (First Edition)` against `Photography (Second Edition)` is a `container/mismatch`, as *Annals of Surgery* is against `Annals of medicine and surgery (2012)`.

## Retraction relations deposited in both directions

**What happens.** Crossref's model makes `update-to` and `updated-by` opposites: a record carrying `update-to: retraction -> X` is the notice that retracted X, and one carrying `updated-by: retraction <- X` was retracted by X. Both cannot be true about the same X and the same type, and Elsevier deposits both.

**Observed.** `10.1016/S0140-6736(20)31324-6`, the Lancet's retraction notice for the Surgisphere hydroxychloroquine paper, carries `update-to: [retraction -> 31180-6, source retraction-watch]` and `updated-by: [retraction <- 31180-6, source publisher]`. Read literally, the notice has itself been retracted, so any manuscript about the scandal that cites the notice would be failed as `RETRACTED`: a false factual claim about a named work, made with the tool's full authority.

**Detection.** `registries/crossref._reciprocal_updates`. The obvious version of this rule clears a retracted paper. The retracted paper `10.1016/S0140-6736(20)31180-6` carries the mirror image (`updated-by: retraction <- 31324-6` and `update-to: retraction -> 31324-6`), so a rule that discounted every reciprocated `updated-by` would clear the retracted paper itself, which is the worst miss this tool can make. What separates the two records is that Retraction Watch recorded the notice side on the notice and the retracted side on the paper. A relation is therefore discounted only when Retraction Watch names this record as the notice and does not also name it as retracted; every other case, including a tie, keeps the finding. `10.29328/journal.jcmhs.1001023` is the tie: its real `partial_retraction` points at itself in both arrays, both with source `publisher`, and it is still reported. All three payloads are recorded under `tests/data/`.

## OpenAlex naming a repository as the venue

Handled in `registries/search.py`, not suppressed. OpenAlex reaches an entry only when the entry carries no identifier. Its `primary_location` is the copy OpenAlex chooses to show, which is not necessarily where the work was published: on 44 of the 239 OpenAlex works in the cache on 2026-11-26 it was a repository. Huber's 1967 paper in the *Proceedings of the Fifth Berkeley Symposium* has "Project Euclid (Cornell University)" there, and Borgan and Liestøl's 1990 note in the *Scandinavian Journal of Statistics* has "NVA (Nasjonalt vitenarkiv)". Two more works name an ebook platform ("Springer eBooks", "Chapman & Hall/CRC eBooks"). Read as the venue, each of these reports a correct entry `container/mismatch` against the name of an archive.

`_openalex_venue` reads the primary location and then every other location OpenAlex lists, and takes the first source whose type is `journal`, `conference` or `book series`. Borgan's journal is OpenAlex's second location, so the entry is compared against it, and a wrong journal still fails. A work that OpenAlex records only in a repository, Huber's among them (27 of the 44), gets no container, so the entry's container is compared against nothing. That is a gap, and the alternative is a false alarm on every such work.

Observed: `tests/data/openalex_repository_venue_journal_elsewhere.json` and `tests/data/openalex_repository_venue_only.json`.

## Registries disagreeing with each other

Not an artifact and not suppressed. When Crossref and PubMed hold different values for the same field, the result is `DISPUTED` and both values are printed. The tool has no basis for choosing between two curated sources, and choosing one would be the same overreach as rewriting the bibliography.

## Open Library

The two entries below are not artifacts, in the same sense as *Online-first versus print year* and *Container titles* above: in neither does the registry hold a wrong value. Open Library is thinner than Crossref (it is crowd-sourced, and some records carry a title and nothing else), and two design decisions in `compare.py` and `registries/openlibrary.py` keep that thinness from reading as a defect in the bibliography. Neither is a `benign.py` suppression, so neither owes the witnessed instance that CLAUDE.md and this file's introduction require of one. What follows documents the design; it does not claim that a specific record was seen to be wrong.

Both are witnessed anyway, because the thinness is the premise the design rests on, and this file exists to stop unmeasured premises. Measured on 2026-08-10 over 2,000 `search.json` documents, 100 each for twenty title queries, 29 (1.45%) carry neither `author_name` nor `first_publish_year`, `/works/OL17582766M` ("Annual report") among them, whose whole document is a title, a key and three availability flags; 210 (10.5%) carry no `author_name`. So about one document in seventy arrives with nothing to corroborate its title against.

**`number_of_pages` is a book's total length, not a citation locator.** Every other registry's `pages` means the opening page of a citation inside a larger work (see *Zero-padded article numbers* and *Article number recorded against a page range* above). For a book there is no larger work, and Open Library's Books API has no such field; the closest it offers is `number_of_pages`, the book's own extent. ISBN 9780201633610, *Design Patterns*, answers `"number_of_pages": 395`, which is how long the book is, not where anything in it starts. `registries/openlibrary.py` maps it into `Record.pages` anyway, because `Record` has no separate extent slot and the alternative is to drop a value Open Library supplies. `compare.py` compensates: `_check_scalar`'s `optional_for_kinds` parameter and the identical guard in `_check_pages` stop a `@book` entry with no `pages` field of its own from picking up a `pages/missing` warning it cannot fix and should not have to. Nearly every `@book` entry lacks one, since BibTeX has no convention for writing a book's total length there. The same guard covers `volume` and `issue`, because an ordinary book has no journal volume or issue to be missing either. A stored `pages`/`volume`/`issue` value that disagrees with the registry's is still reported. Only the case where the entry never stated a value is silenced, and only for `kind == "book"`; a chapter's `pages` is a real page range within its parent book and keeps the ordinary check.

**A thin record cannot confirm a book on title alone.** `confirm_without_id` requires title similarity plus corroboration from the author list or the year before accepting a search candidate for an entry with no identifier; that function's docstring explains why a title-only match is how a plausible but wrong work gets adopted. Each corroboration check is written to skip when the candidate has nothing to compare. Crossref, Europe PMC and OpenAlex candidates reliably carry an author list, a year, or both, so through them the checks never meet a candidate with nothing to compare. Open Library's `search.json` returns exactly that on 1.45% of documents: the 29 in 2,000 counted above, with a title and no `author_name` and no `first_publish_year`. Without an explicit guard, such a record would confirm on the title match alone, with both corroboration checks skipped, which is the failure `confirm_without_id` exists to prevent. `compare.confirm_without_id` therefore refuses a candidate outright when it carries neither authors nor a year, before either corroboration check runs.
