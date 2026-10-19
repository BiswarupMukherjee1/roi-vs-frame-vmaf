"""Stage 4: encodes.

  base : x265 (2-pass ABR) and SVT-AV1 (VBR) at the 4 ladder rates of each clip
  roi  : x265 2-pass ABR at the same rates, with addroi QP offsets on overlay + static faces
  rep  : repeats of the base encodes at ladder point 2 (run-to-run noise floor)

Bitstreams: store/bitstreams/<clip>/<tag>.mp4, metadata next to it as <tag>.json.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass

from .common import Cfg, check_storage, log, read_json, run, write_json
from .roi import addroi_filter


@dataclass(frozen=True)
class Job:
    clip: str
    codec: str            # x265 | svtav1
    mode: str             # base | roi | pilot
    kbps: int = 0         # ABR target (0 for CRF)
    qoffset: float = 0.0  # addroi qoffset (roi mode)
    rep: int = 0
    crf: int = -1         # pilot only

    @property
    def tag(self) -> str:
        if self.mode == "pilot":
            return f"{self.codec}-crf{self.crf}"
        q = f"-q{round(abs(self.qoffset) * 100):02d}" if self.mode == "roi" else ""
        return f"{self.codec}-{self.mode}{q}-{self.kbps}k-r{self.rep}"


def ladders(cfg: Cfg) -> dict:
    fixed = cfg["encode"].get("ladders") or {}
    pilot_p = cfg.results / "pilot.json"
    derived = read_json(pilot_p)["ladders"] if pilot_p.exists() else {}
    out = {}
    for c in cfg.clips:
        if c in fixed:
            out[c] = [int(v) for v in fixed[c]]
        elif c in derived:
            out[c] = derived[c]
    return out


def bitstream(cfg: Cfg, job: Job, sub="bitstreams"):
    return cfg.d(sub, job.clip) / f"{job.tag}.mp4"


def stream_bits(path) -> int:
    res, _ = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                  "packet=size", "-of", "csv=p=0", path])
    return 8 * sum(int(x) for x in res.stdout.split())


def encode(cfg: Cfg, job: Job, sub="bitstreams") -> dict:
    out = bitstream(cfg, job, sub)
    meta_p = out.with_suffix(".json")
    if out.exists() and meta_p.exists():
        return read_json(meta_p)
    meta = cfg.ref_meta(job.clip)
    ref = cfg.ref_path(job.clip)
    e = cfg["encode"]
    T = cfg["compute"]["threads_per_job"]
    K = round(e["keyint_s"] * meta["fps_float"])
    logf = cfg.logs / "encode" / f"{job.clip}_{job.tag}.log"
    logf.parent.mkdir(exist_ok=True)
    logf.unlink(missing_ok=True)
    tmp = out.with_suffix(".tmp.mp4")
    base = ["ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-y", "-i", ref]
    vf = []
    if job.mode == "roi":
        roi = read_json(cfg.results / "roi" / f"{job.clip}.json")
        vf = ["-vf", addroi_filter(roi, job.qoffset)]
    t_enc = 0.0
    if job.codec == "x265":
        xp = f"keyint={K}:pools={T}:log-level=warning"
        common = [*vf, "-c:v", "libx265", "-preset", e["x265"]["preset"], "-pix_fmt", "yuv420p", "-an"]
        if job.mode == "pilot":
            _, t_enc = run([*base, *common, "-crf", job.crf, "-x265-params", xp, tmp], log_file=logf)
        elif e["x265"]["two_pass"]:
            stats = cfg.d("tmp") / f"{job.clip}_{job.tag}.stats"
            _, t1 = run([*base, *common, "-b:v", f"{job.kbps}k",
                         "-x265-params", f"{xp}:pass=1:stats={stats}", "-f", "null", "-"], log_file=logf)
            _, t2 = run([*base, *common, "-b:v", f"{job.kbps}k",
                         "-x265-params", f"{xp}:pass=2:stats={stats}", tmp], log_file=logf)
            t_enc = t1 + t2
            for f in stats.parent.glob(stats.name + "*"):
                f.unlink()
        else:
            _, t_enc = run([*base, *common, "-b:v", f"{job.kbps}k", "-x265-params", xp, tmp], log_file=logf)
    elif job.codec == "svtav1":
        if job.mode == "roi":
            raise ValueError("FFmpeg's libsvtav1 wrapper ignores ROI side data")
        rc = ["-crf", job.crf] if job.mode == "pilot" else ["-b:v", f"{job.kbps}k"]
        _, t_enc = run([*base, "-c:v", "libsvtav1", "-preset", e["svtav1"]["preset"], *rc, "-g", K,
                        "-pix_fmt", "yuv420p", "-an", tmp], log_file=logf)
    else:
        raise ValueError(job.codec)
    tmp.replace(out)
    bits = stream_bits(out)
    dur = meta["frames"] / meta["fps_float"]
    res = dict(asdict(job), tag=job.tag, path=str(out.relative_to(cfg.store)), bytes=out.stat().st_size,
               kbps_actual=bits / dur / 1000.0, enc_seconds=t_enc, enc_fps=meta["frames"] / t_enc,
               frames=meta["frames"], keyint=K)
    write_json(meta_p, res)
    log.info("[encode] %-9s %-26s target %5s -> %7.0f kbps  %.0fs", job.clip, job.tag,
             job.kbps or f"crf{job.crf}", res["kbps_actual"], t_enc)
    return res


def all_jobs(cfg: Cfg, clips=None) -> list[Job]:
    lad = ladders(cfg)
    e = cfg["encode"]
    jobs = []
    for c in clips or cfg.clips:
        if c not in lad:
            raise RuntimeError(f"no ladder for {c}: run the pilot stage first")
        L = lad[c]
        for codec in ("x265", "svtav1"):
            jobs += [Job(c, codec, "base", k) for k in L]
        jobs += [Job(c, "x265", "roi", k, q) for q in e["roi"]["qoffsets"] for k in L]
        for codec in ("x265", "svtav1"):
            jobs += [Job(c, codec, "base", L[1], rep=r) for r in range(1, e["repeats"])]
    return jobs


def run_jobs(cfg: Cfg, jobs, sub="bitstreams"):
    done = []
    with ThreadPoolExecutor(cfg["compute"]["jobs"]) as ex:
        futs = {ex.submit(encode, cfg, j, sub): j for j in jobs}
        for f in as_completed(futs):
            done.append(f.result())
            if len(done) % 10 == 0:
                check_storage(cfg, f"encode {len(done)}/{len(jobs)}")
    return done


def main(cfg: Cfg, clips=None):
    jobs = all_jobs(cfg, clips)
    log.info("[encode] %d jobs", len(jobs))
    run_jobs(cfg, jobs)
    (cfg.results / "encode_jobs.json").write_text(json.dumps([j.tag for j in jobs]))
