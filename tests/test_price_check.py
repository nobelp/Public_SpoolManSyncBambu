"""Prueft price_check.py ohne Netzwerk: Preis-Auslese (JSON-LD, Varianten,
Open Graph, Amazon), Bestands-Auswahl, URL-Pruefung (SSRF), Cache-Merge,
CHF-Umrechnung, Vergleich und HTML-Escaping."""
import json
import os
import tempfile
import time

os.environ["PRICE_CACHE_FILE"] = os.path.join(tempfile.mkdtemp(), "price_cache.json")

import price_check as pc

ok = True


def check(label, cond):
    global ok
    print(("  PASS  " if cond else "  FAIL  ") + label)
    ok = ok and cond


def ld(obj):
    return f'<script type="application/ld+json">{json.dumps(obj)}</script>'


def spool(sid, fid, initial, remaining, archived=False, extra=None, name="Filament"):
    return {"id": sid, "archived": archived, "initial_weight": initial, "remaining_weight": remaining,
            "location": "Lager",
            "filament": {"id": fid, "name": name, "weight": 1000, "color_hex": "112233",
                         "extra": extra or {}}}


print("1) Preiswerte parsen")
for text, expected in [("20.00", 20.0), ("12,82CHF", 12.82), ("1.234,56 €", 1234.56),
                       ("CHF 1'299.50", 1299.5), ("16.05", 16.05), ("1,299.00", 1299.0),
                       (17.15, 17.15), ("gratis", None), ("0.00", None), (None, None)]:
    check(f"{text!r} -> {expected}", pc.parse_price_value(text) == expected)

print("2) JSON-LD: einzelnes Angebot (3DJake)")
page = ld({"@context": "https://schema.org", "@type": "Product", "name": "PLA Basic Black, Spule",
           "offers": {"@type": "Offer", "price": "20.00", "priceCurrency": "CHF",
                      "availability": "https://schema.org/InStock",
                      "url": "https://www.3djake.ch/de-CH/bambu-lab/pla-basic-black?sai=23885"}})
r = pc.extract_price(page, "https://www.3djake.ch/de-CH/bambu-lab/pla-basic-black")
check("Preis 20.00 CHF, lieferbar", r and r["price"] == 20.0 and r["currency"] == "CHF"
      and r["availability"] == "InStock")

print("3) JSON-LD: viele Varianten, Link-Variante gewinnt (Bambu-Store)")
variants = [{"@type": "Product", "sku": str(vid), "name": name,
             "offers": {"@type": "Offer", "price": price, "priceCurrency": "EUR",
                        "availability": "https://schema.org/InStock",
                        "url": f"https://eu.store.bambulab.com/products/pla-basic-filament?id={vid}"}}
            for vid, name, price in [(111, "Rot (10200)", 16.99), (43923829129435, "Schwarz (10101)", 15.49),
                                     (333, "Grau (10103)", 18.99)]]
page = ld({"@context": "https://schema.org", "@graph": variants})
r = pc.extract_price(page, "https://eu.store.bambulab.com/de/products/pla-basic-filament?id=43923829129435")
check("gewaehlte Variante 15.49 EUR", r and r["price"] == 15.49 and r["currency"] == "EUR")
r = pc.extract_price(page, "https://eu.store.bambulab.com/de/products/pla-basic-filament")
check("ohne ?id= -> erstes Angebot", r and r["price"] == 16.99)

print("4) Open Graph und Amazon-Preisfeld")
page = ('<meta property="product:price:amount" content="19.90">'
        '<meta property="product:price:currency" content="CHF">')
r = pc.extract_price(page, "https://shop.example/p/1")
check("Open Graph 19.90 CHF", r and r["price"] == 19.9 and r["currency"] == "CHF")
page = ('<div id="corePrice_feature_div"><span class="a-offscreen">12,82CHF</span></div>'
        '<div id="availability"> Auf Lager </div>')
r = pc.extract_price(page, "https://www.amazon.de/dp/B0BT9FTV3L")
check("Amazon 12.82 CHF, lieferbar", r and r["price"] == 12.82 and r["currency"] == "CHF"
      and r["availability"] == "InStock")
check("Seite ohne Preis -> None", pc.extract_price("<html><p>Hallo</p></html>", "https://x.ch/") is None)

