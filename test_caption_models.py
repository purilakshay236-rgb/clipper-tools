#!/usr/bin/env python3
"""
test_caption_models.py — run the REAL caption rules against several local models
on REAL transcripts, and score rule compliance + RAM + speed.

Zero API cost: talks only to your local Ollama.

Usage:
    test_caption_models.py [--models qwen3:8b qwen3:4b qwen3:1.7b]
                           [--videos 3] [--outdir ~/clipper-tools/caption-test]
                           [--host 127.0.0.1:11434]
"""

import argparse
import json
import os
import re
import subprocess
import time
import urllib.request

from config import RULES_FILE, DATA_DIR

RULES = RULES_FILE
TRANSCRIPTS = os.path.join(DATA_DIR, "transcripts")
CAPTIONS = os.path.join(DATA_DIR, "captions")

# Exactly the prompt the production watcher sends.
PROMPT = """
Generate ONE Instagram caption from the transcript below.

Follow the caption rules exactly.

CAPTION RULES:

{rules}

TRANSCRIPT:

{transcript}

IMPORTANT:

Return ONLY the finished caption.

Do not explain your answer.

Do not mention these instructions.
"""


def post(host, path, payload, timeout=900):
    req = urllib.request.Request(
        f"http://{host}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def loaded_size_mb(host, model):
    """Size of the loaded model as reported by /api/ps."""
    try:
        d = post(host, "/api/ps", {}) if False else json.loads(
            urllib.request.urlopen(f"http://{host}/api/ps", timeout=10).read().decode())
    except Exception:
        return None
    for m in d.get("models", []):
        if m.get("name") == model or m.get("model") == model:
            return round(m.get("size", 0) / 1024 / 1024)
    return None


def runner_rss_mb():
    """Peak RSS across ollama runner processes — the real memory cost."""
    try:
        out = subprocess.run(["ps", "-eo", "rss,comm"], capture_output=True,
                             text=True).stdout
    except Exception:
        return None
    best = 0
    for line in out.splitlines():
        if "ollama" in line and "runner" in line:
            try:
                best = max(best, int(line.split()[0]))
            except (ValueError, IndexError):
                pass
    return round(best / 1024) or None


def generate(host, model, rules, transcript, sysmsg=False):
    if sysmsg:
        messages = [
            {"role": "system",
             "content": "You write Instagram captions. Follow the rules exactly.\n"
                        "Output ONLY the finished caption — three paragraphs "
                        "separated by blank lines.\n\nRULES:\n" + rules},
            {"role": "user",
             "content": "Write ONE Instagram caption from this transcript.\n\n"
                        "TRANSCRIPT:\n\n" + transcript +
                        "\n\nReturn ONLY the finished caption."},
        ]
    else:
        messages = [{"role": "user",
                     "content": PROMPT.format(rules=rules, transcript=transcript)}]
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,
        "keep_alive": 300,
    }
    t0 = time.time()
    d = post(host, "/api/chat", payload)
    wall = time.time() - t0
    caption = (d.get("message") or {}).get("content", "").strip()
    return caption, wall, d


def unload(host, model):
    try:
        post(host, "/api/generate", {"model": model, "prompt": "", "keep_alive": 0},
             timeout=30)
    except Exception:
        pass


# ---------------------------------------------------------------- scoring ----

