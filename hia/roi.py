"""Stage 2: region-of-interest map = known overlay box + faces from YuNet.

Per clip (results/roi/<clip>.json):
  faces_per_frame : padded face boxes per frame (used by per-frame region metrics)
  face_static     : 16-aligned boxes where a face is present in >= static_presence of frames
                    (addroi regions are static for the whole clip, so the encoder gets these)
  overlay         : the scoreboard box
  roi_rects       : overlay + face_static, the list handed to addroi
"""
from __future__ import annotations

import subprocess
import urllib.request

import cv2
import numpy as np

from .common import Cfg, log, read_json, write_json


def model_path(cfg: Cfg):
    p = cfg.d("models") / "face_detection_yunet_2023mar.onnx"
    if not p.exists():
        log.info("download YuNet model")
        urllib.request.urlretrieve(cfg["faces"]["model_url"], p)
    return p


def iter_frames(path, w, h, pix="bgr24", every=1):
    """Yield (index, frame) decoded by ffmpeg. pix: bgr24 -> HxWx3, gray -> HxW."""
    ch = 3 if pix == "bgr24" else 1
    size = w * h * ch
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-f", "rawvideo", "-pix_fmt", pix, "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=size * 2)
    i = 0
    try:
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            if i % every == 0:
                a = np.frombuffer(buf, np.uint8)
                yield i, (a.reshape(h, w, 3) if ch == 3 else a.reshape(h, w))
            i += 1
    finally:
        p.stdout.close()
        p.kill()
        p.wait()


def align_box(x0, y0, x1, y1, W, H, a=16):
    x0, y0 = max(0, (int(x0) // a) * a), max(0, (int(y0) // a) * a)
    x1, y1 = min(W, -(-int(np.ceil(x1)) // a) * a), min(H, -(-int(np.ceil(y1)) // a) * a)
    return [x0, y0, x1 - x0, y1 - y0]


def pad_box(b, pad, W, H):
    x, y, w, h = b
    x0, y0 = max(0.0, x - pad * w), max(0.0, y - pad * h)
    x1, y1 = min(W, x + w * (1 + pad)), min(H, y + h * (1 + pad))
    return [int(x0), int(y0), int(np.ceil(x1 - x0)), int(np.ceil(y1 - y0))]


def detect_clip(cfg: Cfg, clip: str) -> dict:
    out = cfg.r("roi") / f"{clip}.json"
    if out.exists():
        log.info("[roi] %s: exists", clip)
        return read_json(out)
    meta = cfg.ref_meta(clip)
    W, H = meta["width"], meta["height"]
    fc = cfg["faces"]
    det = cv2.FaceDetectorYN.create(str(model_path(cfg)), "", (W, H), fc["score_threshold"], 0.3, 5000)
    ov = meta["overlay"]
    ov_box = [ov["x"], ov["y"], ov["w"], ov["h"]]

    per_frame, scores = [], []
    presence = np.zeros((H, W), np.float32)
    for i, fr in iter_frames(cfg.ref_path(clip), W, H):
        _, faces = det.detect(fr)
        boxes = []
        if faces is not None:
            for f in faces:
                x, y, w, h = map(float, f[:4])
                if min(w, h) < fc["min_size"]:
                    continue
                # ignore anything inside the overlay (it is already an ROI)
                cx, cy = x + w / 2, y + h / 2
                if ov["x"] <= cx <= ov["x"] + ov["w"] and ov["y"] <= cy <= ov["y"] + ov["h"]:
                    continue
                b = pad_box([x, y, w, h], fc["pad"], W, H)
                boxes.append(b)
                scores.append(float(f[-1]))
                presence[b[1]:b[1] + b[3], b[0]:b[0] + b[2]] += 1
        per_frame.append(boxes)
    presence /= max(1, len(per_frame))

    mask = (presence >= fc["static_presence"]).astype(np.uint8)
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    comps = sorted([stats[k] for k in range(1, n)], key=lambda s: -s[4])
    static, area = [], 0
    cap = fc["max_area_frac"] * W * H
    for x, y, w, h, _ in comps:
        if not static and w * h > cap:
            # a single face larger than the cap (close-up): keep it, shrunk around its centre
            k = np.sqrt(cap / (w * h))
            cx, cy = x + w / 2, y + h / 2
            x, y, w, h = cx - k * w / 2, cy - k * h / 2, k * w, k * h
            log.warning("[roi] %s: face component shrunk by %.2f to fit max_area_frac", clip, k)
        b = align_box(x, y, x + w, y + h, W, H)
        if area + b[2] * b[3] > cap * 1.05:  # 5 % slack for block alignment
            continue
        static.append(b)
        area += b[2] * b[3]

    n_face_frames = sum(1 for b in per_frame if b)
    mean_face_area = float(np.mean([sum(bb[2] * bb[3] for bb in b) for b in per_frame if b])) if n_face_frames else 0.0
    res = dict(
        clip=clip, width=W, height=H, frames=len(per_frame), overlay=ov_box,
        faces_per_frame=per_frame, face_static=static, roi_rects=[ov_box] + static,
        stats=dict(frames_with_face=n_face_frames, frac_frames_with_face=n_face_frames / max(1, len(per_frame)),
                   n_detections=len(scores), mean_score=float(np.mean(scores)) if scores else None,
                   mean_face_area_px=mean_face_area, static_face_area_px=area,
                   static_over_mean_area=(area / mean_face_area) if mean_face_area else None,
                   overlay_area_frac=ov["w"] * ov["h"] / (W * H),
                   roi_area_frac=(ov["w"] * ov["h"] + area) / (W * H)))
    write_json(out, res)
    log.info("[roi] %s: faces in %d/%d frames, static face boxes %s, ROI = %.1f %% of frame",
             clip, n_face_frames, len(per_frame), static, 100 * res["stats"]["roi_area_frac"])
    return res


def addroi_filter(roi: dict, qoffset: float, which=("overlay", "face")) -> str:
    rects = []
    if "overlay" in which:
        rects.append(roi["overlay"])
    if "face" in which:
        rects += roi["face_static"]
    return ",".join(f"addroi=x={x}:y={y}:w={w}:h={h}:qoffset={qoffset}" for x, y, w, h in rects)


def main(cfg: Cfg, clips=None):
    for c in clips or cfg.clips:
        detect_clip(cfg, c)
