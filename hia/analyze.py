"""Stage 7: turn per-frame metrics into tables and a report.

Writes results/encodes.csv, results/tables/*.md|csv and results/REPORT.md.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from .bdrate import bd_quality, bd_rate, delta_at_equal, overlap
from .common import Cfg, log, read_json

# metric columns: (name in frames csv, label, higher is better)
FRAME_METRICS = [("vmaf", "frame VMAF"), ("psnr_y", "frame PSNR-Y"), ("ssim_mean", "frame SSIM"),
                 ("xpsnr_y", "frame XPSNR-Y")]
POOLED = [("ssim_mean", "SSIM mean"), ("ssim_pblk", "SSIM p5-block"), ("ssim_roiw", "SSIM ROI-weighted"),
          ("ssim_roi", "SSIM ROI only")]
REGION_METRICS = [("ov_vmaf", "overlay VMAF"), ("ov_psnr", "overlay PSNR-Y"), ("ov_ssim", "overlay SSIM"),
                  ("face_vmaf", "face VMAF (static box)"), ("facedyn_psnr", "face PSNR-Y (per-frame)"),
                  ("facedyn_ssim", "face SSIM (per-frame)")]


def load_frames(cfg: Cfg) -> pd.DataFrame:
    parts = [pd.read_csv(p) for p in sorted((cfg.results / "frames").glob("*/*.csv.gz"))]
    df = pd.concat(parts, ignore_index=True)
    df["qoffset"] = df["qoffset"].fillna(0.0)
    # reference OCR per frame: a misread only counts if Tesseract reads the uncompressed frame correctly
    refs = []
    for c in df["clip"].unique():
        p = cfg.results / "ocr_ref" / f"{c}.csv"
        if p.exists():
            refs.append(pd.read_csv(p)[["frame", "ocr_cer"]].rename(columns={"ocr_cer": "ocr_ref_cer"}).assign(clip=c))
    if refs:
        df = df.merge(pd.concat(refs), on=["clip", "frame"], how="left")
        df["new_misread"] = ((df.ocr_cer > 0) & (df.ocr_ref_cer == 0)).where(df.ocr_cer.notna() & df.ocr_ref_cer.notna())
    return df


def summarize(cfg: Cfg, fr: pd.DataFrame) -> pd.DataFrame:
    h = cfg["metrics"]["hidden"]
    num = [c for c in fr.columns if fr[c].dtype.kind in "fi" and c not in ("frame", "kbps", "qoffset", "rep", "crf", "kbps_actual")]
    g = fr.groupby(["clip", "tag"])
    s = g[num].mean()
    s["vmaf_p5"] = g["vmaf"].quantile(0.05)
    s["ov_vmaf_p5"] = g["ov_vmaf"].quantile(0.05)
    s["ov_psnr_min"] = g["ov_psnr"].min()
    fr = fr.assign(hidden=(fr.vmaf >= h["frame_vmaf_min"]) & (fr.vmaf - fr.ov_vmaf >= h["region_gap"]),
                   ocr_exact=(fr.ocr_cer == 0).astype(float).where(fr.ocr_cer.notna()))
    s["hidden_frac"] = fr.groupby(["clip", "tag"])["hidden"].mean()
    s["new_misread"] = fr.assign(nm=fr.new_misread.astype(float)).groupby(["clip", "tag"])["nm"].mean()
    s["ocr_exact"] = fr.groupby(["clip", "tag"])["ocr_exact"].mean()
    s["ocr_exact_change"] = fr[fr.clock_change].groupby(["clip", "tag"])["ocr_exact"].mean()
    s["ocr_exact_steady"] = fr[~fr.clock_change].groupby(["clip", "tag"])["ocr_exact"].mean()
    meta = fr.groupby(["clip", "tag"])[["codec", "mode", "kbps", "qoffset", "rep", "crf", "kbps_actual"]].first()
    s = meta.join(s).reset_index()
    enc = []
    for c in cfg.clips:
        for j in sorted((cfg.store / "bitstreams" / c).glob("*.json")):
            m = read_json(j)
            enc.append(dict(clip=c, tag=m["tag"], enc_seconds=m["enc_seconds"], enc_fps=m["enc_fps"]))
    s = s.merge(pd.DataFrame(enc), on=["clip", "tag"], how="left")
    ref = {c: pd.read_csv(cfg.results / "ocr_ref" / f"{c}.csv") for c in cfg.clips
           if (cfg.results / "ocr_ref" / f"{c}.csv").exists()}
    s["ocr_exact_ref"] = s["clip"].map({c: float((d.ocr_cer == 0).mean()) for c, d in ref.items()})
    s["ocr_exact_ref_change"] = s["clip"].map({c: float((d[d.clock_change].ocr_cer == 0).mean()) for c, d in ref.items()})
    return s.sort_values(["clip", "codec", "mode", "qoffset", "kbps", "rep"])


def md(df: pd.DataFrame, floatfmt=".2f") -> str:
    return df.to_markdown(index=False, floatfmt=floatfmt)


def base(s):  # first-run baseline encodes
    return s[(s["mode"] == "base") & (s.rep == 0)]


# analyses
def table_bdrate(cfg, s):
    """SVT-AV1 vs x265, by every metric. 'Both ways' = frame vs region metrics."""
    rows = []
    b = base(s)
    cols = FRAME_METRICS + REGION_METRICS[:2] + [("facedyn_psnr", "face PSNR-Y (per-frame)"),
                                                  ("ssim_pblk", "SSIM p5-block"), ("ssim_roiw", "SSIM ROI-weighted")]
    for c in cfg.clips:
        x = b[(b["clip"] == c) & (b.codec == "x265")].sort_values("kbps")
        a = b[(b["clip"] == c) & (b.codec == "svtav1")].sort_values("kbps")
        if len(x) < 2 or len(a) < 2:
            continue
        for col, lab in cols:
            if x[col].isna().any() or a[col].isna().any():
                continue
            rows.append(dict(clip=c, metric=lab, bd_rate_pct=bd_rate(x.kbps_actual, x[col], a.kbps_actual, a[col]),
                             bd_rate_poly_pct=bd_rate(x.kbps_actual, x[col], a.kbps_actual, a[col], "poly"),
                             overlap=overlap(x[col], a[col])))
    return pd.DataFrame(rows)


def table_hidden(cfg, s):
    b = base(s)
    cols = ["clip", "codec", "kbps_actual", "vmaf", "vmaf_p5", "ov_vmaf", "ov_psnr", "psnr_y",
            "face_vmaf", "facedyn_psnr", "ocr_exact", "ocr_exact_change", "ocr_exact_steady", "ocr_exact_ref",
            "new_misread", "hidden_frac"]
    t = b[[c for c in cols if c in b]].copy()
    t["gap_vmaf"] = t.vmaf - t.ov_vmaf
    t["gap_psnr"] = t.ov_psnr - t.psnr_y
    return t


def table_equal_vmaf(cfg, s):
    """Region quality of SVT-AV1 minus x265 at EQUAL frame VMAF (what the frame score cannot tell apart)."""
    b = base(s)
    rows = []
    for c in cfg.clips:
        x = b[(b["clip"] == c) & (b.codec == "x265")]
        a = b[(b["clip"] == c) & (b.codec == "svtav1")]
        if len(x) < 2 or len(a) < 2:
            continue
        d = dict(clip=c, vmaf_range=f"{max(x.vmaf.min(), a.vmaf.min()):.0f}-{min(x.vmaf.max(), a.vmaf.max()):.0f}")
        for col, lab in [("psnr_y", "d frame PSNR dB"), ("ov_psnr", "d overlay PSNR dB"), ("ov_vmaf", "d overlay VMAF"),
                         ("facedyn_psnr", "d face PSNR dB"), ("ocr_exact", "d OCR exact")]:
            if x[col].notna().all() and a[col].notna().all():
                d[lab] = delta_at_equal(x.vmaf, x[col], a.vmaf, a[col])
        rows.append(d)
    return pd.DataFrame(rows)


def frame_level_hidden(cfg, fr):
    """Frames where frame VMAF looks fine but the scoreboard is misread by OCR."""
    h = cfg["metrics"]["hidden"]
    f = fr[(fr["mode"] == "base") & (fr.rep == 0) & fr.ocr_cer.notna()]
    rows = []
    for (c, codec), g in f.groupby(["clip", "codec"]):
        good = g[g.vmaf >= h["frame_vmaf_min"]]
        ch, st = good[good.clock_change], good[~good.clock_change]
        nm = good.new_misread.astype(float)
        rows.append(dict(clip=c, codec=codec, ocr_frames=len(g), frames_vmaf_ok=len(good),
                         misread_when_vmaf_ok=int((good.ocr_cer > 0).sum()),
                         misread_rate_when_vmaf_ok=float((good.ocr_cer > 0).mean()) if len(good) else np.nan,
                         misread_rate_vmaf_ok_clock_change=float((ch.ocr_cer > 0).mean()) if len(ch) else np.nan,
                         misread_rate_vmaf_ok_steady=float((st.ocr_cer > 0).mean()) if len(st) else np.nan,
                         misread_rate_all=float((g.ocr_cer > 0).mean()),
                         new_misreads_when_vmaf_ok=int(nm.sum()),
                         new_misread_rate_when_vmaf_ok=float(nm.mean()) if len(good) else np.nan,
                         new_misread_rate_all=float(g.new_misread.astype(float).mean())))
    return pd.DataFrame(rows)


def table_roi(cfg, s):
    """Part 2: ROI encode minus x265 baseline at the same target bitrate."""
    b = base(s)
    b = b[b.codec == "x265"].set_index(["clip", "kbps"])
    r = s[s["mode"] == "roi"]
    keys = ["kbps_actual", "vmaf", "psnr_y", "ssim_mean", "ssim_bg", "xpsnr_y", "ov_vmaf", "ov_psnr", "ov_ssim",
            "face_vmaf", "facedyn_psnr", "facedyn_ssim", "ocr_exact", "ocr_exact_change", "new_misread", "ssim_pblk", "ssim_roiw",
            "ssim_roi"]
    rows = []
    for _, x in r.iterrows():
        y = b.loc[(x["clip"], x.kbps)]
        d = dict(clip=x["clip"], kbps=x.kbps, qoffset=x.qoffset, dQP=round(x.qoffset * 51, 1))
        for k in keys:
            if k in x and pd.notna(x[k]) and pd.notna(y[k]):
                d["d_" + k] = x[k] - y[k]
        d["rate_err_pct"] = 100 * (x.kbps_actual / y.kbps_actual - 1)
        rows.append(d)
    return pd.DataFrame(rows)


def table_roi_bd(cfg, s):
    """BD-rate / BD-quality of ROI encodes vs x265 baseline, frame cost vs region gain."""
    b = base(s)
    rows = []
    for c in cfg.clips:
        x = b[(b["clip"] == c) & (b.codec == "x265")].sort_values("kbps")
        for q in cfg["encode"]["roi"]["qoffsets"]:
            r = s[(s["clip"] == c) & (s["mode"] == "roi") & np.isclose(s.qoffset, q)].sort_values("kbps")
            if len(r) < 2 or len(x) < 2:
                continue
            d = dict(clip=c, qoffset=q, dQP=round(q * 51, 1))
            for col, lab in [("vmaf", "frame VMAF"), ("ov_vmaf", "overlay VMAF"), ("ov_psnr", "overlay PSNR"),
                             ("facedyn_psnr", "face PSNR")]:
                if x[col].isna().any() or r[col].isna().any():
                    continue
                d[f"BDrate {lab} %"] = bd_rate(x.kbps_actual, x[col], r.kbps_actual, r[col])
                d[f"BDq {lab}"] = bd_quality(x.kbps_actual, x[col], r.kbps_actual, r[col])
            rows.append(d)
    return pd.DataFrame(rows)


def table_pooling(cfg, roi_t, s):
    """Does each frame-level/pooled metric see the overlay improvement of ROI encoding at equal rate?

    For every (clip, rate, strength) pair, the 'truth' direction is the sign of the overlay PSNR change
    (47 of 48 pairs improve the overlay; the test is whether the metric agrees or calls the
    ROI encode worse). Also Spearman rho, within clip, of each metric with overlay PSNR across all
    x265 encodes (base + ROI) at the same 4 rates.
    """
    cands = [("vmaf", "frame VMAF"), ("psnr_y", "frame PSNR-Y"), ("xpsnr_y", "frame XPSNR-Y"),
             ("ssim_mean", "SSIM mean"), ("ssim_pblk", "SSIM p5-block"), ("ssim_roiw", "SSIM ROI-weighted"),
             ("ssim_roi", "SSIM ROI only")]
    rows = []
    for col, lab in cands:
        dcol = "d_" + col
        if dcol not in roi_t:
            continue
        t = roi_t[roi_t.d_ov_psnr > 0]
        agree = (np.sign(t[dcol]) == np.sign(t.d_ov_psnr)).mean()
        rhos = []
        for c in cfg.clips:
            x = s[(s["clip"] == c) & (s.codec == "x265") & (s.rep == 0)]
            for k, g in x.groupby("kbps"):
                if len(g) >= 3 and g[col].notna().all():
                    rhos.append(spearmanr(g[col], g.ov_psnr).statistic)
        rows.append(dict(metric=lab, sees_overlay_gain=agree, n_pairs=len(t),
                         rho_vs_overlay_psnr_same_rate=np.nanmean(rhos) if rhos else np.nan))
    return pd.DataFrame(rows)


def table_svtapp(cfg, s):
    """Extension: SVT-AV1 static vs per-frame ROI maps (CRF sweeps), BD vs the app's own baseline."""
    a = s[s.codec == "svtapp"]
    rows = []
    for c in cfg.clips:
        b = a[(a["clip"] == c) & (a["mode"] == "app-base")].sort_values("crf")
        if len(b) < 2:
            continue
        for (mode, off), r in a[(a["clip"] == c) & (a["mode"] != "app-base")].groupby(["mode", "qoffset"]):
            r = r.sort_values("crf")
            d = dict(clip=c, map=mode[4:], qindex_offset=int(off))
            for col, lab in [("vmaf", "frame VMAF"), ("ov_psnr", "overlay PSNR"), ("facedyn_psnr", "face PSNR"),
                             ("ssim_bg", "background SSIM")]:
                if b[col].isna().any() or r[col].isna().any():
                    continue
                d[f"BDrate {lab} %"] = bd_rate(b.kbps_actual, b[col], r.kbps_actual, r[col])
            d["d_ocr_exact_pp"] = 100 * (r.ocr_exact.mean() - b.ocr_exact.mean())
            d["d_kbps_pct"] = 100 * (r.kbps_actual.values / b.kbps_actual.values - 1).mean()
            rows.append(d)
    return pd.DataFrame(rows)


