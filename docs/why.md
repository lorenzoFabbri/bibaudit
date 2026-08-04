# Why field-level

"The DOI resolves" is precisely the check a wrong citation passes. A resolver
answers one question — is this identifier registered — and a citation can be
wrong in ways that leave the identifier perfectly registered.

## What one audit found

Of confirmed fabricated references in one audit of 53 published papers, 66% were
works that do not exist. A DOI check catches those. But 27% were **real works
with corrupted fields**, and 4% were **valid, resolving DOIs attached to the
wrong paper**.[^taxonomy]

[^taxonomy]: Ansari, S., *Compound Deception in Elite Peer Review: A Failure Mode
Taxonomy of 100 Fabricated Citations at NeurIPS 2025*, arXiv:2602.05930. The 100
citations appeared in 53 published papers, about 1% of that year's accepted
papers; the taxonomy's remaining 3% are placeholder and semantic hallucinations.

Those last two classes are the ones that survive review, and they survive for a
structural reason rather than an accidental one. A reviewer checking a reference
follows the DOI. It resolves. A real paper loads. Nothing about that transaction
compares the year, the journal, the volume or the author list against what the
bibliography actually says — and in the 27% case the paper that loads is the
right one with a wrong year or a wrong journal beside it, while in the 4% case
it is a different paper entirely, wearing a title and author list the entry
never checked it against.

Both are invisible to every tool whose test is "does the identifier resolve".
The first class is the only one that check can see.

## What is compared instead

`compare.CHECKED_FIELDS` is the whole list of stored fields compared against the
registry, and it is deliberately short enough to state:

```
title, authors, year, container, volume, issue, pages, publisher
```

A stored field outside that tuple is never adjudicated: the DOI and the entry's
own type are inspected, but neither can produce a mismatch, and the rest are
read and left alone. Being explicit about that boundary is part of being honest
about what "verified" means.

The DOI is not on the list, and its absence is the point of this page in
miniature. The DOI is the lookup *key*, not a field with two independent
opinions to weigh: the registry record is in hand *because* the stored DOI
resolved to it. When the record's own DOI differs anyway, the cause is a
redirect or an alias — `doi.org` content-negotiates the JSTOR DOI to the
publisher's own — and that is reported as a `doi/alias` note so a reader who
looks the entry up by hand is not surprised by a different identifier. It can
never be a failure.

The three classes of the audit above map onto three verdicts, all defined in
full on [verdicts](verdicts.md):

| Class | Verdict | What made it visible |
|---|---|---|
| the work does not exist | `BAD-ID`, or `UNCONFIRMED` when no identifier is stored at all | no consulted registry holds the identifier — or, with none stored, no searched candidate cleared the bar below |
| a real work, corrupted fields | `FIELD-MISMATCH` | a field in the list above disagrees with a registry |
| a resolving DOI on the wrong paper | `WRONG-WORK` | the stored title does not describe the resolved record, and the author lists do not rescue it |

An entry that stores a DOI and no title cannot be caught in the third class —
there is nothing to compare the resolved record against. It does not pass
silently: the missing title is itself an error-severity finding, and the entry
reports `FIELD-MISMATCH`.

## The title bands

`WRONG-WORK` rests on a similarity score, so the cut-offs are published rather
than tuned in private. They live in `compare.Thresholds` and every one of them
is a score from `normalize.similarity`, which folds both strings — stripping
case, accents and punctuation — and then takes a `difflib.SequenceMatcher`
ratio. A token-set measure was rejected because word order carries meaning in
titles: *Effect of A on B* and *Effect of B on A* are different papers, and a
set-based measure scores them identically.

The best score against *any* consulted registry is the one used. PubMed and
Crossref differ systematically in case and markup, and an entry matching either
of them is describing the right paper.

| What is reported | Articles | Books and chapters |
|---|---|---|
| drift, at `info` — markup, a lost subtitle, an en-dash | 0.97 and above | 0.97 and above |
| drift, as a warning with the score printed beside it | 0.85 to 0.97 | 0.75 to 0.97 |
| the titles genuinely disagree: an error, so `FIELD-MISMATCH` | 0.55 to 0.85 | 0.45 to 0.75 |
| candidate `WRONG-WORK` — see below | below 0.55 | below 0.45 |

Books get the lower bar because book titles are recorded with far more variation
between registries: subtitles, edition statements and series names come and go.

Two titles that are identical once folded never reach the bands at all: a
difference only of glyphs or capitalisation is reported as `COSMETIC`, and a
difference a documented registry defect explains is recorded as a suppressed
registry artifact rather than as a defect in the bibliography.

## Why a low title score is not by itself `WRONG-WORK`

A single fuzzy number is not enough evidence to accuse a bibliography of
pointing at the wrong paper. So the finding is cross-checked against the author
list, which is compared in full — every position, not just the first, because
comparing only the first author cannot see an invented co-author. If the author
lists agree cleanly, the likelier explanation is a registry title defect —
Crossref sometimes registers a shortened title, and the catalogue of such
defects is in [registry defects](registry-artifacts.md) — and the verdict is
downgraded to `FIELD-MISMATCH`. `WRONG-WORK` is reported only when the title is
below the band *and* the author lists do not corroborate.

That downgrade is the third rule of this project applied to its most consequential
verdict: a false alarm costs more than a miss, and "this citation points at a
different paper" is the accusation most likely to be wrong about a bibliography
that is merely awkward.

The same instinct runs through the quieter checks, each of which exists to keep
the field matrix from generating noise that would make it unreadable:

- **Any date the registry itself carries is accepted.** An entry citing the
  online-first year of a work printed the following February is not wrong, so a
  year finding prints every date slot the registries hold rather than naming one
  "correct" year.
- **First page only.** Closing pages disagree constantly and harmlessly between
  registries.
- **Every container title the registries carry is accepted.** Crossref deposits
  both a series and a volume title for a book chapter, and a chapter citing
  either is citing a container Crossref named.
- **An entry type that contradicts the resolved work's type is a warning, not a
  failure.** A `@book` whose DOI resolves to a journal article is nearly always
  citing a *review* of the book — worth surfacing, not worth breaking a build.

## When there is no identifier to resolve

An entry carrying no identifier at all — no DOI, no PMID, no arXiv id, no ISBN —
cannot be checked by any resolver, which is the case a DOI check does not merely
miss but cannot address. bibaudit searches for it, and then holds every
candidate — whoever found it — to the same bar in `compare.confirm_without_id`:
a title similarity of at least 0.90, a first author that does not disagree, and
— where the entry and the candidate both carry a year at all — a year within one
of it, plus a work-type screen because searching for a book by title reliably
turns up reviews of it. A candidate with a matching title and no author or year
data whatsoever is refused outright, since there would be nothing left to
corroborate the title with.

If nothing clears that bar, nothing is confirmed. The entry reports `UNCONFIRMED`,
which means *needs review* and never *fabricated* — registry coverage has real
gaps, and [limits](limits.md) states which.

## What this still does not reach

Field-level checking is what makes the 27% and the 4% visible. It does not touch
the failure mode underneath all of them: whether the work being cited supports
the sentence it is attached to. No metadata check can reach that, every report says so, and
[limits](limits.md) states the boundary in full.
