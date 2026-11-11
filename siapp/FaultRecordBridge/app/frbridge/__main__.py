"""Entry point:  python3 -m frbridge [--config path] [--check]"""
import argparse
import logging
import logging.handlers
import os
import signal
import sys
import time

from . import config as cfgmod
from .bridge import Bridge, VERSION


def setup_logging(level: str, root: str) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        os.makedirs(os.path.join(root, "logs"), exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(os.path.join(root, "logs", "frbridge.log"),
                                                             maxBytes=1_000_000, backupCount=3))
    except OSError:
        pass
    for h in handlers:
        h.setFormatter(fmt)
    logging.basicConfig(level=getattr(logging, str(level).upper(), logging.INFO), handlers=handlers, force=True)
    logging.getLogger("paho").setLevel(logging.WARNING)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="frbridge", description=f"Fault Record Bridge {VERSION}")
    ap.add_argument("--config", help="JSON config file (default: FRB_CONFIG or /persist_data/frb/config.json)")
    ap.add_argument("--dm-params", help="Device Manager parameter file (default: FRB_DM_PARAMS / candidates)")
    ap.add_argument("--check", action="store_true", help="validate configuration and exit")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    while True:
        try:
            cfg = cfgmod.load(a.config, a.dm_params)
            break
        except (cfgmod.ConfigError, OSError, ValueError) as e:
            logging.getLogger("frbridge").error("Configuration error: %s", e)
            if a.check:
                return 2
            # In the SIAR a fast exit would cause a restart loop; wait and re-read instead.
            logging.getLogger("frbridge").error("Waiting 60 s for a valid configuration "
                                                "(set the SIAPP parameters in SICAM Device Manager)")
            time.sleep(60)
    if a.check:
        print("configuration OK")
        return 0
    os.makedirs(cfg["storage"]["root"], exist_ok=True)
    setup_logging(cfg["log_level"], cfg["storage"]["root"])
    bridge = Bridge(cfg)
    signal.signal(signal.SIGTERM, lambda *_: bridge.stop())
    signal.signal(signal.SIGINT, lambda *_: bridge.stop())
    bridge.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
