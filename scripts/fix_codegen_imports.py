#!/usr/bin/env python3
"""Post-process ariadne-codegen output to be Python 3.8 compatible."""
import re
import sys
from pathlib import Path


def fix_file(path: Path) -> None:
    text = path.read_text()
    original = text

    # Replace "from typing import ..., Annotated, ..., Literal, ..."
    # with the same but importing Annotated/Literal from typing_extensions
    needs_extensions = []
    if "Annotated" in text:
        needs_extensions.append("Annotated")
        text = re.sub(r",\s*Annotated", "", text)
        text = re.sub(r"Annotated,\s*", "", text)
    if "Literal" in text:
        needs_extensions.append("Literal")
        text = re.sub(r",\s*Literal", "", text)
        text = re.sub(r"Literal,\s*", "", text)

    if needs_extensions and "from typing_extensions import" not in text:
        ext_import = "from typing_extensions import {}".format(", ".join(sorted(needs_extensions)))
        text = text.replace("from typing import", f"{ext_import}\nfrom typing import", 1)

    # Clean empty typing imports
    text = re.sub(r"from typing import\s*\n", "", text)

    if text != original:
        path.write_text(text)
        print(f"  Fixed: {path}")


def fix_base_client_import(target: Path) -> None:
    """Remove the copied base_client.py and rewrite imports to use the canonical one."""
    copied = target / "base_client.py"
    if copied.exists():
        copied.unlink()
        print(f"  Removed: {copied}")

    for py_file in sorted(target.glob("*.py")):
        text = py_file.read_text()
        original = text
        # Rewrite relative import to absolute
        text = text.replace(
            "from .base_client import GitHubBaseClient",
            "from revup.github.base_client import GitHubBaseClient",
        )
        # Remove re-export line from __init__.py (now an absolute import, not needed)
        if py_file.name == "__init__.py":
            text = re.sub(r"from revup\.github\.base_client import GitHubBaseClient\n", "", text)
            text = re.sub(r'    "GitHubBaseClient",\n', "", text)
        if text != original:
            py_file.write_text(text)
            print(f"  Fixed base_client import: {py_file}")


def main() -> None:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("revup/github/graphql_client")
    if not target.is_dir():
        print(f"Error: {target} is not a directory", file=sys.stderr)
        sys.exit(1)
    fix_base_client_import(target)
    for py_file in sorted(target.glob("*.py")):
        fix_file(py_file)


if __name__ == "__main__":
    main()
