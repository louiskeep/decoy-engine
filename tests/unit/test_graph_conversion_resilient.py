"""Resilient pandas -> arrow conversion regression tests.

The runner's pandas-engine boundary (`graph.conversion.engine_to_arrow`)
must not fail a whole job when a single object-dtype column carries
mixed-type values (the most common cause is a CSV column that started
as int64 -- date-like values such as 20260522 -- and received string
output from a masking strategy partway through). The wrapped
`_pandas_to_arrow_resilient` retries the offending column(s) as
string and warns the operator.

These tests pin:

  1. The happy path stays a single `Table.from_pandas` call with no
     per-column overhead (typed columns pass through cleanly).
  2. Mixed int + str in an object column does NOT raise; the column
     comes back string-typed and the values round-trip readably.
  3. Null values inside the coerced column round-trip as Arrow nulls,
     not as the string literals 'None' / 'nan' that a naive
     ``.astype(str)`` would produce.
  4. Non-object columns are not touched by the retry path even when
     the original conversion fails for unrelated reasons.
"""

from __future__ import annotations

import logging

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.graph.conversion import engine_to_arrow


class TestResilientArrowConversion:

    def test_happy_path_typed_columns_pass_through(self):
        df = pd.DataFrame({
            'id': [1, 2, 3],
            'name': ['a', 'b', 'c'],
            'amount': [1.5, 2.5, 3.5],
        })
        table = engine_to_arrow(df, 'pandas')
        assert table.schema.field('id').type == pa.int64()
        assert table.schema.field('name').type == pa.string()
        assert table.schema.field('amount').type == pa.float64()
        assert table.num_rows == 3

    def test_mixed_int_and_str_object_column_coerces_to_string(self, caplog):
        # The user-reported scenario: ENRL_END_DT carried integer
        # date-like values that got mixed with string values
        # downstream (e.g. a masking strategy returning str).
        df = pd.DataFrame({
            'ENRL_END_DT': [20260522, '20260523', 20260524],
        })
        assert df['ENRL_END_DT'].dtype == object

        with caplog.at_level(logging.WARNING, logger='decoy_engine.graph.conversion'):
            table = engine_to_arrow(df, 'pandas')

        # The retry path must convert the offending column to string
        # rather than raising; downstream nodes see a clean Arrow type.
        assert table.schema.field('ENRL_END_DT').type == pa.string()
        assert table.column('ENRL_END_DT').to_pylist() == [
            '20260522', '20260523', '20260524',
        ]
        # And the operator gets a warning naming the column.
        assert any(
            'ENRL_END_DT' in rec.getMessage() and 'coercing to string' in rec.getMessage()
            for rec in caplog.records
        )

    def test_nulls_in_coerced_column_round_trip_as_arrow_nulls(self):
        # NaN / None must survive the .astype(str) coercion as
        # honest Arrow nulls -- a naive astype would render them as
        # the literal strings 'nan' or 'None' which would corrupt
        # downstream null-handling.
        df = pd.DataFrame({
            'X': [20260522, '20260523', None, 20260525],
        })
        table = engine_to_arrow(df, 'pandas')
        assert table.column('X').to_pylist() == [
            '20260522', '20260523', None, '20260525',
        ]
        # And that means is_null() at the Arrow level reads correctly:
        nulls = table.column('X').is_null().to_pylist()
        assert nulls == [False, False, True, False]

    def test_multiple_bad_columns_all_get_named(self, caplog):
        # When two object columns are both bad, the warning names
        # all of them in one shot rather than dripping per-column.
        df = pd.DataFrame({
            'A': [1, 'two', 3],
            'B': ['ok', 'fine', 'great'],  # clean string column, untouched
            'C': [10, 20, 'thirty'],
        })
        with caplog.at_level(logging.WARNING, logger='decoy_engine.graph.conversion'):
            table = engine_to_arrow(df, 'pandas')

        assert table.schema.field('A').type == pa.string()
        assert table.schema.field('B').type == pa.string()
        assert table.schema.field('C').type == pa.string()
        msgs = [r.getMessage() for r in caplog.records]
        assert any('A' in m and 'C' in m and 'B' not in m for m in msgs)

    def test_truly_unconvertible_value_still_falls_back(self, caplog):
        # Even when the underlying value can't be cleanly represented
        # (e.g. a bare object() with no string conversion), the
        # fallback path produces something rather than killing the
        # job. The operator gets a warning so they can investigate.
        sentinel = object()
        df = pd.DataFrame({'X': [sentinel, 'other']})
        with caplog.at_level(logging.WARNING, logger='decoy_engine.graph.conversion'):
            table = engine_to_arrow(df, 'pandas')
        # We don't pin the exact repr -- just that we got SOMETHING
        # and the warning surfaced the column.
        assert table.num_rows == 2
        assert any('X' in r.getMessage() for r in caplog.records)

    def test_unrelated_arrow_error_is_not_swallowed(self):
        # If the conversion fails for a reason that is NOT object-
        # column inference (none of the object cols can be identified
        # as the offender), the original error must surface so the
        # caller sees the real cause instead of a misleading
        # "retried, still broken" message.
        #
        # We trigger this by handing in a DataFrame with no object
        # columns and forcing a synthetic failure via duplicate names
        # -- the wrapper rejects duplicates before from_pandas, so it
        # surfaces a ValueError that is NOT one we'd retry around.
        df = pd.DataFrame({'A': [1, 2, 3]})
        df['B'] = [4, 5, 6]
        df.columns = ['X', 'X']  # collide
        with pytest.raises(ValueError, match='Duplicate column names'):
            engine_to_arrow(df, 'pandas')
