"""Sphinx configuration for the reep documentation."""

from __future__ import annotations

from importlib.metadata import version as _version

project = "reep"
author = "Withqwerty Ltd"
copyright = "2026, Withqwerty Ltd"
release = _version("reep")
version = release

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx_click",
]

source_suffix = {".md": "markdown", ".rst": "restructuredtext"}
root_doc = "index"
# Keep "--option" as typed in command examples (no en dashes).
smartquotes = False
exclude_patterns = ["_build", "plan.md", "Thumbs.db", ".DS_Store"]

myst_enable_extensions = ["colon_fence", "deflist"]
myst_heading_anchors = 3

autodoc_member_order = "bysource"
autodoc_typehints = "description"
autodoc_typehints_format = "short"
autodoc_class_signature = "separated"
napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_use_ivar = True

intersphinx_mapping = {"python": ("https://docs.python.org/3", None)}

html_theme = "furo"
html_title = f"reep {release}"
html_static_path: list[str] = []
html_extra_path = ["llms.txt"]
html_theme_options = {
    "source_repository": "https://github.com/withqwerty/reep-python/",
    "source_branch": "main",
    "source_directory": "docs/",
}
