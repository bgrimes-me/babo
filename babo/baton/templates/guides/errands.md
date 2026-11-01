---
title: Sending a resident on an errand
when: before you send a resident on an errand (send_errand)
tools: [send_errand]
offices: [mayor]
---
{{ owner }} reads every errand before anything runs. Make it quick to say yes to.

- **Least access that will do.** Exact commands are better than turning the shell on: `sudo pacman -S --noconfirm ripgrep`, written exactly as it will run.
- **Nobody is at a terminal**: give flags like `--noconfirm` or `-y`. `sudo` works only where the machine allows it without a password.
- **Say why** in a sentence {{ owner }} can judge.
- **Private or public.** From a private errand you get the resident's summary. If you need to see the output yourself (a version, a log, a list), ask for `visibility: public`.
- One task per errand; split bigger work into several.
