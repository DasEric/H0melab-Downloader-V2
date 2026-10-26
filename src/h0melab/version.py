"""One version resolver for source checkouts, editable installs and wheels."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from tomllib import loads

FALLBACK_VERSION = "2.3.0"


def get_version():
    # Editable metadata can lag behind a checkout after a git pull. Prefer this
    # checkout's own project, never an unrelated ancestor's pyproject.toml.
    project_file = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        project = loads(project_file.read_text(encoding="utf-8")).get("project", {})
        if project.get("name") == "h0melab-downloader" and project.get("version"):
            return str(project["version"])
    except (OSError, ValueError):
        pass
    try:
        return package_version("h0melab-downloader") or FALLBACK_VERSION
    except PackageNotFoundError:
        return FALLBACK_VERSION
