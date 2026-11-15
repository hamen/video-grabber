#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp==2.3.0"]
# ///
"""grab_mcp: the grab tool as a stdio MCP server.

One tool, grab_transcript(url): download the audio of one TikTok, Instagram, YouTube or
X video with yt-dlp, transcribe it with the local STT command, and return the metadata
and the transcript. No video file is kept.

Run: uv run grab_mcp.py   (stdio; put an HTTP proxy such as mcp-proxy in front of it)

Environment:
  GRAB_TMP          parent of the per-call job directories (default: <system temp>/grab-mcp)
  GRAB_COOKIE_FILE  optional Netscape cookies.txt; each call uses its own copy
  GRAB_STT_CMD      speech-to-text command (default: stt-file)
  GRAB_YTDLP        yt-dlp command (default: latest release through uvx)
  GRAB_TIMEOUT      seconds per call (default: 600)
  GRAB_BUSY_WAIT    seconds a call waits for the one running before it says "busy" (default: 60)
"""
import ipaddress
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlsplit

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

sys.path.insert(0, str(Path(os.path.realpath(__file__)).parent))
import grab_core  # noqa: E402
from grab_core import GrabError, Stopped  # noqa: E402

ALLOWED_DOMAINS = ("tiktok.com", "instagram.com", "instagr.am", "youtube.com", "youtu.be",
                   "x.com", "twitter.com")
# yt-dlp extractor names (full-match regexes) for those sites. The generic extractor is
# off, so a redirect to any other site fails instead of downloading.
EXTRACTORS = r"tiktok,vm\.tiktok,instagram,youtube,youtubeytbe,twitter"
MAX_DURATION = 1200
MATCH_FILTER = f"duration & duration <= {MAX_DURATION} & !is_live"
MAX_FILESIZE = "300M"
MAX_URL_LENGTH = 2048
MAX_JOB_BYTES = 1 << 30
MAX_FALLBACKS = 3
CHUNK_SECONDS = 300

URL_NOT_ALLOWED = "URL not allowed: give an https link to a TikTok, Instagram, YouTube or X video"
NEEDS_LOGIN = "video needs a login"
TOO_LONG = "too long: the video is over 20 minutes, live, of unknown length, or over 300 MB"
DOWNLOAD_FAILED = "download failed"
NO_AUDIO = "no usable audio"
TRANSCRIBE_FAILED = "transcription failed"
EMPTY = "transcript is empty: the video has no speech, or the STT heard none"
BUSY = "busy: another video is in progress, try again in a minute"
TIMED_OUT = "timed out"
INTERNAL = "internal error"

LOGIN_HINT = re.compile(r"log ?in|sign in|cookies|private|age.restricted|confirm your age",
                        re.IGNORECASE)


def log(msg):
    print(f"grab_mcp: {msg}", file=sys.stderr, flush=True)


def check_url(url):
    """Return the URL if it may be downloaded, else raise GrabError(URL_NOT_ALLOWED).

    https only; the host is one of ALLOWED_DOMAINS or a subdomain of one; no user info,
    no port, no IP literal; at most MAX_URL_LENGTH characters."""
    if (not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH
            or url.startswith("-") or any(c.isspace() or ord(c) < 32 or c == "\\" for c in url)):
        raise GrabError(URL_NOT_ALLOWED)
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        host = host.encode("idna").decode("ascii").lower()
    except (ValueError, UnicodeError):
        raise GrabError(URL_NOT_ALLOWED) from None
    if parts.scheme != "https" or "@" in parts.netloc or ":" in parts.netloc or not host:
        raise GrabError(URL_NOT_ALLOWED)
    try:
        ipaddress.ip_address(host)
        raise GrabError(URL_NOT_ALLOWED)
    except ValueError:
        pass
    if not any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS):
        raise GrabError(URL_NOT_ALLOWED)
    return url


def job_bytes(jobdir):
    return sum(p.stat().st_size for p in Path(jobdir).rglob("*") if p.is_file())


def check_space(jobdir):
    if job_bytes(jobdir) > MAX_JOB_BYTES:
        raise GrabError(TOO_LONG)


def copy_cookies(jobdir):
    """A private copy of $GRAB_COOKIE_FILE in the job dir (yt-dlp rewrites the file it
    uses), or None when there is no cookie file."""
    src = os.environ.get("GRAB_COOKIE_FILE")
    if not src:
        return None
    src = os.path.expanduser(src)
    dst = Path(jobdir) / "cookies.txt"
    try:
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with open(src, "rb") as f, os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(f, out)
    except OSError:
        log("cookie file not readable: going on without cookies")
        dst.unlink(missing_ok=True)
        return None
    return dst


def ytdlp_argv(url, jobdir, fmt, cookies):
    cmd = grab_core.ytdlp_cmd() + [
        "--ignore-config", "--quiet", "--no-warnings", "--no-progress", "--no-simulate",
        "--no-playlist", "--use-extractors", EXTRACTORS, "--match-filters", MATCH_FILTER,
        "--max-filesize", MAX_FILESIZE, "-f", fmt, "--write-info-json",
        "-o", f"{jobdir}/media.%(ext)s", "--print", "after_move:filepath",
    ]
    if cookies:
        cmd += ["--cookies", str(cookies)]
    return cmd + ["--", url]


