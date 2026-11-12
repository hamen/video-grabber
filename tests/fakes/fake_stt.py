#!/usr/bin/env python3
"""Stand-in for the STT command in the tests.

It appends the WAV path to $FAKE_LOG and prints "block <seconds>" with the rounded
length of the WAV it got, so a test can see how the audio was cut.

FAKE_STT: ok (default), empty (prints nothing), fail (exit 3).
"""
import os
import subprocess
import sys

wav = sys.argv[1]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(f"stt {wav}\n")

mode = os.environ.get("FAKE_STT", "ok")
if mode == "fail":
    sys.exit(3)
if mode == "empty":
    sys.exit(0)
seconds = float(subprocess.run(
    ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", wav],
    capture_output=True, text=True, check=True).stdout)
print(f"block {round(seconds)}")
