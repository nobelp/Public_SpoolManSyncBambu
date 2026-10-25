"""Tests fuer den SSRF-Schutz beim Seitenabruf (Issue #46, safe_fetch.py).

Ohne Internet: Ziele sind lokale Test-Server auf 127.0.0.1/127.0.0.2 (beides
Loopback). Wo ein "oeffentliches" Ziel noetig ist, wird is_public_address()
im Test gezielt umgebogen. Der Kernbeweis: an ein verbotenes Ziel geht nie
eine HTTP-Anfrage (der Test-Server zaehlt seine Anfragen)."""
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

import safe_fetch

ok = True


def check(label, cond, detail=""):
    global ok
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  → {detail}" if detail and not cond else ""))
    ok = ok and bool(cond)


# ── Test-Server ──────────────────────────────────────────────────────
class _Server:
    def __init__(self, host, handler_factory):
        self.hits = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits.append(self.path)
                handler_factory(self)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer((host, 0), Handler)
        self.port = self.httpd.server_address[1]
        self.host = host
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path="/"):
        return f"http://{self.host}:{self.port}{path}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def ok_page(handler, body=b"<html><title>Testseite</title></html>"):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def raises(exc, func, *args, **kwargs):
    try:
        func(*args, **kwargs)
    except exc as e:
        return e
    except Exception as e:
        return f"falsche Ausnahme: {type(e).__name__}: {e}"
    return None


real_is_public = safe_fetch.is_public_address

# ── 1 ────────────────────────────────────────────────────────────────
print("1) is_public_address: verbotene und erlaubte Adressen")
FORBIDDEN = {
    "127.0.0.1": "Loopback", "127.255.255.254": "Loopback", "::1": "Loopback IPv6",
    "10.0.0.5": "RFC1918 10/8", "172.16.0.1": "RFC1918 172.16/12", "172.31.255.255": "RFC1918 Ende",
    "192.168.1.100": "RFC1918 192.168/16 (HA/Spoolman/NAS)", "169.254.169.254": "Link-Local/Cloud-Metadaten",
    "fe80::1": "Link-Local IPv6", "fe80::1%eth0": "Link-Local mit Scope", "fc00::1": "ULA IPv6", "fd12:3456::1": "ULA IPv6",
    "100.64.0.1": "CGNAT", "0.0.0.0": "unspezifiziert", "::": "unspezifiziert IPv6",
    "224.0.0.1": "Multicast", "255.255.255.255": "Broadcast", "240.0.0.1": "reserviert",
    "::ffff:192.168.1.1": "IPv4-mapped privat", "::ffff:127.0.0.1": "IPv4-mapped Loopback",
    "192.0.2.1": "Dokumentationsnetz", "nicht-eine-ip": "kein IP-Format",
}
for ip, why in FORBIDDEN.items():
    check(f"abgelehnt: {ip} ({why})", not real_is_public(ip))
ALLOWED = ["93.184.216.34", "8.8.8.8", "1.1.1.1", "172.15.0.1", "172.32.0.1", "11.0.0.1", "2606:4700:4700::1111", "::ffff:8.8.8.8"]
for ip in ALLOWED:
    check(f"erlaubt: {ip}", real_is_public(ip))

# ── 2 ────────────────────────────────────────────────────────────────
print("2) check_url: Schema, Host, Aufloesung")
for bad, why in [("file:///etc/passwd", "file"), ("ftp://example.com/x", "ftp"), ("gopher://example.com", "gopher"),
                 ("javascript:alert(1)", "javascript"), ("//example.com/x", "ohne Schema"), ("", "leer"),
                 ("http://", "ohne Host"), ("https:///pfad", "ohne Host (https)")]:
    check(f"abgelehnt: {why}", isinstance(raises(safe_fetch.UnsafeURLError, safe_fetch.check_url, bad), safe_fetch.UnsafeURLError), bad)
for url, why in [
    ("http://127.0.0.1/", "Loopback-IP"), ("http://localhost/", "localhost (aufgeloest)"),
    ("http://192.168.1.100:8123/api/config", "HA im LAN"), ("http://192.168.1.100:7912/api/v1/spool", "Spoolman im LAN"),
    ("http://169.254.169.254/latest/meta-data/", "Cloud-Metadaten"), ("http://[::1]:8080/", "IPv6-Loopback"),
    ("http://2130706433/", "dezimal kodierte 127.0.0.1"), ("http://0x7f000001/", "hex kodierte 127.0.0.1"),
    ("http://0177.0.0.1/", "oktal kodierte 127.0.0.1"), ("http://user:pw@127.0.0.1/", "Benutzerinfo-Trick"),
    ("http://evil.example@192.168.1.100/", "Benutzerinfo-Trick mit Name"), ("http://[::ffff:192.168.1.1]/", "IPv4-mapped"),
]:
    err = raises(safe_fetch.UnsafeURLError, safe_fetch.check_url, url)
    check(f"abgelehnt: {why}", isinstance(err, safe_fetch.UnsafeURLError), f"{url} -> {err}")

