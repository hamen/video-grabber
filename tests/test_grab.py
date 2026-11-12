"""End-to-end tests for grab, with fake yt-dlp and STT commands. No network.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRAB = ROOT / "grab"
FAKES = Path(__file__).resolve().parent / "fakes"


class GrabTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="grab-test-"))
        self.log = self.tmp / "calls.log"
        self.log.touch()
        self.out = self.tmp / "out"
        self.env = dict(os.environ,
                        FAKE_LOG=str(self.log),
                        GRAB_YTDLP=f"{sys.executable} {FAKES / 'fake_ytdlp.py'}",
                        GRAB_STT_CMD=f"{sys.executable} {FAKES / 'fake_stt.py'}")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def run_grab(self, *args, **env):
        return subprocess.run([sys.executable, str(GRAB), "-o", str(self.out), *args],
                              capture_output=True, text=True, env={**self.env, **env})

    def ytdlp_calls(self):
        return [json.loads(l) for l in self.log.read_text().splitlines() if l.startswith("[")]

    def stt_calls(self):
        return [l for l in self.log.read_text().splitlines() if l.startswith("stt ")]

    def folder(self):
        return self.out / "tester_vid1"

    def test_downloads_and_transcribes(self):
        r = self.run_grab("https://example.com/v/1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), str(self.folder()))
        self.assertTrue((self.folder() / "video.mp4").exists())
        self.assertTrue((self.folder() / "video.info.json").exists())
        self.assertEqual((self.folder() / "transcript.txt").read_text(), "block 4\n")
        calls = self.ytdlp_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-1], "https://example.com/v/1")
        self.assertNotIn("--cookies-from-browser", calls[0])
        self.assertNotIn("-f", calls[0])

    def test_long_audio_goes_to_stt_in_blocks(self):
        r = self.run_grab("--chunk", "3", "https://example.com/v/1", FAKE_DURATION="7")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.stt_calls()), 3)
        self.assertEqual((self.folder() / "transcript.txt").read_text(),
                         "block 3\nblock 3\nblock 1\n")

    def test_truncated_audio_triggers_redownload_with_own_audio(self):
        r = self.run_grab("https://example.com/v/1", FAKE_MODE="truncated")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.ytdlp_calls()
        self.assertEqual(len(calls), 2)
        self.assertNotIn("-f", calls[0])
        self.assertEqual(calls[1][calls[1].index("-f") + 1], "b")
        self.assertIn("cut short", r.stderr)
        self.assertEqual((self.folder() / "transcript.txt").read_text(), "block 4\n")

    def test_truncated_audio_in_webm_is_caught(self):
        # WebM has no per-stream durations: the container length must not stand in for
        # the audio length, or a 1-second track in a 4-second video looks complete.
        r = self.run_grab("https://example.com/v?ext=webm&mode=truncated")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("cut short", r.stderr)
        self.assertEqual(len(self.ytdlp_calls()), 2)
        self.assertEqual((self.folder() / "transcript.txt").read_text(), "block 4\n")

    def test_complete_webm_is_accepted_without_retry(self):
        r = self.run_grab("https://example.com/v?ext=webm")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.ytdlp_calls()), 1)

    def test_audio_still_short_after_retry_fails_the_url(self):
        r = self.run_grab("https://example.com/v?mode=alwaysshort")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no format with complete audio", r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.stt_calls(), [])

    def test_silent_video_is_kept_when_not_transcribing(self):
        r = self.run_grab("--no-transcribe", "https://example.com/v?mode=silent")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.ytdlp_calls()), 1)
        self.assertTrue((self.folder() / "video.mp4").exists())

    def test_silent_video_fails_when_transcribing(self):
        r = self.run_grab("https://example.com/v?mode=silent")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no format with complete audio", r.stderr)

    def test_corrupt_file_fails_that_url_and_the_batch_goes_on(self):
        r = self.run_grab("https://example.com/v?id=bad&mode=corrupt",
                          "https://example.com/v?id=good")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout.strip(), str(self.out / "tester_good"))
        self.assertIn("1 of 2 failed", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue((self.out / "tester_good" / "transcript.txt").exists())

    def test_cookies_are_passed_to_ytdlp(self):
        r = self.run_grab("--cookies-from-browser", "chrome", "https://example.com/v/1")
        self.assertEqual(r.returncode, 0, r.stderr)
        call = self.ytdlp_calls()[0]
        self.assertEqual(call[call.index("--cookies-from-browser") + 1], "chrome")

    def test_no_video_deletes_video_and_keeps_transcript(self):
        r = self.run_grab("--no-video", "https://example.com/v/1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.folder() / "video.mp4").exists())
        self.assertTrue((self.folder() / "transcript.txt").exists())

    def test_no_transcribe_skips_stt(self):
        r = self.run_grab("--no-transcribe", "https://example.com/v/1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.stt_calls(), [])
        self.assertFalse((self.folder() / "transcript.txt").exists())

    def test_failed_download_exits_1_with_login_hint(self):
        r = self.run_grab("https://example.com/v/1", FAKE_MODE="fail")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "")
        self.assertIn("download failed", r.stderr)
        self.assertIn("--cookies-from-browser", r.stderr)

    def test_stt_failure_exits_1_and_writes_no_transcript(self):
        r = self.run_grab("https://example.com/v/1", FAKE_STT="fail")
        self.assertEqual(r.returncode, 1)
        self.assertIn("transcription failed", r.stderr)
        self.assertFalse((self.folder() / "transcript.txt").exists())

    def test_empty_transcript_warns_but_succeeds(self):
        r = self.run_grab("https://example.com/v/1", FAKE_STT="empty")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("transcript is empty", r.stderr)
        self.assertEqual((self.folder() / "transcript.txt").read_text(), "")

    def test_url_file_skips_comments_and_blank_lines(self):
        urls = self.tmp / "urls.txt"
        urls.write_text("# my list\nhttps://example.com/v/1  # first\n\n   \n")
        r = self.run_grab("-f", str(urls), "--no-transcribe")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([c[-1] for c in self.ytdlp_calls()], ["https://example.com/v/1"])

    def test_url_file_keeps_fragments_inside_urls(self):
        urls = self.tmp / "urls.txt"
        urls.write_text("https://example.com/v/1#t=30 # start at 30 s\n")
        r = self.run_grab("-f", str(urls), "--no-transcribe")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([c[-1] for c in self.ytdlp_calls()], ["https://example.com/v/1#t=30"])

    def test_missing_url_file_is_a_usage_error(self):
        r = self.run_grab("-f", str(self.tmp / "nope.txt"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("cannot read", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_missing_stt_command_stops_before_download(self):
        r = self.run_grab("https://example.com/v/1", GRAB_STT_CMD="no-such-stt-command")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("STT command not found", r.stderr)
        self.assertEqual(self.ytdlp_calls(), [])

    def test_no_urls_is_a_usage_error(self):
        r = self.run_grab()
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
