import re
import subprocess
from pathlib import Path

from setuptools import setup

# Populates the __version__ variable
with open("revup/__init__.py") as f:
    exec(f.read())


def tag_rev() -> str:
    """
    Get the git sha of the current version from the tag
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", f"v{__version__}"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return "main"


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

    # Replace relative links with absolute, so images appear correctly on PyPI
    readme = readme.replace(
        "docs/images/",
        f"https://raw.githubusercontent.com/skydio/revup/{tag_rev()}/docs/images/",
    )

    return readme


setup(
    long_description=fixed_readme(),
)
