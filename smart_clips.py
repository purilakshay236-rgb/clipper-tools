#!/usr/bin/env python3
"""
smart_clips.py — find clip-worthy moments by READING THE TRANSCRIPT.  (v2)

Why this exists: YouTube's "most replayed" heatmap measures *attention*, not
shareability. People rewind to re-hear a number or catch a missed line, which
says nothing about whether a clip would stop a scroll. And it is deaf/blind to
hooks, clashes, absurd lines and self-contained payoffs.

How v2 differs from v1 (the "moment does not exist" problem):

  v1 judged each 6-minute chunk IN ISOLATION against a list of rules that ALL
  had to pass, told the model to be "RUTHLESS" and that an empty list was the
  right answer, then silently dropped every pick whose quoted line it could not
  find in the transcript. Result: on most videos nothing survived, and the user
  was told the video had no moments.

  v2 makes four changes:

  1. CONTEXT FIRST. One cheap pass reads a condensed version of the whole
     transcript and writes a briefing (what the video is about, who is talking,
     the running arguments, which stretches look charged). Every per-chunk
     judgement then receives that briefing, so a line is scored against the
     video's actual story instead of in a vacuum.
  2. RANK, DON'T REJECT. The per-chunk prompt asks for a 0-10 score and labels
     the KIND of moment (clash / absurd / reveal / emotion / quotable / stakes).
     Weak-but-real moments come back as low scores instead of being thrown away,
     and the results are tiered (strong / ok / mild) rather than filtered.
  3. NEVER EMPTY. If a first pass finds nothing, a looser second-chance pass runs
     before any "no moments" verdict. If that also fails on a YouTube link, the
     most-replayed stretches are offered as a labelled fallback.
  4. MERGING. Two moments that belong together are combined instead of one being
     discarded: overlapping / touching windows are unioned, and two strong
     moments separated by filler become ONE clip (cut both parts, concatenate).

Transcript source, automatically:
  YouTube URL  -> the video's own / auto captions via yt-dlp (fast, free)
  local file   -> mlx_whisper (the same whisper-turbo your caption pipeline uses)

Usage
  smart_clips.py "URL"
  smart_clips.py "URL" --cut                 # also cut the mp4s
  smart_clips.py "URL" --no-hybrid           # transcript judgement only
  smart_clips.py "URL" --no-stitch           # don't merge far-apart moments
  smart_clips.py "local.mp4" --top 4 --workers 4
"""

import argparse
import concurrent.futures as futures
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

YTDLP = os.path.expanduser(
    "~/.hermes/skills/media/youtube-content/.venv/bin/yt-dlp")
FINDER = os.path.join(HERE, "find_viral.py")
from config import MODELS_DIR, FFMPEG_BIN, FFPROBE_BIN, OLLAMA_BIN, OUTPUT_DIR
FFMPEG = FFMPEG_BIN
FFPROBE = FFPROBE_BIN
OLLAMA = OLLAMA_BIN
DEFAULT_OLLAMA = "127.0.0.1:11434"
HF_HOME = MODELS_DIR

# --------------------------------------------------------------- prompts

# PASS 1 — the briefing. Written to be cheap: it only summarises.
CONTEXT_PROMPT = """You are briefing a short-form video clipper who is about to cut TikTok / Reels / Shorts clips out of ONE long video.

Below is a THINNED transcript of that video: one line per ~20 seconds, each with a [mm:ss] stamp.

---
{body}
---

Return ONLY this JSON, no prose, no markdown fences:
{{"subject": "<what this video is really about, max 14 words>",
  "arena": "<debate | interview | podcast | commentary | hearing | tutorial | other>",
  "voices": ["<who is speaking, up to 4 names>"],
  "threads": ["<3-6 arguments or storylines that keep coming back, max 9 words each>"],
  "hotspots": ["<up to 8 [mm:ss] stamps of the stretches that look most charged, quotable or confrontational>"],
  "what_travels": "<one line: which KIND of moment from THIS video would travel on TikTok and why>"}}"""

# PASS 2 — the per-chunk judgement. Ranks and scores; does not filter.
RULES = """You are an editor choosing moments out of a long video that would work as STANDALONE short clips (TikTok / Reels / Shorts).

WHAT THIS VIDEO IS (worked out already from the whole transcript):
{context}

YOUR CHUNK OF THE TRANSCRIPT (every line starts with a [mm:ss] stamp):
---
{chunk}
---

Rank, don't filter. A moment qualifies if a stranger scrolling past would keep watching because of ONE of these:
- clash   — two people disagreeing, interrupting, someone losing patience.
- absurd  — an outrageous claim, a contradiction, an insult, a wild comparison, an unguarded admission.
- reveal  — a number, a fact or an admission that lands as a surprise.
- emotion — real anger, laughter, a stumble, a voice breaking.
- quotable— a line that works on its own as a caption.
- stakes  — a concrete consequence for a named person or group.

Setup, explanation, statistics with no payoff, housekeeping, ads, intros and outros are NOT clips.

HOW TO SCORE (0-10, how likely this stops a scroll) — calibrate honestly:
  9-10 = once in the whole video. At most ONE moment in this chunk may get 9+.
  6-8  = strong; a real viewer reaction.
  4-5  = decent, worth offering.
  1-3  = weak but mentionable.
Most chunks are decent rather than remarkable: a true 5 beats a fake 9. If you cannot honestly say a moment would stop a scroll, score it 3-5 and say so in "why".

RULES:
- Return up to {k} moments, BEST FIRST.
- Do NOT return two moments that overlap. If two candidates overlap, return ONE spanning both.
- Most chunks with conversation contain at least one 4+ moment. Return your best one even if the chunk is mild.
- Return [] ONLY if the chunk is silence, an ad / sponsor read, or pure housekeeping.
- Start AND end on complete sentences, never mid-clause. The first line must land with no setup.
- Clip length must be between {lo:.0f} and {hi:.0f} seconds; if the interesting stretch is longer, take the tightest part that still contains both the setup line and the payoff.
- COPY the [mm:ss] labels character for character. Never calculate, estimate or invent a timestamp.

Return ONLY a JSON array:
[{{"at": "<exact [mm:ss] label of the clip's FIRST line>",
  "to": "<exact [mm:ss] label of the clip's LAST line>",
  "hook": "<the opening spoken line, copied word for word>",
  "last": "<the final spoken line of the clip, copied word for word>",
  "score": <0-10>,
  "kind": "<clash|absurd|reveal|emotion|quotable|stakes>",
  "why": "<max 12 words: why this travels>",
  "title": "<max 6 word label>"}}]
[] if nothing at all qualifies."""

