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
lists, years, container titles, volume, issue, first page, publisher and the
PMID, and none of those is evidence about a claim in your manuscript. A
perfectly recorded citation of an irrelevant paper passes every check in this
tool, cleanly, and nothing here will ever say otherwise. Reading the paper is
the only remedy, and no metadata check substitutes for it.

A resolving identifier is the weakest thing on that list, not the strongest, and
a PMID is worth saying so about because it looks like more than it is. A PMID
that resolves establishes one fact: NLM has indexed some work under that number.
It does not establish that the work is the one the entry describes — that is
what the field comparison beside it is for, which is why a PMID lookup is
followed by the same title, author and year checks a DOI lookup gets. It does
not establish that the work is sound: PubMed indexes journals rather than
judging findings, and a retracted paper keeps its PMID — PMID 9500320, the
Wakefield paper, resolves exactly as it did before, and the retraction is a
field on that record rather than its removal. And it says nothing whatever
about whether that work supports the sentence it was cited for. Two identifiers
agreeing with each other is a stronger statement than one resolving, and it is
still a statement about identifiers.

## It cannot prove that a work does not exist

`BAD-ID` is worded exactly as narrowly as the evidence allows: *resolves in no
consulted registry*. That is a fact about the registries that answered on that
run, not a fact about the world. A registry that could not be reached leaves the
reference `UNCHECKED` instead, because confusing ignorance with absence is the
one way this tool could accuse a real paper of not existing.

How many registries "no consulted registry" covers depends on the identifier. A
DOI is put to Crossref and then to DataCite. A PMID is put to PubMed alone, and
an ISBN to Open Library alone.

One registry is one registry, and the PMID case is worth stating without
flattery. Other indexes do resolve PMIDs — Europe PMC, already in this tool for
the identifier-less search, answers for 9500320 with the Wakefield paper — but
it serves that record from MEDLINE, and a `SRC:MED` hit is NLM's own citation
redistributed. Europe PMC corroborates a title/author match elsewhere in this
tool precisely because it is curated separately there, indexing preprints and
grey literature Crossref never saw; on a PMID it has nothing separate to say,
and agreement between the two would be one answer read twice. That leaves a
limit rather than a reassurance: a `BAD-ID` on a PMID rests on one index's
response, and the second opinion that would widen it does not exist to be had.
The ISBN case is thinner still; see below.

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

- **Two of the four sources are keyed on a DOI.** Crossref's `updated-by` and
  Retraction Watch's export are looked up by DOI — the one stored in the entry,
  or one a title/author search confirmed. An entry resolved by another
  identifier loses those two, and what it keeps differs. A reference resolved by
  its **PMID** keeps both of PubMed's, NLM's `PT - Retracted Publication` and
  the `ECI` cross-reference, because both are fields of the MEDLINE record the
  lookup already returned — so a concern NLM recorded is reported, and what goes
  unreported is a retraction only Retraction Watch or only a publisher's Crossref
  deposit knows about. A book resolved through its **ISBN** alone is not checked
  for retraction in any way: Open Library mints no DOI to ask the first two
  about, and holds no MEDLINE record to read the other two off. Neither case is
  silence: both raise `status/not-asked`, which names the retraction sources the
  run did not ask and prints beside the banner exactly as an outage does.
- **Crossref's and PubMed's own flags depend on somebody having recorded the
  linkage** — a publisher deposit, or NLM's curation. A retraction nobody
  deposited and nobody indexed is invisible to them.
- **Retraction Watch's export is a snapshot, and a slightly stale one.** It
  carries what its curators have recorded and nothing else, and the parsed index
  is cached and only refetched once it is seven days old — so a retraction
  logged there in the last few days may not yet be reflected in a run.
- **A source that went unheard is stated, not passed over.** When a registry
  that carries the signal was unreachable, or was never asked, and no registry
  that did answer records a retraction, the run prints `retraction status not
  corroborated for N reference(s)` beside the banner, naming the sources and
  which of the two happened to them. Two lines, never one: a rerun may settle an
  outage, and no rerun asks a DOI-keyed source about a reference that has no
  DOI. The unasked line goes on to say which of *its* two reasons applied — a
  source that takes an identifier the reference does not carry, or one that had
  a key and was not queried on this run — because only the second has a
  remedy. Four sources are deliberately excluded from both, because bibaudit reads
  no retraction signal from any of them and naming one would put a manufactured
  doubt on a whole class of entry. DataCite and Open Library have no retraction,
  withdrawal or concern element in their data models at all. For Europe PMC and
  OpenAlex the limit is bibaudit's own and so belongs here rather than there:
  **Europe PMC does publish retraction linkage**, in `commentCorrectionList`,
  and `registries/search.py` reads neither that nor anything like it. Both are
  consulted for candidates only, on entries carrying no identifier. A reference
  *no* registry answered for is `UNCHECKED`, and that gap is stated by the
  verdict rather than by this line.
- **`--no-retraction-check` turns the independent pair off**, which is Retraction
  Watch's export and PubMed's `ECI`. A Crossref or PubMed record that itself
  carries a retraction linkage still fails, and every reference records
  `retraction-watch` as `not-asked` and carries the gap. The `ECI` half is the
  one the run cannot state: PubMed answered for the citation, so `consulted`
  says `answered` and no `not-asked` line names it. [Retraction](retraction.md)
  says why there is no fourth consultation state for it.

## A person recorded by the name they go by

Forenames are compared by their first initial, so an entry holding `Edgerton,
V R` and a registry holding `Edgerton, Reggie` disagree — and they are one
person, *V. Reggie Edgerton*, recorded by his middle name on the deposit and by
his initials in MEDLINE. PMID 990943 is the live instance, and it was the one
false alarm the check produced over 3,963 MEDLINE/Crossref pairs of the same
work.

The obvious repair is to let a side written entirely as initials answer with any
of them, so `Reggie` may match the `R` in `V R`. Measured on the same sample,
that buys back the one false alarm and costs **11** caught forename
substitutions. Eleven misses per false alarm avoided is the wrong direction for
the one check whose miss is crediting the wrong person, so the narrow rule ships
and this stays a stated limit. A run that hits it prints both forenames in full;
`kind = "forename"` in a project's `.bibaudit.toml` adjudicates it.

## One registry romanises a byline and the other does not

MEDLINE writes an author's surname in ASCII; Crossref deposits it as the author
writes it. `fold` closes most of that gap by romanising the letters NFKD leaves
whole — `Kjær` and `Kjaer`, `Weiß` and `Weiss`, `Guðmundsdóttir` and
`Gudmundsdottir` (PMID 38747246, `10.1093/eurheartj/ehae331`, where the two
registries spell one person both ways) — and each of those letters has exactly
**one** ASCII spelling in the table.

NLM does not always pick the same one. Eth is `d` on the overwhelming majority
of its bylines and `eth` on a few: `Gudmundsdottir[au]` answers 884 records and
`Guethmundsdottir[au]` none, `Sigurdsson[au]` 2,433 against `Sigurethsson[au]`
5, `Fridriksdottir[au]` 114 against `Friethriksdottir[au]` 2. On the minority
records the two spellings are two comparison keys — `Friðriksdóttir` against
MEDLINE's `Friethriksdottir`, PMID 42541912 — and a correct entry reports
`authors/mismatch`.

That is a stated limit and not a second table row. A letter with two mappings
produces two keys, and nothing in the pair being compared says which of them
the other side used; picking one silently reintroduces the failure on the other
spelling. A run that hits it prints both surnames in full, which is what the
report is for.

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
