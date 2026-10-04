"""Unit tests for the BaseX client's name and literal handling.

Database names are interpolated into BaseX *commands* (`CREATE DB x`), so they
are validated rather than escaped; query string literals are escaped by
doubling the quote.
"""

from __future__ import annotations

import pytest

from oai.basex import _db_name, _xq_string


def test_a_database_name_is_validated_not_escaped() -> None:
    for good in ("sync_Object", "oai_module_check", "a-b.c_1", "db2000"):
        assert _db_name(good) == good
    for bad in ("", "bad name", "a'b", "a;DROP DB x", "a\nb", "-leading"):
        with pytest.raises(ValueError):
            _db_name(bad)


def test_a_query_literal_doubles_the_quote() -> None:
    assert _xq_string("plain") == "'plain'"
    assert _xq_string("a'b") == "'a''b'"