# The second-chance prompt: no scoring theatre, just "find me the best bit".
LOOSE_PROMPT = """Below is a chunk of a longer video's transcript. Every line starts with a [mm:ss] stamp.

WHAT THE VIDEO IS ABOUT:
{context}

---
{chunk}
---

Name the single most interesting moment in this chunk — the place where a scrolling viewer would most likely keep watching. It may be a clash, a joke, a shock number, a personal story, an admission, something absurd or just a strong opinion. Do NOT judge strictness, do NOT return an empty list: pick the best available moment even if it is only mildly interesting.

Return ONLY a JSON array with at most {k} item(s):
[{{"at": "<exact [mm:ss] label of the FIRST line>",
  "to": "<exact [mm:ss] label of the LAST line>",
  "hook": "<the first spoken line, word for word>",
  "last": "<the last spoken line, word for word>",
  "score": <0-10>,
  "kind": "<clash|absurd|reveal|emotion|quotable|stakes|mild>",
  "why": "<max 12 words>",
  "title": "<max 6 word label>"}}]"""


# ------------------------------------------------------------------ utils

def sh(cmd, timeout=1800, **kw):
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, **kw)


def mmss(t):
    t = max(0.0, float(t))
    m, s = divmod(int(t), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def log(msg):
    print(msg, flush=True)


def is_url(s):
    return bool(re.match(r"^https?://", s or "", re.I))


def normalize(s):
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).strip()


# ------------------------------------------------------- transcript: SRT

def parse_srt(path):
    """Parse YouTube auto-caption SRT into clean timestamped sentences.

    Two artefacts have to be handled or the transcript is unusable (~43% of it
    duplicated, which makes any model judge the text garbage and return []):

    1. 'pin' cues of ~10ms (e.g. 00:30:09,270 --> 00:30:09,280) that merely
       repeat the previous line. These are over half of all cues — drop them.
    2. Rolling cues show [tail of previous line][new line], so only the line
       that has not already been shown is new.
    """
    raw = open(path, encoding="utf-8", errors="replace").read()
    blocks = re.split(r"\n\s*\n", raw.strip())
    frags, recent, pins = [], [], 0
    for b in blocks:
        lines = [l.rstrip() for l in b.splitlines()]
        ti = next((i for i, l in enumerate(lines) if " --> " in l), None)
        if ti is None:
            continue
        m = re.match(r"(\d+):(\d+):([\d,.]+)\s*-->\s*(\d+):(\d+):([\d,.]+)",
                     lines[ti])
        if not m:
            continue
        g = m.groups()
        st = int(g[0]) * 3600 + int(g[1]) * 60 + float(g[2].replace(",", "."))
        en = int(g[3]) * 3600 + int(g[4]) * 60 + float(g[5].replace(",", "."))
        if (en - st) < 0.05:                      # 1. pin cue
            pins += 1
            continue
        body = [re.sub(r"<[^>]+>", " ", l).strip() for l in lines[ti + 1:]]
        body = [re.sub(r"\s+", " ", l) for l in body if l.strip()]
        fresh = [l for l in body if l not in recent]   # 2. rolling repeat
        if fresh:
            txt = " ".join(fresh).strip()
            if txt:
                frags.append((st, en, txt))
        recent.extend(body)
        if len(recent) > 60:
            recent = recent[-60:]

    # glue fragments into sentences so the model gets coherent units
    sents, buf, t0, t_end = [], [], None, None
    for st, en, l in frags:
        if t0 is None:
            t0 = st
        buf.append(l)
        t_end = en
        joined = " ".join(buf)
        if re.search(r"[.!?]$", l) and len(joined) > 40:
            sents.append((t0, t_end, re.sub(r"\s+", " ", joined).strip()))
            buf, t0 = [], None
    if buf:
        sents.append((t0, t_end, re.sub(r"\s+", " ", " ".join(buf)).strip()))
    return sents


