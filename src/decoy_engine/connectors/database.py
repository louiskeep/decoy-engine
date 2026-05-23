# decoy_engine/io/database.py
"""DBHandler â€” IOHandler implementation for database sources and targets."""

from __future__ import annotations

from typing import Any

import pandas as pd
from sqlalchemy import create_engine, text

from decoy_engine.connectors.base import IOHandler


class DBHandler(IOHandler):
    """Read from / write to a database table via SQLAlchemy."""

    def __init__(
        self,
        input_config: dict[str, Any],
        output_config: dict[str, Any],
        logger=None,
    ) -> None:
        super().__init__(input_config, output_config, logger=logger)
        self._in_engine = None
        self._out_engine = None

    # â”€â”€ helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _get_in_engine(self):
        if self._in_engine is None:
            dsn = self.input_config["connector_dsn"]
            self._in_engine = create_engine(dsn, pool_pre_ping=True)
        return self._in_engine

    def _get_out_engine(self):
        if self._out_engine is None:
            out_dsn = self.output_config.get("connector_dsn")
            if out_dsn:
                self._out_engine = create_engine(out_dsn, pool_pre_ping=True)
            else:
                # Same database for source and target
                self._out_engine = self._get_in_engine()
        return self._out_engine

    # â”€â”€ IOHandler interface â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def load_data(self) -> pd.DataFrame:
        engine = self._get_in_engine()
        table = self.input_config["table"]
        where = self.input_config.get("where")
        if where:
            query = f'SELECT * FROM "{table}" WHERE {where}'
        else:
            query = f'SELECT * FROM "{table}"'
        return pd.read_sql(text(query), con=engine.connect())

    def save_data(self, df: pd.DataFrame) -> None:
        engine = self._get_out_engine()
        table = self.output_config["table"]
        if_exists = self.output_config.get("if_exists", "replace")
        df.to_sql(table, con=engine, if_exists=if_exists, index=False)

    def close(self) -> None:
        if self._in_engine is not None:
            self._in_engine.dispose()
            self._in_engine = None
        if self._out_engine is not None and self._out_engine is not self._in_engine:
            self._out_engine.dispose()
            self._out_engine = None
