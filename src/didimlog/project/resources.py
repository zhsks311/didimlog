"""Load the canonical project scaffold from the installed package."""

import importlib.resources


_RESOURCE_PACKAGE = "didimlog.resources.project"


def read_project_resource(name: str) -> bytes:
    """Return one canonical project resource as bytes."""
    return importlib.resources.files(_RESOURCE_PACKAGE).joinpath(name).read_bytes()
