import ast
from pathlib import Path

import pytest

import app.engine

ENGINE_DIR = Path(app.engine.__file__).parent
FORBIDDEN = ("app.db", "app.api", "app.collectors", "app.services", "app.pubsub", "app.workers")
FORBIDDEN_LIBS = ("httpx", "redis", "sqlalchemy", "asyncpg", "fastapi")


def imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


@pytest.mark.parametrize("path", sorted(ENGINE_DIR.glob("*.py")), ids=lambda path: path.name)
def test_engine_has_no_io_dependencies(path: Path) -> None:
    """CLAUDE.md, розділ 17: engine — чисті функції, без БД, HTTP і Redis."""
    for module in imported_modules(path):
        assert not module.startswith(FORBIDDEN), f"{path.name} імпортує {module}"
        assert module.split(".")[0] not in FORBIDDEN_LIBS, f"{path.name} імпортує {module}"
