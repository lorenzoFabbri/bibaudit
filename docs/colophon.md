# How this was built

bibaudit was written with [Claude Code](https://claude.com/claude-code) — the
implementation, the 1,204-test suite, and the adversarial review passes that
found most of the defects it now guards against.

That is worth stating precisely, because this tool's first rule is that **no
language model is in the verdict path**. Those are two different claims:

- a model helped write the comparison rules;
- no model evaluates one.

The first is a fact about who wrote this repository, and you have only the
author's word for it. The second is a property of every run, and unlike the
first it is something you can check for yourself.

## What the second claim rests on

Every registry answer is saved to the cache verbatim: one JSON file per lookup,
holding exactly three things.

- `fetched_at` — when the answer arrived, in UTC.
- `url` — the request that produced it, in full.
- `payload` — what came back, whole: a JSON body parsed and written back out, a
  non-JSON one (PubMed's XML, Retraction Watch's CSV) kept as text under a
  `text` key, and a confirmed 404 stored as a marker object so that absence
  replays too. Nothing between the wire and the file selects, summarises or
  rewrites a field.

The verdict is then computed from that stored response alone: fold the two
titles, compare the first page, walk the author lists. Nothing on that path
opens a socket or calls a model, which is what `CLAUDE.md`'s *comparison never
performs I/O* enforces. The modules that decide a verdict — `compare.py`,
`normalize.py`, `names.py`, `benign.py` — import nothing but the standard
library and one another: `re`, `html`, `unicodedata`, `difflib`, `dataclasses`,
the typing machinery, and the module holding the data contracts. The whole
package has two runtime dependencies, a BibTeX parser and a fuzzy string
matcher; even the HTTP client is `urllib` from the standard library. There is
no client for a model to be reached through, and adding one would be visible in
a one-line diff to `pyproject.toml`.

## "Re-derivable" is meant literally

If bibaudit reports a page mismatch, you can open the cache file, read the
`page` field Crossref actually returned, and check the conclusion yourself —
today, or in ten years, with no API key and nothing to run.

```bash
bibaudit cache info
```

That prints the directory. Inside it, records are named for the SHA-256 of the
lookup key and sharded into two-character subdirectories, so a bibliography
with thousands of DOIs never puts thousands of files in one directory. The
filename therefore tells you nothing: you find the record you want by searching
the contents, and each one names the URL it came from.

`--cache-ttl` governs whether bibaudit will still *reuse* a record as a live
answer — 90 days by default. It does not govern whether you can read it.
Nothing expires a file off the disk. An expired record is one bibaudit will
refetch rather than replay, and it stays exactly as readable to a person as the
day it was written.

The same instinct runs further down than it strictly needs to. The jitter added
between retries is derived by hashing the request URL rather than drawn at
random, because a fixture that produces a different sleep on every run
contradicts a tool whose results are supposed to follow from registry responses
alone. Nothing about a verdict depended on it; it was made deterministic
anyway, on the principle that a reproducibility rule with one exception in it
is a rule nobody can state.

## What that settles, and what it does not

It settles who computes a verdict. A rule written with a model's help and a
rule written by hand are checked in exactly the same way — against the stored
response, by whoever wants to check it. That property is what the first rule
protects, and it does not depend on how the code was written.

It does not settle whether the rules are right. A comparison rule can be wrong
in both directions: too strict and it manufactures a mismatch, too loose and it
clears one. What the cache gives you is the ability to find out which, from the
same evidence the verdict was computed on, rather than being asked to trust the
tool. Where a rule is deliberately loose, the reason is written up in
[registry defects](registry-artifacts.md), which exists so a
`REGISTRY-ARTIFACT` line can be argued with.

And it says nothing at all about the questions in [Limits](limits.md). No
amount of determinism makes a metadata check able to tell you whether a paper
supports the sentence it is cited for.

## The rules the work was held to

They are in the repository's `CLAUDE.md`, which is a set of constraints rather
than a style guide: no model in the verdict path, report and never rewrite, a
false alarm costs more than a miss. Below those sit the specific ones that
exist because getting them wrong was possible — a 404 is a fact and a timeout
is ignorance, `updated-by` means this work was retracted while `update-to`
means this work is the notice, never reduce a surname to its last token.

Two of those constraints are enforced by the test suite rather than by good
intentions. Adding a rule that suppresses a difference as a known registry
defect, without a section naming it in [registry defects](registry-artifacts.md),
turns a test red, because a suppression a reader cannot look up is one nobody
can challenge. And the default test run deselects anything networked: 1,193 of
the 1,194 tests run and pass with no internet at all, and the one that does not
run is the `network`-marked test that gives the deselection something to filter.
A suite that needs the network gets skipped, and a skipped suite protects
nobody.
