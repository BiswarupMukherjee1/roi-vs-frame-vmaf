"""Extension: static vs per-frame (dynamic) ROI maps with SVT-AV1.

FFmpeg's libsvtav1 wrapper ignores ROI side data, so this stage uses SvtAv1EncApp (built from the
same 3.1.2 source into store/build) and its --roi-map-file: one line per frame, one qindex offset per
64x64 superblock. Under VBR the frame-level rate control undoes the offsets (checked on a test clip),
so this stage runs CRF and compares variants by BD-rate on measured bitrate.

Variants (codec = "svtapp"):
  app-base   : no map
  app-static : overlay + static face boxes (what addroi can express)
  app-dyn    : overlay + this frame's face boxes (tracks moving faces)
Metadata: qoffset holds the qindex offset (negative = more bits).
"""
from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

from .common import Cfg, check_storage, log, read_json, run, write_json
from .encode import stream_bits

APP = "build/SVT-AV1-v3.1.2/Bin/Release/SvtAv1EncApp"


def sb_map(rects, W, H, off, sb=64) -> np.ndarray:
    cols, rows = -(-W // sb), -(-H // sb)
    m = np.zeros((rows, cols), int)
    for x, y, w, h in rects:
        m[y // sb:-(-(y + h) // sb), x // sb:-(-(x + w) // sb)] = off
    return m


def write_map(cfg: Cfg, clip: str, variant: str, off: int):
    roi = read_json(cfg.results / "roi" / f"{clip}.json")
    W, H = roi["width"], roi["height"]
    p = cfg.d("tmp", "roimaps") / f"{clip}_{variant}_{-off}.txt"
    lines = []
    for i in range(roi["frames"]):
        rects = [roi["overlay"]] + (roi["face_static"] if variant == "app-static" else roi["faces_per_frame"][i])
        lines.append(f"{i} " + " ".join(map(str, sb_map(rects, W, H, off).ravel())))
    p.write_text("\n".join(lines) + "\n")
    cover = np.mean([(sb_map([roi["overlay"]] + (roi["face_static"] if variant == "app-static" else f), W, H, 1)).mean()
                     for f in roi["faces_per_frame"]])
    return p, float(cover)


def encode(cfg: Cfg, clip: str, variant: str, crf: int, off: int) -> dict:
    tag = f"svtapp-{variant[4:]}" + (f"-o{-off}" if variant != "app-base" else "") + f"-crf{crf}"
    out = cfg.d("bitstreams", clip) / f"{tag}.mp4"
    meta_p = out.with_suffix(".json")
    if out.exists() and meta_p.exists():
        return read_json(meta_p)
    meta = cfg.ref_meta(clip)
    K = round(cfg["encode"]["keyint_s"] * meta["fps_float"])
    ivf = out.with_suffix(".ivf")
    logf = cfg.logs / "encode" / f"{clip}_{tag}.log"
    logf.parent.mkdir(exist_ok=True)
    logf.unlink(missing_ok=True)
    cmd = [str(cfg.store / APP), "-i", "stdin", "-b", str(ivf), "--preset", str(cfg["encode"]["svtav1"]["preset"]),
           "--crf", str(crf), "--keyint", str(K), "--progress", "0"]
    cover = 0.0
    if variant != "app-base":
        mp, cover = write_map(cfg, clip, variant, off)
        cmd += ["--roi-map-file", str(mp)]
    import time
    t0 = time.perf_counter()
    with open(logf, "a") as lf:
        lf.write("$ ffmpeg ... | " + " ".join(cmd) + "\n")
        lf.flush()
        dec = subprocess.Popen(["ffmpeg", "-v", "error", "-nostdin", "-i", str(cfg.ref_path(clip)),
                                "-f", "yuv4mpegpipe", "-pix_fmt", "yuv420p", "-strict", "-1", "-"],
                               stdout=subprocess.PIPE, stderr=lf)
        enc = subprocess.run(cmd, stdin=dec.stdout, stdout=lf, stderr=lf)
        dec.stdout.close()
        dec.wait()
    dt = time.perf_counter() - t0
    if enc.returncode != 0:
        raise RuntimeError(f"SvtAv1EncApp failed for {clip}/{tag}, see {logf}")
    run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", ivf, "-c", "copy", out], log_file=logf)
    ivf.unlink()
    bits = stream_bits(out)
    dur = meta["frames"] / meta["fps_float"]
    res = dict(clip=clip, codec="svtapp", mode=variant, kbps=0, qoffset=float(off), rep=0, crf=crf, tag=tag,
               path=str(out.relative_to(cfg.store)), bytes=out.stat().st_size, kbps_actual=bits / dur / 1000,
               enc_seconds=dt, enc_fps=meta["frames"] / dt, frames=meta["frames"], keyint=K, sb_cover=cover)
    write_json(meta_p, res)
    log.info("[svtroi] %-9s %-26s -> %7.0f kbps  %.0fs", clip, tag, res["kbps_actual"], dt)
    return res


def main(cfg: Cfg, clips=None):
    x = cfg["encode"].get("svtapp", {})
    crfs = x.get("crfs", [27, 35, 43, 51])
    offs = x.get("qindex_offsets", [-24, -48])
    if not (cfg.store / APP).exists():
        raise RuntimeError(f"{APP} missing: build it (see README, 'SVT-AV1 app')")
    jobs = []
    for c in clips or cfg.clips:
        for crf in crfs:
            jobs.append((c, "app-base", crf, 0))
            jobs += [(c, "app-static", crf, o) for o in offs]
            jobs += [(c, "app-dyn", crf, o) for o in offs]
    log.info("[svtroi] %d jobs", len(jobs))
    with ThreadPoolExecutor(cfg["compute"]["jobs"]) as ex:
        futs = [ex.submit(encode, cfg, *j) for j in jobs]
        for k, f in enumerate(as_completed(futs), 1):
            f.result()
            if k % 10 == 0:
                check_storage(cfg, f"svtroi {k}/{len(jobs)}")
