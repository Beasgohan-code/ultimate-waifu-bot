#!/usr/bin/env python3
"""Extract *everything* from a Summon-bot checkout — including what its source lost.

Why this exists: the repository at HEAD is not the bot that ran. Character uploads
(``upload_character``, ``upload_callback``, ``upload_to_catbox``, ``upload_to_imgbb``) were
deleted from ``commands_admin.py`` in favour of "URL-only", and ``api.py`` is gone
altogether — but both survive in the committed ``__pycache__/*.cpython-313.pyc`` from the
Termux deployment that ran the older tree. Reading only the ``.py`` files therefore misses
features; reading only the ``.pyc`` misses what was written since. This walks both and
reports the difference, so a port can be checked against the *whole* project instead of a
lucky grep.

Usage::

    python scripts/extract_summon_reference.py PATH_TO_SUMMON_BOT [--json]

The bytecode half needs ``xdis`` (it understands foreign CPython versions; the interpreter
running this script normally does not). Without it the source inventory is still produced
and the report says so — a partial answer that admits it is partial, which is the
difference between a tool and a lie.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

PYC_PRIORITY = ("cpython-313.pyc", "cpython-312.pyc", "cpython-314.pyc", "cpython-311.pyc")

#: Not project code — dependency, session, data and log noise that lives in the tree.
NOISE = re.compile(r"(__pycache__|\.venv|node_modules|bot\.log|bot\.session|\.git)")


@dataclass
class Module:
    """One Python module, from both views of it."""

    name: str
    source_path: Path | None = None
    bytecode_path: Path | None = None
    source_funcs: dict[str, int] = field(default_factory=dict)
    bytecode_funcs: dict[str, int] = field(default_factory=dict)
    source_classes: dict[str, list[str]] = field(default_factory=dict)
    doc: str = ""
    #: Set when a ``.pyc`` could not be decoded — a report that silently reads *most* of
    #: the caches would be worse than one that says which one it could not.
    bytecode_error: str = ""

    @property
    def removed(self) -> list[str]:
        """In the bytecode, not in the source: features the tree no longer ships."""
        return sorted(set(self.bytecode_funcs) - set(self.source_funcs))

    @property
    def added(self) -> list[str]:
        return sorted(set(self.source_funcs) - set(self.bytecode_funcs))

    @property
    def all_names(self) -> list[str]:
        return sorted(set(self.source_funcs) | set(self.bytecode_funcs))


def _rank(pyc_name: str) -> int:
    """Preference order for bytecode flavours (lower is newer/more trustworthy)."""
    for index, tag in enumerate(PYC_PRIORITY):
        if pyc_name.endswith(tag):
            return index
    return len(PYC_PRIORITY)


def _from_source(path: Path, module: Module) -> None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError as exc:  # pragma: no cover - defensive
        module.doc = f"(unparseable: {exc})"
        return
    docstring = (ast.get_docstring(tree) or "").strip().splitlines()
    module.doc = docstring[0] if docstring else ""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            module.source_funcs[node.name] = node.lineno
        elif isinstance(node, ast.ClassDef):
            methods = [
                item.name
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            module.source_classes[node.name] = methods


def _load_pyc(path: Path):
    """Return the top-level code object of a ``.pyc`` from any Python version."""
    try:
        from xdis import load_module  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        result = load_module(str(path))
    except Exception:
        return None
    code = result[3] if isinstance(result, tuple) and len(result) > 3 else None
    return code


def _walk_code(co, prefix: str, out: dict[str, int]) -> None:
    for const in getattr(co, "co_consts", ()) or ():
        if type(const).__name__.startswith("Code") or hasattr(const, "co_name"):
            name = getattr(const, "co_name", "?")
            key = f"{prefix}{name}"
            out.setdefault(key, int(getattr(const, "co_firstlineno", 0) or 0))
            _walk_code(const, f"{key}.", out)


def _from_bytecode(path: Path, module: Module) -> None:
    try:
        # xdis prints a full traceback for a truncated .pyc before raising; a report that a
        # human reads should not lead with somebody else's stack.
        import contextlib
        import io

        with contextlib.redirect_stderr(io.StringIO()):
            code = _load_pyc(path)
        if code is None:
            module.bytecode_error = "unreadable (xdis could not decode this cache)"
            return
        found: dict[str, int] = {}
        _walk_code(code, "", found)
        # Only top-level functions matter for the inventory (nested callbacks are noise).
        module.bytecode_funcs = {name: line for name, line in found.items() if "." not in name}
    except Exception as exc:
        module.bytecode_error = f"{type(exc).__name__}: {exc}"


def discover(root: Path) -> tuple[list[Module], list[Path]]:
    modules: dict[str, Module] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        # ``__pycache__`` is the one piece of noise worth reading: it holds the compiled
        # copy of modules whose source has been edited or deleted since.
        is_pyc = path.suffix == ".pyc" and rel.parent.name == "__pycache__"
        if not is_pyc and (NOISE.search(str(rel)) or path.suffix in {".log", ".session"}):
            continue
        if path.suffix == ".py":
            module = modules.setdefault(path.stem, Module(name=path.stem))
            if module.source_path is None:
                module.source_path = path
                _from_source(path, module)
        elif path.suffix == ".pyc" and rel.parent.name == "__pycache__":
            # ``commands_admin.cpython-313.pyc`` — the interpreter tag is a *suffix* of the
            # stem, and the newest one wins: 3.13 is the deployment's Python, 3.14 is a
            # later local run of the newer tree.
            stem = path.name.split(".")[0]
            module = modules.setdefault(stem, Module(name=stem))
            rank = _rank(path.name)
            if rank < _rank(module.bytecode_path.name if module.bytecode_path else ""):
                module.bytecode_path = path
                module.bytecode_funcs = {}
                _from_bytecode(path, module)
    # A .pyc whose source was deleted still deserves a row — that is the point.
    ordered = sorted(modules.values(), key=lambda item: item.name)
    assets = [
        path.relative_to(root)
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and not NOISE.search(str(path.relative_to(root)))
        and path.suffix not in {".py", ".pyc"}
        and path.parent.name != "__pycache__"
    ]
    return ordered, assets


def load_map(path: Path | None) -> dict:
    """The hand-maintained audit map (module -> where it went, or why it did not)."""
    if path is None or not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"{path}: {exc}") from exc


def _portable_name(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _reference_head(root: Path) -> str:
    """The clone's commit, so the report says which snapshot it describes."""
    import shutil
    import subprocess

    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, read-only git query on a path we chose
            [str(shutil.which("git") or "git"), "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:  # no git, no problem: the section degrades to "unknown"
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def sqlite_schema(root: Path, *, limit: int = 80) -> tuple[str, list[tuple[str, int]]]:
    """``CREATE`` statements plus row counts of a ``summon.db`` found in the tree.

    Opened read-only, and only the *shape* is reported: the schema is what a port has to
    speak to, while the rows are somebody's live players. An unreadable file yields an empty
    section rather than a failed report, because the report's job is to describe what is there.
    """
    database = root / "summon.db"
    if not database.is_file():  # pragma: no cover - depends on the clone
        return "", []
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    except sqlite3.Error:  # pragma: no cover
        return "", []
    statements: list[str] = []
    counts: list[tuple[str, int]] = []
    try:
        rows = conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type DESC, name"
        ).fetchall()
        for kind, name, sql in rows[:limit]:
            statements.append(f"{str(sql).strip()}  -- {kind.upper()} {name}")
        for kind, name, _sql in rows:
            if kind != "table":
                continue
            try:
                total = int(
                    conn.execute(f"SELECT COUNT(*) FROM {_portable_name(name)}").fetchone()[0]
                )
            except sqlite3.Error:  # pragma: no cover - listed but unreadable
                total = -1
            counts.append((name, total))
    finally:
        conn.close()
    return "\n".join(statements), counts


