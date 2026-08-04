# Inputs

bibaudit reads bibliographies, Quarto and Obsidian notes, and Zotero libraries,
and it reads all of them the same way: open, extract, close. No input is ever
written back to; [`--suggest`](suggest.md) writes a separate file beside it, for
you to diff.

| Source | What is read |
|---|---|
| `.bib`, `.bibtex` | every entry, and every field of every entry |
| `.qmd`, `.md`, `.markdown`, `.rmd` | `[@key]`, bare `@key`, Obsidian's `[[@key]]`, YAML `nocite:`, and DOIs typed into prose or into an R table |
| `zotero.sqlite` | every citable item of the personal library, read-only through an immutable URI |
| `.json` | a CSL-JSON export, or a dump of Zotero's own item JSON — told apart by their fields, not by their filename |
| `local` | a running Zotero desktop client, through its read-only local API |

A directory argument is walked for the bibliography and document extensions
only — a Zotero library is read when you name it, or when the directory you
named is itself a Zotero data directory, meaning it contains a `zotero.sqlite`.
Three kinds of path are skipped during the walk: anything with a dot-prefixed
component *relative to the directory you named*, so a vault that lives in
`~/.notes` is still read in full; the generated trees `_site`, `_freeze` and
`node_modules`; and any `*.suggested.bib` a previous [`--suggest`](suggest.md)
run left beside its input. A file named explicitly whose extension matches none
of the above is a usage error and exits 2; a `*.suggested.bib` named explicitly
is read, which is what someone reviewing a suggestion wants.

## `.bib`

Every entry is parsed, and every field is kept. `title`, `author` (or `editor`),
`year` (or `date`), `journal` or `booktitle`, `volume`, `number`, `pages`,
`publisher`, `doi`, `isbn` and `url` are read into the reference bibaudit
checks; the entry's complete field dictionary is carried along beside it
untouched, in the file's own capitalisation, so a report can show you what was
actually written.

- **Authors.** The author field is never split naively on `" and "` — `{The
  Endogenous Hormones and Breast Cancer Collaborative Group}` is one author,
  and splitting it is the most reliable way to invent an author-list mismatch.
  BibTeX's `and others` is recognised as *et al.*, not as a person named
  "others", and it truncates the comparison rather than being counted as a
  name. An entry with no `author` at all but with an `editor` — an edited
  volume cited by its editors is correct BibTeX, not a defect — uses the
  editors, and records that it did so.
- **DOIs.** The dedicated `doi` field wins over a DOI buried in `url`. Crossref's
  own BibTeX export writes the same DOI into both on many entries, differing
  only in case, and preferring one means that difference is never even
  evaluated. A placeholder some reference managers write into `doi` — `N/A`,
  `TBD`, a pair of braces that survives as whitespace — is recognised as
  not-a-DOI and the `url` is tried instead, rather than being normalised into a
  confident-looking fake that a registry would then report as a bad identifier.
- **Year.** `year` is used, falling back to `date` — the field Better BibTeX and
  CSL-flavoured `.bib` files write instead.
- **Locators.** Findings point at `references.bib:412`. The line index comes
  from a brace-balanced scan, not a flat search, because an `@article{other-key,`
  quoted inside another entry's title would otherwise be indexed as `other-key`'s
  line and send the report to the wrong place for the real entry.

Two defects in the file itself are reported as warnings rather than swallowed.
A **duplicate citekey** means the second entry never reaches the audit at all —
saying nothing would return 437 references from a 438-entry file with no
indication why. A **block that fails to parse** is named with its file, line and
reason. Separately, the end of a text report counts probable *accidental*
duplication: two citekeys sharing a DOI, one citekey appearing twice across
merged sources, and pairs of near-identical titles.

## Quarto and Obsidian notes

A note contributes three things: the citekeys it cites, any DOI typed directly
into it, and the bibliography those citekeys are checked against.

Recognised as a citation:

- `[@key]`, including citation lists (`[@a; @b]`) and pandoc's
  suppress-author form `[-@key]`
- a bare `@key` outside brackets — at the start of a line, or after whitespace,
  which is how a citation written into a markdown table cell appears
- Obsidian's Citations-plugin wikilink, `[[@key]]` and `[[@key|display text]]`
- every key in a front-matter `nocite:` field, whether written as a YAML block
  scalar or inline

Quarto cross-references share the `@key` syntax and are excluded by prefix:
`@fig-`, `@tbl-`, `@sec-`, `@eq-`, `@lst-`, `@thm-`, `@def-`, `@exm-`, `@exr-`,
`@cor-`, `@lem-`, `@prp-` and `@cnj-` name a figure or a table, not a work. An
`@` preceded by a word character is not read as a citekey either: that is an
email address.

