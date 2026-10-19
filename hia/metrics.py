"""Stage 5: per-frame metrics for every encode.

Pass A (FFmpeg): frame VMAF/PSNR/SSIM (libvmaf), XPSNR, and libvmaf on the overlay crop and the
                 static face crop (VMAF + PSNR + SSIM of the region).
Pass B (Python): one SSIM map per frame (luma, Gaussian 11x11, sigma 1.5) pooled four ways
                 (mean, p-th percentile of block SSIM, ROI-weighted, ROI only), per-frame face
                 regions from the detector, and OCR of the scoreboard on every N-th frame.

Output: results/frames/<clip>/<tag>.csv.gz (one row per frame).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import cv2
import numpy as np
import pandas as pd

from .common import Cfg, check_storage, log, read_json, run, sync_chain

C1, C2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2


# ------------------------------------------------------------------ SSIM map + pooling
def ssim_map(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    x = x.astype(np.float32)
    y = y.astype(np.float32)
    g = lambda a: cv2.GaussianBlur(a, (11, 11), 1.5, borderType=cv2.BORDER_REFLECT)
    mx, my = g(x), g(y)
    sxx, syy, sxy = g(x * x) - mx * mx, g(y * y) - my * my, g(x * y) - mx * my
    return ((2 * mx * my + C1) * (2 * sxy + C2)) / ((mx * mx + my * my + C1) * (sxx + syy + C2))


def block_means(m: np.ndarray, b: int) -> np.ndarray:
    H, W = m.shape
    rs, cs = np.arange(0, H, b), np.arange(0, W, b)
    s = np.add.reduceat(np.add.reduceat(m, rs, axis=0), cs, axis=1)
    cnt = np.outer(np.diff(np.append(rs, H)), np.diff(np.append(cs, W)))
    return s / cnt


def rect_mask(shape, rects) -> np.ndarray:
    m = np.zeros(shape, bool)
    for x, y, w, h in rects:
        m[y:y + h, x:x + w] = True
    return m


def psnr(x, y) -> float:
    mse = np.mean((x.astype(np.float32) - y.astype(np.float32)) ** 2)
    return float(10 * np.log10(255 ** 2 / mse)) if mse > 0 else 100.0


def pooled(ssim: np.ndarray, block: int, pct: float, roi: np.ndarray, w: float) -> dict:
    bm = block_means(ssim, block)
    s_roi = float(ssim[roi].mean())
    s_bg = float(ssim[~roi].mean())
    return dict(ssim_mean=float(ssim.mean()), ssim_pblk=float(np.percentile(bm, pct)),
                ssim_roi=s_roi, ssim_roiw=w * s_roi + (1 - w) * s_bg, ssim_bg=s_bg)


# ------------------------------------------------------------------ OCR
def norm_text(s: str) -> str:
    return re.sub(r"\s+", "", s).upper()


def cer(hyp: str, ref: str) -> float:
    """Character error rate (Levenshtein / len(ref)) on whitespace-free text."""
    a, b = norm_text(hyp), norm_text(ref)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return min(1.0, prev[-1] / max(1, len(b)))


def ocr(crop: np.ndarray) -> str:
    img = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    ok, png = cv2.imencode(".png", img)
    r = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "6", "-l", "eng"],
                       input=png.tobytes(), capture_output=True, env={**os.environ, "OMP_THREAD_LIMIT": "1"})
    return r.stdout.decode("utf-8", "replace").strip()


# ------------------------------------------------------------------ decoding
def gray_frames(path: Path, w: int, h: int):
    size = w * h
    # extractplanes copies the Y plane bit-exactly (-pix_fmt gray alone would stretch 16-235 to 0-255)
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-vf", "extractplanes=y",
                          "-f", "rawvideo", "-pix_fmt", "gray", "-"], stdout=subprocess.PIPE, bufsize=size * 2)
    try:
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, np.uint8).reshape(h, w)
    finally:
        p.stdout.close()
        p.kill()
        p.wait()


# ------------------------------------------------------------------ pass A
def ffmpeg_pass(cfg: Cfg, dist: Path, clip: str, roi: dict, workdir: Path) -> pd.DataFrame:
    meta = cfg.ref_meta(clip)
    fps = meta["fps"]
    T = cfg["compute"]["threads_per_job"]
    model = cfg["metrics"]["vmaf_model"]
    regions = {"ov": roi["overlay"]}
    if roi["face_static"]:
        regions["face"] = max(roi["face_static"], key=lambda b: b[2] * b[3])
    n = 2 + len(regions)
    sync = sync_chain(fps)
    g = [f"[0:v]{sync},split={n}" + "".join(f"[d{i}]" for i in range(n)),
         f"[1:v]{sync},split={n}" + "".join(f"[r{i}]" for i in range(n))]
    vm = lambda tag: (f"libvmaf=model=version={model}:feature=name=psnr|name=float_ssim:"
                      f"log_fmt=json:log_path={workdir / (tag + '.json')}:n_threads={T}")
    g.append(f"[d0][r0]{vm('frame')}[o0]")
    for i, (name, (x, y, w, h)) in enumerate(regions.items(), start=1):
        g.append(f"[d{i}]crop={w}:{h}:{x}:{y}[dc{i}]")
        g.append(f"[r{i}]crop={w}:{h}:{x}:{y}[rc{i}]")
        g.append(f"[dc{i}][rc{i}]{vm(name)}[o{i}]")
    # xpsnr: first input is the ORIGINAL (activity weights come from it), second the reconstruction
    g.append(f"[r{n - 1}][d{n - 1}]xpsnr=stats_file={workdir / 'xpsnr.log'}[o{n - 1}]")
    maps = sum((["-map", f"[o{i}]"] for i in range(n)), [])
    run(["ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-y", "-i", dist, "-i", cfg.ref_path(clip),
         "-filter_complex", ";".join(g), *maps, "-f", "null", "-"],
        log_file=workdir / "ffmpeg.log")

    def load(tag, prefix):
        js = json.loads((workdir / f"{tag}.json").read_text())
        rows = [{"frame": f["frameNum"], **{f"{prefix}{k}": v for k, v in f["metrics"].items()}} for f in js["frames"]]
        return pd.DataFrame(rows).set_index("frame")

    df = load("frame", "")
    df = df.rename(columns={"float_ssim": "ssim_lib"})[["vmaf", "psnr_y", "psnr_cb", "psnr_cr", "ssim_lib"]]
    for name in regions:
        d = load(name, f"{name}_")
        df = df.join(d[[f"{name}_vmaf", f"{name}_psnr_y", f"{name}_float_ssim"]]
                     .rename(columns={f"{name}_float_ssim": f"{name}_ssim_lib"}))
    xs = []
    for line in (workdir / "xpsnr.log").read_text().splitlines():
        m = re.search(r"n:\s*(\d+).*?XPSNR y:\s*([\d.]+|inf)", line)
        if m:
            xs.append((int(m.group(1)) - 1, float(m.group(2))))
    if xs:
        df = df.join(pd.DataFrame(xs, columns=["frame", "xpsnr_y"]).set_index("frame"))
    return df


# ------------------------------------------------------------------ pass B
def python_pass(cfg: Cfg, dist: Path, clip: str, roi: dict) -> pd.DataFrame:
    meta = cfg.ref_meta(clip)
    W, H = meta["width"], meta["height"]
    mc = cfg["metrics"]
    ox, oy, ow, oh = roi["overlay"]
    ov_mask = rect_mask((H, W), [roi["overlay"]])
    rows, futs = [], {}
    with ThreadPoolExecutor(4) as ex:
        for i, (d, r) in enumerate(zip(gray_frames(dist, W, H), gray_frames(cfg.ref_path(clip), W, H))):
            faces = roi["faces_per_frame"][i]
            face_mask = rect_mask((H, W), faces)
            roi_mask = ov_mask | face_mask
            m = ssim_map(r, d)
            row = dict(frame=i, **pooled(m, mc["block"], mc["percentile"], roi_mask, mc["roi_weight"]))
            row["ov_ssim"] = float(m[ov_mask].mean())
            row["ov_psnr"] = psnr(d[ov_mask], r[ov_mask])
            if faces:
                row["facedyn_ssim"] = float(m[face_mask].mean())
                row["facedyn_psnr"] = psnr(d[face_mask], r[face_mask])
            rows.append(row)
            if i % mc["ocr_every"] == 0:
                futs[ex.submit(ocr, d[oy:oy + oh, ox:ox + ow].copy())] = i
        texts = {futs[f]: f.result() for f in as_completed(futs)}
    df = pd.DataFrame(rows).set_index("frame")
    truth = meta["truth"]
    # first frame showing a new clock value (tenths digit changed): where temporal prediction is stressed
    df["clock_change"] = [i == 0 or truth[i] != truth[i - 1] for i in df.index]
    df["ocr_text"] = pd.Series({i: t for i, t in texts.items()})
    df["ocr_cer"] = pd.Series({i: cer(t, "".join(truth[i])) for i, t in texts.items()})
    return df


def ref_ocr(cfg: Cfg, clip: str) -> pd.DataFrame:
    """OCR of the reference itself (ceiling: what Tesseract reads with no coding loss)."""
    p = cfg.r("ocr_ref") / f"{clip}.csv"
    if p.exists():
        return pd.read_csv(p, index_col="frame")
    meta = cfg.ref_meta(clip)
    ox, oy, ow, oh = meta["overlay"]["x"], meta["overlay"]["y"], meta["overlay"]["w"], meta["overlay"]["h"]
    every = cfg["metrics"]["ocr_every"]
    crops = {i: f[oy:oy + oh, ox:ox + ow].copy()
             for i, f in enumerate(gray_frames(cfg.ref_path(clip), meta["width"], meta["height"])) if i % every == 0}
    with ThreadPoolExecutor(8) as ex:
        texts = dict(zip(crops, ex.map(ocr, crops.values())))
    truth = meta["truth"]
    df = pd.DataFrame({"frame": list(texts), "ocr_text": list(texts.values()),
                       "clock_change": [i == 0 or truth[i] != truth[i - 1] for i in texts],
                       "ocr_cer": [cer(t, "".join(meta["truth"][i])) for i, t in texts.items()]}).set_index("frame")
    df.to_csv(p)
    log.info("[metrics] %s reference OCR: mean CER %.3f, exact %.0f %%", clip, df.ocr_cer.mean(),
             100 * (df.ocr_cer == 0).mean())
    return df


def measure(cfg: Cfg, clip: str, enc_meta: dict) -> Path:
    tag = enc_meta["tag"]
    out = cfg.r("frames", clip) / f"{tag}.csv.gz"
    if out.exists():
        return out
    dist = cfg.store / enc_meta["path"]
    roi = read_json(cfg.results / "roi" / f"{clip}.json")
    work = cfg.d("tmp", "metrics", clip, tag)
    a = ffmpeg_pass(cfg, dist, clip, roi, work)
    b = python_pass(cfg, dist, clip, roi)
    n = cfg.ref_meta(clip)["frames"]
    assert len(a) == n and len(b) == n, f"{clip}/{tag}: frame count {len(a)}/{len(b)} != {n}"
    df = a.join(b)
    for k in ("clip", "codec", "mode", "kbps", "qoffset", "rep", "crf", "tag", "kbps_actual"):
        df[k] = enc_meta[k]
    df.to_csv(out, compression="gzip")
    for f in work.iterdir():
        f.unlink()
    work.rmdir()
    log.info("[metrics] %-9s %-26s VMAF %.1f | overlay VMAF %.1f PSNR %.1f | OCR CER %.3f",
             clip, tag, df.vmaf.mean(), df.ov_vmaf.mean(), df.ov_psnr.mean(), df.ocr_cer.mean())
    return out


def encodes_on_disk(cfg: Cfg, clips=None, sub="bitstreams"):
    for c in clips or cfg.clips:
        d = cfg.store / sub / c
        if d.exists():
            for j in sorted(d.glob("*.json")):
                yield c, read_json(j)


def main(cfg: Cfg, clips=None):
    for c in clips or cfg.clips:
        ref_ocr(cfg, c)
    todo = list(encodes_on_disk(cfg, clips))
    log.info("[metrics] %d encodes", len(todo))
    with ThreadPoolExecutor(cfg["compute"]["jobs"]) as ex:
        futs = [ex.submit(measure, cfg, c, m) for c, m in todo]
        for k, f in enumerate(as_completed(futs), 1):
            f.result()
            if k % 10 == 0:
                check_storage(cfg, f"metrics {k}/{len(todo)}")
