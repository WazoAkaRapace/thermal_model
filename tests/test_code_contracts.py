"""Guard tests for code-level decisions recorded in the plan.

These are cheap and they catch a regression that a reviewer already had to
find once: a task text (or a copy-paste) that reintroduces a forbidden import.
"""

from __future__ import annotations

import ast
from pathlib import Path

COMPONENT_DIR = Path(__file__).parents[1] / "custom_components" / "thermal_model"
FORBIDDEN = ("numpy", "scipy", "pandas", "sklearn")


def _top_level_imports(path: Path) -> set[str]:
    """Return the set of top-level module names imported by a Python file."""
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module.split(".")[0])
    return modules


def test_no_forbidden_native_dependencies() -> None:
    """Standing decision: pure-Python math only (install liability)."""
    offenders: dict[str, set[str]] = {}
    for path in COMPONENT_DIR.rglob("*.py"):
        bad = _top_level_imports(path) & set(FORBIDDEN)
        if bad:
            offenders[str(path.relative_to(COMPONENT_DIR))] = bad
    assert not offenders, f"forbidden imports found: {offenders}"


def test_package_imports_are_stdlib_or_homeassistant() -> None:
    """Keep the dependency surface explicit: stdlib, voluptuous or HA only."""
    allowed_prefixes = ("homeassistant", "voluptuous")
    stdlib_or_allowed = {
        "__future__",
        "abc",
        "asyncio",
        "collections",
        "dataclasses",
        "datetime",
        "enum",
        "json",
        "logging",
        "math",
        "sqlite3",
        "statistics",
        "typing",
    }
    unexpected: dict[str, set[str]] = {}
    for path in COMPONENT_DIR.rglob("*.py"):
        modules = _top_level_imports(path)
        bad = {
            module
            for module in modules
            if module not in stdlib_or_allowed
            and not module.startswith(allowed_prefixes)
            and module not in ("const",)
            and module not in {p.stem for p in COMPONENT_DIR.rglob("*.py")}
        }
        if bad:
            unexpected[str(path.relative_to(COMPONENT_DIR))] = bad
    assert not unexpected, f"unexpected imports: {unexpected}"
