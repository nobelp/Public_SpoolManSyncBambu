#!/usr/bin/env python3
"""
DIV_SpoolManSyncBambu - price_check.py
Nachkauf-Liste mit Preisvergleich (GitHub Issue #31).

Listet alle Filamente, deren GESAMTbestand (Summe Rest / Summe Startgewicht
aller nicht archivierten Spulen) unter einer Schwelle liegt, und vergleicht
die aktuellen Preise der Shops - jeder Shop eine Spalte:

  Primär bestellen  Spoolman-Zusatzfeld  orderurl      (beliebiger oeffentlicher Shop)
  Bambu Lab         Spoolman-Zusatzfeld  url_bambulab  (Bambu-Store, Variante per ColorID)
  CCTREE            Spoolman-Zusatzfeld  url_cctree    (CCTREE-Store auf Amazon, Variante per ColorID)
  Galaxus       Spoolman-Zusatzfeld  url_galaxus
  Amazon        Spoolman-Zusatzfeld  url_amazon
  3DJake        Spoolman-Zusatzfeld  url_3djake

Die Shop-Links werden pro Filament in Spoolman gepflegt - eine automatische
Suche trifft zu oft die falsche Farbe/Gebindegroesse.

Preise werden deterministisch aus strukturierten Seitendaten gelesen
(schema.org/JSON-LD -> Open Graph -> Amazon-Preisfeld), nie geraten. Alle
Preise werden nach CHF umgerechnet (EZB-Tageskurs, fester Fallback-Kurs).

Abrufe laufen im Hintergrund und werden gecacht - die Seite selbst wartet
nie auf einen Shop. Die Aufrufer (consumption.py) liefern nur die Spulen aus
Spoolman; dieses Modul kennt weder HA noch den Webserver.
"""

import hmac
import html
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("consumption.price_check")

# ─── Konfiguration ───────────────────────────────────────────────────────────

DEFAULT_THRESHOLD_PCT = float(os.environ.get("RESTOCK_THRESHOLD_PCT", "20"))
PRICE_CACHE_FILE = os.environ.get("PRICE_CACHE_FILE", "/app/data/price_cache.json")
PRICE_CACHE_TTL_S = float(os.environ.get("PRICE_CACHE_TTL_H", "12")) * 3600
# Blockierte/fehlgeschlagene Abrufe frueher erneut versuchen
PRICE_RETRY_TTL_S = 3600
# Pause zwischen zwei Abrufen beim selben Shop (hoeflich, weniger Bot-Schutz)
PER_HOST_DELAY_S = 3
# Per Lesezeichen uebernommene Preise (Galaxus blockiert automatische Abrufe)
MANUAL_PRICE_MAX_AGE_S = 14 * 24 * 3600
MANUAL_PRICE_MIN_CHF = 1.0
MANUAL_PRICE_MAX_CHF = 500.0
BOOKMARKLET_TOKEN = os.environ.get("RESTOCK_BOOKMARKLET_TOKEN", "").strip()
# Fallback, falls der EZB-Kurs nicht abrufbar ist
EUR_CHF_FALLBACK = float(os.environ.get("EUR_CHF_FALLBACK", "0.94"))
FX_URL = "https://api.frankfurter.app/latest?from=EUR&to=CHF"
FX_TTL_S = 24 * 3600
MANUAL_REFRESH_MIN_INTERVAL_S = 10 * 60
FETCH_TIMEOUT_S = 15
FETCH_MAX_BYTES = 6 * 1024 * 1024
FETCH_MAX_REDIRECTS = 3
FETCH_WORKERS = 4

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

AMAZON_DOMAINS = ("amazon.de", "amazon.com", "amazon.fr", "amazon.it", "amazon.es",
                  "amazon.nl", "amazon.co.uk")

# domains=None: jede oeffentliche https-Adresse (die Bestellseite kann jeder Shop sein)
SHOPS = [
    {"key": "orderurl",     "label": "Primär bestellen", "domains": None},
    {"key": "url_bambulab", "label": "Bambu Lab",        "domains": ("bambulab.com",)},
    {"key": "url_galaxus",  "label": "Galaxus",          "domains": ("galaxus.ch",)},
    {"key": "url_amazon",   "label": "Amazon",           "domains": AMAZON_DOMAINS},
    # CCTREE verkauft Bambu-Original-Refills auf Amazon; eigene Spalte, gleiche Amazon-Domains
    {"key": "url_cctree",   "label": "CCTREE",           "domains": AMAZON_DOMAINS},
    {"key": "url_3djake",   "label": "3DJake",
     "domains": ("3djake.ch", "3djake.de", "3djake.at", "3djake.com")},
]

AVAILABILITY_LABELS = {
    "InStock": "lieferbar",
    "OutOfStock": "ausverkauft",
    "SoldOut": "ausverkauft",
    "PreOrder": "Vorbestellung",
    "BackOrder": "Nachlieferung",
    "LimitedAvailability": "wenige verfügbar",
    "Discontinued": "nicht mehr erhältlich",
}

_http = requests.Session()
_http.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "de-CH,de;q=0.9",
})

_cache_lock = threading.Lock()
_refresh_lock = threading.Lock()
_state = {"refreshing": False, "last_manual_refresh": 0.0}


# ─── Auswahl: Filamente mit niedrigem Gesamtbestand ──────────────────────────

def parse_threshold(value):
    """Schwelle aus Formular/Query lesen; ungueltig -> Standard, sonst 1..100."""
    try:
        pct = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD_PCT
    if pct != pct:  # NaN
        return DEFAULT_THRESHOLD_PCT
    return max(1.0, min(100.0, pct))


def _spool_weights(spool):
    """Gleiche Rechnung wie die Spoolman-Card (spoolWeights) - Anzeige konsistent."""
    fil = spool.get("filament") or {}
    initial = spool.get("initial_weight") or fil.get("weight") or 1000
    remaining = spool.get("remaining_weight")
    if remaining is None:
        remaining = initial
    return float(initial), max(0.0, float(remaining))


def select_low_stock(spools, threshold_pct):
    """Gruppiert nicht archivierte Spulen nach Filament und liefert alle
    Filamente mit Gesamtbestand < threshold_pct, leerstes zuerst."""
    groups = {}
    for spool in spools or []:
        fil = spool.get("filament") or {}
        if spool.get("archived") or not fil.get("id"):
            continue
        g = groups.setdefault(fil["id"], {"filament": fil, "spools": [],
                                          "initial_g": 0.0, "remaining_g": 0.0})
        initial, remaining = _spool_weights(spool)
        g["spools"].append(spool)
        g["initial_g"] += initial
        g["remaining_g"] += remaining

    result = []
    for g in groups.values():
        g["pct"] = (g["remaining_g"] / g["initial_g"] * 100) if g["initial_g"] > 0 else 100.0
        if g["pct"] < threshold_pct:
            result.append(g)
    return sorted(result, key=lambda g: (g["pct"], (g["filament"].get("name") or "").lower()))


def _extra_value(filament, key):
    """Spoolman liefert Zusatzfelder JSON-kodiert (z.B. '"https://..."')."""
    raw = (filament.get("extra") or {}).get(key)
    if raw in (None, ""):
        return ""
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        val = raw
    return val.strip() if isinstance(val, str) else ("" if val is None else str(val))


_AMAZON_ASIN_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)")


_GALAXUS_PID_RE = re.compile(r"-(\d{5,})/?$")


def fetch_key(url):
    """Schluessel fuer Abruf und Cache - je PRODUKT, nicht je URL-Text (Issue #38):
    Amazon -> https://<host>/dp/<ASIN>, Galaxus -> .../product/<Produktnummer>,
    sonst die URL ohne Sprungmarke. So gehoert ein Preis immer zu dem Produkt,
    das die aktuelle URL in Spoolman beschreibt; verschiedene Schreibweisen
    derselben Produkt-URL teilen sich einen Preis, eine geaenderte URL wird neu
    abgefragt."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = (parts.hostname or "").lower()
    if "amazon." in host:
        m = _AMAZON_ASIN_RE.search(parts.path + ("?" if parts.query else ""))
        if m:
            return f"https://{host}/dp/{m.group(1)}"
    if host == "galaxus.ch" or host.endswith(".galaxus.ch"):
        m = _GALAXUS_PID_RE.search(parts.path)
        if m:
            return f"https://www.galaxus.ch/de/s1/product/{m.group(1)}"
    return url.split("#", 1)[0]


# ─── URL-Pruefung (SSRF-Schutz) ──────────────────────────────────────────────

def _host_allowed(host, domains):
    host = (host or "").lower().rstrip(".")
    if domains is None:
        return bool(host)
    return any(host == d or host.endswith("." + d) for d in domains)


def _resolves_public(host):
    """Nur Hosts, die ausschliesslich auf oeffentliche Adressen zeigen -
    verhindert, dass ein Link in Spoolman interne Geraete abfragt."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        return False
    if not infos:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            return False
    return True