def download(url, jobdir, job, fmt, cookies):
    """Path of the downloaded file inside jobdir, or GrabError with a short message."""
    check_space(jobdir)
    res = job.run(ytdlp_argv(url, jobdir, fmt, cookies), capture_stderr=True)
    if res.returncode != 0:
        err = res.stderr.decode("utf-8", "replace")
        raise GrabError(NEEDS_LOGIN if LOGIN_HINT.search(err) else DOWNLOAD_FAILED)
    lines = [l for l in res.stdout.decode("utf-8", "replace").splitlines() if l.strip()]
    if not lines:  # yt-dlp skips a video that fails --match-filters or --max-filesize
        raise GrabError(TOO_LONG)
    media = Path(lines[-1]).resolve()
    if media.parent != Path(jobdir).resolve() or not media.is_file():
        raise GrabError(DOWNLOAD_FAILED)
    return media


def media_with_audio(url, jobdir, job, cookies):
    """Download the audio first; if it is missing or cut, try formats with their own audio."""
    media = download(url, jobdir, job, "ba/b", cookies)
    try:
        if grab_core.audio_status(media, job) == "ok":
            return media
    except (OSError, ValueError, grab_core.subprocess.CalledProcessError):
        pass
    info = Path(jobdir) / "media.info.json"
    fallbacks = list(dict.fromkeys(["b"] + grab_core.combined_formats(info)))[:MAX_FALLBACKS]
    for fmt in fallbacks:
        media.unlink(missing_ok=True)  # yt-dlp skips a file that is already there
        try:
            media = download(url, jobdir, job, fmt, cookies)
            if grab_core.audio_status(media, job) == "ok":
                return media
        except Stopped:
            raise
        except (GrabError, OSError, ValueError, grab_core.subprocess.CalledProcessError):
            continue
    raise GrabError(NO_AUDIO)


class Transcript(TypedDict):
    title: str | None
    uploader: str | None
    duration_s: float | None
    upload_date: str | None
    webpage_url: str | None
    description: str | None
    transcript: str


def metadata(jobdir):
    try:
        info = json.loads((Path(jobdir) / "media.info.json").read_text())
    except (OSError, ValueError):
        info = {}
    if not isinstance(info, dict):
        info = {}
    text = lambda k: info.get(k) if isinstance(info.get(k), str) else None
    duration = info.get("duration")
    return {
        "title": text("title"),
        "uploader": text("uploader") or text("channel") or text("uploader_id"),
        "duration_s": float(duration) if isinstance(duration, (int, float)) else None,
        "upload_date": text("upload_date"),
        "webpage_url": text("webpage_url"),
        "description": text("description"),
    }


def run_job(url, job, root):
    """The whole call, blocking: download, audio check, transcription, cleanup."""
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    jobdir = Path(tempfile.mkdtemp(prefix="job-", dir=root))  # mode 0700
    try:
        cookies = copy_cookies(jobdir)
        media = media_with_audio(url, jobdir, job, cookies)
        check_space(jobdir)
        try:
            text = grab_core.transcribe(media, CHUNK_SECONDS, job, tmp_root=jobdir)
        except Stopped:
            raise
        except (GrabError, OSError, grab_core.subprocess.CalledProcessError):
            raise GrabError(TRANSCRIBE_FAILED) from None
        if not text.strip():
            raise GrabError(EMPTY)
        return {**metadata(jobdir), "transcript": text}
    finally:
        job.kill_all()
        shutil.rmtree(jobdir, ignore_errors=True)


TIMEOUT = float(os.environ.get("GRAB_TIMEOUT", "600"))
BUSY_WAIT = float(os.environ.get("GRAB_BUSY_WAIT", "60"))
ROOT = os.environ.get("GRAB_TMP") or os.path.join(tempfile.gettempdir(), "grab-mcp")

mcp = MCPServer("grab")
_lock = anyio.Lock()


@mcp.tool()
async def grab_transcript(url: str) -> Transcript:
    """Download one TikTok, Instagram, YouTube or X video (https link, at most 20 minutes)
    and return its title, uploader, duration, upload date, description and transcript.
    The transcript comes from local speech-to-text, so names and rare words can be wrong.
    One video at a time; a call can take a few minutes."""
    try:
        url = check_url(url)
    except GrabError as e:
        raise ToolError(str(e)) from None
    try:
        with anyio.fail_after(BUSY_WAIT):
            await _lock.acquire()
    except TimeoutError:
        raise ToolError(BUSY) from None
    try:
        job = grab_core.Job(TIMEOUT)
        done = threading.Event()
        started = time.monotonic()

        def work():
            try:
                return run_job(url, job, ROOT)
            finally:
                done.set()

        try:
            result = await anyio.to_thread.run_sync(work, abandon_on_cancel=True)
        except anyio.get_cancelled_exc_class():
            # The client cancelled: kill the children and wait for the cleanup to finish,
            # so the lock is released only after the job dir is gone.
            job.cancel()
            with anyio.CancelScope(shield=True):
                while not done.is_set():
                    await anyio.sleep(0.05)
            log("call cancelled")
            raise
        except Stopped:
            log(f"call timed out after {time.monotonic() - started:.0f} s")
            raise ToolError(TIMED_OUT) from None
        except GrabError as e:
            log(f"call failed: {e}")
            raise ToolError(str(e)) from None
        except Exception as e:  # never forward a message that can hold a path
            log(f"call failed: unexpected {type(e).__name__}")
            raise ToolError(INTERNAL) from None
        log(f"call done in {time.monotonic() - started:.0f} s")
        return result
    finally:
        _lock.release()


if __name__ == "__main__":
    mcp.run()
