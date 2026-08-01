"""Rendering results for humans, for machines, and for CI.

The terminal report is written to be *acted on*: it leads with what is wrong,
gives the file and line to open, and shows the stored and registry values side
by side so the reader can judge which is right. It never shows a normalised
comparison key, because the whole point of a discrepancy is often the exact
character that normalisation removes.

Every report states the tool's limit in full. A reader who takes "all checks
pass" to mean the citations support the claims they are attached to has been
misled, and preventing that is part of the tool's job.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict
from typing import TextIO

from .model import FAILING_VERDICTS, VERDICTS, Issue, Result, is_registry_artifact

__all__ = [
    "LIMITS_NOTICE",
    "Summary",
    "render_citekey_problems",
    "render_json",
    "render_text",
]

LIMITS_NOTICE = (
    "bibaudit verifies that each reference exists and that its stored metadata "
    "matches the publisher's record. It does not and cannot verify that a cited "
    "work supports the statement it is attached to — that requires reading the paper."
)

#: Order in which verdict groups are printed: worst first, so the terminal's
#: last screenful is the part that needs action rather than the part that passed.
#: Taken from the model rather than restated here — the two copies had already
#: drifted apart on where ``INCOMPLETE`` sits relative to ``REGISTRY-ARTIFACT``,
#: which meant the severity the model documented was not the severity the report
#: showed.
_VERDICT_ORDER = VERDICTS

#: Verdict groups printed without ``--verbose``: everything a reader has to act
#: on, plus ``DISPUTED``, where two curated registries contradict each other and
#: only a human can settle it. Named rather than sliced off the front of
#: :data:`_VERDICT_ORDER`: a positional slice quietly changes meaning the moment
#: a verdict is inserted into ``model.VERDICTS``, and the failure mode is a
#: reference vanishing from the default report.
_ACTIONABLE_VERDICTS = FAILING_VERDICTS | {"DISPUTED"}

_VERDICT_HELP = {
    # "carries a retraction notice" read both ways — a paper that was retracted
    # and a paper that *is* a retraction notice — and those are opposites. The
    # second is an ordinary citable document and never a defect in the citing
    # bibliography, so the wording says which one this verdict means.
    "RETRACTED": "the cited work has itself been retracted",
    "BAD-ID": "the identifier resolves in no consulted registry",
    "WRONG-WORK": "the identifier resolves, but to a different paper",
    "FIELD-MISMATCH": "right work, but stored metadata disagrees with the registry",
    "UNCONFIRMED": "no identifier and no confident registry match — needs review",
    "DISPUTED": "registries disagree with each other; a human must decide",
    "INCOMPLETE": "the registry holds fields the entry omits",
    "ADJUDICATED": "a difference this project's .bibaudit.toml decided to accept",
    "REGISTRY-ARTIFACT": "difference explained by a known registry defect",
    "TITLE-DRIFT": "title differs in wording but denotes the same work",
    "COSMETIC": "identical apart from glyphs or capitalisation",
    "UNCHECKED": "no registry could be reached; nothing was verified",
    "OK": "every checked field agrees",
}


def _supports_colour(stream: TextIO) -> bool:
    """True when ANSI colour is safe: a TTY, not piped, and NO_COLOR unset."""
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return hasattr(stream, "isatty") and stream.isatty()


class _Style:
    """Minimal ANSI styling that degrades to plain text."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def for_verdict(self, verdict: str) -> str:
        if verdict in FAILING_VERDICTS:
            return self.red(verdict)
        # ADJUDICATED is amber, REGISTRY-ARTIFACT green, and the difference is
        # the point: a documented registry defect is settled for everyone and
        # needs no reader, whereas an adjudication is one project's own
        # decision, taken on trust, and it can rot — a citekey gets renamed and
        # the rule silently stops applying, or the registry fixes its record and
        # the entry is being excused for a difference that no longer exists.
        if verdict in {"DISPUTED", "INCOMPLETE", "ADJUDICATED", "UNCHECKED"}:
            return self.yellow(verdict)
        return self.green(verdict)


