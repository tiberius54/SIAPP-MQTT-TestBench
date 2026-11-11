#!/usr/bin/env python3
"""Generate a synthetic COMTRADE 2013 record (cfg/dat/hdr/inf) zipped like SICAM 8 archives it.

Used for bench and unit tests only - the waveform is a clean 60 Hz phase-A fault.
  python3 make_sample_comtrade.py out.zip --station SS01 --device 7KE85_01 --seconds 2 --rate 3840
"""
import argparse
import io
import math
import struct
import zipfile
from datetime import datetime, timedelta, timezone


def build(station="SS01", device="7KE85_01", seconds=0.5, rate=3840, freq=60.0, tz_hours=-7,
          trigger=None, fault_no=42, noise=0.0):
    import random
    trigger = trigger or datetime(2026, 11, 10, 22, 15, 30, 123456, tzinfo=timezone.utc)
    local_trig = (trigger + timedelta(hours=tz_hours)).replace(tzinfo=None)
    pre = 0.1
    start = local_trig - timedelta(seconds=pre)
    n = int(seconds * rate)
    analog = [("IA", "A", "A"), ("IB", "B", "A"), ("IC", "C", "A"), ("VA", "A", "kV"), ("VB", "B", "kV"), ("VC", "C", "kV")]
    digital = ["52A_CLOSED", "TRIP"]
    a_mult = 0.01
    lines = [f"{station},{device},2013",
             f"{len(analog) + len(digital)},{len(analog)}A,{len(digital)}D"]
    for i, (name, ph, unit) in enumerate(analog, 1):
        lines.append(f"{i},{name},{ph},,{unit},{a_mult},0,0,-32767,32767,1,1,P")
    for i, name in enumerate(digital, 1):
        lines.append(f"{i},{name},,,0")
    tc = f"{tz_hours}" if tz_hours <= 0 else f"+{tz_hours}"
    lines += [f"{freq:g}", "1", f"{rate},{n}",
              start.strftime("%d/%m/%Y,%H:%M:%S.%f"), local_trig.strftime("%d/%m/%Y,%H:%M:%S.%f"),
              "BINARY", "1", f"{tc},{tc}", "0,0"]
    cfg = "\r\n".join(lines) + "\r\n"
    dat = io.BytesIO()
    for k in range(n):
        t = k / rate
        fault = t >= pre
        vals = []
        for j, (_, _, unit) in enumerate(analog):
            ph = (j % 3) * 2 * math.pi / 3
            amp = (100.0 if unit == "A" else 66.0) * (12 if (fault and j == 0) else 1) * (0.3 if (fault and j == 3) else 1)
            v = amp * math.sin(2 * math.pi * freq * t - ph) + (random.gauss(0, noise * amp) if noise else 0)
            vals.append(int(max(-32767, min(32767, v / a_mult))))
        dig = (0 if fault and t > pre + 0.05 else 1) | ((1 if fault else 0) << 1)
        dat.write(struct.pack("<II" + "h" * len(vals) + "H", k + 1, int(t * 1e6), *vals, dig))
    hdr = f"Synthetic test record\r\nFault number {fault_no}\r\n"
    inf = f"[Public Record]\r\nFaultNumber={fault_no}\r\n"
    stem = f"{device}_FLT{fault_no:05d}"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(stem + ".cfg", cfg)
        z.writestr(stem + ".dat", dat.getvalue())
        z.writestr(stem + ".hdr", hdr)
        z.writestr(stem + ".inf", inf)
    return buf.getvalue(), stem, trigger


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--station", default="SS01")
    ap.add_argument("--device", default="7KE85_01")
    ap.add_argument("--seconds", type=float, default=0.5)
    ap.add_argument("--rate", type=int, default=3840)
    ap.add_argument("--fault", type=int, default=42)
    a = ap.parse_args()
    data, stem, trig = build(a.station, a.device, a.seconds, a.rate, fault_no=a.fault)
    with open(a.out, "wb") as f:
        f.write(data)
    print(f"wrote {a.out} ({len(data)} bytes), record {stem}, trigger {trig.isoformat()}")
