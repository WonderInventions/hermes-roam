"""Roam platform adapter plugin for Hermes Agent.

``register`` is imported lazily so this package ``__init__`` stays
import-safe even when something loads the file outside its package
context (e.g. pytest's collectors / pytest-asyncio walking up to the
repo root, which is itself the plugin package). At runtime Hermes
imports this directory as a package and calls ``register(ctx)``, at
which point the relative import resolves normally.
"""
from __future__ import annotations

__all__ = ["register"]


def register(ctx) -> None:
    from .adapter import register as _register

    _register(ctx)
