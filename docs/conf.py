# Sphinx configuration for the decoy-engine API reference.
#
# Auto-generated reference for everything declared in
# `decoy_engine.__init__.__all__` (and module-level public symbols
# elsewhere under src/decoy_engine/, except the `internal/` subpackage).
#
# Build:
#     pip install -e .[docs]
#     make -C docs html
#
# Output:
#     docs/_build/html/index.html
#
# CI:
#     .github/workflows/docs.yml regenerates on every push to main and
#     deploys to GitHub Pages when the deploy job is enabled.
#
# This file is referenced by Item 50 Phase E in the cross-repo plan
# (decoy-platform/plans/2026-05-10-three-layer-code-map.md).

from __future__ import annotations

# ── Project metadata ─────────────────────────────────────────────────────
project = "decoy-engine"
author = "louiskeep"
copyright = "2026, louiskeep"

# Pull the version from the package itself so a release bump in
# pyproject.toml shows up in the docs without a parallel edit here.
try:
    from importlib.metadata import version as _pkg_version

    release = _pkg_version("decoy-engine")
except Exception:
    release = "0.1.0"
version = ".".join(release.split(".")[:2])

# ── Sphinx extensions ────────────────────────────────────────────────────
extensions = [
    # Markdown source support so we can write index.md and include the
    # repo-root *_GUIDE.md files without rewriting them in reStructuredText.
    "myst_parser",
    # Google / NumPy style docstring rendering for the few that exist.
    "sphinx.ext.napoleon",
    # (napoleon config below the extension list.)
    # Cross-project link resolution (Python stdlib, pandas, pyarrow).
    "sphinx.ext.intersphinx",
    # Static API reference generation by walking the source tree —
    # contrast with sphinx.ext.autodoc which needs the package importable.
    # autoapi works against source files alone, which keeps the docs build
    # decoupled from the runtime env (no DuckDB / Polars wheels needed
    # just to render the docs).
    "autoapi.extension",
    # Copy buttons on code blocks.
    "sphinx_copybutton",
    # Mermaid diagram rendering for the ```mermaid fenced blocks in
    # architecture.md. Without this, Pygments treats `mermaid` as an unknown
    # lexer and -W escalates to a build failure.
    "sphinxcontrib.mermaid",
]

# `sphinx.ext.viewcode` deliberately omitted: it crashes with an IndexError
# while highlighting pydantic-model modules (e.g. `decoy_engine.disguises.schema`)
# because autoapi-reported line numbers diverge from viewcode's highlighted
# source. Furo's `source_repository` (see `html_theme_options` below) already
# surfaces an "Edit on GitHub" button per page, which is what we actually
# wanted; viewcode's embedded source view was never the goal.

# ── MyST (markdown) configuration ────────────────────────────────────────
# Keep the surface modest — these extensions cover what the existing
# *_GUIDE.md files use. Add more only when a doc actually needs them.
myst_enable_extensions = [
    "colon_fence",  # ::: fenced directive blocks
    "deflist",  # definition lists (already used in TAXONOMY-style guides)
    "linkify",  # auto-link bare URLs
    "smartquotes",
    "tasklist",
]
myst_heading_anchors = 3  # generate slugs for h1-h3 so cross-doc anchor links resolve
# Route ```mermaid fenced blocks to the sphinxcontrib-mermaid directive
# rather than letting Pygments try to highlight `mermaid` as a programming
# language (which it isn't, so -W escalates the lexer-not-found warning).
myst_fence_as_directive = ["mermaid"]

# ── sphinx-autoapi configuration ─────────────────────────────────────────
autoapi_type = "python"
autoapi_dirs = ["../src/decoy_engine"]
# Hide private subpackages and underscore modules. Everything under
# `internal/` is explicitly private per the public-API rule in CLAUDE.md;
# documenting it would actively mislead callers about the contract.
autoapi_ignore = [
    "*/internal/*",
    "*/_*.py",
    "*/conftest.py",
]
autoapi_options = [
    "members",
    "undoc-members",
    "show-inheritance",
    "show-module-summary",
    # `imported-members` makes re-exports from __init__.py appear under
    # `decoy_engine.__init__` rather than only at their original module.
    # Without it, the page for `decoy_engine` (the top-level __init__)
    # would be empty even though `__all__` re-exports 30+ symbols.
    "imported-members",
]
autoapi_python_class_content = "both"  # combine class + __init__ docstrings
autoapi_member_order = "groupwise"  # group classes / functions / data
autoapi_keep_files = False  # don't commit autoapi-generated .rst
autoapi_root = "api"  # output goes to docs/api/ at build time
autoapi_add_toctree_entry = True  # insert "API reference" toctree in index

