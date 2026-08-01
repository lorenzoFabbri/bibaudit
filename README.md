# bibaudit

Checks that every reference in a bibliography **exists** and that **every stored
field matches the publisher's record** — title, every author, year, journal,
volume, issue, pages, publisher — against Crossref, DataCite, PubMed and, for
books, Open Library. Every reference that resolves is also checked for
retraction status against Retraction Watch's own export and PubMed's
expression-of-concern cross-reference, independently of whatever a publisher
happened to deposit with Crossref.

No language model is involved at any point. Every verdict is reproducible from
the cached registry response, and any of them can be re-derived by hand.

```console
$ bibaudit check references.bib
bibaudit — 438 references checked

FIELD-MISMATCH  (2)  right work, but stored metadata disagrees with the registry
    smith2019cohort  references.bib:214
      year/mismatch
        stored   2019
        crossref print=2021, online=2020

BAD-ID  (1)  the identifier resolves in no consulted registry
    jones2020method  references.bib:301
      doi/unresolved  (resolves in no consulted registry)
        stored   10.1016/j.jclinepi.2020.99999

summary
  OK                 361
  INCOMPLETE          74
  FIELD-MISMATCH       2
  BAD-ID               1
  errors by field      year=2, doi=1

FAIL — 3 reference(s) need attention
```

## Why not just check that the DOI resolves

Because that is exactly the check a wrong citation passes. Of confirmed
fabricated references in one audit of 53 published papers, 66% were works that
do not exist — a DOI check catches those — but 27% were **real works with
corrupted fields** and 4% were **valid, resolving DOIs attached to the wrong
paper**.[^taxonomy] Those two classes are invisible to every "does the DOI
resolve" tool, and they are the ones that survive review.

[^taxonomy]: Ansari, *Anatomy of a Fabricated Citation*, arXiv:2602.05930.

## Why it never rewrites your bibliography

Most tools in this space (`betterbib`, `bibcure`, `rebiber`, and the Zotero
metadata plugins) fetch the registry record and **overwrite your entry with it**.
That is not verification. It destroys the evidence that there was ever a
disagreement, and it assumes the registry is right — which it frequently is not:

- Crossref returns `GÃ³mez` for *Gómez* when a deposit was mis-decoded.
- *Environmental Health Perspectives* zero-pads article numbers (`027004`).
- Some deposits carry a doubled `do(x)do(x)` from mangled MathML.
- JSTOR DOIs content-negotiate to the publisher's own DOI — same work.
- Crossref sometimes registers a shortened title (Rubin 1986 as bare "Comment").

bibaudit reports the disagreement, names the likely cause where it recognises
one, and leaves the decision to you. `--suggest` can write a corrected copy
*beside* the original for you to diff, but the original is never touched.

## Install

```bash
uv tool install bibaudit          # or: pipx install bibaudit
```

From a checkout:

```bash
uv sync && uv run bibaudit --help
```

## Use

```bash
# A bibliography
bibaudit check references.bib

# Quarto or Obsidian notes: DOIs typed in prose and tables, plus every
# [@citekey] resolved against the bibliography
bibaudit check sources/ --bibliography references.bib

# A Zotero library, read-only
bibaudit check ~/Zotero/zotero.sqlite
bibaudit check local                 # a running Zotero, via its local API
bibaudit check library.json          # a CSL-JSON export

# Machine-readable, for CI or a dashboard
bibaudit check references.bib --format json --output audit.json
```