class Summary:
    """Counts derived from a run, used by both reporters and by the exit code."""

    def __init__(self, results: Sequence[Result]) -> None:
        self.results = list(results)
        self.verdicts = Counter(r.verdict for r in results)
        self.fields = Counter(
            issue.field for r in results for issue in r.issues if issue.severity == "error"
        )
        self.suppressed = sum(len(r.suppressed) for r in results)
        #: Of those, the ones a *documented registry defect* explains.
        self.artifacts = sum(
            1 for r in results for i in r.suppressed if is_registry_artifact(i)
        )
        #: ...and the ones a project's own ``.bibaudit.toml`` silenced. Counted
        #: apart because "the registry is known to be wrong" and "we decided not
        #: to care" are different amounts of assurance, and a single
        #: "suppressed: 40" line let the second hide inside the first.
        self.adjudicated = self.suppressed - self.artifacts

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def failing(self) -> list[Result]:
        """Results failing under the *default* set — "needs a human's attention"."""
        return [r for r in self.results if r.fails]

    def failing_under(self, failing_verdicts: frozenset[str]) -> list[Result]:
        """Results that break the build under the policy actually in force.

        Distinct from :attr:`failing`: with ``--fail-on ''`` this is empty while
        ``failing`` is not, and conflating the two is what made the banner
        announce a failure on a run the tool had deliberately passed.
        """
        return [r for r in self.results if r.verdict in failing_verdicts]

    @property
    def ok(self) -> int:
        return self.verdicts["OK"]

    def exit_code(self, *, failing_verdicts: frozenset[str] = FAILING_VERDICTS) -> int:
        """0 when nothing in *failing_verdicts* occurred, else 1."""
        return 1 if self.failing_under(failing_verdicts) else 0


#: Longest value printed on one line of the terminal report. Long enough for
#: an ordinary journal title, short enough that a 438-entry report stays
#: readable. The JSON report is not bounded — see :func:`render_json`.
_VALUE_WIDTH = 140


def _abbreviate(value: str) -> str:
    """*value* bounded to :data:`_VALUE_WIDTH`, marked when it was cut.

    The marker is not decoration. Epidemiology titles routinely run past this
    width, and two titles that differ only in a subtitle would otherwise be
    printed as two identical-looking lines under a ``title/mismatch``
    heading — a report that appears to contradict itself is one people stop
    believing. The full value is always in the JSON report.
    """
    if len(value) <= _VALUE_WIDTH:
        return value
    return value[: _VALUE_WIDTH - 1] + "…"


def _format_issue(issue: Issue, style: _Style, indent: str = "      ") -> list[str]:
    """Two aligned lines per issue: what is stored, what the registry holds."""
    lines: list[str] = []
    label = f"{issue.field}/{issue.kind}"
    header = f"{indent}{label}"
    if issue.note:
        header += style.dim(f"  ({issue.note})")
    lines.append(header)
    if issue.stored or issue.registry:
        source = issue.source or "registry"
        lines.append(f"{indent}  stored   {_abbreviate(issue.stored)}")
        lines.append(f"{indent}  {source:<8} {_abbreviate(issue.registry)}")
    return lines


#: The ``Issue.kind`` ``compare`` stamps when no registry that answered records
#: a retraction *and* a registry that could have recorded one was unreachable.
_RETRACTION_GAP = "retraction-unverified"


def _print_retraction_gap(summary: Summary, style: _Style, out: TextIO) -> None:
    """State beside the banner that retraction status could not be corroborated.

    ``compare`` raises this per reference at ``info`` severity, which is right —
    an outage is not a defect in anybody's bibliography, and promoting it would
    relabel every correct entry in the file. But ``info`` is filtered out of the
    default terminal report, so during an NCBI outage the whole run printed
    ``PASS — no reference in the failing set`` with nothing anywhere to say that
    the one separately-curated retraction source had not been asked. A clean
    report that reads cleaner than the evidence supports is the failure mode
    this tool exists to prevent, and it is worst on this field: a retracted
    paper going into a manuscript is the miss with no remedy.

    Printed once for the run rather than once per reference, because with a
    registry down it applies to every entry and 438 identical lines are not a
    warning, they are wallpaper. It does not touch the exit code.
    """
    blind: set[str] = set()
    affected = 0
    for result in summary.results:
        sources = {i.source for i in result.issues if i.kind == _RETRACTION_GAP}
        if sources:
            affected += 1
            blind.update(name for source in sources for name in source.split(","))
    if not affected:
        return
    print(
        style.yellow(
            f"  retraction status not corroborated for {affected} reference(s): "
            f"{', '.join(sorted(blind))} unreachable"
        ),
        file=out,
    )


