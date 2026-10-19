# Design notes

Why the experiment is set up the way it is, what was verified about the tools before relying on
them, and the pitfalls found while building. The results are in [README.md](README.md) and
[results/REPORT.md](results/REPORT.md).

## 1. Design choices

1. **5 s clips at 1080p.** UVG sequences are 5 s natively (600 frames at 120 fps), and lossless 1080p
   references take 0.2–0.45 GB each, so 5 s keeps the whole study under a 5 GB disk budget. UVG 120 fps
   is reduced to 60 fps (every other frame), the usual broadcast rate. Clips: Jockey (horse racing,
   motion), Beauty (face close-up), CrowdRun (crowd, texture), TouchdownPass (American football,
   4:2:2 → 4:2:0).
2. **A realistic overlay, not a flat box.** A static opaque box is the easiest case for an encoder
   (static blocks are skipped). Broadcast scoreboards are semi-transparent and carry a running clock.
   The overlay here is a 2-row bar at 70 % opacity (the moving background shows through), with a
   game clock in tenths that changes every 0.1 s. Its position is 16-aligned (640×96 at 64,48), so the
   ROI map and the measurement crop line up with codec blocks.
3. **A task-based check that is not circular.** Saying "region VMAF drops" uses one metric to judge
   another. The overlay text is known exactly per frame, so Tesseract OCR on the decoded scoreboard,
   scored against that text, gives a "can a viewer still read the clock" proxy that no codec was tuned
   for. It is still not a subjective test.
4. **Region metrics beyond VMAF.** VMAF was trained on full frames at a viewing distance; on a
   640×96 crop its multi-scale features run at very low resolution. Region PSNR and SSIM are reported
   next to region VMAF, and conclusions rely on PSNR and OCR where they disagree.
5. **An explicit pooling study.** One SSIM map per frame (luma, Gaussian 11×11, σ = 1.5) is pooled four
   ways: mean (= SSIM), 5th percentile of 64×64 block means, ROI-weighted (50 % ROI / 50 % background)
   and ROI only. Each is tested on whether it sees a known region improvement at equal bitrate, and
   its compute cost is timed. XPSNR (in FFmpeg, activity-weighted) is a cheap perceptual baseline.
6. **Fair rate matching for ROI encoding.** x265 baseline and ROI encodes both use 2-pass ABR at the
   same target, so "same bitrate" holds (rate error ≤ 1.1 % on small-ROI clips). BD-rates always use
   measured bitrates.
7. **An ROI strength sweep, not one value.** qoffset ∈ {−0.1, −0.2, −0.3} (ΔQP ≈ −5, −10, −15) shows the
   trade-off curve rather than one hand-picked point.
8. **Per-clip rate ladders.** One ladder for all clips would put Beauty at VMAF 97 everywhere and
   CrowdRun at VMAF 40. An x265 CRF pilot picks 4 targets per clip aimed at frame VMAF 70/80/88/94.
   They are fixed in `config.yaml`.
9. **A noise floor.** Three repeat encodes per codec at rung 2 show how large a difference has to be
   before it means anything.

## 2. Tool behaviour verified before use

* FFmpeg 7.1.5 ships `libvmaf`, `addroi`, `xpsnr`, `drawtext`, `libx265` and `libsvtav1`
  (SVT-AV1 3.1.2); OpenCV 4.10 runs YuNet through `cv2.FaceDetectorYN` (no onnxruntime needed).
* FFmpeg's libx265 wrapper turns ROI side data into per-block QP offsets: `ΔQP = qoffset × 51` on
  16×16 blocks. It needs AQ enabled (x265's default `aq-mode=2`).
* `addroi` regions are static for a whole clip (its expressions only see `iw`, `ih`), so x265 gets one
  map per clip: the overlay box plus the face pixels present in ≥ 30 % of frames.
* **FFmpeg's libsvtav1 wrapper ignores ROI side data**, and `roi-map-file` is rejected by
  `-svtav1-params` because it is an `SvtAv1EncApp`-only option. The per-frame map extension therefore
  builds `SvtAv1EncApp` from the same 3.1.2 source. Its map gives qindex offsets (−255..255) per
  64×64 superblock, up to 8 distinct values per frame, applied through AV1 segmentation.
* **SVT-AV1 VBR is not bit-exact between identical runs**; x265 is.

## 3. Pitfalls found while building (all fixed before the final run)

* **Luma range.** `ffmpeg -pix_fmt gray` stretches limited-range Y (16–235) to 0–255, which shifted
  region PSNR by up to 1.3 dB. Python metrics read Y with `extractplanes=y` (bit-exact); region PSNR
  then matches libvmaf's to 0.0000 dB.
* **Two different SSIMs.** libvmaf's `float_ssim` downsamples ×4 at 1080p (like Wang's reference code)
  and gave 0.962 where full-resolution SSIM gave 0.782 on the same encode. Full-resolution SSIM is
  used for local pooling and labelled as such.
* **XPSNR crash.** The `settb,setpts=N` chain that aligns the two inputs leaves the link frame rate at
  0/0, and xpsnr divides by it (SIGFPE). An explicit `fps=` restores it. XPSNR's first input is the
  original.
* **OCR sampling bias.** OCR on every 6th frame hit only clock-change frames at 60 fps but a mix at 50
  and 30 fps, which made "misreads cluster on clock changes" look true. With OCR on every frame, misread
  rates on clock-change and steady frames are the same (≈ 1 %). A misread only counts if the
  uncompressed frame is read correctly (TouchdownPass's reference is only 93 % readable).
* **Face area cap.** Beauty's face is about 27 % of the frame, and a 15 % cap silently dropped it. The
  cap is now 40 %, and an oversized single face is shrunk around its centre instead of dropped.
* **SVT-AV1 ROI under VBR.** `--roi-map-file` raised region quality under CRF but not under VBR, where
  frame-level rate control compensates. The extension therefore runs CRF and is compared by BD-rate.
* **pandas `.clip`.** `df.clip` and `row.clip` are pandas methods, not the `clip` column; all access
  uses `["clip"]`.
* **Detached runs.** `nohup` alone did not survive the end of the controlling session, so `run.sh` uses
  `setsid nohup`. The timing stage checkpoints per clip.

## 4. What the hypothesis turned into

The starting hypothesis was "frame VMAF stays high while the scoreboard is damaged". In the strict
sense that was rare: an OCR misread on a frame with frame VMAF ≥ 80 happened in at most 1.5 % of such
frames, and scoreboard VMAF was never below frame VMAF. What the frame score does hide is
**differences** between encodes and codecs in the regions that matter: up to +4.3 dB of scoreboard
PSNR at equal frame VMAF, codec BD-rates that move from −30 % to −47 % (or change sign) with the region
or pooling, and frame metrics that rank equal-rate encodes in the opposite order to their scoreboard
quality. The README findings are written around that.