def validate_shop_url(url, domains):
    """Liefert (ok, grund). Nur https, erlaubte Domain, oeffentlicher Host."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False, "ungültige URL"
    if parts.scheme != "https":
        return False, "nur https-Links"
    if parts.username or parts.password:
        return False, "ungültige URL"
    host = parts.hostname or ""
    if not _host_allowed(host, domains):
        return False, "falscher Shop für diese Spalte"
    if not _resolves_public(host):
        return False, "Adresse nicht erlaubt"
    return True, ""


# ─── Preis auslesen ──────────────────────────────────────────────────────────

_PRICE_NUM_RE = re.compile(r"\d[\d'’.,\s]*")


def parse_price_value(text):
    """'1.234,56' / '12,82' / '20.00' / '1'299.–' -> float, sonst None."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = _PRICE_NUM_RE.search(str(text))
    if not m:
        return None
    num = re.sub(r"[\s'’]", "", m.group(0)).rstrip(".,")
    if "," in num and "." in num:
        # das zuletzt stehende Zeichen ist das Dezimaltrennzeichen
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif "," in num:
        head, _, tail = num.rpartition(",")
        num = f"{head.replace(',', '')}.{tail}" if len(tail) in (1, 2) else num.replace(",", "")
    elif num.count(".") > 1:
        head, _, tail = num.rpartition(".")
        num = f"{head.replace('.', '')}.{tail}" if len(tail) in (1, 2) else num.replace(".", "")
    try:
        value = float(num)
    except ValueError:
        return None
    return value if value > 0 else None


def _currency_from_text(text):
    t = (text or "").upper()
    if "CHF" in t or "FR." in t:
        return "CHF"
    if "€" in t or "EUR" in t:
        return "EUR"
    return None


def _jsonld_candidates(soup):
    """Alle Angebote aus JSON-LD: Product, ProductGroup(hasVariant), @graph."""
    found = []

    def visit(node):
        if isinstance(node, list):
            for n in node:
                visit(n)
            return
        if not isinstance(node, dict):
            return
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if "Product" in types or "ProductGroup" in types:
            offers = node.get("offers")
            for offer in (offers if isinstance(offers, list) else [offers]):
                if not isinstance(offer, dict):
                    continue
                price = offer.get("price", offer.get("lowPrice"))
                if price is None and isinstance(offer.get("priceSpecification"), dict):
                    price = offer["priceSpecification"].get("price")
                value = parse_price_value(price)
                if value is None:
                    continue
                found.append({
                    "price": value,
                    "currency": (offer.get("priceCurrency") or "").upper() or None,
                    "availability": str(offer.get("availability") or "").rsplit("/", 1)[-1] or None,
                    "offer_url": offer.get("url") or "",
                    "sku": str(node.get("sku") or ""),
                    "name": node.get("name") or "",
                })
            visit(node.get("hasVariant"))
        visit(node.get("@graph"))

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            visit(json.loads(tag.string or tag.get_text() or ""))
        except (TypeError, ValueError):
            continue
    return found


def _pick_variant(candidates, url):
    """Shops wie der Bambu-Store listen alle Varianten; die im Link gewaehlte
    (?id=/?variant=/?sai=) gewinnt, sonst das erste Angebot."""
    query = parse_qs(urlsplit(url).query)
    wanted = {v for k in ("id", "variant", "sai") for v in query.get(k, [])}
    for cand in candidates:
        cand_query = parse_qs(urlsplit(cand["offer_url"]).query)
        cand_ids = {v for k in ("id", "variant", "sai") for v in cand_query.get(k, [])}
        if wanted and (wanted & cand_ids or cand["sku"] in wanted):
            return cand
    return candidates[0]


def _opengraph_price(soup):
    def meta(*names):
        for n in names:
            tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if tag and tag.get("content"):
                return tag["content"]
        return None
    value = parse_price_value(meta("product:price:amount", "og:price:amount"))
    if value is None:
        return None
    currency = (meta("product:price:currency", "og:price:currency") or "").upper() or None
    return {"price": value, "currency": currency, "availability": None, "name": ""}


def _amazon_price(soup):
    for selector in ("#corePrice_feature_div .a-offscreen", ".priceToPay .a-offscreen",
                     "#corePriceDisplay_desktop_feature_div .a-offscreen",
                     "#price_inside_buybox", "#priceblock_ourprice"):
        tag = soup.select_one(selector)
        text = tag.get_text(" ", strip=True) if tag else ""
        value = parse_price_value(text)
        if value is not None:
            avail_tag = soup.select_one("#availability")
            avail_text = avail_tag.get_text(" ", strip=True).lower() if avail_tag else ""
            availability = None
            if "auf lager" in avail_text or "in stock" in avail_text:
                availability = "InStock"
            elif "nicht verfügbar" in avail_text or "unavailable" in avail_text:
                availability = "OutOfStock"
            return {"price": value, "currency": _currency_from_text(text),
                    "availability": availability, "name": ""}
    return None


def extract_price(page_html, url):
    """Preis aus einer Produktseite: JSON-LD -> Open Graph -> Amazon-Preisfeld.
    Liefert dict(price, currency, availability, name) oder None."""
    soup = BeautifulSoup(page_html, "html.parser")
    candidates = _jsonld_candidates(soup)
    if candidates:
        best = _pick_variant(candidates, url)
        return {k: best[k] for k in ("price", "currency", "availability", "name")}
    result = _opengraph_price(soup)
    if result:
        return result
    host = (urlsplit(url).hostname or "").lower()
    if "amazon." in host:
        return _amazon_price(soup)
    return None


# ─── Abruf ───────────────────────────────────────────────────────────────────

def _get_with_checked_redirects(url, domains):
    """GET ohne automatische Redirects: jedes Ziel wird erneut geprueft,
    sonst koennte ein Redirect die SSRF-Pruefung umgehen."""
    current = url
    for _ in range(FETCH_MAX_REDIRECTS + 1):
        ok, reason = validate_shop_url(current, domains)
        if not ok:
            raise ValueError(reason)
        resp = _http.get(current, timeout=FETCH_TIMEOUT_S, allow_redirects=False, stream=True)
        if resp.is_redirect and resp.headers.get("Location"):
            current = urljoin(current, resp.headers["Location"])
            resp.close()
            continue
        body = b""
        for chunk in resp.iter_content(64 * 1024):
            body += chunk
            if len(body) > FETCH_MAX_BYTES:
                break
        resp.close()
        # requests nimmt ohne charset-Angabe ISO-8859-1 an -> Umlaute kaputt
        declared = "charset=" in (resp.headers.get("Content-Type") or "").lower()
        encoding = resp.encoding if declared and resp.encoding else "utf-8"
        return resp.status_code, body.decode(encoding, errors="replace")
    raise ValueError("zu viele Weiterleitungen")


def fetch_price(url, domains):
    """Einen Shop-Link abrufen. Liefert immer ein dict mit 'status':
    ok | no_price | blocked | error (+ 'fetched_at')."""
    result = {"url": url, "fetched_at": time.time()}
    try:
        status, page = _get_with_checked_redirects(url, domains)
    except ValueError as e:
        return {**result, "status": "error", "error": str(e)}
    except requests.RequestException as e:
        log.info(f"Preisabruf fehlgeschlagen ({urlsplit(url).hostname}): {type(e).__name__}")
        return {**result, "status": "error", "error": "Shop nicht erreichbar"}

    if status in (401, 403, 429, 503):
        return {**result, "status": "blocked", "error": f"Bot-Schutz (HTTP {status})"}
    if status != 200:
        return {**result, "status": "error", "error": f"HTTP {status}"}
    # Schutzseiten sind klein; echte Produktseiten (Amazon ~3 MB) enthalten das
    # Wort "captcha" teils auch in normalen Skripten.
    lowered = page.lower() if len(page) < 50000 else ""
    if "captcha" in lowered or "um mit dem einkauf fortzufahren" in lowered:
        return {**result, "status": "blocked", "error": "Bot-Schutz"}
    try:
        price = extract_price(page, url)
    except Exception as e:  # defensiv: kaputtes HTML darf den Abruf nicht abbrechen
        log.warning(f"Preis konnte nicht gelesen werden ({urlsplit(url).hostname}): {e}")
        price = None
    if not price:
        reason = "kein Preis gefunden"
        if "amazon." in (urlsplit(url).hostname or "") and "buybox-see-all-buying-choices" in page:
            # kein eigenes Angebot, nur "Alle Angebote" - Preise der Karussells
            # ("Aehnliche Artikel") gehoeren zu anderen Produkten
            reason = "kein Direktangebot (nur „Alle Angebote“)"
        return {**result, "status": "no_price", "error": reason}
    return {**result, "status": "ok", **price}


# ─── Cache ───────────────────────────────────────────────────────────────────

def _load_cache():
    try:
        with open(PRICE_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(data):
    os.makedirs(os.path.dirname(PRICE_CACHE_FILE) or ".", exist_ok=True)
    tmp = PRICE_CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PRICE_CACHE_FILE)


def _normalize_prices(prices):
    """Bildet Eintraege auf den aktuellen Cache-Schluessel ab (fetch_key). Aeltere
    Eintraege unter URL-Text-Schluesseln bleiben so erhalten; bei mehreren
    Eintraegen je Produkt gewinnt der neueste."""
    out = {}
    for key, entry in (prices or {}).items():
        if not isinstance(entry, dict):
            continue
        new_key = fetch_key(key)
        old = out.get(new_key)
        if old is None or entry.get("fetched_at", 0) >= old.get("fetched_at", 0):
            out[new_key] = {**entry, "url": new_key}
    return out


def get_cached_prices():
    with _cache_lock:
        return _normalize_prices(_load_cache().get("prices", {}))


def _merge_result(old, new):
    """Ein blockierter/fehlgeschlagener Abruf ueberschreibt keinen guten
    Preis: der letzte Preis bleibt (mit Hinweis), nur checked_at wird neu."""
    new = {**new, "checked_at": new["fetched_at"]}
    if new["status"] == "ok" or not old or old.get("status") != "ok":
        return new
    return {**old, "checked_at": new["checked_at"], "last_error": new.get("error")}


def _store_prices(results):
    with _cache_lock:
        data = _load_cache()
        prices = _normalize_prices(data.get("prices", {}))
        for r in results:
            key = fetch_key(r["url"])
            prices[key] = _merge_result(prices.get(key), {**r, "url": key})
        data["prices"] = prices
        _save_cache(data)


