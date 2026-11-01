#!/usr/bin/env python3
"""
find_viral.py — find the timestamps of the most-watched moments in a video.

Two signals, both ZERO API cost (no LLM, no cloud calls):

  1. YouTube / yt-dlp-supported URL  -> YouTube "most replayed" heatmap.
     Real viewer re-watch data straight from Google. Authoritative.
  2. Local video file                -> audio-energy heuristic (ffmpeg RMS).
     Applause / shouting / laughter. Weaker, but offline and free.

Usage:
    find_viral.py <URL|FILE> [--url URL] [--top N] [--target SEC]
                             [--min SEC] [--max SEC] [--skip-intro SEC]
                             [--cut] [--outdir DIR] [--csv FILE] [--json]
    find_viral.py --batch urls.txt [--cut] [--outdir DIR] [--csv FILE]

Examples:
    find_viral.py "https://youtube.com/watch?v=XXXX" --top 5
    find_viral.py "https://youtube.com/watch?v=XXXX" --top 3 --cut -o ~/clips
    find_viral.py ~/Downloads/debate.mp4 --url "https://youtu.be/XXXX"   # heatmap + local cut
    find_viral.py --batch urls.txt --cut --outdir ~/clips

Notes:
    --skip-intro defaults to 0. YouTube heatmaps are inflated in the first
    minute (viewers rewind to the start), so 90 is often a good value on
    long videos.
"""

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

from config import OUTPUT_DIR

# ----------------------------------------------------------------------------
# Binary resolution
# ----------------------------------------------------------------------------

YTDLP_CANDIDATES = [
    os.path.expanduser("~/.hermes/skills/media/youtube-content/.venv/bin/yt-dlp"),
    "/opt/homebrew/bin/yt-dlp",
    "/usr/local/bin/yt-dlp",
]
FFMPEG_CANDIDATES = ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg",
                     "/usr/bin/ffmpeg"]
FFPROBE_CANDIDATES = ["/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe",
                      "/usr/bin/ffprobe"]


def resolve(candidates, name):
    for c in candidates:
        if os.path.exists(c) and os.access(c, os.X_OK):
            return c
    return shutil.which(name)


def is_url(s):
    return bool(re.match(r"^https?://", s, re.I))


# ----------------------------------------------------------------------------
# Signals
# ----------------------------------------------------------------------------

def ytdlp_metadata(url, ytdlp):
    out = subprocess.run([ytdlp, "--skip-download", "--no-warnings", "-J", url],
                         capture_output=True, text=True)
    if out.returncode != 0:
        sys.stderr.write(out.stderr[-2000:] + "\n")
        raise RuntimeError(f"yt-dlp failed for {url}")
    return json.loads(out.stdout)


def heatmap_signal(info):
    hm = info.get("heatmap") or []
    dur = float(info.get("duration") or 0)
    buckets = []
    for e in hm:
        try:
            s, en, v = float(e["start_time"]), float(e["end_time"]), float(e["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if en > s:
            buckets.append((s, en, v))
    if not buckets:
        return [], dur
    vmax = max(v for _, _, v in buckets) or 1.0
    buckets = [(s, en, v / vmax) for s, en, v in buckets]
    if not dur:
        dur = buckets[-1][1]
    return buckets, dur


def local_audio_signal(path, ffmpeg, ffprobe):
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found — cannot analyse a local file.")
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-show_entries",
                              "format=duration", "-of", "csv=p=0", path],
                             capture_output=True, text=True)
        dur = float(out.stdout.strip())
    except (ValueError, AttributeError):
        dur = 0.0

    af = ("aresample=16000,asetnsamples=n=16000,"
          "astats=metadata=1:reset=1,"
          "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-")
    proc = subprocess.run([ffmpeg, "-v", "error", "-i", path, "-af", af,
                           "-f", "null", "-"], capture_output=True, text=True)
    t, per_sec = None, []
    for line in (proc.stdout + proc.stderr).splitlines():
        line = line.strip()
        m = re.search(r"pts_time:([0-9.]+)", line)
        if m:
            t = float(m.group(1))
            continue
        m = re.search(r"RMS_level=(-?[0-9.]+|-inf)", line)
        if m and t is not None:
            per_sec.append((t, -90.0 if m.group(1) == "-inf" else float(m.group(1))))
    if not per_sec:
        return [], dur
    dbs = [d for _, d in per_sec]
    lo, hi = min(dbs), max(dbs)
    span = (hi - lo) or 1.0
    buckets = []
    for i, (t, db) in enumerate(per_sec):
        end = per_sec[i + 1][0] if i + 1 < len(per_sec) else (dur or t + 1.0)
        buckets.append((t, end, (db - lo) / span))
    return buckets, dur or per_sec[-1][0]