def render_text(
    results: Sequence[Result],
    *,
    stream: TextIO | None = None,
    verbose: bool = False,
    show_suppressed: bool = False,
    failing_verdicts: frozenset[str] = FAILING_VERDICTS,
) -> Summary:
    """Write the human report and return the summary.

    ``verbose`` includes informational verdicts (cosmetic, artifact, drift);
    without it the report shows only what a reader must act on, which is what
    makes it readable on a 438-entry bibliography.

    ``failing_verdicts`` is whatever ``--fail-on`` resolved to, and the closing
    banner is computed from it. Computed from the default set instead — which is
    what it did — ``bibaudit check --fail-on ''`` over a retracted citation
    printed ``FAIL — 1 reference(s) need attention`` and exited 0: the banner
    and ``$?`` were two different policies, both unlabelled, and a reader had no
    way to tell which of them was answering their question. The banner now
    states the policy in force, and separately counts the references that need
    attention but were excluded from it, so neither number can be mistaken for
    the other.
    """
    out: TextIO = stream if stream is not None else sys.stdout
    style = _Style(_supports_colour(out))
    summary = Summary(results)

    print(style.bold(f"bibaudit — {summary.total} references checked"), file=out)
    print(file=out)

    interesting = set(_VERDICT_ORDER) if verbose else _ACTIONABLE_VERDICTS
    for verdict in _VERDICT_ORDER:
        group = [r for r in results if r.verdict == verdict]
        if verdict not in interesting:
            # ``--show-suppressed`` has to be able to list what the summary
            # counted. Suppression's own verdict, REGISTRY-ARTIFACT, sits
            # outside the default group set, so without this the flag printed
            # nothing at all unless --verbose happened to be passed too — and
            # the summary's "(--show-suppressed to list)" hint pointed at a
            # flag that did nothing. Only the entries that actually carry a
            # suppressed difference are pulled in, so asking to see them does
            # not also reprint every clean entry.
            group = [r for r in group if r.suppressed] if show_suppressed else []
        if not group:
            continue
        heading = f"{style.for_verdict(verdict)}  ({len(group)})"
        print(f"{heading}  {style.dim(_VERDICT_HELP.get(verdict, ''))}", file=out)
        for result in group:
            print(f"    {style.bold(result.ref.key)}  {style.dim(result.ref.locator)}", file=out)
            shown = result.issues if verbose else [i for i in result.issues if i.severity != "info"]
            for issue in shown:
                for line in _format_issue(issue, style):
                    print(line, file=out)
            if show_suppressed:
                for issue in result.suppressed:
                    for line in _format_issue(issue, style, indent="      ~ "):
                        print(style.dim(line), file=out)
        print(file=out)

    print(style.bold("summary"), file=out)
    for verdict in _VERDICT_ORDER:
        count = summary.verdicts.get(verdict, 0)
        if count:
            print(f"  {verdict:<18} {count}", file=out)
    if summary.fields:
        fields = ", ".join(f"{f}={n}" for f, n in summary.fields.most_common())
        print(f"  {'errors by field':<18} {fields}", file=out)
    if summary.suppressed:
        # Split, because the two halves are different claims: one says a
        # documented registry defect explains the difference, the other says a
        # person on this project decided it did not matter. A single total let
        # a .bibaudit.toml silence a field across a whole bibliography while the
        # summary line looked exactly like a run of known Crossref mojibake.
        breakdown = ", ".join(
            part
            for part in (
                f"{summary.artifacts} registry defect(s)" if summary.artifacts else "",
                f"{summary.adjudicated} adjudicated here" if summary.adjudicated else "",
            )
            if part
        )
        print(
            f"  {'suppressed':<18} {summary.suppressed}  ({breakdown})"
            f"{'' if show_suppressed else '  (--show-suppressed to list)'}",
            file=out,
        )

    print(file=out)
    breaking = summary.failing_under(failing_verdicts)
    if breaking:
        print(
            style.red(f"FAIL — {len(breaking)} reference(s) in the failing set"),
            file=out,
        )
    else:
        print(style.green("PASS — no reference in the failing set"), file=out)
    if failing_verdicts != FAILING_VERDICTS:
        # Only when the policy is not the documented default: printing it on
        # every ordinary run would be noise, and printing it on none of them is
        # how the banner came to be unreadable in the first place.
        listed = ", ".join(sorted(failing_verdicts)) or "empty — nothing fails this run"
        print(style.dim(f"  failing set (--fail-on): {listed}"), file=out)
    _print_retraction_gap(summary, style, out)
    excluded = [r for r in summary.failing if r.verdict not in failing_verdicts]
    if excluded:
        # Not a build failure, and not nothing either. Silence here would let
        # `--fail-on ''` print a clean-looking PASS over a retracted citation.
        print(
            style.yellow(
                f"  {len(excluded)} reference(s) need attention but are outside "
                "the failing set"
            ),
            file=out,
        )
    print(style.dim(LIMITS_NOTICE), file=out)
    return summary


