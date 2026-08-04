# Adjudicating a difference

Sometimes the registry is wrong. You fetch the PDF and the volume Crossref
deposited is not the one on the paper. Sometimes the difference is deliberate —
a house style for publisher names, a container title your field writes
differently. bibaudit can settle neither case: it has no authority to decide
which side is right, and no way to know that you went and looked.

What it can do is let you record the decision in a file that travels with the
project. `.bibaudit.toml` holds the differences *this* bibliography has
adjudicated. Two properties keep that from becoming a way to hide problems: a
suppression must carry a `reason`, and a suppressed difference is moved into a
separate list rather than deleted, so the report can always state how much of
the bibliography is being taken on trust.

## Where the file is found

The search starts at the first path given on the command line — the directory
itself, or the directory containing the file — and walks upward. The first
directory holding a `.bibaudit.toml` wins. The walk stops at a directory
containing `.git`, or at the filesystem root, so a suppression file in an
unrelated parent project cannot silence findings in this one. The `.git` check
happens *after* the config in the same directory, which is what lets the normal
case work: `.bibaudit.toml` beside `references.bib` at the repository root.

## The form

```toml
[[ignore]]
key    = "papantoniou2017colorectal"
field  = "authors"
reason = "Crossref returns mojibake surnames; checked against the PDF 2026-07-30"

[[ignore]]
key    = "*"
field  = "publisher"
reason = "publisher names churn with imprint mergers; not tracked here"
```

Four keys are allowed inside an `[[ignore]]` table: `key`, `field`, `kind` and
`reason`. Anything else is refused by name, because a typo in this file removes
findings rather than adding them — write `fields = "volume"` and `field` falls
back to its default of `"*"`, silencing every difference on that entry, with
nothing downstream to show that it had. Only the `[[ignore]]` array is read; a
table under some other name is not an error and silences nothing.

## A `reason` is required

An unexplained suppression is a finding deleted by someone unknown. A rule
without a `reason`, or with one that is only whitespace, is refused — and one
bad entry voids the whole file rather than loading the rest, because a partially
loaded suppression file is a bibliography checked under rules nobody wrote down.

Every way this file can be wrong ends in a single sentence naming the file,
never a traceback: broken TOML, `[ignore]` written for `[[ignore]]`, a scalar,
an entry that is not a table, a file that is not UTF-8. A fault inside one entry
names its position as well — `.bibaudit.toml: [[ignore]] #2 has no 'reason'`;
the faults that make the file as a whole unreadable can only name the file. The
run exits `2` — the tool could not run — before anything is checked against a
registry. An empty file, or one with only comments, is valid and silences
nothing.

Put the evidence in the `reason`, and date it. It is the only thing a reader —
including you, two years on — has to judge the decision by.

## `key`, `field` and `kind`

All three default to `"*"` and all three are shell-style globs:

| key | what it matches |
| --- | --- |
| `key` | the citekey. `"epic*"` matches `epic2019diet`; `"*"` applies the rule across the whole bibliography |
| `field` | the field the difference is in — `title`, `authors`, `year`, `container`, `volume`, `issue`, `pages`, `publisher`, `kind`, `doi`, `identifier`, `status` |
| `kind` | the sort of difference — `mismatch`, `missing`, `drift`, `cosmetic`, and the rest |

Both halves are printed in the report as `field/kind` on the line above the two
values, so the pair to write is the pair you can read off the finding — as it
reads *before* it is suppressed. A rule is matched against the original `kind`;
the `suppressed:` prefix in the listing below is stamped on afterwards, and is
never what a rule names.

`kind` is what narrows a rule to the difference you actually adjudicated:
`field = "pages"` alone also silences an entry that has *no* pages, and
"the registry's page range is wrong" is not the same claim as "this entry needs
no pages". Add `kind = "mismatch"` when that is what you meant.