def table_noise(cfg, s):
    rows = []
    rep = s[(s["mode"] == "base")]
    for (c, codec, k), g in rep.groupby(["clip", "codec", "kbps"]):
        if len(g) < 2:
            continue
        rows.append(dict(clip=c, codec=codec, kbps=k, n=len(g),
                         sd_kbps_pct=100 * g.kbps_actual.std() / g.kbps_actual.mean(),
                         sd_vmaf=g.vmaf.std(), sd_ov_vmaf=g.ov_vmaf.std(), sd_ov_psnr=g.ov_psnr.std(),
                         sd_ocr_exact=g.ocr_exact.std()))
    return pd.DataFrame(rows)


def table_readme(cfg, s, rung=1):
    """Global vs ROI at one ladder rung: x265, x265 + ROI map (middle strength), SVT-AV1."""
    from .encode import ladders
    lad = ladders(cfg)
    q = cfg["encode"]["roi"]["qoffsets"][1]
    rows = []
    for c in cfg.clips:
        k = lad[c][rung]
        sel = {"x265": s[(s["clip"] == c) & (s.codec == "x265") & (s["mode"] == "base") & (s.rep == 0) & (s.kbps == k)],
               f"x265 + ROI (dQP {q * 51:.0f})": s[(s["clip"] == c) & (s["mode"] == "roi") & (s.kbps == k) & np.isclose(s.qoffset, q)],
               "SVT-AV1": s[(s["clip"] == c) & (s.codec == "svtav1") & (s.rep == 0) & (s.kbps == k)]}
        for name, r in sel.items():
            if r.empty:
                continue
            r = r.iloc[0]
            rows.append({"clip": c, "encode": name, "Mbps": r.kbps_actual / 1000, "frame VMAF": r.vmaf,
                         "frame PSNR": r.psnr_y, "scoreboard PSNR": r.ov_psnr, "scoreboard VMAF": r.ov_vmaf,
                         "face PSNR": r.facedyn_psnr, "OCR exact %": 100 * r.ocr_exact,
                         "OCR exact % (uncompressed)": 100 * r.ocr_exact_ref})
    return pd.DataFrame(rows)