print("5) Auswahl nach Gesamtbestand")
spools = [
    spool(1, 10, 1000, 100),                 # 10 % -> gelistet
    spool(2, 20, 1000, 100), spool(3, 20, 1000, 1000),  # 10 % + volle Reserve = 55 % -> nicht
    spool(4, 30, 1000, 150), spool(5, 30, 1000, 50),    # 10 % gesamt -> gelistet
    spool(6, 40, 1000, 10, archived=True),   # archiviert -> ignoriert
    spool(7, 50, 1000, 190),                 # 19 % -> gelistet
    spool(8, 60, 1000, 200),                 # genau 20 % -> nicht (< Schwelle)
]
sel = pc.select_low_stock(spools, 20)
check("Filamente 10, 30, 50 gelistet", sorted(g["filament"]["id"] for g in sel) == [10, 30, 50])
check("leerstes zuerst", sel[0]["pct"] <= sel[-1]["pct"])
check("Gesamtbestand Filament 30 = 10 %", abs([g for g in sel if g["filament"]["id"] == 30][0]["pct"] - 10) < 1e-9)
check("Schwelle 60 nimmt Reserve-Filament 20 dazu",
      20 in [g["filament"]["id"] for g in pc.select_low_stock(spools, 60)])
check("parse_threshold: Standard/Grenzen/Komma", pc.parse_threshold(None) == pc.DEFAULT_THRESHOLD_PCT
      and pc.parse_threshold("0") == 1 and pc.parse_threshold("250") == 100
      and pc.parse_threshold("12,5") == 12.5 and pc.parse_threshold("abc") == pc.DEFAULT_THRESHOLD_PCT)

print("6) URL-Pruefung (SSRF-Schutz)")
check("127.0.0.1 nicht oeffentlich", not pc._resolves_public("127.0.0.1"))
check("192.168.1.100 nicht oeffentlich", not pc._resolves_public("192.168.1.100"))
check("169.254.169.254 nicht oeffentlich", not pc._resolves_public("169.254.169.254"))
check("8.8.8.8 oeffentlich", pc._resolves_public("8.8.8.8"))
real_resolver = pc._resolves_public
pc._resolves_public = lambda host: True  # Domain-Regeln ohne DNS testen
amazon = next(sh["domains"] for sh in pc.SHOPS if sh["key"] == "url_amazon")
bambu = next(sh["domains"] for sh in pc.SHOPS if sh["key"] == "url_bambulab")
check("https + erlaubte Domain ok", pc.validate_shop_url("https://www.amazon.de/dp/B0BT9FTV3L", amazon)[0])
check("http abgelehnt", not pc.validate_shop_url("http://www.amazon.de/dp/X", amazon)[0])
check("falsche Domain abgelehnt", not pc.validate_shop_url("https://amazon.de.evil.com/dp/X", amazon)[0])
check("Benutzerdaten in URL abgelehnt", not pc.validate_shop_url("https://a:b@www.amazon.de/", amazon)[0])
check("javascript: abgelehnt", not pc.validate_shop_url("javascript:alert(1)", None)[0])
check("Bestellseite: beliebige https-Domain", pc.validate_shop_url("https://eu.store.bambulab.com/x", None)[0])
check("Bambu-Lab-Spalte: Store ok", pc.validate_shop_url("https://eu.store.bambulab.com/de/products/pla-matte?id=1", bambu)[0])
check("Bambu-Lab-Spalte: fremde Domain abgelehnt", not pc.validate_shop_url("https://www.3djake.ch/bambu-lab/x", bambu)[0])
pc._resolves_public = real_resolver

print("7) Amazon-Links zusammenfassen")
check("Tracking/Titel -> /dp/ASIN",
      pc.fetch_key("https://www.amazon.de/SUNLU-Filament/dp/B0FH89RVYF?adgrpid=1&th=1")
      == "https://www.amazon.de/dp/B0FH89RVYF")
check("andere Shops unveraendert", pc.fetch_key("https://www.3djake.ch/a?b=1") == "https://www.3djake.ch/a?b=1")

check("fehlendes https:// wird ergaenzt", pc.normalize_url("amazon.de/x/dp/B0F8TRWHJ8/ref=1")
      == "https://amazon.de/x/dp/B0F8TRWHJ8/ref=1")
