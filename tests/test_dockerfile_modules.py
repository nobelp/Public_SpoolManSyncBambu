"""Jedes vom Dienst importierte Projektmodul muss im Dockerfile kopiert werden.

Hintergrund: Das Dockerfile kopiert die Python-Dateien einzeln. Ein neues Modul
(z.B. safe_fetch.py, Issue #46), das dort fehlt, laeuft lokal und in den Tests,
laesst den Container auf der NAS aber beim Start mit ImportError abstuerzen -
erst beim Deploy bemerkt."""
import ast
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ok = True


def check(label, cond, detail=""):
    global ok
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  → {detail}" if detail and not cond else ""))
    ok = ok and bool(cond)


dockerfile = open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8").read()
copied = set()
for line in dockerfile.splitlines():
    if line.startswith("COPY ") and ".py" in line:
        copied.update(token for token in line.split()[1:-1] if token.endswith(".py"))

local_modules = {f[:-3] for f in os.listdir(ROOT) if f.endswith(".py")}
entrypoints = set()
for script in os.listdir(ROOT):
    if script.startswith("entrypoint") and script.endswith(".sh"):
        entrypoints.update(re.findall(r"python3?\s+(?:-u\s+)?(\w+\.py)", open(os.path.join(ROOT, script), encoding="utf-8").read()))

print("1) Aufgerufene Skripte sind im Image")
check("Entrypoints rufen Python-Dateien auf", len(entrypoints) >= 3, str(entrypoints))
for script in sorted(entrypoints):
    check(f"{script} wird kopiert", script in copied)

print("2) Importierte Projektmodule sind im Image")
for source in sorted(copied):
    tree = ast.parse(open(os.path.join(ROOT, source), encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    for module in sorted(imported & local_modules):
        check(f"{source} -> {module}.py wird kopiert", f"{module}.py" in copied)

check("safe_fetch.py (SSRF-Schutz) ist im Image", "safe_fetch.py" in copied)
print("\nERGEBNIS:", "ALLE TESTS PASS" if ok else "FEHLER")
raise SystemExit(0 if ok else 1)
