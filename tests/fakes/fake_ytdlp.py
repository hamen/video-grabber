#!/usr/bin/env python3
"""Stand-in for yt-dlp in the tests. It never touches the network.

It appends its argv to $FAKE_LOG, makes a real media file with ffmpeg at the path
that the -o template gives, writes video.info.json next to it, and prints the path,
like `yt-dlp --print after_move:filepath`.

FAKE_MODE:
  ok         video and audio of FAKE_DURATION seconds (default 4)
  truncated  audio only 1 second long, unless "-f b" is passed (the TikTok case)
  fail       exit 1 with no output (a site that refuses the download)
"""
import json
import os
import subprocess
import sys
from pathlib import Path

argv = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(argv) + "\n")

mode = os.environ.get("FAKE_MODE", "ok")
if mode == "fail":
    print("ERROR: login required", file=sys.stderr)
    sys.exit(1)

template = argv[argv.index("-o") + 1]
fmt = argv[argv.index("-f") + 1] if "-f" in argv else None
path = Path(template.replace("%(uploader)s", "tester").replace("%(id)s", "vid1")
            .replace("%(ext)s", "mp4"))
if path.exists():  # yt-dlp skips a file that is already there
    print(path)
    sys.exit(0)
path.parent.mkdir(parents=True, exist_ok=True)

duration = float(os.environ.get("FAKE_DURATION", "4"))
audio = 1.0 if mode == "truncated" and fmt != "b" else duration
subprocess.run(
    ["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
     "-f", "lavfi", "-i", f"color=c=black:s=64x64:d={duration}",
     "-f", "lavfi", "-i", f"sine=frequency=440:duration={audio}",
     "-c:v", "libx264", "-c:a", "aac", str(path)],
    check=True)
(path.parent / "video.info.json").write_text(json.dumps({"id": "vid1", "uploader": "tester"}))
print(path)
