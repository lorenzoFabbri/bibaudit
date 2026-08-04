# Limits

This tool's whole claim is that its findings can be checked, which makes the
boundary of what it establishes part of what it reports. The [home
page](index.md) summarises that boundary; this page states it in full. It is the page to read
before treating a passing run as assurance.

## Metadata, not argument

Every report carries the same sentence, `report.LIMITS_NOTICE`:

> bibaudit verifies that each reference exists and that its stored metadata
> matches the publisher's record. It does not and cannot verify that a cited
> work supports the statement it is attached to — that requires reading the
> paper.

The terminal report prints it dimmed, below the closing banner, on every run —
a failing one and a passing one alike. The JSON report carries the identical
string as the payload's `limits` field, so a dashboard built on `--format json`
cannot show the counts without also having been handed the caveat.

bibaudit can tell you that the DOI stored beside a reference resolves to a
different paper than the one described in the entry — that is `WRONG-WORK`, and
it is one of the failure modes a plain "does the DOI resolve" check passes. It
cannot tell you whether the paper that DOI *does* resolve to supports the
sentence you attached the citation to. The comparison works on titles, author
lists, years, container titles, volume, issue, first page and publisher, and
none of those is evidence about a claim in your manuscript. A perfectly
recorded citation of an irrelevant paper passes every check in this tool,
cleanly, and nothing here will ever say otherwise. Reading the paper is the only
remedy, and no metadata check substitutes for it.

## It cannot prove that a work does not exist

`BAD-ID` is worded exactly as narrowly as the evidence allows: *resolves in no
consulted registry*. That is a fact about the registries that answered on that
run, not a fact about the world. A registry that could not be reached leaves the
reference `UNCHECKED` instead, because confusing ignorance with absence is the
one way this tool could accuse a real paper of not existing.

Registry coverage has real gaps — pre-1990 work, grey literature, non-English
publishing, and books. Books are the widest of them. Most were never issued a
DOI, so an entry carrying an `isbn` and no DOI is resolved through Open Library
instead, whose catalogue is crowd-sourced and noticeably thinner than
Crossref's: a great many records carry a title and nothing else, no authors and
no publication year. That thinness is why `compare.confirm_without_id` refuses
outright any candidate carrying neither an author list nor a year, however well
its title scores. A title-only record is evidence that *some* book with that
title exists, not that it is the right one, and accepting it would be the
plausible-but-wrong match that function exists to prevent.

An entry that carries no identifier and that nothing can confirm to that
standard is reported `UNCONFIRMED`, printed under the heading "no identifier and
no confident registry match — needs review", with its issue note saying why no
candidate was accepted. It is in the failing set, because that is the shape a
fabricated reference takes and it needs a person to look at it. It means *needs
review*. It never means *fabricated*, and the report never uses that word.

## What `PASS` means, and what it does not

!!! warning "`PASS` is the run banner, not a verdict"

    The run's closing banner is `PASS — no reference in the failing set` or
    `FAIL — N reference(s) in the failing set`, and it is computed from one
    thing: whether any reference's verdict falls in the failing set — by
    default `RETRACTED`, `BAD-ID`, `WRONG-WORK`, `FIELD-MISMATCH` and
    `UNCONFIRMED`. The exit code follows the same test, 0 or 1. (Two things
    outside the verdicts also set it: a `[@citekey]` that resolves to no
    bibliography entry exits 1, and a tool that could not run at all — bad
    arguments, an unreadable input, a broken config — exits 2.)

    `DISPUTED`, `INCOMPLETE`, `ADJUDICATED`, `REGISTRY-ARTIFACT`,
    `TITLE-DRIFT`, `COSMETIC`, `UNCHECKED` and `OK` are all outside that set.
    So a run in which no registry could be reached reports every reference
    `UNCHECKED`, prints `PASS`, and exits 0. That is deliberate: an outage is
    not a defect in your bibliography, and a check that breaks the build when
    Crossref has a bad afternoon is a check people learn to bypass. An
    `--offline` run against a cache that is empty or past its lifetime
    produces the same result, for the same reason.

    Read the summary counts, not the banner. `bibaudit — N references
    checked` and the per-verdict tally above the banner are what say how much
    was actually established.

`OK`, the per-reference verdict, is the stronger word, and it is still narrower
than "correct": it means every field bibaudit compared agreed with a registry
that answered. Which registries those were is recorded on the result itself —
`consulted` distinguishes `answered`, `unreachable` and `not-asked` per
registry — so an `OK` reached while PubMed was unreachable is distinguishable
from one reached with PubMed's agreement. `--no-corroborate` is not visible
there: it drops PubMed as a corroborating source for the field comparison, but
the retraction check still queries PubMed, so `consulted` records it as
`answered` unless `--no-retraction-check` was passed as well.
[Verdicts](verdicts.md) covers all thirteen and which of them break a build.

## What a clean retraction result establishes

Less than the absence of a `RETRACTED` line suggests, and the shortfall is
worth stating item by item. [Retraction](retraction.md) describes the sources
and how they are combined; these are their edges.

- **Only DOIs are checked.** Retraction status is looked up by DOI — the one
  stored in the entry, or one a title/author search confirmed. A book resolved
  through its ISBN alone is not checked for retraction at all: every source is
  keyed on DOIs and Open Library mints none, so there is nothing to ask them
  about. On such an entry a clean report is silence, not a result.
- **Crossref's and PubMed's own flags depend on somebody having recorded the
  linkage** — a publisher deposit, or NLM's curation. A retraction nobody
  deposited and nobody indexed is invisible to them.
- **Retraction Watch's export is a snapshot, and a slightly stale one.** It
  carries what its curators have recorded and nothing else, and the parsed index
  is cached and only refetched once it is seven days old — so a retraction
  logged there in the last few days may not yet be reflected in a run.
- **A source that could not be reached is stated, not passed over.** When a
  registry that carries the signal was unreachable and no registry that did
  answer records a retraction, the run prints `retraction status not
  corroborated for N reference(s)` beside the banner, naming which source went
  unanswered. DataCite and Open Library are deliberately excluded from that
  notice: neither data model carries a retraction, withdrawal or concern
  element, so an outage at either is not ignorance about retraction and saying
  it was would put a manufactured doubt on every dataset and every book in the
  file. A reference *no* registry answered for is `UNCHECKED`, and that gap is
  stated by the verdict rather than by this line.
- **`--no-retraction-check` turns the independent pair off.** A Crossref or
  PubMed record that itself carries a retraction linkage still fails.

## Registries are sometimes wrong

The registry is a witness, not an authority, and bibaudit has no standing to
decide that the registry is right and your bibliography wrong. That is why it
reports rather than rewrites, and it is a limit as much as a principle: a
finding tells you two records disagree, not which of them to believe.

Where a difference is a known, reproducible registry defect — mojibake
surnames, an article number recorded against a page range, a title the registry
stores truncated where the entry has it in full — it is suppressed as
`REGISTRY-ARTIFACT` and still counted in the summary, with the case written up
in [registry defects](registry-artifacts.md) so you can challenge the call.
Where two registries disagree with each other rather than with you, the verdict
is `DISPUTED`, both values are printed, and neither is preferred: the tool has
no basis for choosing between two curated sources, and pretending otherwise
would be the same overreach as rewriting the file. `DISPUTED` does not fail a
build; it is handed to you to settle. Where you have settled one, record it in
`.bibaudit.toml` with a reason — see [adjudicating a
difference](suppressions.md) — and the entry reports `ADJUDICATED`, counted
apart from a documented defect because one project's decision and a settled
registry bug are different amounts of assurance.