def render_json(
    results: Sequence[Result],
    *,
    stream: TextIO | None = None,
    failing_verdicts: frozenset[str] = FAILING_VERDICTS,
) -> Summary:
    """Write a machine-readable report.

    The full registry values are included so a reviewer can re-derive every
    verdict without re-querying, which is what makes an audit reproducible.

    ``failing_verdicts`` is whatever ``--fail-on`` resolved to, because
    ``summary.exit_code`` in the payload is named after the process's exit
    code and a consumer reads it as that. Computed from the default set
    instead, ``bibaudit check --fail-on '' --format json`` over a retracted
    citation printed ``"exit_code": 1`` in a run that exited 0 — a CI job
    that gated on the field rather than on ``$?`` failed a build the tool had
    deliberately passed. ``render_text`` takes the same argument for the same
    reason; the two reports describe one policy, not two.
    """
    out: TextIO = stream if stream is not None else sys.stdout
    summary = Summary(results)
    payload = {
        "tool": "bibaudit",
        "limits": LIMITS_NOTICE,
        "summary": {
            "total": summary.total,
            "verdicts": dict(summary.verdicts),
            "errors_by_field": dict(summary.fields),
            "suppressed": summary.suppressed,
            # The two halves of `suppressed`, because a consumer auditing how
            # much of a bibliography is taken on trust needs to know how much of
            # it rests on a documented registry defect and how much on a
            # project-local decision. The total stays, so an existing consumer
            # keeps working.
            "registry_artifacts": summary.artifacts,
            "adjudicated": summary.adjudicated,
            "failing_verdicts": sorted(failing_verdicts),
            "exit_code": summary.exit_code(failing_verdicts=failing_verdicts),
        },
        "results": [
            {
                "key": r.ref.key,
                "locator": r.ref.locator,
                "identifier": r.ref.identifier,
                "verdict": r.verdict,
                "fails": r.fails,
                "title_similarity": r.title_similarity,
                "consulted": r.consulted,
                "issues": [asdict(i) for i in r.issues],
                "suppressed": [asdict(i) for i in r.suppressed],
            }
            for r in results
        ],
    }
    json.dump(payload, out, indent=2, ensure_ascii=False)
    out.write("\n")
    return summary


def render_citekey_problems(
    missing: dict[str, list[str]],
    unused: Iterable[str],
    *,
    stream: TextIO | None = None,
) -> int:
    """Report citekeys used but absent from the bibliography, and vice versa.

    A missing key is a build failure waiting to happen; an unused entry is only
    housekeeping. Returns the number of missing keys.
    """
    out: TextIO = stream if stream is not None else sys.stdout
    style = _Style(_supports_colour(out))
    unused_list = sorted(unused)

    if missing:
        print(style.red(f"unresolved citation keys ({len(missing)})"), file=out)
        for key, locators in sorted(missing.items()):
            print(f"    {style.bold(key)}", file=out)
            for locator in locators[:5]:
                print(f"      {style.dim(locator)}", file=out)
            if len(locators) > 5:
                print(f"      {style.dim(f'… {len(locators) - 5} more')}", file=out)
        print(file=out)

    if unused_list:
        print(style.dim(f"bibliography entries never cited ({len(unused_list)})"), file=out)
        print(style.dim("    " + ", ".join(unused_list[:20])), file=out)
        if len(unused_list) > 20:
            print(style.dim(f"    … {len(unused_list) - 20} more"), file=out)
        print(file=out)

    return len(missing)
