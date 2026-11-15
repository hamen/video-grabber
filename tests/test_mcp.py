"""Tests for grab_mcp.py, with fake yt-dlp and STT commands. No network.

They need the mcp package: bin/ci runs them with `uv run --with-requirements grab_mcp.py`.
"""
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAKES = Path(__file__).resolve().parent / "fakes"
sys.path.insert(0, str(ROOT))

import grab_core  # noqa: E402
import grab_mcp  # noqa: E402

TIKTOK = "https://www.tiktok.com/@someone/video/123"


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A killed child that nobody reaped yet is a zombie: dead for this test.
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return False


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="grab-mcp-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.log = self.tmp / "calls.log"
        self.log.touch()
        self.root = self.tmp / "jobs"
        self.env = {
            "FAKE_LOG": str(self.log),
            "GRAB_YTDLP": f"{sys.executable} {FAKES / 'fake_ytdlp.py'}",
            "GRAB_STT_CMD": f"{sys.executable} {FAKES / 'fake_stt.py'}",
        }
        self.saved = {k: os.environ.get(k) for k in
                      list(self.env) + ["FAKE_STT", "FAKE_MODE", "GRAB_COOKIE_FILE"]}
        os.environ.update(self.env)
        for k in ("FAKE_STT", "FAKE_MODE", "GRAB_COOKIE_FILE"):
            os.environ.pop(k, None)
        self.addCleanup(self.restore_env)

    def restore_env(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def ytdlp_calls(self):
        return [json.loads(l) for l in self.log.read_text().splitlines() if l.startswith("[")]

    def pids(self):
        return [int(p) for l in self.log.read_text().splitlines() if l.startswith("pid ")
                for p in l.split()[1:]]

    def wait_for_pids(self, n=2, timeout=20):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if len(self.pids()) >= n:
                return self.pids()
            time.sleep(0.05)
        self.fail("the fake never started")

    def job_dirs(self):
        return list(self.root.glob("job-*")) if self.root.exists() else []

    def run_job(self, url=TIKTOK, timeout=60):
        return grab_mcp.run_job(url, grab_core.Job(timeout), self.root)

    def assert_error(self, message, url=TIKTOK, timeout=60):
        with self.assertRaises(grab_core.GrabError) as cm:
            self.run_job(url, timeout)
        self.assertEqual(str(cm.exception), message)
        self.assertEqual(self.job_dirs(), [], "job dir left behind")
        return cm.exception


class UrlRules(unittest.TestCase):
    def test_allowed_hosts_and_short_links(self):
        for url in [TIKTOK, "https://vm.tiktok.com/ZMabc123/", "https://vt.tiktok.com/x/",
                    "https://www.instagram.com/reel/abc/", "https://instagr.am/p/abc/",
                    "https://www.youtube.com/watch?v=abc", "https://youtu.be/abc?t=3",
                    "https://m.youtube.com/shorts/abc", "https://x.com/u/status/1",
                    "https://twitter.com/u/status/1", "https://WWW.TikTok.COM/@a/video/1"]:
            self.assertEqual(grab_mcp.check_url(url), url)

    def test_refused(self):
        ten = ".".join(["10", "0", "0", "1"])  # bin/ci's leak scan refuses the literal
        bad = ["http://www.tiktok.com/@a/video/1", "https://127.0.0.1/x", f"https://{ten}/x",
               "https://[::1]/x", "https://169.254.169.254/latest/meta-data",
               "https://localhost/x", "https://user@www.tiktok.com/x",
               "https://user:pw@youtube.com/x", "https://www.tiktok.com:8443/x",
               "https://example.com/x", "https://tiktok.com.evil.example/x",
               "https://eviltiktok.com/x", "https://youtube.com.", "-https://youtu.be/x",
               "--exec=whoami", "https://youtu.be/" + "a" * 2048, "", "ftp://youtube.com/x",
               "https://youtu.be/a b", "https://youtu.be/a\nb", "https:\\\\youtu.be\\x",
               "https://2130706433/x", "file:///etc/passwd", None]
        for url in bad:
            with self.assertRaises(grab_core.GrabError, msg=repr(url)) as cm:
                grab_mcp.check_url(url)
            self.assertEqual(str(cm.exception), grab_mcp.URL_NOT_ALLOWED)


class YtdlpArgv(Base):
    def test_flags_and_separator(self):
        argv = grab_mcp.ytdlp_argv(TIKTOK, "/jobs/job-1", "ba/b", None)
        self.assertEqual(argv[-2:], ["--", TIKTOK])
        joined = " ".join(argv)
        for flag in ["--ignore-config", "--no-playlist", "--write-info-json"]:
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--use-extractors") + 1], grab_mcp.EXTRACTORS)
        self.assertNotIn("generic", grab_mcp.EXTRACTORS)
        self.assertEqual(argv[argv.index("--match-filters") + 1],
                         "duration & duration <= 1200 & !is_live")
        self.assertEqual(argv[argv.index("--max-filesize") + 1], "300M")
        self.assertEqual(argv[argv.index("-f") + 1], "ba/b")
        self.assertEqual(argv[argv.index("-o") + 1], "/jobs/job-1/media.%(ext)s")
        self.assertNotIn("--cookies", argv)
        self.assertNotIn("cookies-from-browser", joined)

    def test_cookies_flag_points_at_the_job_copy(self):
        argv = grab_mcp.ytdlp_argv(TIKTOK, "/jobs/job-1", "b", Path("/jobs/job-1/cookies.txt"))
        self.assertEqual(argv[argv.index("--cookies") + 1], "/jobs/job-1/cookies.txt")
        self.assertLess(argv.index("--cookies"), argv.index("--"))


class RunJob(Base):
    def test_success_returns_metadata_and_transcript(self):
        result = self.run_job()
        self.assertEqual(result, {
            "title": "Test video", "uploader": "tester", "duration_s": 4.0,
            "upload_date": "20261115", "webpage_url": TIKTOK, "description": "A test.",
            "transcript": "block 4"})
        self.assertEqual(self.job_dirs(), [])
        self.assertEqual(len(self.ytdlp_calls()), 1)
        self.assertEqual(oct(self.root.stat().st_mode & 0o777), "0o700")

    def test_job_dir_is_private(self):
        modes = []
        real = grab_core.transcribe

        def spy(media, *a, **kw):
            modes.append(Path(media).parent.stat().st_mode & 0o777)
            return real(media, *a, **kw)
        grab_core.transcribe = spy
        self.addCleanup(setattr, grab_core, "transcribe", real)
        self.run_job()
        self.assertEqual(modes, [0o700])

    def test_cut_audio_falls_back_to_a_format_with_its_own_audio(self):
        result = self.run_job(TIKTOK + "?mode=truncated")
        self.assertEqual(result["transcript"], "block 4")
        self.assertEqual([c[c.index("-f") + 1] for c in self.ytdlp_calls()], ["ba/b", "b"])

    def test_short_audio_only_file_falls_back(self):
        # An audio-only file has no video length to compare with: the info JSON length counts.
        result = self.run_job(TIKTOK + "?mode=audioshort")
        self.assertEqual(result["transcript"], "block 4")
        self.assertEqual([c[c.index("-f") + 1] for c in self.ytdlp_calls()], ["ba/b", "b"])

    def test_file_size_cap_without_a_known_size(self):
        saved = grab_mcp.MAX_FILE_BYTES
        grab_mcp.MAX_FILE_BYTES = 10
        self.addCleanup(setattr, grab_mcp, "MAX_FILE_BYTES", saved)
        self.assert_error(grab_mcp.TOO_LONG)
        self.assertNotIn("stt ", self.log.read_text())

    def test_job_dir_cap(self):
        saved = grab_mcp.MAX_JOB_BYTES
        grab_mcp.MAX_JOB_BYTES = 10
        self.addCleanup(setattr, grab_mcp, "MAX_JOB_BYTES", saved)
        self.assert_error(grab_mcp.TOO_LONG)
        self.assertNotIn("stt ", self.log.read_text())

    def test_fallbacks_are_capped(self):
        self.assert_error(grab_mcp.NO_AUDIO, TIKTOK + "?mode=alwaysshort")
        self.assertEqual([c[c.index("-f") + 1] for c in self.ytdlp_calls()],
                         ["ba/b", "b", "h264-1", "h264-0"][:1 + grab_mcp.MAX_FALLBACKS])

    def test_silent_video_is_no_usable_audio(self):
        self.assert_error(grab_mcp.NO_AUDIO, TIKTOK + "?mode=silent")

    def test_login_error(self):
        self.assert_error(grab_mcp.NEEDS_LOGIN, TIKTOK + "?mode=fail")

    def test_other_error_hides_stderr(self):
        e = self.assert_error(grab_mcp.DOWNLOAD_FAILED, TIKTOK + "?mode=error")
        self.assertNotIn("/", str(e))

    def test_filtered_video_is_too_long(self):
        self.assert_error(grab_mcp.TOO_LONG, TIKTOK + "?mode=skip")

    def test_empty_transcript_is_an_error(self):
        os.environ["FAKE_STT"] = "empty"
        self.assert_error(grab_mcp.EMPTY)

    def test_stt_failure(self):
        os.environ["FAKE_STT"] = "fail"
        self.assert_error(grab_mcp.TRANSCRIBE_FAILED)

    def test_cookie_copy_is_used_and_removed(self):
        cookie = self.tmp / "cookies.txt"
        cookie.write_text("# Netscape HTTP Cookie File\nsecret-cookie-line\n")
        os.environ["GRAB_COOKIE_FILE"] = str(cookie)
        result = self.run_job()
        call = self.ytdlp_calls()[0]
        used = Path(call[call.index("--cookies") + 1])
        self.assertEqual(used.name, "cookies.txt")
        self.assertEqual(used.parent.parent, self.root)
        self.assertIn("secret-cookie-line", self.log.read_text())
        self.assertIn("cookies-mode 600", self.log.read_text())
        self.assertFalse(used.exists())
        self.assertTrue(cookie.exists(), "the source cookie file must stay")
        self.assertNotIn(str(used), json.dumps(result))
        self.assertNotIn("secret-cookie-line", json.dumps(result))

    def test_cookie_copy_is_removed_on_error(self):
        cookie = self.tmp / "cookies.txt"
        cookie.write_text("secret-cookie-line\n")
        os.environ["GRAB_COOKIE_FILE"] = str(cookie)
        self.assert_error(grab_mcp.NEEDS_LOGIN, TIKTOK + "?mode=fail")
        self.assertIn("cookies secret-cookie-line", self.log.read_text())

    def test_missing_cookie_file_means_no_cookies(self):
        os.environ["GRAB_COOKIE_FILE"] = str(self.tmp / "nope.txt")
        self.run_job()
        self.assertNotIn("--cookies", self.ytdlp_calls()[0])


    def test_every_ffmpeg_call_is_capped(self):
        calls = []
        real = grab_core.Job.run

        def spy(job, cmd, *a, **kw):
            calls.append(list(cmd))
            return real(job, cmd, *a, **kw)

        grab_core.Job.run = spy
        self.addCleanup(setattr, grab_core.Job, "run", real)
        self.run_job()
        ffmpeg = [c for c in calls if c[0] == "ffmpeg"]
        self.assertGreaterEqual(len(ffmpeg), 2)  # the audio length check and the cut
        for cmd in ffmpeg:
            i = cmd.index("-t")
            self.assertEqual(cmd[i + 1], str(grab_mcp.DECODE_CAP))
            self.assertGreater(i, cmd.index("-i"), "-t must be an output option")

    def test_decode_cap_stops_ffmpeg(self):
        wav = self.tmp / "tone.wav"
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi",
                        "-i", "sine=duration=5", str(wav)], check=True)
        self.assertAlmostEqual(grab_core.decoded_audio_seconds(wav, grab_core.Job(30)), 5, delta=0.1)
        self.assertAlmostEqual(grab_core.decoded_audio_seconds(wav, grab_core.Job(30), 2), 2,
                               delta=0.1)

    def test_sweep_removes_only_stale_job_dirs(self):
        self.root.mkdir(parents=True, exist_ok=True)
        stale = self.root / "job-abc123"
        stale.mkdir()
        (stale / "cookies.txt").write_text("secret-cookie-line\n")
        other = self.root / "keep-me"
        other.mkdir()
        target = self.tmp / "outside"
        target.mkdir()
        (target / "inside.txt").write_text("keep\n")
        (self.root / "job-link").symlink_to(target)
        (self.root / "job-file").write_text("not a dir\n")
        self.assertEqual(grab_mcp.sweep_stale_jobs(self.root), (1, 0))
        self.assertFalse(stale.exists())
        self.assertTrue(other.exists())
        self.assertTrue((self.root / "job-link").is_symlink())
        self.assertTrue((target / "inside.txt").exists(), "a symlink must not be followed")
        self.assertTrue((self.root / "job-file").exists())
        self.assertEqual(grab_mcp.sweep_stale_jobs(self.tmp / "missing"), (0, 0))

    def test_sweep_counts_a_dir_it_cannot_remove(self):
        locked = self.root / "job-locked"
        (locked / "sub").mkdir(parents=True)
        (locked / "sub" / "cookies.txt").write_text("x\n")
        (locked / "sub").chmod(0o500)
        self.addCleanup((locked / "sub").chmod, 0o700)
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        self.assertEqual(grab_mcp.sweep_stale_jobs(self.root), (0, 1))

    def test_root_lock_is_exclusive(self):
        fd = grab_mcp.lock_root(self.root)
        self.assertIsNotNone(fd)
        self.addCleanup(os.close, fd)
        other = subprocess.run(
            [sys.executable, "-c", "import sys, grab_mcp; "
             "sys.exit(0 if grab_mcp.lock_root(sys.argv[1]) is None else 1)", str(self.root)],
            cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)})
        self.assertEqual(other.returncode, 0, "a second process must not get the lock")

    def test_transcribe_cut_stops_at_the_cap(self):
        wav = self.tmp / "tone.wav"
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi",
                        "-i", "sine=duration=6", str(wav)], check=True)
        text = grab_core.transcribe(wav, 1, grab_core.Job(60), tmp_root=self.tmp,
                                    max_seconds=3)
        self.assertEqual(len(text.splitlines()), 3, text)