check("javascript: bleibt (wird abgelehnt)", pc.normalize_url("javascript:alert(1)") == "javascript:alert(1)")
check("vollstaendige URL unveraendert", pc.normalize_url(" https://www.3djake.ch/a ") == "https://www.3djake.ch/a")

print("8) Cache: blockierter Abruf ueberschreibt keinen guten Preis")
old = {"url": "u", "status": "ok", "price": 12.0, "currency": "CHF", "fetched_at": 100, "checked_at": 100}
merged = pc._merge_result(old, {"url": "u", "status": "blocked", "error": "Bot-Schutz", "fetched_at": 200})
check("Preis bleibt, Hinweis gesetzt", merged["price"] == 12.0 and merged["last_error"] == "Bot-Schutz"
      and merged["checked_at"] == 200 and merged["fetched_at"] == 100)
merged = pc._merge_result(old, {"url": "u", "status": "ok", "price": 11.0, "currency": "CHF", "fetched_at": 300})
check("neuer Preis ersetzt alten", merged["price"] == 11.0 and "last_error" not in merged)
now = time.time()
check("blockiert: nach 1 h erneut versuchen",
      pc._stale({"status": "blocked", "checked_at": now - 3700}, now)
      and not pc._stale({"status": "blocked", "checked_at": now - 600}, now))
check("ok: 12 h gueltig", not pc._stale({"status": "ok", "checked_at": now - 3700}, now))

print("9) CHF-Umrechnung und guenstigster Preis")
check("EUR -> CHF", pc.to_chf(16.05, "EUR", 0.9445) == 15.16)
check("CHF bleibt", pc.to_chf(20.0, "CHF", 0.9445) == 20.0)
check("USD wird nicht verglichen", pc.to_chf(10.0, "USD", 0.9445) is None)
extra = {"orderurl": json.dumps("https://eu.store.bambulab.com/p?id=1"),
         "url_amazon": json.dumps("https://www.amazon.de/dp/B0BT9FTV3L"),
         "url_3djake": json.dumps("https://www.3djake.ch/p")}
group = pc.select_low_stock([spool(1, 10, 1000, 50, extra=extra)], 20)[0]
prices = {
    "https://eu.store.bambulab.com/p?id=1": {"status": "ok", "price": 16.0, "currency": "EUR", "availability": "InStock"},
    "https://www.amazon.de/dp/B0BT9FTV3L": {"status": "ok", "price": 12.0, "currency": "CHF", "availability": "OutOfStock"},
    "https://www.3djake.ch/p": {"status": "ok", "price": 17.15, "currency": "CHF", "availability": "InStock"},
}
cells = pc.build_rows([group], prices, 0.9445)[0]["cells"]
check("ausverkauftes Amazon (12.00) ist nicht 'guenstigster'", not cells["url_amazon"].get("cheapest"))
check("Bestellseite 16 EUR = 15.11 CHF ist guenstigster lieferbarer",
      cells["orderurl"].get("cheapest") and cells["orderurl"]["price_chf"] == 15.11)
check("fehlender Galaxus-Link -> missing", cells["url_galaxus"]["state"] == "missing")
check("Spalten-Reihenfolge und Namen", [sh["label"] for sh in pc.SHOPS]
      == ["Primär bestellen", "Bambu Lab", "Galaxus", "Amazon", "CCTREE", "3DJake"])

print("10) HTML: Escaping und Zustaende")
evil = {"orderurl": json.dumps('https://x.ch/"><script>alert(1)</script>')}
g = pc.select_low_stock([spool(1, 10, 1000, 50, extra=evil, name='<img src=x onerror=alert(1)>')], 20)
page = pc.render_page(pc.build_rows(g, {}, 0.94), 20, {"rate": 0.94, "source": "fester Kurs"},
                      spoolman_public_url="https://spoolman.example")
check("kein ungefiltertes <script>/<img>", "<script>alert" not in page and "<img src=x" not in page)
check("Preis wird abgerufen", "Preis wird abgerufen" in page)
check("Link eintragen -> Spoolman", 'href="https://spoolman.example/filament/show/10"' in page)
empty = pc.render_page([], 20, {"rate": 0.94, "source": "fester Kurs"})
check("leere Liste verstaendlich", "muss nichts nachgekauft werden" in empty)
check("Eingabe fuer Schwelle vorhanden", 'name="max"' in empty and 'type="number"' in empty)

