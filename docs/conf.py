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
    # Source-code links from API pages back to GitHub.
    "sphinx.ext.viewcode",
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
]

# ── MyST (markdown) configuration ────────────────────────────────────────
# Keep the surface modest — these extensions cover what the existing
# *_GUIDE.md files use. Add more only when a doc actually needs them.
myst_enable_extensions = [
    "colon_fence",   # ::: fenced directive blocks
    "deflist",       # definition lists (already used in TAXONOMY-style guides)
    "linkify",       # auto-link bare URLs
    "smartquotes",
    "tasklist",
]
myst_heading_anchors = 3  # generate slugs for h1–h3 so cross-doc anchor links resolve

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
autoapi_python_class_content = "both"   # combine class + __init__ docstrings
autoapi_member_order = "groupwise"      # group classes / functions / data
autoapi_keep_files = False              # don't commit autoapi-generated .rst
autoapi_root = "api"                    # output goes to docs/api/ at build time
autoapi_add_toctree_entry = True        # insert "API reference" toctree in index

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
]

# ── Files to ignore ──────────────────────────────────────────────────────
exclude_patterns = [
    "_build",
    "Thumbs.db",
    ".DS_Store",
]
