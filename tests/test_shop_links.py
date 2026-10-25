"""Prueft die automatische Ergaenzung der Shop-Links Bambu Lab / CCTREE ueber
die ColorID (Issue #35) - ohne Netzwerk, mit nachgebauten Shop-Seiten."""
import json
import os
import tempfile

os.environ.setdefault("SHOP_LINKS_STATE_FILE", os.path.join(tempfile.mkdtemp(), "shop_links_state.json"))

import consumption as c
import shop_links as sl

sl.PAGE_DELAY_S = 0
ok = True


def check(label, cond):
    global ok
    print(("  PASS  " if cond else "  FAIL  ") + label)
    ok = ok and cond


def ld(obj):
    return f'<script type="application/ld+json">{json.dumps(obj)}</script>'


def variant(vid, name, price):
    return {"@type": "Product", "sku": str(vid), "name": name,
            "offers": {"@type": "Offer", "price": price, "priceCurrency": "EUR",
                       "url": f"https://eu.store.bambulab.com/products/pla-basic-filament?id={vid}"}}


BAMBU_BASIC = ld({"@graph": [
    variant(111, "PLA Basic - Schwarz (10101) / Filament mit Spule / 1 kg", 19.99),
    variant(222, "PLA Basic - Schwarz (10101) / Nachfüllung / 1 kg", 16.99),
    variant(333, "PLA Basic - Grau (10103) / Filament mit Spule / 1 kg", 19.99),
]})
STORE = '<html>"B0AAAAAAA1" "B0BBBBBBB2"</html>'
DIMS = ('<script>var x = {"dimensionValuesDisplayData" : {"B0AAAAAAA1":["Schwarz 10101"],'
        '"B0CCCCCCC3":["Grau 10103"]}, "other": 1};</script>')


def amazon_product(title, price="18,73CHF"):
    return (f'<span id="productTitle"> {title} </span>'
            f'<div id="corePrice_feature_div"><span class="a-offscreen">{price}</span></div>{DIMS}')


PAGES = {
    sl.BAMBU_BASE + "pla-basic-filament": BAMBU_BASIC,
    sl.CCTREE_STORE_URL: STORE,
    "https://www.amazon.de/dp/B0AAAAAAA1": amazon_product("CCTREE Original PLA Basic Refill, Schwarz 10101"),
    "https://www.amazon.de/dp/B0BBBBBBB2": amazon_product("CCTREE Original PLA Basic Refill, Schwarz 10101"),
    "https://www.amazon.de/dp/B0CCCCCCC3": amazon_product("CCTREE Original PLA Basic Refill, Grau 10103"),
}
fetched = []


def fake_fetch(url):
    fetched.append(url)
    if url in PAGES:
        return PAGES[url]
    raise sl.PageBlocked("nicht vorhanden") if "amazon" in url else Exception("404")


print("1) Bambu Lab: Variante per ColorID, Nachfuellung bevorzugt")
v = sl.parse_bambu_variants(BAMBU_BASIC)
check("3 Varianten mit ColorID erkannt", [x[0] for x in v] == ["10101", "10101", "10103"])
found = sl.find_bambu_links(["10101", "10103", "99999"], fetch=fake_fetch)
check("10101 -> Nachfuellung (?id=222, /de/-Link)", found["10101"]["url"].endswith("/de/products/pla-basic-filament?id=222")
      and found["10101"]["price"] == 16.99)
check("10103 -> nur Spule vorhanden", found["10103"]["url"].endswith("?id=333"))
check("99999 -> kein Treffer", "99999" not in found)

print("2) CCTREE: ASIN per Farbcode, Titel+Preis geprueft")
check("Varianten aus dimensionValuesDisplayData", sl.parse_cctree_variants(DIMS) == {"B0AAAAAAA1": "Schwarz 10101", "B0CCCCCCC3": "Grau 10103"})
fetched.clear()
cc = sl.find_cctree_links(["10101", "10103"], fetch=fake_fetch)
check("10101 und 10103 gefunden", cc["10101"]["url"] == "https://www.amazon.de/dp/B0AAAAAAA1"
      and cc["10103"]["url"] == "https://www.amazon.de/dp/B0CCCCCCC3" and cc["10103"]["price"] == 18.73)
check("Varianten einer Seite nicht erneut geladen (B0BBBBBBB2 uebersprungen)", "https://www.amazon.de/dp/B0BBBBBBB2" not in fetched)
PAGES["https://www.amazon.de/dp/B0CCCCCCC3"] = amazon_product("Anderer Hersteller PLA, Grau 10103")
check("fremder Titel wird abgelehnt", "10103" not in sl.find_cctree_links(["10103"], fetch=fake_fetch))
PAGES["https://www.amazon.de/dp/B0CCCCCCC3"] = amazon_product("CCTREE Original PLA Basic Refill, Grau 10103")

