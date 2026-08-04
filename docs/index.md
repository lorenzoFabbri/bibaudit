# Home

bibaudit checks that every reference in a bibliography **exists**, and that
**every field stored for it matches the publisher's record** — title, every
author, year, journal, volume, issue, pages, publisher — against Crossref,
DataCite, PubMed and, for books, Open Library. Every reference that resolves **to
a DOI** is also checked for retraction, against Retraction Watch's own export and
PubMed's expression-of-concern cross-reference, independently of whatever a
publisher happened to deposit with Crossref — a book matched by ISBN alone is
not, because Open Library mints no DOI for those sources to be keyed on. It reads
`.bib` files, Quarto and Obsidian notes, Zotero libraries and CSL-JSON.

**No language model is involved at any point.** Every verdict is reproducible
from the cached registry response, and any single finding can be checked by hand
against the record it came from. `--offline` never touches the network: a cached
answer is replayed, and anything uncached or past its cache lifetime is reported
`UNCHECKED` rather than guessed at. Registry answers are cached for `--cache-ttl`
days — 90 by default — and the Retraction Watch index for seven, so a replay
inside those windows re-derives the original report exactly, and one outside them
re-derives an honestly narrower one.

**Nothing is ever written to your bibliography.** Not the `.bib`, not the
`.qmd` or `.md`, not the Zotero database — which is opened read-only, because
it is your live library and Zotero may be writing to it. bibaudit reports the
disagreement and leaves the decision to you.

## A run

Three entries: one whose stored fields all agree but which omits a volume PubMed
holds, one carrying a year no registry holds, and one whose DOI resolves nowhere.

```console
$ bibaudit check references.bib
bibaudit — 3 references checked

BAD-ID  (1)  the identifier resolves in no consulted registry
    jones2020method  references.bib:23
      doi/unresolved  (resolves in no consulted registry)
        stored   10.1016/j.jclinepi.2020.99999
        registry 

FIELD-MISMATCH  (1)  right work, but stored metadata disagrees with the registry
    beral2003breast  references.bib:11
      year/mismatch
        stored   2004
        crossref issued=2003, print=2003, pubmed:issued=2003

summary
  BAD-ID             1
  FIELD-MISMATCH     1
  INCOMPLETE         1
  errors by field    year=1, doi=1
  suppressed         1  (1 registry defect(s))  (--show-suppressed to list)

FAIL — 2 reference(s) in the failing set
bibaudit verifies that each reference exists and that its stored metadata matches the publisher's record. It does not and cannot verify that a cited work supports the statement it is attached to — that requires reading the paper.
```

What is in that report, and why:

- `beral2003breast` is the **right work** — the DOI resolves, the title agrees
  — and the stored year is one no registry carries. Every date slot every
  registry does carry is printed rather than a single "correct" year, because
  an entry citing the online-first date when Crossref also holds a print date
  is right, and reporting it would be a false alarm.
- `jones2020method` is `BAD-ID`: the DOI is well-formed and no registry that
  answered holds it. That is a fact about an answer, not a network failure. A
  registry that could not be reached leaves the reference `UNCHECKED`, never
  `BAD-ID` — confusing ignorance with absence is the one way this tool could
  accuse a real paper of not existing.
- The third entry is `INCOMPLETE` and appears only in the summary: PubMed holds
  a volume the entry omits. Nothing to decide, so nothing is printed by default.
  Run with `--verbose` to see it.
- `suppressed 1` is the Million Women Study entry, which stores the consortium
  as one collective author while Crossref splits the byline into people. That is
  a representation difference, not a defect, so it does not fail — but it is
  still counted, because a report that quietly drops what it decided not to tell
  you is not an audit. `--show-suppressed` prints it with both values.
- Every report ends with the sentence about what was *not* checked. It is not
  boilerplate to be skipped; see [what it cannot tell you](#what-it-cannot-tell-you).

The run exits `1`. Exit `2` means the tool could not run at all — bad
arguments, an unreadable input, a broken config. A registry outage is neither:
see [in CI](ci.md).

## Install

```bash
uv tool install git+https://github.com/lorenzoFabbri/bibaudit
```

From a checkout:

```bash
uv sync && uv run bibaudit --help
```

bibaudit is not on PyPI yet, so `uv tool install bibaudit` and
`pipx install bibaudit` will work once 0.1.0 is released and not before.

Then point it at something:

```bash
bibaudit check references.bib
```

## What it cannot tell you

bibaudit verifies **metadata, not argument**. It can tell you that a DOI
resolves to a different paper than the one stored beside it. It cannot tell you
whether that paper supports the sentence the citation is attached to. That is
the failure mode no metadata check can reach, and it requires reading the paper.

It also cannot prove that a work does not exist. Registry coverage has real
gaps — pre-1990 work, grey literature, non-English publishing, and books, whose
Open Library records are crowd-sourced and frequently carry a title and nothing
else. An entry that nothing can confirm is reported `UNCONFIRMED`, which means
*needs review*, never *fabricated*.

!!! warning "A passing report is a narrower claim than it looks"

    `OK`, the per-reference verdict, means every field bibaudit could check
    agreed with a registry that answered. `PASS`, the line at the end of the
    run, is weaker than that and is not a verdict at all: it means no reference
    fell into the *failing set*. `INCOMPLETE`, `DISPUTED` and `UNCHECKED` are
    all outside that set, so a run in which no registry could be reached reports
    every entry `UNCHECKED` and still prints `PASS`, and still exits 0 — because
    an outage is not a defect in your bibliography and must not break your build.
    Read the summary counts, not the last line.

    Neither word means the references support the claims they are attached to,
    and neither means nothing in the file was ever retracted — retraction
    linkage has to have been deposited or logged somewhere before anyone can
    find it. [Limits](limits.md) states the boundary in full.

## Start here

- [Why field-level](why.md) — why "the DOI resolves" is precisely the check a
  wrong citation passes.
- [Inputs](inputs.md) — what is read out of `.bib`, Quarto and Obsidian notes,
  Zotero and CSL-JSON, and what is deliberately not read as a citation.
- [Verdicts](verdicts.md) — all thirteen, and which ones break a build.
- [Command line](cli.md) — every flag and what it costs to turn off.
- [Retraction](retraction.md) — the four sources, why they are consulted as a
  union, and what a clean result does not establish.
- [Limits](limits.md) — the boundary, stated plainly.

The rest: [in CI](ci.md), [registry defects](registry-artifacts.md),
[adjudicating a difference](suppressions.md) and
[proposing fixes](suggest.md).

## The three rules

1. **No model in the verdict path.** Not as a fallback, not for the hard cases,
   not behind a flag. A verdict must be reproducible from the cached registry
   response by anyone, indefinitely. Where a check cannot be made deterministic,
   the tool reports the uncertainty instead of resolving it.

2. **Report, never rewrite.** `betterbib`, `bibcure`, `rebiber` and the Zotero
   metadata plugins fetch the registry record and overwrite your entry with it.
   That is not verification: it destroys the evidence that there was ever a
   disagreement, and it assumes the registry is right, which it often is not —
   see [registry defects](registry-artifacts.md). bibaudit has no authority to
   decide which side is wrong. [`--suggest`](suggest.md) writes a *separate*
   file for you to diff; the original is never opened for writing.

3. **A false alarm costs more than a miss.** A report full of noise stops being
   read, and an unread report still looks like assurance. Every check ships with
   its known-benign exceptions — the collective author above is one — or it does
   not ship.

Licensed MIT.