print("11) Galaxus-Preis per Lesezeichen")
G = "https://www.galaxus.ch/de/s1/product/bambu-lab-pla-basic-pla-1000-g-175-mm-3d-filament-37001434"
check("Produktnummer aus URL", pc.galaxus_product_id(G) == "37001434")
check("Produktnummer ohne https:// (normalisiert)", pc.galaxus_product_id(G.replace("https://", "")) == "37001434")
check("fremde Domain -> keine Nummer", pc.galaxus_product_id("https://www.galaxus.ch.evil.com/x-37001434") is None)
tok = pc.get_bookmarklet_token()
check("Schluessel erzeugt und stabil", len(tok) >= 20 and pc.get_bookmarklet_token() == tok)
fils = [{"id": 3, "name": "Black", "extra": {"url_galaxus": json.dumps(G)}},
        {"id": 4, "name": "Other", "extra": {}}]
check("falscher Schluessel abgelehnt", pc.submit_manual_price(fils, G, "19.90", "CHF", "falsch")[0] == "schluessel")
check("keine Galaxus-Seite abgelehnt", pc.submit_manual_price(fils, "https://www.3djake.ch/x-123456", "19.90", "CHF", tok)[0] == "keine-galaxus-seite")
check("unbekanntes Produkt abgelehnt",
      pc.submit_manual_price(fils, "https://www.galaxus.ch/de/s1/product/x-99999999", "19.90", "CHF", tok)[0] == "unbekannt")
check("EUR abgelehnt", pc.submit_manual_price(fils, G, "19.90", "EUR", tok)[0] == "waehrung")
check("unplausibler Preis abgelehnt", pc.submit_manual_price(fils, G, "0.50", "CHF", tok)[0] == "preis"
      and pc.submit_manual_price(fils, G, "9999", "CHF", tok)[0] == "preis")
code, fil = pc.submit_manual_price(fils, G + "?utm=x", "19,90", "CHF", tok)
entry = pc.get_cached_prices().get(pc.fetch_key(G))
check("gueltiger Preis gespeichert (Komma, Query egal)", code == "ok" and fil["id"] == 3
      and entry and entry["price"] == 19.9 and entry["source"] == "manual")
group = {"filament": fils[0], "spools": [], "pct": 5, "remaining_g": 50, "initial_g": 1000}
check("automatischer Abruf ueberspringt uebernommenen Preis", pc.refresh_prices([group]) is False)
cells = pc.build_rows([group], pc.get_cached_prices(), 0.94)[0]["cells"]
check("Zelle: von dir uebernommen, nicht veraltet", cells["url_galaxus"].get("manual") and not cells["url_galaxus"].get("stale"))
old = dict(entry, fetched_at=time.time() - 15 * 24 * 3600)
extra2 = {"url_galaxus": json.dumps(G), "url_3djake": json.dumps("https://www.3djake.ch/p2")}
group2 = {"filament": {"id": 3, "extra": extra2}, "spools": [], "pct": 5, "remaining_g": 50, "initial_g": 1000}
prices2 = {pc.fetch_key(G): old,
           "https://www.3djake.ch/p2": {"status": "ok", "price": 25.0, "currency": "CHF", "availability": "InStock"}}
cells2 = pc.build_rows([group2], prices2, 0.94)[0]["cells"]
check("nach 14 Tagen veraltet und nicht 'guenstigster'", cells2["url_galaxus"].get("stale")
      and not cells2["url_galaxus"].get("cheapest"))
html_cell = pc._render_cell(pc.SHOPS[2], cells2["url_galaxus"], "https://sm", 3)
check("veraltet wird angezeigt", "veraltet" in html_cell and "shop--stale" in html_cell)
js = pc.bookmarklet_js("https://history.example/nachkaufen/galaxus", tok)
check("Lesezeichen: javascript:, ohne '#'/'%', nur ASCII", js.startswith("javascript:") and "#" not in js
      and "%" not in js and all(ord(ch) < 128 for ch in js))
