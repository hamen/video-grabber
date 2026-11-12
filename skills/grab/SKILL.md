---
name: grab
description: >
  Download a video from TikTok, Instagram, YouTube, X or any other site yt-dlp supports, and
  transcribe it with the local speech-to-text command. Use when the user pastes a video link and
  wants the video, its transcript, or what the video says ("download this", "transcribe this
  TikTok", "what does this reel say", "grab these videos"). Not for live streams.
allowed-tools: Bash, Read
---

# grab

`grab` downloads each URL with yt-dlp, checks that the audio track is complete, and sends the
audio to the local STT command in 5-minute blocks. It writes one folder per video.

## Run it

```bash
grab URL [URL ...]                                   # video + transcript
grab --cookies-from-browser chrome URL               # TikTok, Instagram: sites that want a login
grab --no-video URL                                  # transcript only, delete the video after
grab --no-transcribe URL                             # video only
grab -f urls.txt -o ~/videos                         # many URLs, one per line, # for comments
```

Short links work as they are (`vm.tiktok.com/...`, `youtu.be/...`, `instagram.com/reel/...`).

## Read the result

- **stdout** has one line per finished video: its folder. Nothing else goes to stdout, so
  `grab URL | while read -r d; do cat "$d/transcript.txt"; done` is safe.
- **stderr** has progress, warnings and errors.
- Each folder, `OUT/<uploader>_<id>/` (default `OUT` is `./grab_out`), holds:
  - `video.<ext>`: the video (gone with `--no-video`)
  - `video.info.json`: yt-dlp metadata: `title`, `description`, `uploader`, `duration`,
    `upload_date`, `view_count`, `webpage_url`
  - `transcript.txt`: plain text, no timestamps, one line per 5-minute block
- **Exit status**: 0 when every URL worked, 1 when at least one failed, 2 for a usage error.
  The URLs that worked are still on stdout when the status is 1.

## Handle the usual failures

| stderr says | Do this |
| --- | --- |
| `download failed` + login hint | Run again with `--cookies-from-browser chrome` (or the browser the user is logged in with). Ask the user first if you do not know that they agreed to this: it uses their account. |
| `audio track missing or cut short` | Nothing. `grab` downloads a format with its own audio by itself. |
| `no format with complete audio` | The site gave no usable audio. Tell the user; do not retry in a loop. |
| `transcript is empty` | The video has no speech (music, silent clip) or the STT heard none. Say so; do not invent a summary. |
| `transcription failed` | The STT command broke. Check it on its own: `$GRAB_STT_CMD some.wav`. |
| `STT command not found` | Set `GRAB_STT_CMD`, or run with `--no-transcribe`. |

## Rules for agents

- Report what the transcript says, not what you think the video says. If it is empty, say that.
- The transcript has no punctuation guarantees and can split a word at a 5-minute cut. Quote it
  with that in mind.
- Do not upload the video or the transcript anywhere unless the user asks. A downloaded video
  belongs to its creator.
- Use `--cookies-from-browser` only with the user's consent: it reads their browser login.