real_getaddrinfo = socket.getaddrinfo


def fake_getaddrinfo(table):
    def _inner(host, port, *args, **kwargs):
        if host in table:
            return [(socket.AF_INET if ":" not in ip else socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in table[host]]
        return real_getaddrinfo(host, port, *args, **kwargs)
    return _inner


socket.getaddrinfo = fake_getaddrinfo({
    "shop.test": ["93.184.216.34"], "mixed.test": ["93.184.216.34", "10.0.0.7"],
    "internal.test": ["192.168.1.100"], "v6.test": ["2606:4700:4700::1111"],
})
check("erlaubt: Hostname mit oeffentlicher Adresse", safe_fetch.check_url("https://shop.test/produkt") is None)
check("erlaubt: Hostname mit oeffentlicher IPv6", safe_fetch.check_url("https://v6.test/produkt") is None)
check("abgelehnt: Hostname -> interne Adresse", isinstance(raises(safe_fetch.UnsafeURLError, safe_fetch.check_url, "http://internal.test/"), safe_fetch.UnsafeURLError))
err = raises(safe_fetch.UnsafeURLError, safe_fetch.check_url, "http://mixed.test/")
check("abgelehnt: Hostname mit oeffentlicher UND interner Adresse (jede Adresse zaehlt)", isinstance(err, safe_fetch.UnsafeURLError), str(err))
check("Meldung nennt die Ursache (Host und Adresse)", isinstance(err, safe_fetch.UnsafeURLError) and "10.0.0.7" in str(err), str(err))
socket.getaddrinfo = real_getaddrinfo
check("nicht aufloesbarer Host ist hier kein Sicherheitsfehler (normale Netzwerkmeldung folgt)",
      safe_fetch.check_url("http://dieser-host-existiert-nicht.invalid/") is None)

# ── 3 ────────────────────────────────────────────────────────────────
print("3) Echte Abrufe: verbotene Ziele werden NIE angefragt")
server = _Server("127.0.0.1", ok_page)
err = raises(safe_fetch.UnsafeURLError, safe_fetch.fetch_text, server.url("/geheim"))
check("Loopback-Server: UnsafeURLError", isinstance(err, safe_fetch.UnsafeURLError), str(err))
check("... und der Server hat KEINE Anfrage erhalten", server.hits == [], str(server.hits))
server.stop()

# ── 4 ────────────────────────────────────────────────────────────────
print("4) DNS-Rebinding: Pruefung sieht eine oeffentliche Adresse, die Verbindung geht nach 127.0.0.1")
server = _Server("127.0.0.1", ok_page)
calls = {"n": 0}


def rebinding_getaddrinfo(host, port, *args, **kwargs):
    if host == "rebind.test":
        calls["n"] += 1
        # 1. Aufruf (check_url): oeffentlich; danach (Verbindungsaufbau): Loopback
        ip = "93.184.216.34" if calls["n"] == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
    return real_getaddrinfo(host, port, *args, **kwargs)


socket.getaddrinfo = rebinding_getaddrinfo
err = raises(safe_fetch.UnsafeURLError, safe_fetch.fetch_text, f"http://rebind.test:{server.port}/")
socket.getaddrinfo = real_getaddrinfo
check("Rebinding wird beim Verbindungsaufbau erkannt", isinstance(err, safe_fetch.UnsafeURLError), str(err))
check("... Vorab-Pruefung wurde tatsaechlich ausgetrickst (Test aussagekraeftig)", calls["n"] >= 2, str(calls))
check("... und der Server hat KEINE HTTP-Anfrage erhalten", server.hits == [], str(server.hits))
server.stop()

# ── 5 ────────────────────────────────────────────────────────────────
print("5) Redirect auf ein internes Ziel (erster Schritt oeffentlich, zweiter nicht)")
target = _Server("127.0.0.2", ok_page)                           # "intern"
first = _Server("127.0.0.1", lambda h: (h.send_response(302), h.send_header("Location", target.url("/intern")),
                                         h.send_header("Content-Length", "0"), h.end_headers()))
safe_fetch.is_public_address = lambda ip: ip == "127.0.0.1"      # nur der erste Server gilt als oeffentlich
err = raises(safe_fetch.UnsafeURLError, safe_fetch.fetch_text, first.url("/start"))
check("Redirect auf 127.0.0.2 wird blockiert", isinstance(err, safe_fetch.UnsafeURLError), str(err))
check("... der erste Schritt wurde abgerufen", first.hits == ["/start"], str(first.hits))
check("... am internen Ziel kam KEINE HTTP-Anfrage an", target.hits == [], str(target.hits))
first.stop()
target.stop()

# ── 6 ────────────────────────────────────────────────────────────────
print("6) Legitime Abrufe (Ziel gilt als oeffentlich): Inhalt, Redirects, Grenzen")
safe_fetch.is_public_address = lambda ip: ip == "127.0.0.1"
page = _Server("127.0.0.1", lambda h: ok_page(h, "<html><title>Größe ä ö ü €</title></html>".encode("utf-8")))
text = safe_fetch.fetch_text(page.url("/produkt"), headers={"User-Agent": "Test"})
check("Seite wird geladen und als UTF-8 dekodiert", "Größe ä ö ü €" in text, text)
page.stop()

dest = _Server("127.0.0.1", ok_page)
redirector = _Server("127.0.0.1", lambda h: (h.send_response(301), h.send_header("Location", dest.url("/ziel")),
                                              h.send_header("Content-Length", "0"), h.end_headers()))
text = safe_fetch.fetch_text(redirector.url("/kurzlink"))
check("Redirect auf ein erlaubtes Ziel wird gefolgt (z.B. Amazon-Kurzlinks)", "Testseite" in text and dest.hits == ["/ziel"], f"{text!r} {dest.hits}")
redirector.stop()
dest.stop()

loop = _Server("127.0.0.1", lambda h: (h.send_response(302), h.send_header("Location", "/weiter"), h.send_header("Content-Length", "0"), h.end_headers()))
err = raises(requests.RequestException, safe_fetch.fetch_text, loop.url("/a"))
check(f"Endlos-Redirect bricht nach {safe_fetch.MAX_REDIRECTS} Schritten ab", isinstance(err, requests.RequestException), str(err))
check("... mit begrenzter Zahl Anfragen", len(loop.hits) <= safe_fetch.MAX_REDIRECTS + 2, str(len(loop.hits)))
loop.stop()

big = _Server("127.0.0.1", lambda h: ok_page(h, b"A" * 2_000_000))
text = safe_fetch.fetch_text(big.url("/gross"), max_bytes=100_000)
check("Antwort wird auf max_bytes begrenzt", len(text) == 100_000, str(len(text)))
big.stop()

notfound = _Server("127.0.0.1", lambda h: (h.send_response(404), h.send_header("Content-Length", "0"), h.end_headers()))
err = raises(requests.HTTPError, safe_fetch.fetch_text, notfound.url("/weg"))
check("HTTP-Fehler bleibt ein normaler Abruffehler (kein Sicherheitsfehler)",
      isinstance(err, requests.HTTPError) and not isinstance(err, safe_fetch.UnsafeURLError), str(err))
notfound.stop()

dead_port = socket.socket()
dead_port.bind(("127.0.0.1", 0))
port = dead_port.getsockname()[1]
dead_port.close()
err = raises(requests.RequestException, safe_fetch.fetch_text, f"http://127.0.0.1:{port}/")
check("nicht erreichbares Ziel: normale Verbindungsstoerung", isinstance(err, requests.ConnectionError) and not isinstance(err, safe_fetch.UnsafeURLError), str(err))

# ── 7 ────────────────────────────────────────────────────────────────
print("7) Proxy-Umgebung wird ignoriert (die Pruefung koennte nicht hinter den Proxy sehen)")
import os
os.environ["HTTP_PROXY"] = "http://192.168.1.1:3128"
page = _Server("127.0.0.1", ok_page)
text = safe_fetch.fetch_text(page.url("/"))
check("Abruf geht direkt, nicht ueber den Proxy", "Testseite" in text and page.hits == ["/"], f"{text!r} {page.hits}")
del os.environ["HTTP_PROXY"]
page.stop()

safe_fetch.is_public_address = real_is_public

print("\nERGEBNIS:", "ALLE TESTS PASS" if ok else "FEHLER")
raise SystemExit(0 if ok else 1)
