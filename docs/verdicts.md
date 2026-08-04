# Verdicts

Every reference ends a run with exactly one of thirteen verdicts. The verdict is
derived from the field-level issues by a single function, `compare.verdict_for`,
which takes the issues and the suppressed differences and returns a string — no
model, no heuristic that varies between runs, nothing that cannot be re-derived
from the cached registry response.

`UNCHECKED` is the one verdict that function never returns. When no registry
could be reached there is no record to compare against and so no field-level
issue to derive anything from, and `compare.compare` records it directly.

## The thirteen

Listed worst first. This is the order `model.VERDICTS` declares and the order the
terminal report prints groups in, so the last thing on the screen at the end of a
run is the part that needs no action.

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
| `UNCHECKED` | nothing was verified: no registry answered, or none was asked | no |
| `OK` | every checked field agrees | no |

The verdict is the most severe single thing found. An unresolvable identifier
outranks everything except retraction; any remaining error field gives
`FIELD-MISMATCH`; a registry contradicting another gives `DISPUTED`; any warning
gives `INCOMPLETE`. `TITLE-DRIFT` and `COSMETIC` are reached only when nothing
above them applied, which is why a wording difference on an entry that is also
missing a volume reports as `INCOMPLETE` rather than as drift.

`WRONG-WORK` additionally requires the author lists to disagree. A low title
score on its own gives `FIELD-MISMATCH` instead: if the bylines match, the
likelier explanation is a defective registry title, and "this DOI is a different
paper" is an accusation that evidence does not support.

By default the report prints only the five failing groups plus `DISPUTED` — what
a reader has to act on. `--verbose` prints every group and every informational
issue; `--show-suppressed` pulls in the entries carrying a suppressed difference
even from groups the default report hides.

## `OK` is a verdict. `PASS` is not

`OK` is a per-reference verdict and means every checked field agreed with a
registry that answered.

`PASS` is the run-level banner and is not in `VERDICTS` at all. `report.py`
prints `PASS — no reference in the failing set` whenever no reference falls in
the failing set — so a run whose entries are all `INCOMPLETE`, all `DISPUTED`, or
all `UNCHECKED` prints `PASS` and exits 0. A total registry outage produces
exactly that: every reference `UNCHECKED`, banner `PASS`, exit 0. Read the
summary counts, which name every verdict that occurred, rather than the banner.

The banner is computed from the policy actually in force, not from the default
one, so it can never announce a failure on a run the tool deliberately passed.
When `--fail-on` is set to something other than the default, the report prints
the set in force underneath the banner. References that need attention but sit
outside the configured set are counted on their own line rather than passed over
in silence.

## What fails a build

`FAILING_VERDICTS` is exactly five: `RETRACTED`, `BAD-ID`, `WRONG-WORK`,
`FIELD-MISMATCH`, `UNCONFIRMED`. Anything in that set makes the exit code 1; no
other verdict does. Change the set with `--fail-on`, whose default is those five:

```bash
bibaudit check references.bib --fail-on RETRACTED,BAD-ID,WRONG-WORK
bibaudit check references.bib --fail-on ''    # no verdict fails the run
```

An unresolved citekey — used in a document, absent from the bibliography — also
exits 1, independently of the verdicts. Exit 2 means the tool could not run:
a usage error or an unreadable file. See [in CI](ci.md).

## `RETRACTED` is about the cited work

`RETRACTED` means the work you cited was itself retracted. A retraction
**notice** — the editorial statement — is an ordinary citable document, and
citing one deliberately, in a paper about a retraction, is correct. The tool
never reports that as a defect. The two readings are opposites, which is why the
verdict's help string says "the cited work has itself been retracted" rather than
anything about carrying a notice. [Retraction](retraction.md) covers the sources
and what a clean result does not establish.

One honest compromise: an expression of concern is an error and so lands under
the `FIELD-MISMATCH` heading, whose wording does not describe it. The issue line
on the reference says exactly what was found; the group heading is the part that
reads oddly. Giving it a verdict of its own would mean changing `VERDICTS`,
`FAILING_VERDICTS`, the report's help strings and this table together, and that
has not been done rather than done halfway.

