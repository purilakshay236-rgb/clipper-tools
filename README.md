# clipper-tools

Local video downloader / "viral moment" finder with a small web UI.

- `viral_server.py` – local web server (default http://127.0.0.1:8765)
- `find_viral.py` – CLI to find the most-watched moments of a video
- `smart_clips.py` – transcript/LLM-based clip picking
- `viral_on.sh` / `viral_off.sh` – start/stop the server

## Requirements
- Python 3
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) and ffmpeg on your PATH
  (`brew install yt-dlp ffmpeg` on macOS)

## Run
```
python3 viral_server.py
```
Then open http://127.0.0.1:8765.

## Configuration (optional)
All output locations have portable defaults and work on a fresh clone with
zero edits. Override any of them with environment variables if you like:

| Env var | What it controls | Default |
|---|---|---|
| `CLIPPER_OUTPUT` | where finished clips are written | `~/Viral Clips` |
| `CLIPPER_DOWNLOAD` | where raw downloads are saved | `~/Downloads` |
| `CLIPPER_BROWSE` | top folder the UI lets you browse | home folder |
| `CLIPPER_MODELS` | Whisper model cache | `~/Library/Caches/clipper-tools/whisper_models` |
| `CLIPPER_YTDLP` | path to a specific yt-dlp binary | bundled `./bin/yt-dlp`, else PATH |

See `config.py` for the full list.
