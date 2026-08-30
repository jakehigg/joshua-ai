#!/usr/bin/env python3
"""Fail the pipeline on two test-hygiene violations.

1. A `skip` or `xfail` marker with no `reason` that names an issue (`#<number>`).
2. A member source module with no test that imports it.

Run from the repository root. Exit 0 when clean, 1 when a rule is broken.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

MEMBERS = ("shared", "channels", "core", "gateway")
EXCLUDED_MODULES = ("__init__.py", "__main__.py")
SKIP_XFAIL = re.compile(r"pytest\.mark\.skip\(|xfail\(")
REASON_WITH_ISSUE = re.compile(r"""reason\s*=\s*(['"])(?:(?!\1).)*#\d+""")


def _call_body(text: str, open_paren: int) -> str:
    """Return the text between the parentheses of a call, parentheses balanced."""
    depth = 0
    for index in range(open_paren, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1 : index]
    return text[open_paren + 1 :]


def check_skip_reasons(test_files: list[Path]) -> list[str]:
    problems: list[str] = []
    for path in test_files:
        text = path.read_text(encoding="utf-8")
        for match in SKIP_XFAIL.finditer(text):
            open_paren = text.index("(", match.start())
            body = _call_body(text, open_paren)
            if not REASON_WITH_ISSUE.search(body):
                line = text.count("\n", 0, match.start()) + 1
                problems.append(
                    f'{path}:{line}: {match.group().rstrip("(")} needs reason="… #<issue>"'
                )
    return problems


def _imported_modules(path: Path) -> set[str]:
    """Return every module name a test file imports, with dotted parents."""
    names: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            for alias in node.names:
                names.add(f"{node.module}.{alias.name}")
    return names


def check_module_coverage(root: Path) -> list[str]:
    problems: list[str] = []
    for member in MEMBERS:
        package = f"joshua_{member}"
        package_dir = root / member / package
        tests_dir = root / member / "tests"
        if not package_dir.is_dir():
            continue
        imported: set[str] = set()
        for test_file in sorted(tests_dir.rglob("test_*.py")):
            imported |= _imported_modules(test_file)
        for source in sorted(package_dir.rglob("*.py")):
            if source.name in EXCLUDED_MODULES:
                continue
            parts = source.relative_to(package_dir).with_suffix("").parts
            module = ".".join((package, *parts))
            if module not in imported:
                problems.append(f"{source}: no test in {member}/tests imports {module}")
    return problems


def _project_test_files(root: Path) -> list[Path]:
    """Return the test files under each member's own tests directory.

    Only project test directories are scanned. Dependency and build-artifact
    directories (`.uv-cache`, `.venv`, `.git`) hold vendored test suites that
    the policy must never flag.
    """
    files: list[Path] = []
    for member in MEMBERS:
        tests_dir = root / member / "tests"
        if tests_dir.is_dir():
            files.extend(sorted(tests_dir.rglob("test_*.py")))
    return files


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    test_files = _project_test_files(root)
    problems = check_skip_reasons(test_files) + check_module_coverage(root)
    if problems:
        print("test-policy: violations found:")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print("test-policy: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
