---
title: Asking for a session
when: before you ask for a session (request_session)
tools: [request_session]
offices: [mayor]
---
{{ owner }} reads every request before anything runs. Make it quick to say yes to.

- **Least access that will do.** Exact commands are better than turning the shell on: `sudo pacman -S --noconfirm ripgrep`, written exactly as it will run.
- **Nobody is at a terminal**: give flags like `--noconfirm` or `-y`. `sudo` works only where the machine allows it without a password.
- **Say why** in a sentence {{ owner }} can judge.
- **Private or public.** From a private session you get the resident's summary. If you need to see the output yourself (a version, a log, a list), ask for `visibility: public`.
- One task per request; split bigger work into several.
