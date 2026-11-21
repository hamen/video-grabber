"""Shared logic for the grab CLI and the grab MCP server (grab_mcp.py).

Standard library only, Python 3.8+. Functions return values or raise GrabError; none of
them calls sys.exit, so a long-running server can import them.

Every function that runs a child process takes an optional Job. Without one, it behaves
exactly as the CLI always did: no time limit, the child's stderr goes to the terminal.
With one, each child runs in its own process group with no stdin and no stderr, and the
Job can kill the whole group (uvx and yt-dlp, ffmpeg, the STT client) on a deadline or a
cancel.
"""
import json
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

# Audio shorter than this share of the video means a broken audio track. TikTok serves
# H.265 video-only formats plus a separate "audio" format that can be a 2-second stub,
# and yt-dlp merges the two by default.
MIN_AUDIO_RATIO = 0.9

MAX_FALLBACK_FORMATS = 4


class GrabError(RuntimeError):
    """A failure with a short message that is safe to show to the user."""


class Stopped(GrabError):
    """The Job passed its deadline or was cancelled; its children are dead."""


def ytdlp_cmd():
    env = os.environ.get("GRAB_YTDLP")
    if env:
        return shlex.split(env)
    # Sites like TikTok and Instagram break old extractors often: prefer the latest release.
    if shutil.which("uvx"):
        return ["uvx", "--quiet", "--from", "yt-dlp[default]@latest", "yt-dlp"]
    if shutil.which("yt-dlp"):
        return ["yt-dlp"]
    raise GrabError("neither uvx nor yt-dlp is on PATH")


def stt_cmd():
    return shlex.split(os.environ.get("GRAB_STT_CMD", "stt-file"))


class Job:
    """Deadline and cancel switch for one MCP call."""

    def __init__(self, timeout):
        self.deadline = time.monotonic() + timeout
        self._cancelled = threading.Event()
        self._procs = set()
        self._lock = threading.Lock()

    def timed_out(self):
        return time.monotonic() >= self.deadline

    def stopped(self):
        return self._cancelled.is_set() or self.timed_out()

    def cancel(self):
        self._cancelled.set()
        self.kill_all()

    def check(self):
        """Raise Stopped if the job passed its deadline or was cancelled."""
        if self.stopped():
            raise Stopped("timed out" if self.timed_out() else "cancelled")

    @staticmethod
    def _kill(proc):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def kill_all(self):
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            self._kill(proc)

    def run(self, cmd, check=False, capture_stderr=False, watch=None):
        """Run cmd to the end, or kill its process group when the job stops.

        watch, if given, is called about every 0.2 s while cmd runs; an exception from it
        kills the process group and propagates.
        Returns a CompletedProcess with bytes stdout (and stderr if captured)."""
        self.check()
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if capture_stderr else subprocess.DEVNULL,
            start_new_session=True)
        with self._lock:
            self._procs.add(proc)
        try:
            while True:
                try:
                    out, err = proc.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    if self.stopped():
                        self._kill(proc)
                        proc.communicate()
                        self.check()
                    if watch is not None:
                        watch()
        finally:
            # The direct child can exit while a grandchild still runs: kill the group.
            self._kill(proc)
            if proc.poll() is None:
                proc.wait()
            for pipe in (proc.stdout, proc.stderr):  # still open if watch() raised
                if pipe is not None:
                    pipe.close()
            with self._lock:
                self._procs.discard(proc)
        # A cancel can kill the child just before communicate() returns: its exit status
        # then says nothing about the command.
        self.check()
        if check and proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd)
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def seconds(value):
    """A positive duration from ffprobe, or None for missing, "N/A" or zero."""
    try:
        d = float(value)
    except (TypeError, ValueError):
        return None
    return d if d > 0 else None


def time_cap(max_seconds):
    """ffmpeg output options that stop decoding after max_seconds (none if None)."""
    return [] if max_seconds is None else ["-t", str(max_seconds)]