def get_eur_chf_rate():
    """EZB-Tageskurs (frankfurter.app, kostenlos, ohne Schluessel), 24 h
    gecacht; bei Fehler der letzte bekannte Kurs bzw. der feste Fallback."""
    with _cache_lock:
        data = _load_cache()
    fx = data.get("fx") or {}
    if fx.get("rate") and time.time() - fx.get("fetched_at", 0) < FX_TTL_S:
        return fx
    try:
        resp = _http.get(FX_URL, timeout=10, headers={"Accept": "application/json"})
        resp.raise_for_status()
        payload = resp.json()
        rate = float(payload["rates"]["CHF"])
        fx = {"rate": rate, "date": payload.get("date"), "source": "EZB",
              "fetched_at": time.time()}
        with _cache_lock:
            data = _load_cache()
            data["fx"] = fx
            _save_cache(data)
        return fx
    except (requests.RequestException, KeyError, TypeError, ValueError) as e:
        log.info(f"EUR/CHF-Kurs nicht abrufbar ({type(e).__name__}) - nutze Fallback")
        if fx.get("rate"):
            return {**fx, "source": "EZB (veraltet)"}
        return {"rate": EUR_CHF_FALLBACK, "date": None, "source": "fester Kurs", "fetched_at": 0}


def to_chf(price, currency, fx_rate):
    if price is None:
        return None
    if currency in (None, "CHF"):
        return round(price, 2)
    if currency == "EUR":
        return round(price * fx_rate, 2)
    return None  # andere Waehrungen werden nicht verglichen


# ─── Hintergrund-Aktualisierung ──────────────────────────────────────────────

def normalize_url(url):
    """Beim Kopieren geht oft 'https://' verloren ('amazon.de/dp/...') -
    ohne Schema wird https ergaenzt; andere Schemata bleiben (und werden
    spaeter von validate_shop_url abgelehnt)."""
    url = (url or "").strip()
    if url and "://" not in url and re.match(r"^[a-z0-9.-]+\.[a-z]{2,}(/|$)", url, re.I):
        return "https://" + url
    return url


def shop_links(group):
    """[(shop, url)] fuer ein Filament - nur Spalten mit hinterlegtem Link."""
    links = []
    for shop in SHOPS:
        url = normalize_url(_extra_value(group["filament"], shop["key"]))
        if url:
            links.append((shop, url))
    return links


def _stale(entry, now):
    if not entry:
        return True
    checked = entry.get("checked_at", entry.get("fetched_at", 0))
    fresh_ok = entry.get("status") == "ok" and not entry.get("last_error")
    return now - checked > (PRICE_CACHE_TTL_S if fresh_ok else PRICE_RETRY_TTL_S)


def refresh_prices(groups, force=False):
    """Fehlende/veraltete Preise im Hintergrund holen. Liefert True, wenn ein
    Abruf gestartet wurde. Laeuft nie doppelt."""
    now = time.time()
    cached = get_cached_prices()
    todo = []
    seen = set()
    for group in groups:
        for shop, url in shop_links(group):
            key = fetch_key(url)
            entry = cached.get(key)
            if entry and entry.get("source") == "manual" and not force:
                continue  # von dir uebernommen - nicht automatisch ueberschreiben
            if key not in seen and (force or _stale(entry, now)):
                seen.add(key)
                todo.append((key, shop["domains"]))
    if not todo:
        return False
    if not _refresh_lock.acquire(blocking=False):
        return False  # laeuft bereits

    by_host = {}
    for key, domains in todo:
        by_host.setdefault(urlsplit(key).hostname or "", []).append((key, domains))

    def fetch_host(items):
        # pro Shop nacheinander mit Pause, die Shops untereinander parallel
        results = []
        for i, (key, domains) in enumerate(items):
            if i:
                time.sleep(PER_HOST_DELAY_S)
            results.append(fetch_price(key, domains))
        return results

    def worker():
        try:
            _state["refreshing"] = True
            with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
                results = [r for batch in pool.map(fetch_host, by_host.values()) for r in batch]
            _store_prices(results)
            ok = sum(1 for r in results if r["status"] == "ok")
            log.info(f"Preisabruf: {ok}/{len(results)} Preise gelesen")
        except Exception as e:
            log.error(f"Preisabruf abgebrochen: {e}", exc_info=True)
        finally:
            _state["refreshing"] = False
            _refresh_lock.release()

    threading.Thread(target=worker, name="price-refresh", daemon=True).start()
    return True


def request_manual_refresh(groups):
    """'Preise aktualisieren': hoechstens alle 10 Minuten (schont die Shops).
    Liefert (gestartet, Sekunden bis zum naechsten erlaubten Abruf)."""
    wait = MANUAL_REFRESH_MIN_INTERVAL_S - (time.time() - _state["last_manual_refresh"])
    if wait > 0:
        return False, int(wait)
    started = refresh_prices(groups, force=True)
    if started:  # Sperre nur, wenn wirklich abgerufen wird
        _state["last_manual_refresh"] = time.time()
    return started, 0


def is_refreshing():
    return _state["refreshing"]


# ─── Aufbereitung ────────────────────────────────────────────────────────────

def build_rows(groups, prices, fx_rate):
    """Verknuepft Filamente mit den gecachten Preisen und markiert den
    guenstigsten (lieferbaren) Preis je Zeile."""
    rows = []
    for group in groups:
        cells = {}
        for shop in SHOPS:
            url = normalize_url(_extra_value(group["filament"], shop["key"]))
            if not url:
                cells[shop["key"]] = {"state": "missing"}
                continue
            entry = prices.get(fetch_key(url))
            if not entry:
                cells[shop["key"]] = {"state": "pending", "url": url}
                continue
            cell = {"state": entry["status"], "url": url, "error": entry.get("error"),
                    "fetched_at": entry.get("fetched_at"), "last_error": entry.get("last_error")}
            if entry["status"] == "ok":
                cell.update(price=entry["price"], currency=entry.get("currency"),
                            availability=entry.get("availability"),
                            price_chf=to_chf(entry["price"], entry.get("currency"), fx_rate))
                if entry.get("source") == "manual":
                    cell["manual"] = True
                    cell["stale"] = time.time() - entry.get("fetched_at", 0) > MANUAL_PRICE_MAX_AGE_S
            cells[shop["key"]] = cell

        # veraltete uebernommene Preise werden angezeigt, zaehlen aber nicht fuer den Vergleich
        priced = [c for c in cells.values() if c.get("price_chf") is not None and not c.get("stale")]
        in_stock = [c for c in priced if c.get("availability") not in
                    ("OutOfStock", "SoldOut", "Discontinued")]
        pool = in_stock or priced
        if len(pool) >= 1:
            min(pool, key=lambda c: c["price_chf"])["cheapest"] = len(priced) > 1
        rows.append({"group": group, "cells": cells})
    return rows


def rows_to_json(rows, threshold, fx):
    out = []
    for row in rows:
        fil = row["group"]["filament"]
        out.append({
            "filament_id": fil.get("id"),
            "name": fil.get("name"),
            "vendor": (fil.get("vendor") or {}).get("name"),
            "color_hex": fil.get("color_hex"),
            "colorid": _extra_value(fil, "colorid"),
            "stock_pct": round(row["group"]["pct"], 1),
            "remaining_g": round(row["group"]["remaining_g"]),
            "spools": [s.get("id") for s in row["group"]["spools"]],
            "shops": {k: {kk: vv for kk, vv in v.items() if kk != "fetched_at"}
                      for k, v in row["cells"].items()},
        })
    return {"threshold_pct": threshold, "eur_chf": fx, "refreshing": is_refreshing(),
            "filaments": out}


# ─── Preis per Lesezeichen uebernehmen (Galaxus) ─────────────────────────────
#
# Galaxus blockiert jeden automatischen Abruf (auch Headless-Browser). Das
# Lesezeichen laeuft deshalb im Browser des Nutzers auf der normal geoeffneten
# Galaxus-Seite, liest dort den Preis und oeffnet /nachkaufen/galaxus mit
# Preis + URL. Es wird nichts automatisiert und kein Schutz umgangen.

_GALAXUS_ID_RE = re.compile(r"-(\d{5,})/?$")


def galaxus_product_id(url):
    """Produktnummer aus https://www.galaxus.ch/.../product/...-37001434"""
    try:
        parts = urlsplit(normalize_url(url))
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not _host_allowed(host, ("galaxus.ch",)):
        return None
    m = _GALAXUS_ID_RE.search(parts.path)
    return m.group(1) if m else None


def get_bookmarklet_token():
    """Geheimer Schluessel fuer das Lesezeichen: aus RESTOCK_BOOKMARKLET_TOKEN
    oder einmalig zufaellig erzeugt und im Preis-Cache gespeichert."""
    if BOOKMARKLET_TOKEN:
        return BOOKMARKLET_TOKEN
    with _cache_lock:
        data = _load_cache()
        token = data.get("bookmarklet_token")
        if not token:
            token = secrets.token_urlsafe(24)
            data["bookmarklet_token"] = token
            _save_cache(data)
        return token


