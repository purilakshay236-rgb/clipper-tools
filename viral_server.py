#!/usr/bin/env python3
"""
viral_server.py — a tiny local web UI for find_viral.py.

No dependencies (stdlib only), no API calls, nothing leaves your Mac.
Start it with the "Viral Finder.command" icon, then use the page it opens.

Routes
  GET  /            the UI
  GET  /run         run find_viral.py, stream progress (SSE)
  GET  /preview     cut a small throwaway copy of one moment, stream progress (SSE)
  GET  /pv/<file>   stream a cached preview (Range requests, so it can be scrubbed)
  GET  /pvclear     empty the preview cache
  GET  /files       what is already downloaded/cut (readable by a friend)
  GET  /shared/<k>/<name>  send one of those files to whoever asked
  GET  /pick        native macOS file picker -> returns a path
  GET  /pickfolder  native macOS folder picker -> returns a path
  GET  /open?path=  reveal a file/folder in Finder
  GET  /quit        stop the server
"""

import glob
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
FINDER = os.path.join(HERE, "find_viral.py")
SMART = os.path.join(HERE, "smart_clips.py")
# Clips must NEVER land on the Mac's internal drive — the SSD only.
# (Standing rule from the user: no folders on the Desktop or internal storage.)
DEFAULT_OUT = "/Volumes/SanDiskSSD/Viral Clips"
DEFAULT_BROWSE = "/Volumes/SanDiskSSD"
PORT = int(os.environ.get("VIRAL_UI_PORT", "8765"))
SETTINGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ui_settings.json")