def table_timing(cfg):
    p = cfg.results / "timing.csv"
    if not p.exists():
        return None
    t = pd.read_csv(p)
    t = t[t.metric != "decode only"]
    return (t.groupby("metric").ms_per_frame_net.agg(["mean", "min", "max"]).reset_index()
            .rename(columns={"mean": "ms/frame (mean over clips)", "min": "min", "max": "max"})
            .sort_values("ms/frame (mean over clips)"))


def main(cfg: Cfg, clips=None):
    fr = load_frames(cfg)
    s = summarize(cfg, fr)
    s.to_csv(cfg.results / "encodes.csv", index=False)
    T = cfg.r("tables")
    out = {}
    out["bdrate"] = table_bdrate(cfg, s)
    out["hidden"] = table_hidden(cfg, s)
    out["hidden_frames"] = frame_level_hidden(cfg, fr)
    out["equal_vmaf"] = table_equal_vmaf(cfg, s)
    out["readme_rung2"] = table_readme(cfg, s, 1)
    out["readme_rung1"] = table_readme(cfg, s, 0)
    out["roi"] = table_roi(cfg, s)
    out["roi_bd"] = table_roi_bd(cfg, s)
    out["pooling"] = table_pooling(cfg, out["roi"], s)
    out["noise"] = table_noise(cfg, s)
    if (s.codec == "svtapp").any():
        out["svtapp"] = table_svtapp(cfg, s)
    tm = table_timing(cfg)
    if tm is not None:
        out["timing"] = tm
    for k, v in out.items():
        v.to_csv(T / f"{k}.csv", index=False)
        (T / f"{k}.md").write_text(md(v, ".3f" if k in ("pooling",) else ".2f") + "\n")
    write_report(cfg, s, out)
    log.info("[analyze] %d encodes, %d frames -> %s", len(s), len(fr), cfg.results / "REPORT.md")