def decoded_audio_seconds(path, job=None, max_seconds=None):
    """Length of the first audio track, measured by decoding it.

    Stream durations in the container cannot be trusted: WebM often has none, and
    the container duration covers the video too, so a short track would look fine.
    Decoding 11 minutes of audio takes about a second. With max_seconds, decoding
    stops there, so a file that lies about its length cannot fill the memory."""
    rate = 8000
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), *time_cap(max_seconds),
           "-map", "0:a:0", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"]
    if job:
        return len(job.run(cmd).stdout) / (2 * rate)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    total = 0
    while True:
        chunk = proc.stdout.read(1 << 16)
        if not chunk:
            break
        total += len(chunk)
    proc.wait()
    return total / (2 * rate)


def audio_status(path, job=None, max_seconds=None):
    """"ok", "short" (under MIN_AUDIO_RATIO of the video) or "none" (no audio stream)."""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration",
           "-show_entries", "format=duration", "-of", "json", str(path)]
    if job:
        out = job.run(cmd, check=True).stdout.decode("utf-8", "replace")
    else:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    streams = data.get("streams", [])
    if not any(s.get("codec_type") == "audio" for s in streams):
        return "none"
    video = next((seconds(s.get("duration")) for s in streams
                  if s.get("codec_type") == "video"), None)
    if video is None and any(s.get("codec_type") == "video" for s in streams):
        video = seconds(data.get("format", {}).get("duration"))
    if video is None:  # audio-only download, or no length to compare with
        return "ok"
    return ("ok" if decoded_audio_seconds(path, job, max_seconds) >= MIN_AUDIO_RATIO * video
            else "short")


def transcribe(media, chunk_seconds, job=None, tmp_root=None, max_seconds=None):
    """Cut the audio into chunk_seconds blocks and run the STT command on each one.

    Many local models have a maximum input length (Nemotron streaming stops near 6.5
    minutes), so long videos must go in blocks. A cut can split a word in two.
    The blocks go to a temporary directory under tmp_root (default: the system temp).
    With max_seconds, only that much audio is cut, so the blocks cannot fill the disk."""
    with tempfile.TemporaryDirectory(prefix="grab-", dir=tmp_root) as tmp:
        cut = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(media),
               *time_cap(max_seconds), "-vn",
               "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "segment",
               "-segment_time", str(chunk_seconds), f"{tmp}/chunk%04d.wav"]
        if job:
            job.run(cut, check=True)
        else:
            subprocess.run(cut, check=True)
        parts = []
        for chunk in sorted(Path(tmp).glob("chunk*.wav")):
            if job:
                res = job.run(stt_cmd() + [str(chunk)])
                stdout = res.stdout.decode("utf-8", "replace")
            else:
                res = subprocess.run(stt_cmd() + [str(chunk)], stdout=subprocess.PIPE, text=True)
                stdout = res.stdout
            if res.returncode != 0:
                raise GrabError(f"STT command failed on {chunk.name} (exit {res.returncode})")
            text = stdout.strip()
            if text:
                parts.append(text)
        return "\n".join(parts)


def combined_formats(info_path, limit=MAX_FALLBACK_FORMATS):
    """Format ids that list both audio and video, best first, from yt-dlp's info JSON.

    "-f b" is one of them, but a site can serve a silent or cut copy of it from one
    mirror (TikTok did, for an 11-minute video): then the other copies are worth a try."""
    try:
        formats = json.loads(Path(info_path).read_text()).get("formats") or []
    except (OSError, ValueError, AttributeError):
        return []
    if not isinstance(formats, list):
        return []
    has = lambda f, k: f.get(k) not in (None, "", "none")
    # yt-dlp lists formats worst first. An id with / or + would read as a selector.
    ids = [f["format_id"] for f in reversed(formats)
           if isinstance(f, dict) and isinstance(f.get("format_id"), str)
           and not set(f["format_id"]) & set("/+") and has(f, "acodec") and has(f, "vcodec")]
    return list(dict.fromkeys(ids))[:limit]
