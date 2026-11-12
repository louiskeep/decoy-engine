### Changed (LIB evidence project binding, 2026-11-12)

The local record library's `evidence` payload (`cli-local-1`) accepts the two optional keys `decoy run` writes inside a project with a `decoy.yaml` marker: `path_base` (only `"project"`) and `project_manifest` (a relative path or `null`). They come as a pair. A manifest without them stores and exports unchanged, and `project_manifest` is reduced to a file name in a default export like `pipeline_path`. No registry code is added.