check("Lesezeichen enthaelt Schluessel und Ziel", json.dumps(tok) in js and "history.example/nachkaufen/galaxus" in js)
page = pc.render_page([], 20, {"rate": 0.94, "source": "fester Kurs"}, bookmarklet_href=js)
check("Lesezeichen-Knopf auf der Seite", "📌 Galaxus-Preis übernehmen" in page and 'href="javascript:' in page)

print("12) Galaxus-Runde (Tampermonkey)")
GA = "https://www.galaxus.ch/de/s1/product/a-11111111"
GB = "https://www.galaxus.ch/de/s1/product/b-22222222"
GC = "https://www.galaxus.ch/de/s1/product/c-33333333"
rfils = [{"id": i, "name": f"F{i}", "extra": {"url_galaxus": json.dumps(u)}} for i, u in ((11, GA), (12, GB), (13, GC))]
rid, rurls = pc.start_round(rfils + [{"id": 14, "extra": {}},
                                     {"id": 15, "extra": {"url_galaxus": json.dumps(GA)}}])
check("Runde: alle Galaxus-Links, ohne Filamente ohne Link, doppelte nur einmal", rurls == [GA, GB, GC])
check("falscher Schluessel", pc.round_step(rid, GA, "10", "CHF", "x", rfils)["code"] == "schluessel")
check("unbekannte Runde", pc.round_step("gibtsnicht", GA, "10", "CHF", tok, rfils)["code"] == "runde-unbekannt")
check("Produkt nicht in Runde",
      pc.round_step(rid, "https://www.galaxus.ch/de/s1/product/z-99999999", "10", "CHF", tok, rfils)["code"] == "nicht-in-runde")
r1 = pc.round_step(rid, GA + "?x=1", "19.90", "CHF", tok, rfils)
check("Schritt 1: Preis gespeichert, naechster Link", r1["ok"] and r1["code"] == "ok" and r1["next"] == GB
      and r1["position"] == 1 and r1["total"] == 3 and r1["saved"] == 1)
r2 = pc.round_step(rid, GB, "", "CHF", tok, rfils)
check("Schritt 2: kein Preis -> uebersprungen, weiter", r2["ok"] and r2["code"] == "kein-preis" and r2["next"] == GC)
r3 = pc.round_step(rid, GC, "21.50", "CHF", tok, rfils)
check("Schritt 3: letzter -> kein naechster, 2 gespeichert", r3["next"] is None and r3["saved"] == 2)
check("Preis aus Runde im Cache", pc.get_cached_prices()[pc.fetch_key(GC)]["price"] == 21.5)
r3b = pc.round_step(rid, GC, "99.00", "CHF", tok, rfils)
check("Neuladen derselben Seite: 'bereits', nichts gespeichert, Zaehler gleich",
      r3b["ok"] and r3b["code"] == "bereits" and r3b["saved"] == 2
      and pc.get_cached_prices()[pc.fetch_key(GC)]["price"] == 21.5)
r1b = pc.round_step(rid, GA, "5.00", "CHF", tok, rfils)
check("frueherer Schritt erneut: gleicher naechster Link, nichts ueberschrieben",
      r1b["code"] == "bereits" and r1b["next"] is None
      and pc.get_cached_prices()[pc.fetch_key(GA)]["price"] == 19.9)
import threading as _th
rid2, _ = pc.start_round(rfils)
results = []
ths = [_th.Thread(target=lambda: results.append(pc.round_step(rid2, GB, "15.00", "CHF", tok, rfils))) for _ in range(5)]
[t.start() for t in ths]; [t.join() for t in ths]
check("5 gleichzeitige Meldungen: genau 1x gespeichert", sorted(r["code"] for r in results).count("ok") == 1
      and max(r["saved"] for r in results) == 1)
pc._rounds[rid]["created"] -= pc.ROUND_TTL_S + 1
check("abgelaufene Runde", pc.round_step(rid, GA, "10", "CHF", tok, rfils)["code"] == "runde-unbekannt")
us = pc.userscript_js("https://history.example", tok)
check("Userscript: Kopf, @connect, nur galaxus.ch, Schluessel", "// ==UserScript==" in us
      and "// @connect      history.example" in us and "// @match        https://www.galaxus.ch/*" in us
      and json.dumps(tok) in us and "u.hash = 'spoolman-runde=' + ROUND" in us
      and "const BASE = 'https://history.example';" in us
      and "// @namespace    https://history.example" in us
      and "GM_xmlhttpRequest({" in us and "GM\\_" not in us and "](http" not in us
      and "// @run-at       document-start" in us and "GM_setValue(KEY" in us
      and "// @updateURL    https://history.example/nachkaufen/galaxus.user.js" in us)
