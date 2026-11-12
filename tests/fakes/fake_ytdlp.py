#!/usr/bin/env python3
"""Stand-in for yt-dlp in the tests. It never touches the network.

It appends its argv to $FAKE_LOG, makes a real media file with ffmpeg at the path
that the -o template gives, writes video.info.json next to it, and prints the path,
like `yt-dlp --print after_move:filepath`.

The URL query sets the behavior, so one batch can mix cases:
  id=NAME      video id, so the folder is tester_NAME (default vid1)
  mode=MODE    see below; $FAKE_MODE is the default
  ext=webm     VP9 + Opus in WebM (no per-stream durations) instead of H.264 + AAC in MP4
  dur=SECONDS  length (default $FAKE_DURATION, else 4)

MODE:
  ok           video and audio of the same length
  truncated    audio 1 second long, unless "-f b" is passed (the TikTok case)
  alwaysshort  audio 1 second long, also with "-f b"
  silent       video with no audio stream
  mirror       audio 1 second long for every format except "-f h264-0" (a bad mirror of "b")
  mirrorsilent no audio stream for every format except "-f h264-0"
  mirrorbad    like mirror, but "-f h264-1" is not media at all
  corrupt      a file that is not media at all
  fail         exit 1 with no output (a site that refuses the download)
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

argv = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(argv) + "\n")

query = {k: v[0] for k, v in parse_qs(urlparse(argv[-1]).query).items()}
mode = query.get("mode", os.environ.get("FAKE_MODE", "ok"))
if mode == "fail":
    print("ERROR: login required", file=sys.stderr)
    sys.exit(1)

ext = query.get("ext", "mp4")
template = argv[argv.index("-o") + 1]
fmt = argv[argv.index("-f") + 1] if "-f" in argv else None
path = Path(template.replace("%(uploader)s", "tester")
            .replace("%(id)s", query.get("id", "vid1")).replace("%(ext)s", ext))
if path.exists():  # yt-dlp skips a file that is already there
    print(path)
    sys.exit(0)
path.parent.mkdir(parents=True, exist_ok=True)
(path.parent / "video.info.json").write_text(json.dumps({"id": "vid1", "uploader": "tester", "formats": [
    {"format_id": "audio", "acodec": "aac", "vcodec": "none"},
    {"format_id": "h264-0", "acodec": "aac", "vcodec": "h264"},
    {"format_id": "h264-1", "acodec": "aac", "vcodec": "h264"},
    {"format_id": "h265", "acodec": "none", "vcodec": "h265"}]}))

if mode == "corrupt" or (mode == "mirrorbad" and fmt == "h264-1"):
    path.write_bytes(b"this is not a video\n" * 100)
    print(path)
    sys.exit(0)

duration = float(query.get("dur", os.environ.get("FAKE_DURATION", "4")))
short = (mode == "alwaysshort" or (mode == "truncated" and fmt != "b")
         or (mode in ("mirror", "mirrorbad") and fmt != "h264-0"))
audio = 1.0 if short else duration
cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
       "-f", "lavfi", "-i", f"color=c=black:s=64x64:d={duration}"]
if mode != "silent" and not (mode == "mirrorsilent" and fmt != "h264-0"):
    cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={audio}"]
if ext == "webm":
    cmd += ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-c:a", "libopus"]
else:
    cmd += ["-c:v", "libx264", "-c:a", "aac"]
subprocess.run(cmd + [str(path)], check=True)
print(path)
