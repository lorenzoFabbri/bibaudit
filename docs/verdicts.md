# Verdicts

Every reference ends a run with exactly one of thirteen verdicts. The verdict is derived from the field-level issues by a single function, `compare.verdict_for`, which takes the issues and the suppressed differences and returns a string — no model, no heuristic that varies between runs, nothing that cannot be re-derived from the cached registry response.

`UNCHECKED` is the verdict that function mostly does not return. When no registry could be reached there is no record to compare against and so no field-level issue to derive anything from, and `compare.compare` records it directly. The one case it *is* derived from an issue is the third path below: there a record did answer, and `identifier/uncompared` is the finding that says so.

## The thirteen

Listed worst first. This is the order `model.VERDICTS` declares and the order the terminal report prints groups in, so the last thing on the screen at the end of a run is the part that needs no action.

| Verdict | Meaning | Fails CI |
|---|---|---|
| `RETRACTED` | the cited work has itself been retracted | yes |
| `BAD-ID` | the identifier resolves in no consulted registry | yes |
| `WRONG-WORK` | the identifier resolves, but to a different paper | yes |
| `FIELD-MISMATCH` | right work, but stored metadata disagrees with the registry | yes |
| `UNCONFIRMED` | no identifier and no confident registry match — needs review | yes |
| `DISPUTED` | registries disagree with each other; a human must decide | no |
| `INCOMPLETE` | the registry holds fields the entry omits | no |
| `ADJUDICATED` | a difference this project's `.bibaudit.toml` decided to accept | no |
| `REGISTRY-ARTIFACT` | difference explained by a known registry defect | no |
| `TITLE-DRIFT` | title differs in wording but denotes the same work | no |
| `COSMETIC` | identical apart from glyphs or capitalisation | no |
| `UNCHECKED` | nothing was verified: nobody answered, nobody was asked, or the record held nothing to compare | no |
| `OK` | every checked field agrees | no |

The verdict is the most severe single thing found. An unresolvable identifier outranks everything except retraction; any remaining error field gives `FIELD-MISMATCH`; a registry contradicting another gives `DISPUTED`; any warning gives `INCOMPLETE`. `TITLE-DRIFT` and `COSMETIC` are reached only when nothing above them applied, which is why a wording difference on an entry that is also missing a volume reports as `INCOMPLETE` rather than as drift.

`WRONG-WORK` additionally requires the author lists to disagree. A low title score on its own gives `FIELD-MISMATCH` instead: if the bylines match, the likelier explanation is a defective registry title, and "this DOI is a different paper" is an accusation that evidence does not support.

By default the report prints only the five failing groups plus `DISPUTED` — what a reader has to act on. `--verbose` prints every group and every informational issue; `--show-suppressed` pulls in the entries carrying a suppressed difference even from groups the default report hides.

## `OK` is a verdict. `PASS` is not

`OK` is a per-reference verdict and means every checked field agreed with a registry that answered.

`PASS` is the run-level banner and is not in `VERDICTS` at all. `report.py` prints `PASS — no reference in the failing set` whenever no reference falls in the failing set, so a run whose entries are all `INCOMPLETE`, all `DISPUTED`, or all `UNCHECKED` prints `PASS` and exits 0. A total registry outage produces exactly that: every reference `UNCHECKED`, banner `PASS`, exit 0. Read the summary counts, which name every verdict that occurred, rather than the banner.

The banner is computed from the policy in force, not from the default one, so it can never announce a failure on a run the tool deliberately passed. When `--fail-on` is set to something other than the default, the report prints the set in force underneath the banner. References that need attention but sit outside the configured set are counted on their own line rather than passed over in silence.

The groups printed follow the same policy. Adding a verdict to `--fail-on` brings its references into the report — a run cannot exit 1 over a group whose citekeys it never printed — and removing one takes nothing away, because the verdicts a reader has to act on are printed whether or not this run fails on them.

## What fails a build

`FAILING_VERDICTS` is exactly five: `RETRACTED`, `BAD-ID`, `WRONG-WORK`, `FIELD-MISMATCH`, `UNCONFIRMED`. Anything in that set makes the exit code 1; no other verdict does. Change the set with `--fail-on`, whose default is those five:

```bash
bibaudit check references.bib --fail-on RETRACTED,BAD-ID,WRONG-WORK
bibaudit check references.bib --fail-on ''    # no verdict fails the run
```

Two things exit 1 independently of the verdicts: a bibliography entry that could not be read and so was never audited, and an unresolved citekey, used in a document and absent from the bibliography. Exit 2 means the tool could not run: a usage error or an unreadable file. See [in CI](ci.md).

## `BAD-ID` is about whichever identifier did the looking up

An entry is resolved by the strongest identifier it carries — `doi`, then `pmid`, then `isbn` — and `BAD-ID` says that identifier resolves in no registry that answered. Which one it was is on the finding rather than in the verdict: a DOI nobody holds is reported as `doi/unresolved`, and any other identifier as `identifier/unresolved` with the stored value beside it. That is the pair a `.bibaudit.toml` rule has to name, and [adjudicating a difference](suppressions.md) covers how.

The evidence behind it is narrower for a PMID than for a DOI, in two ways. A DOI is put to Crossref and then to DataCite; a PMID is put to PubMed and nowhere else, because no other registry in this tool is keyed on one. And PubMed's answer is an omission rather than a sentence: `efetch` returns the citations it holds for the numbers in the request and says nothing about the rest, so what `BAD-ID` rests on is that no citation came back under that number. That much is a fact about the response, and it is all that is claimed. It does not distinguish a number NLM never assigned from one it assigned and later withdrew — `efetch` answers both with an empty body — so a `BAD-ID` on a PMID says the number resolves to nothing today, not that it never named a paper.

An answer of any other shape is not that fact and is not this verdict. `efetch` returning a citation under a number nobody asked for, or the request failing outright, leaves the entry `UNCHECKED` with an `identifier/inconclusive` finding naming what came back instead. Take PubMed away with `--no-corroborate` and the verdict is `UNCHECKED` too, never `BAD-ID`.

## `RETRACTED` is about the cited work

`RETRACTED` means the work you cited was itself retracted. A retraction **notice** — the editorial statement — is an ordinary citable document, and citing one deliberately, in a paper about a retraction, is correct. The tool never reports that as a defect. The two readings are opposites, which is why the verdict's help string says "the cited work has itself been retracted" rather than anything about carrying a notice. [Retraction](retraction.md) covers the sources and what a clean result does not establish.

An expression of concern is an error and so lands under the `FIELD-MISMATCH` heading, whose wording does not describe it. The issue line on the reference says what was found; only the group heading reads oddly. Giving expressions of concern a verdict of their own means changing `VERDICTS`, `FAILING_VERDICTS`, the report's help strings and this table together, so until all four change they stay under `FIELD-MISMATCH`.

## `ADJUDICATED` and `REGISTRY-ARTIFACT` are different claims

Both are non-failing, and they are separate verdicts because they make incompatible statements that a report has to keep distinguishable.

`REGISTRY-ARTIFACT` says a **documented registry defect** explains the difference. It is settled for everybody, it needs no reader, and it can be challenged: every rule that can produce one is written up in [registry defects](registry-artifacts.md), and a test fails the build if one is not named there. That covers both kinds of rule: the field-level checks in `benign.CHECKS`, and the author-comparison escapes in `names.ARTIFACT_REASONS`, which are decided while walking two bylines in step and so cannot be field-level checks. What the test enforces is that the rule is named, not that the prose around it explains anything; the explaining is the author's job, and many sections go further and name a witnessed instance by DOI, with the registry's own response kept verbatim under `tests/data` so the suppression can be checked against the bytes that produced it.

`ADJUDICATED` says somebody on **this project** wrote a rule in `.bibaudit.toml` saying not to care. That rests on a person's say-so and it can go stale — a citekey is renamed and the rule silently stops applying, or the registry fixes its record and the entry is being excused for a difference that no longer exists. See [adjudicating a difference](suppressions.md).

The report keeps them apart in three places: `ADJUDICATED` ranks higher, so it prints first and is the one you re-read; it is coloured amber where `REGISTRY-ARTIFACT` is green; and the summary counts them separately — `N registry defect(s)` and `N adjudicated here` on the `suppressed` line, with `registry_artifacts` and `adjudicated` as separate fields in the JSON report. Separate counts keep a project-local rule that silences a field across a whole bibliography from looking like a run of known Crossref mojibake.