# ── Napoleon (Google/NumPy docstring) configuration ──────────────────────
# Render docstring `Attributes:` sections as `:ivar:` fields inside the
# class description rather than as standalone `.. py:attribute::` directives.
# autoapi already emits a `py:attribute` for every annotated dataclass field;
# without this, a class that also documents those fields in an `Attributes:`
# section gets each one described twice on the same page, which trips the
# (typeless, unsuppressable) "duplicate object description" warning under -W.
napoleon_use_ivar = True

# ── Autodoc fallback (used by autoapi internally) ────────────────────────
# Render type hints in the description rather than the signature so
# parameter lists stay readable. autoapi inherits this.
autodoc_typehints = "description"
autodoc_typehints_format = "short"

# ── Intersphinx mapping ──────────────────────────────────────────────────
intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "pandas": ("https://pandas.pydata.org/docs/", None),
    "pyarrow": ("https://arrow.apache.org/docs/", None),
    "polars": ("https://docs.pola.rs/api/python/stable/", None),
}
intersphinx_disabled_reftypes = ["std:doc"]  # avoid noisy cross-project doc resolution

# ── HTML output ──────────────────────────────────────────────────────────
html_theme = "furo"
html_title = "decoy-engine"
html_static_path = ["_static"]
html_show_sourcelink = False  # the GitHub-link button below replaces this

html_theme_options = {
    "source_repository": "https://github.com/louiskeep/decoy-engine/",
    "source_branch": "main",
    "source_directory": "docs/",
    # Keep accent colors aligned with the CLI's semantic-token palette
    # (see ../../decoy/src/decoy/ui/theme.py). When the unified design
    # system (Item 3) lands, this gets pulled from the shared tokens.
    "light_css_variables": {
        "color-brand-primary": "#0ea5e9",
        "color-brand-content": "#0ea5e9",
    },
    "dark_css_variables": {
        "color-brand-primary": "#38bdf8",
        "color-brand-content": "#38bdf8",
    },
}

# ── Build hardening ──────────────────────────────────────────────────────
# CI runs sphinx-build with -W (warnings as errors) so any unresolved
# reference, missing toctree entry, or duplicate label fails the build.
# Listed here as a comment so contributors know the contract: a green
# local build with -W is what lands on Pages.
nitpicky = True
nitpick_ignore = [
    # Forward references in type hints that intersphinx can't resolve.
    # Add specific entries here when CI flags them, rather than relaxing
    # the global -W flag. Keeps the docs build tight.
    #
    # `Ellipsis` shows up wherever a type hint uses `...` (e.g. `Callable[..., T]`
    # or `tuple[int, ...]`); autoapi renders it as a class reference that no
    # intersphinx inventory ships.
    ("py:class", "Ellipsis"),
    # Stdlib / third-party classes that the intersphinx inventories don't
    # expose under their canonical dotted path — listed individually rather
    # than relaxing nitpicky globally.
    ("py:class", "pathlib.Path"),
    ("py:class", "pandas.Series"),
    ("py:class", "pandas.DataFrame"),
    ("py:class", "pyarrow.Table"),
    ("py:class", "numpy.random.Generator"),
    ("py:class", "pydantic.BaseModel"),
    ("py:obj", "pydantic.BaseModel"),
    ("py:class", "pydantic.SecretStr"),
    # Python stdlib `abc.ABC` shows up in the base-class list rendered by
    # autoapi's inheritance section. It's documented in the Python
    # intersphinx inventory under `:class:`, but autoapi cross-references
    # it via the `:obj:` role, which the inventory doesn't index. Listed
    # explicitly so the resolution doesn't depend on which role the
    # autoapi version of the day decides to emit.
    ("py:obj", "abc.ABC"),
    # Type aliases and TypeVars defined inside the engine that autoapi
    # rendering exposes as bare class references but doesn't link to a
    # documented page.
    ("py:class", "EngineType"),
    ("py:class", "decoy_engine.graph.conversion.EngineType"),
    ("py:class", "GraphEngineMode"),
    ("py:class", "ConfigT"),
    ("py:obj", "ConfigT"),
    ("py:class", "DetectorFn"),
    ("py:class", "TransformChoice"),
    # More module-level type aliases (Literal / tuple / union) that autoapi
    # renders as bare class references without emitting a linkable target.
    ("py:class", "Severity"),
    ("py:class", "StrategyDefault"),
    ("py:class", "ExpectedField"),
    ("py:obj", "DecoyError"),
    # `ConfigError` is re-exported by `decoy_engine.sdk` from
    # `decoy_engine.exceptions`. autoapi records both targets, so any
    # cross-reference becomes ambiguous; the exceptions module is canonical.
    ("py:class", "ConfigError"),
    # `ExecutionError` is defined in `execution/_errors.py` (autoapi-ignored
    # private module) and re-exported via `decoy_engine.execution`; the
    # Raises cross-reference from the public unmask module has no
    # documented target to land on. Same class of issue as ConfigError.
    ("py:exc", "ExecutionError"),
]