def transcript_from_url(url, workdir):
    os.makedirs(workdir, exist_ok=True)
    log("📝 fetching captions from YouTube …")
    base = os.path.join(workdir, "sub")
    sh([YTDLP, "--skip-download", "--write-auto-subs", "--write-subs",
        "--sub-langs", "en.*", "--convert-subs", "srt",
        "-o", base, url])
    srt = [os.path.join(workdir, f) for f in os.listdir(workdir)
           if f.endswith(".srt")]
    if not srt:
        raise RuntimeError("no captions on this video (tried subs + auto-subs)")
    srt.sort(key=lambda p: (".en." not in p, len(p)))
    log(f"   ✓ {os.path.basename(srt[0])}")
    return parse_srt(srt[0])


def transcript_from_file(path, workdir):
    log("🎧 transcribing locally with mlx_whisper (whisper-turbo) …")
    try:
        import mlx_whisper
    except ImportError:
        raise RuntimeError(
            "mlx_whisper not importable — run this with the python that has it: "
            "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3")
    if os.path.isdir(HF_HOME):
        os.environ.setdefault("HF_HOME", HF_HOME)
    res = mlx_whisper.transcribe(path, path_or_hf_repo="mlx-community/whisper-turbo",
                                 verbose=False)
    segs = [(float(s["start"]), float(s["end"]), s["text"].strip())
            for s in res.get("segments", []) if s.get("text", "").strip()]
    log(f"   ✓ {len(segs)} segments")
    return segs


# ------------------------------------------------------------- llm pass

def ollama_post(host, payload, timeout=900):
    req = urllib.request.Request(
        f"http://{host}/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def ensure_ollama(host):
    addr = host.split(":")[0]
    port = int(host.split(":")[1]) if ":" in host else 11434
    import socket
    s = socket.socket()
    s.settimeout(0.4)
    try:
        s.connect((addr, port))
        s.close()
        return None
    except OSError:
        pass
    log(f"🚀 starting a private ollama on {host} …")
    proc = subprocess.Popen([OLLAMA, "serve"],
                            env=dict(os.environ, OLLAMA_HOST=host),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            s = socket.socket(); s.settimeout(0.4); s.connect((addr, port)); s.close()
            return proc
        except OSError:
            time.sleep(0.5)
    raise RuntimeError("ollama did not come up")


def stop_ollama(host, model, proc):
    """Leave NO ollama running after a run.

    Two separate things keep it alive otherwise:
      1. the model stays resident for its keep_alive window (was "5m"), and
      2. a server left over from an earlier session is never stopped, because
         ensure_ollama() only returns a process when IT started one — so the old
         `if proc: proc.terminate()` silently did nothing and the server lingered
         for days.
    Nothing else in this setup needs this port: the CapCut caption pipeline runs
    its own ollama on 11435 and terminates it itself.
    """
    # 1. unload the model so the RAM comes back immediately
    try:
        ollama_post(host, {"model": model, "keep_alive": 0}, timeout=30)
    except Exception:
        pass

    # 2. stop the server — ours if we started it, otherwise a leftover bound to
    #    this port from an earlier run
    stopped = False
    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=10)
            stopped = True
        except Exception:
            try:
                proc.kill()
                stopped = True
            except Exception:
                pass
    if not stopped:
        try:
            port = host.split(":")[1] if ":" in host else "11434"
            out = subprocess.run(["/usr/sbin/lsof", "-ti", f"TCP:{port}",
                                  "-sTCP:LISTEN"],
                                 capture_output=True, text=True).stdout.split()
            for pid in out:
                os.kill(int(pid), 15)
                log(f"   ⏹ stopped the leftover ollama server (pid {pid})")
        except Exception:
            pass

    # 3. The Ollama.app menu-bar app supervises its own server and spawns a fresh
    #    one the moment the old one dies, so close the app as well. The command
    #    line still works afterwards: /usr/local/bin/ollama is a symlink into the
    #    bundle, and the bundle stays installed — only the running app goes away.
    try:
        def app_pids():
            return subprocess.run(["pgrep", "-f", "Ollama.app/Contents/MacOS"],
                                  capture_output=True, text=True).stdout.split()
        if app_pids():
            subprocess.run(["osascript", "-e", 'quit app "Ollama"'],
                           capture_output=True, text=True, timeout=15)
            time.sleep(2)
            for pid in app_pids():
                os.kill(int(pid), 15)
            log("   ⏹ closed the Ollama app so it can't restart its server")
    except Exception:
        pass


def chunk_transcript(cues, minutes):
    """Group cues into ~N-minute chunks carrying their timestamps."""
    span = minutes * 60.0
    if not cues:
        return []
    start = cues[0][0]
    chunks, cur = [], []
    for st, en, txt in cues:
        if st - start > span and cur:
            chunks.append(cur)
            cur = []
            start = st
        cur.append((st, en, txt))
    if cur:
        chunks.append(cur)
    return chunks


def render_chunk(chunk):
    """One line per sentence, each with a timestamp the model can copy verbatim."""
    return "\n".join(f"[{mmss(st)}] {txt}" for st, en, txt in chunk)


def condense(cues, max_chars=5200):
    """Thin the transcript to one line per ~20s for the context pass."""
    out, last = [], -99.0
    for st, en, txt in cues:
        if st - last < 20.0:
            continue
        last = st
        out.append(f"[{mmss(st)}] {txt}")
        if sum(len(x) for x in out) > max_chars:
            break
    return "\n".join(out)


def parse_ts(s):
    """mm:ss or h:mm:ss -> seconds"""
    parts = [float(p) for p in re.split(r":", str(s).strip()) if p != ""]
    if not parts:
        return None
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0]