def submit_manual_price(filaments, url, price_text, currency, token):
    """Vom Lesezeichen gemeldeten Galaxus-Preis pruefen und speichern.
    Liefert (code, filament) - code ist 'ok' oder ein Fehlergrund."""
    if not token or not hmac.compare_digest(str(token), get_bookmarklet_token()):
        return "schluessel", None
    product_id = galaxus_product_id(url)
    if not product_id:
        return "keine-galaxus-seite", None
    match = None
    for fil in filaments or []:
        if galaxus_product_id(_extra_value(fil, "url_galaxus")) == product_id:
            match = fil
            break
    if not match:
        return "unbekannt", None
    if (currency or "CHF").strip().upper() != "CHF":
        return "waehrung", match
    price = parse_price_value(price_text)
    if price is None or not (MANUAL_PRICE_MIN_CHF <= price <= MANUAL_PRICE_MAX_CHF):
        return "preis", match
    key = fetch_key(normalize_url(_extra_value(match, "url_galaxus")))
    now = time.time()
    with _cache_lock:
        data = _load_cache()
        data["prices"] = _normalize_prices(data.get("prices", {}))
        data["prices"][key] = {
            "url": key, "status": "ok", "price": round(price, 2), "currency": "CHF",
            "availability": None, "name": "", "source": "manual",
            "fetched_at": now, "checked_at": now,
        }
        _save_cache(data)
    log.info(f"Galaxus-Preis uebernommen (Lesezeichen/Runde): Filament #{match.get('id')} = CHF {price:.2f}")
    return "ok", match


def bookmarklet_js(endpoint, token):
    """JavaScript fuer das Lesezeichen. Liest den Preis aus den strukturierten
    Produktdaten (JSON-LD, sonst Meta-Tags) und fragt sonst nach dem Preis.
    Uebergabe per neuem Tab (window.open) statt Formular: funktioniert auch,
    wenn die Shop-Seite Formular-Ziele per CSP einschraenkt."""
    js = (
        "(()=>{"
        "if(!/(^|\\.)galaxus\\.ch$/.test(location.hostname)){alert('Bitte auf einer Galaxus-Produktseite ausf\\u00fchren.');return}"
        "let p=null,c='CHF';"
        "const w=n=>{if(!n||typeof n!=='object'||p)return;if(Array.isArray(n)){n.forEach(w);return}"
        "if(n['@graph'])w(n['@graph']);const t=[].concat(n['@type']);"
        "if(t.includes('Product')||t.includes('ProductGroup')){const o=[].concat(n.offers||[])[0];"
        "if(o&&(o.price||o.lowPrice)){p=o.price||o.lowPrice;c=o.priceCurrency||c}}"
        "if(n.hasVariant)w(n.hasVariant)};"
        "document.querySelectorAll('script[type=\"application/ld+json\"]').forEach(s=>{try{w(JSON.parse(s.textContent))}catch(e){}});"
        "if(!p){const m=document.querySelector('meta[property=\"product:price:amount\"],meta[itemprop=\"price\"]');if(m)p=m.content}"
        "if(!p){p=prompt('Preis nicht automatisch gefunden. Bitte den Galaxus-Preis in CHF eingeben:');if(!p)return}"
        "const q=new URLSearchParams({token:TOKEN,url:location.origin+location.pathname+location.search,price:String(p),currency:c});"
        "window.open(ENDPOINT+'?'+q.toString(),'_blank')"
        "})()"
    )
    js = js.replace("TOKEN", json.dumps(token)).replace("ENDPOINT", json.dumps(endpoint))
    # '#' wuerde in einer javascript:-Adresse als Sprungmarke gelesen, '%' dekodiert
    assert "#" not in js and "%" not in js, "Lesezeichen-Code darf weder '#' noch '%' enthalten"
    return "javascript:" + js


# ─── Galaxus-Runde (Tampermonkey-Skript im Browser des Nutzers) ──────────────
#
# Eine Webseite darf nicht in fremde Tabs hineinlesen. Die Runde nutzt deshalb
# ein Userscript (Tampermonkey) auf galaxus.ch: Der Nutzer startet die Runde
# selbst, der Tab ruft ALLE in Spoolman hinterlegten Galaxus-Links nacheinander auf
# (ruhiges Tempo), das Skript meldet je Seite den Preis und bekommt vom Dienst
# den naechsten Link. Kein Zeitplan, kein Hintergrundbetrieb.

ROUND_TTL_S = 3600               # so lange kann eine Runde laufen
ROUND_LOG_TTL_S = 24 * 3600      # so lange bleibt ihr Protokoll einsehbar
ROUND_STEP_DELAY_S = 5
ROUND_SEARCH_DEADLINE_S = 12     # spaetestens dann meldet das Skript "kein Preis"
USERSCRIPT_VERSION = "1.4"
# Zustaende einer Galaxus-Seite im Rundenprotokoll (Issue #34)
ROUND_STATES = {
    "ausstehend": "ausstehend",
    "gestartet": "Seite geladen, suche Preis",
    "preis-gefunden": "Preis gefunden, wird gemeldet",
    "ok": "Preis übernommen",
    "kein-preis": "kein Preis gefunden",
    "fehler": "Fehler im Skript",
    "bereits": "bereits verarbeitet",
}
_FINAL_STATES = {"ok", "kein-preis", "bereits", "preis", "waehrung", "unbekannt"}
_rounds = {}
_rounds_lock = threading.Lock()


def start_round(filaments):
    """Legt eine Runde mit den Galaxus-Links aller Filamente an (unabhaengig
    vom Bestand; gleiche Links nur einmal). Liefert (runden_id, [urls]); leere
    Liste, wenn kein Filament einen Galaxus-Link hat."""
    urls, seen = [], set()
    for fil in filaments or []:
        url = normalize_url(_extra_value(fil, "url_galaxus"))
        pid = galaxus_product_id(url)
        if pid and pid not in seen:  # je Produkt einmal - auch bei abweichender URL-Schreibweise
            seen.add(pid)
            urls.append(url)
    round_id = secrets.token_urlsafe(9)
    now = time.time()
    with _rounds_lock:
        for rid in [r for r, v in _rounds.items() if now - v["created"] > ROUND_LOG_TTL_S]:
            del _rounds[rid]
        _rounds[round_id] = {
            "urls": urls, "created": now, "done": set(), "saved": 0,
            # Protokoll je Galaxus-Seite (Produktnummer) - nachvollziehbar, wo eine Runde haengt
            "pages": {galaxus_product_id(u): {"url": u, "state": "ausstehend", "ts": None, "detail": ""}
                      for u in urls},
            "active": False,
        }
    return round_id, urls


def _round_event(rnd, round_id, pid, state, detail=""):
    """Protokolliert den Zustand einer Seite (unter _rounds_lock aufrufen).
    Ein Endzustand wird nicht durch eine spaetere Zwischenmeldung ueberschrieben."""
    page = rnd["pages"].get(pid)
    if page is None:
        return
    if page["state"] in _FINAL_STATES and state not in _FINAL_STATES:
        return
    # Ohne neues Detail bleibt die letzte Diagnose stehen (z.B. "Preissuche:
    # kaputte Seite" bleibt beim anschliessenden "kein Preis" sichtbar)
    page.update(state=state, ts=time.time(), detail=str(detail)[:200] if detail else page["detail"])
    rnd["active"] = True
    ids = list(rnd["pages"])
    log.info(f"Galaxus-Runde {round_id}: {ids.index(pid) + 1}/{len(ids)} Produkt {pid} "
             f"-> {ROUND_STATES.get(state, state)}{' – ' + page['detail'] if page['detail'] else ''}")


def round_status(round_id, url, phase, detail, token):
    """Diagnose-Meldung des Skripts (Seite gestartet, Preis gefunden, kein
    Preis, Fehler). Speichert nichts ausser dem Protokoll. Liefert dict ok/code."""
    if not token or not hmac.compare_digest(str(token), get_bookmarklet_token()):
        return {"ok": False, "code": "schluessel"}
    if phase not in ("gestartet", "preis-gefunden", "kein-preis", "fehler"):
        return {"ok": False, "code": "phase"}
    pid = galaxus_product_id(url)
    with _rounds_lock:
        rnd = _rounds.get(round_id or "")
        if not rnd or time.time() - rnd["created"] > ROUND_TTL_S:
            return {"ok": False, "code": "runde-unbekannt"}
        if pid not in rnd["pages"]:
            return {"ok": False, "code": "nicht-in-runde"}
        _round_event(rnd, round_id, pid, phase, detail)
    return {"ok": True, "code": "ok"}


def round_ping(token, version, pid, has_hash, expect, round_id, decision):
    """Fruehe Diagnose des Skripts auf einer Galaxus-Seite (Issue #37): nur Log."""
    if not token or not hmac.compare_digest(str(token), get_bookmarklet_token()):
        return {"ok": False, "code": "schluessel"}
    clean = lambda v, n=40: re.sub(r"[^\w .:/-]", "", str(v or ""))[:n]
    log.info(f"Galaxus-Skript {clean(version, 8)}: Seite {clean(pid, 12)} geladen – Hash: {clean(has_hash, 4)}, "
             f"erwartet: {clean(expect, 12)}, Runde: {clean(round_id, 20) or '-'} -> {clean(decision, 30)}")
    return {"ok": True}


def round_protocol(round_id):
    """Kopie des Rundenprotokolls fuer die Anzeige (oder None)."""
    with _rounds_lock:
        rnd = _rounds.get(round_id or "")
        if not rnd:
            return None
        return {"created": rnd["created"], "saved": rnd["saved"],
                "running": time.time() - rnd["created"] <= ROUND_TTL_S,
                "pages": [dict(p, pid=pid) for pid, p in rnd["pages"].items()]}


