"""Readers that turn a citation surface into :class:`~bibaudit.model.Reference`.

An adapter's only job is faithful extraction: parse what is stored, record where
it was found, and change nothing. Adapters never contact a registry and never
judge whether a value is correct.

Every adapter opens its source read-only. The Zotero adapter in particular
operates on a live database that the user's own application is writing to.
"""