def write_report(cfg, s, t):
    L = ["# Results report (auto-generated by `hia.analyze`)", "",
         "Numbers in README findings are taken from this file. All metrics are luma unless noted;",
         "SSIM = full-resolution Gaussian SSIM (Wang 2004). Bitrates are measured, not targets.", ""]
    roi = {c: read_json(cfg.results / "roi" / f"{c}.json")["stats"] for c in cfg.clips
           if (cfg.results / "roi" / f"{c}.json").exists()}
    L += ["## ROI maps", "", md(pd.DataFrame([dict(clip=c, frames_with_face=v["frac_frames_with_face"],
                                                    roi_area_pct=100 * v["roi_area_frac"],
                                                    static_face_px=v["static_face_area_px"],
                                                    mean_face_px=v["mean_face_area_px"]) for c, v in roi.items()])), ""]
    sec = [("bdrate", "1. BD-rate of SVT-AV1 vs x265, by metric (negative = AV1 saves bits)"),
           ("readme_rung2", "0. Global vs ROI at ladder rung 2 (README table)"),
           ("readme_rung1", "0b. Global vs ROI at ladder rung 1 (lowest)"),
           ("hidden", "2. Frame vs region quality, baseline encodes"),
           ("equal_vmaf", "2b. SVT-AV1 minus x265 at equal frame VMAF (region differences the frame score hides)"),
           ("hidden_frames", "3. OCR misreads in frames where frame VMAF >= threshold (new = reference read correctly)"),
           ("pooling", "4. Which metric sees the overlay gain of ROI encoding at equal rate"),
           ("roi", "5. ROI encoding minus baseline (x265, same 2-pass target)"),
           ("roi_bd", "6. ROI encoding: BD-rate / BD-quality vs baseline"),
           ("noise", "7. Run-to-run noise (repeats at ladder point 2)"),
           ("svtapp", "7b. Extension: SVT-AV1 static vs per-frame ROI map (CRF, BD vs no map)"),
           ("timing", "8. Metric compute cost (CPU ms per frame, single thread, net of decoding)")]
    for k, title in sec:
        if k in t:
            L += [f"## {title}", "", md(t[k], ".3f" if k == "pooling" else ".2f"), ""]
    (cfg.results / "REPORT.md").write_text("\n".join(L))