def recent_rounds(limit=5):
    """Die letzten Runden mit Aktivitaet (neueste zuerst) fuer die Rundenseite."""
    with _rounds_lock:
        items = [(rid, r) for rid, r in _rounds.items() if r.get("active")]
    items.sort(key=lambda x: x[1]["created"], reverse=True)
    out = []
    for rid, r in items[:limit]:
        states = [p["state"] for p in r["pages"].values()]
        out.append({"id": rid, "created": r["created"], "total": len(states), "saved": r["saved"],
                    "done": sum(1 for st in states if st in _FINAL_STATES)})
    return out


def round_step(round_id, url, price_text, currency, token, filaments):
    """Ein Schritt der Runde: Preis (falls gefunden) speichern, naechsten Link
    liefern. Antwort-dict: ok, code, next (URL oder None), position, total, saved.

    Idempotent: Jede Produktseite wird pro Runde nur einmal verarbeitet. Wird
    dieselbe Seite erneut gemeldet (Neuladen, doppelter Request, Antwort ging
    unterwegs verloren), wird nichts gespeichert, sondern mit code 'bereits'
    derselbe naechste Link geliefert. Die Seite wird unter Sperre reserviert,
    BEVOR gespeichert wird - so koennen auch zwei gleichzeitige Meldungen nicht
    beide speichern."""
    if not token or not hmac.compare_digest(str(token), get_bookmarklet_token()):
        return {"ok": False, "code": "schluessel", "next": None}
    pid = galaxus_product_id(url)
    with _rounds_lock:
        rnd = _rounds.get(round_id or "")
        if not rnd or time.time() - rnd["created"] > ROUND_TTL_S:
            return {"ok": False, "code": "runde-unbekannt", "next": None}
        ids = [galaxus_product_id(u) for u in rnd["urls"]]
        if pid not in ids:
            return {"ok": False, "code": "nicht-in-runde", "next": None}
        already = pid in rnd["done"]
        rnd["done"].add(pid)  # reservieren, bevor gespeichert wird

    if already:
        code = "bereits"
    elif str(price_text or "").strip():
        code, _fil = submit_manual_price(filaments, url, price_text, currency, token)
    else:
        code = "kein-preis"

    with _rounds_lock:
        if code == "ok":
            rnd["saved"] += 1
        if not already:
            entry = get_cached_prices().get(fetch_key(normalize_url(url))) if code == "ok" else None
            _round_event(rnd, round_id, pid, code,
                         f"CHF {entry['price']:.2f}" if entry and entry.get("price") else "")
        pos = ids.index(pid)
        nxt = next((u for u, i in zip(rnd["urls"][pos + 1:], ids[pos + 1:]) if i not in rnd["done"]), None)
        return {"ok": True, "code": code, "next": nxt, "position": pos + 1,
                "total": len(ids), "saved": rnd["saved"]}


# Vorlage statt f-String: keine doppelten Klammern, weniger fehleranfaellig.
# Platzhalter __BASE__, __HOST__, __TOKEN__, __VERSION__, __DELAY_MS__.
_USERSCRIPT_TEMPLATE = r"""// ==UserScript==
// @name         Spoolman Nachkaufen – Galaxus-Runde
// @namespace    __BASE__
// @version      __VERSION__
// @description  Liest in einer selbst gestarteten Runde den Galaxus-Preis und meldet ihn an /nachkaufen.
// @match        https://www.galaxus.ch/*
// @match        __BASE__/nachkaufen/galaxus-runde*
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_deleteValue
// @connect      __HOST__
// @run-at       document-start
// @updateURL    __BASE__/nachkaufen/galaxus.user.js
// @downloadURL  __BASE__/nachkaufen/galaxus.user.js
// ==/UserScript==
(function () {
  'use strict';

  const TOKEN = __TOKEN__;
  const BASE = __BASE_JS__;
  const VERSION = '__VERSION__';
  const DELAY = __DELAY_MS__;
  const REQUEST_TIMEOUT = 15000;
  const SEARCH_DEADLINE = __DEADLINE_MS__;
  // Rundenzustand im Tampermonkey-Speicher (gilt ueber Domains hinweg). Galaxus
  // entfernt den Hash per JavaScript, und document-start laeuft in aktuellen
  // Browsern nicht zuverlaessig davor (Issue #37) - daher kein Verlass auf den Hash.
  const KEY = 'spoolman-runde';
  const MAX_AGE_MS = 60 * 60 * 1000;
  const whenReady = fn => {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn, { once: true });
    else fn();
  };
  const pidOf = path => ((String(path || '').match(/-(\d{5,})\/?$/) || [])[1] || null);
  const loadState = () => { try { return JSON.parse(GM_getValue(KEY, 'null')); } catch (e) { return null; } };
  const saveState = st => { try { GM_setValue(KEY, JSON.stringify(st)); } catch (e) { /* egal */ } };
  const clearState = () => { try { GM_deleteValue(KEY); } catch (e) { /* egal */ } };

  // ── Auf der Rundenseite: Skript melden, Rundenstart merken ─────────────
  if (location.href.indexOf(BASE + '/nachkaufen/galaxus-runde') === 0) {
    whenReady(() => {
      const status = document.getElementById('userscript-status');
      if (status) {
        status.textContent = '✓ Tampermonkey-Skript aktiv (Version ' + VERSION + ')';
        status.className = 'script-ok';
      }
    });
    document.addEventListener('click', e => {
      const a = e.target && e.target.closest ? e.target.closest('a.start[data-round]') : null;
      if (a) saveState({ round: a.dataset.round, expect: a.dataset.first, ts: Date.now() });
    }, true);
    return;
  }

  // ── Auf Galaxus: nur in einer laufenden Runde und nur auf der erwarteten Seite ─
  const pid = pidOf(location.pathname);
  const hashMatch = location.hash.match(/spoolman-runde=([A-Za-z0-9_-]+)/);
  const stored = loadState();
  const storedValid = !!(stored && stored.round && Date.now() - Number(stored.ts || 0) < MAX_AGE_MS);
  let ROUND = null;
  if (hashMatch) ROUND = hashMatch[1];
  else if (storedValid && pid && stored.expect === pid) ROUND = stored.round;

  // Fruehe Diagnose, bevor entschieden wird (nur mit Rundenbezug, nie beim normalen Surfen)
  if (pid && (hashMatch || storedValid)) {
    try {
      GM_xmlhttpRequest({
        method: 'POST', url: BASE + '/nachkaufen/galaxus-runde/ping', timeout: 5000,
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        data: new URLSearchParams({ token: TOKEN, version: VERSION, pid: pid,
          hash: hashMatch ? 'ja' : 'nein', expect: storedValid ? String(stored.expect) : '-',
          round: ROUND || (storedValid ? stored.round : ''), decision: ROUND ? 'aktiv' : 'nicht erwartete Seite' }).toString(),
      });
    } catch (e) { /* Diagnose darf nie stoeren */ }
  }
  if (!ROUND) return; // normales Surfen oder nicht die erwartete Seite

  // ── Statusfenster: sofort anzeigen ──────────────────────────────────────
  const box = document.createElement('div');
  box.setAttribute('role', 'status');
  box.setAttribute('aria-live', 'polite');
  box.style.cssText = 'position:fixed;z-index:2147483647;right:16px;bottom:16px;max-width:380px;' +
    'padding:12px 14px;background:#1a1a1a;color:#fff;border:2px solid #0ea5e9;border-radius:10px;' +
    'font:14px/1.4 sans-serif;box-shadow:0 6px 18px rgba(0,0,0,.4)';
  const show = (text, isError) => {
    box.style.borderColor = isError ? '#ef4444' : '#0ea5e9';
    box.textContent = 'Spoolman-Runde: ' + text;
    if (!box.isConnected) (document.body || document.documentElement).appendChild(box);
  };
  whenReady(() => show('lese Preis …'));

  // ── Preis aus JSON-LD, sonst Meta-Tag ───────────────────────────────────
  const findPrice = () => {
    let price = null;
    let currency = 'CHF';
    const walk = node => {
      if (!node || typeof node !== 'object' || price) return;
      if (Array.isArray(node)) { node.forEach(walk); return; }
      if (node['@graph']) walk(node['@graph']);
      const types = [].concat(node['@type']);
      if (types.includes('Product') || types.includes('ProductGroup')) {
        const offer = [].concat(node.offers || [])[0];
        if (offer && (offer.price || offer.lowPrice)) {
          price = offer.price || offer.lowPrice;
          currency = offer.priceCurrency || currency;
        }
      }
      if (node.hasVariant) walk(node.hasVariant);
    };
    document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
      try { walk(JSON.parse(s.textContent)); } catch (e) { /* ungueltiges JSON-LD ignorieren */ }
    });
    if (!price) {
      const meta = document.querySelector('meta[property="product:price:amount"],meta[itemprop="price"]');
      if (meta && meta.content) price = meta.content;
    }
    return price ? { price: String(price), currency: String(currency) } : null;
  };

  // Runden-Hash sicher anfuegen, auch wenn next schon Query oder Hash hat
  const withRoundHash = target => {
    const u = new URL(target, location.href);
    u.hash = 'spoolman-runde=' + ROUND;
    return u.toString();
  };

  const MESSAGES = {
    'schluessel': 'ungültiger Schlüssel – Skript von der Runden-Seite neu installieren.',
    'runde-unbekannt': 'Runde unbekannt oder abgelaufen (1 h) – Runden-Seite neu laden und neu starten.',
    'nicht-in-runde': 'diese Seite gehört nicht zur Runde.',
    'spoolman': 'Spoolman nicht erreichbar – später erneut versuchen.',
  };
  const RESULT = {
    'ok': 'Preis übernommen',
    'bereits': 'bereits verarbeitet',
    'kein-preis': 'kein Preis gefunden, übersprungen',
    'preis': 'Preis nicht plausibel, übersprungen',
    'waehrung': 'keine CHF-Preisangabe, übersprungen',
    'unbekannt': 'Produkt in Spoolman nicht gefunden, übersprungen',
  };

  // Diagnose-Meldung an den Dienst (Rundenprotokoll, Issue #34) - speichert
  // keinen Preis, macht aber nachvollziehbar, wo eine Runde haengt.
  const beacon = (phase, detail) => {
    try {
      GM_xmlhttpRequest({
        method: 'POST',
        url: BASE + '/nachkaufen/galaxus-runde/status',
        data: new URLSearchParams({ token: TOKEN, round: ROUND, url: location.origin + location.pathname,
          phase: phase, detail: String(detail || '').slice(0, 200) }).toString(),
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        timeout: 5000,
      });
    } catch (e) { /* Diagnose darf die Runde nie stoeren */ }
  };

  let sent = false; // pro Seitenaufruf nur ein Request
  const send = found => {
    if (sent) return;
    sent = true;
    const body = new URLSearchParams({
      token: TOKEN,
      round: ROUND,
      url: location.origin + location.pathname,
      price: found ? found.price : '',
      currency: found ? found.currency : 'CHF',
    });
    GM_xmlhttpRequest({
      method: 'POST',
      url: BASE + '/nachkaufen/galaxus-runde/schritt',
      data: body.toString(),
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      timeout: REQUEST_TIMEOUT,
      onload: r => {
        if (r.status < 200 || r.status >= 300) {
          show('Serverfehler (HTTP ' + r.status + ') – Runde angehalten. Seite neu laden, um es erneut zu versuchen.', true);
          return;
        }
        let d;
        try { d = JSON.parse(r.responseText); } catch (e) {
          show('ungültige Antwort vom Server – Runde angehalten.', true);
          return;
        }
        if (!d || typeof d !== 'object') {
          show('ungültige Antwort vom Server – Runde angehalten.', true);
          return;
        }
        if (!d.ok) {
          clearState(); // Runde ungueltig/abgelaufen - nicht weiter ausloesen
          show('abgebrochen: ' + (MESSAGES[d.code] || d.code || 'unbekannter Fehler'), true);
          return;
        }
        const what = RESULT[d.code] || d.code;
        const step = d.position + '/' + d.total;
        if (d.next) {
          let nextUrl;
          try { nextUrl = withRoundHash(d.next); } catch (e) {
            show('ungültiger nächster Link – Runde angehalten.', true);
            return;
          }
          saveState({ round: ROUND, expect: pidOf(new URL(nextUrl).pathname), ts: Date.now() });
          show(step + ' ' + what + ' – weiter in ' + DELAY / 1000 + ' s …');
          setTimeout(() => { location.href = nextUrl; }, DELAY);
        } else {
          clearState(); // Runde beendet
          show(step + ' ' + what + ' – fertig, zurück zur Liste …');
          setTimeout(() => {
            location.href = BASE + '/nachkaufen?hinweis=runde-' + Number(d.saved || 0) + '-' + Number(d.total || 0);
          }, DELAY);
        }
      },
      onerror: () => show('Dienst nicht erreichbar – nichts gespeichert. Seite neu laden, um es erneut zu versuchen.', true),
      ontimeout: () => show('Zeitüberschreitung – Seite neu laden, um es erneut zu versuchen (bereits gespeicherte Preise werden nicht doppelt gezählt).', true),
    });
  };

  // Galaxus laedt Inhalte nach: bis zur Frist auf Produktdaten warten.
  // Fehler in der Suche oder Fristablauf fuehren IMMER zu einer Meldung
  // ("kein Preis") - die Runde bleibt nie auf einer Seite haengen.
  whenReady(() => {
    let jsonld = '?';
    try { jsonld = document.querySelectorAll('script[type="application/ld+json"]').length; } catch (e) { /* Diagnose */ }
    beacon('gestartet', 'Titel: ' + (document.title || '–').slice(0, 80) + ' | JSON-LD-Bloecke: ' + jsonld);
    const started = Date.now();
    const timer = setInterval(() => {
      let found = null;
      try {
        found = findPrice();
      } catch (e) {
        clearInterval(timer);
        beacon('fehler', 'Preissuche: ' + (e && e.message ? e.message : e));
        show('Fehler bei der Preissuche – übersprungen …', true);
        send(null);
        return;
      }
      if (found) {
        clearInterval(timer);
        beacon('preis-gefunden', found.price + ' ' + found.currency);
        send(found);
        return;
      }
      const left = Math.ceil((SEARCH_DEADLINE - (Date.now() - started)) / 1000);
      if (left <= 0) {
        clearInterval(timer);
        beacon('kein-preis', 'keine Produktdaten nach ' + SEARCH_DEADLINE / 1000 + ' s (Titel: ' + (document.title || '–').slice(0, 60) + ')');
        send(null);
        return;
      }
      show('suche Preis … ' + left + ' s');
    }, 500);
  });
})();
"""


