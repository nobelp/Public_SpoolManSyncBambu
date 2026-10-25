#!/usr/bin/env python3
"""
DIV_SpoolManSyncBambu - shop_links.py
Ergaenzt fuer Bambu-Filamente mit ColorID die Shop-Links "Bambu Lab"
(url_bambulab) und "CCTREE" (url_cctree) automatisch (GitHub Issue #35).

  Bambu Lab: Die Store-Produktseiten listen im JSON-LD alle Varianten mit der
             ColorID im Namen ("PLA Basic - Schwarz (10101) / Nachfuellung / 1 kg")
             und dem Link "?id=<Variante>". Bevorzugt: Nachfuellung, sonst Spule.
  CCTREE:    Die CCTREE-Store-Seite auf Amazon liefert die Produkt-ASINs; jede
             Produktseite enthaelt alle Farbvarianten mit Bambu-Farbcode
             ("dimensionValuesDisplayData": {"B0F8TRWHJ8": ["Grau 10103"]}).

Grundsaetze: von Hand eingetragene Links werden nie ueberschrieben; jeder
Treffer wird geprueft (ColorID in Variante/Titel, Preis lesbar); fehlende
Angebote werden gemerkt und nur selten erneut gesucht; jede Aenderung wird
geloggt. Der Seitenabruf wird uebergeben - dadurch ohne Netzwerk testbar.
"""

import json
import logging
import os
import re
import threading
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

import price_check as pc

log = logging.getLogger("consumption.shop_links")

STATE_FILE = os.environ.get("SHOP_LINKS_STATE_FILE", "/app/data/shop_links_state.json")
RUN_INTERVAL_S = float(os.environ.get("SHOP_LINKS_INTERVAL_H", "6")) * 3600
MISSING_RETRY_S = 7 * 24 * 3600      # "gibt es nicht" erst nach einer Woche erneut suchen
PAGE_DELAY_S = 2                     # Pause zwischen zwei Seiten desselben Shops
MAX_EVENTS = 50

BAMBU_DOMAINS = ("bambulab.com",)
BAMBU_BASE = "https://eu.store.bambulab.com/de/products/"
# Produktlinien des Bambu-Stores, in denen nach ColorIDs gesucht wird
BAMBU_PRODUCT_SLUGS = (
    "pla-basic-filament", "pla-matte", "pla-galaxy", "pla-wood", "pla-silk-multi-color",
    "pla-silk-upgrade", "petg-basic", "pla-sparkle", "pla-marble", "pla-metal", "petg-hf",
    "pla-tough-upgrade", "petg-translucent", "pla-translucent",
)
CCTREE_STORE_URL = "https://www.amazon.de/stores/CCTREE/page/9C3DC9D1-CEE4-4546-8152-D55BAEA61C75"
# Angebote, die auf der Store-Seite nicht verlinkt sind (z.B. "PETG Basic Refill"),
# als zusaetzliche Startpunkte - jede Produktseite liefert alle Farben des Angebots
CCTREE_EXTRA_ASINS = ("B0FX9YMBXY",)

FIELD_BAMBU = "url_bambulab"
FIELD_CCTREE = "url_cctree"

_COLORID_IN_NAME = re.compile(r"\((\d{5})\)")
_COLORID_IN_LABEL = re.compile(r"\b(\d{5})\b")
_ASIN_RE = re.compile(r'"(B0[A-Z0-9]{8})"')

_run_lock = threading.Lock()
_state_lock = threading.Lock()


class PageBlocked(Exception):
    """Shop zeigt eine Bot-Schutzseite - spaeter erneut versuchen, nicht als 'fehlt' merken."""


# ─── Seitenabruf ─────────────────────────────────────────────────────────────

def fetch_page(url, domains, retries=3, retry_delay_s=6):
    """HTML einer Shop-Seite (SSRF-geprueft ueber price_check). Amazon zeigt
    zufaellig eine Bot-Schutzseite - dann bis zu `retries` Versuche, danach
    PageBlocked. Andere Fehler -> Exception."""
    last = None
    for attempt in range(retries):
        status, page = pc._get_with_checked_redirects(url, domains)
        if status == 200 and not _looks_blocked(page):
            return page
        last = f"HTTP {status}" if status != 200 else "Bot-Schutzseite"
        if attempt < retries - 1:
            time.sleep(retry_delay_s)
    raise PageBlocked(f"{urlsplit(url).hostname}: {last}")


def _looks_blocked(page):
    small = page.lower() if len(page) < 50000 else ""
    return "captcha" in small or "um mit dem einkauf fortzufahren" in small


