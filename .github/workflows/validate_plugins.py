"""???? CI??? plugins/*/plugin.json + ???? main.py?"""
from __future__ import annotations
import ast, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
PLUGINS = ROOT / "plugins"
FORBIDDEN = {"subprocess", "socket", "ctypes", "pickle", "marshal", "multiprocessing", "pty", "telnetlib", "asyncio.subprocess"}
SDK = "mediaclaw_plugins.sdk"
REQUIRED = {"id", "name", "version", "entry"}
def main() -> int:
    errors: list[str] = []
    count = 0
    if not PLUGINS.is_dir():
        print("plugins/ ????????")
        return 0
    for pkg in sorted(PLUGINS.iterdir()):
        if not pkg.is_dir() or pkg.name.startswith("."):
            continue
        count += 1
        mp = pkg / "plugin.json"
        if not mp.is_file():
            errors.append(f"{pkg.name}: ?? plugin.json")
            continue
        try:
            manifest = json.loads(mp.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"{pkg.name}: plugin.json ???? - {exc}")
            continue
        missing = REQUIRED - set(manifest)
        if missing:
            errors.append(f"{pkg.name}: ???? {sorted(missing)}")
        entry = pkg / manifest.get("entry", "main.py")
        if not entry.is_file():
            errors.append(f"{pkg.name}: ?? {entry.name} ???")
        for pyf in pkg.rglob("*.py"):
            if "__pycache__" in pyf.parts:
                continue
            try:
                tree = ast.parse(pyf.read_text(encoding="utf-8"))
            except SyntaxError as exc:
                errors.append(f"{pkg.name}/{pyf.name}: ???? {exc.msg}")
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] in FORBIDDEN:
                            errors.append(f"{pkg.name}/{pyf.name}: ???? {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    if node.module in FORBIDDEN:
                        errors.append(f"{pkg.name}/{pyf.name}: ???? {node.module}")
                    elif node.module.startswith("mediaclaw_") and node.module != SDK:
                        errors.append(f"{pkg.name}/{pyf.name}: ???????? {node.module}")
    print(f"checked {count} plugins")
    if errors:
        for e in errors:
            print("FAIL:", e)
        return 1
    print("all plugins OK")
    return 0
if __name__ == "__main__":
    sys.exit(main())