def userscript_js(endpoint_base, token):
    """Tampermonkey-Skript: laeuft nur auf galaxus.ch-Seiten, die mit der
    Runden-Markierung (#spoolman-runde=<id>) geoeffnet wurden."""
    host = urlsplit(endpoint_base).hostname or ""
    return (_USERSCRIPT_TEMPLATE
            .replace("__BASE_JS__", "'" + endpoint_base.replace("\\", "\\\\").replace("'", "\\'") + "'")
            .replace("__BASE__", endpoint_base)
            .replace("__HOST__", host)
            .replace("__TOKEN__", json.dumps(token))
            .replace("__VERSION__", USERSCRIPT_VERSION)
            .replace("__DELAY_MS__", str(ROUND_STEP_DELAY_S * 1000))
            .replace("__DEADLINE_MS__", str(ROUND_SEARCH_DEADLINE_S * 1000)))


def _recent_rounds_html():
    rounds = recent_rounds()
    if not rounds:
        return ""
    items = "".join(
        f'<li><a href="/nachkaufen/galaxus-runde/protokoll?round={_esc(r["id"])}">'
        f'{datetime.fromtimestamp(r["created"]).strftime("%d.%m. %H:%M")}</a> – '
        f'{r["done"]}/{r["total"]} Seiten bearbeitet, {r["saved"]} Preise übernommen</li>' for r in rounds)
    return f'<h2>Letzte Runden</h2><ul>{items}</ul>'


def render_round_protocol(round_id):
    """Protokollseite einer Runde (Issue #34): je Galaxus-Seite Zustand, Zeit, Detail."""
    proto = round_protocol(round_id)
    if proto is None:
        body = ('<p>Diese Runde ist unbekannt oder älter als 24 h.</p>'
                '<p><a href="/nachkaufen/galaxus-runde">zur Galaxus-Runde</a></p>')
        refresh = ""
    else:
        rows = []
        for i, pg in enumerate(proto["pages"], 1):
            when = datetime.fromtimestamp(pg["ts"]).strftime("%H:%M:%S") if pg["ts"] else "–"
            cls = {"ok": "ok", "kein-preis": "warn", "fehler": "bad", "ausstehend": "muted"}.get(pg["state"], "")
            rows.append(
                f'<tr class="{cls}"><td>{i}</td>'
                f'<td><a href="{_esc(pg["url"])}" target="_blank" rel="noopener noreferrer">{_esc(pg["pid"])}</a></td>'
                f'<td>{_esc(ROUND_STATES.get(pg["state"], pg["state"]))}</td><td>{when}</td>'
                f'<td>{_esc(pg["detail"])}</td></tr>')
        done = sum(1 for pg in proto["pages"] if pg["state"] in _FINAL_STATES)
        started = datetime.fromtimestamp(proto["created"]).strftime("%d.%m.%Y %H:%M")
        body = (f'<p>Runde vom {started}: <strong>{done}/{len(proto["pages"])}</strong> Seiten bearbeitet, '
                f'<strong>{proto["saved"]}</strong> Preise übernommen.'
                f'{" Aktualisiert sich automatisch." if proto["running"] and done < len(proto["pages"]) else ""}</p>'
                f'<table><caption class="sr-only">Protokoll der Galaxus-Runde</caption><thead><tr>'
                f'<th scope="col">#</th><th scope="col">Produkt</th><th scope="col">Zustand</th>'
                f'<th scope="col">Zeit</th><th scope="col">Detail</th></tr></thead><tbody>{"".join(rows)}</tbody></table>'
                f'<p><a href="/nachkaufen/galaxus-runde">zur Galaxus-Runde</a> · <a href="/nachkaufen">zur Nachkauf-Liste</a></p>')
        refresh = ('<meta http-equiv="refresh" content="5">'
                   if proto["running"] and done < len(proto["pages"]) else "")
    return f'''<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
{refresh}<title>Protokoll Galaxus-Runde</title>
<style>
  body {{ margin:0; padding:20px; background:#1a1a1a; color:#f1f1f1; font:14px/1.5 -apple-system,"Segoe UI",Roboto,sans-serif; }}
  main {{ max-width:960px; margin:0 auto; }} h1 {{ font-size:1.3rem; }} a {{ color:#0ea5e9; }}
  table {{ width:100%; border-collapse:collapse; }} th, td {{ padding:6px 8px; border-bottom:1px solid #333; text-align:left; vertical-align:top; }}
  th {{ color:#a8a8a8; font-size:.75rem; text-transform:uppercase; }}
  tr.ok td:nth-child(3) {{ color:#22c55e; }} tr.warn td:nth-child(3) {{ color:#f59e0b; }}
  tr.bad td:nth-child(3) {{ color:#ef4444; }} tr.muted td {{ color:#8a8a8a; }}
  .sr-only {{ position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0,0,0,0); }}
</style></head><body><main><h1>Protokoll Galaxus-Runde</h1>{body}</main></body></html>'''


