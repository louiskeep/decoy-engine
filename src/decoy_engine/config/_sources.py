"""SourceDescriptor: discriminated union over source-type variants.

S14-CLOUD-SRC-S3GCS (2026-05-30): V2 grows cloud source variants per the
R1 marketed scope (`decoy_release_1_scope` memory: file + cloud object
storage). Adds `S3Source` and `GCSSource` next to the existing
`FileSource`; the discriminator is the literal `type` field.

S4-FIXED-WIDTH (finish-open-ended program): `FileSource.format` grows a
`fixed_width` variant alongside `csv` / `parquet`, backed by the
declarative `FixedWidthLayout` column-spec (`config._fixed_width`).
Additive-only change (compatibility-contract §5: new Literal member,
new optional field with a default) -- existing `csv` / `parquet`
configs are unaffected. `layout` is required iff `format=="fixed_width"`
and forbidden otherwise, enforced below.

CSV-PARAMS (delimited input hardening): `FileSource` grows three optional
delimited-dialect fields -- `has_header`, `column_names`, `delimiter` --
so a headerless or non-comma delimited file can be declared. Additive
(all three default to today's behaviour: a comma CSV with a header row),
so existing configs are unaffected. The fields are csv-only; setting any
on a parquet/fixed_width source is a validation error. The reserved
`ValidationCode.SOURCE_FILE_*` entries (no_header_columns,
column_names_with_header, bad_delimiter, csv_param_on_non_csv,
bad_has_header_type) name these exact checks; config validation is
pydantic here (the engine has no ValidationResult-based config path), so
each check raises `ValueError` with the corresponding semantic.

Credentials: `credentials_ref` is OPAQUE to the engine. The engine treats
it as a string identifier the platform layer resolves against its
credentials store at run time before issuing the SDK call. The engine
never sees raw secrets in the config (ISO/IEC 27002 §8.24 / §5.17 -- key
management isolation; the engine layer is not a secrets boundary).

DB connectors are still V2.1 per `decoy_release_1_scope`; SFTP rides
along on `connectors/sftp.py` and ships in a separate S18 sprint.

Pydantic 2 discriminated-union pattern per
https://docs.pydantic.dev/latest/concepts/unions/#discriminated-unions.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from decoy_engine.config._fixed_width import FixedWidthLayout


def _strip_serialization(node: Any) -> Any:
    """Return `node` (a pydantic core-schema tree) with every `serialization`
    entry removed, so a JSON-schema handler regenerates the field schema instead
    of the generic object a custom serializer otherwise produces."""
    if isinstance(node, dict):
        return {k: _strip_serialization(v) for k, v in node.items() if k != "serialization"}
    if isinstance(node, list):
        return [_strip_serialization(item) for item in node]
    return node


class FileSource(BaseModel):
    """A local-filesystem CSV, Parquet, or fixed-width source.

    `layout` carries the column-spec for `format=="fixed_width"` (see
    `FixedWidthLayout` for the schema contract); it is required for
    that format and forbidden for `csv` / `parquet`.

    `has_header` / `column_names` / `delimiter` are csv-only delimited-dialect
    controls. They default to today's behaviour (a comma-delimited file whose
    first row is the header), so an existing `format: csv` source keeps reading
    exactly as before. To read a headerless file, set `has_header: false` and
    supply `column_names` (the names your mask rules key on); otherwise the
    columns would get pandas' positional `0,1,2...` names and no rule matches.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["file"]
    format: Literal["csv", "parquet", "fixed_width"]
    path: str
    layout: FixedWidthLayout | None = Field(
        default=None,
        description=(
            "Column-spec for format=='fixed_width'; required for that "
            "format, forbidden (must be omitted/None) for csv/parquet."
        ),
    )
    has_header: bool = Field(
        default=True,
        description=(
            "csv only. Whether the first row is a header. Set false for a "
            "headerless file and supply `column_names`."
        ),
    )
    column_names: list[str] | None = Field(
        default=None,
        description=(
            "csv only. Column names for a headerless file (has_header=false); "
            "required in that case, forbidden when has_header is true."
        ),
    )
    delimiter: str = Field(
        default=",",
        description=(
            "csv only. The single-character field delimiter (',' default, "
            "'\\t' for TSV, '|' for pipe-delimited)."
        ),
    )

    @model_validator(mode="after")
    def _validate_layout_matches_format(self) -> FileSource:
        if self.format == "fixed_width" and self.layout is None:
            raise ValueError("FileSource: format='fixed_width' requires a `layout` column-spec")
        if self.format != "fixed_width" and self.layout is not None:
            raise ValueError(
                "FileSource: `layout` is only valid when format='fixed_width' "
                f"(got format={self.format!r})"
            )
        return self

    @model_validator(mode="after")
    def _validate_delimited_params(self) -> FileSource:
        # The delimited-dialect fields are csv-only. `has_header`/`delimiter`
        # have non-None defaults, so "was it set on a non-csv source?" is read
        # from model_fields_set, not from the value (source_file.csv_param_on_non_csv).
        if self.format != "csv":
            offenders = sorted({"has_header", "column_names", "delimiter"} & self.model_fields_set)
            if offenders:
                raise ValueError(
                    "FileSource: delimited params "
                    f"({', '.join(offenders)}) are only valid when format='csv' "
                    f"(got format={self.format!r})"
                )
            return self
        # csv: header presence and names must agree.
        if self.has_header and self.column_names is not None:
            raise ValueError(
                "FileSource: `column_names` is forbidden when has_header=true "
                "(the header row already names the columns)"
            )
        if not self.has_header and not self.column_names:
            # None OR an empty list: an empty list would read a zero-column frame
            # and silently drop the whole file from profiling/masking.
            raise ValueError(
                "FileSource: has_header=false requires a non-empty `column_names` "
                "(a headerless file has no names to read)"
            )
        if self.column_names is not None:
            # Names are the handle every mask rule keys on; a blank or duplicated
            # name would mislabel a column, so reject at config time. Count-vs-file
            # cannot be checked here (no file read) -- the reader guards that.
            if any(not name.strip() for name in self.column_names):
                raise ValueError("FileSource: `column_names` entries must be non-empty")
            if len(set(self.column_names)) != len(self.column_names):
                raise ValueError("FileSource: `column_names` must be unique")
        if len(self.delimiter) != 1:
            raise ValueError(
                f"FileSource: `delimiter` must be exactly one character (got {self.delimiter!r})"
            )
        if self.delimiter in ("\n", "\r"):
            # A line terminator as the field delimiter has no coherent reader
            # meaning (pandas treats it as a record boundary); reject at config.
            raise ValueError("FileSource: `delimiter` must not be a line terminator")
        if not self.delimiter.isprintable() and self.delimiter != "\t":
            # A NUL or other control char has no coherent single-char delimiter
            # meaning: csv.reader rejects NUL outright, and the reader drops NUL
            # bytes before tokenizing, so a NUL delimiter would be erased rather
            # than split on. Tab is the one control char that is a real delimiter
            # (TSV), so it is the sole exception.
            raise ValueError(
                "FileSource: `delimiter` must be a printable character or a tab "
                f"(got {self.delimiter!r})"
            )
        return self

    @model_serializer(mode="wrap")
    def _omit_default_delimited_params(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        # The delimited params at their defaults stay out of the dump, so a csv
        # source that never sets them round-trips and persists byte-identically to
        # before the fields existed, and an older engine can re-validate the dump.
        # (`pipeline_config_hash` already excludes `sources`; this keeps the config
        # dump itself stable, not that hash.) An explicit default means the same as
        # an omitted one, so both drop.
        data: dict[str, Any] = handler(self)
        if data.get("has_header") is True:
            data.pop("has_header", None)
        if data.get("column_names") is None:
            data.pop("column_names", None)
        if data.get("delimiter") == ",":
            data.pop("delimiter", None)
        return data

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: Any, handler: Any) -> Any:
        # The wrap serializer above makes pydantic report the SERIALIZATION schema
        # as a generic object. GlobalSettings/ColumnConfig recover by dropping the
        # `serialization` entry, but here the model node sits under two
        # `model_validator(mode="after")` wrappers, so the entry to drop is nested,
        # not top-level. Strip every `serialization` entry in the core-schema tree,
        # then re-ask, so the serialization JSON schema still enumerates the fields.
        schema = handler(core_schema)
        if "properties" not in schema and "$ref" not in schema:
            schema = handler(_strip_serialization(core_schema))
        return schema


