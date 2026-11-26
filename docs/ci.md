# In CI

bibaudit is built to be run by a machine: it reads the files you point it at — plus any bibliography a document's own front matter names, and the project's `.bibaudit.toml` — writes nothing back to any of them, and answers with an exit code. The exit code means few things on purpose, because people remove a check whose failures they cannot predict.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | No reference's verdict is in the failing set. |
| `1` | At least one is, or a bibliography entry could not be read and so was not audited, or a `[@citekey]` used in a document has no entry in any bibliography that was read. |
| `2` | The tool could not run at all. |

`Summary.exit_code` decides between `0` and `1`. It returns `1` when any result's verdict is in the set `--fail-on` resolved to, and for the two failures below; otherwise `0`. Nothing else about the run enters it: not how many fields differed, not how many registries answered.

Those two failures sit outside the verdict policy. Each exits `1` regardless of `--fail-on`, including `--fail-on ''`, because neither is a metadata disagreement a policy could choose to tolerate.

An entry bibtexparser could not read is one the audit never checked: a block with a syntax error, such as a comma inside a citekey, or the second of two entries sharing a citekey, which bibtexparser discards. The report lists each under `entries not audited` with its file, line and reason, and the banner reads `FAIL — 1 entry not audited` even when every reference it did check passed. A bibliography in which no entry could be read is reported the same way, not as exit `2`.

When a document cites `[@key]` and no bibliography read in the same run defines it, the citation will not build. `--no-citekey-check` turns that check off entirely, and so, silently, does giving the run no bibliography at all: with nothing defining any key, there is no set to resolve against and the check does not run.

Exit `2` means the invocation was wrong, not the bibliography. It is what you get from a path that does not exist, a file no adapter claims (`bibaudit: do not know how to read notes.docx`), a `--bibliography` that is not there, inputs that between them yielded no references at all, an invalid `.bibaudit.toml`, and any `OSError` on the way, such as `--output` pointing into a directory that does not exist. An `[[ignore]]` with no `reason` makes the file invalid, so it stops the run rather than silently applying. Each of those prints one `bibaudit: …` line on stderr, never a traceback. Bad command-line arguments are exit `2` as well, from `argparse` itself, which prints its own usage block and a `bibaudit check: error: …` line. A cancelled run (`KeyboardInterrupt`) exits `130`.

## An outage is not a failure

A reference that no registry could be reached about gets the verdict `UNCHECKED`, and `UNCHECKED` is not in the default failing set. A run in which every registry times out reports `UNCHECKED` for every reference it had to ask a registry about, prints `PASS — no reference in the failing set`, and exits `0`.

That is a decision, not an oversight. A check that breaks the build whenever Crossref is down for an afternoon is a check people learn to bypass, and a bypassed check protects nobody. The cost is that a green build is a weaker statement than it looks: read the summary counts, not the exit code alone.

For retractions the report states the gap. When no registry that answered recorded a retraction *and* a registry that could have recorded one was unreachable, a line prints beside the banner: `retraction status not corroborated for 3 reference(s): pubmed unreachable`. It is printed once for the run, and it does not touch the exit code. A job that gates only on `$?` will not see it.

!!! warning "Do not turn an outage into a failure by adding `UNCHECKED` to `--fail-on`"

    The flag accepts it, and it turns every registry outage into a red build on a bibliography that may be correct. If you want a run that cannot be weakened by the network, use `--offline` against a warm cache instead: a cache hit is replayed, a miss is `UNCHECKED`, and nothing reaches the network at all. A response older than `--cache-ttl` counts as a miss, so a cache left to age past it replays less each time.

## GitHub Actions

```yaml
name: references
on: [push, pull_request]

jobs:
  bibaudit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v10.4.0
      - run: uv tool install bibaudit
      - run: echo "$HOME/.local/bin" >> "$GITHUB_PATH"

      - name: Restore the registry cache
        uses: actions/cache@v6
        with:
          path: .bibaudit-cache
          key: bibaudit-${{ hashFiles('references.bib') }}
          restore-keys: bibaudit-

      - name: Check the bibliography
        run: >
          bibaudit check references.bib
          --cache-dir .bibaudit-cache
          --mailto ${{ secrets.CONTACT_EMAIL }}

      - name: Machine-readable report
        if: always()
        continue-on-error: true
        run: >
          bibaudit check references.bib --offline
          --cache-dir .bibaudit-cache
          --format json --output audit.json

      - uses: actions/upload-artifact@v7
        if: always()
        with:
          name: bibaudit-report
          path: audit.json
```