rp = pc.render_round_page(rid, rurls, "https://history.example/nachkaufen/galaxus.user.js")
check("Rundenseite: Start-Link mit Markierung", f"{GA}#spoolman-runde={rid}" in rp and "Runde starten (3" in rp)
check("Rundenseite ohne Links verstaendlich", "Galaxus-Link" in pc.render_round_page("x", [], "u"))
open(os.path.join(tempfile.gettempdir(), "spoolman_userscript_test.js"), "w").write(us)

print("13) Rundenprotokoll und Diagnose (Issue #34)")
rid3, urls3 = pc.start_round(rfils)
proto = pc.round_protocol(rid3)
check("neue Runde: alle Seiten 'ausstehend'", [p["state"] for p in proto["pages"]] == ["ausstehend"] * 3)
check("Status: falscher Schluessel", pc.round_status(rid3, GA, "gestartet", "", "x")["code"] == "schluessel")
check("Status: unbekannte Phase", pc.round_status(rid3, GA, "hack", "", tok)["code"] == "phase")
check("Status: fremdes Produkt", pc.round_status(rid3, "https://www.galaxus.ch/de/s1/product/z-99999999", "gestartet", "", tok)["code"] == "nicht-in-runde")
check("Status 'gestartet' protokolliert", pc.round_status(rid3, GA, "gestartet", "Titel: X | JSON-LD-Bloecke: 2", tok)["ok"]
      and pc.round_protocol(rid3)["pages"][0]["state"] == "gestartet"
      and "JSON-LD" in pc.round_protocol(rid3)["pages"][0]["detail"])
pc.round_step(rid3, GA, "18.40", "CHF", tok, rfils)
check("Schritt mit Preis -> 'ok' mit Betrag", pc.round_protocol(rid3)["pages"][0]["state"] == "ok"
      and pc.round_protocol(rid3)["pages"][0]["detail"] == "CHF 18.40")
pc.round_status(rid3, GA, "gestartet", "spaet", tok)
check("Endzustand wird nicht durch spaete Zwischenmeldung ueberschrieben", pc.round_protocol(rid3)["pages"][0]["state"] == "ok")
pc.round_status(rid3, GB, "kein-preis", "keine Produktdaten nach 12 s", tok)
pc.round_step(rid3, GB, "", "CHF", tok, rfils)
check("Seite ohne Preis -> 'kein-preis', Diagnose bleibt erhalten", pc.round_protocol(rid3)["pages"][1]["state"] == "kein-preis"
      and "12 s" in pc.round_protocol(rid3)["pages"][1]["detail"])
check("letzte Runden listet aktive Runde", any(r["id"] == rid3 and r["done"] == 2 and r["saved"] == 1 for r in pc.recent_rounds()))
html_p = pc.render_round_protocol(rid3)
check("Protokollseite zeigt Zustaende", "Preis übernommen" in html_p and "kein Preis gefunden" in html_p and "ausstehend" in html_p
      and "2/3" in html_p and 'http-equiv="refresh"' in html_p)
check("unbekannte Runde verstaendlich", "unbekannt oder älter als 24 h" in pc.render_round_protocol("gibtsnicht"))
pc._rounds[rid3]["created"] -= pc.ROUND_TTL_S + 1
check("nach 1 h: Status abgelehnt, Protokoll noch lesbar", pc.round_status(rid3, GC, "gestartet", "", tok)["code"] == "runde-unbekannt"
      and pc.round_protocol(rid3) is not None and 'http-equiv="refresh"' not in pc.render_round_protocol(rid3))
us13 = pc.userscript_js("https://history.example", tok)
check("Userscript: Diagnose, Frist, Fehlerbehandlung", "// @version      1.4" in us13 and "/nachkaufen/galaxus-runde/status" in us13
      and "SEARCH_DEADLINE = 12000" in us13 and "beacon('fehler'" in us13 and "beacon('gestartet'" in us13)
