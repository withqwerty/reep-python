"""Sphinx configuration for the reep documentation."""

from __future__ import annotations

import os
import sys
from importlib.metadata import version as _version
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "_ext"))

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
    "sphinxext.opengraph",
    "sphinx_sitemap",
    "notfound.extension",
    "llms_full",
]

source_suffix = {".md": "markdown", ".rst": "restructuredtext"}
language = "en"

# The published address: https://reep.football/docs/python/ (reep-site serves a
# copy of the build). It sets the canonical URLs, the sitemap and Open Graph.
html_baseurl = os.environ.get("REEP_DOCS_BASEURL", "https://reep.football/docs/python/")
root_doc = "index"
# Keep "--option" as typed in command examples (no en dashes).
smartquotes = False
exclude_patterns = ["_build", "_ext", "plan.md", "Thumbs.db", ".DS_Store"]

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
html_favicon = "https://reep.football/favicon.ico"
# Each guide page has a Markdown copy (see _ext/llms_full.py) in place of _sources.
html_copy_source = False
html_show_sourcelink = False
html_extra_path = ["llms.txt"]
html_theme_options = {
    "source_repository": "https://github.com/withqwerty/reep-python/",
    "source_branch": "main",
    "source_directory": "docs/",
}

ogp_site_url = html_baseurl
ogp_site_name = "reep: Python SDK for the Reep Register"
ogp_image = "https://reep.football/og-image.png"
ogp_social_cards = {"enable": False}

sitemap_url_scheme = "{link}"
sitemap_excludes = ["search.html", "genindex.html", "404.html"]

notfound_urls_prefix = "/docs/python/"