print("3) Lauf: ergaenzen, nie ueberschreiben, 'kein Angebot' merken")
filaments = [
    {"id": 1, "name": "Basic Black", "extra": {"colorid": "10101"}},
    {"id": 2, "name": "Basic Gray", "extra": {"colorid": "10103", "url_bambulab": json.dumps("https://eu.store.bambulab.com/x?id=1")}},
    {"id": 3, "name": "Silk", "extra": {"colorid": "13909"}},
    {"id": 4, "name": "Fremdmarke", "extra": {}},
]
written = []


def write_link(fid, field, url):
    written.append((fid, field, url))
    return True


fetched.clear()
summary = sl.run(filaments, write_link, fetch_bambu=fake_fetch, fetch_amazon=fake_fetch, now=1_000_000)
check("#1 bekommt Bambu Lab und CCTREE", (1, "url_bambulab", found["10101"]["url"]) in written
      and (1, "url_cctree", "https://www.amazon.de/dp/B0AAAAAAA1") in written)
check("#2: von Hand gesetzter Bambu-Link bleibt, CCTREE ergaenzt", not any(w[0] == 2 and w[1] == "url_bambulab" for w in written)
      and (2, "url_cctree", "https://www.amazon.de/dp/B0CCCCCCC3") in written)
check("#4 ohne ColorID ignoriert", not any(w[0] == 4 for w in written))
check("Zusammenfassung", summary["filled"] == 3 and summary["not_offered"] == 2 and summary["blocked"] == [])
state = sl.load_state()
check("13909 als 'kein Angebot' gemerkt (beide Shops)", "13909" in state["missing"]["url_bambulab"]
      and "13909" in state["missing"]["url_cctree"])
check("Ereignisse protokolliert", len(state["events"]) == 3)

print("4) Kein Dauer-Abfragen, Wiederholung nach 7 Tagen")
filaments[0]["extra"].update(url_bambulab='"x"', url_cctree='"y"')
filaments[1]["extra"]["url_cctree"] = '"z"'
fetched.clear(); written.clear()
sl.run(filaments, write_link, fetch_bambu=fake_fetch, fetch_amazon=fake_fetch, now=1_000_000 + 3600)
check("innerhalb 7 Tagen: kein Abruf fuer 13909", not fetched and not written)
fetched.clear()
sl.run(filaments, write_link, fetch_bambu=fake_fetch, fetch_amazon=fake_fetch, now=1_000_000 + sl.MISSING_RETRY_S + 1)
check("nach 7 Tagen: erneut gesucht", any("bambulab" in u for u in fetched))

print("5) Amazon blockiert: nichts falsch merken")
os.remove(sl.STATE_FILE)
blocked_fetch = lambda url: (_ for _ in ()).throw(sl.PageBlocked("Bot-Schutz"))
filaments2 = [{"id": 5, "name": "Neu", "extra": {"colorid": "10101"}}]
s2 = sl.run(filaments2, write_link, fetch_bambu=fake_fetch, fetch_amazon=blocked_fetch, now=2_000_000)
check("CCTREE als 'nicht erreichbar' gemeldet", s2["blocked"] == ["CCTREE"])
check("nicht als 'kein Angebot' gemerkt", "10101" not in sl.load_state().get("missing", {}).get("url_cctree", {}))
check("offene Felder gezaehlt", s2["open"] == 1)

print("6) Schreiben in Spoolman: Felder zusammenfuehren, von Hand gesetzte behalten")
store = {7: {"id": 7, "extra": {"colorid": '"10101"', "orderurl": '"https://shop/x"'}},
         8: {"id": 8, "extra": {"colorid": '"10103"', "url_bambulab": '"https://von-hand"'}}}
patched = []
c.sm_get = lambda path, params=None: store[int(path.rsplit("/", 1)[1])]
c.sm_patch = lambda path, data: patched.append((path, data))
check("leeres Feld wird geschrieben", c._write_shop_link(7, "url_bambulab", "https://eu.store.bambulab.com/y?id=9"))
check("andere Zusatzfelder bleiben erhalten", patched[-1][1]["extra"]["orderurl"] == '"https://shop/x"'
      and patched[-1][1]["extra"]["url_bambulab"] == json.dumps("https://eu.store.bambulab.com/y?id=9"))
n = len(patched)
check("inzwischen von Hand gesetzt -> nicht ueberschrieben", not c._write_shop_link(8, "url_bambulab", "https://auto") and len(patched) == n)

print("7) Uebersicht")
html = sl.render_overview([{"id": 1, "name": "Black", "extra": {"colorid": "10101", "url_bambulab": '"https://b"'}},
                           {"id": 3, "name": "Silk", "extra": {"colorid": "13909"}}],
                          {"missing": {"url_cctree": {"13909": 2_000_000}}, "events": [], "last_run": s2})
check("zeigt vorhanden / offen / kein Angebot", "vorhanden" in html and "offen" in html and "kein Angebot" in html
      and "Jetzt ergänzen" in html)

print()
print("ALLE TESTS OK" if ok else "FEHLER")
raise SystemExit(0 if ok else 1)
