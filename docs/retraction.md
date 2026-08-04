# Retraction

Every reference that resolves **to a DOI** is checked for retraction, and the
answer is the **union over every source that answered** — never the primary
registry's opinion. A retraction that only one source records is still reported,
and the finding names which one, because "both curated sources agree" and "only
PubMed knows about this; the publisher never deposited the linkage" are different
things to hand a reader. The second is also a bug report for the publisher.

A book resolved through its ISBN alone is not checked at all. That is not an
oversight; it is stated below, under [what a clean result does not
establish](#what-a-clean-result-does-not-establish).

## The four sources

Two are read off the bibliographic record a registry already returned. Two are
contributed by `registries/retractions.py`, and exist because a bibliographic
record carrying nothing is not evidence that there is nothing to carry.

| Source | What is read | What it depends on |
|---|---|---|
| **Crossref** | the `updated-by` relation on the work's own record | a publisher having deposited the notice, and Crossref's pipeline having linked it to the work |
| **PubMed/MEDLINE** | `PT - Retracted Publication`, curated by NLM independently of the publisher's Crossref deposit | NLM having indexed the work and applied the type |
| **Retraction Watch** | its own bulk export, read directly — not the subset Crossref surfaced | the database having logged the retraction |
| **PubMed `ECI`** | the "Expression of Concern In:" cross-reference, which MEDLINE records on the concerned paper's own entry | NLM having recorded the concern |

The last two were added because each closed a gap found by running the tool
against real DOIs. Crossref's `updated-by` does carry Retraction Watch's
linkage — each entry arrives stamped with the source that contributed it,
`publisher` or `retraction-watch` — but a Retraction Watch record only reaches
it where a publisher's deposit and Retraction Watch's record agreed well enough
for Crossref's pipeline to link them, and only for DOIs Crossref carries at all,
so a retraction Retraction Watch has logged and no deposit ever linked was
invisible. And `registries/pubmed.py` reads only `PT`, which is not where NLM
records a concern — it records one as an `ECI` cross-reference on the concerned
paper's own entry. PMID 23741377 carries `PT - Journal Article` and `PT -
Research Support, Non-U.S. Gov't`, nothing retraction-shaped, and separately an
`ECI` line naming the actual notice.

DataCite is deliberately not a fifth source. Its schema does carry an
`IsObsoletedBy` relation, which looked like a candidate until it was queried: it
means "a newer version of this deposit exists", and reading it as a retraction
would fail every Zenodo software release with more than one version. A DOI only
DataCite answers for still gets the Retraction Watch and PubMed checks, because
`Retractions.status_for` takes DOIs and knows nothing about which registry
answered for the work's fields.

## Why a union

`compare._status_issues` collects every record that carries a retraction flag,
whichever source produced it. There is no primary. The tool used to read the flag
off Crossref — or, failing that, DataCite — and drop PubMed's, so a paper NLM
records as `PT - Retracted Publication` whose publisher never deposited the
Crossref linkage passed as clean. That is the worst miss available to this tool,
and it defeats the stated reason PubMed is consulted at all: it is curated
separately, so it is the source that can know something Crossref does not.

The finding names the sources that asserted it, and names any source that
answered for the work and carries no linkage — "recorded by pubmed and not by
crossref, which answered for this work and carries no retraction linkage". A
source that recorded a *concern* is never listed as dissenting: it has not
contradicted the retraction, and printing it that way would read as a second
opinion against a finding it in fact corroborates more weakly.

PubMed's `ECI` reading is folded into the same `pubmed` record the bibliographic
fetch already produced, in place, rather than added under a second key. It is one
MEDLINE record read one field further, not a second witness, and a second key
would let one fact be attributed to two named sources.

## `updated-by`, never `update-to`

Crossref links a work and its notice with a pair of fields that are opposites.
`updated-by` on a record means **this work was retracted**. `update-to` means
**this record is the notice**. Read backwards, the tool clears retracted papers
and accuses the people who cite the notice — which, in a paper about a
retraction, is the ordinary and correct thing to cite. The direction was
confirmed against 10.1016/S0140-6736(20)31180-6, the retracted Surgisphere
paper, and its notice 10.1016/S0140-6736(20)31324-6.

Publishers do deposit both directions at once. Elsevier deposits `update-to`
*and* `updated-by` for the same pair on the notice, so read naively the notice
has itself been retracted. The obvious fix — discount every reciprocated
`updated-by` — is catastrophic, because the retracted paper carries the mirror
image and would be cleared. What separates the two records is that Retraction
Watch recorded the notice side on the notice and the retracted side on the paper.
So a relation is discounted only when Retraction Watch says this record is the
notice and does **not** also say it was retracted; everything else keeps the
`updated-by` entry. A tie breaks towards the finding.

MEDLINE sets the same trap twice, and both are handled the same way. `PT -
Retracted Publication` means the record was retracted; a record that *is* a
notice carries a different publication type in the same controlled vocabulary,
close enough in wording that any check looser than exact equality on the folded
value — a substring test for "retract", say — would flag the notice that
retracted it, an ordinary and perfectly citable document, as a retracted work.
PMID 9500320, the Wakefield paper, carries `PT - Retracted Publication`; the
Lancet notice that retracted it, PMID 20137807,
does not. Likewise `ECI` ("Expression of Concern In:", on the concerned paper)
against `ECF` ("Expression of Concern For:", on the notice): only `ECI` is read.
All four MEDLINE records are kept verbatim in `tests/data/`.

## An expression of concern is not a retraction

A concern is reported under its own finding, `status/expression-of-concern`, and
never under the word *retracted*. Its note says so in as many words: that is a
stated doubt, not a retraction — the work stands, and citing it is legitimate
once the notice has been read.

It carries the same severity as a retraction, because an author who cites a paper
under a concern needs to know before submission, and the distinction was
introduced by re-labelling the finding, never by relaxing it. The instance that
forced it is 10.1371/journal.pone.0064723, whose Crossref record carries two
`expression_of_concern` entries in `updated-by` and no retraction of any kind:
the report printed "the cited work has itself been retracted" about a paper
nobody has retracted, which is a false statement about a named work made with the
tool's full authority.

Membership of the concern vocabulary is an exact match on the folded kind, never
a substring — the two vocabularies overlap in real data, and a notice titled
"Resolution of expression of concern" is as often a retraction as an exoneration.
Anything not in that closed set counts as a retraction, including a kind this
tool has never seen: a source it has not heard of cannot talk it out of the
finding. If one source records a retraction and another a concern for the same
work, both findings are printed and the verdict is `RETRACTED`.

!!! note "Where a concern lands in the verdict table"

    A concern is an error, so the entry fails — but it currently fails under
    `FIELD-MISMATCH`, whose group heading reads "right work, but stored metadata
    disagrees with the registry". The issue line beneath it says exactly what is
    wrong. This is documented in `compare.verdict_for` as a compromise rather
    than a design: the honest label is a verdict of its own. See
    [verdicts](verdicts.md).

## What a clean result does not establish

**Only DOIs are checked.** All four sources are keyed on DOIs, and Open Library
mints none, so a book resolved through its ISBN alone has nothing to ask them
about. Its retraction status is not checked, and a clean report does not claim
otherwise. A DOI carried by a candidate that a title/author search confirmed *is*
checked, on the spot, because it is new to the run.

**A notice never promotes a DOI to "resolved".** Retraction status is looked up
for every stored DOI, resolved or not, but a notice is only attached where some
bibliographic registry already answered. A DOI nothing can resolve stays
`BAD-ID` rather than acquiring a fieldless stub record and a wall of "missing
title" findings.

**Crossref's and PubMed's own flags depend on a linkage existing.** A retraction
nobody deposited and NLM never indexed is invisible to both.

**Retraction Watch's database is community-maintained and not exhaustive**, and
its parsed index is refetched at most once every seven days — much shorter than
the 90 days ordinary registry lookups get by default, because retraction status
changes under a DOI whose bibliographic metadata never changes again. A
retraction logged there in the last few days may not yet be reflected. The
seven-day window belongs to that index's own cache; `--refresh` does not shorten
it.

**`--no-retraction-check` turns the independent pair off.** A Crossref or PubMed
record that itself carries a retraction linkage still fails regardless — the flag
removes corroboration, not reporting. See [the command line](cli.md).

## When a source could not be reached

Silence from a source nobody could reach is not a clean bill of health, and on
this field a miss is the one with no remedy. The two independent sources fail
through different channels, and both end in the run's unreachable set.

A Retraction Watch outage is caught at that source's own boundary, so PubMed's
independent answer is not lost with it. It does not vanish: `status_for` returns
it in `RetractionStatus.unreachable`, naming `retraction-watch`, and `audit.py`
folds that into the run's unreachable set. A `RuntimeWarning` is issued as well,
for a library consumer not reading that field. A PubMed outage propagates instead,
and `audit.py` records `pubmed` as unreachable.

Either way, `compare` then raises `status/retraction-unverified` on each affected
reference: no source that answered records a retraction, and a source that could
have was unreachable — "which is not the same as there being none". It is
`info` severity, so the verdict does not move; an outage is not a defect in
anybody's bibliography, and a run-wide outage must not relabel every correct
entry in the file. It reaches the reader three ways: through the JSON report's
issue list, through `--verbose`, and — because `info` is filtered out of the
default terminal report — as one line printed after the `PASS`/`FAIL` banner,
reading `retraction status not corroborated for N reference(s):` followed by the
names of the sources and the word `unreachable`. Printed once for the run, not
once per reference: with a source down it applies to every entry, and 438
identical lines are wallpaper rather than a warning. Each affected reference's
`consulted` map also records that source as `unreachable`.

DataCite and Open Library are excluded from that line, because neither data model
carries a retraction signal at all and naming them would manufacture a doubt
neither could ever resolve — on every dataset, preprint and book in the file. The
rule is written as an exclusion rather than as a list of sources that do carry
the signal, so whoever adds the next registry is counted by default and has to
come and opt out. `retraction-watch` is deliberately not excluded: it is the one
source that exists only to carry this signal.

[Limits](limits.md) states the whole boundary, of which this is one part.
