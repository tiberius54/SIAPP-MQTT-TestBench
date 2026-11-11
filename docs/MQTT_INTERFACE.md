# Fault Record Bridge – MQTT Interface Specification (v1)

Audience: the control-center / EMS integration team. This document is the contract between the
SICAM 8 SIAPP "Fault Record Bridge" (publisher) and any system that consumes fault records from it.
`tools/ems_receiver.py` is a working reference implementation of the receiving side.

## 1. Transport

| Item | Value |
|---|---|
| Protocol | MQTT 3.1.1 (default) or MQTT 5.0 (configurable) |
| Security | TLS 1.2+ on port 8883; broker certificate validated against the configured CA; username/password; optional client certificate |
| QoS | 1 for records, events, acks and requests (configurable) |
| Session | persistent session (`clean_session = false` / MQTT 5 session expiry 3600 s) |
| Message size | file data is split into chunks of `chunk_size` bytes (default 131072 = 128 KiB). The broker's maximum packet size must be larger than the chunk size plus topic overhead |
| Character set | topics and JSON are UTF-8 |

## 2. Topic tree

`{prefix}` is configured per site (default `utility/site01`). `{ied}` is the IED identifier
(configured mapping, e.g. `7ke85-01`). `{bridge}` is the bridge ID (e.g. `frb01`).

| Topic | Direction | Payload | Retained |
|---|---|---|---|
| `{prefix}/{ied}/dfr/event` | bridge → EMS | JSON event (§3) | no |
| `{prefix}/{ied}/dfr/last_event` | bridge → EMS | last JSON event | yes |
| `{prefix}/{ied}/dfr/{rtype}/{record_id}/meta` | bridge → EMS | JSON metadata (§4), sent **before** the chunks | no |
| `{prefix}/{ied}/dfr/{rtype}/{record_id}/chunk/{n}` | bridge → EMS | raw bytes of the file, `n = 0 … chunk_count-1` | no |
| `{prefix}/bridge/{bridge}/status` | bridge → EMS | `{"state":"online"\|"offline", ...}` – `offline` is the MQTT last will | yes |
| `{prefix}/bridge/{bridge}/health` | bridge → EMS | queue, store and counters (§6) | yes |
| `{prefix}/bridge/{bridge}/ack` | EMS → bridge | acknowledgement (§5) | no |
| `{prefix}/bridge/{bridge}/request` | EMS → bridge | data request (§7) | no |
| `{prefix}/bridge/{bridge}/response` | bridge → EMS | answer to a data request | no |

`rtype` ∈ `comtrade` (disturbance/transient records), `pqdif` (PQ, trend, continuous records),
`comfede` (fault/device logs), `settings`, `asset`, `other`.
Subscribe to everything for a site with `{prefix}/#`, or to records only with `{prefix}/+/dfr/#`.

## 3. Event payload