DOIs are read out of prose, and out of fenced code blocks — including the two
shapes an R publications table takes, `read.delim(text = "...")` and
`data.frame(... DOI = c(...))`, where a Year, Author or Journal column beside
the DOI column is read too and paired with it. The two shapes pair differently.
In a `read.delim` literal the pairing is by row, so a row too short to reach the
DOI column is not read as a table row at all and any DOI in it survives only as
a bare DOI from the chunk's generic sweep. In a `data.frame` call the columns
are separate vectors and the pairing is positional, so it is only done when
every sibling vector has exactly the same length as the DOI vector: one short
vector means position *i* no longer lines up, and the whole call falls back to
DOI-only references — each one carrying a note of which vector mismatched —
rather than filing one paper's year under another paper's DOI.

Two exclusions worth knowing. A citekey inside an inline code span —
`` `[@citekey]` `` in documentation about citing — is not a citation, while a
DOI inside one still is; a code span is how you write *about* a citation, but a
DOI is a DOI wherever it is written. And front matter is not scanned for prose
citekeys or DOIs; only its `nocite:` and `bibliography:` fields are read.

### What is deliberately not read as a citation

Obsidian's other constructs are navigation. They are not near-misses that a
better regex would catch — they address notes, sections and topics inside a
vault, and none of them names a bibliography entry:

- ordinary wikilinks: `[[Some Note]]`, `[[Some Note|display]]`, `[[#Heading]]`
- embeds: `![[Some Note]]`, and `![[@key]]` too — an embed transcludes a note's
  content, it does not cite a work
- block references: `^block-id`
- tags: `#tag`, `#nested/tag`

Wikilinks and embeds are blanked before the generic citekey scan runs, because
a vault that also uses `@name` for people would otherwise have a note title
picked up as a citekey. Block references and tags need no such handling: neither
can contain the `@` a citekey requires, so there is no failure mode to prevent.
The omission is deliberate, not an oversight.

### Where the bibliography comes from

A citekey is only checkable against a bibliography, and the path to that
bibliography is resolved with the rule of whichever tool the note belongs to.

A note's own front-matter `bibliography:` is resolved against **the note's own
directory** — Quarto's rule — unless the note sits inside an Obsidian vault,
meaning a directory carrying a `.obsidian` folder somewhere above it. Then it
resolves against **the vault root** instead, because Obsidian citation plugins
such as obsidian-pandoc-reference-list take that path as vault-relative. A vault
declaring `bibliography: material/refs.bib` identically in a note two folders
deep and one three folders deep is only consistent under the vault-relative
reading; resolving against the note's own directory would send the deeper one
looking for a file that is not there.

A note with no front-matter `bibliography:` gets the one declared in the nearest
`_quarto.yml` found by walking upward, resolved against that file's directory.
The walk stops at the first `_quarto.yml` whether or not it declares one, since
that file marks the project root and a project has no parent to inherit from.
Scalar, flow-list and block-list forms are all accepted. `--bibliography` (or
`-b`, repeatable) adds one explicitly.

When a bibliography was read, every citekey that resolves to no entry in it
makes the run exit 1 — in either output format, since an unresolved key is a
build failure waiting to happen whichever way you asked for the report. The
text report lists each one with where it was cited. Entries never cited are
listed too, as housekeeping, and fail nothing. `--no-citekey-check` turns both
off.

## Zotero

### `zotero.sqlite`

The database is opened as `file:...?immutable=1`, and a `PRAGMA query_only = 1`
is issued on the connection besides. This is your live library, and Zotero may
be writing to it while bibaudit reads — this is the one file this tool touches
that belongs to another application, so it gets belt and suspenders: the URI
opens it as an unchangeable, read-only file, and the pragma makes a bug that
ever slipped a write statement into that module fail loudly rather than corrupt
a library.

If a `-wal` or `-journal` sidecar exists, Zotero is mid-write and the main file
may hold a torn page, so a copy is made in a temp directory and *that* is read.
You get a warning saying so. The copy is not a synchronised point-in-time backup
— no WAL checkpoint is forced — only a guarantee against reading a database
while it is being written.

What is read: every item that is not an `attachment`, a `note` or an
`annotation`, since a library with one article and three attached PDFs is one
citation, not four. Trashed items are excluded. Field IDs and item-type IDs are
resolved **by name** at read time, never hardcoded, because Zotero renumbers
them across schema migrations and a library synced since 2015 has been through
several.

