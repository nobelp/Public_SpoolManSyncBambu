# DIV_SpoolManSyncBambu

Automatic spool management and print history for Bambu Lab 3D printers. The service connects Home Assistant, Spoolman and the Bambu Lab integration.

**German guide:** [README.md](README.md)

---

## Features

- **Spool detection:** New spools in the AMS and the external spool holder are registered in Spoolman automatically.
- **Consumption tracking:** Filament usage is calculated after each print and deducted in Spoolman.
- **Location tracking:** The spool location in Spoolman follows the AMS slot.
- **Print history:** Web interface with statistics, images and filtering. Multi-colour prints are broken down per filament.
- **Manual entry:** Prints can be added afterwards, e.g. when Home Assistant was down.
- **Filament enrichment:** Take price, article number and metadata from a product URL into Spoolman.
- **Restocking:** Filaments with low remaining stock and a price comparison.

---

## Requirements

- Docker and Docker Compose
- **Home Assistant** with the **Bambu Lab integration**. The service reads the printer entities through the Home Assistant API.
- **Spoolman** (your own instance, not installed by this project)
- A Bambu Lab printer with AMS, integrated into Home Assistant

---

## Installation

```bash
git clone https://github.com/nobelp/Public_SpoolManSyncBambu.git
cd Public_SpoolManSyncBambu

# Create the configuration
cp .env.example .env
```

Set at least the following in `.env`:

| Variable | Meaning |
|---|---|
| `HA_URL` | Address of Home Assistant, e.g. `http://homeassistant.local:8123` |
| `HA_TOKEN` | Long-lived access token from Home Assistant (profile → security) |
| `SPOOLMAN_URL` | Address of your Spoolman instance, e.g. `http://spoolman.local:7912` |
| `PRINTER_ENTITY_PREFIX` | Common prefix of the printer entities, e.g. `sensor.p2s_<serial>` |

You can find the entity names with `discover_entities.py`. The command is read-only and changes nothing. It prints ready-made lines for `.env` (the prefix and all printer entities); copy **all** of them into your `.env`. Without these lines only placeholder names apply:

```bash
docker exec DIV_SpoolManSyncBambu python3 discover_entities.py
```

All other variables are optional. Their defaults are listed as comments in `.env.example`.

### Start

```bash
docker compose up -d --build

# Logs
docker logs -f DIV_SpoolManSyncBambu
docker logs -f DIV_SpoolManSyncBambu_Consumption
```

### Services and ports

| Service | Script | Port | Task |
|---|---|---|---|
| Spoolman sync | `sync.py` | – | Detects new spools, registers them, tracks locations |
| Consumption tracking | `consumption.py` | 8100 | Reduces remaining quantities, print history web interface |
| Filament enrichment | `filament_enrich.py` | 8099 | Product import, metadata filling, form |

The interface is available at `http://<host>:8100`.

---

## Security

- The web interfaces have **no login**. Do not expose ports 8099 and 8100 to the internet; run them on your local network only.
- `.env` contains credentials and must never be committed. It is listed in `.gitignore`.
- The `data/` directory contains history and tokens and is not versioned either.

---

## Tests

The tests need no Docker, no Home Assistant and no Spoolman instance:

```bash
pip install -r requirements.txt
bash tests/run_all.sh
```

---

## Troubleshooting

- **No spools are detected:** Check the entity names (`discover_entities.py`) and the logs of `DIV_SpoolManSyncBambu`.
- **Consumption is wrong:** Increase the log level of consumption tracking and check the logs of `DIV_SpoolManSyncBambu_Consumption`.
- **Printer sensors are `unavailable`:** Check the Bambu Lab integration in Home Assistant. Short outages do not discard slot assignments.
- **Home Assistant returns 401:** `HA_TOKEN` is invalid or expired.

---

## License

MIT, see [LICENSE](LICENSE).
