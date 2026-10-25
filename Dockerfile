FROM python:3.11-slim

RUN apt-get update -q && \
    apt-get install -y tzdata curl git --no-install-recommends -q && \
    ln -sf /usr/share/zoneinfo/Europe/Zurich /etc/localtime && \
    echo 'Europe/Zurich' > /etc/timezone && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# REIHENFOLGE IST BEWUSST: von "aendert sich selten" nach "aendert sich immer".
# Jede COPY-Zeile invalidiert den Docker-Cache fuer ALLE folgenden Schichten.
# Die Git-Info (.version/.commit/.timestamp) wird vom deploy_to_nas.sh bei JEDEM
# Deploy neu erzeugt - stand sie vor dem pip install, wurden die Abhaengigkeiten
# bei jedem Deploy komplett neu installiert (~45s auf der NAS ohne Nutzen).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY entrypoint-sync.sh entrypoint-consumption.sh entrypoint-filament-enrich.sh ./
RUN chmod +x /app/entrypoint-*.sh && mkdir -p /app/data /app/logs

COPY sync.py consumption.py reconcile_locations.py filament_enrich.py history_store.py price_check.py plate_types.py title_image.py shop_links.py bambu_cloud.py safe_fetch.py entity_config.py discover_entities.py bambu_colors.json ./

# Zuletzt: Release-Version aus VERSION und die optionale Git-Info (vom Deploy-Script
# erzeugt, im Export fehlt sie). Die Wildcards brauchen mindestens einen Treffer -
# VERSION ist immer da. Wird zur Laufzeit als Versions-Anzeige gelesen
# (siehe _get_git_info()); VERSION hat dort Vorrang vor .version.
COPY VERSION .version* .commit* .timestamp* ./

# Standard: Registrierungs-Job (mit Auto-Init). Wird in docker-compose je Service via entrypoint überschrieben.
ENTRYPOINT ["/app/entrypoint-sync.sh"]