def extract_json(text):
    """Pull the first JSON array/object out of a model reply."""
    text = re.sub(r"```(?:json)?", "", text or "").strip()
    for op, cl in (("[" , "]"), ("{", "}")):
        i, j = text.find(op), text.rfind(cl)
        if i != -1 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except Exception:
                pass
    # last resort: a truncated array — close it and retry
    i = text.find("[")
    if i != -1:
        frag = text[i:].rstrip().rstrip(",")
        if not frag.endswith("]"):
            frag += "]"
        try:
            return json.loads(frag)
        except Exception:
            pass
    return None


def ask_json(host, model, prompt, timeout=900, num_predict=None):
    opts = {"temperature": 0.3, "num_ctx": 8192}
    if num_predict:
        opts["num_predict"] = num_predict
    r = ollama_post(host, {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "think": False,
        "options": opts,
        # 0 = drop the model from RAM as soon as this call returns. "5m" kept
        # ~3.5 GB resident for five minutes after every run.
        "keep_alive": 0,
    }, timeout=timeout)
    return (r.get("message") or {}).get("content", "")


def ctx_block(ctx):
    """Render the briefing as prompt text."""
    if not ctx:
        return "(no briefing available — judge this chunk on its own merits)"
    lines = []
    if ctx.get("subject"):
        lines.append(f"- about: {ctx['subject']}")
    if ctx.get("arena"):
        lines.append(f"- format: {ctx['arena']}")
    if ctx.get("voices"):
        lines.append(f"- speakers: {', '.join(map(str, ctx['voices']))[:160]}")
    if ctx.get("threads"):
        lines.append(f"- running arguments: {'; '.join(map(str, ctx['threads']))[:400]}")
    if ctx.get("hotspots"):
        lines.append(f"- stretches that looked charged: {', '.join(map(str, ctx['hotspots']))[:220]}")
    if ctx.get("what_travels"):
        lines.append(f"- what tends to travel from this video: {ctx['what_travels']}")
    return "\n".join(lines)


def ask_context(host, model, cues, dump=None):
    body = condense(cues)
    try:
        raw = ask_json(host, model, CONTEXT_PROMPT.format(body=body),
                       timeout=600, num_predict=400)
        if dump:
            dump.append(("context", raw))
        data = extract_json(raw)
        if isinstance(data, list) and data:
            data = data[0]
        if isinstance(data, dict):
            log(f"   ✓ briefing: {str(data.get('subject') or '')[:80]}")
            return data
        log("   · briefing could not be parsed — judging chunks cold")
    except Exception as e:
        log(f"   · briefing failed ({e}) — judging chunks cold")
    return {}


def _candidates(data, loose=False):
    """Normalise a model reply into candidate dicts."""
    if isinstance(data, dict):
        data = data.get("clips") or data.get("moments") or [data]
    good = []
    for n, it in enumerate(data, 1):
        if not isinstance(it, dict):
            continue
        hint = it.get("at", it.get("start"))
        if isinstance(hint, str):
            hint = hint.strip().strip("[]").strip()
        to = it.get("to", it.get("end"))
        if isinstance(to, str):
            to = to.strip().strip("[]").strip()
        hook = str(it.get("hook") or "").strip()
        if not hook:
            continue
        raw_sc = it.get("score")
        try:
            sc = float(raw_sc)
        except (TypeError, ValueError):
            sc = 4.0
        if sc > 1.0:                    # 0-10 scale -> 0-1
            sc = sc / 10.0
        good.append({
            "hint": parse_ts(hint) if hint not in (None, "") else None,
            "to_hint": parse_ts(to) if to not in (None, "") else None,
            "score": max(0.0, min(1.0, sc)),
            "hook": hook,
            "last": str(it.get("last") or "").strip(),
            "why": str(it.get("why") or "").strip(),
            "title": str(it.get("title") or "").strip(),
            "kind": str(it.get("kind") or ("mild" if loose else "")).strip().lower(),
            # the model was asked for its best moment FIRST — v2 uses that order
            # as the tie-break, because this model hands out 10/10 far too freely
            "rank": n,
        })
    return good


def ask_chunk(host, model, chunk, k, ctx, idx, total, loose=False, dump=None):
    prompt = (LOOSE_PROMPT if loose else RULES).format(
        chunk=render_chunk(chunk), k=k, context=ctx or "(unknown)",
        lo=15, hi=150)
    try:
        raw = ask_json(host, model, prompt, timeout=600)
        if dump is not None:
            dump.append((f"chunk{idx}{'-loose' if loose else ''}", raw))
        data = extract_json(raw)
        if data is None:
            log(f"   · chunk {idx}/{total}: unparseable reply")
            return []
        good = _candidates(data, loose=loose)
        log(f"   · chunk {idx}/{total}: {len(good)} candidate(s)")
        return good
    except Exception as e:
        log(f"   · chunk {idx}/{total}: failed ({e})")
        return []


# ------------------------------------------- placing a candidate in time

def _similar(a, b):
    """How well do two transcript lines look like the same sentence?"""
    at, bt = set(a.split()), set(b.split())
    if not at or not bt:
        return 0.0
    s = len(at & bt) / max(1, len(at))
    if len(a) >= 25 and a[:45] and a[:45] in b:
        s += 0.5                                   # near-exact quote
    s = max(s, difflib.SequenceMatcher(None, a[:90], b[:90]).ratio() * 0.9)
    return s