`--mailto` puts the address into the `User-Agent` bibaudit sends. Crossref grants its polite pool (lower latency, priority when it is shedding load) to requests carrying one, and no account or API key is involved anywhere in this tool. The address comes from a secret only to keep it out of the log. A pull request from a fork leaves the secret unset; the expansion is then empty, `--mailto` is left without its value, and the run exits `2` rather than quietly running without a contact address.

The second invocation runs `--offline` because the report can go to the terminal or to a file, not both, so a readable log *and* a JSON artifact take two runs. The second replays what the first cached and sends nothing over the network. Anything the first run could not fetch is still unfetched, so the replay is never more confident than the run it replays.

`if: always()` produces the artifact on the runs you most want it on, the ones where the first step failed, and `continue-on-error: true` keeps that second step from failing the job a second time for the same finding.

`--cache-dir` is given explicitly rather than left to the per-user default, because `actions/cache` has to be told which path to restore. Cached answers stay valid for `--cache-ttl` days, 90 by default. The directory also holds the parsed Retraction Watch index, in a `retraction-watch` subdirectory, so restoring it saves a ~66 MB download. That index has its own seven-day TTL rather than `--cache-ttl`'s 90 days, and the export it was parsed from is not stored in the cache at all. If the directory cannot be created, the run warns and continues without a cache; it does not fail.

## As a `make` target

```make
verify-refs:
	bibaudit check references.bib sources/
```

`make` propagates the exit code, so this fails the target on exit `1` or `2` and works identically under any runner.

## Tightening or loosening the failing set

`--fail-on` takes a comma-separated list of verdicts. The default is `BAD-ID,FIELD-MISMATCH,RETRACTED,UNCONFIRMED,WRONG-WORK`. Names are trimmed and upper-cased, so `--fail-on ' field-mismatch '` works; an empty string means nothing fails. [Verdicts](verdicts.md) describes what each one asserts, and [command line](cli.md) the rest of the flags.

The value replaces the default outright rather than adding to it, so widening means listing the default set *and* the addition: `--fail-on BAD-ID,FIELD-MISMATCH,RETRACTED,UNCONFIRMED,WRONG-WORK,INCOMPLETE` is how you enforce the house rule that no entry may omit a field the registry holds. A verdict added this way is printed as well as failed on, so the log that exits 1 names the entries: `INCOMPLETE` sits outside the default print set, and a build failing over a group it never printed gives nobody anything to fix. Narrowing is how you adopt the tool on an existing bibliography without a red build on day one.

!!! warning "A narrowed `--fail-on` does not make the findings go away"

    Narrowing changes the exit code and only the exit code. The excluded references are still listed under their verdicts, and the report adds a line counting them: `2 reference(s) need attention but are outside the failing set`. That count is computed against the *default* failing set, not against yours: it is how many references the default policy would have failed and yours does not, so narrowing the policy makes it larger, never smaller.

    Whenever `--fail-on` differs from the default, the report also names the policy in force — `failing set (--fail-on): BAD-ID`, or `empty — no verdict fails this run`. Verdict names are not validated against the list of verdicts, so a misspelt one matches nothing and the build passes; that line is where you will see it.

    Under a narrowed policy, `PASS — no reference in the failing set` says only that no reference has a verdict in the narrowed set. A run with a retracted citation and `--fail-on ''` prints `PASS`, exits `0`, and still shows the `RETRACTED` entry and the count of what was excluded.

## Reading the JSON report from a script

`summary.failing_verdicts` reflects the policy `--fail-on` resolved to, and `summary.exit_code` is the process's exit code, so a job can gate on the field instead of on `$?`. The top-level `unread` array lists every entry that was not audited, each with `locator`, `reason` and the `key` when the parser recovered one, and `unresolved_citekeys` maps each unresolved key to the places that cite it. Per result, `fails` is membership of the **default** failing set, which is what lets a consumer tell "needs attention" from "breaks this build": read `fails` beside whether that result's `verdict` appears in `summary.failing_verdicts`.