Matching is case-sensitive on every platform. `fnmatch` folds case through
`os.path.normcase`, which is a no-op on POSIX and `str.lower` on Windows, so a
rule written for `Smith2020` would have silenced `smith2020` on one machine and
not the other — and two machines that disagree about whether a bibliography
passes is not a reproducible verdict. Rules are tried in file order, and the
first one that matches a difference is the one whose `reason` is attached to it.

## What the report does with it

The silenced difference keeps both values and gains the reason. `--show-suppressed`
prints it under the entry, marked with `~`:

```console
$ bibaudit check references.bib --show-suppressed
bibaudit — 1 references checked

ADJUDICATED  (1)  a difference this project's .bibaudit.toml decided to accept
    papantoniou2017colorectal  references.bib:41
      ~ volume/suppressed:mismatch  (Crossref has the wrong volume; checked against the PDF 2026-07-30)
      ~   stored   186
      ~   crossref 185

summary
  ADJUDICATED        1
  suppressed         1  (1 adjudicated here)

PASS — no reference in the failing set
bibaudit verifies that each reference exists and that its stored metadata matches the publisher's record. It does not and cannot verify that a cited work supports the statement it is attached to — that requires reading the paper.
```

Without that flag the entry is not printed — there is nothing to act on — but
the count stays, as
`suppressed         1  (1 adjudicated here)  (--show-suppressed to list)`. The
summary line always splits the total into registry defects and adjudications
made here, because those are different amounts of assurance and a single number
let the second hide inside the first. The JSON report carries the same split as
`registry_artifacts` and `adjudicated`, and every suppressed difference appears
in full under its entry's `suppressed` list.

## `ADJUDICATED`, never `REGISTRY-ARTIFACT`

An entry silenced by `.bibaudit.toml` reports [`ADJUDICATED`](verdicts.md).
A difference explained by a [documented registry defect](registry-artifacts.md)
reports `REGISTRY-ARTIFACT`. These were one verdict, and merging them made two
incompatible claims indistinguishable: "the registry is known to be wrong here,
reproducibly, and here is the write-up" versus "somebody on this project wrote a
rule saying not to care". The first needs no reader. The second rests on a
person's say-so and can rot, so it ranks higher, prints first of the two, and is
coloured amber where the registry defect is green.

Neither verdict is in the default failing set. `--fail-on` can name
`ADJUDICATED` if you want a build to stop until the file is re-reviewed.

## What a rule cannot silence

A retraction. The verdict is re-derived after suppression by the same rule that
produced it, and `RETRACTED` is returned before anything else is considered — so
even `key = "*"`, `field = "*"` leaves a retracted citation failing. No
project-local decision makes a retracted paper safe to cite.

Findings on the same entry that the rule does not name also stand: silencing
`volume` leaves a title mismatch beside it a `FIELD-MISMATCH`, and adjudicating
one field never changes what the tool concludes about another.

!!! warning "A wildcard field is broader than it looks"

    A wildcard `field` matches every difference, and the identifier findings are
    differences too — they are recorded against `doi` and `identifier`. A rule
    with `field = "*"` on an entry whose DOI resolves in no registry moves that
    finding out of the way, and the entry reports `ADJUDICATED` instead of
    `BAD-ID`. Name the field you adjudicated.

## A rule can go stale

Rename a citekey, re-export the bibliography from Zotero with a different key
scheme, or wait for the registry to fix its record, and a rule stops matching
anything. The report looks exactly the same as the day the difference was
adjudicated — the difference is simply no longer being found.

bibaudit tracks which rules have silenced something and exposes the ones that
have not, but `bibaudit check` does not currently print them, and the report
does not name the `.bibaudit.toml` it loaded. So nothing in a run will tell you
that a rule went stale. Treat the file as something to re-read, keep the rules
narrow, and keep the date in every `reason`.

## With `--suggest`

A suppressed difference has been moved out of the entry's findings before
[`--suggest`](suggest.md) ever sees it, so it is never proposed as a fix in the
suggested copy of a `.bib`. Adjudicating a `missing` field means `--suggest`
will stop offering to fill it.
