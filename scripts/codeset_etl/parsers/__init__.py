"""Per-source parser plug-ins for the codeset ETL pipeline.

Each parser owns exactly one concern: turn a downloaded source archive's raw
bytes into a :class:`~scripts.codeset_etl.parsers._base.ParsedCorpus`.
Fetching (network I/O) and writing (Parquet + provenance) are shared
(``_fetch.py`` / ``_write.py``) and do not belong here.

``PARSERS`` is the registry ``update.update_corpus`` looks corpus names up
in -- the single place a new source parser must be added to become
reachable from the CLI.
"""

from __future__ import annotations

from ._base import CorpusParser, ParsedCorpus
from ._hcpcs import HcpcsParser
from ._icd10cm import Icd10CmParser
from ._ndc import NdcParser

#: Corpus name -> parser instance. Mirrors CODESET_REGISTRY's role
#: (transforms/_codeset_provenance.py) for the shipped-seed side: a single
#: source of truth for "which corpora does this pipeline know how to build."
#: Not required to cover every CODESET_REGISTRY name -- HC-1 slice 2 ships
#: NDC and ICD-10-CM end-to-end, plus this HCPCS follow-on; MS-DRG remains
#: unbuilt (see package docstring).
PARSERS: dict[str, CorpusParser] = {
    "ndc": NdcParser(),
    "icd10": Icd10CmParser(),
    "hcpcs": HcpcsParser(),
}

__all__ = ["PARSERS", "CorpusParser", "ParsedCorpus"]