Published when an RTU signal assigned to the SIAPP changes (e.g. the recorder's `RDRE.RcdMade`,
read by the RTU's IEC 61850 client). Events arrive within seconds; the file follows when the
RTU has retrieved and archived it.

```json
{
  "bridge_id": "frb01",
  "published_utc": "2026-11-10T22:15:31.402Z",
  "event": "record_made",            // record_made | recorder_health | status_change
  "ied": "7ke85-01",
  "fault_number": 42,                // record_made only, from RDRE.FltNum when configured
  "healthy": true,                   // recorder_health only
  "signal": "DFR01_RcdMade",
  "value": 1,
  "quality": ["VALID_VALUE"],
  "time": "2026-11-10T22:15:30.123456Z"   // RTU time stamp of the signal change (UTC)
}
```

## 4. Record metadata (`.../meta`)

```json
{
  "schema": "frb.record.v1",
  "record_id": "9f2c51d0a4b7e3c1",          // first 16 hex chars of the SHA-256 of the file
  "bridge_id": "frb01",
  "ied": "7ke85-01",
  "rtype": "comtrade",
  "file_name": "261110,151530123456,-7h,SS01,7KE85_01,ACME.zip",   // IEEE C37.232 style name (COMTRADE)
  "original_name": "7KE85_01_FLT00042",
  "source": "xconnect",                      // xconnect | ftps | folder
  "size": 278431,
  "sha256": "9f2c51d0a4b7e3c1…",
  "content_type": "application/zip",
  "chunk_size": 131072,
  "chunk_count": 3,
  "trigger_utc": "2026-11-10T22:15:30.123456Z",
  "start_utc": "2026-11-10T22:15:30.023456Z",
  "station": "SS01", "device": "7KE85_01", "rev_year": "2013", "time_code": "-7",
  "channels_analog": 6, "channels_digital": 2,
  "members": ["7KE85_01_FLT00042.cfg", "7KE85_01_FLT00042.dat", "7KE85_01_FLT00042.hdr", "7KE85_01_FLT00042.inf"],
  "published_utc": "2026-11-10T22:16:02.551Z",
  "attempt": 1,                              // increases on every re-publication
  "ack_topic": "utility/site01/bridge/frb01/ack",
  "require_ack": true,
  "warnings": []
}
```

The zip is delivered byte-for-byte as SICAM 8 archived it; COMTRADE members are not modified.
MQTT 5 sessions additionally carry user properties `record_id`, `index`, `count` and the content type on every chunk.

### Reassembly rules
1. Buffer chunks per `record_id` (chunks may arrive before the metadata).
2. When `meta` is present and `chunk_count` chunks are received, concatenate in index order.
3. Verify `size` and `sha256`. On mismatch, publish an ack with `"status":"resend"`.
4. If a record stays incomplete (e.g. 5 minutes), request a resend.
5. A record with a newer `attempt` replaces partial data from an older attempt.
6. Receiving a record that was already stored is normal (re-publication); acknowledge it again.

## 5. Acknowledgement (EMS → bridge)

```json
{"record_id": "9f2c51d0a4b7e3c1", "status": "ok", "receiver": "ems-cc1"}
{"record_id": "9f2c51d0a4b7e3c1", "status": "resend", "reason": "integrity check failed"}
```

Without an `ok` ack the bridge re-publishes the record every `ack_timeout_s` (default 120 s) and
keeps it in its store. Acknowledged records are kept for `retention_days` (default 30) so that
they can still be requested (§7), then deleted. If the store fills with unacknowledged records,
new records are refused and the bridge raises its store alarm.

## 6. Health (retained)

```json
{"bridge_id":"frb01","time":"2026-11-10T22:20:00.000Z","version":"0.1.0","broker_connected":true,
 "queue":0,"store_bytes":5873120,"store_alarm":false,
 "counters":{"received":30,"duplicates":1,"published":31,"acked":30,"failed":0,"events":4}}
```

## 7. Data requests (EMS → bridge)

Supports retrieval of stored records for data requests (e.g. CLO-003 R7 "provide within 15 days").

```json
{"request_id": "q-001", "cmd": "list", "ied": "7ke85-01", "from": "2026-11-01T00:00:00Z", "to": "2026-11-10T23:59:59Z"}
{"request_id": "q-002", "cmd": "republish", "record_id": "9f2c51d0a4b7e3c1"}
{"request_id": "q-003", "cmd": "republish", "ied": "7ke85-01", "rtype": "comtrade", "from": "2026-11-09T00:00:00Z"}
```

Filters: `record_id`, `ied`, `rtype`, `from`, `to` (ISO 8601 UTC, compared with the trigger time),
`limit` (default 200, max 1000). Response on `.../response`:

```json
{"request_id":"q-001","cmd":"list","status":"ok","bridge_id":"frb01","time":"…",
 "records":[{"record_id":"9f2c51d0a4b7e3c1","ied":"7ke85-01","rtype":"comtrade",
             "file_name":"261110,151530123456,-7h,SS01,7KE85_01,ACME.zip",
             "trigger_utc":"2026-11-10T22:15:30.123456Z","size":278431,"state":"acked"}]}
```

`republish` queues the matching records for publication again (normal record flow, §4).

## 8. Broker access control (recommended)

| Account | Allowed |
|---|---|
| bridge (`frb01`) | write `{prefix}/+/dfr/#`, `{prefix}/bridge/frb01/{status,health,response}`; read `{prefix}/bridge/frb01/{ack,request}` |
| EMS / archive | read `{prefix}/#`; write `{prefix}/bridge/+/ack`, `{prefix}/bridge/+/request` |

See `tools/broker/acl` for a Mosquitto example. No account other than the bridge may publish
under `/dfr/`, so a consumer can trust that record topics come from the bridge.
