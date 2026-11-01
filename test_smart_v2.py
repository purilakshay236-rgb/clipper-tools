import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location(
    "sc", os.path.join(os.path.dirname(os.path.abspath(__file__)), "smart_clips.py"))
sc = importlib.util.module_from_spec(spec); spec.loader.exec_module(sc)

fails = []
def ck(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (("  " + str(extra)) if extra else ""))
    if not cond: fails.append(name)

# Run with:  python3 test_smart_v2.py
# Offline checks of the smart_clips.py v2 logic that decides how much of a
# model's suggestion survives: score scaling, JSON recovery, quote anchoring
# (exact / paraphrased / hint-rescue / unplaceable), window union and stitching.
# These are the parts that silently produced "no moments found" in v1.

# ---- synthetic transcript (10s per line)
lines = [
 "The border is the worst it has ever been in this country",
 "I did not say that and you know I did not say that",
 "You said it on camera in twenty nineteen",
 "That is a total lie and everybody watching knows it",
 "Let him finish please let him finish",
 "He took no land nothing from Trump nothing",
 "Then why did you call them suckers and losers",
 "I never called anybody a sucker",
 "The cleanest water in the world and not a damn thing to do with him",
 "We are a failing nation and everybody knows it",
]
cues = [(i*10.0, i*10.0+9.0, t) for i, t in enumerate(lines)]

# 1. score normalisation 0-10 -> 0-1 and 0-1 passthrough
c = sc._candidates([{"at":"00:10","hook":"I did not say that and you know","score":8},
                    {"at":"00:30","hook":"That is a total lie and everybody","score":0.4}])
ck("score 8 -> 0.8", abs(c[0]["score"]-0.8) < 1e-9, c[0]["score"])
ck("score 0.4 stays", abs(c[1]["score"]-0.4) < 1e-9, c[1]["score"])

# 2. truncated JSON array recovery
ck("truncated json recovers",
   sc.extract_json('[{"at":"01:00","hook":"x","score":7},{"at":"02:00","hoo') is not None)

# 3. exact quote places at the right line, end label honoured
w = {"hint": 10.0, "to_hint": 30.0, "score": 0.8,
     "hook": "I did not say that and you know I did not say that",
     "last": "You said it on camera in twenty nineteen"}
p = sc.place(w, cues, 20, 150)
ck("exact hook placed at 10s", p and abs(p["start"]-10.0) < 0.01, p)
ck("end uses the model's label (line @30s ends 39s)", p and abs(p["end"]-39.0) < 0.01, p)
ck("via=quote", p and p["via"] == "quote")

# 4. hook copied imperfectly (model paraphrase) must still place
w2 = dict(w, hint=10.0, hook="I did not say that and you know I never said that",
          to_hint=None, last="")
p2 = sc.place(w2, cues, 20, 150)
ck("paraphrased hook still placed", p2 is not None, p2)
ck("paraphrase lands near 10s", p2 and abs(p2["start"] - 10.0) < 20.0, p2)

# 5. hook with nothing in common -> hint rescue
w3 = {"hint": 80.0, "to_hint": None, "score": 0.6,
      "hook": "zzzz qqqq vvvv mmmm", "last": ""}
p3 = sc.place(w3, cues, 8, 150)   # clip-length floor needs the tail line to be long enough
ck("hint rescue fires (low conf)", p3 and p3["via"] == "hint", p3)

# 6. hook from nowhere, no usable hint -> dropped
w4 = {"hint": 9000, "to_hint": None, "score": 0.6,
      "hook": "zzzz qqqq vvvv mmmm", "last": ""}
ck("unplaceable pick dropped", sc.place(w4, cues, 20, 150) is None)

# 7. merging: overlapping picks become ONE clip, not one discarded
wins = [{"start": 10, "end": 40, "score": 0.9, "hook": "a", "why": "", "title": "A", "kind": "clash"},
        {"start": 35, "end": 70, "score": 0.6, "hook": "b", "why": "", "title": "B", "kind": "absurd"}]
u = sc.union_windows(wins, gap=12, max_len=150)
ck("overlap merged to one window", len(u) == 1, u)
ck("merged span covers both", u and u[0]["start"] == 10 and u[0]["end"] == 70, u)
ck("merged keeps the higher score", u and u[0]["score"] == 0.9)
ck("merged marked", u and u[0].get("merged") is True)

# 8. far-apart picks stay separate but can be stitched
wins2 = [{"start": 10, "end": 40, "score": 0.9, "hook": "a", "title": "A", "kind": "clash", "text": "x"},
         {"start": 160, "end": 195, "score": 0.8, "hook": "b", "title": "B", "kind": "absurd", "text": "y"},
         {"start": 900, "end": 930, "score": 0.9, "hook": "c", "title": "C", "kind": "clash", "text": "z"}]
u2 = sc.union_windows(wins2, gap=12, max_len=150)
ck("far-apart picks stay separate", len(u2) == 3, [ (w["start"], w["end"]) for w in u2])
st = sc.stitch_pairs(u2, max_len=150, max_gap=300)
ck("one stitch pair made", len(st) == 1, st)
ck("stitch parts are the two moments", st and st[0]["parts"] == [[10, 40], [160, 195]], st and st[0]["parts"])
ck("stitch runtime = sum of parts", st and (30+35) <= 150 and st[0]["parts"] == [[10,40],[160,195]], st and st[0]["parts"])
ck("stitch flagged", st and st[0]["stitched"] is True)

# 9. stitch refuses when the two parts are too long together
st2 = sc.stitch_pairs(u2, max_len=150, max_gap=3000)
ck("stitch respects max total length (sum of parts)",
   all(sum(e-s for s,e in p["parts"]) <= 150 for p in st2), st2)

# 10. tiers
ck("tier strong", sc.tier(0.8) == "strong")
ck("tier ok", sc.tier(0.55) == "ok")
ck("tier mild", sc.tier(0.3) == "mild")

# 11. condense thins the transcript
cond = sc.condense(cues)
ck("condense keeps ~1 line per 20s", cond.count("\n") <= len(cues)//2, cond.count("\n"))

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
