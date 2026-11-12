"""Corpus importers: convert a foreign memory format into sigil entries.

`sigil import fabric <src_dir> --vault <v>` dispatches here. Each importer is a
module in this package exposing `import_<format>(src_dir, vault_root, ...)`.
"""
from .fabric import import_fabric

__all__ = ["import_fabric"]
