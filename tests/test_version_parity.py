"""The version is duplicated in revup/version.py and version.bzl because
MODULE.bazel cannot `load()` and Bazel needs a Starlark-readable form. This
test catches drift between the two."""

import re
from pathlib import Path

from revup.version import REVUP_VERSION


def _read_starlark_version(path: Path) -> str:
    text = path.read_text()
    match = re.search(r'^REVUP_VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, f"REVUP_VERSION not found in {path}"
    return match.group(1)


def _read_module_bazel_version(path: Path) -> str:
    text = path.read_text()
    match = re.search(r'^\s*version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, f"module() version not found in {path}"
    return match.group(1)


def test_version_files_match():
    repo_root = Path(__file__).parent.parent
    bzl_version = _read_starlark_version(repo_root / "version.bzl")
    module_version = _read_module_bazel_version(repo_root / "MODULE.bazel")

    assert REVUP_VERSION == bzl_version, (
        f"revup/version.py ({REVUP_VERSION}) and version.bzl ({bzl_version}) disagree."
    )
    assert REVUP_VERSION == module_version, (
        f"revup/version.py ({REVUP_VERSION}) and MODULE.bazel ({module_version}) disagree."
    )
