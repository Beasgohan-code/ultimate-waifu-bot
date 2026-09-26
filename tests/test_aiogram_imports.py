"""Every aiogram import in the codebase must name something aiogram ships.

Two of these shipped broken (``BotCommandScopeChatAdmins`` in the command
menu, ``WebAppButtonInfo`` in the mini-app plugin): invented type names in
*lazy* imports that no test ever executed — each one a deploy-day crash.
This audit turns the whole codebase into a test, so the next invented name
fails here, not on the platform.
"""

from __future__ import annotations

import ast
import importlib
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _aiogram_imports() -> list[tuple[pathlib.Path, ast.ImportFrom]]:
    found: list[tuple[pathlib.Path, ast.ImportFrom]] = []
    for path in (ROOT / "waifu").rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("aiogram")
            ):
                found.append((path, node))
    return found


def test_aiogram_imports_resolve_against_the_installed_version() -> None:
    missing: list[str] = []
    for path, node in _aiogram_imports():
        module = importlib.import_module(node.module)
        for alias in node.names:
            if alias.name != "*" and not hasattr(module, alias.name):
                missing.append(f"{path.relative_to(ROOT)}: {node.module}.{alias.name}")
    assert not missing, "aiogram import names that do not exist:\n" + "\n".join(missing)
