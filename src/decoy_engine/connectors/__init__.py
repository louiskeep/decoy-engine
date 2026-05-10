"""Connectors package: legacy I/O handlers plus SDK-based file connectors.

Two abstractions live here:

* `IOHandler` and its subclasses (`CSVHandler`, `FixedWidthHandler`,
  `DBHandler`) are the legacy path-based interface that powers the
  classic Masker / DataGenerator flow. Path in, DataFrame out.
* `S3FileSource`, `S3FileSink`, and friends are the new SDK-based
  streaming connectors built on `decoy_engine.sdk.FileSource` /
  `FileSink`. List + open + write semantics; no DataFrame conversion at
  the connector layer.

The two abstractions coexist on purpose. Sprint G connectors do not
need to participate in the IOHandler factory; they are loaded through
the connector SDK entry-point mechanism instead.
"""

from decoy_engine.connectors.base import IOHandler
from decoy_engine.connectors.csv_connector import CSVHandler
from decoy_engine.connectors.fixed_width import FixedWidthHandler
from decoy_engine.connectors.factory import create_io_handler
from decoy_engine.connectors.s3 import S3Config, S3FileSink, S3FileSource

__all__ = [
    # Legacy IOHandler family.
    "IOHandler",
    "CSVHandler",
    "FixedWidthHandler",
    "create_io_handler",
    # SDK-based file connectors (Sprint G).
    "S3Config",
    "S3FileSource",
    "S3FileSink",
]