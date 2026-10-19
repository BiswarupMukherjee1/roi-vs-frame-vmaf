"""Stage 6: compute cost of each metric / pooling, single-threaded, in CPU ms per frame.

FFmpeg metrics are timed as (CPU time with metric) - (CPU time of decode-only), median of reps.
Python pooling is timed per operation on top of one SSIM map per frame.
Uses the x265 base encode at ladder point 2 of every clip.
Output: results/timing.csv
"""
from __future__ import annotations

import os
import subprocess
import time

import cv2
import numpy as np
import pandas as pd

from .common import Cfg, log, read_json, sync_chain
from .metrics import block_means, gray_frames, rect_mask, ssim_map


def ff_time(cfg, clip, dist, filt) -> float:
    """CPU seconds (user + sys) of one FFmpeg run. FFmpeg 7 decodes and filters in separate threads,
    so wall time would hide cheap metrics under decoding; CPU time adds up the real work."""
    fps = cfg.ref_meta(clip)["fps"]
    sync = sync_chain(fps)
    g = f"[0:v]{sync}[d];[1:v]{sync}[r];" + filt
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-threads", "1", "-filter_threads", "1",
           "-filter_complex_threads", "1", "-i", str(dist), "-threads", "1", "-i", str(cfg.ref_path(clip)),
           "-filter_complex", g, "-f", "null", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    _, status, ru = os.wait4(p.pid, 0)
    p.returncode = os.waitstatus_to_exitcode(status)
    err = p.stderr.read().decode(errors="replace")
    p.stderr.close()
    if p.returncode != 0:
        raise RuntimeError(f"timing run failed: {' '.join(cmd)}\n{err[-1000:]}")
    return ru.ru_utime + ru.ru_stime


def main(cfg: Cfg, clips=None):
    cv2.setNumThreads(1)  # single-threaded like the FFmpeg runs
    out = cfg.results / "timing.csv"
    reps = cfg["metrics"]["timing_reps"]
    model = cfg["metrics"]["vmaf_model"]
    from .encode import ladders
    lad = ladders(cfg)
    raw_p = cfg.results / "timing_raw.csv"  # per-clip checkpoint: finished clips are not re-timed
    rows = pd.read_csv(raw_p).to_dict("records") if raw_p.exists() else []
    for clip in clips or cfg.clips:
        if any(r["clip"] == clip for r in rows):
            log.info("[timing] %s: already timed", clip)
            continue
        meta = cfg.ref_meta(clip)
        N, W, H = meta["frames"], meta["width"], meta["height"]
        tag = f"x265-base-{lad[clip][1]}k-r0"
        dist = cfg.store / "bitstreams" / clip / f"{tag}.mp4"
        roi = read_json(cfg.results / "roi" / f"{clip}.json")
        x, y, w, h = roi["overlay"]
        crop = f"crop={w}:{h}:{x}:{y}"
        filters = {
            "decode only": "[d][r]blend=all_mode=normal:all_opacity=0",
            "PSNR (frame)": "[d][r]psnr",
            "SSIM (frame)": "[d][r]ssim",
            "XPSNR (frame)": "[r][d]xpsnr",
            "VMAF (frame)": f"[d][r]libvmaf=model=version={model}:n_threads=1",
            "VMAF (overlay crop)": f"[d]{crop}[dc];[r]{crop}[rc];[dc][rc]libvmaf=model=version={model}:n_threads=1",
        }
        for name, f in filters.items():
            ts = [ff_time(cfg, clip, dist, f) for _ in range(reps)]
            rows.append(dict(clip=clip, metric=name, cpu_s=float(np.median(ts)), frames=N))
            log.info("[timing] %s %-22s %.2f s CPU", clip, name, rows[-1]["cpu_s"])

        # python pooling on the same frames
        roi_rects = [roi["overlay"]] + roi["face_static"]
        mask = rect_mask((H, W), roi_rects)
        t = dict(decode_py=0.0, ssim_map=0.0, pool_mean=0.0, pool_pblk=0.0, pool_roiw=0.0)
        t0 = time.process_time()
        for d, r in zip(gray_frames(dist, W, H), gray_frames(cfg.ref_path(clip), W, H)):
            t1 = time.process_time()
            t["decode_py"] += t1 - t0
            m = ssim_map(r, d)
            t2 = time.process_time()
            _ = float(m.mean())
            t3 = time.process_time()
            _ = float(np.percentile(block_means(m, cfg["metrics"]["block"]), cfg["metrics"]["percentile"]))
            t4 = time.process_time()
            _ = 0.5 * float(m[mask].mean()) + 0.5 * float(m[~mask].mean())
            t5 = time.process_time()
            t["ssim_map"] += t2 - t1
            t["pool_mean"] += t3 - t2
            t["pool_pblk"] += t4 - t3
            t["pool_roiw"] += t5 - t4
            t0 = time.process_time()
        for k, v in t.items():
            rows.append(dict(clip=clip, metric=f"py:{k}", cpu_s=v, frames=N))
        pd.DataFrame(rows).to_csv(raw_p, index=False)

    df = pd.DataFrame(rows)
    df["ms_per_frame"] = 1000 * df.cpu_s / df.frames
    base = df[df.metric == "decode only"].set_index("clip")["ms_per_frame"]
    ff = ~df.metric.str.startswith("py:") & (df.metric != "decode only")
    df["ms_per_frame_net"] = df["ms_per_frame"] - np.where(ff, df["clip"].map(base), 0.0)
    df.to_csv(out, index=False)
    log.info("[timing] wrote %s", out)
