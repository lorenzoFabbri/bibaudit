"""Command-line interface.

    bibaudit check references.bib
    bibaudit check notes/**/*.md --bibliography library.bib
    bibaudit check ~/Zotero/zotero.sqlite
    bibaudit check . --format json --output audit.json

Exit codes are the contract with CI:

======  ====================================================================
``0``   nothing in the failing set occurred
``1``   at least one reference failed
``2``   the tool could not run — bad arguments, unreadable input, bad config
======  ====================================================================

A registry being unreachable is deliberately *not* a failure. Making an outage
break the build teaches people to pass ``--no-verify``, and a check that gets
routinely bypassed protects nobody.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .adapters.bibtex import duplicate_report, read_bibtex
from .adapters.markdown import scan_markdown
from .adapters.zotero import read_zotero
from .audit import AuditOptions, audit
from .compare import Thresholds
from .model import FAILING_VERDICTS, Reference, Result
from .registries.http import Cache, default_cache_dir
from .report import render_citekey_problems, render_json, render_text
from .suggest import SuggestionOutcome, write_suggestions
from .suppress import SuppressionError, load_suppressions

__all__ = ["build_parser", "main"]

#: Threshold defaults for ``--help``. Read from an instance, never from the
#: class: ``Thresholds`` uses ``slots=True``, so class attribute access yields
#: the slot descriptor rather than the default value.
_DEFAULTS = Thresholds()

#: Extensions each adapter claims. A directory argument is expanded using these.
_BIB_SUFFIXES = {".bib", ".bibtex"}
_TEXT_SUFFIXES = {".qmd", ".md", ".markdown", ".rmd"}
_ZOTERO_SUFFIXES = {".sqlite", ".json"}

#: Directory names that hold generated copies of the sources rather than the
#: sources themselves. Auditing a rendered copy reports every finding twice
#: and points the second one at a file the user cannot fix.
_GENERATED_DIRECTORIES = {"_site", "_freeze", "node_modules"}

#: Files this tool writes itself. ``--suggest`` puts ``<name>.suggested.bib``
#: beside its input, so the *second* ``bibaudit check .`` in a project picked
#: it up as another bibliography: every entry was then checked twice, every
#: DOI appeared under "duplicate doi", and half the findings pointed at a
#: generated file the user cannot usefully edit — the same defect
#: :data:`_GENERATED_DIRECTORIES` exists for, arriving by a different route.
#: Only *directory* expansion skips it; naming the file on the command line
#: still audits it, which is exactly what someone reviewing a suggestion
#: before applying it wants to do.
_SUGGESTED_SUFFIX = ".suggested.bib"


# Not ``_UsageError``: this is caught once, in ``main``, and turned into the
# documented exit code 2 with a one-line message. The N818 suffix convention
# is waived so the name reads as the *category* of exit ("usage") that the
# module docstring's exit-code table names, rather than as a runtime fault.
class _Usage(Exception):  # noqa: N818
    """A problem with how the tool was invoked, reported without a traceback."""


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser (exposed so tests can inspect it)."""
    parser = argparse.ArgumentParser(
        prog="bibaudit",
        description=(
            "Verify that every reference exists and that every stored field "
            "matches the publisher's record. Deterministic: no language model "
            "is used, and nothing is ever written to your bibliography."
        ),
        epilog=(
            "bibaudit cannot tell you whether a cited work supports the claim "
            "it is attached to. That requires reading the paper."
        ),
    )
    parser.add_argument("--version", action="version", version=f"bibaudit {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="check references and report discrepancies")
    check.add_argument(
        "paths",
        nargs="+",
        help=(
            "bibliographies (.bib), documents (.qmd/.md), Zotero libraries "
            "(zotero.sqlite, CSL-JSON export, or the word 'local' for a running "
            "Zotero), or directories containing them"
        ),
    )
    check.add_argument(
        "-b", "--bibliography", action="append", type=Path, default=[],
        help="bibliography used to resolve [@citekey] references in documents; "
             "repeatable. Discovered from Quarto/YAML front matter when omitted",
    )
    check.add_argument(
        "--format", choices=("text", "json"), default="text", help="report format",
    )
    check.add_argument(
        "-o", "--output", type=Path, help="write the report here instead of stdout",
    )
    check.add_argument(
        "--fail-on", default=",".join(sorted(FAILING_VERDICTS)),
        help="comma-separated verdicts that make the exit code non-zero "
             "(default: %(default)s). Pass an empty string to never fail",
    )

    network = check.add_argument_group("registries")
    network.add_argument(
        "--mailto", metavar="EMAIL",
        help="contact address sent to Crossref and NCBI; puts requests in "
             "Crossref's polite pool. No account or key is needed",
    )
    network.add_argument(
        "--no-corroborate", action="store_true",
        help="skip PubMed corroboration (halves request volume; loses the "
             "independent second opinion that catches registry defects)",
    )
    network.add_argument(
        "--no-search", action="store_true",
        help="do not try to confirm entries that carry no identifier",
    )
    network.add_argument(
        "--no-retraction-check", action="store_true",
        help="skip the independent Retraction Watch / PubMed expression-of-"
             "concern check run for every reference that resolves; a "
             "retraction or concern never deposited in Crossref's own "
             "updated-by linkage, or recorded only as PubMed's ECI "
             "cross-reference, then goes unnoticed (a retraction Crossref or "
             "PubMed already carries on the record itself still fails)",
    )
    network.add_argument(
        "--no-isbn", action="store_true",
        help="do not resolve books through Open Library, by ISBN or by "
             "title/author search; a book stored with only an ISBN is then "
             "reported UNCHECKED, since nothing that could hold it was "
             "consulted, and a book with no identifier at all falls back to "
             "UNCONFIRMED",
    )
    network.add_argument(
        "--no-europepmc", action="store_true",
        help="drop Europe PMC from the search used to confirm an entry with "
             "no identifier; loses one independently-curated source of "
             "candidates (preprints and grey literature PubMed does not "
             "index)",
    )
    network.add_argument(
        "--no-openalex", action="store_true",
        help="drop OpenAlex from the search used to confirm an entry with no "
             "identifier; OpenAlex never corroborates a match on its own "
             "(see registries/search.py), so this only narrows what can be "
             "found in the first place",
    )
    network.add_argument(
        "--offline", action="store_true",
        help="use only cached registry responses; anything uncached is reported "
             "as UNCHECKED rather than fetched",
    )
    network.add_argument(
        "--refresh", action="store_true", help="ignore cached responses and refetch",
    )
    network.add_argument(
        "--cache-dir", type=Path, default=default_cache_dir(),
        help="where registry responses are cached, so a verdict can be re-derived "
             "later without the network (default: %(default)s)",
    )
    network.add_argument(
        "--cache-ttl", type=int, default=90, metavar="DAYS",
        help="how long a cached registry response stays valid (default: %(default)s)",
    )
    network.add_argument(
        "--timeout", type=float, default=30.0, metavar="SECONDS",
        help="per-request timeout; exceeding it is treated as ignorance "
             "(UNCHECKED), never as a missing work (default: %(default)s)",
    )

    output = check.add_argument_group("report detail")
    output.add_argument(
        "-v", "--verbose", action="store_true",
        help="include informational verdicts (cosmetic, drift, registry artifacts)",
    )
    output.add_argument(
        "--show-suppressed", action="store_true",
        help="list differences that were suppressed and why",
    )
    output.add_argument(
        "--no-citekey-check", action="store_true",
        help="skip checking that every [@citekey] resolves to a bibliography entry",
    )
    output.add_argument(
        "--suggest", action="store_true",
        help="for every checked .bib with a fillable gap, write "
             "<name>.suggested.bib and <name>.suggested.diff beside it "
             "(only fields the entry has none of; the original is never "
             "written to; off by default)",
    )

    tuning = check.add_argument_group("thresholds")
    tuning.add_argument(
        "--title-wrong-work", type=float, default=_DEFAULTS.wrong_work, metavar="R",
        help="title similarity below which the identifier may point at a "
             "different work (default: %(default)s)",
    )
    tuning.add_argument(
        "--title-mismatch", type=float, default=_DEFAULTS.title_mismatch, metavar="R",
        help="title similarity below which titles are reported as disagreeing "
             "(default: %(default)s)",
    )

    cache = sub.add_parser("cache", help="inspect or clear the registry cache")
    cache.add_argument("action", choices=("info", "clear"))
    cache.add_argument("--cache-dir", type=Path, default=default_cache_dir())

    return parser


def _expand(paths: Sequence[str]) -> tuple[list[Path], list[Path], list[str]]:
    """Sort the arguments into bibliographies, documents and library sources."""
    bibs: list[Path] = []
    docs: list[Path] = []
    libraries: list[str] = []

    for raw in paths:
        if raw == "local":
            libraries.append(raw)
            continue
        path = Path(raw).expanduser()
        if not path.exists():
            raise _Usage(f"no such path: {raw}")
        if path.is_dir():
            if (path / "zotero.sqlite").is_file():
                libraries.append(str(path / "zotero.sqlite"))
                continue
            for child in sorted(path.rglob("*")):
                # Matched against the path *inside* the scanned tree, never
                # the absolute one: a project that legitimately lives under a
                # dot-directory — an Obsidian vault in ~/.notes, a CI checkout
                # under ~/.cache — has a dot component in every absolute part,
                # and testing those discarded every file in the tree. The user
                # saw "no references found in the given paths", which reads
                # like an empty bibliography rather than like a bug.
                if not child.is_file() or any(
                    part.startswith(".") or part in _GENERATED_DIRECTORIES
                    for part in child.relative_to(path).parts
                ):
                    continue
                if child.name.lower().endswith(_SUGGESTED_SUFFIX):
                    continue
                if child.suffix.lower() in _BIB_SUFFIXES:
                    bibs.append(child)
                elif child.suffix.lower() in _TEXT_SUFFIXES:
                    docs.append(child)
            continue
        suffix = path.suffix.lower()
        if suffix in _BIB_SUFFIXES:
            bibs.append(path)
        elif suffix in _TEXT_SUFFIXES:
            docs.append(path)
        elif path.name == "zotero.sqlite" or suffix in _ZOTERO_SUFFIXES:
            libraries.append(str(path))
        else:
            raise _Usage(f"do not know how to read {raw}")

    return bibs, docs, libraries


def _collect(
    args: argparse.Namespace,
) -> tuple[list[Reference], dict[str, list[str]], set[str], list[Path]]:
    """Read every input and return references, cited keys, defined keys, and bib paths.

    The bibliography paths are returned (not just consumed here) so
    ``--suggest`` can re-read each one afterwards — it needs the resolved
    file list, including whatever a document's own front matter discovered,
    not just what was named on the command line.
    """
    bibs, docs, libraries = _expand(args.paths)
    references: list[Reference] = []
    defined: set[str] = set()
    citekeys: dict[str, list[str]] = {}

    scan = scan_markdown(docs) if docs else None
    if scan is not None:
        references.extend(scan.references)
        citekeys = scan.citekeys
        # A document may declare its own bibliography; those count as inputs.
        for discovered in scan.bibliographies:
            if discovered not in bibs:
                bibs.append(discovered)

    for extra in args.bibliography:
        path = Path(extra).expanduser()
        if not path.is_file():
            raise _Usage(f"no such bibliography: {extra}")
        if path not in bibs:
            bibs.append(path)

    for bib in bibs:
        entries = read_bibtex(bib)
        references.extend(entries)
        defined.update(entry.key for entry in entries)

    for library in libraries:
        references.extend(read_zotero(library))

    if not references:
        raise _Usage("no references found in the given paths")
    return references, citekeys, defined, bibs


def _deduplicate(refs: Sequence[Reference]) -> list[Reference]:
    """Collapse references that denote the same work from different surfaces.

    A DOI listed in a document table and also present in the bibliography is one
    work; checking it twice doubles the request volume and reports the same
    finding twice. The bibliography entry is preferred because it carries more
    fields to check.
    """
    best: dict[str, Reference] = {}
    ordered: list[Reference] = []
    for ref in refs:
        identifier = ref.identifier
        if not identifier:
            ordered.append(ref)
            continue
        existing = best.get(identifier)
        if existing is None:
            best[identifier] = ref
            ordered.append(ref)
            continue
        # Keep whichever carries more checkable content.
        def richness(candidate: Reference) -> int:
            return sum(
                1 for value in (
                    candidate.title, candidate.authors, candidate.year,
                    candidate.container, candidate.volume, candidate.issue,
                    candidate.pages,
                ) if value
            )
        if richness(ref) > richness(existing):
            ordered[ordered.index(existing)] = ref
            best[identifier] = ref
    return ordered


def _report_suggestions(outcomes: Sequence[SuggestionOutcome]) -> None:
    """Announce what ``--suggest`` wrote, on stderr, never on the report stream.

    Silent when there was nothing fillable anywhere — a line saying "wrote
    nothing" for every ordinary, complete bibliography would be exactly the
    kind of noise a reader learns to stop reading.
    """
    for outcome in outcomes:
        print(
            f"bibaudit: {outcome.suggested_path} ({outcome.entries_changed} "
            f"entries, {outcome.fields_filled} fields) — review before use; "
            f"{outcome.source} was not modified",
            file=sys.stderr,
        )


def _run_check(args: argparse.Namespace) -> int:
    collected, citekeys, defined, bibs = _collect(args)
    references = _deduplicate(collected)

    try:
        suppressions = load_suppressions(Path(args.paths[0]).expanduser().resolve())
    except SuppressionError as exc:
        raise _Usage(str(exc)) from exc

    options = AuditOptions(
        cache_dir=args.cache_dir,
        cache_ttl_days=args.cache_ttl,
        mailto=args.mailto,
        refresh=args.refresh,
        offline=args.offline,
        corroborate=not args.no_corroborate,
        search_unidentified=not args.no_search,
        retraction_check=not args.no_retraction_check,
        use_isbn=not args.no_isbn,
        use_europepmc=not args.no_europepmc,
        use_openalex=not args.no_openalex,
        thresholds=Thresholds(
            wrong_work=args.title_wrong_work,
            title_mismatch=args.title_mismatch,
        ),
        suppressions=suppressions or None,
        timeout=args.timeout,
    )

    results: list[Result] = audit(references, options)

    if args.suggest:
        # Always run, independent of --format: a JSON report's stdout is a
        # machine-readable contract, so suggestion outcomes go to stderr
        # rather than risk trailing non-JSON text on that stream.
        _report_suggestions(write_suggestions(results, bibs))

    failing = frozenset(v.strip().upper() for v in args.fail_on.split(",") if v.strip())

    # Resolved before the report is written, and outside the format branch: a
    # citekey with no bibliography entry is a build failure waiting to happen,
    # and it has to fail the run whichever report format was asked for. When
    # this lived in the text branch alone, a CI job that switched to
    # ``--format json`` for machine parsing silently stopped catching them.
    missing_keys: dict[str, list[str]] = {}
    if citekeys and defined and not args.no_citekey_check:
        missing_keys = {k: v for k, v in citekeys.items() if k not in defined}

    stream = args.output.open("w", encoding="utf-8") if args.output else sys.stdout
    try:
        if args.format == "json":
            summary = render_json(results, stream=stream, failing_verdicts=failing)
        else:
            summary = render_text(
                results, stream=stream, verbose=args.verbose,
                show_suppressed=args.show_suppressed,
                # The banner must describe the policy the exit code below
                # honours. Left to its default, ``--fail-on ''`` printed
                # "FAIL — 1 reference(s) need attention" and then exited 0: two
                # unlabelled policies in one report, and a reader had no way to
                # tell which was answering their question.
                failing_verdicts=failing,
            )
            if citekeys and defined and not args.no_citekey_check:
                render_citekey_problems(
                    missing_keys, defined - set(citekeys), stream=stream
                )
            # Reported on the references as collected, not as deduplicated:
            # _deduplicate collapses two entries sharing a DOI into one, so
            # running this afterwards could never see the defect it exists for
            # — the same paper pasted into a bibliography twice under two
            # different citekeys.
            duplicates = duplicate_report(collected)
            for kind, groups in duplicates.items():
                if groups:
                    print(f"duplicate {kind}: {len(groups)}", file=stream)
    finally:
        if args.output:
            stream.close()

    if missing_keys:
        return 1
    return summary.exit_code(failing_verdicts=failing)


def _run_cache(args: argparse.Namespace) -> int:
    """Inspect or clear the registry cache.

    Both actions state it when the cache root could not be created, because an
    unusable cache and an empty one produce identical output otherwise:
    ``cache clear`` printed "cleared <path>" having cleared nothing, and
    ``cache info`` printed "0 cached responses" without saying the directory
    does not exist. Someone diagnosing why ``--offline`` reports every reference
    as ``UNCHECKED`` would read both as a working, empty cache and look
    elsewhere. ``Cache`` already carries the fact — see :attr:`Cache.usable`;
    it only had no reader.
    """
    cache = Cache(args.cache_dir)
    if args.action == "clear":
        cache.clear()
        if not cache.usable:
            print(
                f"bibaudit: {cache.path} cannot be created; there was no cache "
                "to clear",
                file=sys.stderr,
            )
            return 0
        print(f"cleared {cache.path}")
        return 0
    files = list(cache.path.rglob("*.json")) if cache.path.exists() else []
    size = sum(f.stat().st_size for f in files)
    print(f"{cache.path}\n  {len(files)} cached responses, {size / 1e6:.1f} MB")
    if not cache.usable:
        print(
            "  unusable: this directory cannot be created, so nothing is being "
            "cached and --offline has nothing to replay",
            file=sys.stderr,
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns the process exit code rather than calling ``exit``."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            return _run_check(args)
        if args.command == "cache":
            return _run_cache(args)
        # The subparsers are declared ``required=True``, so argparse rejects a
        # missing command before we get here; reaching this line means
        # ``build_parser`` grew a subcommand that nobody wired up. Raising the
        # usage error keeps that a normal exit 2 with a message. The previous
        # form called ``parser.error(...)`` — typed ``NoReturn`` — followed by
        # a ``return 2`` that mypy proved unreachable, which is a fair warning:
        # the fallback was dead code that could never produce the exit code it
        # claimed to.
        raise _Usage(f"unknown command: {args.command!r}")
    except _Usage as exc:
        print(f"bibaudit: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        print("bibaudit: interrupted", file=sys.stderr)
        return 130
    except OSError as exc:
        print(f"bibaudit: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
