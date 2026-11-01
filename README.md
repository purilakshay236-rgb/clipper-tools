# clipper-tools

Local video downloader / "viral moment" finder with a small web UI.

- `viral_server.py` – local web server (default http://127.0.0.1:8765)
- `find_viral.py` – CLI to find the most-watched moments of a video
- `smart_clips.py` – transcript/LLM-based clip picking
- `viral_on.sh` / `viral_off.sh` – start/stop the server

## Requirements
- Python 3
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) and ffmpeg on your PATH

## Run
```
python3 viral_server.py
```
Then open http://127.0.0.1:8765.
