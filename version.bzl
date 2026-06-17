"""Single source of truth for the revup version number.

This file is intentionally a real file (not a symlink) for Windows
compatibility. A test (tests/test_version_parity.py) ensures it stays in sync
with revup/version.py, which Python imports.
"""

REVUP_VERSION = "0.4.0"
