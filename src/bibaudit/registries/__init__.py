"""Clients for the bibliographic registries consulted during a check.

Each client turns a registry's own response shape into
:class:`~bibaudit.model.Record`, and nothing else: no comparison logic lives
here, so a registry's quirks stay contained to its own module.

All of them share one rule. A 404 is a *fact* — the registry has no such record.
A timeout, a connection error or a run of 5xx responses is *ignorance* —
:class:`~bibaudit.registries.http.Transient` is raised so the caller can mark
the reference unchecked. Collapsing the two would let a network outage be
reported as a bibliography full of fabricated citations.
"""
