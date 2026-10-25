"""Regressionstest: Die Kopfzeile des Druckverlaufs zeigte nach einem Release weiter
die alte Version (z.B. v4.0.9-2-g85f8eee), weil sie `git describe`/.version nutzte.
Der NAS-Checkout holt per `git pull` keine Tags. Massgeblich ist die Datei VERSION
(wie bei /api/version und der HA-Anzeige)."""
import os
import re
import tempfile

tmp = tempfile.mkdtemp()
os.environ.update({
    "CONSUMPTION_STATE_FILE": os.path.join(tmp, "state.json"),
    "CONSUMPTION_LOG_FILE": os.path.join(tmp, "c.log"),
    "CONSUMPTION_HISTORY_FILE": os.path.join(tmp, "h.json"),
    "CONSUMPTION_HISTORY_DB": os.path.join(tmp, "h.db"),
    "CONSUMPTION_HISTORY_IMAGES_DIR": os.path.join(tmp, "img"),
    "WARTUNG_BACKUP_FILE": os.path.join(tmp, "w.json"),
    "CONSUMPTION_OVERRIDES_FILE": os.path.join(tmp, "o.json"),
})

import consumption as c

ok = True


def check(label, cond, detail=""):
    global ok
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  → {detail}" if detail and not cond else ""))
    ok = ok and cond


c.init_history_storage()
release = open(os.path.join(os.path.dirname(os.path.abspath(c.__file__)), "VERSION")).read().strip()

c._get_git_version = lambda: "v0.0.1-stale-2-gdeadbee"  # so liefert der NAS-Checkout ohne Tags


def header_version(info):
    c._get_git_info = lambda: info
    page = c._render_history_page("desc")
    return page, re.search(r"<h2>Druckverlauf <span[^>]*>\((.*?)\)</span>", page).group(1)


page, shown = header_version({"version": release, "commit": "3f11a76"})
check("Kopfzeile zeigt Release-Version und Commit, getrennt durch Leerzeichen", shown == f"v{release} 3f11a76", shown)
check("Kein git-describe-Rest (-N-g) in der Kopfzeile", "stale" not in page and "-g3f11a76" not in shown)
_, shown = header_version({"version": release, "commit": "unknown"})
check("Ohne Commit-Info nur die Version", shown == f"v{release}", shown)

print("\nOK" if ok else "\nFEHLER")
raise SystemExit(0 if ok else 1)