def render_round_page(round_id, urls, userscript_url):
    first = f"{urls[0]}#spoolman-runde={round_id}" if urls else ""
    items = "".join(f'<li><a href="{_esc(u)}" target="_blank" rel="noopener noreferrer">{_esc(u.rsplit("/", 1)[-1])}</a></li>'
                    for u in urls)
    start = (f'<a class="start" href="{_esc(first)}" data-round="{_esc(round_id)}" '
             f'data-first="{_esc(galaxus_product_id(urls[0]) or "")}">▶ Runde starten ({len(urls)} Galaxus-Seiten)</a>'
             if urls else '<p>Kein Filament hat in Spoolman einen Galaxus-Link (Feld „Galaxus“).</p>')
    return f'''<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Galaxus-Runde – Nachkaufen</title>
<style>
  body {{ margin:0; padding:20px; background:#1a1a1a; color:#f1f1f1; font:15px/1.5 -apple-system,"Segoe UI",Roboto,sans-serif; }}
  main {{ max-width:760px; margin:0 auto; }}
  h1 {{ font-size:1.4rem; margin:0 0 10px; }} h2 {{ font-size:1.05rem; margin:22px 0 6px; }}
  a {{ color:#0ea5e9; }} a:focus-visible {{ outline:2px solid #0ea5e9; outline-offset:2px; }}
  .start {{ display:inline-block; margin:12px 0; padding:10px 18px; border-radius:8px; background:#0ea5e9;
            color:#06121a; font-weight:700; text-decoration:none; }}
  .muted {{ color:#a8a8a8; font-size:.9rem; }}
  .script-warn, .script-ok {{ padding:8px 12px; border-radius:8px; margin:10px 0; }}
  .script-warn {{ background:#3a2a10; border-left:4px solid #f59e0b; }}
  .script-ok {{ background:#10301c; border-left:4px solid #22c55e; }}
  ol, ul {{ padding-left:22px; }}
</style></head>
<body><main>
  <h1>Galaxus-Runde</h1>
  <p id="userscript-status" class="script-warn" role="status">⚠ Tampermonkey-Skript nicht erkannt. Ohne Skript werden
     keine Preise übernommen – siehe „Einmalig einrichten“ unten (Skript installieren, bei Chrome/Edge
     „Benutzerskripts zulassen“ einschalten, Browser neu starten, diese Seite neu laden).</p>
  <p>Ruft <strong>alle</strong> in Spoolman hinterlegten Galaxus-Seiten (Feld „Galaxus“, unabhängig vom Bestand)
     nacheinander in diesem Tab auf und übernimmt die Preise. Gleiche Links werden nur einmal aufgerufen.
     Am Ende landest du wieder auf der Nachkauf-Liste.</p>
  {start}
  <p class="muted">Tempo: eine Seite etwa alle {ROUND_STEP_DELAY_S} s. Abbrechen: Tab schliessen oder zurück navigieren.</p>
  <p><a href="/nachkaufen/galaxus-runde/protokoll?round={_esc(round_id)}" target="_blank" rel="noopener">Protokoll dieser Runde</a>
     (zeigt je Galaxus-Seite, ob sie geladen wurde und was passiert ist)</p>
  {_recent_rounds_html()}
  <h2>Einmalig einrichten</h2>
  <ol>
    <li>Browser-Erweiterung <strong>Tampermonkey</strong> installieren (Chrome, Edge, Firefox, Safari).</li>
    <li><a href="{_esc(userscript_url)}">Skript „Spoolman Nachkaufen – Galaxus-Runde“ installieren</a> – Tampermonkey fragt nach, auf „Installieren“ klicken.</li>
    <li>Nur Chrome/Edge: Bei Erweiterungen für Tampermonkey „Benutzerskripte zulassen“ bzw. den Entwicklermodus aktivieren.</li>
  </ol>
  <p class="muted">Das Skript ist nur auf galaxus.ch aktiv und nur, wenn die Seite über diese Runde geöffnet wurde.</p>
  <h2>Galaxus-Seiten dieser Runde</h2>
  <ul>{items or "<li>–</li>"}</ul>
  <p><a href="/nachkaufen">← zurück zur Nachkauf-Liste</a></p>
</main></body></html>'''


# ─── HTML ────────────────────────────────────────────────────────────────────

def _esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def _fmt_chf(value):
    return f"CHF {value:,.2f}".replace(",", "'")


def _fmt_age(ts):
    if not ts:
        return "–"
    minutes = int((time.time() - ts) / 60)
    if minutes < 1:
        return "gerade eben"
    if minutes < 60:
        return f"vor {minutes} min"
    hours = minutes // 60
    return f"vor {hours} h" if hours < 48 else datetime.fromtimestamp(ts).strftime("%d.%m.%Y")


def _safe_hex(value):
    value = (value or "").strip().lstrip("#")
    return value if re.fullmatch(r"[0-9a-fA-F]{6}", value) else "888888"


def _render_cell(shop, cell, spoolman_public_url, filament_id):
    state = cell.get("state")
    if state == "missing":
        edit = f"{spoolman_public_url}/filament/show/{filament_id}"
        return (f'<td class="shop shop--missing"><div class="stack"><span class="muted">–</span>'
                f'<a class="small-link" href="{_esc(edit)}" target="_blank" rel="noopener noreferrer">'
                f'Link eintragen</a></div></td>')
    url = _esc(cell["url"])
    open_label = f"Bei {_esc(shop['label'])} öffnen (neuer Tab)"
    if state == "pending":
        return (f'<td class="shop"><div class="stack"><a class="shop-link" href="{url}" target="_blank" '
                f'rel="noopener noreferrer" title="{open_label}">Shop öffnen</a>'
                f'<span class="muted">Preis wird abgerufen…</span></div></td>')
    if state != "ok":
        reason = "Preis nicht abrufbar" if state == "blocked" else _esc(cell.get("error") or "kein Preis gefunden")
        detail = _esc(cell.get("error") or "")
        return (f'<td class="shop shop--nodata"><div class="stack"><a class="shop-link" href="{url}" '
                f'target="_blank" rel="noopener noreferrer" title="{open_label}">Shop öffnen</a>'
                f'<span class="muted" title="{detail}">{reason}</span></div></td>')

    cheapest = cell.get("cheapest")
    price_chf = cell.get("price_chf")
    main = _fmt_chf(price_chf) if price_chf is not None else f"{cell['price']:.2f} {_esc(cell.get('currency'))}"
    original = ""
    if cell.get("currency") and cell["currency"] != "CHF":
        original = f'<span class="muted">({cell["price"]:.2f} {_esc(cell["currency"])})</span>'
    avail = AVAILABILITY_LABELS.get(cell.get("availability") or "", "")
    avail_cls = " avail--bad" if cell.get("availability") in ("OutOfStock", "SoldOut", "Discontinued") else ""
    avail_html = f'<span class="avail{avail_cls}">{_esc(avail)}</span>' if avail else ""
    badge = '<span class="cheapest-badge">★ günstigster</span>' if cheapest else ""
    if cell.get("manual"):
        stamp = datetime.fromtimestamp(cell.get("fetched_at") or 0).strftime("%d.%m.")
        if cell.get("stale"):
            avail_html += (f'<span class="stale-note">veraltet – von dir übernommen am {stamp}, '
                           f'bitte neu übernehmen</span>')
        else:
            avail_html += f'<span class="muted">von dir übernommen · Stand {stamp}</span>'
    elif cell.get("last_error"):
        avail_html += (f'<span class="muted" title="{_esc(cell["last_error"])}">'
                       f'Stand {_fmt_age(cell.get("fetched_at"))}</span>')
    stale_cls = " shop--stale" if cell.get("stale") else ""
    return (f'<td class="shop{" shop--cheapest" if cheapest else ""}{stale_cls}"><div class="stack">'
            f'<a class="price-link" href="{url}" target="_blank" rel="noopener noreferrer" title="{open_label}">'
            f'<span class="price">{main}</span></a>{original}{badge}{avail_html}</div></td>')


def _bookmarklet_section(href):
    if not href:
        return ""
    return f'''
    <section class="bookmarklet-box" aria-labelledby="bm-title">
      <h2 id="bm-title">Galaxus-Preise übernehmen</h2>
      <p><a class="round-link" href="/nachkaufen/galaxus-runde" target="_blank" rel="noopener">▶ Galaxus-Runde: alle Galaxus-Preise automatisch nacheinander übernehmen</a>
         (einmalig Tampermonkey nötig). Einzelne Preise gehen auch mit dem Lesezeichen:</p>
      <p>Galaxus sperrt automatische Abrufe. Mit diesem Lesezeichen übernimmst du den Preis mit einem Klick,
         während du die Galaxus-Seite ganz normal im Browser offen hast.</p>
      <a class="bookmarklet" href="{_esc(href)}" title="In die Lesezeichenleiste ziehen">📌 Galaxus-Preis übernehmen</a>
      <ol>
        <li>Einmalig: den Knopf oben mit der Maus in die Lesezeichenleiste ziehen.</li>
        <li>In der Galaxus-Spalte auf „Shop öffnen“ klicken.</li>
        <li>Auf der Galaxus-Seite das Lesezeichen anklicken – der Preis erscheint hier (gilt 14 Tage).</li>
      </ol>
      <details><summary>Lesezeichen von Hand anlegen (z. B. Handy)</summary>
        <p>Neues Lesezeichen anlegen und als Adresse diesen Text einfügen:</p>
        <code>{_esc(href)}</code>
      </details>
    </section>'''


