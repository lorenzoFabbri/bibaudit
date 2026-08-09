# Retraction

Every reference that resolves **to a DOI** is checked for retraction, and the
answer is the **union over every source that answered** — never the primary
registry's opinion. A retraction that only one source records is still reported,
and the finding names which one, because "both curated sources agree" and "only
PubMed knows about this; the publisher never deposited the linkage" are different
things to hand a reader. The second is also a bug report for the publisher.

Two of the four sources are keyed on a DOI and two are fields of the MEDLINE
citation itself, so how much of the union a reference gets depends on which
identifier resolved it. A reference resolved by its PMID gets both of PubMed's
and neither of the DOI-keyed pair. A book resolved through its ISBN alone gets
none. Neither is an oversight; both are stated below, under [what a clean result
does not establish](#what-a-clean-result-does-not-establish).

## The four sources

Two are read off the bibliographic record a registry already returned. Two are
contributed by `registries/retractions.py`, and exist because a bibliographic
record carrying nothing is not evidence that there is nothing to carry.

| Source | What is read | What it depends on | What it is asked with |
|---|---|---|---|
| **Crossref** | the `updated-by` relation on the work's own record | a publisher having deposited the notice, and Crossref's pipeline having linked it to the work | a DOI |
| **PubMed/MEDLINE** | `PT - Retracted Publication`, curated by NLM independently of the publisher's Crossref deposit | NLM having indexed the work and applied the type | nothing: a field of the record already in hand |
| **Retraction Watch** | its own bulk export, read directly — not the subset Crossref surfaced | the database having logged the retraction | a DOI (this tool's key, not the export's only one — see below) |
| **PubMed `ECI`** | the "Expression of Concern In:" cross-reference, which MEDLINE records on the concerned paper's own entry | NLM having recorded the concern | nothing: a field of the record already in hand |

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

The fourth column is what a reference with no DOI keeps. `ECI` sits in
`registries/retractions.py` beside the other independent source because that is
where the direction rule and the concern vocabulary live, not because it needs a
key: `retractions.concern_in` takes a record and reads one field of it, and
`audit._with_pubmed_concern` calls it for an entry resolved by its PMID. Sitting
in that module does mean `--no-retraction-check` switches it off, on the PMID
path and the DOI path alike.

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

Neither is a source this tool reads no retraction signal from — DataCite, Open
Library, Europe PMC and OpenAlex. Their records carry no retraction linkage for
*any* work, so naming one as dissenting states a fact about the client as
though it were a fact about the paper, and states it against the one finding
where weakening it costs most. They are excluded from the two notes below for
the same reason, in the other direction.

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
work, both findings are printed and the verdict is `RETRACTED` — but the
concern's note then reads *it does not undo the retraction recorded for this
work* rather than that the work stands. The same goes for a correction. Both
statements are true at once and neither finding is dropped for the other; what
may not stand is a report telling a reader in one breath that a work has been
withdrawn and that citing it is legitimate.

One source recording both resolves to the retraction alone. A concern raised
first and a retraction issued afterwards is the ordinary escalation, and NLM
keeps the `ECI` cross-reference on the paper's own record when it adds `PT -
Retracted Publication`: PMID 32450107, the Surgisphere *Lancet* paper, and PMID
41224473, a *BMJ* trial retracted on 2026-03-31, both carry the pair, as does
PMID 9500320 above. A `Record` holds one status kind and the more definitive one
is what it holds — `retractions._notice_from_pubmed` returns the retraction
before it reads `ECI`, `audit._with_pubmed_concern` leaves an already-flagged
record alone, and `_KIND_PRIORITY` ranks the two the same way where sources are
merged. Printing the concern's note beside the retraction would tell a reader in
one report that the work has been withdrawn and that it stands.

## A correction is neither

Retraction Watch's export carries a `RetractionNature` of `Correction` as well
as `Retraction`, `Expression of concern` and `Reinstatement`, and
`registries/retractions.py` maps it onto a notice kind of its own. A correction
says the work stands and has been amended — the mildest of the three things a
notice can say, milder than a concern, which leaves the work standing and
doubted.

It is reported as `status/correction` at `info` severity, so the verdict does
not move and the entry can still be `OK`. Failing a build because a cited paper
was corrected is the false alarm this tool's third rule is about. What the
finding is for is the reader who wants the corrected version's numbers rather
than the original's, and it reaches them through the JSON report and
`--verbose`, the same routes `year/alternate-date` and `doi/alias` take. There
is no `--fail-on` switch that makes it bite: `--fail-on` selects verdicts, and
a correction produces none of its own. Beside a retraction another source
recorded, the note says the correction does not undo it and the entry fails on
the retraction, as it would have anyway.

Anything not in the concern or the correction vocabulary is still a retraction,
kind included when this tool has never seen it. The two sets are for kinds this
project *mints* — both come out of `_RW_KIND_MAP` — and neither widens the
"a registry I have not heard of cannot talk me out of the finding" default. A
correction reaching `compare` untagged is how 10.3390/nano14090769
(*Nanomaterials*), for which Retraction Watch logs a correction and nothing
else, came to print `RETRACTED — the cited work has itself been retracted` and
exit 1.

Where two notices disagree about the same DOI, `_KIND_PRIORITY` keeps the more
definitive kind, and a correction is last: retraction, withdrawal, removal,
expression of concern, correction. Ranked above the concern it *softened* a
finding — a DOI Retraction Watch logs a correction for and NLM carries an `ECI`
against merged to the correction, and the concern went unreported.

That merged kind is the answer to "what is the status of this DOI", and it is
not what the report attributes to each source. Each keeps the kind it recorded,
and `compare` takes the union across them, so a work Retraction Watch logs a
correction for and NLM records as retracted is reported as retracted *and* as
corrected, under the name of the source that logged each. Stamped on both, the
registry column stated a retraction Retraction Watch's export does not hold —
and that column is the reader's only route to challenge a finding.

The same order settles two rows of Retraction Watch's own export, which logs a
DOI as often as its status is restated. **The strongest row wins, not the
newest.** A work that has ever been retracted is retracted, and a correction
published afterwards amends the notice rather than the withdrawal: RW's later
row for `10.1002/ana.24658` is a 2019 correction whose `Reason` column reads
`Upgrade/Update of Prior Notice(s)`, over a 2016 retraction, and Crossref
carries no `updated-by` for that DOI at all. 48 DOIs in the 2026-08-09 export
carry a retraction row under a later correction, four more under a later
concern. Within one kind the later row still wins: two rows saying the same
thing are one status restated, and the later one is its current wording.

A `Reinstatement` is the only row that withdraws rather than asserts, so it
alone remains a question of date. It removes every notice for its DOI dated at
or before it and leaves any later one standing — `10.1308/rcsann.2020.0038` was
reinstated in September 2021 and had an expression of concern raised against it
six months after.

A `RetractionNature` this build cannot rank is skipped — guessing "retraction"
for the next category Retraction Watch adds would be the false alarm the third
rule exists to prevent, and it may turn out to be milder than anything here —
but the run says so out loud, in a `RuntimeWarning` naming the value and how
many rows carried it. Skipping is a *missed* notice on the field where a miss
has no remedy, and nothing else in the run would ever mention it. Every value
in the 2026-08-09 export is one this tool ranks (`Retraction` 66,155,
`Expression of concern` 3,586, `Correction` 1,499, blank 241, `Reinstatement`
160), so the warning is silence on an ordinary run. A blank cell is different
evidence and is read as a retraction: it makes no claim about the kind at all,
and the database's whole subject supplies the one it left out.

An unreadable date is ignorance on both sides of that comparison, and ignorance
may neither clear a retraction nor be cleared by one. A reinstatement carrying
one never becomes a cutoff; a notice carrying one is not dated "at or before"
anything either, so no reinstatement removes it. Read as a very old date
instead, an undated retraction was withdrawn by any reinstatement in the file —
the same sentence running the other way. 241 rows of the 2026-08-09 export
carry no date at all.

!!! note "Where a concern lands in the verdict table"

    A concern is an error, so the entry fails — but it currently fails under
    `FIELD-MISMATCH`, whose group heading reads "right work, but stored metadata
    disagrees with the registry". The issue line beneath it says exactly what is
    wrong. This is documented in `compare.verdict_for` as a compromise rather
    than a design: the honest label is a verdict of its own. See
    [verdicts](verdicts.md).

## What a clean result does not establish

**Two of the four sources are keyed on DOIs.** A DOI carried by a candidate that
a title/author search confirmed *is* checked, on the spot, because it is new to
the run. An entry resolved by some other identifier is a different matter, and
the shortfall is not the same for the two of them.

A reference resolved by its **PMID** keeps `PT - Retracted Publication` and the
`ECI` cross-reference, and loses Crossref's `updated-by` and Retraction Watch's
export. Both of PubMed's arrive free: they are fields of the MEDLINE record the
PMID lookup already returned, so a retraction NLM has indexed reports
`RETRACTED` and fails, and a concern NLM has recorded reports as a concern. What
goes unreported is a retraction Retraction Watch logged that NLM never indexed,
and one a publisher deposited that NLM never indexed either. Measured: 600
random retraction rows of the 29,566 in the 2026-08-09 export that carry an
`OriginalPaperPubMedID`, refetched from NLM through this tool's own client — all
600 came back, 555 carry `PT - Retracted Publication`, none carries an `ECI`
instead, and **45 (7.5%) carry no retraction signal in MEDLINE at all**.
Retraction Watch holds a PMID for every one of them; this tool indexes its
export by DOI alone and so cannot ask.

A book resolved through its **ISBN** alone loses all four, because Open Library
mints no DOI to ask the first two about and holds no MEDLINE record to read the
other two off. Its retraction status is not checked at all, and a clean report
does not claim otherwise.

**A notice never promotes a DOI to "resolved", and is reported anyway.**
Retraction status is looked up for every stored DOI, resolved or not.
Retraction Watch answers for DOIs no bibliographic registry carries — 3 of a
random 400 of its retraction DOIs resolve in none of Crossref, DataCite and
PubMed — and the finding is stated on those entries: the source that answered
said the work was retracted, and dropping that because nobody else could name
the work is the one source that did answer going unheard. What such an entry
does *not* get is a verdict of `RETRACTED`. It stays `BAD-ID`: the identifier
resolved nowhere, which is what the reader has to act on first, and calling the
entry retracted would assert that the work Retraction Watch logged is the work
this reference cites — the one thing no registry could confirm. Both statements
are on the report; only one of them is provable from the evidence in hand.

`compare` is where that separation lives. A source carrying post-publication
status and no bibliographic record is never taken as the record an entry is
compared against, so it cannot become a fieldless stub with a wall of "missing
title" findings behind it. PubMed's own flag is held to the same line from the
other side: it is a bibliographic registry, so on a DOI nothing resolved its
notice is not turned into a record of its own — reachable only under
`--no-corroborate`, where no MEDLINE citation is in hand to carry the flag.

No "retraction status not corroborated" clause is added beneath a failing
identifier. Nothing about that entry was corroborated, its verdict says so, and
the caveat would print on every bad DOI in a file.

**Crossref's and PubMed's own flags depend on a linkage existing.** A retraction
nobody deposited and NLM never indexed is invisible to both.

**Retraction Watch's database is community-maintained and not exhaustive**, and
its parsed index is refetched at most once every seven days — much shorter than
the 90 days ordinary registry lookups get by default, because retraction status
changes under a DOI whose bibliographic metadata never changes again. A
retraction logged there in the last few days may not yet be reflected. The
seven-day window belongs to that index's own cache; `--refresh` does not shorten
it. The index lives in a `retraction-watch` subdirectory of whichever cache root
the run was given, so `bibaudit cache info` counts it and `bibaudit cache clear`
is the way to discard it — which, with `--refresh` not reaching it, is the only
way.

**`--no-retraction-check` turns the independent pair off.** The pair is
Retraction Watch's export and PubMed's `ECI`, so the flag costs a concern NLM
recorded as well as a retraction only Retraction Watch logged — on the PMID path
and the DOI path alike. A Crossref or PubMed record that itself carries a
retraction linkage still fails regardless: `updated-by` and `PT` are read off
the bibliographic record and the flag does not reach them. What the flag removes
is corroboration, not reporting.

`consulted` states half of that and cannot state the other half. Retraction
Watch appears as `not-asked` and the reference carries the gap; PubMed appears as
`answered`, because PubMed did answer — it returned the citation every other
field was compared against. `Consultation` records what happened to a *source*,
and there is no state in it for a source that answered one question and was not
asked a second one off the same reply. So a run given this flag has one unstated
gap, and it is stated here instead. See [the command line](cli.md).

## When a source could not be reached

Silence from a source nobody could reach is not a clean bill of health, and on
this field a miss is the one with no remedy. The two independent sources fail
through different channels, and both end in the run's unreachable set.

**An export that carries no usable row is an outage too**, and not a database
with nothing in it. Everywhere else in this project a confirmed absence is a
fact: `/works/10.x/y` answering 404 settles that the registry does not hold that
work. This request asks for the whole database, so the same 404 is a fact about
the endpoint — one that has already moved once under this tool — and a body that
is not the export (a maintenance page, a rate-limit notice, a truncated
download) reaches the parser as zero rows and cannot be told apart from it. Read
as "nothing found", either turns the one source that exists to carry this signal
into a source that answered and had nothing, and the report then states no gap
at all. Nothing decides that on a row count: the test is that the export yielded
nothing whatsoever, so nobody has to tell this tool how big Retraction Watch is.
The emptiness is not written to the index cache either — with a seven-day TTL
and no `--refresh` reaching it, one such fetch would answer a week of healthy
runs.

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

DataCite, Open Library, Europe PMC and OpenAlex are excluded from that line,
because this tool reads no retraction signal from any of them and naming one
would manufacture a doubt a reachable source could not have resolved. For the
first two the limit is the source's: neither data model carries a retraction
element, so the caveat would land on every dataset, preprint and book in the
file. For the second two it is this tool's: Europe PMC's own API does carry
retraction linkage, `registries/search.py` does not read it, and a reference
with no identifier — whose whole candidate pool comes from those two plus
Crossref — carried the doubt on all three names.

The rule is written as an exclusion rather than as a list of sources that do
carry the signal, so whoever adds the next registry is counted by default and
has to come and opt out. Europe PMC and OpenAlex did not, which is why the
partition is now derived from the registry clients themselves in
`tests/test_compare.py` and a client that sets no `Record.retracted` and is
missing from the exclusion fails the build. `retraction-watch` is deliberately
not excluded: it is the one source that exists only to carry this signal.

## When a source was never asked

The same rule from the other direction. A reference resolved by its **PMID** is
asked of PubMed and of nobody else: the two DOI-keyed sources have no key to be
asked with. Left to speak for itself such an entry renders `verdict: OK, issues:
[]` — a clean bill of health from a run that reached two of the four sources and
had no way to reach the other two, which is the output this tool exists to
prevent. A book resolved by its ISBN, and any run given
`--no-retraction-check`, have the same shape.

So where no source that answered records a retraction, `compare` raises
`status/not-asked` on the reference, at `info` severity, naming every source
that carries the signal and was not asked:

| The reference | What the finding names | And why they went unasked |
|---|---|---|
| resolved by its PMID | `crossref, retraction-watch` | take a DOI this reference does not carry |
| a book resolved by its ISBN | `crossref, retraction-watch` / `pubmed` | take a DOI / takes a DOI or a PMID, this reference does not carry |
| any reference under `--no-retraction-check` | `retraction-watch` | was not queried on this run |

The third column is in the printed note, and each group is named beside the key
*it* is looked up by. Pooled into one clause, the second row read `crossref,
pubmed, retraction-watch take a DOI or a PMID this reference does not carry` —
true of PubMed, loose about the other two, and a reader adding a PMID to that
entry would have found two of the three still unasked. The clause is there
because the two reasons are not one: nothing this tool does asks Retraction
Watch about a reference with no DOI, while a source that *had* a key and went
unasked was left out by a flag the reader chose, and dropping the flag is the
whole of the remedy. A run can produce several clauses at once, and each names
its own sources.

**"Takes a DOI" is this tool's key, not a limit of Retraction Watch's.** 33,403
of the 71,641 rows in the 2026-08-09 export carry an `OriginalPaperPubMedID`,
and 715 retraction rows carry one with no DOI beside it — works reachable by a
PMID and by nothing this tool asks with. This module indexes the export by its
DOI column alone, so a reference resolved by its PMID loses Retraction Watch's
answer to that choice rather than to the source, and [what that costs is
measured above](#what-a-clean-result-does-not-establish): 45 of 600 sampled
retraction rows carry a PMID whose MEDLINE citation shows nothing.

It reaches the reader by the three routes above, the banner line ending in `not
asked` rather than `unreachable`. An entry a source *did* report a retraction for
gets the retraction instead: an answer arrived, and there is no gap left to
state. `consulted` states the gap too — `crossref`, `datacite`, `pubmed` and
`retraction-watch` are named on every reference in every run, so a source that
was not asked reads as `not-asked` rather than as a key that is not there.

The third row is the narrower of the three, and the difference is the one this
page states above: `--no-retraction-check` also stops PubMed's `ECI` being read,
and no `not-asked` line says so, because PubMed answered.

Two kinds rather than one, because the reader's next move differs: a rerun may
settle an outage, and no rerun asks Retraction Watch about a reference with no
DOI. `Consultation` keeps `unreachable` and `not-asked` apart for the same
reason. Both are `info`: coverage a reference's own identifier denies it is no
more a defect in a bibliography than a timeout is. The exclusion above applies
unchanged, so DataCite going unasked is never named.

### Reading the DOI off MEDLINE instead

MEDLINE's `AID` list carries the work's own DOI on most modern records, and
using it as a second lookup key would restore both missing sources for a
PMID-resolved reference. It is not done, and the reasons are worth keeping
because the option stays open:

- **It is a widening of coverage, not a statement of one.** A DOI the entry
  never declared would become a lookup key, and the records it fetches would
  reach the field matrix — a change to the verdict path, needing its own tests
  and its own answer to whether a reference may be judged against a work it did
  not name.
- **It is not free.** The DOI arrives at no extra cost, but using it means a
  Crossref lookup, a DataCite lookup for what Crossref lacks, and
  `Retractions.status_for`, which runs `esearch`/`esummary`/`efetch` of its own.
  A PMID lookup costs one request today; this would make it four.
- **It would not close the gap, only narrow it.** `AID` is absent from older
  records — `efetch` for PMID 13351639 (Warburg, *Science* 1956) returns a full
  citation with no `AID` line at all. Those references would keep exactly
  today's coverage, so the gap must be stated either way.

[Limits](limits.md) states the whole boundary, of which this is one part.
