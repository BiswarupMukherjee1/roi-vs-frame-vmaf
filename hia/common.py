"""Shared helpers: config, paths, logging, subprocess, storage guard."""
from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import time
from fractions import Fraction
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
# keeping every cache inside the project (matplotlib would otherwise write to $XDG_CACHE_HOME or ~)
os.environ.setdefault("MPLCONFIGDIR", str(ROOT / "store" / "cache" / "matplotlib"))
log = logging.getLogger("hia")


class StorageLimit(RuntimeError):
    pass


class Cfg:
    """Thin wrapper around config.yaml with resolved paths."""

    def __init__(self, path: Path = ROOT / "config.yaml"):
        self.path = Path(path)
        self.raw = yaml.safe_load(self.path.read_text())
        p = self.raw["paths"]
        self.store = ROOT / p["store"]
        self.results = ROOT / p["results"]
        self.logs = ROOT / p["logs"]
        for d in (self.store, self.results, self.logs):
            d.mkdir(parents=True, exist_ok=True)

    def __getitem__(self, k):
        return self.raw[k]

    @property
    def clips(self) -> dict:
        return self.raw["clips"]

    # directory layout
    def d(self, *parts) -> Path:
        out = self.store.joinpath(*parts)
        out.mkdir(parents=True, exist_ok=True)
        return out

    def r(self, *parts) -> Path:
        out = self.results.joinpath(*parts)
        out.mkdir(parents=True, exist_ok=True)
        return out

    def ref_path(self, clip: str) -> Path:
        return self.d("refs") / f"{clip}.mkv"

    def ref_meta(self, clip: str) -> dict:
        return json.loads((self.d("refs") / f"{clip}.json").read_text())


def fps_of(v) -> Fraction:
    return Fraction(str(v)).limit_denominator(1001)


def sync_chain(fps) -> str:
    """Filter chain that puts both inputs of a metric on the same frame-index clock.
    The trailing fps= keeps the link frame rate set (xpsnr divides by it)."""
    fr = fps_of(fps)
    return f"settb={fr.denominator}/{fr.numerator},setpts=N,fps={fr.numerator}/{fr.denominator},format=yuv420p"


def setup_logging(cfg: Cfg, name: str = "pipeline"):
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    root = logging.getLogger("hia")
    root.setLevel(logging.INFO)
    if not root.handlers:
        fh = logging.FileHandler(cfg.logs / f"{name}.log")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(fh)
        root.addHandler(sh)


def du_bytes(path: Path) -> int:
    out = subprocess.run(["du", "-sb", str(path)], capture_output=True, text=True, check=True)
    return int(out.stdout.split()[0])


def check_storage(cfg: Cfg, where: str = "") -> float:
    """Return project size in GB; warn above soft limit, raise above hard limit."""
    gb = du_bytes(ROOT) / 1e9
    s = cfg["storage"]
    if gb >= s["hard_gb"]:
        log.error("STORAGE %.2f GB >= hard limit %.1f GB (%s) -> stopping", gb, s["hard_gb"], where)
        raise StorageLimit(f"{gb:.2f} GB")
    if gb >= s["soft_gb"]:
        log.warning("STORAGE %.2f GB >= soft limit %.1f GB (%s)", gb, s["soft_gb"], where)
    return gb


def run(cmd: list[str], *, log_file: Path | None = None, check: bool = True,
        capture: bool = False, **kw) -> tuple[subprocess.CompletedProcess, float]:
    """Run a command, return (result, wall seconds). stderr goes to log_file if given."""
    cmd = [str(c) for c in cmd]
    t0 = time.perf_counter()
    if log_file is not None:
        with open(log_file, "a") as lf:
            lf.write("$ " + shlex.join(cmd) + "\n")
            lf.flush()
            res = subprocess.run(cmd, stdout=subprocess.PIPE if capture else lf,
                                 stderr=lf, text=True, **kw)
    else:
        res = subprocess.run(cmd, capture_output=True, text=True, **kw)
    dt = time.perf_counter() - t0
    if check and res.returncode != 0:
        tail = (res.stderr or "")[-2000:] if log_file is None else f"see {log_file}"
        raise RuntimeError(f"command failed ({res.returncode}): {shlex.join(cmd)}\n{tail}")
    return res, dt


def write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=float))
    tmp.replace(path)


def read_json(path: Path):
    return json.loads(Path(path).read_text())
