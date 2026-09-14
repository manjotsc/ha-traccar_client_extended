"""Shared test setup: make the custom_components package importable, and give
the flow-shape guards a way to resolve a dynamically built schema."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import voluptuous as vol

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def step_keys(step) -> list:
    """The option keys a flow step owns, resolving a dynamically built schema.

    The attributes step builds its picker from live data, so its schema is a
    coroutine function rather than a literal. Resolving it here keeps the shape
    guards checking the schema a user is actually shown.

    `isinstance` rather than `callable`: a vol.Schema is callable too -- that is
    how it validates -- so a callable check invokes the static schemas as
    validators and fails with "expected a dictionary".
    """
    schema = step.schema
    if not isinstance(schema, vol.Schema):
        handler = SimpleNamespace(
            parent_handler=SimpleNamespace(
                config_entry=SimpleNamespace(options={}, runtime_data=None)
            )
        )
        schema = asyncio.run(schema(handler))
    return list(schema.schema)
