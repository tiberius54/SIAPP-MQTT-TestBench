# Fault Record Bridge – Bench Test Plan (Phase 2)

Bench: SICAM S8000 (Hyper-V VM, authorized configuration), SICAM A8000 CP-8050, SIPROTEC 7SX85,
engineering PC with SICAM Device Manager V26.20+, a lab VM/PC with Docker or Podman.
Run the S8000 first (x64, 32 GB per SIAPP, easier debugging), then repeat on the CP-8050.

## 0. Build machine

1. Clone the SDK next to this repo: `git clone https://github.com/siemens/siapp-sdk.git ../siapp-sdk`
   (tested against 2.1.8). No copying needed; run the commands below from the repo root.
2. Linux: install QEMU binfmt for the arm/v7 build (see SDK README). Podman inside a Hyper-V VM needs
   `Set-VMProcessor -VMName <VM> -ExposeVirtualizationExtensions $true`. Windows: Docker Desktop.
3. Build (use `python` on Windows, where `python3` may be the Store stub):
   - S8000: `python ../siapp-sdk/build.py -t docker -p sws -v 0.1.0 siapp/FaultRecordBridge`
   - CP-8050: `python ../siapp-sdk/build.py -t docker -p a8000 -v 0.1.0 siapp/FaultRecordBridge`
   The package lands in `build/faultrecordbridge-<version>/`. Note the **recommended SIAPP slot size**
   printed by the build. It covers the image only, so add the `/persist_data` store (`MAX_STORE_MB`).
   Recorded: sws v0.1.0 = 49 MB rootfs, 100 MB recommended slot, 16 MB `.siapp`.
4. Optional emulation: `python ../siapp-sdk/run.py siapp/FaultRecordBridge -p sws` (uses `discover.csv` / `events.csv`).

## 1. Lab broker and EMS stand-in

1. `tools/gen_lab_certs.sh ./certs <broker-ip> <siapp-veth-ip>`
2. Create `tools/broker/passwd` for users `frb01` and `ems` (see `docker-compose.yaml` header), `docker compose up -d`.
3. Start the reference receiver:
   `python3 tools/ems_receiver.py --host <broker-ip> --cafile certs/ca.pem --user ems --password <pw> --prefix utility/site01 --out ./received`
4. MQTTX (as in the SICAM A8000 MQTTX test document) subscribed to `utility/site01/#` is useful to watch traffic.

## 2. SICAM 8 engineering (S8000 first)

| Step | Where (Device Manager) | Setting |
|---|---|---|
| 2.1 | 7SX85 (DIGSI 5) | IEC 61850 file service enabled; date format DD.MM.YYYY; fault recorder configured |
| 2.2 | Application configuration and licensing → Station configuration | 7SX85 station, IP |
| 2.3 | IEC 61850 File Transfer Client (PROTI5) | LAN interface, polling interval (60 s for test), ZIP size limit (start 10 MB), number of files per category |
| 2.4 | RTU IEC 61850 client | map `RDRE.RcdMade`, `RDRE.FltNum` and a device-healthy indication to signals `DFR01_RcdMade`, `DFR01_FltNum`, `DFR01_Health` |
| 2.5 | Path A – xConnect | xConnect (Advanced licence for COMTRADE), file transfer of fault records to `http(s)://<siapp-veth-ip>:8443/upload`, basic auth `xconnect` / `<pw>` |
| 2.5 | Path B – File Archive Access | licence for third-party FTPS clients; LAN interface; allow the SIAPP IP; user `ftp` + password; file list structure *Topology (multiple folders)* |
| 2.6 | Communication → LAN interface | *Internal virtual Ethernet* VETH0; LAN interface on VETH0 (platform side IP) |
| 2.7 | Firewall | TCP 8443 on VETH0 (xConnect → SIAPP); ICMP on VETH0 for ping; outbound to broker 8883 |
| 2.8 | Application configuration and licensing → SIAPP Runtimes | add SIAPP Runtime 0 (SIAR); RAM limit 128 MB, CPU share 25 %, process limit 100 (dev guide used 105 MB / 25 %) |
| 2.9 | SIAPP → SIAPP Runtime 0 → Ports | VETH0 with the container IP; a second port on a physical port group (not the SICAM WEB one) toward the broker network |
| 2.10 | SIAPP → SIAPPs | add `FaultRecordBridge-sws` (or `-a8000`), flash ≥ recommended size (allow store: 700 MB default on A8000), behaviour after error = Restart, signals read/write = yes |
| 2.11 | SIAPP signals | assign read topics `DFR01_*`, write topics `FRB_BrokerOK`, `FRB_Queue`, `FRB_DeliveryOK`, `FRB_StoreAlarm`, `FRB_Heartbeat` |
| 2.12 | SIAPP parameters | MQTT_HOST, MQTT_USER/PASS, MQTT_CA (ca.pem), HTTP_PASS, COMPANY, DEFAULT_IED … |
| 2.13 | Upload & Download | Upload to the device; check SICAM WEB → Applications → SIAPP status *running* |