# ─── Bambu Lab ───────────────────────────────────────────────────────────────

def parse_bambu_variants(page_html):
    """[(colorid, name, url, price, currency)] aus einer Bambu-Store-Produktseite."""
    soup = BeautifulSoup(page_html, "html.parser")
    out = []
    for cand in pc._jsonld_candidates(soup):
        m = _COLORID_IN_NAME.search(cand.get("name") or "")
        if not m or not cand.get("offer_url"):
            continue
        url = cand["offer_url"].replace("eu.store.bambulab.com/products/", "eu.store.bambulab.com/de/products/")
        out.append((m.group(1), cand["name"], url, cand.get("price"), cand.get("currency")))
    return out


def pick_bambu_variant(variants):
    """Nachfuellung vor Spule vor erster Variante."""
    for pattern in (r"Nachfüll|Refill", r"Spule|Spool"):
        for v in variants:
            if re.search(pattern, v[1], re.IGNORECASE):
                return v
    return variants[0] if variants else None


def find_bambu_links(colorids, fetch=None):
    """{colorid: {"url", "name", "price", "currency"}} fuer die gesuchten ColorIDs.
    Durchsucht die Produktlinien nur, bis alle gefunden sind."""
    fetch = fetch or (lambda url: fetch_page(url, BAMBU_DOMAINS, retries=2, retry_delay_s=3))
    wanted = set(colorids)
    by_colorid = {}
    for i, slug in enumerate(BAMBU_PRODUCT_SLUGS):
        if not wanted - set(by_colorid):
            break
        if i:
            time.sleep(PAGE_DELAY_S)
        try:
            page = fetch(BAMBU_BASE + slug)
        except Exception as e:
            log.info(f"Shop-Links: Bambu-Store {slug} nicht lesbar ({e})")
            continue
        for v in parse_bambu_variants(page):
            if v[0] in wanted:
                by_colorid.setdefault(v[0], []).append(v)
    result = {}
    for cid, variants in by_colorid.items():
        v = pick_bambu_variant(variants)
        if v and v[3] is not None:  # Pruefung: Variante hat ColorID im Namen (Parser) und lesbaren Preis
            result[cid] = {"url": v[2], "name": v[1], "price": v[3], "currency": v[4]}
    return result


# ─── CCTREE (Amazon) ─────────────────────────────────────────────────────────

def parse_store_asins(page_html):
    return sorted(set(_ASIN_RE.findall(page_html)))