def render(
    modules: list[Module],
    assets: list[Path],
    *,
    xdis: bool,
    root: Path,
    map_path: Path | None = None,
) -> str:
    port_map = load_map(map_path)
    lines = [
        "<!-- GENERATED by scripts/extract_summon_reference.py — do not edit. Re-run:",
        "     make extract  (needs xdis: pip install -e '.[dev]'; reads .scratch/summon-ref)",
        "-->",
        "",
        "# Everything in Summon-bot, inventoried from source *and* bytecode",
        "",
        "The reference repository's `.py` files are one snapshot of one branch; its",
        "`__pycache__` is a snapshot of what the running bot actually had. The two disagree,",
        "and the disagreement is where the interesting features live — character uploads among",
        "them. This page is the raw material the port is checked against: every function in",
        "every module, marked **lost** when it exists only in bytecode, plus the non-Python",
        "assets the code assumes exist.",
        "",
        f"Bytecode decoding: {'`xdis` available' if xdis else '**xdis not installed — bytecode not read**'}.",
        f"Reference commit: `{_reference_head(root) or 'unknown'}`.",
        "",
    ]
    preamble = port_map.get("preamble") or []
    if preamble:
        lines += ["## How to read this", "", *[str(line) for line in preamble], ""]
    lost_total = sum(len(module.removed) for module in modules)
    lines += [
        f"**{len(modules)} modules**, **{sum(len(m.all_names) for m in modules)} functions**,",
        f"**{lost_total} functions present only in bytecode** (i.e. deleted from the source tree).",
        "",
        "| module | source | funcs (source) | funcs (bytecode) | only in bytecode | only in source |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    unreadable = [module for module in modules if module.bytecode_error]
    if unreadable:
        lines += [
            "",
            f"⚠️ {len(unreadable)} bytecode cache(s) could not be decoded and are reported from "
            "source only: " + ", ".join(f"`{m.name}`" for m in unreadable) + ".",
        ]
    for module in modules:
        state = (
            "both"
            if module.source_path and module.bytecode_path
            else ("source only" if module.source_path else "**bytecode only (source deleted)**")
        )
        lines.append(
            f"| `{module.name}` | {state} | {len(module.source_funcs)} | {len(module.bytecode_funcs)} "
            f"| {', '.join(f'`{n}`' for n in module.removed) or '—'} "
            f"| {', '.join(f'`{n}`' for n in module.added) or '—'} |"
        )
    lines.append("")
    for module in modules:
        if not module.all_names and not module.source_classes:
            continue
        lines.append(f"## `{module.name}`")
        lines.append("")
        if module.doc:
            lines.append(f"> {module.doc[:200]}")
            lines.append("")
        if module.removed:
            lines.append(
                "**Only in the bytecode — this is code the tree no longer ships, and the port"
                " has to read it from there:** " + ", ".join(f"`{name}`" for name in module.removed)
            )
            lines.append("")
        if module.source_funcs or module.bytecode_funcs:
            lines.append("| function | line | where |")
            lines.append("| --- | --- | --- |")
            for name in module.all_names:
                in_source = name in module.source_funcs
                in_byte = name in module.bytecode_funcs
                where = (
                    "source + bytecode"
                    if in_source and in_byte
                    else ("source" if in_source else "**bytecode only — lost**")
                )
                line = module.source_funcs.get(name) or module.bytecode_funcs.get(name) or 0
                lines.append(f"| `{name}` | {line} | {where} |")
            lines.append("")
        for cls, methods in sorted(module.source_classes.items()):
            lines.append(
                f"`{cls}` ({len(methods)} methods): " + ", ".join(f"`{m}`" for m in methods)
            )
        if module.source_classes:
            lines.append("")
    lines += [
        "## Non-Python assets in the tree",
        "",
        "Data, fonts, logs and deployment files the code expects — each one is a fact about",
        "the deployment a port has to answer with a setting or a file of its own.",
        "",
        "| file | size |",
        "| --- | --- |",
    ]
    for asset in assets:
        try:
            size = (root / asset).stat().st_size
        except OSError:  # pragma: no cover - a dangling entry in the listing
            size = 0
        lines.append(f"| `{asset}` | {size:,} |")
    lines.append("")

    entries = port_map.get("modules") or {}
    lines += [
        "## Port map",
        "",
        "One row per module above. `in this repo` is a path in *this* repository, so every",
        "claim is one `git grep` away from being checked.",
        "",
        "| module | status | in this repo | what happened to it |",
        "| --- | --- | --- | --- |",
    ]
    for module in modules:
        entry = entries.get(module.name) or {}
        where = ", ".join(f"`{path}`" for path in entry.get("where", [])) or "—"
        status = entry.get("status") or "**unmapped**"
        note = str(entry.get("note") or "").replace("\n", " ")
        lines.append(f"| `{module.name}` | {status} | {where} | {note} |")
    lines.append("")

    names = {module.name for module in modules}
    unmapped = sorted(names - set(entries))
    if unmapped:
        lines += [
            f"⚠️ {len(unmapped)} module(s) have **no entry in `scripts/summon_port_map.json`**: "
            + ", ".join(f"`{name}`" for name in unmapped)
            + ". An unmapped module is exactly what this document exists to catch.",
            "",
        ]
    stale = sorted(set(entries) - names)
    if stale:
        lines += [
            f"⚠️ {len(stale)} map ent(ies) name a module the reference no longer has: "
            + ", ".join(f"`{name}`" for name in stale)
            + " — fix the clone or delete the row.",
            "",
        ]
    tally: dict[str, int] = {}
    for module in modules:
        status = str((entries.get(module.name) or {}).get("status") or "unmapped")
        tally[status] = tally.get(status, 0) + 1
    lines += [
        "**Totals:** "
        + " · ".join(f"{status} {count}" for status, count in sorted(tally.items()))
        + f" across {len(modules)} modules.",
        "",
    ]

    schema, counts = sqlite_schema(root)
    if schema:
        lines += [
            "## Reference database schema (`summon.db`)",
            "",
            "The clone ships a real deployment's database. Its rows are not part of this audit",
            "(they are somebody's players); its *shape* is, because that is what a port has to",
            "speak to. Counts show what a working deployment actually holds.",
            "",
            "```sql",
            schema,
            "```",
            "",
            "| table | rows |",
            "| --- | --- |",
        ]
        lines += [
            f"| `{name}` | {'unreadable' if count < 0 else f'{count:,}'} |"
            for name, count in counts
        ]
        lines.append("")

    for wanted in ("requirements.txt", "runtime.txt"):
        handle = root / wanted
        if handle.is_file():
            body = handle.read_text(encoding="utf-8", errors="replace").strip()
            lines += [f"### `{wanted}`", "", "```", body, "```", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", help="path to a Summon-bot checkout")
    parser.add_argument("--json", action="store_true", help="machine-readable inventory")
    parser.add_argument(
        "--map",
        default=str(Path(__file__).with_name("summon_port_map.json")),
        help="audit map (module -> where it went) rendered alongside the inventory",
    )
    parser.add_argument(
        "--out",
        default="",
        help="write the markdown here instead of stdout (parent directories are created)",
    )
    args = parser.parse_args(argv)
    root = Path(args.root)
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2
    modules, assets = discover(root)
    if args.json:
        print(
            json.dumps(
                {
                    module.name: {
                        "source": str(module.source_path.relative_to(root))
                        if module.source_path
                        else "",
                        "bytecode": str(module.bytecode_path.relative_to(root))
                        if module.bytecode_path
                        else "",
                        "source_funcs": module.source_funcs,
                        "bytecode_funcs": module.bytecode_funcs,
                        "classes": module.source_classes,
                    }
                    for module in modules
                },
                indent=2,
            )
        )
        return 0
    body = render(
        modules,
        assets,
        xdis=_xdis_available(),
        root=root,
        map_path=Path(args.map) if args.map else None,
    )
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body + "\n", encoding="utf-8")
        print(f"wrote {target}")
    else:
        print(body)
    return 0


def _xdis_available() -> bool:
    try:
        import xdis  # noqa: F401  # type: ignore[import-not-found]

        return True
    except ImportError:
        return False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
