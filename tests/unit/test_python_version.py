"""Guard: every Python-version pin in the repo must agree.

This project targets exactly ONE interpreter (3.14) rather than a range, and
that choice is written down in five places which have no mechanical link to each
other:

* ``pyproject.toml``      -- ``requires-python``  (what uv resolves against)
* ``Dockerfile``          -- ``FROM python:X-slim`` (what production runs)
* ``ruff.toml``           -- ``target-version``   (what pyupgrade rewrites to)
* ``ty.toml``             -- ``python-version``   (the lint gate)
* ``pyrightconfig.json``  -- ``pythonVersion``    (the editor LSP)

Two of those files -- ``pyproject.toml`` and ``Dockerfile`` -- are
base-template-owned (see AGENTS.md "Ownership"), so ``agents-cli scaffold
upgrade`` can silently revert them to the template's default 3.12 while the
three checker configs still claim 3.14. That failure mode is nasty precisely
because it is quiet: the checkers keep happily accepting 3.14-only syntax, and
the first symptom is a container that dies at import time in the cluster.

This test makes that divergence loud and local. It is the version-pin analogue
of ``test_a2a_tracing.py``, which guards the other deliberate overlay.

Deliberately parses the raw text rather than importing tomllib and a JSON
parser for the Dockerfile's sake: one uniform mechanism, and it keeps working
if a file grows comments around the pin.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The single source of truth. Changing the project's Python version means
# changing this constant and the five files below together.
EXPECTED_MAJOR_MINOR = (3, 14)

_EXPECTED_DOTTED = f"{EXPECTED_MAJOR_MINOR[0]}.{EXPECTED_MAJOR_MINOR[1]}"


def _strip_jsonc(text: str) -> str:
    """Remove ``//`` line comments so ``json`` can parse a pyrightconfig.

    Only handles whole-line comments, which is all pyrightconfig.json uses here.
    A ``//`` inside a string literal (e.g. the ``$schema`` URL) must survive, so
    lines are only dropped when the comment marker starts the trimmed line.

    Args:
        text: Raw JSON-with-comments source.

    Returns:
        The same source with comment-only lines removed.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("//")
    )


def test_pyproject_requires_python_matches() -> None:
    """`requires-python` must pin the expected minor version exclusively."""
    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
    requires = pyproject["project"]["requires-python"]

    major, minor = EXPECTED_MAJOR_MINOR
    expected = f">={major}.{minor},<{major}.{minor + 1}"
    assert requires == expected, (
        f"pyproject.toml requires-python is {requires!r}, expected {expected!r}. "
        "If the project's Python version changed on purpose, update "
        "EXPECTED_MAJOR_MINOR in this test and every file it checks."
    )


def test_dockerfile_base_image_matches() -> None:
    """The production base image must be the expected interpreter.

    This is the pin that actually decides what runs in the cluster; the others
    only describe it.
    """
    dockerfile = (_REPO_ROOT / "Dockerfile").read_text()
    match = re.search(r"^FROM python:(\d+)\.(\d+)", dockerfile, re.MULTILINE)
    assert match is not None, (
        "No `FROM python:<major>.<minor>` line found in the Dockerfile. If the "
        "base image was changed to a non-python image, this guard needs updating."
    )

    found = (int(match.group(1)), int(match.group(2)))
    assert found == EXPECTED_MAJOR_MINOR, (
        f"Dockerfile builds on Python {found[0]}.{found[1]} but the project "
        f"targets {_EXPECTED_DOTTED}. The Dockerfile is base-template-owned, so "
        "a scaffold upgrade may have reverted it."
    )


def test_ruff_target_version_matches() -> None:
    """ruff's `target-version` must match, so pyupgrade rewrites safely."""
    ruff_toml = tomllib.loads((_REPO_ROOT / "ruff.toml").read_text())
    major, minor = EXPECTED_MAJOR_MINOR
    expected = f"py{major}{minor}"
    assert ruff_toml["target-version"] == expected, (
        f"ruff.toml target-version is {ruff_toml['target-version']!r}, "
        f"expected {expected!r}."
    )


def test_ty_python_version_matches() -> None:
    """ty's `python-version` must match, so the lint gate checks the right stdlib."""
    ty_toml = tomllib.loads((_REPO_ROOT / "ty.toml").read_text())
    assert ty_toml["environment"]["python-version"] == _EXPECTED_DOTTED, (
        f"ty.toml python-version is "
        f"{ty_toml['environment']['python-version']!r}, "
        f"expected {_EXPECTED_DOTTED!r}."
    )


def test_basedpyright_python_version_matches() -> None:
    """basedpyright's `pythonVersion` must match, so the editor agrees with CI."""
    raw = (_REPO_ROOT / "pyrightconfig.json").read_text()
    config = json.loads(_strip_jsonc(raw))
    assert config["pythonVersion"] == _EXPECTED_DOTTED, (
        f"pyrightconfig.json pythonVersion is {config['pythonVersion']!r}, "
        f"expected {_EXPECTED_DOTTED!r}."
    )


@pytest.mark.skipif(
    sys.version_info[:2] != EXPECTED_MAJOR_MINOR,
    reason="Interpreter differs from the project pin; the config guards above "
    "are what matter in that case.",
)
def test_running_interpreter_matches() -> None:
    """Sanity check that the test run itself is on the pinned interpreter.

    Skipped rather than failed when it is not, so a deliberate
    `uv run --python ...` experiment does not look like a config bug.
    """
    assert sys.version_info[:2] == EXPECTED_MAJOR_MINOR