def find_cue(text, cues, lo_i=0, hi_i=None, hint=None, allow_hint=True):
    """Locate a quoted line in the real transcript -> (index, confidence, via).

    The model cannot be trusted with time arithmetic (a 4B model invented
    6-hour timestamps on a 98-minute video), but it CAN copy text. v1 dropped
    the whole clip whenever this lookup missed; v2 also accepts a near-miss
    (fuzzy ratio / longest shared opening) and, as a last resort, trusts the
    model's [mm:ss] hint when it lands next to a real cue.
    """
    t = normalize(text)
    if len(t) < 8:
        return None, 0.0, "none"
    hi_i = len(cues) if hi_i is None else min(hi_i, len(cues))
    best_i, best_s = None, 0.0
    for i in range(lo_i, hi_i):
        c = normalize(cues[i][2])
        if not c:
            continue
        s = _similar(t, c)
        if s > best_s:
            best_s, best_i = s, i
    if allow_hint and hint is not None and hi_i > lo_i:
        j = min(range(lo_i, hi_i), key=lambda k: abs(cues[k][0] - hint))
        if abs(cues[j][0] - hint) <= 60:
            if best_s < 0.28:
                return j, 0.25, "hint"          # rescue on the model's stamp
            best_s += 0.10
    return best_i, best_s, "quote"


def extend(cues, i, st, min_len, max_len):
    """Run the clip on through continuous speech, stopping after min_len at the
    first real pause (>=1.2s silence) — a pause means the exchange ended."""
    end = cues[i][1]
    j = i
    while j + 1 < len(cues):
        nxt_s, nxt_e, _ = cues[j + 1]
        if (nxt_e - st) > max_len:
            break
        gap = nxt_s - cues[j][1]
        if (end - st) >= min_len and gap >= 1.2:
            break
        j += 1
        end = cues[j][1]
    return end


def place(w, cues, min_len, max_len, strict=True):
    """-> dict(start, end, conf, via, end_via) or None."""
    thr = 0.30 if strict else 0.22
    i, conf, via = find_cue(w.get("hook"), cues, hint=w.get("hint"))
    if via == "hint":
        thr = 0.22        # the model's own [mm:ss] stamp, checked against a real cue
    if i is None or conf < thr:
        return None
    st = cues[i][0]
    max_cue = max(3.0, min(max_len, cues[i][1] - st))

    end, end_via = None, None
    # 1. the model's end LABEL, if it agrees with where the words actually are
    if w.get("to_hint") is not None and w["to_hint"] > st:
        if min_len * 0.5 <= (w["to_hint"] - st) <= max_len:
            j = min(range(i, len(cues)), key=lambda k: abs(cues[k][0] - w["to_hint"]))
            if abs(cues[j][0] - w["to_hint"]) <= 20:
                end, end_via = cues[j][1], "label"
    # 2. the quoted LAST line, located in the real transcript
    if end is None and len(normalize(w.get("last") or "")) >= 8:
        window = [k for k in range(i, len(cues)) if cues[k][0] - st <= max_len + 10]
        k, c2, _ = find_cue(w["last"], cues, lo_i=i,
                            hi_i=(window[-1] + 1 if window else len(cues)),
                            allow_hint=False)
        if k is not None and c2 >= 0.32 and min_len * 0.5 <= cues[k][1] - st <= max_len:
            end, end_via = cues[k][1], "quote"
    # 3. fall back to the pause rule
    if end is None or (end - st) < min_len * 0.5:
        e2 = extend(cues, i, st, min_len, max_len)
        if e2 > (end or 0):
            end, end_via = e2, "pause"
    if end - st > max_len:
        end = extend(cues, i, st, max_len * 0.6, max_len)
        end_via = "capped"
    if end - st < min_len:
        if strict and end_via != "pause":
            e2 = extend(cues, i, st, min_len, max_len)
            if e2 - st >= min_len:
                end, end_via = e2, "pause"
        if end - st < min_len:
            return None
    del max_cue
    return {"start": st, "end": end, "conf": round(min(1.0, conf), 2),
            "via": via, "end_via": end_via}


# -------------------------------------------------------- merging windows

def union_windows(wins, gap=12.0, max_len=150.0):
    """Union overlapping / touching picks instead of discarding the weaker one.

    v1's dedupe() threw the lower-scoring of two overlapping picks away, which
    loses real content when one is the setup and the other is the payoff.
    """
    if not wins:
        return []
    wins = sorted(wins, key=lambda w: w["start"])
    out = [dict(wins[0])]
    for w in wins[1:]:
        last = out[-1]
        if w["start"] - last["end"] <= gap and (max(w["end"], last["end"]) - last["start"]) <= max_len:
            last["end"] = max(last["end"], w["end"])
            last["merged"] = True
            last["members"] = sorted(set(map(tuple, (last.get("members") or
                                     [[last["start"], w["start"]]]) +
                                     [[w["start"], w["end"]]])))
            if w["score"] > last["score"]:
                last.update({k: w[k] for k in ("score", "hook", "why", "title",
                                               "kind", "text", "rank", "conf",
                                               "end_via")})
            last["tier"] = tier(last["score"])
        else:
            out.append(dict(w))
    for w in out:
        w["parts"] = [[round(w["start"], 1), round(w["end"], 1)]]
    return out