check("Userscript 1.4: Rundenseite + Tampermonkey-Speicher + Ping", "// @match        https://history.example/nachkaufen/galaxus-runde*" in us13
      and "// @grant        GM_setValue" in us13 and "// @grant        GM_getValue" in us13 and "// @grant        GM_deleteValue" in us13
      and "/nachkaufen/galaxus-runde/ping" in us13 and "sessionStorage" not in us13)
rp14 = pc.render_round_page("RID123", [GA, GB], "https://history.example/nachkaufen/galaxus.user.js")
check("Rundenseite: Start-Link mit data-round/data-first, Skript-Status", 'data-round="RID123"' in rp14
      and 'data-first="11111111"' in rp14 and 'id="userscript-status"' in rp14 and "nicht erkannt" in rp14)
check("Ping: Schluessel geprueft, Log", pc.round_ping("x", "1.4", "1", "ja", "-", "", "aktiv")["ok"] is False
      and pc.round_ping(tok, "1.4", "11111111", "nein", "11111111", "RID", "aktiv")["ok"] is True)
open(os.path.join(tempfile.gettempdir(), "spoolman_userscript_test.js"), "w").write(us13)

print("14) Preise je Produkt statt je URL-Text (Issue #38)")
U6 = "https://www.galaxus.ch/de/s1/product/bambu-lab-pla-basic-pla-1000-g-175-mm-3d-filament-40297740"
U9 = "https://www.galaxus.ch/de/s1/product/bambu-lab-pla-basic-pla-1000-g-175-mm-filament-40297740"
check("Galaxus: gleiche Produktnummer -> gleicher Schluessel", pc.fetch_key(U6) == pc.fetch_key(U9)
      == "https://www.galaxus.ch/de/s1/product/40297740")
check("Sprungmarke gehoert nicht zum Schluessel", pc.fetch_key("https://www.3djake.ch/a?b=1#x") == "https://www.3djake.ch/a?b=1")
f69 = [{"id": 6, "name": "PLA", "extra": {"url_galaxus": json.dumps(U6)}},
       {"id": 9, "name": "PETG", "extra": {"url_galaxus": json.dumps(U9)}}]
check("uebernommener Preis (URL von #9) ...", pc.submit_manual_price(f69, U9, "22.90", "CHF", tok)[0] == "ok")
cells6 = pc.build_rows([{"filament": f69[0], "spools": [], "pct": 5, "remaining_g": 1, "initial_g": 1000}],
                       pc.get_cached_prices(), 0.94)[0]["cells"]["url_galaxus"]
cells9 = pc.build_rows([{"filament": f69[1], "spools": [], "pct": 5, "remaining_g": 1, "initial_g": 1000}],
                       pc.get_cached_prices(), 0.94)[0]["cells"]["url_galaxus"]
check("... gilt fuer #6 und #9", cells6.get("price") == 22.9 and cells9.get("price") == 22.9)
changed = {"id": 6, "name": "PLA", "extra": {"url_galaxus": json.dumps("https://www.galaxus.ch/de/s1/product/neu-55555555")}}
c_changed = pc.build_rows([{"filament": changed, "spools": [], "pct": 5, "remaining_g": 1, "initial_g": 1000}],
                          pc.get_cached_prices(), 0.94)[0]["cells"]["url_galaxus"]
check("URL geaendert -> kein alter Preis, wird abgefragt", c_changed["state"] == "pending" and "price" not in c_changed)
raw = {U6: {"url": U6, "status": "ok", "price": 20.0, "fetched_at": 100},
       U9: {"url": U9, "status": "ok", "price": 21.0, "fetched_at": 200}}
norm = pc._normalize_prices(raw)
check("Altbestand: auf Produkt-Schluessel abgebildet, neuester gewinnt", list(norm) == ["https://www.galaxus.ch/de/s1/product/40297740"]
      and norm["https://www.galaxus.ch/de/s1/product/40297740"]["price"] == 21.0)
rid5, urls5 = pc.start_round(f69 + rfils)
check("Runde: gleiche Produktnummer nur einmal (Gesamtzahl = Protokoll)", len(urls5) == 4
      and len(pc.round_protocol(rid5)["pages"]) == 4)

print()
print("ALLE TESTS OK" if ok else "FEHLER")
raise SystemExit(0 if ok else 1)
