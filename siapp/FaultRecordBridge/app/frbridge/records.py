"""Inspect incoming record files: classify, read COMTRADE headers, build IEEE C37.232 names.

Incoming files are what SICAM 8 produces in its file archive and hands out via
xConnect (zipped, named ``<original>_<YYYYMMDDThhmmssZ>.zip``) or File Archive
Access (FTPS).  Nothing here modifies the record contents; we only read them to
build metadata and a standard file name.
"""
from __future__ import annotations

import io
import os
import re
import zipfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

XCONNECT_NAME = re.compile(r"^(?P<orig>.+?)_(?P<ts>\d{8}T\d{6}Z)\.zip$", re.IGNORECASE)
XCONNECT_RANGE = re.compile(r"^(?P<ts>\d{8}T\d{6}Z)_(?P<ts2>\d{8}T\d{6}Z)\.zip$", re.IGNORECASE)
TIME_CODE = re.compile(r"^\s*([+-]?)(\d{1,2})(?:h(\d{2})?)?\s*$", re.IGNORECASE)

RTYPES = ("comtrade", "pqdif", "comfede", "settings", "asset", "other")


@dataclass
class RecordInfo:
    rtype: str = "other"
    members: list[str] = field(default_factory=list)
    station: str = ""
    device: str = ""
    rev_year: str = ""
    start_utc: Optional[str] = None      # first sample time, ISO 8601 UTC
    trigger_utc: Optional[str] = None    # trigger time, ISO 8601 UTC
    time_code: str = ""                  # COMTRADE 2013 time_code (offset of local time from UTC)
    channels_analog: int = 0
    channels_digital: int = 0
    xconnect_utc: Optional[str] = None   # time stamp xConnect put in the file name
    original_name: str = ""
    c37232_name: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_time_code(code: str) -> Optional[timedelta]:
    """COMTRADE 2013 time_code, e.g. '-7', '-5h30', '+0'.  Returns local-minus-UTC offset."""
    if not code:
        return None
    m = TIME_CODE.match(code)
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    return sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))


def _parse_cfg_time(s: str, rev_year: str) -> Optional[datetime]:
    s = s.strip()
    try:
        d, t = [x.strip() for x in s.split(",", 1)]
    except ValueError:
        return None
    if "." in t:
        hms, frac = t.split(".", 1)
        frac = (frac + "000000")[:6]
    else:
        hms, frac = t, "000000"
    try:
        if rev_year in ("1999", "2013"):
            day, mon, year = d.split("/")          # dd/mm/yyyy
        else:
            mon, day, year = d.split("/")          # 1991: mm/dd/yy
            if len(year) == 2:
                year = "19" + year if int(year) > 70 else "20" + year
        hh, mm, ss = hms.split(":")
        return datetime(int(year), int(mon), int(day), int(hh), int(mm), int(float(ss)), int(frac))
    except (ValueError, TypeError):
        return None


def parse_comtrade_cfg(text: str, info: RecordInfo) -> None:
    lines = [ln.rstrip("\r") for ln in text.splitlines()]
    if len(lines) < 4:
        info.warnings.append("cfg too short")
        return
    first = [x.strip() for x in lines[0].split(",")]
    info.station = first[0] if first else ""
    info.device = first[1] if len(first) > 1 else ""
    info.rev_year = first[2] if len(first) > 2 else "1991"
    try:
        counts = [x.strip() for x in lines[1].split(",")]
        total = int(counts[0])
        info.channels_analog = int(counts[1].rstrip("Aa"))
        info.channels_digital = int(counts[2].rstrip("Dd"))
        i = 2 + total            # skip channel lines
        i += 1                   # line frequency
        nrates = int(lines[i].strip()); i += 1
        i += max(nrates, 1)      # sample-rate lines (one line even when nrates == 0)
        start = _parse_cfg_time(lines[i], info.rev_year); i += 1
        trig = _parse_cfg_time(lines[i], info.rev_year); i += 1
        i += 1                   # file type (ASCII/BINARY/...)
        i += 1                   # timemult
        offset = None
        if info.rev_year == "2013" and i < len(lines):
            tc = lines[i].split(",")[0].strip()
            info.time_code = tc
            offset = parse_time_code(tc)
        if offset is None:
            info.warnings.append("no COMTRADE time_code: times assumed UTC")
            offset = timedelta(0)
        if start:
            info.start_utc = _iso((start - offset).replace(tzinfo=timezone.utc))
        if trig:
            info.trigger_utc = _iso((trig - offset).replace(tzinfo=timezone.utc))
    except (ValueError, IndexError) as e:
        info.warnings.append(f"cfg parse error: {e}")


