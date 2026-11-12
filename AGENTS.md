# AGENTS.md

Instructions for AI agents.

- **To use the tool**, read [`skills/grab/SKILL.md`](skills/grab/SKILL.md). Install it as a skill
  with the commands in the README, section "For AI agents".
- **To change the tool**: `grab` is one Python file with the standard library only. Keep it that
  way. Run `bin/ci` before you push; the pre-push hook runs it too
  (`git config core.hooksPath .githooks` once per clone). Never bypass the hook.
- Every behavior change gets a test in `tests/test_grab.py`. The tests use the fakes in
  `tests/fakes/` and never touch the network.
- The repo is public. `bin/ci` refuses home paths, LAN addresses and personal e-mail addresses.
  Use `example.com` URLs in docs and tests.
