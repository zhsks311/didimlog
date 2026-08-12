"""Didimlog public package metadata."""

from importlib.metadata import version as distribution_version


def version() -> str:
    """Return the installed Didimlog distribution version."""
    return distribution_version("didimlog")