Only the personal library — "My Library" — is read. Item keys and collection
names are unique only *within* a library, so merging a group library into the
personal one would file one library's work under another's key. Group libraries
that hold readable items are named in a warning rather than silently dropped,
because "scoped away" and "nothing there" otherwise produce the identical empty
report. To check one, export it from Zotero as CSL JSON and pass the file.

### `local`

`bibaudit check local` talks to the Zotero desktop client's built-in HTTP API on
`127.0.0.1:23119`, the same one Zotero's own browser connector uses. It is a
read replica: it has no item-creation or item-modification routes, which is what
makes calling it compatible with never writing to your library. Requests carry
the `Zotero-Allowed-Request` header Zotero requires, results are paged, and the
same non-bibliographic item types are skipped as on the SQLite path. This route,
too, sees only the personal library — the local connector exposes nothing else.
If Zotero is not running, or the "allow other applications to communicate with
Zotero" setting is off, you get an error naming both, not an empty library.

### CSL-JSON

There is no separate CSL-JSON adapter: a `.json` path goes to the Zotero
adapter, which decides what it is holding by looking at the items themselves. An
item carrying `itemType` is Zotero's own item JSON — the shape its API and its
connector return; CSL calls that same concept `type` and never has an
`itemType`, which makes it a safe discriminator, since a false positive would
require a CSL producer to invent a field the CSL spec forbids. Reading one shape
as the other would not fail loudly, it would produce items with every field
empty, which is exactly the outcome worth spending a discriminator on.

From a CSL item, bibaudit reads `DOI`, `ISBN`, `URL`, `title`, `container-title`,
`volume`, `issue`, `page`, `publisher`, and the year out of `issued` — from
`date-parts`, or from the `raw`/`literal` forms a processor falls back to when
it could not decompose a date. `author` is used when present and `editor`
substituted when it is not, the same rule the other two Zotero paths apply, and
a creator written as a CSL `literal` stays one collective author rather than
being torn into a given and a family name. A bare top-level array is the usual
shape; `{"items": [...]}`, as some export tools write, is accepted too.

Two things only a live source can do, because an export does not carry what they
need. Filtering to a named collection: an export stores collections as opaque
keys with no accompanying name table, so a name cannot be resolved from the file
alone. And telling a trashed item from a live one: an export holds whatever was
in the library when it was made, with no per-item flag left to read afterwards.
Both are options on `read_zotero()`, the Python entry point behind all three
Zotero routes, and asking for a collection by name against a `.json` file is an
error rather than a filter that quietly matches nothing.

## ISBNs

An `isbn` field — BibTeX's `isbn`, Zotero's own ISBN field, CSL-JSON's `ISBN` —
is read as an identifier in its own right, not as decoration. Most books never
had a DOI minted at all, so a book is resolved through Open Library, the one
registry in this tool organised around books rather than DOIs, exactly as a
DOI-bearing entry is resolved through Crossref.

Before anything is looked up, the ISBN is checked against its own check digit,
per ISO 2108: the mod-11 weighted sum for a ten-character ISBN (where a trailing
`X` stands for 10), the alternating-weight EAN-13 checksum for a thirteen-digit
one. Hyphens and spaces are stripped first, so `0-201-63361-2` and `0201633612`
are the same identifier. A valid ISBN-10 is converted to its ISBN-13 form by
prefixing `978` to its first nine digits and computing a fresh check digit — a
conversion only ever applied to a string that already validated, because
converting a broken ISBN-10 would manufacture a plausible-looking ISBN-13 that
resolves nowhere for a reason no report could explain.

An ISBN whose check digit fails is never sent to Open Library, and is not
reported as a book Open Library does not have. It is reported as a malformed
identifier, with a note saying it was not queried against any registry. That is
a sibling of the distinction `BAD-ID` and `UNCHECKED` rest on (see
[verdicts](verdicts.md)): a failed check digit is neither a fact about a
registry's holdings nor ignorance about them — it is a defect in the
bibliography's own data, provable without a network call.

The ISBN is consulted **only when no DOI is stored**. An entry carrying both is
resolved through the DOI, the stronger identifier of the two — which also means
a book matched by ISBN alone gets no retraction check, because Open Library
mints no DOI for the retraction sources to be keyed on. [Retraction](retraction.md)
states that gap in full. `--no-isbn` skips Open Library entirely, at a cost
worth knowing before you reach for it: a book stored with only an ISBN is then
never looked up at all, and is reported `UNCHECKED` — nothing was verified about
it. It does not fail the run, and it is not evidence against the book; it simply
records that the one registry organised around books was not consulted.
