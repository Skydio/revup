import os
import re
from pathlib import Path

from setuptools import setup


def fixed_readme() -> str:
    """
    Fix things in the README for PyPI
    """
    readme = Path("README.md").read_text()

    # Remove the PYPI_REMOVE tags
    readme = re.sub(
        r"<!--\s*PYPI_REMOVE\s*-->((?!PYPI_REMOVE).)*<!--\s*/PYPI_REMOVE\s*-->",
        "",
        readme,
        flags=re.MULTILINE | re.DOTALL,
    )

    # Git hash of the commit tagged with the current version. Set by Makefile.
    revup_version_hash = os.environ.get("REVUP_VERSION_HASH", "main")

    # Replace relative links with absolute, so images appear correctly on PyPI
    readme = readme.replace(
        "docs/images/",
        f"https://raw.githubusercontent.com/skydio/revup/{revup_version_hash}/docs/images/",
    )

    return readme


setup(
    long_description=fixed_readme(),
)