def classify_members(names: list[str]) -> str:
    ext = {os.path.splitext(n)[1].lower() for n in names}
    if ".cfg" in ext and (".dat" in ext or ".cff" in ext) or ".cff" in ext:
        return "comtrade"
    if ".pqd" in ext:
        return "pqdif"
    if ".xml" in ext and any("comfede" in n.lower() or "log" in n.lower() for n in names):
        return "comfede"
    if ".xml" in ext:
        return "comfede"
    if ".csv" in ext:
        return "settings"
    if ".json" in ext:
        return "asset"
    return "other"


def inspect(data: bytes, filename: str) -> RecordInfo:
    info = RecordInfo(original_name=filename)
    base = os.path.basename(filename)
    m = XCONNECT_NAME.match(base)
    if m:
        info.original_name = m.group("orig")
        info.xconnect_utc = _iso(datetime.strptime(m.group("ts"), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc))
    elif XCONNECT_RANGE.match(base):
        mr = XCONNECT_RANGE.match(base)
        info.xconnect_utc = _iso(datetime.strptime(mr.group("ts2"), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc))
    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            info.members = [n for n in z.namelist() if not n.endswith("/")]
            info.rtype = classify_members(info.members)
            cfgs = [n for n in info.members if n.lower().endswith(".cfg")]
            if info.rtype == "comtrade" and cfgs:
                raw = z.read(cfgs[0])
                parse_comtrade_cfg(raw.decode("latin-1", errors="replace"), info)
    else:
        info.members = [base]
        info.rtype = classify_members([base])
        if info.rtype == "comtrade" and base.lower().endswith(".cfg"):
            parse_comtrade_cfg(data.decode("latin-1", errors="replace"), info)
    return info


def _clean_field(s: str) -> str:
    # C37.232 fields are comma separated; keep them file-system and MQTT safe
    s = re.sub(r"[,\\/:*?\"<>|#+\s]+", "_", s.strip())
    return s.strip("_") or "NA"


def tcode_from(info: RecordInfo) -> str:
    off = parse_time_code(info.time_code) if info.time_code else timedelta(0)
    total = int(off.total_seconds() // 60) if off is not None else 0
    sign = "-" if total < 0 else "+"
    h, mnt = divmod(abs(total), 60)
    return f"{sign}{h}h{mnt:02d}" if mnt else f"{sign}{h}h"


def c37232_name(info: RecordInfo, naming: dict, ext: str = ".zip") -> str:
    """Build an IEEE C37.232 style name: date,time,timecode,station,device,company.

    Date yymmdd and time hhmmssuuuuuu are taken from the trigger time (fallback: start
    time, xConnect time).  NOTE: verify the exact field formats (especially the time
    code) against your copy of IEEE C37.232; the template is configurable.
    """
    t = info.trigger_utc or info.start_utc or info.xconnect_utc
    if t:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        off = parse_time_code(info.time_code) or timedelta(0)
        local = dt + off       # C37.232 names use the recorder's local time + time code
    else:
        local = datetime.now(timezone.utc)
    fields = {
        "date": local.strftime("%y%m%d"),
        "time": local.strftime("%H%M%S") + f"{local.microsecond:06d}",
        "tcode": tcode_from(info),
        "station": _clean_field(naming.get("station") or info.station or "STATION"),
        "device": _clean_field(naming.get("device") or info.device or "DEVICE"),
        "company": _clean_field(naming.get("company") or "COMPANY"),
        "rtype": info.rtype,
    }
    return naming.get("template", "{date},{time},{tcode},{station},{device},{company}").format(**fields) + ext
