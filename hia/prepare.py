"""Stage 1: fetch sources, cut to ~5 s, burn the synthetic scoreboard, store a lossless reference.

Output per clip (in store/refs/): <clip>.mkv (FFV1, yuv420p, overlay burned in) and <clip>.json
(geometry, fps, frame count, ground-truth overlay text per frame).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .common import Cfg, check_storage, fps_of, log, run, write_json

FONT_SANS = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"


def clock_fps(out_fps) -> int:
    return round(float(fps_of(out_fps)))


def overlay_text(cfg: Cfg, n: int, F: int) -> tuple[str, str]:
    """Ground truth of what drawtext renders at frame n (must match overlay_filter)."""
    o = cfg["overlay"]
    k = (n * 10) // F
    return o["row1"], f"{o['clock_prefix']}{o['clock_start_s'] + k // 10:02d}.{k % 10}"


def overlay_filter(cfg: Cfg, F: int) -> str:
    o = cfg["overlay"]
    x, y, w, h = o["x"], o["y"], o["w"], o["h"]
    k = f"trunc(n*10/{F})"
    prefix = o["clock_prefix"].replace(":", "\\:")
    clock = (f"{prefix}%{{eif\\:{o['clock_start_s']}+trunc({k}/10)\\:d\\:2}}"
             f".%{{eif\\:mod({k}\\,10)\\:d}}")
    parts = [
        f"drawbox=x={x}:y={y}:w={w}:h={h}:color=0x101828@{o['opacity']}:t=fill",
        f"drawbox=x={x}:y={y}:w=8:h={h}:color=0xE11D48@1.0:t=fill",
        f"drawtext=fontfile={FONT_SANS}:text='{o['row1']}':fontsize=34:fontcolor=white:x={x + 28}:y={y + 8}",
        f"drawtext=fontfile={FONT_MONO}:text='{clock}':fontsize=30:fontcolor=0xFACC15:x={x + 28}:y={y + 54}",
    ]
    return ",\n".join(parts)


def _download(url: str, dst: Path, logf: Path):
    if dst.exists() and dst.stat().st_size > 0:
        ok = subprocess.run(["7z", "t", str(dst)], capture_output=True).returncode == 0
        if ok:
            return
    log.info("download %s", url)
    run(["curl", "-L", "--fail", "--retry", "3", "-C", "-", "-o", dst, url], log_file=logf)


def prepare_clip(cfg: Cfg, name: str) -> Path:
    c = cfg.clips[name]
    out = cfg.ref_path(name)
    meta_p = out.with_suffix(".json")
    if out.exists() and meta_p.exists():
        log.info("[prepare] %s: exists", name)
        return out
    check_storage(cfg, f"prepare {name}")
    logf = cfg.logs / f"prepare_{name}.log"
    src_fps, out_fps = fps_of(c["src_fps"]), fps_of(c["out_fps"])
    step = src_fps / out_fps
    assert step.denominator == 1, "src_fps must be an integer multiple of out_fps"
    step = int(step)
    F = clock_fps(out_fps)
    n = int(c["frames"])

    vf = []
    if step > 1:
        vf += [f"select=not(mod(n\\,{step}))", f"setpts=N/({out_fps.numerator}/{out_fps.denominator})/TB"]
    vf += ["format=yuv420p", overlay_filter(cfg, F)]
    script = cfg.d("refs") / f"{name}.filter.txt"
    script.write_text(",\n".join(vf))

    enc = ["-frames:v", n, "-r", f"{out_fps.numerator}/{out_fps.denominator}",
           "-c:v", "ffv1", "-level", "3", "-g", "1", "-slices", "24", "-threads", "16",
           "-an", "-sn", "-y"]
    tmp = out.with_suffix(".tmp.mkv")

    if c["kind"] == "y4m_stream":
        # ffmpeg reads the HTTP stream sequentially and stops after n*step frames: nothing is stored.
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-i", c["url"],
               "-filter_script:v", script, *enc, tmp]
        run(cmd, log_file=logf)
    elif c["kind"] == "uvg7z":
        arc = cfg.d("downloads") / Path(c["url"]).name
        _download(c["url"], arc, logf)
        check_storage(cfg, f"prepare {name} (after download)")
        w, h = 1920, 1080
        sz = f"{w}x{h}"
        with open(logf, "a") as lf:
            p7 = subprocess.Popen(["7z", "e", "-so", str(arc)], stdout=subprocess.PIPE, stderr=lf)
            cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-f", "rawvideo", "-pix_fmt", "yuv420p",
                   "-video_size", sz, "-framerate", str(src_fps), "-i", "pipe:0",
                   "-filter_script:v", str(script), *map(str, enc), str(tmp)]
            lf.write("$ 7z e -so ... | " + " ".join(cmd) + "\n")
            lf.flush()
            pf = subprocess.run(cmd, stdin=p7.stdout, stderr=lf)
            p7.stdout.close()
            p7.kill()
            p7.wait()
        if pf.returncode != 0:
            raise RuntimeError(f"ffmpeg failed for {name}, see {logf}")
        if not cfg["keep_downloads"]:
            log.info("[prepare] %s: removing transient archive %s", name, arc.name)
            arc.unlink()
    else:
        raise ValueError(c["kind"])

    # verify frame count
    res, _ = run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                  "-show_entries", "stream=nb_read_frames,width,height,pix_fmt", "-of", "csv=p=0", tmp])
    width, height, pix, nb = res.stdout.strip().split(",")
    assert int(nb) == n, f"{name}: got {nb} frames, want {n}"
    tmp.replace(out)

    o = cfg["overlay"]
    truth = [list(overlay_text(cfg, i, F)) for i in range(n)]
    write_json(meta_p, dict(name=name, width=int(width), height=int(height), pix_fmt=pix,
                            frames=n, fps=str(out_fps), fps_float=float(out_fps), clock_fps=F,
                            overlay=dict(x=o["x"], y=o["y"], w=o["w"], h=o["h"]),
                            truth=truth, note=c.get("note", "")))
    log.info("[prepare] %s: %d frames %sx%s -> %s (%.0f MB)", name, n, width, height, out.name,
             out.stat().st_size / 1e6)
    return out


def main(cfg: Cfg, clips=None):
    for name in clips or cfg.clips:
        prepare_clip(cfg, name)