def parse_cctree_variants(page_html):
    """{asin: label} aus den Farbvarianten einer Amazon-Produktseite."""
    m = re.search(r'"dimensionValuesDisplayData"\s*:\s*(\{.*?\})\s*,\s*"', page_html, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return {}
    return {asin: " ".join(v) if isinstance(v, list) else str(v) for asin, v in data.items()
            if re.fullmatch(r"B0[A-Z0-9]{8}", asin)}


def _product_title(page_html):
    tag = BeautifulSoup(page_html, "html.parser").select_one("#productTitle")
    return tag.get_text(" ", strip=True) if tag else ""


def find_cctree_links(colorids, fetch=None):
    """{colorid: {"url", "title", "price", "currency"}} fuer die gesuchten ColorIDs.
    Raises PageBlocked, wenn Amazon die Store-Seite nicht liefert."""
    fetch = fetch or (lambda url: fetch_page(url, pc.AMAZON_DOMAINS))
    wanted = set(colorids)
    store = fetch(CCTREE_STORE_URL)  # PageBlocked -> spaeter erneut
    asin_by_colorid, seen = {}, set()
    for asin in list(CCTREE_EXTRA_ASINS) + [a for a in parse_store_asins(store) if a not in CCTREE_EXTRA_ASINS]:
        if not wanted - set(asin_by_colorid):
            break
        if asin in seen:
            continue
        time.sleep(PAGE_DELAY_S)
        try:
            page = fetch(f"https://www.amazon.de/dp/{asin}")
        except PageBlocked:
            continue
        variants = parse_cctree_variants(page) or {asin: _product_title(page)}
        for v_asin, label in variants.items():
            seen.add(v_asin)
            for cid in _COLORID_IN_LABEL.findall(label):
                if cid in wanted:
                    asin_by_colorid.setdefault(cid, v_asin)
    result = {}
    for cid, asin in asin_by_colorid.items():
        url = f"https://www.amazon.de/dp/{asin}"
        time.sleep(PAGE_DELAY_S)
        try:
            page = fetch(url)
        except PageBlocked:
            continue  # beim naechsten Lauf erneut
        title = _product_title(page)
        price = pc.extract_price(page, url)
        # Pruefung: CCTREE-Produkt mit genau dieser ColorID und lesbarem Preis
        if "CCTREE" in title.upper() and cid in title and price:
            result[cid] = {"url": url, "title": title[:120], "price": price["price"],
                           "currency": price.get("currency")}
    return result


# ─── Zustand ─────────────────────────────────────────────────────────────────

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(data):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def _colorid(fil):
    cid = pc._extra_value(fil, "colorid")
    return cid if re.fullmatch(r"\d{5}", cid or "") else None


def open_candidates(filaments, field, missing, now):
    """Filamente mit ColorID und leerem Feld, deren ColorID nicht kuerzlich
    als 'gibt es nicht' gemerkt wurde."""
    out = []
    for fil in filaments or []:
        cid = _colorid(fil)
        if not cid or pc._extra_value(fil, field):
            continue
        if now - float(missing.get(cid, 0)) < MISSING_RETRY_S:
            continue
        out.append((fil, cid))
    return out


def count_open(filaments, missing, filled=()):
    """Anzahl leerer Felder (Bambu Lab + CCTREE), die weder als 'kein Angebot'
    gemerkt noch im laufenden Durchgang ergaenzt wurden (filled: {(id, feld)})."""
    total = 0
    for fil in filaments or []:
        cid = _colorid(fil)
        if not cid:
            continue
        for field in (FIELD_BAMBU, FIELD_CCTREE):
            if (not pc._extra_value(fil, field) and cid not in missing.get(field, {})
                    and (fil.get("id"), field) not in filled):
                total += 1
    return total


# ─── Lauf ────────────────────────────────────────────────────────────────────

def run(filaments, write_link, fetch_bambu=None, fetch_amazon=None, now=None):
    """Ergaenzt fehlende Links. write_link(filament_id, field, url) schreibt nur,
    wenn das Feld in Spoolman (noch) leer ist, und liefert True bei Erfolg.
    Liefert eine Zusammenfassung; laeuft nie doppelt (None, falls schon aktiv)."""
    if not _run_lock.acquire(blocking=False):
        return None
    try:
        now = now or time.time()
        with _state_lock:
            state = load_state()
        missing = state.setdefault("missing", {})
        events = state.setdefault("events", [])
        summary = {"started": now, "filled": 0, "not_offered": 0, "blocked": [], "open": 0}
        filled = set()

        jobs = (
            ("Bambu Lab", FIELD_BAMBU, lambda cids: find_bambu_links(cids, fetch_bambu)),
            ("CCTREE", FIELD_CCTREE, lambda cids: find_cctree_links(cids, fetch_amazon)),
        )
        for label, field, finder in jobs:
            shop_missing = missing.setdefault(field, {})
            candidates = open_candidates(filaments, field, shop_missing, now)
            if not candidates:
                continue
            cids = sorted({cid for _, cid in candidates})
            try:
                found = finder(cids)
            except PageBlocked as e:
                summary["blocked"].append(label)
                log.info(f"Shop-Links: {label} gerade nicht erreichbar ({e}) - naechster Lauf versucht es erneut")
                continue
            except Exception as e:
                summary["blocked"].append(label)
                log.warning(f"Shop-Links: {label} fehlgeschlagen: {e}")
                continue
            for fil, cid in candidates:
                hit = found.get(cid)
                if not hit:
                    shop_missing[cid] = now
                    summary["not_offered"] += 1
                    log.info(f"Shop-Links: Filament #{fil.get('id')} ({cid}) {label} -> kein Angebot, "
                             f"erneute Suche in {MISSING_RETRY_S // 86400} Tagen")
                    continue
                shop_missing.pop(cid, None)
                if write_link(fil.get("id"), field, hit["url"]):
                    summary["filled"] += 1
                    filled.add((fil.get("id"), field))
                    price = f", {hit['price']:.2f} {hit.get('currency') or ''}".rstrip() if hit.get("price") else ""
                    log.info(f"Shop-Links: Filament #{fil.get('id')} ({cid}) {label} -> {hit['url']}"
                             f" ({hit.get('name') or hit.get('title') or ''}{price})")
                    events.append({"ts": now, "filament_id": fil.get("id"), "name": fil.get("name"),
                                   "colorid": cid, "shop": label, "url": hit["url"]})
        summary["open"] = count_open(filaments, missing, filled)
        state["events"] = events[-MAX_EVENTS:]
        state["last_run"] = summary
        with _state_lock:
            _save_state(state)
        return summary
    finally:
        _run_lock.release()


def is_running():
    return _run_lock.locked()


# ─── Uebersicht ──────────────────────────────────────────────────────────────

def render_overview(filaments, state, running=False, notice=""):
    esc = pc._esc
    rows = []
    missing = state.get("missing", {})
    for fil in sorted(filaments or [], key=lambda f: f.get("id") or 0):
        cid = _colorid(fil)
        if not cid:
            continue
        cells = []
        for field in (FIELD_BAMBU, FIELD_CCTREE):
            url = pc._extra_value(fil, field)
            if url:
                cells.append(f'<td><a href="{esc(url)}" target="_blank" rel="noopener noreferrer">vorhanden</a></td>')
            elif cid in missing.get(field, {}):
                seen = time.strftime("%d.%m.%Y", time.localtime(float(missing[field][cid])))
                cells.append(f'<td class="muted">kein Angebot (geprüft {seen})</td>')
            else:
                cells.append('<td class="open">offen</td>')
        rows.append(f'<tr><td>#{esc(fil.get("id"))}</td><td>{esc(fil.get("name"))}</td><td>{esc(cid)}</td>{"".join(cells)}</tr>')
    last = state.get("last_run") or {}
    last_txt = ("noch kein Lauf" if not last else
                f'{time.strftime("%d.%m.%Y %H:%M", time.localtime(last["started"]))}: '
                f'{last.get("filled", 0)} ergänzt, {last.get("not_offered", 0)} ohne Angebot'
                + (f', nicht erreichbar: {", ".join(last["blocked"])}' if last.get("blocked") else ""))
    events = "".join(
        f'<li>{time.strftime("%d.%m. %H:%M", time.localtime(e["ts"]))} – #{esc(e["filament_id"])} {esc(e.get("name"))} '
        f'({esc(e["colorid"])}): {esc(e["shop"])} → <a href="{esc(e["url"])}" target="_blank" rel="noopener noreferrer">Link</a></li>'
        for e in reversed(state.get("events", [])[-15:]))
    status = "läuft gerade …" if running else f"automatisch alle {int(RUN_INTERVAL_S // 3600)} h"
    notice_html = f'<p class="notice" role="status">{esc(notice)}</p>' if notice else ""
    refresh = '<meta http-equiv="refresh" content="10">' if running else ""
    return f'''<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
{refresh}<title>Shop-Links Bambu Lab & CCTREE</title>
<style>
  body {{ margin:0; padding:20px; background:#1a1a1a; color:#f1f1f1; font:14px/1.5 -apple-system,"Segoe UI",Roboto,sans-serif; }}
  main {{ max-width:960px; margin:0 auto; }} h1 {{ font-size:1.3rem; }} h2 {{ font-size:1.05rem; margin-top:22px; }}
  a {{ color:#0ea5e9; }} table {{ width:100%; border-collapse:collapse; }}
  th, td {{ padding:6px 8px; border-bottom:1px solid #333; text-align:left; }}
  th {{ color:#a8a8a8; font-size:.75rem; text-transform:uppercase; }}
  .muted {{ color:#8a8a8a; }} .open {{ color:#f59e0b; }}
  button {{ padding:6px 12px; border:1px solid #0ea5e9; border-radius:6px; background:transparent; color:#f1f1f1; font:inherit; cursor:pointer; }}
  button:focus-visible, a:focus-visible {{ outline:2px solid #0ea5e9; outline-offset:2px; }}
  .notice {{ padding:8px 10px; border-left:3px solid #0ea5e9; background:#1e2a33; }}
</style></head><body><main>
  <h1>Shop-Links Bambu Lab & CCTREE</h1>
  <p>Für Bambu-Filamente mit ColorID werden die Links „Bambu Lab“ und „CCTREE“ automatisch gesucht und in Spoolman
     eingetragen – von Hand eingetragene Links bleiben unverändert. Status: {status}. Letzter Lauf: {esc(last_txt)}.</p>
  {notice_html}
  <form method="post" action="/nachkaufen/shop-links"><button type="submit">Jetzt ergänzen</button></form>
  <h2>Filamente mit ColorID</h2>
  <table><thead><tr><th scope="col">ID</th><th scope="col">Filament</th><th scope="col">ColorID</th>
    <th scope="col">Bambu Lab</th><th scope="col">CCTREE</th></tr></thead><tbody>{"".join(rows)}</tbody></table>
  <h2>Zuletzt ergänzt</h2><ul>{events or "<li>–</li>"}</ul>
  <p><a href="/nachkaufen">← zur Nachkauf-Liste</a></p>
</main></body></html>'''
