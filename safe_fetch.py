"""Abruf fremder Webseiten mit SSRF-Schutz (Issue #46).

Die Produkt-URL, die der Nutzer in filament_enrich.py einfuegt (HA-Feld oder
Web-Formular), wird server-seitig abgerufen. Ohne Schutz koennte darueber ein
interner Dienst (HA, Spoolman, Router, Cloud-Metadaten 169.254.169.254)
angesprochen werden. Dieses Modul erlaubt nur oeffentliche Ziele:

1. Vorab-Pruefung (check_url): Schema http/https, Host vorhanden, ALLE vom
   Hostnamen aufgeloesten Adressen muessen oeffentlich sein. Liefert schnelle,
   verstaendliche Fehlermeldungen.
2. Pruefung beim echten Verbindungsaufbau (_GuardMixin): nach dem TCP-Connect
   und VOR dem Senden der HTTP-Anfrage wird die tatsaechlich verbundene
   Gegenstelle geprueft. Das deckt DNS-Rebinding ab (der Name loest zwischen
   Pruefung und Verbindung auf eine andere Adresse auf) und JEDEN Redirect-
   Schritt, weil auch Weiterleitungen ueber denselben Adapter laufen. An ein
   verbotenes Ziel geht damit nie eine HTTP-Anfrage.

Grenzen: Proxys werden bewusst nicht unterstuetzt (trust_env=False) - die
Pruefung sieht nur den Proxy, nicht das Ziel dahinter. Redirects sind auf
max. 5 Schritte begrenzt, die Antwort auf max_bytes.
"""
import ipaddress
import socket
import threading
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3 import connection as _urllib3_connection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

MAX_REDIRECTS = 5
_CHUNK_BYTES = 16384
_ALLOWED_SCHEMES = ("http", "https")


class UnsafeURLError(ValueError):
    """Die URL zeigt auf kein oeffentliches Ziel (oder ist kein http(s))."""


def is_public_address(ip):
    """True nur fuer oeffentlich routbare Adressen. Abgelehnt werden private
    (RFC1918, fc00::/7), Loopback, Link-Local (169.254.0.0/16 inkl.
    Cloud-Metadaten, fe80::/10), CGNAT (100.64.0.0/10), reservierte,
    Multicast- und unspezifizierte Adressen. IPv4-mapped IPv6
    (::ffff:192.168.1.1) wird als die enthaltene IPv4 bewertet."""
    try:
        address = ipaddress.ip_address(str(ip).split("%")[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


def _blocked_message(target):
    return f"URL nicht erlaubt: zeigt auf eine interne oder lokale Adresse ({target})"


def check_url(url):
    """Vorab-Pruefung. Wirft UnsafeURLError. Nicht aufloesbare Hosts sind hier
    KEIN Fehler - der Abruf scheitert dann mit der normalen Netzwerkmeldung."""
    parts = urlsplit((url or "").strip())
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeURLError("URL nicht erlaubt: nur http(s) ist zulaessig")
    host = parts.hostname
    if not host:
        raise UnsafeURLError("URL nicht erlaubt: kein Host angegeben")
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return
    for info in infos:
        if not is_public_address(info[4][0]):
            raise UnsafeURLError(_blocked_message(f"{host} -> {info[4][0]}"))


# Merkt sich pro Thread, welche Adresse beim Verbindungsaufbau verboten war.
# urllib3/requests verpacken Fehler aus dem Verbindungsaufbau in eigene
# Ausnahmen - so bleibt die Ursache trotzdem eindeutig erkennbar.
_state = threading.local()


class _GuardMixin:
    def _new_conn(self):
        sock = super()._new_conn()
        peer = sock.getpeername()[0]
        if not is_public_address(peer):
            sock.close()
            _state.blocked = peer
            raise UnsafeURLError(_blocked_message(peer))
        return sock


class _GuardedHTTPConnection(_GuardMixin, _urllib3_connection.HTTPConnection):
    pass


class _GuardedHTTPSConnection(_GuardMixin, _urllib3_connection.HTTPSConnection):
    pass


class _GuardedHTTPPool(HTTPConnectionPool):
    ConnectionCls = _GuardedHTTPConnection


class _GuardedHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _GuardedHTTPSConnection


class _GuardedAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {"http": _GuardedHTTPPool, "https": _GuardedHTTPSPool}


def _new_session():
    session = requests.Session()
    session.trust_env = False
    session.max_redirects = MAX_REDIRECTS
    adapter = _GuardedAdapter()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def fetch_text(url, headers=None, timeout=20, max_bytes=5_000_000):
    """Laedt die Seite und gibt hoechstens max_bytes als Text zurueck.
    Wirft UnsafeURLError bei verbotenem Ziel (auch nach Redirect/DNS-Rebinding),
    sonst die ueblichen requests-Ausnahmen (HTTP-Fehler, Timeout, ...)."""
    check_url(url)
    _state.blocked = None
    session = _new_session()
    try:
        with session.get(url, headers=headers, timeout=timeout, stream=True) as response:
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=_CHUNK_BYTES):
                chunks.append(chunk)
                size += len(chunk)
                if size >= max_bytes:
                    break
            content = b"".join(chunks)[:max_bytes]
            return content.decode(response.encoding or "utf-8", errors="replace")
    except requests.RequestException:
        if _state.blocked:
            raise UnsafeURLError(_blocked_message(_state.blocked)) from None
        raise
    finally:
        session.close()
