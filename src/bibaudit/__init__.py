"""bibaudit — deterministic, field-level verification of bibliographies.

Checks that every reference exists *and* that every stored field matches the
publisher's record, against Crossref, DataCite, PubMed and, for books, Open
Library. Every reference that resolves is separately checked for retraction
status against Retraction Watch's own export and PubMed's expression-of-concern
cross-reference, independent of whatever a publisher deposited with Crossref.
No language model is involved at any point, so verdicts are reproducible and
each one can be re-derived by hand from the cached registry response.

The tool reports; it never rewrites a bibliography. Adopting a registry value
wholesale — what most "bibliography fixers" do — destroys the evidence that
there was a discrepancy at all, and registries are themselves wrong often
enough that the evidence is worth keeping.

What it cannot do: judge whether a cited work supports the claim it is attached
to. That requires reading the paper, and no metadata check substitutes for it.
"""

from __future__ import annotations

from .audit import AuditOptions, audit
from .compare import Thresholds, compare, verdict_for
from .model import FAILING_VERDICTS, VERDICTS, Issue, Name, Record, Reference, Result
from .report import LIMITS_NOTICE, Summary, render_json, render_text
from .suppress import Suppressions, load_suppressions

__all__ = [
    "FAILING_VERDICTS",
    "LIMITS_NOTICE",
    "VERDICTS",
    "AuditOptions",
    "Issue",
    "Name",
    "Record",
    "Reference",
    "Result",
    "Summary",
    "Suppressions",
    "Thresholds",
    "audit",
    "compare",
    "load_suppressions",
    "render_json",
    "render_text",
    "verdict_for",
]

try:  # pragma: no cover - trivial, and absent only in a source checkout
    from importlib.metadata import version as _version

    __version__ = _version("bibaudit")
except Exception:  # pragma: no cover
    __version__ = "0.0.0.dev0"
