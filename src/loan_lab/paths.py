"""Locations shared by the web app and the development tools."""

from __future__ import annotations

import tomllib
from pathlib import Path

PROJECT_NAME = "loan-origination-conversion-lab"
DEFAULT_DATABASE_RELATIVE = Path("data") / "loan_lab_dev.db"
CONVERSION_ROOT_RELATIVE = Path("data") / "conversion"
EVIDENCE_ROOT_RELATIVE = Path("output") / "conversion"


def find_project_root(start: Path) -> Path:
    """Return the nearest ancestor of `start` whose pyproject.toml declares this project."""
    for directory in start.resolve().parents:
        pyproject = directory / "pyproject.toml"
        if pyproject.is_file():
            with pyproject.open("rb") as file:
                name = tomllib.load(file).get("project", {}).get("name")
            if name == PROJECT_NAME:
                return directory
    raise FileNotFoundError(f"Could not find the {PROJECT_NAME} project root above {start}.")


def default_database_path() -> Path:
    """The development database under the project root, independent of the working directory."""
    return find_project_root(Path(__file__)) / DEFAULT_DATABASE_RELATIVE


def default_conversion_root() -> Path:
    """Parent of the per-run conversion databases, data/conversion/<run_id>/."""
    return find_project_root(Path(__file__)) / CONVERSION_ROOT_RELATIVE


def default_evidence_root() -> Path:
    """Parent of the per-run evidence directories, output/conversion/<run_id>/."""
    return find_project_root(Path(__file__)) / EVIDENCE_ROOT_RELATIVE
