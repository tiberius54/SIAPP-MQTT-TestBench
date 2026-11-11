# Fault Record Bridge – Design

## 1. Goal

Deliver SIPROTEC fault-recorder data (7KE85 in production, 7SX85 on the bench) – COMTRADE
disturbance/transient records, PQDIF, COMFEDE fault/device logs – plus "record made" events and
recorder-health alarms from a SICAM A8000 (CP-8050) or SICAM S8000 to the control center / EMS
**over MQTT**, without Electrification X. Supports the CLO-003-1 disturbance-monitoring data
flow (R7 retrieval/retention, R8 recording-failure monitoring) in the NERC Project 2026-02
architecture drawing.

## 2. Why a SIAPP

| Option | Result |
|---|---|
| SICAM 8 MQTT Client (MQTTCLIENT01) | Publishes signals only (binary, measured value, command, setpoint, step position) in a JSON template, QoS 0 observed. **Cannot carry files.** |
| SICAM GridEdge IoT Monitoring & Control | Carries files over MQTT, but only to Electrification X. |
| xConnect / File Archive Access alone | Move files over HTTPS / FTPS, not MQTT. |
| **SIAPP (this design)** | Custom container on the RTU: takes files from the SICAM 8 file archive (via xConnect push or FTPS pull) and publishes them, chunked and acknowledged, over MQTT/TLS. Events and health via the Edge Data API. |

## 3. Data flow

```
 7KE85 / 7SX85 ──IEC 61850 MMS (file service, reports)──► SICAM 8 (CP-8050 or S8000)
                                                            │ PROTI5 / ETI5 → File Archive
                                                            │ RTU IEC 61850 client → signals (RcdMade, FltNum, health)
             ┌──────────────────────────────────────────────┤
             │ xConnect (Advanced) HTTPS POST    or          │ File Archive Access (FTPS)
             ▼                                               ▼
   ┌───────────────────────── Fault Record Bridge SIAPP ─────────────────────────┐
   │ ingest_http (xConnect receiver) │ ingest_files (FTPS pull, watch folder)    │
   │ records.py: classify, read COMTRADE .cfg, IEEE C37.232 name                 │
   │ store.py: SQLite + files in /persist_data (queue, retention, dedupe)       │
   │ publisher.py: MQTT/TLS, meta + chunks, acks, data requests, LWT status     │
   │ edge.py: Edge Data API → events; bridge health → RTU write signals          │
   └─────────────────────────────────────────────────────────────────────────────┘
             │ MQTT/TLS 8883 (outbound only)
             ▼
       MQTT broker (control center / DMZ) ──► EMS / fault-record archive (subscriber, acks)
```

## 4. Key decisions

1. **Python, stdlib + paho-mqtt only.** The SIAPP Expert Forum reports ~300 MB RAM shared by all
   SIAPPs on an A8000 and 60 % CPU per SIAPP, and that numpy/pandas exceeded the limits. This bridge
   measured **~34 MB RSS** while moving 30 records in a soak test. Runtime image: `python:3.12-alpine`
   (multi-stage, no compiler), built for `a8000` (linux/arm/v7) and `sws` (linux/amd64).
2. **Two ingest paths, one store.** xConnect push (needs xConnect *Advanced* for COMTRADE per the
   SICAM 8 V26.20 training deck) and FTPS pull from File Archive Access (licensed per client type).
   Whichever the project licenses works; duplicates are removed by content hash.
3. **Files are never modified.** The zip SICAM 8 archived is delivered byte-for-byte; metadata is
   read from it, and a C37.232-style name is added.
4. **Store-and-forward with end-to-end acknowledgement.** MQTT QoS 1 only covers bridge→broker. The
   EMS acknowledges each record on an ack topic after a SHA-256 check; until then the bridge
   re-publishes. Acknowledged records stay for `retention_days` so they can be requested again.
5. **Never silently drop unacknowledged records.** When the store is full, new records are refused
   (xConnect gets HTTP 507 and retries; FTPS files stay on the archive) and the store alarm is set.
6. **Events and health through the SIAPP**, so the MQTT Client app is optional – the CP-8050 allows
   only 4 SICAM 8 applications (SIAPPs do not count).
7. **Configuration in SICAM Device Manager** via `cmd/parameter_schema.json` (45 parameters, PEM
   blobs for CA/cert/key), with a JSON file and environment overrides as fallback.

## 5. Failure behaviour

| Condition | Behaviour | Visible as |
|---|---|---|
| Broker unreachable | Records queue in `/persist_data`; paho reconnects (1–60 s back-off) | `FRB_BrokerOK = 0`, status topic `offline` (LWT) |
| EMS does not acknowledge | Re-publish every `ack_timeout_s` | `FRB_DeliveryOK = 0` after 3× timeout (min 10 min) |
| Chunk lost / corrupted | Receiver requests resend; record re-published | receiver log |
| Store > 90 % / full | Purge acked by age; refuse new when full of unacked | `FRB_StoreAlarm = 1`, HTTP 507 |
| Invalid configuration | Logs the error, waits and re-reads every 60 s (no restart loop) | SIAPP log in SICAM WEB |
| Edge Data API not ready | Retries every 30 s | log |
| SIAPP restart / update | Queue survives in `/persist_data` | – |

Note: on the A8000, `/persist_data` is erased if the SIAPP slot size is changed or the slot is
recreated. The bridge is a forwarding buffer, not the archive of record.

## 6. Security

- Outbound MQTT/TLS 1.2+ only, broker certificate validated, per-device credentials, broker ACL
  restricting publish rights under `/dfr/` to the bridge account.
- xConnect receiver: basic auth or API key (constant-time compare), HTTPS optional; bind it to
  the internal VETH address so it is not reachable from field or WAN ports.
- FTPS client: certificate verification on by default.
- Inbound MQTT messages limited to two JSON topics (ack, request) with strict validation; no
  command path into the RTU (the SIAPP writes only its own status signals).
- No SSH or debug services in the image (the SDK demo SIAPPs include an SSH daemon – not used).
- Secrets come from Device Manager parameters and are masked in logs. Note: Device Manager
  string parameters are not password fields; protect the project file accordingly.
- CIP framing: the bridge is a custom component on the asset; for CIP-applicable sites it belongs
  in the configuration baseline and change management of the responsible entity.

## 7. Open items (to close on the bench – see BENCH_TEST_PLAN.md)

1. Where Device Manager delivers SIAPP parameter values inside the SIAR (add the path to
   `config.DM_PARAM_CANDIDATES`).
2. xConnect HTTP request format for file transfer (raw / multipart / JSON) – use `ingest.http.capture`.
3. Whether xConnect can target the SIAPP's VETH address; otherwise use the FTPS path.
4. File Archive Access folder layout and whether records appear zipped or as loose members.
5. xConnect licence level needed for COMTRADE (Advanced per training deck; manual lists Standard).
6. IEEE C37.232 time-code format – confirm the template against the standard text.
7. 7KE85 record sizes vs PROTI5 ZIP limit (1–10 MB) and the broker packet limit.