# ----------------------------------------------------------------------------
# Peak windows
# ----------------------------------------------------------------------------

def sample(buckets, step):
    total = buckets[-1][1]
    n = max(1, int(total / step) + 1)
    grid, hits = [0.0] * n, [0] * n
    for s, e, v in buckets:
        for i in range(max(0, int(s / step)), min(n - 1, int(e / step)) + 1):
            grid[i] += v
            hits[i] += 1
    return [grid[i] / hits[i] if hits[i] else 0.0 for i in range(n)], total


def mean_at(vals, step, start, end):
    i0, i1 = max(0, int(start / step)), min(len(vals) - 1, int(end / step))
    if i1 < i0:
        return 0.0
    seg = vals[i0:i1 + 1]
    return sum(seg) / len(seg)


def natural_end(vals, step, start, min_len, max_len, total):
    """End the clip at a natural break inside [min_len, max_len] from `start`.

    A lull in the signal (quiet audio, or a dip in rewatch activity) is the end
    of a sentence / exchange, which is where a clip should stop. Prefer the first
    genuine pause; if the stretch has no pause at all, fall back to its quietest
    point; if that still fails, run to max_len.
    """
    lo = max(0, int((start + min_len) / step))
    hi = min(len(vals), int(min(total, start + max_len) / step))
    if hi <= lo:
        return min(total, start + max_len)
    seg = vals[lo:hi]
    if not seg:
        return min(total, start + max_len)
    thr = 0.6 * (sum(seg) / len(seg))
    for i, v in enumerate(seg):
        if (v <= thr and v <= seg[max(0, i - 1)] and v <= seg[min(len(seg) - 1, i + 1)]):
            return min(total, (lo + i) * step)
    return min(total, (lo + seg.index(min(seg))) * step)


def find_windows(buckets, top, target, min_len, max_len, gap=15.0, skip_intro=0.0):
    step = 0.5
    vals, total = sample(buckets, step)
    if not vals or total <= 0:
        return []
    for i in range(min(len(vals), int(skip_intro / step))):
        vals[i] = 0.0
    peak = max(vals)
    if peak <= 0:
        return []
    floor = max(0.30 * peak, 0.15)

    seeds = [(vals[i], i * step) for i in range(1, len(vals) - 1)
             if vals[i] >= floor and vals[i] >= vals[i - 1] and vals[i] >= vals[i + 1]]
    if not seeds:
        seeds = [(peak, vals.index(peak) * step)]
    seeds.sort(reverse=True)

    half, chosen = target / 2.0, []
    for _, t in seeds:
        if len(chosen) >= top:
            break
        start = max(0.0, t - half)
        # run to a natural break inside the allowed range instead of a fixed length
        end = natural_end(vals, step, start, min_len, max_len, total)
        if end - start < min_len:
            end = min(total, start + min_len)
        if end - start > max_len:
            end = start + max_len
        if any(not (end <= s - gap or start >= e + gap) for s, e, _ in chosen):
            continue
        chosen.append((start, end, mean_at(vals, step, start, end)))

    chosen.sort(key=lambda x: x[2], reverse=True)
    chosen = [(round(s, 2), round(e, 2), round(sc, 3)) for s, e, sc in chosen[:top]]
    final = []
    for s, e, sc in chosen:
        if any(not (e <= s2 - gap or s >= e2 + gap) for s2, e2, _ in final):
            continue
        final.append((s, e, sc))
    final.sort()
    return final


def snap_to_speech(windows, info):
    subs = [s for s in (info.get("automatic_captions", {}) or {}).get("en", [])
            if s.get("ext") == "json3"]
    if not subs:
        return windows, False
    try:
        import urllib.request
        with urllib.request.urlopen(subs[0]["url"], timeout=30) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return windows, False
    stamps = []
    for ev in data.get("events", []):
        text = "".join(s.get("utf8", "") for s in (ev.get("segs") or [])).strip()
        if text:
            stamps.append((ev.get("tStartMs", 0) / 1000.0, text))
    if not stamps:
        return windows, False
    enders = (".", "?", "!", '"')
    out = []
    for s, e, sc in windows:
        near_s = [t for t, _ in stamps if abs(t - s) <= 3.5]
        near_e = [t for t, txt in stamps if abs(t - e) <= 3.5 and txt.endswith(enders)]
        if near_s:
            s = min(near_s, key=lambda t: abs(t - s))
        if near_e:
            e = min(near_e, key=lambda t: abs(t - e))
        if e - s < 25:
            e = s + 30
        out.append((round(s, 2), round(e, 2), sc))
    return out, True


# ----------------------------------------------------------------------------
# Cutting
# ----------------------------------------------------------------------------

