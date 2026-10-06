"""Pytest fixtures for the suite.

The helpers live in `tests/support.py`, so a test module can `import support`
and use them by name; this file owns the fixtures. The split by need matters:

* most tests need **nothing** - the protocol, mapping and ingest logic is pure
  Python, and those run with no BaseX present at all;
* the integration tests need BaseX **and** a seeded scratch database. They skip
  themselves when BaseX is down rather than failing, so `pytest -q` is runnable
  anywhere.

Nothing here touches the shipped `sync_*` databases: those hold a real export
and a test that reads them is neither hermetic nor fast (see
`test_schema_conformance`).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from oai.config import Config

import support


@pytest.fixture(scope="session")
def shipped_config() -> Config:
    """The shipped `oai.toml`, for tests about the configuration itself."""
    return Config.load(support.ROOT / "oai.toml")


@pytest.fixture(scope="session")
def object_store() -> Iterator[Config]:
    """A scratch Object database, seeded once from the sample.

    Session-scoped on purpose: seeding is the expensive part of the
    integration tests, and none of the tests that share this fixture mutate
    the store. A test that needs different data seeds its own database over
    `support.object_config("...")` instead of disturbing this one.
    """
    cfg = support.object_config("oai_provider_conftest")
    if not support.basex_live(cfg):
        pytest.skip("BaseX is not answering")
    support.seed(cfg, reset=True)
    try:
        yield cfg
    finally:
        support.drop(cfg)