def render_page(rows, threshold, fx, embed=False, refreshing=False,
                spoolman_public_url="", notice="", bookmarklet_href=""):
    embed_field = '<input type="hidden" name="embed" value="1">' if embed else ""
    threshold_str = f"{threshold:g}"

    body_rows = []
    for row in rows:
        group = row["group"]
        fil = group["filament"]
        vendor = (fil.get("vendor") or {}).get("name") or ""
        colorid = _extra_value(fil, "colorid")
        pct = group["pct"]
        bar_cls = "bar--red" if pct < 10 else ("bar--orange" if pct < 20 else "bar--green")
        locations = sorted({s.get("location") or "–" for s in group["spools"]})
        spool_word = "Spule" if len(group["spools"]) == 1 else "Spulen"
        sub = " · ".join(p for p in (_esc(vendor), _esc(colorid)) if p)
        cells_html = "".join(
            _render_cell(shop, row["cells"][shop["key"]], spoolman_public_url, fil.get("id"))
            for shop in SHOPS)
        body_rows.append(f'''
        <tr>
          <td class="fil"><div class="fil-wrap">
            <span class="dot" style="background:#{_safe_hex(fil.get("color_hex"))}" aria-hidden="true"></span>
            <span class="fil-names"><span class="fil-name">{_esc(fil.get("name"))}</span>
            <span class="muted">{sub}</span></span>
          </div></td>
          <td class="stock"><div class="stack">
            <span class="stock-line"><strong>{pct:.0f}&nbsp;%</strong> · {group["remaining_g"]:.0f}&nbsp;g</span>
            <span class="bar" role="img" aria-label="Gesamtbestand {pct:.0f} Prozent"><span class="{bar_cls}" style="width:{max(2, min(100, pct)):.0f}%"></span></span>
            <span class="muted">{len(group["spools"])} {spool_word} · {_esc(", ".join(locations))}</span>
          </div></td>
          {cells_html}
        </tr>''')

    if body_rows:
        table = f'''
      <div class="tbl-wrap">
        <table>
          <caption class="sr-only">Filamente mit Gesamtbestand unter {threshold_str} Prozent und Shop-Preise in CHF</caption>
          <thead><tr>
            <th scope="col">Filament</th><th scope="col">Bestand</th>
            {"".join(f'<th scope="col">{_esc(s["label"])}</th>' for s in SHOPS)}
          </tr></thead>
          <tbody>{"".join(body_rows)}</tbody>
        </table>
      </div>'''
    else:
        table = (f'<p class="empty">Kein Filament unter {threshold_str}&nbsp;% Gesamtbestand – '
                 f'aktuell muss nichts nachgekauft werden.</p>')

    fetched = [c.get("fetched_at") for r in rows for c in r["cells"].values() if c.get("fetched_at")]
    oldest = min(fetched) if fetched else None
    fx_text = f"1 EUR = {fx['rate']:.4f} CHF ({_esc(fx.get('source'))}{', ' + _esc(fx['date']) if fx.get('date') else ''})"
    status = ('<span role="status" class="refreshing">Preise werden abgerufen…</span>' if refreshing
              else f'<span role="status">Preise: Stand {_fmt_age(oldest)}</span>')
    notice_html = f'<p class="notice" role="status">{_esc(notice)}</p>' if notice else ""
    # Waehrend eines Abrufs automatisch neu laden, bis die Preise da sind
    auto_reload = '<meta http-equiv="refresh" content="15">' if refreshing else ""
    header = "" if embed else '<h1>Nachkaufen</h1>'

    return f'''<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{auto_reload}
<title>Nachkaufen – Filamente mit niedrigem Bestand</title>
<style>
  :root {{ --bg:#1a1a1a; --card:#222; --line:#333; --text:#f1f1f1; --muted:#a8a8a8;
          --accent:#0ea5e9; --green:#22c55e; --orange:#f59e0b; --red:#ef4444; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; padding:{"6px" if embed else "20px"}; background:var(--bg); color:var(--text);
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; font-size:14px; }}
  h1 {{ margin:0 0 12px; font-size:1.4rem; }}
  .toolbar {{ display:flex; flex-wrap:wrap; align-items:center; gap:10px 16px; margin-bottom:10px; }}
  .toolbar form {{ display:flex; align-items:center; gap:8px; margin:0; }}
  label {{ color:var(--text); }}
  input[type=number] {{ width:5.5em; padding:6px 8px; border:1px solid #555; border-radius:6px;
                        background:#2b2b2b; color:var(--text); font:inherit; }}
  button {{ padding:6px 12px; border:1px solid var(--accent); border-radius:6px; background:transparent;
            color:var(--text); font:inherit; cursor:pointer; }}
  button:hover {{ background:rgba(14,165,233,.15); }}
  input:focus-visible, button:focus-visible, a:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; }}
  .status {{ margin-left:auto; color:var(--muted); font-size:.85rem; }}
  .refreshing {{ color:var(--accent); }}
  .notice {{ margin:0 0 10px; padding:8px 10px; border-left:3px solid var(--accent); background:#1e2a33; }}
  .tbl-wrap {{ overflow-x:auto; border:1px solid var(--line); border-radius:10px; }}
  table {{ width:100%; border-collapse:collapse; min-width:860px; }}
  th {{ text-align:left; font-size:.75rem; text-transform:uppercase; letter-spacing:.04em; color:var(--muted);
        padding:10px; background:#262626; border-bottom:1px solid var(--line); }}
  td {{ padding:10px; border-bottom:1px solid var(--line); vertical-align:top; }}
  tr:last-child td {{ border-bottom:none; }}
  .fil-wrap {{ display:flex; gap:10px; align-items:flex-start; min-width:200px; }}
  .dot {{ flex:none; width:22px; height:22px; border-radius:50%; border:1px solid rgba(255,255,255,.35); margin-top:2px; }}
  .fil-names, .stack {{ display:flex; flex-direction:column; gap:3px; }}
  .fil-name {{ font-weight:600; }}
  .muted {{ color:var(--muted); font-size:.8rem; }}
  .stock {{ min-width:150px; }}
  .bar {{ display:block; height:6px; border-radius:3px; background:#3a3a3a; overflow:hidden; }}
  .bar span {{ display:block; height:100%; }}
  .bar--red {{ background:var(--red); }} .bar--orange {{ background:var(--orange); }} .bar--green {{ background:var(--green); }}
  .shop {{ min-width:130px; }}
  .price-link {{ color:var(--text); text-decoration:none; }}
  .price-link:hover .price {{ text-decoration:underline; }}
  .price {{ font-weight:700; font-size:1rem; font-variant-numeric:tabular-nums; }}
  .shop--cheapest {{ background:rgba(34,197,94,.10); }}
  .cheapest-badge {{ color:var(--green); font-weight:600; font-size:.8rem; }}
  .avail {{ font-size:.8rem; color:var(--green); }}
  .avail--bad {{ color:var(--red); }}
  .shop-link, .small-link {{ color:var(--accent); }}
  .small-link {{ font-size:.8rem; }}
  .shop--stale .price {{ color:var(--muted); text-decoration:line-through; }}
  .stale-note {{ color:var(--orange); font-size:.8rem; }}
  .bookmarklet-box {{ margin-top:14px; padding:12px 14px; border:1px dashed #555; border-radius:10px; }}
  .bookmarklet-box h2 {{ margin:0 0 6px; font-size:1rem; }}
  .bookmarklet-box p, .bookmarklet-box li {{ margin:4px 0; color:var(--muted); font-size:.85rem; }}
  .bookmarklet {{ display:inline-block; margin:6px 0; padding:6px 12px; border:1px solid var(--accent);
                  border-radius:16px; color:var(--text); text-decoration:none; font-weight:600; cursor:grab; }}
  .bookmarklet:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; }}
  .bookmarklet-box code {{ display:block; max-height:6em; overflow:auto; padding:6px; background:#111;
                           border-radius:6px; word-break:break-all; font-size:.72rem; color:var(--muted); }}
  .empty {{ padding:24px; text-align:center; color:var(--muted); border:1px solid var(--line); border-radius:10px; }}
  .footnote {{ margin-top:8px; color:var(--muted); font-size:.78rem; }}
  .sr-only {{ position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; }}
</style>
</head>
<body>
  <main>
    {header}
    <div class="toolbar">
      <form method="get" action="/nachkaufen">
        {embed_field}
        <label for="max">Gesamtbestand unter</label>
        <input id="max" name="max" type="number" min="1" max="100" step="1" value="{_esc(threshold_str)}" inputmode="numeric">
        <span aria-hidden="true">%</span>
        <button type="submit">Anwenden</button>
      </form>
      <form method="post" action="/nachkaufen/refresh">
        {embed_field}
        <input type="hidden" name="max" value="{_esc(threshold_str)}">
        <button type="submit">Preise aktualisieren</button>
      </form>
      <span class="status">{status}</span>
    </div>
    {notice_html}
    {table}
    <p class="footnote">Preise laut Shop inkl. MwSt., ohne Versand, umgerechnet in CHF · {fx_text} ·
      ★ = günstigster lieferbarer Preis · Shop-Links pflegst du pro Filament in Spoolman
      (Felder Bestellseite, Bambu Lab, Galaxus, Amazon, CCTREE, 3DJake) · Links „Bambu Lab“ und „CCTREE“ werden für
      Filamente mit ColorID automatisch ergänzt (<a href="/nachkaufen/shop-links" target="_blank" rel="noopener">Übersicht</a>).</p>
    {_bookmarklet_section(bookmarklet_href)}
  </main>
</body>
</html>'''