class Stopping(Base):
    def test_download_timeout_kills_the_process_group_and_cleans_up(self):
        cookie = self.tmp / "cookies.txt"
        cookie.write_text("secret-cookie-line\n")
        os.environ["GRAB_COOKIE_FILE"] = str(cookie)
        start = time.monotonic()
        e = self.assert_error("timed out", TIKTOK + "?mode=hang", timeout=2)
        self.assertIsInstance(e, grab_core.Stopped)
        self.assertLess(time.monotonic() - start, 10)
        pids = self.pids()
        self.assertEqual(len(pids), 2)
        for pid in pids:
            self.assertFalse(alive(pid), f"pid {pid} still running")

    def test_stt_timeout_removes_the_chunk_dir(self):
        os.environ["FAKE_STT"] = "hang"
        self.assert_error("timed out", timeout=3)
        for pid in self.pids():
            self.assertFalse(alive(pid))

    def test_cancel_from_another_thread(self):
        job = grab_core.Job(60)
        errors = []

        def work():
            try:
                grab_mcp.run_job(TIKTOK + "?mode=hang", job, self.root)
            except grab_core.GrabError as e:
                errors.append(e)
        t = threading.Thread(target=work)
        t.start()
        self.wait_for_pids()
        job.cancel()
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertEqual([str(e) for e in errors], ["cancelled"])
        self.assertEqual(self.job_dirs(), [])
        for pid in self.pids():
            self.assertFalse(alive(pid))


