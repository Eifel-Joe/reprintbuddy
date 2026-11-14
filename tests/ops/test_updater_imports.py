"""The update helper and what it uses for the way back import everything at the top (Spec 11.3a "Rückweg").

pip replaces the package files while the helper runs; on the way back to 1.0.0 the modules of the way
back are gone afterwards. An import inside a function would then load new code, or none at all. Alembic
loads its scripts when it is used, so all Alembic work lies before pip (`updater._run_return`).
"""

import ast
from pathlib import Path

import pytest

OPS = Path(__file__).resolve().parents[2] / "src" / "reprintbuddy" / "ops"
MODULES = ("updater.py", "migrationsteps.py")


def late_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            for inner in ast.walk(node):
                if isinstance(inner, (ast.Import, ast.ImportFrom)):
                    found.append(f"{path.name}:{inner.lineno}")
    return found


@pytest.mark.parametrize("name", MODULES)
def test_no_import_inside_a_function(name):
    assert late_imports(OPS / name) == []


def test_the_check_finds_a_late_import(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text("import os\n\ndef f():\n    import json\n    return json\n", encoding="utf-8")
    assert late_imports(sample) == ["sample.py:4"]


def test_the_helper_imports_the_modules_of_the_way_back_at_the_top():
    tree = ast.parse((OPS / "updater.py").read_text(encoding="utf-8"))
    top = {node.module for node in tree.body if isinstance(node, ast.ImportFrom)}
    top |= {f"{node.module}.{alias.name}" for node in tree.body if isinstance(node, ast.ImportFrom) for alias in node.names}
    assert "reprintbuddy.ops.migrationsteps" in top
