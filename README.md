# Fault Record Bridge – SICAM 8 SIAPP

Sends SIPROTEC fault-recorder data (COMTRADE disturbance/transient records, PQDIF, COMFEDE logs),
"record made" events and recorder-health alarms from a **SICAM A8000 CP-8050** or **SICAM S8000**
to an EMS / control-center **MQTT broker** – the part the SICAM 8 MQTT Client cannot do
(it publishes signals only) and that SICAM GridEdge only does toward Electrification X.

Status: **Phase 1 complete** – code, tests and documentation, verified against a TLS Mosquitto
broker on a PC. Phase 2 (container build with the SIAPP SDK, deployment and bench tests on the
S8000 / CP-8050 / 7SX85) follows `docs/BENCH_TEST_PLAN.md`.

```
7KE85/7SX85 ─IEC 61850─► SICAM 8 (PROTI5 → File Archive) ─xConnect HTTPS / FTPS─► SIAPP ─MQTT/TLS─► broker ─► EMS
                         RTU IEC 61850 client → RcdMade / FltNum / health ─Edge Data API─┘
```

## Repository layout

| Path | Content |
|---|---|
| `siapp/FaultRecordBridge/` | SIAPP project – pass this folder to the SDK `build.py` |
| `  Dockerfile` | multi-stage build (`python:3.12-alpine`), works for `a8000` (arm/v7) and `sws` (amd64) |
| `  app/frbridge/` | the bridge (Python, stdlib + paho-mqtt) |
| `  cmd/parameter_schema.json` | 45 SIAPP parameters shown in SICAM Device Manager |
| `  config/frbridge.siapp.json` | defaults baked into the image |
| `  discover.csv`, `events.csv` | Edge Data API signals for the SDK emulator (`run.py`) |
| `  edgedataapi/`, `src/edgedataapi.py` | Edge Data API from SIAPP SDK 2.1.8 (MIT) |
| `tools/ems_receiver.py` | reference receiver for the EMS side: reassemble, verify, store, acknowledge, data requests |
| `tools/xconnect_sim.py` | simulates xConnect pushing a file (raw / multipart / JSON) |
| `tools/make_sample_comtrade.py` | synthetic COMTRADE 2013 record generator |
| `tools/gen_lab_certs.sh`, `tools/broker/` | lab PKI and Mosquitto broker (config, ACL, compose) |
| `tests/` | 18 tests: unit, FTPS ingest, end-to-end over TLS with ACLs, outage, resend |
| `docs/DESIGN.md` | architecture, decisions, failure behaviour, security, open items |
| `docs/MQTT_INTERFACE.md` | topic and payload contract for the EMS integrator |
| `docs/BENCH_TEST_PLAN.md` | Phase 2 build, Device Manager engineering and 15 bench tests |
| `docs/config.example.json` | full configuration example |

## Quick start on a PC (no SICAM hardware)

```bash
pip install paho-mqtt pytest pyftpdlib pyopenssl     # plus mosquitto (apt install mosquitto)
python3 -m pytest -q tests                            # 18 passed
```

Run the bridge by hand (folder ingest, lab broker):

```bash
export PYTHONPATH=siapp/FaultRecordBridge/app
python3 -m frbridge --config my-config.json --check   # validate
python3 -m frbridge --config my-config.json
python3 tools/make_sample_comtrade.py /path/to/inbox/test.zip
```

## Build the SIAPP (Phase 2)

```bash
git clone https://github.com/siemens/siapp-sdk.git
python siapp-sdk/build.py -t docker -p sws   siapp/FaultRecordBridge   # SICAM S8000
python siapp-sdk/build.py -t docker -p a8000 siapp/FaultRecordBridge   # CP-8031 / CP-8050
```

Upload the `.siapp` from `siapp-sdk/build` with SICAM Device Manager (SIAPP → SIAPPs), set the
parameters, upload to the device and check SICAM WEB → Applications.

## Configuration

Precedence: image defaults < JSON file (`FRB_CONFIG` or `/persist_data/frb/config.json`) <
Device Manager parameters < environment (`FRB__MQTT__HOST=…`). Main parameters:

| Parameter | Meaning |
|---|---|
| `MQTT_HOST`, `MQTT_PORT`, `MQTT_TLS`, `MQTT_USER`, `MQTT_PASS`, `MQTT_CA` | broker connection |
| `TOPIC_PREFIX`, `BRIDGE_ID`, `IED_MAP`, `DEFAULT_IED` | topic tree |
| `HTTP_*` | xConnect receiver (port 8443, basic auth / API key, optional HTTPS) |
| `FTPS_*` | File Archive Access pull |
| `EDGE_SIGNALS`, `OUT_*` | RTU input signals and status outputs |
| `REQUIRE_ACK`, `ACK_TIMEOUT_S`, `RETENTION_DAYS`, `MAX_STORE_MB`, `CHUNK_KB` | delivery and storage |

## Licences needed on the SICAM side

- IEC 61850 File Transfer Client (PROTI5): no licence.
- xConnect file transfer **with COMTRADE: xConnect Advanced (150 FP)** per SICAM 8 V26.20 training
  material (Standard, 120 FP, is "file transfer without COMTRADE") – confirm with licensing.
- or File Archive Access: licence for third-party FTPS clients.
- SIAPP Runtime (SIAR) licence; on S8000 also SICORE (+ RTU) licences.

## Important limits

- CP-8050: max 4 SICAM 8 applications (SIAPPs not counted), max 3 SIAPPs, 1000 MB flash per SIAPP
  (1500 MB total), ~300 MB RAM for all SIAPPs. S8000: 8 applications, 32 GB per SIAPP.
- PROTI5 ZIP size limit 1–10 MB per record file.
- `/persist_data` on the A8000 is erased when the SIAPP slot is resized or recreated.

This is engineering prototype software. Validate it on the bench before any use on a live system
(the SIAPP SDK makes the creator responsible for testing and validating a SIAPP).