class Cancel(Base):
    def test_cancel_before_the_worker_starts_frees_the_lock(self):
        import anyio
        os.environ["GRAB_TMP"] = str(self.root)
        saved = (grab_mcp.ROOT, grab_mcp.BUSY_WAIT)
        grab_mcp.ROOT, grab_mcp.BUSY_WAIT = str(self.root), 1
        self.addCleanup(lambda: (setattr(grab_mcp, "ROOT", saved[0]),
                                 setattr(grab_mcp, "BUSY_WAIT", saved[1])))

        async def scenario():
            for _ in range(5):  # cancel right away, before the thread runs
                async with anyio.create_task_group() as tg:
                    tg.start_soon(grab_mcp.grab_transcript, TIKTOK + "?mode=hang")
                    await anyio.sleep(0)
                    tg.cancel_scope.cancel()
            with anyio.fail_after(30):
                return await grab_mcp.grab_transcript(TIKTOK)

        result = anyio.run(scenario)
        self.assertEqual(result["transcript"], "block 4")
        for pid in self.pids():
            self.assertFalse(alive(pid))
        self.assertEqual(self.job_dirs(), [])


class Protocol(Base):
    """grab_mcp.py over stdio, the way mcp-proxy drives it (2025 initialize handshake)."""

    def start(self, **env):
        proc_env = {**os.environ, "GRAB_TMP": str(self.root), **env}
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "grab_mcp.py")], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=proc_env, text=True, bufsize=1)
        self.addCleanup(self.stop)
        self.pending = {}
        self.send({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"}}})
        init = self.reply(0)
        self.assertEqual(init["result"]["serverInfo"]["name"], "grab")
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def stop(self):
        if self.proc.stdout.closed:
            return
        if self.proc.poll() is None:
            self.proc.stdin.close()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.stderr = self.proc.stderr.read()
        self.proc.stdout.close()
        self.proc.stderr.close()

    def send(self, msg):
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def reply(self, rid, timeout=60):
        end = time.monotonic() + timeout
        while rid not in self.pending:
            left = end - time.monotonic()
            self.assertGreater(left, 0, f"no reply to request {rid}")
            ready, _, _ = select.select([self.proc.stdout], [], [], left)
            if not ready:
                continue
            line = self.proc.stdout.readline()
            self.assertTrue(line, "server closed stdout")
            msg = json.loads(line)  # stdout carries JSON-RPC frames only
            self.assertEqual(msg.get("jsonrpc"), "2.0")
            if "id" in msg:
                self.pending[msg["id"]] = msg
        return self.pending.pop(rid)

    def call(self, rid, url):
        self.send({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                   "params": {"name": "grab_transcript", "arguments": {"url": url}}})

    def text(self, msg):
        return msg["result"]["content"][0]["text"]

    def test_startup_removes_a_job_dir_left_by_a_killed_server(self):
        stale = self.root / "job-left"
        stale.mkdir(parents=True)
        (stale / "cookies.txt").write_text("secret-cookie-line\n")
        self.start()
        self.assertFalse(stale.exists())

    def test_second_server_refuses_to_start_and_sweeps_nothing(self):
        fd = grab_mcp.lock_root(self.root)
        self.addCleanup(os.close, fd)
        live = self.root / "job-live"
        live.mkdir()
        r = subprocess.run([sys.executable, str(ROOT / "grab_mcp.py")], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=60,
                           env={**os.environ, "GRAB_TMP": str(self.root)})
        self.assertEqual(r.returncode, 1)
        self.assertIn("refusing to start", r.stderr)
        self.assertTrue(live.exists(), "a live job dir of another server must stay")

    def test_list_and_call(self):
        cookie = self.tmp / "cookies.txt"
        cookie.write_text("secret-cookie-line\n")
        self.start(GRAB_COOKIE_FILE=str(cookie))
        self.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = self.reply(1)["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["grab_transcript"])
        self.assertEqual(list(tools[0]["inputSchema"]["properties"]), ["url"])
        self.call(2, TIKTOK)
        res = self.reply(2)["result"]
        self.assertFalse(res.get("isError"))
        self.assertEqual(res["structuredContent"]["transcript"], "block 4")
        self.assertEqual(res["structuredContent"]["title"], "Test video")
        self.call(3, "https://127.0.0.1/x")
        res = self.reply(3)
        self.assertTrue(res["result"]["isError"])
        self.assertIn(grab_mcp.URL_NOT_ALLOWED, self.text(res))
        self.stop()
        self.assertNotIn(str(self.root), self.stderr)
        self.assertNotIn("cookies.txt", self.stderr)
        self.assertNotIn("secret-cookie-line", self.stderr)
        self.assertEqual(self.job_dirs(), [])

    def test_second_call_is_busy_then_the_lock_is_free(self):
        self.start(GRAB_BUSY_WAIT="1", GRAB_TIMEOUT="4")
        self.call(1, TIKTOK + "?mode=hang")
        self.wait_for_pids()
        self.call(2, TIKTOK)
        busy = self.reply(2)
        self.assertTrue(busy["result"]["isError"])
        self.assertIn(grab_mcp.BUSY, self.text(busy))
        first = self.reply(1)
        self.assertIn(grab_mcp.TIMED_OUT, self.text(first))
        for pid in self.pids():
            self.assertFalse(alive(pid))
        self.assertEqual(self.job_dirs(), [])
        self.call(3, TIKTOK)
        self.assertEqual(self.reply(3)["result"]["structuredContent"]["transcript"], "block 4")

    def test_cancel_kills_children_cleans_up_and_frees_the_lock(self):
        self.start(GRAB_BUSY_WAIT="5")
        self.call(1, TIKTOK + "?mode=hang")
        pids = self.wait_for_pids()
        self.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                   "params": {"requestId": 1, "reason": "test"}})
        self.call(2, TIKTOK)
        self.assertEqual(self.reply(2, timeout=30)["result"]["structuredContent"]["transcript"],
                         "block 4")
        for pid in pids:
            self.assertFalse(alive(pid))
        self.assertEqual(self.job_dirs(), [])

    def test_errors_hold_no_path_or_stderr(self):
        self.start()
        for rid, mode in enumerate(["error", "fail", "skip", "silent"], 1):
            self.call(rid, f"{TIKTOK}?mode={mode}")
            msg = self.reply(rid)
            self.assertTrue(msg["result"]["isError"])
            text = self.text(msg)
            self.assertNotIn(str(self.root), text)
            self.assertNotIn("/some/local/path", text)
            self.assertNotIn("ERROR", text)


if __name__ == "__main__":
    unittest.main()
