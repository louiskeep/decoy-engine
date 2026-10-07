"""The sink registry the file-access sentries resolve (plan Appendix B)."""

from __future__ import annotations

# Exact canonical names.
EXACT: frozenset[str] = frozenset(
    {
        "open",
        "builtins.open",
        "io.open",
        "os.open",
        "os.stat",
        "os.lstat",
        "os.access",
        "os.path.exists",
        "os.path.getsize",
        "os.path.isfile",
        "os.path.isdir",
        "os.path.realpath",
        "os.listdir",
        "os.scandir",
        "os.walk",
        "glob.glob",
        "glob.iglob",
        "os.link",
        "os.symlink",
        "os.unlink",
        "os.remove",
        "os.rmdir",
        "os.rename",
        "os.replace",
        "os.makedirs",
        "os.mkdir",
        "os.readlink",
        "os.truncate",
        "os.chmod",
        "os.utime",
        "os.chdir",
        "tempfile.mkstemp",
        "tempfile.mkdtemp",
        "tempfile.NamedTemporaryFile",
        "tempfile.TemporaryDirectory",
        "zipfile.ZipFile",
        "tarfile.open",
        "gzip.open",
        "bz2.open",
        "lzma.open",
        "sqlite3.connect",
        "shelve.open",
        "dbm.open",
        "mmap.mmap",
        "json.load",
        "yaml.load",
        "yaml.safe_load",
        "yaml.full_load",
        "yaml.unsafe_load",
        "pickle.load",
        "joblib.load",
        "joblib.dump",
        "numpy.load",
        "numpy.save",
        "logging.FileHandler",
        "logging.handlers.RotatingFileHandler",
        "logging.handlers.TimedRotatingFileHandler",
        "logging.handlers.WatchedFileHandler",
        "pyarrow.OSFile",
        "pyarrow.memory_map",
        "pyarrow.dataset.dataset",
        "pyarrow.dataset.write_dataset",
        "duckdb.connect",
        "duckdb.sql",
        "duckdb.query",
        "duckdb.execute",
        "smart_open.open",
        "fsspec.open",
        "os.system",
        "os.popen",
        "multiprocessing.Process",
        "concurrent.futures.ProcessPoolExecutor",
    }
)

# Canonical-name prefixes: everything beneath is a sink.
PREFIXES: tuple[str, ...] = (
    "shutil.",
    "subprocess.",
    "pandas.read_",
    "pyarrow.parquet.",
    "pyarrow.ipc.open_",
    "pyarrow.ipc.new_",
    "pyarrow.csv.",
    "pyarrow.json.",
    "pyarrow.fs.",
    "pyarrow.feather.",
    "polars.scan_",
    "polars.read_",
    "polars.sink_",
    "polars.write_",
    "duckdb.read_",
    "duckdb.from_",
    "os.exec",
    "os.spawn",
    "gcsfs.",
    "s3fs.",
    "adlfs.",
    "paramiko.SFTPClient",
)

# Method names (attribute calls) that are sinks on a receiver known to be a path object.
PATH_METHODS: frozenset[str] = frozenset(
    {
        "open",
        "read_text",
        "read_bytes",
        "write_text",
        "write_bytes",
        "stat",
        "lstat",
        "exists",
        "is_file",
        "is_dir",
        "is_symlink",
        "mkdir",
        "resolve",
        "iterdir",
        "glob",
        "rglob",
        "unlink",
        "rename",
        "replace",
        "touch",
        "chmod",
        "rmdir",
        "walk",
        "samefile",
        "symlink_to",
        "hardlink_to",
    }
)

# Path method names that no string, bytes or other common receiver has, so a call on an
# unresolved receiver is still flagged.
UNIQUE_PATH_METHODS: frozenset[str] = frozenset(
    {
        "read_text",
        "read_bytes",
        "write_text",
        "write_bytes",
        "iterdir",
        "is_file",
        "is_dir",
        "is_symlink",
        "rglob",
        "touch",
        "samefile",
        "symlink_to",
        "hardlink_to",
        "mkdir",
        "rmdir",
        "unlink",
        "exists",
    }
)

# Frame, array and client methods that write or read a file, unique to those types.
FILE_METHODS: frozenset[str] = frozenset(
    {
        "to_csv",
        "to_parquet",
        "to_json",
        "to_feather",
        "to_pickle",
        "to_excel",
        "to_hdf",
        "to_orc",
        "write_parquet",
        "write_csv",
        "write_ipc",
        "write_ndjson",
        "write_excel",
        "sink_parquet",
        "sink_csv",
        "sink_ipc",
        "sink_ndjson",
        "download_file",
        "upload_file",
        "download_fileobj",
    }
)

# Methods of a DuckDB connection (receiver known to come from a connect call).
DUCKDB_CONNECTION_METHODS: frozenset[str] = frozenset(
    {
        "execute",
        "executemany",
        "sql",
        "query",
        "read_parquet",
        "read_csv",
        "read_json",
        "from_parquet",
        "from_csv_auto",
        "install_extension",
        "load_extension",
    }
)

# The positional index of the path-bearing argument when it is not 0.
PATH_ARG_INDEX: dict[str, int] = {
    "pyarrow.parquet.write_table": 1,
}

PROCESS_PREFIXES: tuple[str, ...] = ("subprocess.", "os.exec", "os.spawn")
PROCESS_EXACT: frozenset[str] = frozenset(
    {
        "os.system",
        "os.popen",
        "multiprocessing.Process",
        "concurrent.futures.ProcessPoolExecutor",
    }
)


def is_sink_name(name: str) -> bool:
    if name in EXACT:
        return True
    return any(name.startswith(p) for p in PREFIXES)


def is_process_sink(name: str) -> bool:
    return name in PROCESS_EXACT or any(name.startswith(p) for p in PROCESS_PREFIXES)


# Operate on a descriptor that is already open: not a sink anywhere, but a capability module
# may call them only on a held descriptor.
DESCRIPTOR_ONLY: frozenset[str] = frozenset(
    {"os.fstat", "os.close", "os.dup", "os.dup2", "os.fchmod", "os.ftruncate", "os.fsync"}
)
