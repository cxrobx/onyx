"""The read-only phone mirror (docs/plans/phone-mirror.md).

Nothing here runs unless ``mirror.toml`` in the data directory says ``enabled = true``. This module
imports nothing on purpose: ``cryptography`` is an opt-in extra and is imported inside the functions
that need it, so ``import onyx`` never reaches it.
"""
