<p align="center">
  <img src="docs/header.png" alt="grab: paste a video link, get the video and its transcript. TikTok, Instagram or YouTube, through yt-dlp, an audio check and your local speech-to-text, into transcript.txt." width="100%">
</p>

# grab

`grab` downloads a video from TikTok, Instagram, YouTube or any of the other sites that
[yt-dlp](https://github.com/yt-dlp/yt-dlp) supports, and transcribes it with a speech-to-text
command that runs on your machine. No audio goes to a cloud service.

```console
$ grab --cookies-from-browser chrome https://vm.tiktok.com/<link>/
== https://vm.tiktok.com/<link>/
   audio track missing or cut short: downloading a format with its own audio
   transcript: grab_out/<creator>_<video-id>/transcript.txt (2170 words)
grab_out/<creator>_<video-id>
```

That run is an 11-minute TikTok. It shows the two problems `grab` handles for you:

1. **Broken audio from TikTok.** yt-dlp picks the best video format by default. On TikTok that
   is an H.265 stream with no sound, which yt-dlp joins with TikTok's separate audio stream. In
   this video that audio stream was 1.7 seconds long. `grab` measures the audio after every
   download, and when it is missing or shorter than 90% of the video, it downloads a format that
   has its own audio.
2. **Length limits in local models.** The model behind my STT command refuses audio longer than
   about 6.5 minutes. `grab` cuts the audio into 5-minute blocks (`--chunk` changes the length)
   and joins the text.

It also runs the latest yt-dlp release through `uvx` each time, because TikTok and Instagram
change often and an old yt-dlp stops working on them.

## Install

You need Python 3.8 or later, `ffmpeg` and `ffprobe`, and [`uv`](https://docs.astral.sh/uv/)
(or a `yt-dlp` on your `PATH`). `grab` uses the Python standard library only.

```bash
git clone https://github.com/hamen/video-grabber.git
ln -s "$PWD/video-grabber/grab" ~/.local/bin/grab
```

Then tell `grab` how to transcribe (next section), or use it with `--no-transcribe`.

## Connect your speech-to-text

`grab` calls one command for each audio block:

```
$GRAB_STT_CMD /path/to/block.wav
```

The file is a 16 kHz mono WAV. The command prints the text on stdout and exits 0. Anything that
works this way will do: a wrapper around whisper.cpp, faster-whisper, or a local STT server.
The default is `stt-file`, the name of the wrapper I use around the Nemotron server in
[speech-to-text-for-ubuntu](https://github.com/hamen/speech-to-text-for-ubuntu).

```bash
export GRAB_STT_CMD="my-stt --model small"   # parsed like a shell command line
```

## Use

```bash
grab URL [URL ...]                       # video + transcript
grab --cookies-from-browser chrome URL   # sites that want a login (TikTok, Instagram)
grab --no-video URL                      # keep only the transcript and the metadata
grab --no-transcribe URL                 # download only
grab -f urls.txt -o ~/videos             # one URL per line; # starts a comment
```

Each video gets a folder, `grab_out/<uploader>_<id>/`, with:

- `video.mp4` (or whatever format the site gave), unless you pass `--no-video`
- `video.info.json`, the yt-dlp metadata: title, description, uploader, duration, upload date
- `transcript.txt`, plain text, one line per audio block

On stdout, `grab` prints the folder of each finished video and nothing else, so you can pipe it.
Progress and errors go to stderr. The exit status is 1 if any URL failed.

`--cookies-from-browser` hands your browser's login for the site to yt-dlp. In November 2026
TikTok refused every download without it. Downloads then count against your account.

I tested `grab` on TikTok (short `vm.tiktok.com` links included) and YouTube. Instagram goes
through the same yt-dlp path, but I have not tested it yet.

## For AI agents

The agent instructions are in [`skills/grab/SKILL.md`](skills/grab/SKILL.md): when to use the
tool, how to read its output, and what to do for each error. Install it as a skill for Claude
Code and for the agents that read `~/.agents/skills` (Codex and others):

```bash
ln -s "$PWD/video-grabber/skills/grab" ~/.claude/skills/grab
ln -s "$PWD/video-grabber/skills/grab" ~/.agents/skills/grab
```

Link the directory, not the `SKILL.md` file: Codex skips a skill whose `SKILL.md` is a symlink.
Agents that change the code read [`AGENTS.md`](AGENTS.md).

## MCP mode

`grab_mcp.py` serves `grab` as an [MCP](https://modelcontextprotocol.io) server over stdio,
so an AI client can ask for a transcript. It has one tool:

```
grab_transcript(url) -> {title, uploader, duration_s, upload_date, webpage_url, description, transcript}
```

```bash
uv run grab_mcp.py      # uv installs the mcp package that the script declares inline
```

It is stricter than the CLI, because the caller is a model and not you at a terminal:

- **Sites:** an `https` link to TikTok, Instagram, YouTube or X (their subdomains and short
  links too). No other host, no IP address, no port, no user name in the URL. yt-dlp runs
  with only the extractors for those sites, so a link that leads to a page on another site
  fails. This is a filter, not network isolation: the media URLs that an extractor finds are
  not checked.
- **Limits:** videos of at most 20 minutes with a known length, no live streams, at most
  300 MB per file, 10 minutes per call. One call runs at a time; a second call waits up to
  60 seconds, then gets "busy".
- **Audio only:** it downloads the audio track first, and a full format only when that audio
  is missing or cut. No video file is kept.
- **Clean up:** each call works in a new private directory under `$GRAB_TMP`, deleted at the
  end, also on a timeout or a cancel. A timeout or a cancel kills yt-dlp, ffmpeg and the STT
  command, with their children.
- **Errors:** short messages ("video needs a login", "too long", "transcript is empty", ...).
  The result never holds yt-dlp, ffmpeg or STT output or a local path. An empty transcript is
  an error.
- **Cookies:** it never reads a browser. Set `GRAB_COOKIE_FILE` to a Netscape `cookies.txt`
  (for example one that `yt-dlp --cookies-from-browser chrome --cookies FILE` wrote) and each
  call uses a private copy of it. Keep only the cookies of the sites above in that file.

Environment: `GRAB_TMP` (default: a `grab-mcp` directory in the system temp),
`GRAB_COOKIE_FILE`, `GRAB_STT_CMD`, `GRAB_YTDLP`, `GRAB_TIMEOUT` (seconds, default 600),
`GRAB_BUSY_WAIT` (seconds, default 60).

To reach it from a remote client, put an HTTP bridge such as
[mcp-proxy](https://github.com/sparfenyuk/mcp-proxy) in front of it, bound to localhost, and
an authentication layer in front of that. Anyone who can call the tool makes your machine
download from those sites, with your cookies if you set a cookie file. mcp-proxy does not pass
its own environment to the server: give every variable above, and `PATH`, with `--env`.

## Develop

```bash
git config core.hooksPath .githooks   # once per clone: the pre-push hook runs bin/ci
bin/ci                                 # syntax, leak scan, tests
```

The tests use fake yt-dlp and STT commands in `tests/fakes/` and never touch the network. They
need `ffmpeg`, because they make real media files, and `uv`, which brings the `mcp` package for
the MCP tests.

## License

MIT. See [LICENSE](LICENSE).
