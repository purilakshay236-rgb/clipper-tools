"""
Portable path configuration for clipper-tools.

All locations can be overridden with environment variables, so the same code
runs unchanged on any Mac:

    CLIPPER_OUTPUT    where finished clips are written   (default: ~/Viral Clips)
    CLIPPER_DOWNLOAD  where raw downloads are saved      (default: ~/Downloads)
    CLIPPER_BROWSE    top folder the UI lets you browse  (default: home folder)
    CLIPPER_MODELS    where Whisper models are cached    (default: ~/Library/Caches/clipper-tools/whisper_models)
    CLIPPER_YTDLP     full path to a yt-dlp binary       (default: bundled ./bin/yt-dlp, else PATH)
    CLIPPER_RULES     caption rules file (test scripts)  (default: legacy SSD location)
    CLIPPER_DATA      transcripts/captions folders       (default: legacy SSD location)

Backwards compatibility: if this machine still has the original hard-coded
"SanDiskSSD" locations, they are used automatically. On any other machine
those paths simply don't exist and the portable defaults above take over,
so nothing here needs editing after a fresh clone.
"""

import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")

# Legacy locations from the very first version of this project. They only
# kick in when they actually exist on the machine running the code.
_LEGACY_ROOT = "/Volumes/SanDiskSSD"
_LEGACY_OUT = os.path.join(_LEGACY_ROOT, "Viral Clips")
_LEGACY_DL = os.path.join(_LEGACY_ROOT, "Downloads")
_LEGACY_MODELS = os.path.join(_LEGACY_ROOT, "whisper_models")


def _pick(env_var, legacy, fallback):
    """env var > legacy path (only if it exists) > portable default."""
    v = os.environ.get(env_var)
    if v:
        return v
    if legacy and os.path.isdir(legacy):
        return legacy
    return fallback


# --- folders ---------------------------------------------------------------
OUTPUT_DIR = _pick("CLIPPER_OUTPUT", _LEGACY_OUT, os.path.join(HOME, "Viral Clips"))
DOWNLOADS_DIR = _pick("CLIPPER_DOWNLOAD", _LEGACY_DL, os.path.join(HOME, "Downloads"))
BROWSE_ROOT = _pick("CLIPPER_BROWSE", _LEGACY_ROOT, HOME)

# --- whisper model cache ---------------------------------------------------
if sys.platform == "darwin":
    _CACHE_ROOT = os.path.join(HOME, "Library", "Caches", "clipper-tools")
else:
    _CACHE_ROOT = os.path.join(HOME, ".cache", "clipper-tools")
MODELS_DIR = _pick("CLIPPER_MODELS", _LEGACY_MODELS, os.path.join(_CACHE_ROOT, "whisper_models"))

# --- external binaries -----------------------------------------------------
def _first_existing(*paths):
    for p in paths:
        if p and os.path.isfile(p):
            return p
    return None


BUNDLED_YTDLP = os.path.join(HERE, "bin", "yt-dlp")
YTDLP_BIN = _first_existing(
    os.environ.get("CLIPPER_YTDLP"), BUNDLED_YTDLP, shutil.which("yt-dlp")
) or "yt-dlp"

FFMPEG_BIN = _first_existing(
    shutil.which("ffmpeg"), "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"
) or "ffmpeg"
FFPROBE_BIN = _first_existing(
    shutil.which("ffprobe"), "/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe"
) or "ffprobe"
OLLAMA_BIN = _first_existing(
    shutil.which("ollama"), "/usr/local/bin/ollama", "/opt/homebrew/bin/ollama"
) or "ollama"

# --- personal data used only by the dev/test scripts ------------------------
RULES_FILE = _pick("CLIPPER_RULES", os.path.join(_LEGACY_ROOT, "caption_rules.txt"),
                   os.path.join(HERE, "caption_rules.txt"))
DATA_DIR = _pick("CLIPPER_DATA", _LEGACY_ROOT, HERE)
