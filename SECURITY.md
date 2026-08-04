# Security

## Reporting a vulnerability

Report privately through GitHub's
[security advisory form](https://github.com/lorenzoFabbri/bibaudit/security/advisories/new)
rather than in a public issue. This is a single-maintainer project, so expect an
acknowledgement within a couple of weeks rather than within hours.

## What this tool does with your data

Worth stating plainly, because it bounds what a vulnerability here could reach.

- **It never writes to your bibliography.** Not the `.bib`, not the `.qmd`, not
  the Zotero database, which is opened read-only through an immutable URI
  because it is a live library the Zotero application may be writing to.
  `--suggest` writes `*.suggested.bib` and `*.suggested.diff` beside the
  original and never opens the original for writing.
- **It sends bibliographic identifiers to public registries**, and which
  identifier goes where is not a union. DOIs go to Crossref, DataCite and
  PubMed. An ISBN goes to Open Library alone. Titles and author names leave the
  machine only for an entry carrying no identifier, and only to the search
  sources — Crossref, Europe PMC and OpenAlex — plus Open Library for a book.
  Retraction Watch receives nothing about your bibliography: its export is one
  unauthenticated bulk download, matched against locally.
  If an unpublished title is sensitive, `--no-search` keeps it off the wire and
  `--offline` sends nothing at all.
- **`--mailto` appends your address to the `User-Agent` header of every
  request the tool makes** — not only Crossref and NCBI, which are the services
  that ask for it. One HTTP client is shared, so the address reaches every host
  the run contacts, including the Retraction Watch download. It is the only
  personal data the tool transmits, and it is opt-in.
- **No credentials of any kind.** There is no API key, token or account
  anywhere in this tool, so there is nothing for it to leak.
- **The cache holds registry responses verbatim** under `--cache-dir`
  (`bibaudit cache info` prints the path). It contains public bibliographic
  records, and `bibaudit cache clear` empties it.

## Scope

In scope: anything that lets a crafted `.bib`, note, Zotero database, CSL-JSON
file or registry response execute code, escape the cache directory, or write
outside the paths documented above.

Out of scope: a wrong verdict. That is a correctness bug and belongs in a public
issue — see [`CONTRIBUTING.md`](CONTRIBUTING.md), which explains why the cached
response is usually enough to settle one.