# Private (underscore-prefixed) classes, the `decoy_engine.internal.*`
# subpackage, and `decoy_engine.graph.ops._*` helper modules are excluded
# from the public API surface by autoapi_ignore. References to them from
# public modules still appear in type hints and rendered class hierarchies,
# but the targets aren't documented — so they trip nitpick. Regex form keeps
# the explicit ignore list from ballooning every time a new private helper
# is added.
nitpick_ignore_regex = [
    (r"py:.*", r"_[A-Za-z][A-Za-z0-9_]*"),
    (r"py:.*", r"decoy_engine\.internal\..*"),
    (r"py:.*", r"decoy_engine\.graph\.ops\._.*"),
]

# Warnings emitted by autoapi / docutils that are noisy but not actionable
# for the published build. Keep this list narrow — each entry should be
# justified by a comment.
suppress_warnings = [
    # autoapi walks every import and warns when it can't statically resolve
    # one to a module it's documenting. `internal/` and underscore-prefixed
    # helper modules (e.g. `graph.ops._base`, `graph.ops._cloud_io`) are
    # excluded by `autoapi_ignore` above on purpose — they're not part of
    # the public surface — so every public module that imports from them
    # produces a resolution warning. Documenting them just to silence the
    # warning would violate the public-API rule in CLAUDE.md.
    "autoapi.python_import_resolution",
    # autoapi-rendered docstrings occasionally trip docutils' strict rst
    # parser (definition lists without blank trailing lines, unintended
    # indentation in narrative text, etc.). These are rendering nits in
    # the auto-generated pages, not contract problems; the published HTML
    # is still legible. Suppressing here keeps -W focused on real failures
    # (broken refs, missing toctree entries) rather than docstring polish.
    "docutils",
    # NOTE: the "document isn't included in any toctree" warning is emitted
    # by sphinx.environment without a warning type (Sphinx 8.x), so it is NOT
    # suppressible here. Every autoapi-generated page is instead given a home
    # via the hidden `:glob:` toctree in index.md (api/**). Kept for the
    # remaining typed toc warnings.
    "toc.not_included",
    # `decoy_engine.sdk` re-exports `ConfigError` from `decoy_engine.exceptions`
    # via `__all__`, which is the right public-API shape but makes autoapi
    # record two targets for the same class. The resulting ambiguity warning
    # fires on unqualified `:class:\`ConfigError\`` refs even though both
    # targets are the same object. The not-found `ref.class` variant is
    # still caught — only this Python-domain ambiguity gets silenced.
    "ref.python",
]

# ── Files to ignore ──────────────────────────────────────────────────────
exclude_patterns = [
    "_build",
    "Thumbs.db",
    ".DS_Store",
    # The ADR template itself isn't a record — it's a copy-from skeleton.
    # Including it produces a `toc.not_included` warning and an empty page.
    "adr/template.md",
]
