#!/usr/bin/env python3
"""Stand-in for yt-dlp in the tests. It never touches the network.

It appends its argv to $FAKE_LOG, makes a real media file with ffmpeg at the path
that the -o template gives, writes <name>.info.json next to it (video.info.json for
video.mp4), and prints the path, like `yt-dlp --print after_move:filepath`. With
--cookies FILE it also logs "cookies <content of FILE>".

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
  error        exit 1 with an error that is not about a login
  skip         exit 0 with no output (a video that --match-filters rejects)
  hang         log "pid <own pid> <child pid>", start a child that sleeps, sleep
  grow         log "pid <own pid>", then grow media.<ext>.part without end (a fragmented
               download whose size yt-dlp does not know)
  audioshort   with "-f ba...", an audio-only M4A 1 second long (the info JSON says the
               full length); any other format is complete
  music        "-f ba/b" (no filter) picks the "audio" format, which is TikTok's music
               track: an audio-only M4A 1.5 times the video length. "ba[format_id!=audio]"
               finds no other audio-only format and falls back to "b", the full video.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

argv = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(argv) + "\n")
    if "--cookies" in argv:
        cookies = Path(argv[argv.index("--cookies") + 1])
        f.write(f"cookies-mode {cookies.stat().st_mode & 0o777:o}\n")
        f.write("cookies " + cookies.read_text().strip() + "\n")

query = {k: v[0] for k, v in parse_qs(urlparse(argv[-1]).query).items()}
mode = query.get("mode", os.environ.get("FAKE_MODE", "ok"))
if mode == "fail":
    print("ERROR: login required", file=sys.stderr)
    sys.exit(1)
if mode == "error":
    print("ERROR: Unsupported URL: /some/local/path", file=sys.stderr)
    sys.exit(1)
if mode == "skip":
    sys.exit(0)
if mode == "hang":
    child = subprocess.Popen(["sleep", "600"])
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(f"pid {os.getpid()} {child.pid}\n")
    time.sleep(600)

template = argv[argv.index("-o") + 1]
if mode == "grow":
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(f"pid {os.getpid()}\n")
    part = Path(template.replace("%(ext)s", "mp4") + ".part")
    with open(part, "wb") as f:
        for _ in range(1200):
            f.write(b"x" * 65536)
            f.flush()
            time.sleep(0.05)
    sys.exit(0)
fmt = argv[argv.index("-f") + 1] if "-f" in argv else None
audio_only = mode == "audioshort" and (fmt or "").startswith("ba")
# The only audio-only format is "audio" (TikTok's music track): the first selector gets
# it unless it carries the filter that excludes it, and then yt-dlp goes on to "b".
first = (fmt or "").split("/")[0]
music = (mode == "music" and first.startswith("ba")
         and "[format_id!=audio]" not in first)
ext = "m4a" if audio_only or music else query.get("ext", "mp4")
duration = float(query.get("dur", os.environ.get("FAKE_DURATION", "4")))
path = Path(template.replace("%(uploader)s", "tester")
            .replace("%(id)s", query.get("id", "vid1")).replace("%(ext)s", ext))
if path.exists():  # yt-dlp skips a file that is already there
    print(path)
    sys.exit(0)
path.parent.mkdir(parents=True, exist_ok=True)
(path.parent / (path.stem + ".info.json")).write_text(json.dumps({
    "id": "vid1", "uploader": "tester", "title": "Test video", "duration": duration,
    "upload_date": "20261115", "webpage_url": argv[-1], "description": "A test.", "formats": [
    {"format_id": "audio", "acodec": "aac", "vcodec": "none"},
    {"format_id": "h264-0", "acodec": "aac", "vcodec": "h264"},
    {"format_id": "h264-1", "acodec": "aac", "vcodec": "h264"},
    {"format_id": "h265", "acodec": "none", "vcodec": "h265"}]}))

if mode == "corrupt" or (mode == "mirrorbad" and fmt == "h264-1"):
    path.write_bytes(b"this is not a video\n" * 100)
    print(path)
    sys.exit(0)

if audio_only or music:
    length = duration * 1.5 if music else 1
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={length}", "-c:a", "aac", str(path)],
                   check=True)
    print(path)
    sys.exit(0)

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
