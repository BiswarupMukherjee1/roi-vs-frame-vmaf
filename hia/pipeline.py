"""Entry point:  python3 -m hia.pipeline [stage ...] [--clips a,b]

Stages (in order): prepare roi pilot encode svtroi metrics timing analyze figures
'all' runs every stage. Every stage is idempotent: finished work is skipped.
"""
from __future__ import annotations

import argparse
import importlib
import sys
import time

from .common import Cfg, check_storage, log, setup_logging

STAGES = ["prepare", "roi", "pilot", "encode", "svtroi", "metrics", "timing", "analyze", "figures"]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("stages", nargs="*", default=["all"])
    ap.add_argument("--clips", default=None, help="comma-separated subset of clips")
    ap.add_argument("--config", default=None)
    a = ap.parse_args(argv)
    cfg = Cfg(a.config) if a.config else Cfg()
    setup_logging(cfg)
    stages = STAGES if a.stages == ["all"] else a.stages
    clips = a.clips.split(",") if a.clips else None
    log.info("=== run stages=%s clips=%s", stages, clips or "all")
    for st in stages:
        if st not in STAGES:
            sys.exit(f"unknown stage {st}; choose from {STAGES}")
        t0 = time.time()
        log.info("--- stage %s", st)
        mod = importlib.import_module(f"hia.{st}")
        mod.main(cfg, clips)
        gb = check_storage(cfg, st)
        log.info("--- stage %s done in %.0f s (project %.2f GB)", st, time.time() - t0, gb)
    log.info("=== finished")


if __name__ == "__main__":
    main()