def score(caption, transcript):
    paras = [p.strip() for p in re.split(r"\n\s*\n", caption.strip()) if p.strip()]
    checks = {}
    checks["exactly 3 paragraphs"] = len(paras) == 3
    p1 = paras[0] if paras else ""
    p3 = paras[-1] if paras else ""
    checks["opens with one yes/no question"] = bool(
        p1.count("?") == 1 and re.match(
            r"^(Is|Are|Does|Do|Can|Could|Should|Was|Were|Would|Will|Has|Have|Did)\b",
            p1, re.I))
    checks["comment question starts with 💬"] = p3.startswith("💬")
    checks["comment question is a question"] = p3.rstrip().endswith("?")
    body = paras[1] if len(paras) == 3 else (paras[1] if len(paras) > 1 else "")
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", body) if s.strip()]
    checks["explanation ~3 sentences"] = 2 <= len(sentences) <= 4
    checks["no hashtags"] = "#" not in caption
    checks["no asterisks"] = "*" not in caption
    checks["no em/en dash separator"] = ("—" not in caption and "–" not in caption)
    checks["no bullet/numbered list"] = not re.search(r"^\s*([-•]|\d+[.)])\s",
                                                      caption, re.M)
    checks["no PART/Hook labels"] = not re.search(
        r"\b(PART\s*\d|Hook|Explanation|Comment Question)\s*:", caption, re.I)
    checks["no banned words"] = not re.search(r"\b(rape|sexual assault)\b",
                                              caption, re.I)
    checks["no leaked instructions"] = not re.search(
        r"(transcript:|caption rules:|as an ai|here is)", caption, re.I)
    checks["no leaked reasoning"] = not re.search(
        r"(we are given|we need to|let'?s think|central issue|key points|"
        r"part 1|part 2|i need to (create|write)|the speaker is)", caption, re.I)
    checks["no trailing double spaces"] = "  " not in caption
    passed = sum(1 for v in checks.values() if v)
    return passed, len(checks), checks, paras, len(body.split())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+",
                    default=["qwen3:8b", "qwen3:4b", "qwen3:1.7b"])
    ap.add_argument("--videos", type=int, default=3)
    ap.add_argument("--outdir", default=os.path.expanduser("~/clipper-tools/caption-test"))
    ap.add_argument("--host", default="127.0.0.1:11434")
    ap.add_argument("--sysmsg", action="store_true",
                    help="put the rules in a system message (helps small models)")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    logpath = os.path.join(args.outdir, "run.log")
    logf = open(logpath, "a", encoding="utf-8")

    def log(msg=""):
        """stdout may be lost if the run is interrupted — always keep a file."""
        print(msg, flush=True)
        logf.write(msg + "\n")
        logf.flush()

    log(f"\n\n##### run {time.strftime('%Y-%m-%d %H:%M:%S')} "
        f"models={args.models} videos={args.videos}")

    rules = open(RULES, encoding="utf-8").read()

    # newest transcript+caption pairs, longest transcripts first for substance
    pairs = []
    for f in os.listdir(TRANSCRIPTS):
        if not f.endswith(".txt"):
            continue
        base = f[:-4]
        cap = os.path.join(CAPTIONS, base + ".txt")
        if os.path.exists(cap):
            t = open(os.path.join(TRANSCRIPTS, f), encoding="utf-8",
                     errors="replace").read()
            pairs.append((os.path.getmtime(os.path.join(TRANSCRIPTS, f)),
                          base, t, open(cap, encoding="utf-8",
                                        errors="replace").read()))
    pairs.sort(reverse=True)
    pairs = pairs[:args.videos]

    os.makedirs(args.outdir, exist_ok=True)
    report = []
    summary = []

    for model in args.models:
        log(f"\n{'=' * 66}\nMODEL {model}\n{'=' * 66}")
        for _, base, transcript, old_cap in pairs:
            log(f"  generating: {base} ...")
            try:
                caption, wall, raw = generate(args.host, model, rules, transcript,
                                              args.sysmsg)
            except Exception as e:
                log(f"    FAILED: {e}")
                continue
            size = loaded_size_mb(args.host, model)
            rss = runner_rss_mb()
            unload(args.host, model)
            n_pass, n_tot, checks, paras, body_words = score(caption, transcript)
            ev = (raw.get("eval_count") or 0)
            ed = (raw.get("eval_duration") or 1) / 1e9
            tps = round(ev / ed, 1) if ed else 0

            tag = "_sysmsg" if args.sysmsg else ""
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", base)
            out = os.path.join(args.outdir,
                               f"{model.replace(':', '_')}{tag}__{safe}.txt")
            open(out, "w", encoding="utf-8").write(caption)

            summary.append((model, base, n_pass, n_tot, wall, tps, size, rss,
                            body_words, ",".join(k for k, v in checks.items() if not v)))
            report.append({
                "model": model, "video": base, "caption": caption,
                "score": f"{n_pass}/{n_tot}",
                "failed_checks": [k for k, v in checks.items() if not v],
                "wall_seconds": round(wall, 1), "tokens_per_sec": tps,
                "model_size_mb": size, "runner_rss_mb": rss,
                "explanation_words": body_words,
                "production_8b_caption": old_cap,
            })
            log(f"    {n_pass}/{n_tot} rules | {wall:.0f}s | {tps} tok/s | "
                f"model {size}MB | runner RSS {rss}MB")
            for k, v in checks.items():
                if not v:
                    log(f"      ✗ {k}")

    json.dump(report, open(os.path.join(args.outdir, "report.json"), "w"),
              indent=2, ensure_ascii=False)

    log(f"\n\n{'=' * 66}\nSUMMARY  (rules passed / time / speed / RAM / explanation words)\n{'=' * 66}")
    log(f"{'model':<14}{'video':<26}{'rules':>7}{'sec':>7}{'tok/s':>8}"
        f"{'MB':>8}{'RSS':>8}{'words':>7}")
    for model, base, np_, nt, wall, tps, size, rss, bw, failed in summary:
        log(f"{model:<14}{base[:24]:<26}{f'{np_}/{nt}':>7}{wall:>7.0f}"
            f"{tps:>8}{str(size):>8}{str(rss):>8}{bw:>7}")
    log(f"\ncaptions + report.json in {args.outdir}")


if __name__ == "__main__":
    main()
