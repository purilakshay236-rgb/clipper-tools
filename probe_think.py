#!/usr/bin/env python3
"""Probe how each qwen3 size behaves with / without the thinking switch."""
import json, os, sys, time, urllib.request

from config import RULES_FILE, DATA_DIR

HOST = "127.0.0.1:11434"
RULES = open(RULES_FILE, encoding="utf-8").read()
TR = open(os.path.join(DATA_DIR, "transcripts", "11'09 bs 3 video.txt"), encoding="utf-8").read()
PROMPT = f"""
Generate ONE Instagram caption from the transcript below.

Follow the caption rules exactly.

CAPTION RULES:

{RULES}

TRANSCRIPT:

{TR}

IMPORTANT:

Return ONLY the finished caption.

Do not explain your answer.

Do not mention these instructions.
"""

LEAK = ["part 1", "part 2", "we are given", "transcript:", "let's think",
        "i need to", "let me ", "here's the", "okay, so i"]


def ask(model, prompt, think, timeout=900):
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "stream": False, "think": think, "keep_alive": 60}
    req = urllib.request.Request(f"http://{HOST}/api/chat",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    d = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
    out = (d.get("message") or {}).get("content", "")
    ec = d.get("eval_count") or 0
    ed = (d.get("eval_duration") or 1) / 1e9
    return out, time.time() - t0, ec, (ec / ed if ed else 0)


VARIANTS = [
    ("qwen3:4b",    "think=false", PROMPT, False),
    ("qwen3:4b",    "/no_think",   PROMPT + "\n/no_think", False),
    ("qwen3:1.7b",  "/no_think",   PROMPT + "\n/no_think", False),
    ("qwen3:8b",    "/no_think",   PROMPT + "\n/no_think", False),
]

for model, label, prompt, think in VARIANTS:
    try:
        out, wall, ec, tps = ask(model, prompt, think)
    except Exception as e:
        print(f"\n### {model} | {label} -> FAILED: {e}", flush=True)
        continue
    leaked = [k for k in LEAK if k in out.lower()]
    print(f"\n{'=' * 66}\n{model}  |  {label}\n{'=' * 66}", flush=True)
    print(f"chars={len(out)}  wall={wall:.0f}s  tokens={ec}  {tps:.1f} tok/s  "
          f"leaked_reasoning={bool(leaked)} {leaked or ''}", flush=True)
    print("-" * 46, flush=True)
    print(out[:1200], flush=True)
    open("/tmp/think_probe_last.txt", "w", encoding="utf-8").write(out)
print("\nPROBE DONE", flush=True)
