"""Stage 8: figures for the README (results/figures/*.png)."""
from __future__ import annotations

import subprocess

import matplotlib

matplotlib.use("Agg")
import matplotlib.patches as mpatches  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from .common import Cfg, log, read_json  # noqa: E402
from .metrics import ssim_map  # noqa: E402

# plot colours
SURF, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
SERIES = {"x265": "#2a78d6", "svtav1": "#eb6834", "roi": "#1baf7a"}
LABEL = {"x265": "x265", "svtav1": "SVT-AV1", "roi": "x265 + ROI map"}
ORDINAL = ["#86b6ef", "#2a78d6", "#104281"]
NICE = {"jockey": "Jockey (sport)", "beauty": "Beauty (face)", "crowdrun": "CrowdRun (crowd)",
        "touchdown": "TouchdownPass (sport)"}


def style():
    plt.rcParams.update({
        "figure.facecolor": SURF, "axes.facecolor": SURF, "savefig.facecolor": SURF,
        "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
        "text.color": INK, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
        "axes.spines.top": False, "axes.spines.right": False, "font.size": 10,
        "axes.titlesize": 11, "axes.titleweight": "bold", "legend.frameon": False,
        "lines.linewidth": 2, "lines.markersize": 7})


def grab(path, n, W, H, pix="rgb24"):
    ch = 3 if pix == "rgb24" else 1
    vf = f"select=eq(n\\,{n})" + (",extractplanes=y" if pix == "gray" else "")
    out = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-vf", vf, "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", pix, "-"], capture_output=True, check=True).stdout
    a = np.frombuffer(out, np.uint8)
    return a.reshape(H, W, 3) if ch == 3 else a.reshape(H, W)


def roi_q(cfg):
    return cfg["encode"]["roi"]["qoffsets"][1]


# fig 1: RD curves, global vs region
def fig_rd(cfg, s, out):
    clips = [c for c in cfg.clips if c in set(s["clip"])]
    rows = [("vmaf", "frame VMAF"), ("ov_psnr", "scoreboard PSNR-Y (dB)"), ("ocr_exact", "scoreboard read\nexactly (OCR, %)")]
    fig, axs = plt.subplots(len(rows), len(clips), figsize=(3.4 * len(clips), 2.6 * len(rows)), squeeze=False,
                            sharex="col")
    q = roi_q(cfg)
    for j, c in enumerate(clips):
        d = s[(s["clip"] == c) & (s.rep == 0)]
        series = {"x265": d[(d.codec == "x265") & (d["mode"] == "base")],
                  "svtav1": d[(d.codec == "svtav1") & (d["mode"] == "base")],
                  "roi": d[(d["mode"] == "roi") & np.isclose(d.qoffset, q)]}
        for i, (col, lab) in enumerate(rows):
            ax = axs[i, j]
            if col == "ocr_exact":
                ax.set_ylim(75, 101)
            for k, g in series.items():
                g = g.sort_values("kbps_actual")
                y = g[col] * (100 if col == "ocr_exact" else 1)
                ax.plot(g.kbps_actual / 1000, y, "--o" if k == "roi" else "-o", color=SERIES[k], label=LABEL[k],
                        markeredgecolor=SURF, markeredgewidth=1.5, zorder=3 if k == "roi" else 2)
            if col == "ocr_exact" and pd.notna(d.ocr_exact_ref.iloc[0]):
                yr = 100 * d.ocr_exact_ref.iloc[0]
                ax.axhline(yr, color=INK2, lw=1, ls=":")
                ax.annotate("uncompressed", (1, yr), xycoords=("axes fraction", "data"), xytext=(-2, -3),
                            textcoords="offset points", ha="right", va="top", color=INK2, fontsize=8)
            ax.set_xscale("log")
            ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:g}"))
            ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
            if j == 0:
                ax.set_ylabel(lab)
            if i == 0:
                ax.set_title(NICE.get(c, c))
            if i == len(rows) - 1:
                ax.set_xlabel("bitrate (Mbps, log)")
    for i in range(len(rows) - 1):  # share y per row (OCR row has a fixed range)
        lo = min(a.get_ylim()[0] for a in axs[i])
        hi = max(a.get_ylim()[1] for a in axs[i])
        for a in axs[i]:
            a.set_ylim(lo, hi)
    h, l = axs[0, 0].get_legend_handles_labels()
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.legend(h, [x.replace("ROI map", f"ROI map (ΔQP {q * 51:.0f})") for x in l], loc="upper center",
               ncol=3, bbox_to_anchor=(0.5, 1.0))
    fig.savefig(out, dpi=150)
    plt.close(fig)


