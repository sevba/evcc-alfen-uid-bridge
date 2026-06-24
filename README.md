# evcc-alfen-uid-bridge

Deterministic vehicle identification for EVCC when charging via an Alfen Single Pro-line.

EVCC controls the charger over Modbus (EMS mode) and has no visibility of which RFID card started the session. This bridge reads the card UID from the Alfen's local HTTPS API and tells EVCC which vehicle to assign to the loadpoint.

## How it works

```
Car plugged in + RFID tap
        │
        ▼
Alfen logs NFC tag in device log
        │
EVCC sees car connected → publishes MQTT connected=true
        │
        ▼
Bridge reads Alfen log → extracts UID → maps to EVCC vehicle name
        │
        ▼
Bridge calls EVCC REST API: POST /api/loadpoints/{id}/vehicle/{name}
```

On disconnect, bridge calls `DELETE /api/loadpoints/{id}/vehicle` to revert to automatic detection.

## Important: single-session constraint

The Alfen allows only **one** management API session at a time. The bridge holds the session only briefly per connect event. **Do not run the bridge alongside MyEve, ACE Service Installer, or the Home Assistant Alfen integration** — they share the same session slot. Between charging sessions the session is free.

## Setup

### 1. Discover your card UIDs

Run with plaintext UID logging enabled:

```
LOG_LEVEL=DEBUG LOG_UID_PLAINTEXT=true DRY_RUN=true docker compose up
```

Tap your RFID card at the charger and watch the logs. The bridge will log lines like:

```
alfen: tag candidate uid=041CF6BAC01690 lid=631424
```

Full UIDs appear in two log patterns:
- `Reader 0 Got NFC tag: {UID}` — appears on first/online-auth tap
- `Tag {UID} is authorised by server` — appears on whitelist updates

If only a short/truncated UID appears (e.g. `5B9F` instead of `5B9F4379`), check the OCPP transaction log in the charger's web UI (`https://10.0.40.66`) for the full UID, or look for a `Tag X is authorised` line from a previous session.

### 2. Configure the UID map

In `.env`, set `UID_VEHICLE_MAP` to a JSON object mapping each card UID to the EVCC vehicle name:

```
UID_VEHICLE_MAP={"041CF6BAC01690": "bmw320e", "04F6E5D4C3B2": "q6"}
```

UIDs are matched case-insensitively with separators stripped (`04:A1:B2` = `04A1B2`).

### 3. First run in dry-run mode

```
DRY_RUN=true docker compose up
```

Check that the bridge correctly identifies the card and logs the intended EVCC action without executing it.

### 4. Production run

```
docker compose up -d
```

## Configuration

All settings via environment variables. See `.env.example` for the full list.

| Key | Default | Description |
|-----|---------|-------------|
| `ALFEN_HOST` | — | Charger IP (e.g. `10.0.40.66`) |
| `ALFEN_PASSWORD` | — | Admin password |
| `EVCC_BASE_URL` | — | EVCC REST base URL |
| `EVCC_LOADPOINT_ID` | — | Numeric loadpoint ID |
| `MQTT_HOST` | — | MQTT broker host |
| `UID_VEHICLE_MAP` | — | JSON: UID → EVCC vehicle name |
| `TAG_WAIT_TIMEOUT_S` | `15` | Max seconds to wait for a tag after connect |
| `RELEASE_ON_DISCONNECT` | `true` | Clear vehicle on disconnect |
| `ON_UNKNOWN_TAG` | `auto` | `auto` = leave EVCC on auto-detection; `default` = set `DEFAULT_VEHICLE` |
| `DRY_RUN` | `false` | Log actions without executing them |
| `LOG_UID_PLAINTEXT` | `false` | Log full UIDs (personal data — discovery only) |

## Troubleshooting

**Bridge logs `401` from Alfen**: Connection reuse failed. Another client (MyEve, HA integration) is holding the session. Stop the other client and restart the bridge.

**Bridge logs `no RFID tag found within Xs window`**: The card UID doesn't appear in the patterns the bridge searches. Run with `LOG_UID_PLAINTEXT=true` at DEBUG to see raw log lines. Check whether your firmware logs a different pattern.

**EVCC still shows wrong vehicle after a tap**: Check `DRY_RUN=false` and that the UID in `UID_VEHICLE_MAP` matches exactly (check normalisation: strip colons, uppercase).

**Rate limit warning**: Another process is triggering rapid logins. The bridge self-limits to 5 logins/60s.

## Running tests

```bash
pip install -r requirements.txt
pytest tests/
```

## Network requirements

The container needs reachability to:
- `10.0.40.66:443` — Alfen HTTPS API (IoT VLAN)
- `127.0.0.1:7070` — EVCC REST API
- `127.0.0.1:1883` — MQTT broker