def tier(score):
    if score >= 0.70:
        return "strong"
    return "ok" if score >= 0.50 else "mild"


def stitch_pairs(wins, max_len=150.0, min_gap=12.0, max_gap=300.0,
                 min_part=10.0, max_pairs=2):
    """Two strong moments with filler between them -> ONE clip made of both.

    The user's own suggestion: a 10-30s moment and a 60-78s moment can be cut
    as a single clip. The filler in between is dropped (hard cut), so the two
    parts are listed explicitly and labelled in the output.
    """
    strong = [w for w in sorted(wins, key=lambda w: w["start"])
              if w["score"] >= 0.45 and (w["end"] - w["start"]) >= min_part]
    pairs, used = [], set()
    for a, b in zip(strong, strong[1:]):
        if id(a) in used or id(b) in used:
            continue
        gap = b["start"] - a["end"]
        if not (min_gap <= gap <= max_gap):
            continue
        total = (a["end"] - a["start"]) + (b["end"] - b["start"])
        if total > max_len:
            continue
        used.update((id(a), id(b)))
        pairs.append({
            "start": a["start"], "end": b["end"],
            "score": round(min(1.0, (a["score"] + b["score"]) / 2 + 0.05), 2),
            "hook": a["hook"], "why": f"two moments joined (gap {int(gap)}s)",
            "title": (a.get("title") or "first moment") + " + " + (b.get("title") or "second moment"),
            "kind": "+".join(filter(None, [a.get("kind"), b.get("kind")])),
            "parts": [[round(a["start"], 1), round(a["end"], 1)],
                      [round(b["start"], 1), round(b["end"], 1)]],
            "stitched": True, "tier": tier((a["score"] + b["score"]) / 2),
            "text": (a.get("text", "")[:300] + " … " + b.get("text", "")[:300]),
            "conf": min(a.get("conf", 1), b.get("conf", 1)),
        })
        if len(pairs) >= max_pairs:
            break
    return pairs


# --------------------------------------------------------------- fallback

def replay_windows(url, top, min_len, max_len, skip=0.0):
    """Where viewers rewound (YouTube heatmap) — the labelled fallback.

    Only used when the transcript judgement produced nothing, or when the user
    asks to blend the two signals. Attention is NOT virality: the caller labels
    these as "most replayed", never as "viral".
    """
    jf = tempfile.mktemp(suffix=".json", prefix="replay_")
    try:
        sh([sys.executable, FINDER, url, "--top", str(top), "--min", str(min_len),
            "--max", str(max_len), "--skip-intro", str(skip),
            "--no-fallback", "--json-out", jf], timeout=600)
        if not os.path.exists(jf):
            return []
        data = json.load(open(jf, encoding="utf-8"))
        res = (data.get("results") or [{}])[0]
        if res.get("mode") != "youtube-heatmap":
            return []
        out = []
        for c in res.get("clips", []):
            out.append({
                "start": float(c["start"]), "end": float(c["end"]),
                "score": round(min(0.55, float(c.get("score") or 0.5) * 0.6), 2),
                "hook": "", "last": "", "conf": 0.0, "via": "replay",
                "end_via": "replay", "kind": "replay", "tier": "replay",
                "title": "Most replayed stretch",
                "why": "viewers rewound here — attention, not necessarily a clip",
                "parts": [[c["start"], c["end"]]],
                "text": "", "replay": True,
            })
        return out
    except Exception as e:
        log(f"   · replay fallback unavailable ({e})")
        return []
    finally:
        if os.path.exists(jf):
            os.unlink(jf)


# ------------------------------------------------------------------ cutting

def cut_segment(local, start, end, out, crf=20):
    r = sh([FFMPEG, "-y", "-v", "error", "-ss", f"{start:.2f}", "-to", f"{end:.2f}",
            "-i", local, "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", out])
    return os.path.exists(out) and os.path.getsize(out) > 10000, r.stderr[-200:]


def concat_segments(parts, out):
    """Join already-encoded parts with the concat demuxer (-c copy: same params)."""
    lst = out + ".txt"
    with open(lst, "w", encoding="utf-8") as f:
        for p in parts:
            f.write("file '" + p.replace("'", "'\\''") + "'\n")
    r = sh([FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0",
            "-i", lst, "-c", "copy", "-movflags", "+faststart", out])
    try:
        os.unlink(lst)
    except OSError:
        pass
    return os.path.exists(out) and os.path.getsize(out) > 10000, r.stderr[-200:]


# ------------------------------------------------------------------ main

def judge(host, model, chunks, ctx, per_chunk, workers, loose=False, dump=None):
    found = []
    with futures.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        jobs = [ex.submit(ask_chunk, host, model, c, per_chunk, ctx,
                          i + 1, len(chunks), loose, dump)
                for i, c in enumerate(chunks)]
        for j in jobs:
            found.extend(j.result())
    return found


def place_all(found, cues, min_len, max_len, strict=True):
    wins, dropped = [], 0
    for seq, w in enumerate(found):
        a = place(w, cues, min_len, max_len, strict=strict)
        if not a:
            dropped += 1
            log(f"   · dropped (quote not found): {w.get('hook','')[:58]}")
            continue
        st, en = a["start"], a["end"]
        title = w.get("title") or " ".join((w.get("hook") or "").split()[:6])
        wins.append({**w, **a, "title": title, "seq": seq, "tier": tier(w["score"]),
                     "match": a["conf"],
                     "text": " ".join(t for s, e, t in cues
                                      if e > st and s < en)[:700]})
    return wins, dropped


