# Fault Record Bridge – notes for Claude Code (Phase 2)

## What this is
A SICAM 8 SIAPP (Docker-based container app) that forwards SIPROTEC fault records
(COMTRADE/PQDIF/COMFEDE) and recorder events from a SICAM S8000 or A8000 CP-8050 to an EMS MQTT
broker. Phase 1 (code + PC tests) is done. Phase 2 = build with the SIAPP SDK, deploy, bench test.
Read `docs/DESIGN.md`, `docs/MQTT_INTERFACE.md`, `docs/BENCH_TEST_PLAN.md` first.

## Bench
- SICAM S8000 in Hyper-V (authorized), SICAM A8000 CP-8050, SIPROTEC 7SX85 (stand-in for 7KE85).
- SICAM Device Manager V26.20+ on the engineering PC; SICAM WEB shows SIAPP status and logs.
- Deploy to the S8000 first (`-p sws`, amd64), then CP-8050 (`-p a8000`, linux/arm/v7).

## Commands
- Tests: `python3 -m pytest -q tests` (needs mosquitto, paho-mqtt, pyftpdlib, pyopenssl).
- Build: `python siapp-sdk/build.py -t docker -p sws siapp/FaultRecordBridge` (SDK 2.1.8 tested).
- Emulate: `python siapp-sdk/run.py siapp/FaultRecordBridge -p sws` (uses discover.csv / events.csv).
- Validate config: `PYTHONPATH=siapp/FaultRecordBridge/app python3 -m frbridge --config X --check`.

## Constraints (from Siemens documents)
- A8000 SIAPP resources: ~300 MB RAM shared by all SIAPPs, 60 % CPU max per SIAPP, 1000 MB flash per
  SIAPP. Keep dependencies to stdlib + paho-mqtt (numpy/pandas exceeded limits in Siemens' experience).
- Image must be armhf for CP-8031/8050; Dockerfile is multi-arch via `python:3.12-alpine`.
- `/persist_data` survives SIAPP updates but not slot resize/recreate (A8000).
- Edge Data API: signals only (no file access). Library `/edgedata.so`, wrapper `/app/edgedataapi.py`.
- Never add SSH/debug services to production builds (SDK demos do – don't copy that).

## Open items to close on the bench (DESIGN.md §7)
1. Path where Device Manager delivers SIAPP parameters → add to `config.DM_PARAM_CANDIDATES`.
2. xConnect file-transfer request format → enable `ingest.http.capture`, inspect `/persist_data/frb/capture`.
3. Can xConnect reach the SIAPP VETH address? Else use FTPS (File Archive Access).
4. File Archive Access folder layout / zipped vs loose files.
5. xConnect licence level for COMTRADE (Advanced vs Standard).
6. IEEE C37.232 time-code format (template in `naming.template`).
7. Real 7KE85 record sizes vs PROTI5 ZIP limit and broker packet limit.

## Conventions
- Keep the MQTT contract in `docs/MQTT_INTERFACE.md` in sync with `publisher.py` / `bridge.py`.
- Every new parameter: add to `cmd/parameter_schema.json` AND `config.DM_PARAM_MAP` (`test_parameter_schema_matches_config_map` enforces it).
- Records are never modified; unacknowledged records are never deleted automatically.