# fig 2: the damage frame
def pick_damage_frame(cfg, fr):
    """A frame where x265 baseline and x265+ROI (same target bitrate) have ~equal frame VMAF
    (>= threshold) but the scoreboard differs most. Small-ROI clips only (the close-up is a different case)."""
    q = roi_q(cfg)
    h = cfg["metrics"]["hidden"]
    key = ["clip", "kbps", "frame"]
    cols = key + ["tag", "vmaf", "ov_psnr", "ov_vmaf", "ocr_text", "ocr_cer", "psnr_y"]
    b = fr[(fr.codec == "x265") & (fr["mode"] == "base") & (fr.rep == 0)][cols]
    r = fr[(fr["mode"] == "roi") & np.isclose(fr.qoffset, q)][cols]
    m = b.merge(r, on=key, suffixes=("_b", "_r"))
    m = m[(m.vmaf_b >= h["frame_vmaf_min"]) & ((m.vmaf_b - m.vmaf_r).abs() < 1.0) & (m["clip"] != "beauty")]
    return m.assign(gain=m.ov_psnr_r - m.ov_psnr_b).sort_values("gain").iloc[-1]


def fig_damage(cfg, fr, s, out):
    r = pick_damage_frame(cfg, fr)
    clip, n = r["clip"], int(r["frame"])
    meta = cfg.ref_meta(clip)
    W, H = meta["width"], meta["height"]
    x, y, w, h = meta["overlay"]["x"], meta["overlay"]["y"], meta["overlay"]["w"], meta["overlay"]["h"]
    ref = cfg.ref_path(clip)
    path = lambda tag: cfg.store / read_json(cfg.store / "bitstreams" / clip / f"{tag}.json")["path"]
    g_ref = grab(ref, n, W, H, "gray")
    rows = [("Reference", ref, None), ("x265 baseline", path(r.tag_b), "_b"),
            (f"x265 + ROI map (ΔQP {roi_q(cfg) * 51:.0f}), same bitrate", path(r.tag_r), "_r")]
    # zoom on the text part of the scoreboard (clock included) so coding artifacts are visible
    zx0, zx1 = x, x + 400
    crop = lambda a: a[y:y + h, zx0:zx1]
    VMAX = 0.3

    fig = plt.figure(figsize=(12, 10.5))
    gs = fig.add_gridspec(4, 2, height_ratios=[1.35, 1, 1, 1], hspace=0.42, wspace=0.05)
    rgb_b = grab(rows[1][1], n, W, H)
    m_b = ssim_map(g_ref, grab(rows[1][1], n, W, H, "gray"))
    ax = fig.add_subplot(gs[0, 0])
    ax.imshow(rgb_b)
    ax.add_patch(mpatches.Rectangle((x, y), w, h, fill=False, ec="#eda100", lw=2))
    ax.set_title(f"{NICE.get(clip, clip)} · frame {n} · x265 @ {r.kbps / 1000:g} Mbps", loc="left", fontsize=10)
    ax.axis("off")
    ax2 = fig.add_subplot(gs[0, 1])
    im = ax2.imshow(1 - m_b, cmap="Blues", vmin=0, vmax=VMAX)
    ax2.add_patch(mpatches.Rectangle((x, y), w, h, fill=False, ec="#eda100", lw=1.5))
    ax2.set_title("Damage map, 1 − SSIM (darker = worse)", loc="left", fontsize=10)
    ax2.axis("off")
    fig.colorbar(im, ax=ax2, fraction=0.03, pad=0.01)
    for k, (ttl, pth, sfx) in enumerate(rows, start=1):
        a = fig.add_subplot(gs[k, 0])
        a.imshow(crop(grab(pth, n, W, H)), interpolation="nearest")
        a.axis("off")
        a2 = fig.add_subplot(gs[k, 1])
        a2.axis("off")
        if sfx is None:
            a.set_title(ttl + " (zoomed)", loc="left", fontsize=10)
            a2.text(0.0, 0.5, "scoreboard text:\n" + " ".join("  ".join(meta["truth"][n]).split()), fontsize=11,
                    family="monospace", va="center", transform=a2.transAxes, color=INK)
            continue
        m = ssim_map(g_ref, grab(pth, n, W, H, "gray"))
        a2.imshow(crop(1 - m), cmap="Blues", vmin=0, vmax=VMAX, interpolation="nearest")
        a.set_title(f"{ttl}\nframe VMAF {r['vmaf' + sfx]:.1f}   ·   scoreboard PSNR {r['ov_psnr' + sfx]:.1f} dB",
                    loc="left", fontsize=10)
        a2.set_title("scoreboard damage map", loc="left", fontsize=10, color=INK2)
    fig.savefig(out, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return dict(clip=clip, frame=n, kbps=int(r.kbps), tag_base=r.tag_b, tag_roi=r.tag_r,
                vmaf_base=float(r.vmaf_b), vmaf_roi=float(r.vmaf_r), ov_psnr_base=float(r.ov_psnr_b),
                ov_psnr_roi=float(r.ov_psnr_r), ov_vmaf_base=float(r.ov_vmaf_b), ov_vmaf_roi=float(r.ov_vmaf_r),
                ocr_base=str(r.ocr_text_b), ocr_roi=str(r.ocr_text_r), truth="  ".join(meta["truth"][n]))


# fig 3: ROI trade-off
def fig_roi(cfg, roi_t, out):
    clips = [c for c in cfg.clips if c in set(roi_t["clip"])]
    qs = sorted(roi_t.qoffset.unique(), reverse=True)
    fig, axs = plt.subplots(1, len(clips), figsize=(3.4 * len(clips), 3.3), squeeze=False)
    for j, c in enumerate(clips):
        ax = axs[0, j]
        d = roi_t[roi_t["clip"] == c]
        for k, q in enumerate(qs):
            g = d[np.isclose(d.qoffset, q)].sort_values("kbps")
            ax.plot(g.d_vmaf, g.d_ov_psnr, "o", color=ORDINAL[k % 3], markersize=8,
                    markeredgecolor=SURF, markeredgewidth=1.5, label=f"ΔQP {q * 51:.0f}")
        ax.axhline(0, color=INK2, lw=0.8)
        ax.axvline(0, color=INK2, lw=0.8)
        ax.set_title(NICE.get(c, c))
        ax.set_xlabel("Δ frame VMAF (cost)")
        if j == 0:
            ax.set_ylabel("Δ overlay PSNR, dB (gain)")
    h, l = axs[0, 0].get_legend_handles_labels()
    fig.legend(h, l, loc="upper center", ncol=len(qs), bbox_to_anchor=(0.5, 1.04))
    fig.text(0.5, -0.02, "each point = one target bitrate; ROI and baseline are x265 2-pass at the same target",
             ha="center", color=INK2, fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)


#  fig 0: ROI maps
def fig_maps(cfg, out):
    clips = list(cfg.clips)
    fig, axs = plt.subplots(1, len(clips), figsize=(3.6 * len(clips), 2.4), squeeze=False)
    for j, c in enumerate(clips):
        meta = cfg.ref_meta(c)
        roi = read_json(cfg.results / "roi" / f"{c}.json")
        n = meta["frames"] // 2
        ax = axs[0, j]
        ax.imshow(grab(cfg.ref_path(c), n, meta["width"], meta["height"]))
        for b in roi["faces_per_frame"][n]:
            ax.add_patch(mpatches.Rectangle(b[:2], b[2], b[3], fill=False, ec="#1baf7a", lw=1, ls="--"))
        for b in roi["roi_rects"]:
            ax.add_patch(mpatches.Rectangle(b[:2], b[2], b[3], fill=False, ec="#eda100", lw=2))
        ax.set_title(NICE.get(c, c), fontsize=10)
        ax.axis("off")
    handles = [mpatches.Patch(fill=False, ec="#eda100", lw=2, label="static ROI map (to x265)"),
               mpatches.Patch(fill=False, ec="#1baf7a", lw=1, ls="--", label="YuNet faces in this frame")]
    fig.legend(handles=handles, loc="upper center", ncol=2, bbox_to_anchor=(0.5, 1.16))
    fig.tight_layout()
    fig.savefig(out, dpi=120, bbox_inches="tight")
    plt.close(fig)


def main(cfg: Cfg, clips=None):
    from .analyze import load_frames
    style()
    F = cfg.r("figures")
    s = pd.read_csv(cfg.results / "encodes.csv")
    fr = load_frames(cfg)
    roi_t = pd.read_csv(cfg.results / "tables" / "roi.csv")
    fig_maps(cfg, F / "fig0_roi_maps.png")
    fig_rd(cfg, s, F / "fig1_rd_global_vs_region.png")
    info = fig_damage(cfg, fr, s, F / "fig2_damage_frame.png")
    fig_roi(cfg, roi_t, F / "fig3_roi_tradeoff.png")
    pd.Series(info).to_json(F / "fig2_damage_frame.json", indent=1)
    log.info("[figures] wrote %s (damage frame: %s)", F, info)
