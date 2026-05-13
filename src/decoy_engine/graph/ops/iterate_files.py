"""iterate_files: run a sub-pipeline once per file in a remote source.

Use case: the file-mode workhorse. "Mask every CSV in this S3 prefix
with HIPAA disguise." Builds directly on the SDK's `FileSource.list()`.

Config:
    source_class: str               fully-qualified class path to a
                                    `FileSource` subclass, e.g.
                                    'decoy_engine.connectors.s3.S3FileSource'.
                                    Imported and instantiated with the
                                    config dict below.
    source_config: dict             config dict matching the source
                                    class's `ConnectorConfig` subclass.
    prefix: str                     optional prefix passed to
                                    `source.list()` to scope the iteration.
    pipeline_ref: str               sub-pipeline YAML path
    output_node: str                sub-pipeline node id whose output
                                    flows downstream
    output: 'concat' | 'void'       output mode (default concat)

The sub-pipeline accesses:
    {{iteration.value}}       the full file path (the FileMeta.path)
    {{iteration.value.size}}  the file size in bytes (None if unknown)
    {{iteration.index}}       0-based iteration count

The source connector lifecycle is owned by this op: instantiated at
apply-time, `close()`-d in a finally block. One connector per
iteration run; sub-pipelines that need to talk to the same source
should be passed connector configs through their own template vars.
"""
from __future__ import annotations

from importlib import import_module
from typing import Any

from decoy_engine.graph.ops._base import OpError
from decoy_engine.graph.ops._iterator_core import (
    run_iterations,
    validate_iterator_config,
)
from decoy_engine.internal.validator import ValidationError

KIND = "iterate_files"
NATIVE_ENGINE = "arrow"
INPUT_ARITY: tuple[int, int | None] = (0, 0)
OUTPUT_KIND = "stream"


def validate_config(config: dict[str, Any]) -> None:
    validate_iterator_config(config)
    source_class = config.get("source_class")
    if not isinstance(source_class, str) or "." not in source_class:
        raise ValidationError(
            "'source_class' must be a fully-qualified dotted path",
            "config.source_class",
        )
    source_config = config.get("source_config")
    if not isinstance(source_config, dict):
        raise ValidationError(
            "'source_config' must be a dict", "config.source_config"
        )
    prefix = config.get("prefix")
    if prefix is not None and not isinstance(prefix, str):
        raise ValidationError(
            "'prefix' must be a string if provided", "config.prefix"
        )


def apply(inputs, config, ctx):
    source = _build_source(config["source_class"], config["source_config"])

    try:
        # `list()` returns an iterator of FileMeta; materialize early so
        # connector close happens before any sub-pipeline runs. Avoids
        # leaving a long-lived HTTP / SSH connection open across what
        # could be many minutes of masking work.
        try:
            metas = list(source.list(prefix=config.get("prefix")))
        except Exception as exc:
            raise OpError(
                f"iterate_files: listing failed for "
                f"{config['source_class']}: {exc}"
            ) from exc
    finally:
        source.close()

    # Sort by path so iteration order is deterministic regardless of
    # connector implementation (S3 returns lex order, GCS returns
    # lex order, SFTP listdir returns directory order which is OS-defined
    # and not portable). Same-key + same-source -> same iteration order.
    metas.sort(key=lambda m: m.path)

    table = run_iterations(
        values=[m.path for m in metas],
        pipeline_ref=config["pipeline_ref"],
        output_node=config["output_node"],
        output_mode=config.get("output", "concat"),
        ctx=ctx,
        log_prefix="iterate_files",
        extra_template_vars=_make_size_extra_vars(metas),
    )
    if config.get("__engine") == "pandas":
        return table.to_pandas()
    return table


def _build_source(class_path: str, source_config: dict):
    """Import the source class and instantiate it with its Config subclass.

    Source classes follow the convention `<Module>.<ClassName>`; the
    matching config class is `<ClassName>` minus the `FileSource`
    suffix plus `Config`, e.g. `S3FileSource` -> `S3Config`. The op
    discovers the config class by inspecting the source's typing
    annotation, falling back to convention if that fails.
    """
    module_name, _, class_name = class_path.rpartition(".")
    try:
        module = import_module(module_name)
    except ImportError as exc:
        raise OpError(
            f"iterate_files: cannot import source module {module_name!r}: {exc}"
        ) from exc

    try:
        source_cls = getattr(module, class_name)
    except AttributeError as exc:
        raise OpError(
            f"iterate_files: source class {class_name!r} not found in "
            f"{module_name!r}"
        ) from exc

    # Find the ConnectorConfig subclass via Generic typing on the source.
    config_cls = _resolve_config_class(source_cls, module)

    try:
        cfg = config_cls(**source_config)
    except Exception as exc:
        raise OpError(
            f"iterate_files: source config invalid for {class_path}: {exc}"
        ) from exc

    return source_cls(cfg)


def _resolve_config_class(source_cls, module):
    """Find the ConnectorConfig subclass the source is parameterized on.

    Tries (1) the type-arg of the FileSource generic if present, then
    (2) the `<ClassName>Config` convention in the same module.
    """
    # Try the generic arg first: FileSource[S3Config] makes S3Config
    # discoverable via __orig_bases__.
    for base in getattr(source_cls, "__orig_bases__", []):
        args = getattr(base, "__args__", ())
        for arg in args:
            if isinstance(arg, type):
                return arg

    # Convention fallback: strip "FileSource"/"FileSink" suffix, append
    # "Config", look in the same module.
    name = source_cls.__name__
    for suffix in ("FileSource", "FileSink"):
        if name.endswith(suffix):
            candidate = name[: -len(suffix)] + "Config"
            cls = getattr(module, candidate, None)
            if cls is not None:
                return cls
            break

    raise OpError(
        f"iterate_files: cannot determine ConnectorConfig subclass for "
        f"{source_cls.__name__}; declare it via FileSource[Config] generic "
        f"or use the <Name>Config naming convention"
    )


def _make_size_extra_vars(metas):
    """Closure that maps an iteration index back to its FileMeta.size.

    Exposes `iteration.value.size` as a template var; useful for
    sub-pipelines that branch on file size (e.g. "skip large files",
    "use a heavier mask for big inputs").
    """
    by_path = {m.path: m for m in metas}

    def _extra(path, index):
        meta = by_path.get(path)
        if meta is None:
            return {}
        return {
            "iteration.value.size": meta.size if meta.size is not None else "",
            "iteration.value.path": meta.path,
        }

    return _extra