def main():
    ap = argparse.ArgumentParser(
        description="Find clip-worthy moments by reading the transcript.")
    ap.add_argument("source", help="YouTube URL or local video file")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--model", default="qwen3:4b-instruct")
    ap.add_argument("--host", default=DEFAULT_OLLAMA)
    ap.add_argument("--chunk-min", type=float, default=6.0,
                    help="minutes of transcript per model call")
    ap.add_argument("--per-chunk", type=int, default=3,
                    help="max candidates to ask for per chunk")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--min", dest="min_len", type=float, default=30.0,
                    help="shortest acceptable clip (seconds)")
    ap.add_argument("--max", dest="max_len", type=float, default=150.0,
                    help="longest acceptable clip (seconds)")
    ap.add_argument("--merge-gap", type=float, default=12.0,
                    help="windows closer than this become one clip")
    ap.add_argument("--stitch", dest="stitch", action="store_true", default=True,
                    help="join two strong moments separated by filler (default on)")
    ap.add_argument("--no-stitch", dest="stitch", action="store_false")
    ap.add_argument("--stitch-gap", type=float, default=300.0,
                    help="max filler gap that still gets stitched (seconds)")
    ap.add_argument("--hybrid", dest="hybrid", action="store_true", default=True,
                    help="blend in the most-replayed stretches (default on)")
    ap.add_argument("--no-hybrid", dest="hybrid", action="store_false")
    ap.add_argument("--skip-intro", type=float, default=0.0,
                    help="ignore the first N seconds of replay data")
    ap.add_argument("--cut", action="store_true")
    ap.add_argument("--outdir", "-o", default=OUTPUT_DIR,
                    help="where to write the cut clips")
    ap.add_argument("--json-out", dest="json_out")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dump-raw", dest="dump_raw",
                    help="write every raw model reply here (debugging)")
    args = ap.parse_args()

    workdir = tempfile.mkdtemp(prefix="smartclips_")
    proc = None
    dump = []
    try:
        if is_url(args.source):
            cues = transcript_from_url(args.source, workdir)
        else:
            cues = transcript_from_file(args.source, workdir)
        if not cues:
            raise RuntimeError("empty transcript")
        dur = cues[-1][1]
        log(f"   transcript covers {mmss(dur)}  ({sum(len(c[2]) for c in cues)} chars)")

        proc = ensure_ollama(args.host)

        # ---- 1. briefing: what is this video, and what tends to travel in it
        log(f"🧭 reading the whole video once for context ({args.model}) …")
        ctx = ask_context(args.host, args.model, cues, dump)

        # ---- 2. per-chunk judgement, briefed
        chunks = chunk_transcript(cues, args.chunk_min)
        log(f"🧠 judging {len(chunks)} transcript chunks with {args.model} "
            f"({args.workers} at a time) — local, $0 …")
        found = judge(args.host, args.model, chunks, ctx_block(ctx),
                      args.per_chunk, args.workers, dump=dump)

        wins, dropped = place_all(found, cues, args.min_len, args.max_len)

        # ---- 3. second chance: same chunks, no strictness
        if not wins:
            log("   · nothing survived the first read — second pass, looser …")
            found2 = judge(args.host, args.model, chunks, ctx_block(ctx), 1,
                           args.workers, loose=True, dump=dump)
            wins, _ = place_all(found2, cues, args.min_len, args.max_len,
                                strict=False)
            if wins:
                for w in wins:
                    w["via_second_pass"] = True

        # ---- 4. merging: union overlapping picks, stitch far-apart pairs
        wins = union_windows(wins, gap=args.merge_gap, max_len=args.max_len)
        merged = sum(1 for w in wins if w.get("merged"))
        if merged:
            log(f"   · merged overlapping picks into {len(wins)} window(s)")
        stitched = []
        if args.stitch and wins:
            stitched = stitch_pairs(wins, max_len=args.max_len,
                                    max_gap=args.stitch_gap)
            if stitched:
                log(f"   · stitched {len(stitched)} pair(s) of far-apart moments")

        # ---- 5. hybrid / fallback: the most-replayed stretches
        # score first, then the model's own best-first order (this model inflates
        # scores, so ties are common and the order it returned is the better signal)
        picked = sorted(wins, key=lambda w: (-w["score"], w.get("rank", 9),
                                            w.get("seq", 0)))[:args.top]
        if stitched:
            picked = picked + stitched
        if is_url(args.source) and args.hybrid and len(picked) < args.top:
            extra = replay_windows(args.source, args.top, args.min_len,
                                   args.max_len, args.skip_intro)
            room = args.top - len(picked)
            if extra and room > 0:
                log(f"   · blending in {len(extra[:room])} most-replayed stretch(es)")
                picked += extra[:room]

        if not picked:
            raise RuntimeError(
                "Nothing in this video would work as a standalone clip. The "
                "transcript is silence, ads or housekeeping throughout — there "
                "is no dialogue to judge. (This is a judgement, not a failure.)")

        picked = sorted(picked, key=lambda w: w["start"])
        for i, w in enumerate(picked, 1):
            w["index"] = i
            # a stitched clip is the SUM of its parts — the gap between them is
            # dropped, so end-start would overstate its real runtime
            w["length"] = round(
                sum(e - s for s, e in w["parts"]) if w.get("stitched")
                else w["end"] - w["start"], 1)

        strong = sum(1 for w in picked if w.get("tier") == "strong")
        mild = sum(1 for w in picked if w.get("tier") in ("mild", "replay"))
        note = ""
        if not strong and mild:
            note = ("No unmistakable clash or mic-drop in this video — these are "
                    "the most interesting stretches it does have.")
        judged = [w for w in picked if not w.get("replay")]
        if len(judged) >= 4 and sum(1 for w in judged if w["score"] >= 0.9) > len(judged) / 2:
            note = ((note + " ") if note else "") + (
                "The judge gave nearly every pick top marks, so treat the order "
                "as its ranking (best first from each part of the video) rather "
                "than a precise score.")

        res = {"source": args.source, "title": os.path.basename(args.source),
               "duration": dur, "mode": "transcript-llm", "model": args.model,
               "context": ctx, "clips": picked, "note": note,
               "tiers": {"strong": strong,
                         "mild": mild,
                         "second_pass": bool(picked and picked[0].get("via_second_pass"))}}

        # ---- 6. cut
        if args.cut and picked:
            if not os.path.exists(FFMPEG):
                raise RuntimeError("ffmpeg not found")
            local = args.source
            if is_url(local):
                log("⬇ downloading the video to cut from …")
                local = os.path.join(workdir, "src.mp4")
                sh([YTDLP, "-q", "--no-progress",
                    "-f", "bv*[height<=1080]+ba/b[height<=1080]",
                    "--merge-output-format", "mp4", "-o", local, args.source])
                if not os.path.exists(local):
                    raise RuntimeError("download failed")
            os.makedirs(args.outdir, exist_ok=True)
            stem = re.sub(r"[^A-Za-z0-9]+", "-", os.path.splitext(
                os.path.basename(args.source))[0])[:40].strip("-") or "clip"
            cuts = []
            for i, w in enumerate(picked, 1):
                stamp = mmss(w["start"]).replace(":", ".")
                if w.get("stitched"):
                    tag = "MERGED"
                    out = os.path.join(args.outdir, f"{i:02d}_{tag}_{stem}_{stamp}.mp4")
                    tmp = []
                    ok = True
                    for n, (ps, pe) in enumerate(w["parts"]):
                        part = os.path.join(workdir, f"st_{i}_{n}.mp4")
                        good, err = cut_segment(local, ps, pe, part)
                        if not good:
                            ok = False
                            log(f"   ✗ merged part {n+1} failed: {err}")
                            break
                        tmp.append(part)
                    if ok:
                        good, err = concat_segments(tmp, out)
                        if not good:
                            log(f"   ✗ merge failed: {err}")
                    if ok and os.path.exists(out):
                        w["file"] = out
                        cuts.append(out)
                        log(f"   ✂ {os.path.basename(out)}  "
                            f"({'+'.join(f'{s:.0f}-{e:.0f}s' for s, e in w['parts'])})")
                else:
                    out = os.path.join(args.outdir, f"{i:02d}_{stem}_{stamp}.mp4")
                    good, err = cut_segment(local, w["start"], w["end"], out)
                    if good:
                        w["file"] = out
                        cuts.append(out)
                        log(f"   ✂ {os.path.basename(out)}")
                    else:
                        log(f"   ✗ cut failed: {err}")
            res["files"] = cuts

        if args.dump_raw and dump:
            with open(args.dump_raw, "w", encoding="utf-8") as f:
                for tag, raw in dump:
                    f.write(f"\n===== {tag} =====\n{raw}\n")

        payload = {"results": [res], "errors": []}
        if args.json_out:
            with open(args.json_out, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(f"\n{res['title']}\n{mmss(dur)}  |  {len(picked)} moments  "
                  f"|  judged by {args.model} from the transcript\n")
            if ctx.get("subject"):
                print(f"  about: {ctx['subject']}")
            if ctx.get("what_travels"):
                print(f"  travels: {ctx['what_travels']}")
            print()
            for w in picked:
                tag = f"· {w.get('tier','')}"
                if w.get("stitched"):
                    tag += " · MERGED from " + " + ".join(
                        f"{mmss(s)}-{mmss(e)}" for s, e in w["parts"])
                print(f"  {w['index']}. {mmss(w['start'])} -> {mmss(w['end'])}  "
                      f"({w['length']:.0f}s, score {w['score']:.2f} {tag})")
                if w.get("title"):
                    print(f"     {w['title']}")
                if w.get("kind"):
                    print(f"     kind: {w['kind']}")
                if w.get("hook"):
                    print(f"     hook: \"{w['hook'][:110]}\"")
                if w.get("why"):
                    print(f"     why : {w['why']}")
                if w.get("text"):
                    print(f"     says: \"{w['text'][:150]}…\"")
                if w.get("file"):
                    print(f"     file: {w['file']}")
                print()
            if note:
                print(f"  ℹ {note}")
    except Exception as e:
        payload = {"results": [], "errors": [{"source": args.source,
                                              "error": str(e)}]}
        if args.json or args.json_out:
            if args.json:
                print(json.dumps(payload, indent=2, ensure_ascii=False))
            if args.json_out:
                with open(args.json_out, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2, ensure_ascii=False)
        else:
            print(f"\n❌ {e}", file=sys.stderr)
        return 1
    finally:
        stop_ollama(args.host, args.model, proc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