## `UNCHECKED` never fails

A registry outage is ignorance, not a defect in your bibliography. A check that breaks the build whenever Crossref is down for an afternoon is a check people learn to bypass, and a bypassed check protects nobody. So `UNCHECKED` stays out of the failing set — but it is never silent: it appears in the summary counts, and when a source that carries retraction linkage could not be reached the report says so beside the banner.

### Three ways to reach it, and the third is not an outage

Nobody answered, nobody was asked, and — `identifier/uncompared` — a registry answered with a record holding no title, byline, year, container, volume, issue, pages or publisher for the entry to be compared against. Every check in `compare` returns in silence when the registry's value is empty, correctly, since a registry omitting a field says nothing about a bibliography. A record that omits *all* of them produces no issues, and without `identifier/uncompared` the entry would report `OK`: "every checked field agrees", over zero checked fields. The finding keeps a run that asked, got an answer and compared nothing from printing the clean bill of health that only a full comparison earns.

MEDLINE's whole-book records are one source of that shape: they file the title under `BTI` and the byline under `FED`, and bibaudit reads both tags. The finding is defined by the shape, not by any one source of it. It ranks below every other verdict, so anything found still outranks it, and it can only displace a verdict reached over an empty comparison.

## `UNCONFIRMED` means *needs review*

`UNCONFIRMED` is raised for an entry that stores no identifier and that no consulted source could confirm — the issue is `identifier/absent`, and when the title/author search ran, its note gives the reason each candidate found was rejected. That is the shape a fabricated reference takes, which is why it fails. It is also the shape a real 1974 conference paper takes. Registry coverage has gaps, and the verdict is worded as review, never as fabrication. What it never means is that nothing could be reached: that is `UNCHECKED`.

## What `consulted` records

Each result carries a `consulted` map — in the JSON report, per reference — saying what each registry contributed. Each registry is in one of three states:

`answered`
:   Queried and replied, including an authoritative "I do not hold this DOI", which is evidence and is what makes `BAD-ID` a fact rather than a guess.

`unreachable`
:   Queried and could not reply, through a timeout or a run of 5xx responses. That is ignorance, never absence.

`not-asked`
:   Never queried. DataCite is only asked about DOIs Crossref did not answer for. On a DOI-bearing reference `--no-corroborate` drops PubMed's bibliographic corroboration but does not by itself put PubMed here: the retraction check queries PubMed too, so that takes `--no-retraction-check` as well. A reference resolved by its PMID does put PubMed here, and Crossref and DataCite land here too — neither is keyed on a PMID, so neither is ever asked about one. That holds unless the registry is `unreachable`, which is run-wide and outranks `not-asked`, so Crossref timing out while some *other* reference was being resolved is reported `unreachable` on this one as well. See the note below.

The map records what evidence a verdict rests on, so a registry is `answered` only when it was queried and replied; a reference never claims a curated second opinion nobody sought.

`crossref`, `datacite`, `pubmed` and `retraction-watch` are named on every reference, whatever happened. The first three are the registries. The fourth is named because a retraction source the map left out would let its absence read as nothing to report, and the case where that matters most is a PMID-resolved entry, where none of the DOI-keyed sources is asked. Other names appear only when they were involved: `openlibrary` on a book — resolved by its ISBN, or searched for when it carries no identifier — the search sources on an entry with no identifier, and `zbmath` on an entry whose creators past the registry's byline it was asked about.

An unasked source that carries a retraction signal is also stated on the reference itself, as a `status/not-asked` issue, and beside the run's banner. [Retraction](retraction.md) has that.

!!! note "`unreachable` is run-wide, on purpose"

    A registry that fell over while a *different* entry was being resolved is reported `unreachable` on this one too, even if this DOI never reached it. That is pessimistic, and it is the same set the `UNCHECKED` verdict is derived from — so the map always explains the verdict printed beside it. A report saying "everything answered" next to "nothing could be reached" would contradict itself; an overly pessimistic map does not.
