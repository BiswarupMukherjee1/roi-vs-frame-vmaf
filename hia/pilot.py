"""Stage 3: pick per-clip rate ladders.

x265 CRF probes -> (bitrate, frame VMAF) -> interpolate log(bitrate) at the target VMAFs.
Output: results/pilot.json  {probes: ..., ladders: {clip: [kbps x4]}}
Ladders fixed in config.yaml (encode.ladders) take priority over these.
"""
from __future__ import annotations

import re

import numpy as np

from .common import Cfg, log, read_json, run, sync_chain, write_json
from .encode import Job, run_jobs


def quick_vmaf(cfg: Cfg, clip: str, dist) -> float:
    fps = cfg.ref_meta(clip)["fps"]
    sync = sync_chain(fps)
    res, _ = run(["ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-i", dist, "-i", cfg.ref_path(clip),
                  "-lavfi", f"[0:v]{sync}[d];[1:v]{sync}[r];[d][r]libvmaf=model=version={cfg['metrics']['vmaf_model']}"
                  f":n_threads={cfg['compute']['threads_per_job'] * 2}", "-f", "null", "-"])
    return float(re.search(r"VMAF score: ([\d.]+)", res.stderr).group(1))


def nice(k: float) -> int:
    """Round to 2 significant digits (e.g. 3473 -> 3500)."""
    p = 10 ** max(0, int(np.floor(np.log10(k))) - 1)
    return int(round(k / p) * p)


def main(cfg: Cfg, clips=None):
    out = cfg.results / "pilot.json"
    state = read_json(out) if out.exists() else {"probes": {}, "ladders": {}}
    e = cfg["encode"]
    clips = clips or list(cfg.clips)
    jobs = [Job(c, "x265", "pilot", crf=q) for c in clips for q in e["pilot_crf_x265"]]
    metas = run_jobs(cfg, jobs, sub="pilot")
    for c in clips:
        pts = []
        for m in metas:
            if m["clip"] != c:
                continue
            v = quick_vmaf(cfg, c, cfg.store / m["path"])
            pts.append(dict(crf=m["crf"], kbps=m["kbps_actual"], vmaf=v))
        pts.sort(key=lambda p: p["kbps"])
        lk = np.log([p["kbps"] for p in pts])
        vm = np.array([p["vmaf"] for p in pts])
        # monotone in practice; enforce for interpolation
        vm = np.maximum.accumulate(vm)
        lad = []
        for t in e["ladder_vmaf_targets"]:
            if t <= vm[0]:
                s = (lk[1] - lk[0]) / max(1e-6, vm[1] - vm[0])
                lk_t = lk[0] + s * (t - vm[0])
            elif t >= vm[-1]:
                s = (lk[-1] - lk[-2]) / max(1e-6, vm[-1] - vm[-2])
                lk_t = lk[-1] + s * (t - vm[-1])
            else:
                lk_t = np.interp(t, vm, lk)
            lad.append(nice(float(np.exp(lk_t))))
        state["probes"][c] = pts
        state["ladders"][c] = lad
        log.info("[pilot] %s probes %s -> ladder %s kbps", c,
                 [(round(p["kbps"]), round(p["vmaf"], 1)) for p in pts], lad)
    write_json(out, state)