## `ADJUDICATED` and `REGISTRY-ARTIFACT` are different claims

Both are non-failing, and they were once one verdict — which made two
incompatible statements indistinguishable in a report.

`REGISTRY-ARTIFACT` says a **documented registry defect** explains the
difference. It is settled for everybody, it needs no reader, and it can be
challenged: every rule that can produce one is written up in
[registry defects](registry-artifacts.md), and a test fails the build if one is
not named there. That covers both halves — the field-level checks and the
author-comparison escapes, which are decided while walking two bylines in step
and so live outside the check module. What the test enforces is that the rule is
named, not that the prose around it explains anything; the explaining is the
author's job, and many sections go further and name a witnessed instance by DOI,
with the registry's own response kept verbatim under `tests/data` so the
suppression can be checked against the bytes that produced it.

`ADJUDICATED` says somebody on **this project** wrote a rule in `.bibaudit.toml`
saying not to care. That rests on a person's say-so and it can go stale — a
citekey is renamed and the rule silently stops applying, or the registry fixes
its record and the entry is being excused for a difference that no longer exists.
See [adjudicating a difference](suppressions.md).

The report keeps them apart in three places: `ADJUDICATED` ranks higher, so it
prints first and is the one you re-read; it is coloured amber where
`REGISTRY-ARTIFACT` is green; and the summary counts them separately —
`N registry defect(s)` and `N adjudicated here` on the `suppressed` line, with
`registry_artifacts` and `adjudicated` as separate fields in the JSON report. A
single total let a project-local rule silencing a field across a whole
bibliography look exactly like a run of known Crossref mojibake.

## `UNCHECKED` never fails

A registry outage is ignorance, not a defect in your bibliography. A check that
breaks the build when Crossref has a bad afternoon is a check people learn to
bypass, and a bypassed check protects nobody. So `UNCHECKED` stays out of the
failing set — but it is never silent: it appears in the summary counts, and when
a source that carries retraction linkage could not be reached the report says so
beside the banner.

## `UNCONFIRMED` means *needs review*

`UNCONFIRMED` is raised for an entry that stores no identifier and that no
consulted source could confirm — the issue is `identifier/absent`, and when the
title/author search ran, its note gives the reason each candidate found was
rejected. That is the shape a fabricated reference takes, which is why it fails.
It is also the shape a real 1974 conference paper takes. Registry coverage has
genuine gaps, and the verdict is worded as review, never as fabrication. What it
never means is that nothing could be reached: that is `UNCHECKED`.

## What `consulted` records

Each result carries a `consulted` map — in the JSON report, per reference —
saying what each registry contributed. Three states, not a bool:

`answered`
:   Queried and replied. Including an authoritative "I do not hold this DOI",
    which is evidence, and is what makes `BAD-ID` a fact rather than a guess.

`unreachable`
:   Queried and could not reply — a timeout, a run of 5xx. Ignorance, never
    absence.

`not-asked`
:   Never queried. DataCite is only asked about DOIs Crossref did not answer for.
    `--no-corroborate` drops PubMed's bibliographic corroboration but does not by
    itself put PubMed here: the retraction check queries PubMed too, so that
    takes `--no-retraction-check` as well.

It was a bool once, computed as "not known to be unreachable" — so a run with
`--no-corroborate` reported `"pubmed": true` on every reference in the file. The
map exists to record what evidence a verdict rests on, and that version claimed a
curated second opinion nobody had sought.

Names beyond `crossref`, `datacite` and `pubmed` appear when they were involved:
`retraction-watch` on a retraction check, `openlibrary` on a book — resolved by
its ISBN, or searched for when it carries no identifier — and the search sources
on an entry with no identifier.

!!! note "`unreachable` is run-wide, on purpose"

    A registry that fell over while a *different* entry was being resolved is
    reported `unreachable` on this one too, even if this DOI never reached it.
    That is pessimistic, and it is the same set the `UNCHECKED` verdict is
    derived from — so the map always explains the verdict printed beside it. A
    report saying "everything answered" next to "nothing could be reached" would
    be worse than a slightly gloomy one.