Useful flags: `--offline` (cache only — for reproducing an earlier run),
`--refresh` (ignore the cache), `--no-corroborate` (skip PubMed, halves request
volume), `--no-retraction-check` (skip the independent Retraction Watch /
PubMed expression-of-concern check — see [Retraction](#retraction) below),
`--no-isbn` (skip Open Library entirely), `--verbose` (show cosmetic and
informational findings), `--mailto you@example.org` (puts Crossref requests in
the polite pool; no account or key needed anywhere in this tool), `--suggest`
(propose fixes for missing fields — see below). Run `bibaudit check --help`
for the full list, including `--no-europepmc`/`--no-openalex` for the sources
consulted when confirming an entry that carries no identifier.

### Inputs

| Source | What is read |
|---|---|
| `.bib` | every entry and every field |
| `.qmd`, `.md`, `.rmd` | `[@key]`, bare `@key`, Obsidian's `[[@key]]` / `[[@key\|display]]`, YAML `nocite:`, and DOIs typed in prose or inline tables. Ordinary wikilinks (`[[Some Note]]`), embeds (`![[Some Note]]`), block references (`^block-id`) and tags (`#tag`) are Obsidian navigation, not citations, and are never read as one |
| `zotero.sqlite` | every item, read-only via an immutable URI |
| CSL-JSON | every item |
| `local` | a running Zotero, through its read-only local API |

A note's own `bibliography:` front matter is resolved against the note's
directory (Quarto's rule) unless the note sits inside an Obsidian vault (a
directory carrying `.obsidian` above it), in which case it resolves against
the vault root instead — matching how Obsidian citation plugins such as
obsidian-pandoc-reference-list interpret that path.

An entry's `isbn` field (BibTeX's `isbn`, Zotero's own field, CSL-JSON's
`ISBN`) is read as an identifier in its own right, checked against its ISO 2108
check digit and resolved through Open Library — the one registry in this tool
organised around books rather than DOIs. It is only ever consulted when no DOI
is stored: a reference carrying both is resolved through the DOI, the stronger
identifier of the two.

## Verdicts

| Verdict | Meaning | Fails CI |
|---|---|---|
| `RETRACTED` | the cited work has itself been retracted | yes |
| `BAD-ID` | the identifier resolves in no consulted registry | yes |
| `WRONG-WORK` | the identifier resolves, but to a different paper | yes |
| `FIELD-MISMATCH` | right work, stored metadata disagrees | yes |
| `UNCONFIRMED` | no identifier and no confident match — needs review | yes |
| `DISPUTED` | registries disagree with *each other* | no |
| `INCOMPLETE` | the registry holds fields the entry omits | no |
| `ADJUDICATED` | a difference this project's `.bibaudit.toml` decided to accept | no |
| `REGISTRY-ARTIFACT` | difference explained by a known registry defect | no |
| `TITLE-DRIFT` | wording differs, same work | no |
| `COSMETIC` | differs only in glyphs or capitalisation | no |
| `UNCHECKED` | no registry could be reached | no |
| `OK` | every checked field agrees | no |

`RETRACTED` means the *cited work* was retracted. A retraction **notice** — the
editorial statement itself — is an ordinary citable document and is never
reported as one; citing it deliberately, in a paper about a retraction, is
correct and the tool says nothing.

`ADJUDICATED` and `REGISTRY-ARTIFACT` are both non-failing and are deliberately
separate. The second says a registry defect documented in
[`docs/registry-artifacts.md`](docs/registry-artifacts.md) explains the
difference, and is settled for everybody. The first says somebody on *this*
project wrote a rule in `.bibaudit.toml` saying not to care — which rests on a
person's say-so and can go stale when a citekey is renamed or a bibliography is
re-exported. The summary counts them apart for the same reason.

Change what fails with `--fail-on RETRACTED,BAD-ID,WRONG-WORK`.

A registry outage is `UNCHECKED`, never a failure. A check that breaks the build
when Crossref has a bad afternoon is a check people learn to bypass.

### Retraction

Retraction status is the **union over every registry that answered**, not the
primary registry's opinion. Four sources carry it, the first two by whatever a
publisher chose to deposit and the second two independently of it:

- **Crossref**, through the `updated-by` relation, which includes whatever
  Retraction Watch linkage a publisher's own deposit agreed with closely
  enough for Crossref's pipeline to attach;
- **PubMed**, through MEDLINE's `PT - Retracted Publication`, curated by NLM
  independently of the publisher;
- **Retraction Watch's own export**, read directly rather than only as far as
  Crossref happens to surface it — a retraction Retraction Watch has logged
  but no publisher deposit ever linked is caught here, and would not be by
  the first bullet alone;
- **PubMed's `ECI` cross-reference** ("Expression of Concern In:"), which
  MEDLINE records on the *concerning* paper's own entry but never as the
  `PT` value the second bullet reads — closing a gap where NLM knows about a
  concern and the tool, until this was added, did not.

Checked for every reference that resolves, however it resolved — by DOI, by
ISBN, or by a title/author search that confirmed one. A retraction **either
one source records alone is still reported**, and the finding names which one
— "recorded by pubmed and not by crossref, which answered for this work and
carries no retraction linkage" is a different message from "recorded by
crossref, pubmed", and the first is also a bug report for the publisher. An
expression of concern is reported too, under its own heading and never under
the word *retracted*: the work stands, and citing it is legitimate once the
notice has been read.

If a registry that carries the signal could not be reached, the report says so
beside the banner — *retraction status not corroborated for N reference(s)*.
Silence from a registry nobody could reach is not a clean bill of health.

**Coverage is still not complete**, and reading a clean result as proof
nothing here was ever retracted overstates what was checked. Crossref's and
PubMed's own flags depend on a linkage having been deposited at all.
Retraction Watch's database is community-maintained, not exhaustive, and its
export is refetched at most every seven days, so a retraction logged there in
the last few days may not yet be reflected. `--no-retraction-check` turns the
independent pair off; a Crossref or PubMed record that itself carries a
retraction linkage still fails regardless.

### What `consulted` records

Each result's `consulted` map says what each registry contributed, in three
states rather than a yes/no:

| | |
|---|---|
| `answered` | queried and replied — including an authoritative "I do not hold this DOI", which is the evidence that makes `BAD-ID` a fact |
| `unreachable` | queried and could not reply: a timeout, a run of 5xx. Ignorance, never absence |
| `not-asked` | never queried — `--no-corroborate` skips PubMed entirely, and DataCite is only asked about DOIs Crossref did not answer for |

It used to be a bool computed as "not known to be unreachable", so a run with
`--no-corroborate` reported `"pubmed": true` on every reference in the file.
`unreachable` is still run-wide rather than per-reference: a registry that fell
over while a *different* entry was being resolved is reported `unreachable` here
too. That is pessimistic on purpose — it is the same set the `UNCHECKED` verdict
is derived from, so the map always explains the verdict beside it.

## Suppressing an adjudicated difference

When you have read the paper and concluded the registry is wrong, record it in
`.bibaudit.toml` beside the bibliography:

```toml
[[ignore]]
key    = "papantoniou2017colorectal"
field  = "authors"
reason = "Crossref returns mojibake surnames; checked against the PDF 2026-07-30"
```

A `reason` is required, and suppressed differences are still counted in the
summary — the report always states how much is being taken on trust. An entry
silenced this way reports `ADJUDICATED`, not `REGISTRY-ARTIFACT`: the tool will
not let one project's decision read as a documented defect of the registry.

## Proposing fixes with `--suggest`

```bash
bibaudit check references.bib --suggest
```

For every `.bib` that has at least one fillable gap, this writes two files
*beside* it — `references.suggested.bib` and `references.suggested.diff` —
and never opens `references.bib` itself for writing. Only two things ever go
into the suggested copy:

- a field the entry has **no value for at all**, where a consulted registry
  supplied one (an `INCOMPLETE` entry's `missing` fields);
- a proposed DOI for an entry that had none, confirmed by title, author and
  year corroboration.

A field where the stored and registry values *disagree* is never touched —
that is exactly the case this tool exists to surface to a human, not resolve
on its own — and neither is anything suppressed or explained as a known
registry defect (`REGISTRY-ARTIFACT`); both are excluded before `--suggest`
ever sees them. The author list is also never filled in, even when it is
entirely missing: the report's own `authors` value is truncated to the first
three creators for display, and writing that into a `.bib` file would present
an incomplete list as a complete one.

The suggested file opens with a comment stating it is generated, which
registry each value came from, and that it must be reviewed before use. Read
the diff; apply by hand only what you have checked against the registry
yourself.

## In CI

```yaml
- run: uv tool install bibaudit
- run: bibaudit check references.bib --mailto ${{ secrets.CONTACT_EMAIL }}
```

Or as a `make` target:

```make
verify-refs:
	bibaudit check references.bib sources/
```

## What this does not do

**bibaudit cannot tell you whether a cited work supports the claim it is
attached to.** That is the failure mode no metadata check can reach, and it
requires reading the paper. Every report says so.

It also cannot prove a work does not exist — registry coverage has real gaps,
particularly for pre-1990 work, grey literature and non-English publishing. A
book with an ISBN is resolved through Open Library, whose catalogue is
crowd-sourced and noticeably patchier than Crossref's: many records carry a
title and nothing else, which is why a thin record can never *by itself*
confirm a book that has no identifier at all — see
`docs/registry-artifacts.md`. An entry nothing can confirm is reported as
`UNCONFIRMED`, meaning *needs review*, never *fabricated*.

## How this was built

bibaudit was written with [Claude Code](https://claude.com/claude-code) — the
implementation, the 1186-test suite, and the adversarial review passes that
found most of the defects it now guards against, including the ones described
above.

That is worth stating precisely, because this tool's first rule is that **no
language model is in the verdict path**. Those are different claims: a model
helped write the comparison rules, and no model evaluates one.

Every registry answer is saved to the cache verbatim — the URL that was asked,
the timestamp, and the response body exactly as it arrived. The verdict is then
computed from that stored response alone: fold the two titles, compare the first
page, walk the author lists. Nothing in that path opens a socket or calls a
model, which is what `CLAUDE.md`'s *comparison never performs I/O* enforces.

So "re-derivable" is meant literally. If bibaudit reports a page mismatch, you
can open the cache file, read the `page` field Crossref actually returned, and
check the conclusion yourself — today, or in ten years, with no API key and
nothing to run. That property is what the rule protects, and it does not depend
on how the code was written.

The rules the work was held to are in [`CLAUDE.md`](CLAUDE.md).

## Licence

MIT.