class S3Source(BaseModel):
    """An S3 (or S3-compatible) CSV / Parquet source.

    Compatible with real AWS S3 plus any S3-compatible service that accepts
    a custom `endpoint_url` (MinIO, Cloudflare R2, Wasabi, B2). The engine
    fetches the object once into a buffer + reads into Arrow; no streaming
    in the V1 spec because the masked output is in-memory anyway.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["s3"]
    format: Literal["csv", "parquet"]
    bucket: str = Field(..., min_length=1, max_length=255)
    key: str = Field(..., min_length=1)
    region: str | None = None
    endpoint_url: str | None = Field(
        default=None,
        description=(
            "Override the AWS endpoint (e.g. http://localhost:9000 for MinIO, "
            "https://<account>.r2.cloudflarestorage.com for Cloudflare R2). "
            "Leave None for real AWS S3."
        ),
    )
    credentials_ref: str | None = Field(
        default=None,
        description=(
            "Opaque credentials identifier the platform resolves at run time. "
            "The engine never sees raw secrets; when None the SDK walks its "
            "default credential chain (env vars / instance profile / etc.)."
        ),
    )


class GCSSource(BaseModel):
    """A Google Cloud Storage CSV / Parquet source.

    Structural mirror of S3Source. GCS uses `object` instead of `key` to
    match the GCS API surface (`bucket.object_name` vs S3 `bucket.key`).
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["gcs"]
    format: Literal["csv", "parquet"]
    bucket: str = Field(..., min_length=1, max_length=255)
    object: str = Field(..., min_length=1)
    credentials_ref: str | None = Field(
        default=None,
        description=(
            "Opaque credentials identifier the platform resolves at run time. "
            "The engine never sees raw secrets; when None the SDK uses GCP "
            "ADC (Application Default Credentials)."
        ),
    )


SourceDescriptor = Annotated[
    FileSource | S3Source | GCSSource,
    Field(discriminator="type"),
]