def ts(t, always_hours=False):
    h, m, s = int(t // 3600), int((t % 3600) // 60), t % 60
    return f"{h:02d}:{m:02d}:{s:04.1f}" if (h or always_hours) else f"{m:02d}:{s:04.1f}"


def download(url, dest_dir, ytdlp):
    os.makedirs(dest_dir, exist_ok=True)
    subprocess.run([ytdlp, "-q", "--no-progress",
                    "-f", "bv*[height<=1080]+ba/b[height<=1080]",
                    "--merge-output-format", "mp4",
                    "-o", os.path.join(dest_dir, "src.%(ext)s"), url], check=True)
    for ext in ("mp4", "mkv", "webm"):
        p = os.path.join(dest_dir, f"src.{ext}")
        if os.path.exists(p):
            return p
    raise RuntimeError("download produced no file")


def download_audio(url, dest_dir, ytdlp):
    """Audio only — ~10x smaller than video, and all the energy signal needs."""
    os.makedirs(dest_dir, exist_ok=True)
    subprocess.run([ytdlp, "-q", "--no-progress", "-f", "ba/b",
                    "-o", os.path.join(dest_dir, "src.%(ext)s"), url], check=True)
    for f in os.listdir(dest_dir):
        if f.startswith("src."):
            return os.path.join(dest_dir, f)
    raise RuntimeError("audio download produced no file")


def cut(src, start, end, dest, ffmpeg):
    subprocess.run([ffmpeg, "-y", "-v", "error",
                    "-ss", ts(start), "-to", ts(end), "-i", src,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart", dest], check=True)


def probe_duration(path, ffprobe):
    try:
        return float(subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip())
    except ValueError:
        return 0.0


# ----------------------------------------------------------------------------
# One video
# ----------------------------------------------------------------------------

def analyse(source, url, bins, args):
    """Return a result dict for one video."""
    ytdlp, ffmpeg, ffprobe = bins["ytdlp"], bins["ffmpeg"], bins["ffprobe"]
    url_mode = is_url(source)
    info, local_src, downloaded = {}, None, False
    local_dur = None

    if url_mode:
        if not ytdlp:
            raise RuntimeError("yt-dlp not found.")
        info = ytdlp_metadata(source, ytdlp)
        buckets, dur = heatmap_signal(info)
        mode = "youtube-heatmap"
        if not buckets:
            if args.no_fallback:
                raise RuntimeError("no heatmap available (--no-fallback set)")
            sys.stderr.write("  ! no heatmap for this video — analysing audio energy\n")
            local_src = download_audio(source, tempfile.mkdtemp(prefix="findviral_"),
                                       ytdlp)
            downloaded = True
            buckets, dur = local_audio_signal(local_src, ffmpeg, ffprobe)
            mode = "audio-energy"
    else:
        if not os.path.exists(source):
            raise RuntimeError(f"No such file: {source}")
        local_src = source
        info = {"title": os.path.basename(source)}
        if url and ytdlp:
            # Heatmap from YouTube, clip from the local file. Best of both.
            try:
                info = ytdlp_metadata(url, ytdlp)
                buckets, dur = heatmap_signal(info)
                mode = "youtube-heatmap"
            except Exception as e:
                sys.stderr.write(f"  ! url metadata failed ({e}) — audio energy\n")
                buckets, dur = [], 0.0
            if not buckets:
                mode = "audio-energy"
        else:
            mode = "audio-energy"
        if mode == "audio-energy":
            buckets, dur = local_audio_signal(local_src, ffmpeg, ffprobe)
        local_dur = probe_duration(local_src, ffprobe)

    if not buckets:
        raise RuntimeError("no signal for this video")

    windows = find_windows(buckets, args.top, args.target, args.min_len,
                           args.max_len, args.gap, args.skip_intro)

    # Guard: a URL that does not describe the local file gives wrong timestamps.
    if local_dur and mode == "youtube-heatmap" and dur and local_dur < 0.9 * dur:
        sys.stderr.write(
            f"  !! WARNING: the URL is {dur:.0f}s long but the local file is only "
            f"{local_dur:.0f}s.\n     These are different videos — timestamps past "
            f"{local_dur:.0f}s would be wrong.\n")
        windows = [w for w in windows if w[0] < local_dur]

    snapped = False
    if mode == "youtube-heatmap" and windows:
        windows, snapped = snap_to_speech(windows, info)

    return {
        "source": source, "url": url or (source if url_mode else ""),
        "title": info.get("title", ""), "duration": dur, "mode": mode,
        "snapped_to_speech": snapped,
        "clips": [{"start": s, "end": e, "score": sc, "length": round(e - s, 1)}
                  for s, e, sc in windows],
        "chapters": [{"start": c.get("start_time"), "title": c.get("title")}
                     for c in (info.get("chapters") or [])],
        "_local_src": local_src, "_downloaded": downloaded,
    }


def print_result(res):
    print(f"\n{res['title']}")
    print(f"duration {ts(res['duration'], True)}  |  signal: {res['mode']}  |  "
          f"{'edges snapped to speech' if res['snapped_to_speech'] else 'raw edges'}")
    if not res["clips"]:
        print("  no clear peaks — nothing worth cutting here")
    for i, c in enumerate(res["clips"], 1):
        print(f"  {i}. {ts(c['start'])}  ->  {ts(c['end'])}   "
              f"({c['length']:.0f}s, peak {c['score']:.2f})")
    if res["chapters"]:
        print("  chapters:")
        for c in res["chapters"][:12]:
            print(f"    {ts(c['start'] or 0, True)}  {c['title']}")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("source", nargs="?", help="YouTube URL or local video file")
    ap.add_argument("--batch", help="file with one URL/line per line")
    ap.add_argument("--url", help="heatmap source URL when SOURCE is a local file")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--target", type=float, default=45.0)
    ap.add_argument("--min", dest="min_len", type=float, default=30.0,
                    help="shortest acceptable clip (seconds)")
    ap.add_argument("--max", dest="max_len", type=float, default=150.0,
                    help="longest acceptable clip (seconds)")
    ap.add_argument("--skip-intro", type=float, default=0.0,
                    help="ignore the first N seconds (YouTube intros are inflated)")
    ap.add_argument("--gap", type=float, default=15.0)
    ap.add_argument("--no-fallback", action="store_true",
                    help="skip videos that have no heatmap instead of "
                         "downloading their audio")
    ap.add_argument("--cut", action="store_true")
    ap.add_argument("--outdir", "-o", default=OUTPUT_DIR,
                    help="where to write cut clips")
    ap.add_argument("--csv", dest="csv_out", help="write a timestamps CSV here")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--json-out", dest="json_out",
                    help="write the JSON result to this file while still printing "
                         "the human-readable report (used by the web UI)")
    return ap


def main():
    args = build_parser().parse_args()
    if not args.source and not args.batch:
        build_parser().error("give a SOURCE (URL or file) or --batch FILE")

    bins = {"ytdlp": resolve(YTDLP_CANDIDATES, "yt-dlp"),
            "ffmpeg": resolve(FFMPEG_CANDIDATES, "ffmpeg"),
            "ffprobe": resolve(FFPROBE_CANDIDATES, "ffprobe")}

    if args.batch:
        lines = [l.strip() for l in open(args.batch, encoding="utf-8")
                 if l.strip() and not l.startswith("#")]
    else:
        lines = [args.source]

    results, errors = [], []
    for i, src in enumerate(lines, 1):
        if args.batch:
            print(f"\n[{i}/{len(lines)}] {src}")
        try:
            results.append(analyse(src, args.url if len(lines) == 1 else None,
                                   bins, args))
        except Exception as e:
            print(f"  FAILED: {e}")
            errors.append({"source": src, "error": str(e)})

    payload = {"results": results, "errors": errors}
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        for r in results:
            print_result(r)
        if args.batch and results:
            print(f"\n{'-' * 60}\nBATCH SUMMARY ({len(results)} ok, {len(errors)} failed)")
            print(f"{'start':>9}{'len':>6}  video")
            for r in results:
                for c in r["clips"]:
                    print(f"{ts(c['start']):>9}{c['length']:>6.0f}  "
                          f"{r['title'][:44]}")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

    if args.csv_out:
        with open(args.csv_out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["video", "url", "start", "end", "length_sec",
                        "peak", "signal"])
            for r in results:
                for c in r["clips"]:
                    w.writerow([r["title"], r["url"], ts(c["start"]),
                                ts(c["end"]), c["length"], c["score"], r["mode"]])
        print(f"\ncsv -> {args.csv_out}")

    if args.cut:
        if not bins["ffmpeg"]:
            raise SystemExit("ffmpeg not found — cannot cut.")
        for r in results:
            os.makedirs(args.outdir, exist_ok=True)
            src = r["_local_src"]
            if not src:
                src = download(r["url"] or r["source"], args.outdir, bins["ytdlp"])
            slug = re.sub(r"[^A-Za-z0-9]+", "_", r["title"] or "clip")[:32].strip("_")
            print()
            for i, c in enumerate(r["clips"], 1):
                dest = os.path.join(
                    args.outdir, f"{slug}_{i:02d}_{int(c['start'])}s_{int(c['length'])}s.mp4")
                cut(src, c["start"], c["end"], dest, bins["ffmpeg"])
                print(f"  cut {dest}  ({probe_duration(dest, bins['ffprobe']):.1f}s on disk)")


if __name__ == "__main__":
    main()