def load_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_settings(**kw):
    d = load_settings()
    d.update(kw)
    try:
        with open(SETTINGS, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass


def out_dir():
    """Where clips go: the user's saved choice, else the default.

    The saved folder itself need not exist yet (it gets created on the first
    cut) — but if its volume is missing (e.g. the SSD is unmounted) fall back
    to the default instead of failing or writing somewhere surprising.
    """
    p = (load_settings().get("outdir") or "").strip()
    if not p:
        return DEFAULT_OUT
    p = os.path.expanduser(p)
    if not os.path.isabs(p):
        return DEFAULT_OUT
    parent = os.path.dirname(p.rstrip("/")) or "/"
    return p if os.path.isdir(parent) else DEFAULT_OUT

# ---------------------------------------------------------------- osascript

def osascript(script, timeout=300):
    """Run AppleScript. Returns (ok, stdout+stderr)."""
    try:
        p = subprocess.run(["/usr/bin/osascript", "-e", script],
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, (p.stdout or p.stderr).strip()
    except Exception as e:
        return False, str(e)


def pick_file():
    start = DEFAULT_BROWSE if os.path.isdir(DEFAULT_BROWSE) else os.path.expanduser("~")
    ok, out = osascript(
        'set f to choose file with prompt "Choose a video" '
        f'default location (POSIX file "{start}")\n'
        'return POSIX path of f')
    return out if ok and out.startswith("/") else ""


def pick_folder():
    start = out_dir()
    if not os.path.isdir(start):
        start = os.path.expanduser("~")
    ok, out = osascript(
        'set f to choose folder with prompt "Where should the clips go?" '
        f'default location (POSIX file "{start}")\n'
        'return POSIX path of f')
    out = out.rstrip("/") if ok and out.startswith("/") else ""
    if out:
        save_settings(outdir=out)      # remember it for next time
    return out


# --------------------------------------------------------------- downloads
# A standalone yt-dlp kept on the SSD — NOT the copy inside a Hermes skill venv,
# which can move or disappear. ffmpeg (homebrew) does the video+audio merge.
YTDLP = "/Volumes/SanDiskSSD/clipper-tools/bin/yt-dlp"
FFMPEG = "/opt/homebrew/bin/ffmpeg"
DEFAULT_DL = "/Volumes/SanDiskSSD/Downloads"

# "CapCut-safe" matters: plain "best" returns AV1 video + Opus audio, which
# CapCut frequently refuses to import. avc1 + mp4a is the SAME resolution in a
# codec every editor accepts. Verified: 1080p av01+opus vs 1080p avc1+mp4a.
FMT_CAPCUT = "bv*[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"
FMT_MAX = "bv*+ba/b"

# ---- TikTok without a VPN -------------------------------------------------
# India blocks TikTok's web/API hosts, so yt-dlp dies there (connection reset in
# ~0.3s — NOT a DNS problem, DNS resolves fine). But the host that actually
# serves the video bytes, tiktokcdn-us.com, is NOT blocked. So a third-party
# resolver can hand us a signed CDN url we can fetch directly.
# Trade-off: 576x1024 (TikTok's mobile rendition) instead of the full original,
# and the resolver rate-limits if hammered. yt-dlp + VPN stays the better path.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
TIKWM = "https://www.tikwm.com/api/?url="

# The TikTok app's share sheet hands out SHORT links (vm.tiktok.com/Zxxxx,
# vt.tiktok.com/..., tiktok.com/t/..., tiktok.com/d/1/...) rather than the
# canonical tiktok.com/@user/video/<id>. A short link carries no video id at
# all — it is a key TikTok's own server swaps for the real URL — and the
# resolver can only parse links that already contain the id, so it answers
# "Url parsing is failed!" and nothing downloads. Following the redirect
# ourselves does not work from India either: tiktok.com answers an Indian IP
# with a 302 to /in/about for every user agent tried (Chrome, iPad Safari,
# TikTok's own Cronet UA). So a resolver outside India expands the short link
# for us, and the resulting id goes to tikwm like any other video.
TT_EXPANDERS = ("https://api.redirect-checker.net/?url=",
                "https://unshorten.me/s/")
_TT_CACHE = {}          # short link -> video id, so a repeated batch is free


def tiktok_video_id(text):
    """The numeric TikTok video id inside any link or response, or ''."""
    for pat in (r"/video/(\d{6,25})", r"/v/(\d{6,25})",
                r"/embed/(?:v\d+/)?(\d{6,25})",
                r"share_item_id=(\d{6,25})",
                r"item_?[iI]d[\"']?\s*[:=]\s*[\"']?(\d{6,25})"):
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return ""


def is_tiktok_short(link):
    """True for the app's share-link shapes, i.e. links with no video id."""
    l = link.lower()
    if tiktok_video_id(l):
        return False
    return ("vm.tiktok.com" in l or "vt.tiktok.com" in l
            or "/t/" in l or "/d/1/" in l)


def expand_tiktok(link):
    """(kind, id, why). Asks non-Indian resolvers what a share link points to.

    kind is "video", "photo" (a slideshow post — no video file exists) or ""
    when nothing usable came back.
    """
    key = link.strip()
    if key in _TT_CACHE:
        return _TT_CACHE[key]
    last = ""
    for base in TT_EXPANDERS:
        url = base + urllib.parse.quote(key, safe="")
        if "redirect-checker" in base:
            url += "&timeout=10&maxhops=6"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                # The answers arrive JSON-escaped ("\/video\/123"), so undo that
                # before looking for an id or an id would only ever be found in
                # the share_item_id parameter some of them also carry.
                raw = r.read().decode("utf-8", "replace").replace("\\/", "/")
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            continue
        # A resolved photo post looks like /@/photo/<id> and also carries
        # share_item_id=<id>, so the photo shape has to be checked first or the
        # id pattern would claim it as a video.
        ph = re.search(r"/photo/(\d{6,25})", raw)
        if ph:
            out = ("photo", ph.group(1), "")
            _TT_CACHE[key] = out
            return out
        vid = tiktok_video_id(raw)
        if vid:
            out = ("video", vid, "")
            _TT_CACHE[key] = out
            return out
        last = "the expander did not find a video behind it"
    return ("", "", last or "no expander answered")


def is_tiktok(link):
    return "tiktok.com" in link.lower()


def clean_link(l):
    """One pasted line -> a url. Share sheets and chat apps add punctuation,
    and a link copied out of a browser address bar has no scheme at all."""
    l = l.strip().strip("\"'<>,").strip()
    if l and not re.match(r"^https?://", l, re.I) and re.match(
            r"^(?:www\.|m\.|vm\.|vt\.)?(?:tiktok|youtube|youtu|instagram)\.", l, re.I):
        l = "https://" + l
    return l


# ---- Instagram needs a login ----------------------------------------------
# Instagram answers logged-out clients with "empty media response", so reels and
# photo posts cannot be fetched anonymously any more. The user's Zen (Firefox
# family) profile is the auth source — the same session he is already logged
# into — so lift its instagram cookies into a Netscape jar that yt-dlp reads.
# Nothing leaves the Mac; the jar is rewritten from the live profile on every
# Instagram link, so a re-login in Zen is picked up with no extra steps.
ZEN_PROFILES = os.path.expanduser("~/Library/Application Support/zen/Profiles")
IG_JAR = os.path.join(tempfile.gettempdir(), "viral_ig_cookies.txt")
COOKIE_DOMAINS = ("instagram.com", "cdninstagram.com")


def is_instagram(link):
    return "instagram.com" in link.lower()


def ig_cookie_jar(force=False):
    """(jar_path, note). Jar_path is None when there is no usable login."""
    try:
        if (not force and os.path.exists(IG_JAR)
                and time.time() - os.path.getmtime(IG_JAR) < 900):
            return IG_JAR, "cached"
    except Exception:
        pass

    tmp = tempfile.mkdtemp(prefix="igck")
    try:
        src, rows = None, None
        for db_path in sorted(glob.glob(os.path.join(ZEN_PROFILES, "*", "cookies.sqlite"))):
            try:
                shutil.copy(db_path, tmp)                 # the live db is locked
                wal = db_path + "-wal"
                if os.path.exists(wal):
                    shutil.copy(wal, tmp)                 # uncommitted session
                con = sqlite3.connect(os.path.join(tmp, "cookies.sqlite"))
                n = con.execute("select count(*) from moz_cookies where name='sessionid' "
                                "and host like '%instagram%'").fetchone()[0]
                if n:
                    src = db_path
                    rows = con.execute("select host,name,value,path,expiry "
                                       "from moz_cookies").fetchall()
                    con.close()
                    break
                con.close()
            except Exception:
                continue
        if not src:
            return None, ("no Instagram login in your Zen profile — open instagram.com "
                          "in Zen and sign in once, then try again")

        out = ["# Netscape HTTP Cookie File",
               "# generated from the Zen profile by viral_server.py"]
        kept = 0
        for host, name, value, path, expiry in rows:
            if not any(d in host for d in COOKIE_DOMAINS):
                continue
            exp = int(expiry) if expiry else int(time.time()) + 86400 * 365
            out.append("\t".join([host, "TRUE" if host.startswith(".") else "FALSE",
                                  path or "/", "TRUE", str(exp), name, value]))
            kept += 1
        if not kept:
            return None, "the Zen profile has no Instagram cookies"
        with open(IG_JAR, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        os.chmod(IG_JAR, 0o600)
        return IG_JAR, f"{kept} cookies from {os.path.basename(os.path.dirname(src))}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- YouTube needs a login too (bot gate) ---------------------------------
# YouTube answers anonymous yt-dlp requests from many residential IPs with
# "Sign in to confirm you're not a bot" (LOGIN_REQUIRED), which looks like a
# download bug but is a sign-in demand. Cookies from a signed-in profile clear
# it. Same Zen source as Instagram; set "yt_cookie_profile" in
# .ui_settings.json to an absolute profile path to use a burner instead.
YT_JAR = os.path.join(tempfile.gettempdir(), "viral_yt_cookies.txt")
YT_COOKIE_DOMAINS = ("youtube.com", "google.com", "googlevideo.com", "ytimg.com")
YT_AUTH_NAMES = ("SID", "HSID", "SSID", "SAPISID", "APISID",
                 "__Secure-1PSID", "__Secure-3PSID", "LOGIN_INFO")


def is_youtube(link):
    l = link.lower()
    return "youtube.com" in l or "youtu.be" in l


def yt_cookie_jar(force=False):
    """(jar_path, note). Jar_path is None when there is no usable login."""
    try:
        if (not force and os.path.exists(YT_JAR)
                and time.time() - os.path.getmtime(YT_JAR) < 900):
            return YT_JAR, "cached"
    except Exception:
        pass

    prof = (load_settings().get("yt_cookie_profile") or "").strip()
    if prof:
        dbs = [os.path.join(os.path.expanduser(prof), "cookies.sqlite")]
    else:
        dbs = sorted(glob.glob(os.path.join(ZEN_PROFILES, "*", "cookies.sqlite")))

    tmp = tempfile.mkdtemp(prefix="ytck")
    try:
        src, rows = None, None
        for db_path in dbs:
            try:
                shutil.copy(db_path, tmp)                 # the live db is locked
                wal = db_path + "-wal"
                if os.path.exists(wal):
                    shutil.copy(wal, tmp)                 # uncommitted session
                con = sqlite3.connect(os.path.join(tmp, "cookies.sqlite"))
                rows = con.execute("select host,name,value,path,expiry "
                                   "from moz_cookies").fetchall()
                con.close()
                auth = [r for r in rows if r[1] in YT_AUTH_NAMES
                        and any(d in r[0] for d in ("youtube.com", "google.com"))]
                if auth:
                    src = db_path
                    break
                rows = None
            except Exception:
                rows = None
                continue
        if not src or not rows:
            return None, ("no YouTube login in your Zen profile — open youtube.com "
                          "in Zen and sign in once, then try again")

        out = ["# Netscape HTTP Cookie File",
               "# generated from the Zen profile by viral_server.py"]
        kept = 0
        for host, name, value, path, expiry in rows:
            if not any(d in host for d in YT_COOKIE_DOMAINS):
                continue
            exp = int(expiry) if expiry else int(time.time()) + 86400 * 365
            out.append("\t".join([host, "TRUE" if host.startswith(".") else "FALSE",
                                  path or "/", "TRUE", str(exp), name, value]))
            kept += 1
        if not kept:
            return None, "the Zen profile has no YouTube cookies"
        with open(YT_JAR, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        os.chmod(YT_JAR, 0o600)
        return YT_JAR, f"{kept} cookies from {os.path.basename(os.path.dirname(src))}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def exit_country(timeout=8):
    """Which country does the internet see us from? ("IN" = no VPN)."""
    try:
        req = urllib.request.Request("https://ipinfo.io/json",
                                     headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return (json.loads(r.read().decode("utf-8", "replace"))
                    .get("country") or "").upper()
    except Exception:
        return ""


def tiktok_blocked_here():
    """True when yt-dlp's TikTok route cannot work from this network.

    Never probe TikTok itself to decide this. Two blocks stack against it:
      1. Airtel blocks tiktok.com at the DNS level (RPZ -> 202.56.230.x sinkhole).
      2. TikTok geo-blocks Indian IPs, redirecting to /in/about with no video data
         — so even with working DNS, extraction fails from an Indian IP.
    Worse, Python's own resolver returns the Airtel sinkhole for tiktok.com even
    while the VPN is up, so any direct probe just times out and would falsely
    report "blocked" — which would wrongly skip yt-dlp and then fail, because the
    no-VPN resolver 403s on the VPN's datacenter IP. The exit country is the one
    signal that is reliable in both states.
    """
    return exit_country() == "IN"


def tiktok_no_vpn(link, dest, log=None):
    """Download one TikTok video without a VPN. Returns (path, None) or (None, why)."""
    def say(t):
        if log:
            log(t)
    try:
        target = link
        if is_tiktok_short(link):
            # A share link has to be traded for the real video first — tikwm
            # cannot parse it, it only ever answers "Url parsing is failed!".
            kind, vid, why = expand_tiktok(link)
            if kind == "photo":
                return None, ("that share link points at a TikTok photo post "
                              "(slideshow) — there is no video file to download")
            if not vid:
                return None, ("this is a TikTok share link and it could not be "
                              "expanded (" + why + ") — the link may be expired; "
                              "open the video and copy the full address "
                              "(tiktok.com/@user/video/1234…) instead")
            target = f"https://www.tiktok.com/@/video/{vid}"
            say(f"   🔗 share link — video id {vid}")
        elif not tiktok_video_id(link):
            # A profile, a search page, a collection — nothing to download. Say
            # so plainly instead of letting the resolver answer "url parsing". 
            return None, ("this link has no video id in it — it looks like a "
                          "profile or a list page, not one video. Open the video "
                          "itself and copy that link.")
        # &hd=1 is what unlocks hdplay (1080x1920); without it the resolver only
        # returns play/wmplay at 576x1024. Measured on the same video:
        #   default -> play 576x1024 | hd=1 -> hdplay 1080x1920
        req = urllib.request.Request(
            TIKWM + urllib.parse.quote(target, safe="") + "&hd=1",
            headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        v = d.get("data") or {}
        url = v.get("hdplay") or v.get("play") or v.get("wmplay")
        if not url:
            msg = d.get("msg") or "resolver returned no video url"
            if "parsing" in msg.lower():
                # The link itself was fine (share links are expanded above), so
                # the complaint is really about the video, not the url.
                msg += (" — TikTok would not hand this one over: it is deleted, "
                        "private, or not available outside its own country")
            return None, msg
        raw = v.get("title") or "tiktok"
        # emoji/hashtags become spaces, then collapse the runs — otherwise a title
        # like "frogs 🐸 #Minecraft today!" turns into underscore soup.
        title = "".join(c if (c.isalnum() or c in " -_") else " " for c in raw)
        title = " ".join(title.split()).strip()[:80]
        out = os.path.join(dest, f"{title or 'tiktok'} [{v.get('id') or 'tt'}].mp4")
        req2 = urllib.request.Request(url, headers={
            "User-Agent": UA, "Referer": "https://www.tiktok.com/"})
        with urllib.request.urlopen(req2, timeout=120) as r, open(out, "wb") as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
        if os.path.getsize(out) < 10240:
            os.remove(out)
            return None, "what came back was too small to be a video"
        return out, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def dl_dir():
    """Where downloaded videos go — SSD only, never created until needed."""
    p = (load_settings().get("dldir") or "").strip()
    if not p:
        return DEFAULT_DL
    p = os.path.expanduser(p)
    if not os.path.isabs(p):
        return DEFAULT_DL
    parent = os.path.dirname(p.rstrip("/")) or "/"
    return p if os.path.isdir(parent) else DEFAULT_DL


def pick_dl_folder():
    start = dl_dir()
    if not os.path.isdir(start):
        start = "/Volumes/SanDiskSSD"
    ok, out = osascript(
        'set f to choose folder with prompt "Where should downloaded videos go?" '
        f'default location (POSIX file "{start}")\n'
        'return POSIX path of f')
    out = out.rstrip("/") if ok and out.startswith("/") else ""
    if out:
        save_settings(dldir=out)
    return out


def ytdlp_fetch(link, dest):
    """Download one link with yt-dlp and return the finished file, or "".

    Only useful with the VPN up: from an Indian connection yt-dlp cannot reach
    tiktok.com at all. It is still the better TikTok route when it works (full
    original file instead of the mobile rendition).
    """
    if not os.path.exists(YTDLP):
        return ""
    tmpl = os.path.join(dest, "%(title).80s [%(id)s].%(ext)s")
    try:
        p = subprocess.run(
            [YTDLP, "--no-warnings", "--no-playlist", "--ffmpeg-location", FFMPEG,
             "-f", FMT_CAPCUT, "--merge-output-format", "mp4", "-o", tmpl,
             "--print", "after_move:filepath", link],
            capture_output=True, text=True, timeout=1800)
    except Exception:
        return ""
    if p.returncode != 0:
        return ""
    for line in reversed((p.stdout or "").strip().splitlines()):
        line = line.strip()
        if line and os.path.isfile(line):
            return line
    return ""


def fetch_tiktok(link, dest, log=None):
    """TikTok link -> a local file, using whichever route can work from here.

    Returns (path, why). With the VPN up yt-dlp goes first; with it down yt-dlp
    cannot even reach tiktok.com, so the no-VPN resolver goes first and yt-dlp
    is only a second chance. Both routes are always tried.
    """
    def say(t):
        if log:
            log(t)
    order = ("resolver",) if tiktok_blocked_here() else ("ytdlp", "resolver")
    why = ""
    for route in order:
        if route == "ytdlp":
            say("   … trying yt-dlp (VPN route)")
            path = ytdlp_fetch(link, dest)
            if path:
                return path, ""
            why = "yt-dlp could not fetch it"
        else:
            say("   … trying the no-VPN route")
            path, why = tiktok_no_vpn(link, dest, log=log)
            if path:
                return path, ""
    return "", why


# ---- which python actually has mlx_whisper --------------------------------
# The server can be started by any python3 on the Mac (the double-click
# launcher prefers homebrew's) but mlx_whisper only lives in the python.org
# framework install — so Smart mode used to die with "mlx_whisper not
# importable". Resolve the interpreter once, by asking, not by guessing.
SMART_PY = None


def smart_python():
    global SMART_PY
    if SMART_PY:
        return SMART_PY
    for p in ("/Library/Frameworks/Python.framework/Versions/3.14/bin/python3",
              "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3",
              "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
              sys.executable,
              "/opt/homebrew/bin/python3",
              "/usr/local/bin/python3"):
        if not os.path.exists(p):
            continue
        try:
            if subprocess.run([p, "-c", "import mlx_whisper"],
                              capture_output=True, timeout=90).returncode == 0:
                SMART_PY = p
                return p
        except Exception:
            continue
    SMART_PY = sys.executable
    return SMART_PY


# --------------------------------------------------------------- sharing
# A friend on your Tailscale network reaches this same server, so everything
# still runs here: your yt-dlp, your ffmpeg, your login cookies, your SSD under
# /Volumes/SanDiskSSD. The two output folders do NOT move and nobody picks a
# path for them — a visitor just pulls what is already there onto their own
# device. Only flat files inside those two folders are reachable; there is no
# way to walk the rest of the disk.
SHARE_KINDS = {"downloads": dl_dir, "clips": out_dir}


def share_root(kind):
    fn = SHARE_KINDS.get(kind)
    return fn() if fn else ""


def share_list():
    out = {}
    for kind in SHARE_KINDS:
        d = share_root(kind)
        files = []
        if d and os.path.isdir(d):
            try:
                names = os.listdir(d)
            except OSError:
                names = []
            for n in names:
                # exFAT leaves a 4 KB "._name" companion next to every file,
                # and a half-finished yt-dlp .part must not look downloadable.
                if n.startswith(".") or n.endswith(".part"):
                    continue
                p = os.path.join(d, n)
                try:
                    if not os.path.isfile(p):
                        continue
                    st = os.stat(p)
                except OSError:
                    continue
                files.append({"name": n, "size": st.st_size,
                              "mtime": int(st.st_mtime)})
        files.sort(key=lambda x: -x["mtime"])
        out[kind] = {"dir": d, "files": files}
    return out


def share_path(kind, name):
    """One file inside a share root - or "" when it misses or tries to escape."""
    d = share_root(kind)
    if not d:
        return ""
    name = os.path.basename(urllib.parse.unquote(name or ""))
    if not name or name.startswith("."):
        return ""
    real = os.path.realpath(os.path.join(d, name))
    if os.path.dirname(real) != os.path.realpath(d):
        return ""
    return real if os.path.isfile(real) else ""


def tailnet_hosts():
    """Loopback always, plus the Tailscale address when Tailscale is up.

    Binding the tailnet IP (not 0.0.0.0) is deliberate: the friend gets in, the
    rest of the local network still cannot. No Tailscale installed = unchanged.
    """
    hosts = ["127.0.0.1"]
    for cli in ("/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                "/opt/homebrew/bin/tailscale", "/usr/local/bin/tailscale"):
        if not os.path.exists(cli):
            continue
        try:
            r = subprocess.run([cli, "ip", "-4"], capture_output=True,
                               text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        ip = (r.stdout or "").strip().split("\n")[0].strip()
        if re.match(r"^100\.\d+\.\d+\.\d+$", ip) and ip not in hosts:
            hosts.append(ip)
        break
    return hosts


# ------------------------------------------------------------------ previews
# "Would this moment actually land?" — watch the clip WITHOUT cutting it.
#
# A preview is the SAME start/end as the real cut (same -ss/-to flags), encoded
# small (max 640px wide, ultrafast) into preview_cache/ on the SSD and streamed
# to a <video> tag in the page. Nothing is written to the clips folder, and the
# cache is pruned to the newest few files so it can never grow without bound.
PV_DIR = os.path.join(HERE, "preview_cache")
PV_KEEP = 40            # newest segment previews kept on disk
PV_KEEP_SRC = 3         # newest low-res source copies kept (the expensive ones)
# 360p avc1+m4a: tiny next to the real video, and enough to judge framing,
# who is on screen and whether the moment moves.
PV_FMT = ("bv*[height<=360][ext=mp4][vcodec^=avc1]+ba[ext=m4a]"
          "/b[height<=360][ext=mp4]/bv*[height<=480]+ba/b[height<=480]/b")


def pv_ts(sec):
    """Seconds -> HH:MM:SS.mmm, so the preview seeks exactly like the real cut."""
    sec = max(0.0, float(sec))
    h, rem = divmod(sec, 3600.0)
    m, s = divmod(rem, 60.0)
    return "%02d:%02d:%06.3f" % (int(h), int(m), s)


def pv_mmss(sec):
    sec = max(0, int(float(sec)))
    return "%02d:%02d" % (sec // 60, sec % 60)


def pv_dir(kind):
    d = os.path.join(PV_DIR, kind)
    os.makedirs(d, exist_ok=True)
    return d


def pv_key(*bits):
    return hashlib.sha1("|".join(str(b) for b in bits).encode()).hexdigest()[:16]


def pv_prune(kind, keep):
    """Keep only the newest `keep` files — a cache, not an archive."""
    d = os.path.join(PV_DIR, kind)
    if not os.path.isdir(d):
        return
    files = []
    for f in os.listdir(d):
        p = os.path.join(d, f)
        if f.startswith(".") or not os.path.isfile(p):
            continue
        files.append(p)
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    for p in files[keep:]:
        for q in (p, os.path.join(os.path.dirname(p), "._" + os.path.basename(p))):
            try:
                os.remove(q)          # the SSD writes AppleDouble ._ companions
            except OSError:
                pass


def pv_size(kind):
    d = os.path.join(PV_DIR, kind)
    total = 0
    if os.path.isdir(d):
        for f in os.listdir(d):
            p = os.path.join(d, f)
            try:
                if os.path.isfile(p):
                    total += os.path.getsize(p)
            except OSError:
                pass
    return total


def pv_mb(kind):
    return round(pv_size(kind) / 1048576.0, 1)


def pv_source(source, log):
    """A local video file to cut previews from, plus a note for the page.

    A local file is used as-is. A link needs real pixels, so a small 360p copy
    is downloaded ONCE per video and reused for every later preview of it.
    """
    src = os.path.expanduser((source or "").strip())
    if not src:
        raise RuntimeError("no video given")
    if not re.match(r"^https?://", src, re.I):
        if not os.path.isfile(src):
            raise RuntimeError("can't find that video file: " + src)
        return src, ""
    if not os.path.exists(YTDLP):
        raise RuntimeError("yt-dlp is missing at " + YTDLP)
    d = pv_dir("src")
    hit = os.path.join(d, pv_key(src, "src360") + ".mp4")
    if os.path.isfile(hit) and os.path.getsize(hit) > 200000:
        return hit, "using the small copy of this video from last time"
    log("downloading a small 360p copy of this video once — every preview of "
        "this video is instant after that")
    part = hit + ".part.mp4"
    if os.path.exists(part):
        os.remove(part)
    proc = subprocess.Popen(
        [YTDLP, "--no-warnings", "--no-playlist", "--newline",
         "--ffmpeg-location", FFMPEG, "-f", PV_FMT,
         "--merge-output-format", "mp4", "-o", part, src],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        bufsize=1, cwd=d)
    last = -25.0
    for line in proc.stdout:
        line = line.rstrip("\n")
        if line.startswith("[download]") and "%" in line:
            try:
                pct = float(line.split("%")[0].split()[-1])
            except Exception:
                continue
            if (pct >= last + 25 or pct >= 100) and pct != last:
                last = pct
                log("copy: %.0f%%" % pct)
        elif "ERROR" in line:
            log(line[:170])
    try:
        proc.wait(timeout=3600)
    except Exception:
        proc.kill()
        raise RuntimeError("the download of the small copy timed out")
    if not os.path.isfile(part) or os.path.getsize(part) < 200000:
        raise RuntimeError("couldn't get a copy of that video to preview "
                           "(yt-dlp failed — details in the log above)")
    os.replace(part, hit)
    pv_prune("src", PV_KEEP_SRC)
    return hit, "small 360p copy saved once — reused for every preview here"


def pv_cut(src, start, end, dest):
    """Same window as the real cut, just small and quick to encode."""
    subprocess.run(
        [FFMPEG, "-y", "-v", "error",
         "-ss", pv_ts(start), "-to", pv_ts(end), "-i", src,
         "-vf", r"scale=min(640\,iw):-2",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
         "-movflags", "+faststart", dest], check=True)


def pv_concat(parts, dest):
    """A joined clip (two moments + a dropped middle) previews as one file."""
    lst = dest + ".txt"
    with open(lst, "w", encoding="utf-8") as f:
        for p in parts:
            f.write("file '%s'\n" % p.replace("'", "'\\''"))
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0",
                    "-i", lst, "-c", "copy", dest], check=True)
    try:
        os.remove(lst)
    except OSError:
        pass


# ------------------------------------------------------------------- the UI

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Viral Finder</title>
<style>
  :root{
    --bg:#0e0f13; --panel:#16181f; --panel2:#1c1f28; --line:#2a2f3b;
    --txt:#e8eaf0; --dim:#8b93a7; --accent:#5b8cff; --good:#38d39f;
    --warn:#ffb454; --bad:#ff6b6b;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
       font:15px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif}
  .wrap{max-width:900px;margin:0 auto;padding:28px 20px 80px}
  h1{font-size:22px;margin:0 0 4px;letter-spacing:-.2px}
  h1 span{color:var(--accent)}
  .sub{color:var(--dim);font-size:13.5px;margin-bottom:24px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;
        padding:20px;margin-bottom:16px}
  label{display:block;font-size:12.5px;font-weight:600;text-transform:uppercase;
        letter-spacing:.5px;color:var(--dim);margin-bottom:8px}
  input[type=text],textarea{width:100%;background:var(--bg);border:1px solid var(--line);
        color:var(--txt);border-radius:8px;padding:11px 13px;font-size:15px;
        font-family:inherit}
  input[type=text]:focus,textarea:focus{outline:none;border-color:var(--accent)}
  textarea{resize:vertical;min-height:96px;
        font-family:ui-monospace,Menlo,monospace;font-size:12.5px;line-height:1.65}
  .row{display:flex;gap:10px;margin-top:10px;align-items:center;flex-wrap:wrap}
  .or{color:var(--dim);font-size:13px}
  button{font-family:inherit;font-size:14px;font-weight:600;border-radius:8px;
        border:1px solid var(--line);background:var(--panel2);color:var(--txt);
        padding:10px 16px;cursor:pointer;transition:.12s}
  button:hover{border-color:#3d4657;background:#232734}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
  button.primary:hover{background:#4a7bf0}
  button:disabled{opacity:.45;cursor:not-allowed}
  button.mode.on{border-color:var(--accent);background:#1b2540}
  button.mode.on>div:first-child{color:#cfe0ff}
  button.sm{padding:6px 11px;font-size:12.5px}
  .grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px}
  @media(max-width:640px){.grid{grid-template-columns:1fr}}
  .hint{color:var(--dim);font-size:12px;margin-top:6px}
  .path{font-family:ui-monospace,Menlo,monospace;font-size:12.5px;color:var(--good);
        word-break:break-all;margin-top:8px;min-height:18px}
  .chk{display:flex;align-items:center;gap:9px;cursor:pointer;user-select:none}
  .chk input{width:17px;height:17px;accent-color:var(--accent);cursor:pointer}
  .chk span{font-size:14px;font-weight:500}
  .log{background:#0a0b0e;border:1px solid var(--line);border-radius:8px;
       padding:13px;font-family:ui-monospace,Menlo,monospace;font-size:12.5px;
       color:#b9c2d4;white-space:pre-wrap;max-height:230px;overflow:auto;
       margin-top:14px;display:none}
  .log.on{display:block}
  .spin{display:inline-block;width:13px;height:13px;border:2px solid #3d4657;
        border-top-color:var(--accent);border-radius:50%;
        animation:sp .7s linear infinite;vertical-align:-2px;margin-right:8px}
  @keyframes sp{to{transform:rotate(360deg)}}
  .clip{background:var(--panel2);border:1px solid var(--line);border-radius:10px;
        padding:14px 16px;margin-bottom:10px;display:flex;flex-wrap:wrap;
        justify-content:space-between;align-items:center;gap:14px}
  .clip .acts{display:flex;gap:8px;align-items:center;flex-shrink:0}
  .clip .pv{flex-basis:100%;order:99;display:none}
  .clip .pv.on{display:block}
  .clip .pv video{width:100%;max-width:480px;border-radius:8px;background:#000;
        display:block;margin-top:2px}
  .pvstat{color:var(--dim);font-size:12.5px;margin-top:7px}
  .pvstat.bad{color:#ffb3b3}
  .clip .t{font-family:ui-monospace,Menlo,monospace;font-size:17px;
        font-weight:600;letter-spacing:-.3px}
  .clip .meta{color:var(--dim);font-size:12.5px;margin-top:3px}
  .bar{height:4px;background:var(--line);border-radius:2px;margin-top:9px;width:150px}
  .bar i{display:block;height:100%;background:var(--accent);border-radius:2px}
  .tag{display:inline-block;font-size:11px;font-weight:700;padding:2px 7px;
       border-radius:5px;background:#243044;color:#8fb3ff;margin-left:8px;
       text-transform:uppercase;letter-spacing:.4px;vertical-align:1px}
  .tag.audio{background:#2e2a1e;color:var(--warn)}
  .err{background:#2a1c1f;border:1px solid #5b2b30;color:#ffb3b3;
       padding:12px 14px;border-radius:8px;margin-top:12px;display:none}
  .err.on{display:block}
  .note{background:#1b2540;border:1px solid #33507f;color:#bcd3ff;
        padding:14px 16px;border-radius:8px;margin-top:12px;display:none;
        font-size:14px;line-height:1.55}
  .note.on{display:block}
  .note b{color:#dbe8ff}
  .foot{color:var(--dim);font-size:12.5px;text-align:center;margin-top:26px}
  .foot a{color:var(--dim);cursor:pointer}
</style></head><body>
<div class="wrap">

  <h1>Viral <span>Finder</span></h1>
  <div class="sub">Finds the moments people actually rewatch — and cuts them. Runs entirely on your Mac, no API cost.</div>

  <div class="card">
    <label>1 · Where's the video?</label>
    <input type="text" id="src" placeholder="Paste a YouTube or TikTok link  —  https://youtube.com/watch?v=..." autocomplete="off">
    <div class="row">
      <span class="or">or</span>
      <button id="btnPick">📁 Choose a video on my Mac</button>
    </div>
    <div class="path" id="picked"></div>
    <div class="hint" id="srcHint">A YouTube link lets me read the video's own captions (fast, and works with either mode). A TikTok link gets downloaded to your download folder first (share links like vm.tiktok.com/… included), then read like a local file. A local file has no captions, so it must be transcribed by whisper first.</div>
  </div>

  <div class="card">
    <label>2 · How should I judge what's viral?</label>
    <div class="row" style="margin-top:0">
      <button class="mode on" id="mFast" data-mode="fast" style="flex:1;text-align:left;padding:12px 14px">
        <div style="font-weight:700">⚡ Fast</div>
        <div style="font-weight:400;font-size:12px;color:var(--dim);margin-top:3px">YouTube's rewatch data · 4 seconds</div>
      </button>
      <button class="mode" id="mSmart" data-mode="smart" style="flex:1;text-align:left;padding:12px 14px">
        <div style="font-weight:700">🧠 Smart</div>
        <div style="font-weight:400;font-size:12px;color:var(--dim);margin-top:3px">Reads it properly · 5-15 min</div>
      </button>
    </div>
    <div class="hint" id="modeHint"><b>Fast</b> — replays mean attention, not that a clip would explode. Good for a quick shortlist.</div>
  </div>

  <div class="card">
    <label>3 · Settings</label>
    <div class="grid">
      <div>
        <label style="margin-bottom:6px">How many moments</label>
        <input type="text" id="top" value="5">
      </div>
      <div>
        <label style="margin-bottom:6px">Skip first (seconds)</label>
        <input type="text" id="skip" value="0">
        <div class="hint">Use ~90 on YouTube videos to skip the intro.</div>
      </div>
      <div>
        <label style="margin-bottom:6px">Clip length (seconds)</label>
        <div style="display:flex;gap:8px;align-items:center">
          <input type="text" id="minlen" value="30" style="width:74px">
          <span class="or">to</span>
          <input type="text" id="maxlen" value="150" style="width:74px">
        </div>
        <div class="hint">Each clip runs on until the moment finishes, then stops at a natural pause in this range.</div>
      </div>
    </div>
    <div class="row" style="margin-top:16px">
      <label class="chk" style="margin:0"><input type="checkbox" id="cut"><span>Also cut the clips into mp4 files</span></label>
    </div>
    <div id="smartOpts" style="display:none;margin-top:14px">
      <label class="chk" style="margin:0 0 9px"><input type="checkbox" id="stitch" checked><span>Join two far-apart moments into one clip (e.g. 10-30s + 60-78s, filler dropped)</span></label>
      <label class="chk" style="margin:0"><input type="checkbox" id="hybrid" checked><span>Also offer YouTube's most-replayed stretches when the talk is thin</span></label>
    </div>
    <div class="row" id="outRow" style="margin-top:12px">
      <button class="sm" id="btnFolder">📁 Choose where clips are saved</button>
      <span class="or">saving to</span>
      <span class="path" id="outPath" style="margin:0"></span>
      <span class="or" id="outHint"></span>
    </div>
  </div>

  <div class="card">
    <label>Download videos</label>
    <div class="hint" style="margin:0 0 10px">Paste links — one per line. YouTube, Instagram, TikTok. TikTok share links (vm.tiktok.com/…, tiktok.com/t/…) work too.</div>
    <textarea id="dlLinks" spellcheck="false" placeholder="https://www.youtube.com/watch?v=...&#10;https://www.instagram.com/reel/...&#10;https://www.tiktok.com/@user/video/..."></textarea>
    <div class="row" style="margin-top:12px">
      <button class="sm" id="btnDlFolder">📁 Choose where downloads are saved</button>
      <span class="or">saving to</span>
      <span class="path" id="dlPath" style="margin:0"></span>
      <span class="or" id="dlHint"></span>
    </div>
    <div class="row" style="margin-top:12px">
      <span class="or">Quality</span>
      <button class="sm on" id="qSafe">🎬 CapCut-safe</button>
      <button class="sm" id="qMax">💎 Absolute best</button>
    </div>
    <div class="hint" id="qHint">H.264 + AAC — imports into CapCut cleanly, same resolution.</div>
    <div class="row" style="margin-top:12px">
      <label class="chk" style="margin:0"><input type="checkbox" id="dlForce"><span>Re-download videos I already have</span></label>
    </div>
    <div class="row" style="margin-top:16px">
      <button id="btnDownload">⬇ Download</button>
    </div>
    <div class="log" id="dlLog"></div>
    <div class="note" id="dlNote"></div>
  </div>

  <div class="card">
    <label>Shared files</label>
    <div class="hint" style="margin:0 0 10px">What's already on this Mac, ready to save onto whatever device you're reading this on. The files stay exactly where they are — this never changes a folder.</div>
    <div class="row" style="margin-top:0">
      <button class="sm on" id="shDl">📥 Downloads</button>
      <button class="sm" id="shClips">✂️ Clips</button>
      <button class="sm" id="shRefresh" style="margin-left:auto">↻ Refresh</button>
    </div>
    <div class="hint" id="shWhere"></div>
    <div id="shList" style="margin-top:6px"></div>
  </div>

  <div class="row" style="margin-bottom:4px">
    <button class="primary" id="btnRun" style="padding:12px 26px">🔍 Find viral moments</button>
    <button id="btnQuit" class="sm" style="margin-left:auto">Quit</button>
  </div>

  <div class="log" id="log"></div>
  <div class="err" id="err"></div>
  <div class="note" id="note"></div>
  <div id="out"></div>

  <div class="foot">Leave this tab open while it runs · <a id="reveal">Open output folder</a></div>
</div>

<script>
const $ = id => document.getElementById(id);
let pickedFile = "", outFolder = "", DEFOUT = "/Volumes/SanDiskSSD/Viral Clips", busy = false, es = null, mode = "fast", dlDir = "", dlQ = "safe", RESULTS = [], lastSrc = "";

function esc(s){ return (s||"").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
function hms(t){
  t = Math.max(0, Math.floor(t));
  const h = Math.floor(t/3600), m = Math.floor(t%3600/60), s = t%60;
  const p = n => String(n).padStart(2,"0");
  return h ? `${h}:${p(m)}:${p(s)}` : `${p(m)}:${p(s)}`;
}
function logLine(txt){
  const L = $("log"); L.classList.add("on");
  L.textContent += txt + "\n"; L.scrollTop = L.scrollHeight;
}

$("top").oninput = $("skip").oninput = () => {};
$("src").oninput = () => {
  const v = $("src").value.trim();
  $("picked").textContent = "";
  pickedFile = "";
  $("srcHint").textContent = v
    ? (v.startsWith("http")
        ? (v.toLowerCase().includes("tiktok")
            ? "TikTok link — I'll download it here first, then go through it like a local file."
            : "YouTube link — captions can be read straight from the video.")
        : "Looks like a path — this will be transcribed by whisper.")
    : "A YouTube link lets me read the video's own captions (fast, and works with either mode). A TikTok link gets downloaded to your download folder first (share links like vm.tiktok.com/… included), then read like a local file. A local file has no captions, so it must be transcribed by whisper first.";
};

$("btnPick").onclick = async () => {
  const r = await fetch("/pick"); const d = await r.json();
  if (d.path){ pickedFile = d.path; $("src").value = ""; $("picked").textContent = "🎬 " + d.path; }
};

document.querySelectorAll("button.mode").forEach(b => {
  b.onclick = () => {
    mode = b.dataset.mode;
    document.querySelectorAll("button.mode").forEach(x => x.classList.toggle("on", x === b));
    const smart = mode === "smart";
    $("skip").disabled = false;
    $("skip").style.opacity = 1;
    $("smartOpts").style.display = smart ? "block" : "none";
    $("btnRun").textContent = smart ? "🧠 Read the transcript & find moments"
                                    : "🔍 Find viral moments";
    $("modeHint").innerHTML = smart
      ? "<b>Smart</b> — first reads the whole video to work out what it's about, then judges each part against that, ranks moments instead of rejecting them, and can join two separate moments into one clip. Slower, but it almost never comes back empty. Best used on a YouTube link."
      : "<b>Fast</b> — replays mean attention, not that a clip would explode. Good for a quick shortlist.";
  };
});
// ---- shared files: what's already on the Mac, pullable to this device ----
let shKind = "downloads", shData = {};
const shBtn = 'text-decoration:none;border:1px solid var(--line);background:var(--panel2);color:var(--txt);border-radius:8px;padding:6px 11px;font-size:12.5px';
function shRender(){
  const box = $("shList"), d = shData[shKind] || {files:[]};
  $("shWhere").textContent = d.dir ? "on the Mac at " + d.dir : "";
  if (!d.files || !d.files.length){ box.innerHTML = '<div class="hint">Nothing here yet.</div>'; return; }
  box.innerHTML = d.files.map(f => {
    const mb = f.size / 1048576;
    const size = mb >= 1024 ? (mb/1024).toFixed(2) + " GB" : mb.toFixed(1) + " MB";
    const u = "/shared/" + shKind + "/" + encodeURIComponent(f.name);
    return `<div style="display:flex;gap:9px;align-items:center;padding:7px 0;border-top:1px solid var(--line)">
      <div style="flex:1;min-width:0">
        <div style="font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(f.name)}</div>
        <div style="font-size:11.5px;color:var(--dim)">${size} · ${new Date(f.mtime*1000).toLocaleDateString()}</div>
      </div>
      <a style="${shBtn}" href="${u}" target="_blank" rel="noopener">▶ Play</a>
      <a style="${shBtn}" href="${u}?dl=1">⬇ Save</a>
    </div>`;
  }).join("");
}
function shLoad(){ fetch("/files").then(r=>r.json()).then(d=>{ shData = d; shRender(); }).catch(()=>{}); }
$("shDl").onclick = () => { shKind="downloads"; $("shDl").classList.add("on"); $("shClips").classList.remove("on"); shRender(); };
$("shClips").onclick = () => { shKind="clips"; $("shClips").classList.add("on"); $("shDl").classList.remove("on"); shRender(); };
$("shRefresh").onclick = shLoad;
shLoad();

// load the remembered clips folder on open
fetch("/settings").then(r => r.json()).then(d => {
  DEFOUT = d.default || DEFOUT;
  outFolder = d.outdir || "";
  $("outPath").textContent = outFolder || DEFOUT;
  $("outHint").textContent = d.chosen ? "" : "· not chosen yet — click to pick";
  dlDir = d.dldir || "";
  $("dlPath").textContent = dlDir || d.dl_default || "";
  $("dlHint").textContent = d.dl_chosen ? "" : "· not chosen yet — click to pick";
  if (d.remote){
    // A visitor cannot pick a folder on this Mac or stop the server, so those
    // buttons are hidden and the server refuses them anyway.
    ["btnPick","btnFolder","btnDlFolder","btnQuit","reveal"].forEach(id => {
      const el = $(id); if (el) el.style.display = "none";
    });
    $("outHint").textContent = ""; $("dlHint").textContent = "";
  }
}).catch(() => {});

$("btnFolder").onclick = async () => {
  const r = await fetch("/pickfolder"); const d = await r.json();
  if (d.path){ outFolder = d.path; $("outPath").textContent = d.path; }
};
$("reveal").onclick = () => fetch("/open?path=" + encodeURIComponent(
  outFolder || DEFOUT)).then(()=>{});
$("btnQuit").onclick = () => { if (confirm("Stop the Viral Finder server?")) fetch("/quit").then(()=>{ document.body.innerHTML =
  '<div class="wrap"><h1>Stopped.</h1><div class="sub">You can close this tab. Reopen the Viral Finder icon to start again.</div></div>'; }); };

$("btnRun").onclick = () => {
  if (busy) return;
  const src = pickedFile || $("src").value.trim();
  if (!src){ alert("Paste a YouTube link or choose a video first."); return; }

  busy = true;
  lastSrc = src;
  $("btnRun").disabled = true;
  $("out").innerHTML = "";
  $("err").classList.remove("on");
  $("note").classList.remove("on");
  $("log").classList.remove("on"); $("log").textContent = "";
  logLine(mode === "smart"
    ? "🧠 Reading the transcript and judging each moment… (a long video takes a few minutes — leave this tab open)"
    : "🔎 Working…  (a long video can take a minute)");

  const q = new URLSearchParams({
    mode: mode,
    source: src,
    top: $("top").value.trim() || "5",
    skip: $("skip").value.trim() || "0",
    min: $("minlen").value.trim() || "30",
    max: $("maxlen").value.trim() || "150",
    cut: $("cut").checked ? "1" : "0",
    stitch: $("stitch").checked ? "1" : "0",
    hybrid: $("hybrid").checked ? "1" : "0",
    outdir: outFolder
  });

  es = new EventSource("/run?" + q.toString());
  es.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.type === "log"){ logLine(m.text); }
    else if (m.type === "empty"){ showNote(m.text); finish(); }
    else if (m.type === "error"){ showErr(m.text); finish(); }
    else if (m.type === "done"){ render(m.payload); finish(); }
  };
  es.onerror = () => { showErr("Lost connection to the local server. Is the Viral Finder window still open?"); finish(); };
};

function finish(){
  busy = false; $("btnRun").disabled = false;
  if (es){ es.close(); es = null; }
}
function showErr(t){ $("err").textContent = t; $("err").classList.add("on"); logLine("❌ " + t); }
function showNote(t){ $("note").textContent = t; $("note").classList.add("on"); logLine("ℹ " + t); }

const TIER = {strong:"🔥 strongest", ok:"solid", mild:"mild", replay:"most replayed"};
function clipLabel(c){   // a joined clip has two ranges: 10:00-10:30  +  11:02-11:18
  if (c.parts && c.parts.length > 1)
    return c.parts.map(p => hms(p[0]) + "-" + hms(p[1])).join("  +  ");
  return hms(c.start) + " - " + hms(c.end);
}

function render(payload){
  const rs = payload.results || [];
  if (!rs.length){ showErr((payload.errors && payload.errors[0] && payload.errors[0].error) || "Nothing found."); return; }
  RESULTS = rs;
  let html = "";
  rs.forEach((r, ri) => {
    const isSmart = r.mode === "transcript-llm";
    const isHeat = r.mode === "youtube-heatmap";
    const tag = isSmart ? "AI transcript" : (isHeat ? "most replayed" : "audio energy");
    html += `<div class="card" style="margin-top:16px">
      <div style="font-weight:600;font-size:16px;word-break:break-word">${esc(r.title)}
        <span class="tag ${isHeat?"":"audio"}">${tag}</span></div>
      <div class="hint" style="margin-top:4px">${hms(r.duration)} long · ${r.clips.length} moments found${r.model?" · judged by "+esc(r.model):""}</div>`;
    if (r.context && r.context.subject)
      html += `<div class="hint" style="margin-top:6px;color:#9fb4d8">🧭 ${esc(r.context.subject)}${r.context.what_travels?" — "+esc(r.context.what_travels):""}</div>`;
    if (r.note)
      html += `<div class="hint" style="margin-top:6px;color:#e0c37a">ℹ ${esc(r.note)}</div>`;
    r.clips.forEach((c,i) => {
      const pct = Math.round(Math.min(1,c.score) * 100);
      const len = (c.length!=null ? c.length : (c.end - c.start));
      const merged = !!(c.stitched || c.merged);
      const range = clipLabel(c);
      const meta = isSmart
        ? [c.tier?TIER[c.tier]||c.tier:"",
           c.stitched ? "JOINED - the filler between them was dropped" : "",
           c.merged && !c.stitched ? "one continuous stretch (neighbouring picks merged)" : "",
           c.kind, c.why].filter(Boolean).map(esc).join(" · ")
        : (r.chapters&&r.chapters.length ? esc(r.chapters[i]||"") : "peak score "+c.score.toFixed(2));
      const says = c.text ? `<div class="meta" style="margin-top:7px;color:#aab3c6">“${esc(c.text.slice(0,190))}…”</div>` : "";
      html += `<div class="clip">
        <div style="flex:1">
          <div class="t">${range}</div>
          <div class="meta">clip ${i+1} · ${Math.round(len)}s${meta?" · "+meta:""}</div>
          ${says}
          <div class="bar"><i style="width:${pct}%"></i></div>
        </div>
        <div class="acts">
          <button class="sm" onclick="previewClip(${ri},${i},this)">▶ Preview</button>
          <button class="sm" data-range="${range.replace(/"/g,"")}" onclick="navigator.clipboard.writeText(this.dataset.range);this.textContent='✓ Copied';setTimeout(()=>this.textContent='Copy',1200)">Copy</button>
        </div>
        <div class="pv"></div>
      </div>`;
    });
    const all = r.clips.map((c,i)=>`${i+1}. ${clipLabel(c)}`).join("\n");
    html += `<div class="row"><button class="sm" onclick='navigator.clipboard.writeText(${JSON.stringify(all)});this.textContent="✓ Copied all";setTimeout(()=>this.textContent="Copy all timestamps",1200)'>Copy all timestamps</button>
      <button class="sm" onclick="fetch('/open?path='+encodeURIComponent(${JSON.stringify(outFolder||DEFOUT)}))">Open clips folder</button>
      <button class="sm" onclick="clearPreviews(this)">🗑 Clear cached previews</button>
      <span class="hint" style="margin:0">▶ Preview plays the moment as it would be cut — nothing lands in your clips folder.</span></div></div>`;
  });
  $("out").innerHTML = html;
  logLine("✅ Done.");
}

// ---------------------------------------------------------------- previews
// Watch a moment without cutting it. The server encodes a small throwaway copy
// of exactly this start/end (a joined clip previews with its middle dropped,
// same as the real file) and streams it here.
function clipParts(c){
  return (c.parts && c.parts.length > 1) ? c.parts : [[c.start, c.end]];
}
function previewClip(ri, ci, btn){
  const r = RESULTS[ri], c = r.clips[ci];
  const host = btn.closest(".clip").querySelector(".pv");
  host.classList.add("on");
  if (host.dataset.ready){                       // already built: just replay
    const v = host.querySelector("video"); if (v) v.play().catch(()=>{});
    return;
  }
  const src = (r.source || lastSrc || "").trim();
  if (!src){ host.innerHTML = '<div class="pvstat bad">I lost track of which video this came from — run the search again.</div>'; return; }
  btn.disabled = true; btn.textContent = "▶ Preview";
  host.innerHTML = '<div class="pvstat"><span class="spin"></span>preparing the preview…</div>';
  const stat = () => host.querySelector(".pvstat");
  const q = new URLSearchParams({source: src, start: c.start, end: c.end,
                                 parts: JSON.stringify(clipParts(c))});
  const es = new EventSource("/preview?" + q.toString());
  let done = false;
  es.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.type === "log"){ if (stat()) stat().innerHTML = '<span class="spin"></span>' + esc(m.text); }
    else if (m.type === "error"){
      done = true; es.close(); btn.disabled = false;
      host.innerHTML = '<div class="pvstat bad">' + esc(m.text) + "</div>";
    }
    else if (m.type === "done"){
      done = true; es.close(); btn.disabled = false; host.dataset.ready = "1";
      host.innerHTML = '<video controls playsinline preload="auto" src="' + m.url + '"></video>' +
        '<div class="pvstat">' + esc(clipLabel(c)) + " · " + Math.round(m.secs) +
        "s · preview only, nothing was saved to your clips folder" +
        (m.cached ? " · already cached" : "") + "</div>";
      const v = host.querySelector("video");
      if (v) v.play().catch(() => {      // if the browser blocks the sound, play
        v.muted = true;                  // it muted rather than doing nothing
        v.play().catch(() => {});
        const st = host.querySelector(".pvstat");
        if (st) st.textContent += " · playing muted — unmute in the player";
      });
      logLine("▶ preview ready: " + clipLabel(c));
    }
  };
  es.onerror = () => {
    if (done) return;
    es.close(); btn.disabled = false;
    host.innerHTML = '<div class="pvstat bad">the preview stopped — is the Viral Finder window still open?</div>';
  };
}
function clearPreviews(btn){
  if (btn) btn.disabled = true;
  fetch("/pvclear").then(r => r.json()).then(d => {
    if (btn) btn.disabled = false;
    logLine("🗑 cleared " + d.removed + " cached preview(s) · previews: " + d.clips_mb +
            " MB now · small video copies kept so previews stay fast: " + d.src_mb + " MB");
  }).catch(() => {});
  document.querySelectorAll(".clip .pv.ready").forEach(() => {});
}

// ---------------------------------------------------------------- downloads
function setQ(q){
  dlQ = q;
  $("qSafe").classList.toggle("on", q === "safe");
  $("qMax").classList.toggle("on", q === "max");
  $("qHint").textContent = (q === "safe")
    ? "H.264 + AAC — imports into CapCut cleanly, same resolution."
    : "Highest bitrate/codec — often AV1 + Opus, which CapCut can refuse to import.";
}
$("qSafe").onclick = () => setQ("safe");
$("qMax").onclick  = () => setQ("max");
setQ("safe");

$("btnDlFolder").onclick = async () => {
  const r = await fetch("/dlpick"); const d = await r.json();
  if (d.path){ dlDir = d.path; $("dlPath").textContent = d.path; $("dlHint").textContent = ""; }
};

$("btnDownload").onclick = () => {
  const links = $("dlLinks").value.trim();
  if (!links){ alert("Paste at least one link first."); return; }
  const btn = $("btnDownload"), lg = $("dlLog"), n = $("dlNote");
  btn.disabled = true;
  n.classList.remove("on");
  lg.classList.add("on"); lg.textContent = "";
  const qs = new URLSearchParams({ links: links, dldir: dlDir, quality: dlQ,
                                   force: $("dlForce").checked ? "1" : "0" });
  const ses = new EventSource("/dl?" + qs.toString());
  const add = t => { lg.textContent += t + "\n"; lg.scrollTop = lg.scrollHeight; };
  ses.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.type === "log"){ add(m.text); }
    else if (m.type === "error"){ add("❌ " + m.text); btn.disabled = false; ses.close(); }
    else if (m.type === "dl_done"){
      ses.close(); btn.disabled = false;
      const ok = (m.files || []).length,
            sk = (m.skipped || []).length,
            bad = (m.failed || []).length;
      const parts = [];
      if (ok)  parts.push(`✅ ${ok} downloaded`);
      if (sk)  parts.push(`⏭ ${sk} already saved`);
      if (bad) parts.push(`❌ ${bad} failed`);
      let msg = parts.length ? parts.join(" · ") : "Nothing to do.";
      if (sk && !ok) msg += ' — tick "Re-download videos I already have" to fetch them again';
      if (ok || sk) msg += "  →  " + m.dest;
      n.textContent = msg;
      n.classList.add("on");
      if (m.dest) $("dlPath").textContent = m.dest;
      shLoad();
    }
  };
  ses.onerror = () => { ses.close(); btn.disabled = false; };
};
</script>
</body></html>
"""


# ------------------------------------------------------------------ server

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):        # keep the console quiet
        pass

    def is_remote(self):
        """True when the request came from the network (a friend), not this
        Mac. Native dialogs and /quit stay the owner's alone: a friend
        clicking them would pop windows on his screen or kill his server."""
        return self.client_address[0] not in ("127.0.0.1", "::1")

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)

        if u.path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")

        if u.path in ("/pick", "/pickfolder", "/dlpick"):
            if self.is_remote():
                return self._send(403, json.dumps({"error": "owner only"}))
            if u.path == "/pick":
                return self._send(200, json.dumps({"path": pick_file()}))
            if u.path == "/pickfolder":
                return self._send(200, json.dumps({"path": pick_folder()}))
            return self._send(200, json.dumps({"path": pick_dl_folder()}))

        if u.path == "/settings":
            s = load_settings()
            return self._send(200, json.dumps({
                "outdir": out_dir(),
                "chosen": bool((s.get("outdir") or "").strip()),
                "default": DEFAULT_OUT,
                "dldir": dl_dir(),
                "dl_chosen": bool((s.get("dldir") or "").strip()),
                "dl_default": DEFAULT_DL,
                "pv_clips_mb": pv_mb("clips"),
                "pv_src_mb": pv_mb("src"),
                "remote": self.is_remote(),
            }))

        if u.path == "/open":
            if self.is_remote():
                return self._send(403, json.dumps({"error": "owner only"}))
            p = os.path.expanduser(q.get("path", ["~"])[0])
            if os.path.exists(p):
                subprocess.Popen(["/usr/bin/open", p])
            return self._send(200, json.dumps({"ok": True}))

        if u.path == "/quit":
            if self.is_remote():
                return self._send(403, json.dumps({"error": "owner only"}))
            self._send(200, json.dumps({"ok": True}))
            threading.Timer(0.4, lambda: os._exit(0)).start()
            return

        if u.path == "/run":
            return self.stream_run(q)

        if u.path == "/dl":
            return self.stream_dl(q)

        if u.path == "/preview":
            return self.stream_preview(q)

        if u.path.startswith("/pv/"):
            return self.serve_media(u.path[4:])

        if u.path == "/pvclear":
            # Only the throwaway segment files go; the small source copies stay,
            # because re-downloading them is what makes a preview slow.
            removed = 0
            d = os.path.join(PV_DIR, "clips")
            if os.path.isdir(d):
                for f in os.listdir(d):
                    try:
                        os.remove(os.path.join(d, f))
                        removed += 1
                    except OSError:
                        pass
            return self._send(200, json.dumps(
                {"ok": True, "removed": removed,
                 "src_mb": pv_mb("src"), "clips_mb": pv_mb("clips")}))

        if u.path == "/files":
            return self._send(200, json.dumps(share_list()))

        if u.path.startswith("/shared/"):
            bits = [b for b in u.path[len("/shared/"):].split("/") if b]
            if len(bits) != 2:
                return self._send(404, json.dumps({"error": "no such file"}))
            real = share_path(bits[0], bits[1])
            if not real:
                return self._send(404, json.dumps({"error": "no such file"}))
            ctype = mimetypes.guess_type(real)[0] or "application/octet-stream"
            return self._stream_file(real, ctype,
                                     attach=(q.get("dl", [""])[0] == "1"))

        self._send(404, json.dumps({"error": "not found"}))

    # ---- SSE run -------------------------------------------------------
    def stream_run(self, q):
        def one(name, default=""):
            return (q.get(name, [default])[0] or default).strip()

        src = one("source")
        if not src:
            return self._send(400, json.dumps({"error": "no source"}))

        # "smart" reads the transcript with a local model (slower, better picks);
        # anything else uses the heatmap / audio-energy signal.
        lo, hi = one("min", "30"), one("max", "150")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        jf = os.path.join(HERE, ".last_run.json")
        env = dict(os.environ, PYTHONUNBUFFERED="1")

        def emit(obj):
            try:
                self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
                self.wfile.flush()
                return True
            except Exception:
                return False

        # A TikTok link is not something the analysers can fetch for themselves:
        # from an Indian connection yt-dlp cannot even reach tiktok.com. Pull it
        # in here first (share links included) and analyse the local file.
        if is_tiktok(src) and not os.path.exists(os.path.expanduser(src)):
            if is_tiktok_short(src):
                # Trade the share link for the real video now, so the "already
                # saved?" check below can actually recognise the file.
                kind0, vid0, why0 = expand_tiktok(src)
                if kind0 == "photo":
                    return emit({"type": "error", "text":
                                 "that share link points at a TikTok photo post "
                                 "(slideshow) — there is no video for me to read"})
                if not vid0:
                    return emit({"type": "error", "text":
                                 f"could not read that TikTok share link ({why0}) — "
                                 "the link may be expired; open the video and copy "
                                 "the full address (tiktok.com/@user/video/1234…) "
                                 "instead"})
                src = f"https://www.tiktok.com/@/video/{vid0}"
            d = dl_dir()
            try:
                os.makedirs(d, exist_ok=True)
            except OSError:
                d = DEFAULT_DL
                os.makedirs(d, exist_ok=True)
            vid = tiktok_video_id(src)
            hit = ""
            if vid:
                # Same video already pulled in earlier? Reuse it — re-downloading
                # a video just to analyse it again would be pure waiting.
                for n in os.listdir(d):
                    if vid in n and n.lower().endswith(".mp4") and not n.startswith("."):
                        hit = os.path.join(d, n)
                        break
            if hit:
                emit({"type": "log", "text":
                      f"♻ using the copy already saved: {os.path.basename(hit)}"})
                src = hit
            else:
                emit({"type": "log", "text": "⬇ TikTok link — fetching it first…"})
                path, why = fetch_tiktok(
                    src, d, lambda t: emit({"type": "log", "text": t}))
                if not path:
                    return emit({"type": "error",
                                 "text": f"could not download that TikTok: {why}"})
                src = path

        if one("mode") == "smart":
            # smart_python(), not sys.executable: the server may be running on a
            # python that has no mlx_whisper (see smart_python above).
            cmd = [smart_python(), SMART, src,
                   "--top", one("top", "5"),
                   "--min", lo, "--max", hi,
                   "--skip-intro", one("skip", "0")]
            # the two v2 extras: join far-apart moments, blend replay data
            if one("stitch", "1") == "0":
                cmd.append("--no-stitch")
            if one("hybrid", "1") == "0":
                cmd.append("--no-hybrid")
        else:
            cmd = [sys.executable, FINDER, src,
                   "--top", one("top", "5"),
                   "--skip-intro", one("skip", "0"),
                   "--min", lo, "--max", hi]
        if one("cut") == "1":
            dest = one("outdir")
            cmd += ["--cut", "--outdir",
                    os.path.expanduser(dest) if dest else out_dir()]
        cmd += ["--json-out", jf]

        try:
            proc = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    bufsize=1, env=env)
        except Exception as e:
            emit({"type": "error", "text": f"could not start: {e}"})
            return

        assert emit({"type": "log", "text": "▶ " + " ".join(
            "…" + c[-40:] if len(c) > 44 else c for c in cmd[2:])})

        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                if line:
                    emit({"type": "log", "text": line})
            proc.wait(timeout=1800)
        except Exception:
            proc.kill()

        if os.path.exists(jf):
            try:
                payload = json.loads(open(jf, encoding="utf-8").read())
                if payload.get("results"):
                    emit({"type": "done", "payload": payload})
                else:
                    err = (payload.get("errors") or [{}])[0].get("error", "Nothing found.")
                    # "found nothing" is a verdict, not a crash — show it neutrally
                    kind = ("empty" if "would work as a standalone clip" in err
                            else "error")
                    emit({"type": kind, "text": err})
            except Exception as e:
                emit({"type": "error", "text": f"could not read result: {e}"})
        else:
            emit({"type": "error", "text": "The run produced no result — see the log above."})

    # ---- SSE download --------------------------------------------------
    def stream_dl(self, q):
        def one(k, d=""):
            return (q.get(k, [d]) or [d])[0]

        links = [clean_link(l) for l in one("links").splitlines()]
        links = [l for l in links if l]
        if not links:
            return self._send(400, json.dumps({"error": "no links"}))

        dest = one("dldir")
        dest = os.path.expanduser(dest) if dest else dl_dir()
        fmt = FMT_MAX if one("quality") == "max" else FMT_CAPCUT

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(obj):
            try:
                self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
                self.wfile.flush()
                return True
            except Exception:
                return False

        if not os.path.exists(YTDLP):
            return emit({"type": "error", "text": f"yt-dlp missing at {YTDLP}"})
        parent = os.path.dirname(dest.rstrip("/")) or "/"
        if not os.path.isdir(parent):
            return emit({"type": "error",
                         "text": f"can't reach {dest} — is the SSD plugged in?"})
        os.makedirs(dest, exist_ok=True)

        emit({"type": "log", "text": f"⬇ {len(links)} link(s) → {dest}"})
        emit({"type": "log", "text": "   quality: " + (
            "absolute best (may be AV1/Opus — CapCut may reject it)"
            if fmt == FMT_MAX else "CapCut-safe (H.264 + AAC)")})

        got, failed, skipped = [], [], []
        force = one("force") == "1"
        # Decide the TikTok route ONCE per batch, not per link (it costs a request).
        tk_blocked = False
        if any(is_tiktok(l) for l in links):
            cc = exit_country()
            tk_blocked = (cc == "IN")
            emit({"type": "log", "text":
                  f"   exit country: {cc or 'unknown'} — TikTok "
                  + ("is geo-blocked here, using the no-VPN route"
                     if tk_blocked else "is reachable, using yt-dlp")})
        for i, link in enumerate(links, 1):
            emit({"type": "log", "text": f"\n[{i}/{len(links)}] {link[:110]}"})
            before = set(os.listdir(dest))
            # TikTok with the VPN off: probe first. yt-dlp would otherwise burn a
            # full connect-timeout before we fall back, which is pure dead time.
            tk = is_tiktok(link)
            if tk and tk_blocked:
                emit({"type": "log", "text":
                      "   ⚡ TikTok unreachable from here (VPN off) — going straight to the no-VPN route…"})
                path, why = tiktok_no_vpn(
                    link, dest, lambda t: emit({"type": "log", "text": t}))
                if path:
                    got.append(path)
                    emit({"type": "log", "text":
                          f"   ✅ {os.path.basename(path)}"
                          "   (no-VPN route · HD where available)"})
                    time.sleep(6)          # the resolver rate-limits if hammered
                else:
                    failed.append(link)
                    emit({"type": "log", "text": f"   ❌ no-VPN route failed: {why}"})
                continue
            # Instagram serves "empty media response" to logged-out clients, so
            # hand yt-dlp the session cookies from the Zen profile. Rebuilt per
            # link, so a fresh login in Zen works with no restart.
            ig = is_instagram(link)
            jar = None
            if ig:
                jar, note = ig_cookie_jar(force=True)
                emit({"type": "log", "text":
                      ("   🔑 using your Instagram login (" + note + ")") if jar
                      else ("   ⚠ " + note)})
            elif is_youtube(link):
                jar, note = yt_cookie_jar(force=True)
                emit({"type": "log", "text":
                      ("   🔑 using your YouTube login (" + note + ")") if jar
                      else ("   ⚠ " + note)})
            cmd = [YTDLP, "--no-warnings", "--no-playlist", "--newline",
                   "--ffmpeg-location", FFMPEG,
                   "-f", fmt, "--merge-output-format", "mp4",
                   "-o", os.path.join(dest, "%(title).80s [%(id)s].%(ext)s")]
            if jar:
                cmd += ["--cookies", jar]
            if force:
                # re-download even when the file is already present — e.g. to
                # replace an older low-res copy with the HD one
                cmd.append("--force-overwrites")
            cmd.append(link)
            already = []          # files yt-dlp says it already has
            merged = []           # output paths from "[Merger] Merging ... into"
            dests = []            # "[download] Destination:" paths
            try:
                proc = subprocess.Popen(cmd, cwd=dest, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True,
                                        bufsize=1, env=dict(os.environ))
            except Exception as e:
                emit({"type": "log", "text": f"   ❌ could not start yt-dlp: {e}"})
                failed.append(link)
                continue

            last = -25.0
            for line in proc.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue
                if line.startswith("[download]") and "%" in line:
                    try:
                        pct = float(line.split("%")[0].split()[-1])
                    except Exception:
                        continue
                    if pct >= last + 25 or pct >= 100:
                        last = pct
                        emit({"type": "log", "text": f"   {pct:.0f}%"})
                    continue
                if "has already been downloaded" in line:
                    # yt-dlp skips a file it already has. That is SUCCESS, not
                    # failure — the folder-diff alone can't see it, because no new
                    # file appeared. Pull the path out of the message.
                    p = line.split("]", 1)[-1].strip()
                    p = p[: -len("has already been downloaded")].strip()
                    if p:
                        already.append(p)
                        emit({"type": "log", "text":
                              f"   ⏭ already saved: {os.path.basename(p)}"})
                    continue
                if "Destination:" in line:
                    dests.append(line.split("Destination:", 1)[1].strip())
                if "[Merger]" in line and '"' in line:
                    merged.append(line.split('"')[1])
                if ("Destination" in line or "Merger" in line
                        or "ERROR" in line or "Extracting URL" in line):
                    emit({"type": "log", "text": "   " + line[:170]})
            proc.wait()

            new = [f for f in (set(os.listdir(dest)) - before)
                   if not f.startswith(".") and not f.endswith(".part")]
            reused = [p for p in already if os.path.exists(p)]
            # A file OVERWRITTEN in place (force re-download) never appears in the
            # folder diff, and yt-dlp only prints "already been downloaded" when it
            # SKIPS — so trust the paths yt-dlp itself reported and verify on disk.
            overwritten = []
            if merged:
                overwritten.extend(merged)
            elif dests:
                overwritten.append(dests[-1])    # single-format download, no merge
            overwritten = [p for p in overwritten if os.path.exists(p)]
            if proc.returncode == 0 and (new or reused or overwritten):
                for f in new:
                    got.append(os.path.join(dest, f))
                    emit({"type": "log", "text": f"   ✅ {f}"})
                skipped.extend(reused)
                for p in overwritten:
                    b = os.path.basename(p)
                    if b not in new:
                        got.append(p)
                        emit({"type": "log", "text": f"   ✅ {b}  (replaced)"})
                continue
            # yt-dlp couldn't fetch it. On an Indian connection that is the normal
            # outcome for TikTok, so try the no-VPN resolver before giving up.
            if tk:
                emit({"type": "log", "text":
                      "   ↻ TikTok is blocked from this network — trying the no-VPN route…"})
                path, why = tiktok_no_vpn(
                    link, dest, lambda t: emit({"type": "log", "text": t}))
                if path:
                    got.append(path)
                    emit({"type": "log", "text":
                          f"   ✅ {os.path.basename(path)}"
                          "   (no-VPN route · HD where available)"})
                    time.sleep(6)          # the resolver rate-limits if hammered
                    continue
                emit({"type": "log", "text": f"   ↻ that failed too: {why}"})
            failed.append(link)
            if ig:
                emit({"type": "log", "text":
                      "   ❌ failed — if it says 'empty media response' above, the post is "
                      "gone/deleted, or your Instagram login in Zen has expired (open "
                      "instagram.com in Zen and check you are still signed in)."})
            else:
                emit({"type": "log", "text": "   ❌ failed (reason above)"})

        emit({"type": "dl_done", "files": got, "skipped": skipped,
              "failed": failed, "dest": dest})

    # ---- previews ------------------------------------------------------
    def stream_preview(self, q):
        """Cut one throwaway low-res copy of a moment and hand back a URL."""
        def one(k, d=""):
            return (q.get(k, [d]) or [d])[0]

        source = one("source").strip()
        try:
            start, end = float(one("start", "0")), float(one("end", "0"))
        except ValueError:
            return self._send(400, json.dumps({"error": "bad timestamps"}))

        spec = []
        if one("parts"):
            try:
                for p in json.loads(one("parts")):
                    a, b = float(p[0]), float(p[1])
                    if b > a:
                        spec.append((a, b))
            except Exception:
                spec = []
        if not spec:
            spec = [(start, end)]
        if not source or any(b <= a for a, b in spec):
            return self._send(400, json.dumps({"error": "no source / bad range"}))

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(obj):
            try:
                self.wfile.write(("data: %s\n\n" % json.dumps(obj)).encode())
                self.wfile.flush()
                return True
            except Exception as e:
                sys.stderr.write("preview: emit failed: %r\n" % (e,))
                return False

        total = round(sum(b - a for a, b in spec), 1)
        name = pv_key(source, "|".join("%.1f-%.1f" % (a, b) for a, b in spec)) + ".mp4"
        dest = os.path.join(pv_dir("clips"), name)
        try:
            if os.path.isfile(dest) and os.path.getsize(dest) > 10000:
                return emit({"type": "done", "url": "/pv/clips/" + name,
                             "cached": True, "secs": total})
            emit({"type": "log", "text": "finding the video to cut from…"})
            src, note = pv_source(source, lambda t: emit({"type": "log", "text": t}))
            if note:
                emit({"type": "log", "text": note})
            # Build the pieces next to the final file so os.replace stays on the
            # same disk (a cross-volume move of a temp file would fail).
            tmp = tempfile.mkdtemp(prefix=".tmp_", dir=pv_dir("clips"))
            pieces = []
            try:
                for i, (a, b) in enumerate(spec, 1):
                    label = pv_mmss(a) + "–" + pv_mmss(b)
                    emit({"type": "log", "text": "cutting the preview at " + label +
                          ("" if len(spec) == 1 else "  (part %d of %d)" % (i, len(spec)))})
                    out = os.path.join(tmp, "p%d.mp4" % i)
                    pv_cut(src, a, b, out)
                    pieces.append(out)
                if len(pieces) == 1:
                    os.replace(pieces[0], dest)
                else:
                    emit({"type": "log", "text": "joining the two moments…"})
                    pv_concat(pieces, dest)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            if not os.path.isfile(dest) or os.path.getsize(dest) < 10000:
                raise RuntimeError("the preview didn't encode — try again")
            pv_prune("clips", PV_KEEP)
            emit({"type": "done", "url": "/pv/clips/" + name, "cached": False,
                  "secs": total})
        except Exception as e:
            try:
                if os.path.isfile(dest + ".txt"):
                    os.remove(dest + ".txt")
            except OSError:
                pass
            emit({"type": "error", "text": str(e)})

    def serve_media(self, rel):
        """Stream a cached preview. Range support = the player can be scrubbed."""
        rel = urllib.parse.unquote(rel)
        real = os.path.realpath(os.path.join(PV_DIR, rel))
        if (not real.startswith(os.path.realpath(PV_DIR) + os.sep)
                or not os.path.isfile(real)):
            return self._send(404, json.dumps({"error": "no such preview"}))
        return self._stream_file(real, "video/mp4")

    def _stream_file(self, real, ctype, attach=False):
        """Send one file, with Range support. /pv/ and /shared/ both use this."""
        size = os.path.getsize(real)
        start, end, code = 0, max(0, size - 1), 200
        m = re.match(r"bytes=(\d*)-(\d*)\s*$", (self.headers.get("Range") or "").strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:                                   # "bytes=-500" = last 500 bytes
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            code = 206
        n = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if attach:
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''%s"
                             % urllib.parse.quote(os.path.basename(real)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(n))
        self.send_header("Cache-Control", "private, max-age=3600")
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        with open(real, "rb") as f:
            f.seek(start)
            left = n
            while left > 0:
                chunk = f.read(min(262144, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                left -= len(chunk)


def main():
    if not os.path.exists(FINDER):
        print(f"✗ can't find {FINDER}")
        return 1
    # Do NOT pre-create the clips folder here: the user must choose the location,
    # and nothing should be written to disk until a cut is actually requested.
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    srv.daemon_threads = True
    # Loopback comes up first and always: the launcher polls that port.
    # A friend on your Tailscale network gets the tailnet address as a second
    # listener; the rest of the local network still cannot reach the page.
    for extra in tailnet_hosts()[1:]:
        try:
            s2 = ThreadingHTTPServer((extra, PORT), H)
            s2.daemon_threads = True
            threading.Thread(target=s2.serve_forever, daemon=True).start()
            print(f"  also reachable on your Tailscale network →  http://{extra}:{PORT}")
        except OSError as e:
            print(f"  (couldn't also listen on {extra}: {e})")
    print(f"\n  Viral Finder is running →  http://127.0.0.1:{PORT}")
    print("  (leave this window open; close it or press the Quit button to stop)\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    print("  stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