All settings, including the IED mapping (`IED_MAP`), the RTU input signals (`EDGE_SIGNALS`) and the
status output topics (`OUT_*`), are SIAPP parameters in Device Manager. Example values:
`IED_MAP = 7SX85=7sx85-01;7KE85_01=7ke85-01` and
`EDGE_SIGNALS = DFR01_RcdMade:7sx85-01:record_made;DFR01_FltNum:7sx85-01:fault_number;DFR01_Health:7sx85-01:health`.

## 3. Tests

| # | Test | Pass criteria |
|---|---|---|
| T1 | **Find the parameter path.** Temporarily set `LOG_LEVEL=DEBUG`; check log for "Device Manager parameters loaded from …". If missing, list `/cmd` and `/persist_data` from a debug build | Path recorded and added to `DM_PARAM_CANDIDATES` (open item 1) |
| T2 | SIAPP start with valid config | Log `MQTT connected`; retained status `online`; `FRB_BrokerOK = 1` in SICAM WEB |
| T3 | **Capture xConnect format.** `HTTP_*` set, `ingest.http.capture=true`; trigger a 7SX85 test fault | Capture JSON in `/persist_data/frb/capture` shows method, headers, encoding (open item 2) |
| T4 | Fault record end to end (path A) | Within PROTI5 poll + xConnect interval: EMS receives meta + chunks, SHA-256 OK, file saved with C37.232 name, ack → state `acked` |
| T5 | Fault record end to end (path B, FTPS) | Same as T4 with `FTPS_ENABLE=1`; no duplicate if both paths active |
| T6 | Event | `record_made` event with fault number and RTU time stamp seconds after the trigger |
| T7 | Broker outage 10 min, 3 faults during outage | Records queued; delivered in order after reconnect; status went `offline` (LWT) then `online` |
| T8 | EMS receiver stopped | Re-publish every `ACK_TIMEOUT_S`; `FRB_DeliveryOK = 0` after 10 min; delivered + acked after restart |
| T9 | Data request | `ems_receiver.py --request list --from …` lists records; `--request republish --record-id …` re-delivers |
| T10 | Store full | Set `MAX_STORE_MB` small, stop EMS; `FRB_StoreAlarm = 1`, no unacked record lost, xConnect receives 507 |
| T11 | SIAPP restart / device restart | Queue intact; delivery resumes |
| T12 | Resources | SICAM WEB SIAPP RAM/CPU within runtime limits during a burst of 10 records (soak test on PC: ~34 MB RSS) |
| T13 | Security | Wrong broker CA → no connection; wrong HTTP password → 401; EMS account cannot publish under `/dfr/`; no listening port other than 8443 on the SIAPP |
| T14 | Record size | Largest real 7KE85 record (16 kHz, longest duration) passes PROTI5 ZIP limit, xConnect and broker packet limit |
| T15 | Repeat T2–T13 on CP-8050 (a8000 build) | Same results; note RAM/CPU |

## 4. Record results

For each test note date, firmware/app versions (CPCI85/SICORE, PROTI5, xConnect, File Archive
Access, Device Manager), SIAPP build hash, result and evidence (screenshot / log excerpt). These
records double as commissioning evidence for the customer.
