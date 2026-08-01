"""Project-local suppressions for differences a human has already adjudicated.

:mod:`~bibaudit.benign` holds registry defects that are true everywhere.
This module holds the ones that are true only for *your* bibliography — the
handful of entries where you have checked the paper yourself and concluded the
registry is wrong, or the difference is deliberate.

Two rules keep this from becoming a way to hide problems:

* a suppression **must** carry a ``reason``; an unexplained one is refused;
* suppressed issues are counted and reported, never silently dropped, so the
  report always states how much of the bibliography is being taken on trust.

Configuration lives in ``.bibaudit.toml`` beside the bibliography::

    [[ignore]]
    key    = "papantoniou2017colorectal"
    field  = "authors"
    reason = "Crossref returns mojibake surnames; checked against the PDF 2026-07-30"

    [[ignore]]
    key    = "*"
    field  = "publisher"
    reason = "publisher names churn with imprint mergers; not tracked here"
"""

from __future__ import annotations

import fnmatch
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .model import Issue, Result

__all__ = [
    "CONFIG_NAME",
    "Suppression",
    "SuppressionError",
    "Suppressions",
    "load_suppressions",
]

CONFIG_NAME = ".bibaudit.toml"

#: Everything an ``[[ignore]]`` table may carry. Anything else is a typo, and a
#: typo in this file removes findings rather than adding them.
_ALLOWED_KEYS = frozenset({"key", "field", "kind", "reason"})


class SuppressionError(ValueError):
    """Raised when a suppression is malformed — never silently ignored."""


@dataclass(frozen=True, slots=True)
class Suppression:
    """One adjudicated difference.

    ``key`` and ``field`` accept shell-style globs, so ``field = "*"`` silences
    an entry entirely and ``key = "*"`` silences a field across the whole
    bibliography. ``kind`` narrows further, e.g. only ``missing`` issues.
    """

    key: str
    field: str
    reason: str
    kind: str = "*"

    def matches(self, citekey: str, issue: Issue) -> bool:
        # ``fnmatchcase``, never ``fnmatch``: the latter folds case through
        # ``os.path.normcase``, so ``key = "Smith2020"`` would silence
        # ``smith2020`` on Windows and not on Linux. Citekeys are
        # case-sensitive, and a verdict that depends on the operating system is
        # not reproducible.
        return (
            fnmatch.fnmatchcase(citekey, self.key)
            and fnmatch.fnmatchcase(issue.field, self.field)
            and fnmatch.fnmatchcase(issue.kind, self.kind)
        )


@dataclass(slots=True)
class Suppressions:
    """A loaded suppression set."""

    rules: list[Suppression]
    source: Path | None = None
    #: Indices into :attr:`rules` that have silenced at least one issue. Tracked
    #: because a rule matching nothing is a silent failure: a citekey gets
    #: renamed, its suppression stops applying, and the report looks the same as
    #: the day the difference was adjudicated. :attr:`unused` lets the caller say
    #: so instead.
    used: set[int] = field(default_factory=set)

    def apply(self, result: Result) -> int:
        """Move matching issues out of ``result.issues`` into ``result.suppressed``.

        Returns how many were moved. The verdict is *not* recomputed here; the
        caller does that, so that the decision to re-derive a verdict is always
        explicit.
        """
        if not self.rules:
            return 0
        kept: list[Issue] = []
        moved = 0
        for issue in result.issues:
            match = next(
                (
                    (index, rule)
                    for index, rule in enumerate(self.rules)
                    if rule.matches(result.ref.key, issue)
                ),
                None,
            )
            if match is None:
                kept.append(issue)
                continue
            index, rule = match
            self.used.add(index)
            issue.severity = "info"
            issue.kind = f"suppressed:{issue.kind}"
            issue.note = (issue.note + " — " if issue.note else "") + rule.reason
            result.suppressed.append(issue)
            moved += 1
        result.issues = kept
        return moved

    @property
    def unused(self) -> list[Suppression]:
        """Rules that have silenced nothing, in file order.

        Only meaningful once :meth:`apply` has run over every result: a rule is
        "unused" for the run as a whole, not for one entry.
        """
        return [rule for index, rule in enumerate(self.rules) if index not in self.used]

    def __bool__(self) -> bool:
        return bool(self.rules)


def load_suppressions(start: Path) -> Suppressions:
    """Find and load ``.bibaudit.toml``, searching upward from *start*.

    The search stops at a filesystem root or a ``.git`` directory, so a
    suppression file cannot be picked up from an unrelated parent project.
    """
    directory = start if start.is_dir() else start.parent
    for candidate in [directory, *directory.parents]:
        config = candidate / CONFIG_NAME
        if config.is_file():
            return _parse(config)
        if (candidate / ".git").exists():
            break
    return Suppressions(rules=[])


def _parse(path: Path) -> Suppressions:
    """Parse a suppression file, refusing entries without a reason.

    Every way this file can be wrong ends in a :class:`SuppressionError` naming
    the file. A traceback out of here would be read as "bibaudit is broken" when
    what happened is a typo in the user's own config, and the run would abort
    without checking a single reference.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SuppressionError(f"{path}: not valid UTF-8 ({exc})") from exc
    except OSError as exc:
        raise SuppressionError(f"{path}: cannot be read ({exc})") from exc

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SuppressionError(f"{path}: {exc}") from exc

    entries = data.get("ignore", [])
    if not isinstance(entries, list):
        # ``ignore = 5`` or ``[ignore]`` instead of ``[[ignore]]``. Iterating
        # either raises somewhere less helpful — a TypeError for the scalar, a
        # confusing per-key complaint for the table.
        raise SuppressionError(
            f"{path}: 'ignore' must be a list of tables, written [[ignore]], "
            f"not {type(entries).__name__}"
        )

    rules: list[Suppression] = []
    for index, raw in enumerate(entries, start=1):
        if not isinstance(raw, dict):
            raise SuppressionError(f"{path}: [[ignore]] #{index} is not a table")
        unknown = sorted(set(raw) - _ALLOWED_KEYS)
        if unknown:
            # A typo widens a suppression instead of narrowing it: write
            # ``fields = "volume"`` and ``field`` falls back to ``"*"``, which
            # silences every difference on that entry. Refusing the key is the
            # only way the user finds out.
            raise SuppressionError(
                f"{path}: [[ignore]] #{index} has unknown key(s) {', '.join(unknown)}. "
                f"Allowed: {', '.join(sorted(_ALLOWED_KEYS))}."
            )
        reason = str(raw.get("reason", "")).strip()
        if not reason:
            raise SuppressionError(
                f"{path}: [[ignore]] #{index} has no 'reason'. "
                "Every suppression must record why the difference is acceptable."
            )
        rules.append(
            Suppression(
                key=str(raw.get("key", "*")),
                field=str(raw.get("field", "*")),
                kind=str(raw.get("kind", "*")),
                reason=reason,
            )
        )
    return Suppressions(rules=rules, source=path)
