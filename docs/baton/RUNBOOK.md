# Babo install runbook

For the installing agent. Follow the steps in order. Do exactly what each step says.

## Rules

1. Run each step with `bash install/run.sh <step>`. Do not run the step scripts directly. Do not edit any script.
2. A step succeeded only if its last line starts with `OK:`.
3. If the last line starts with `FAIL:`, **stop**. `run.sh` has already saved the full output in `install/reports/` (and, if the owner set `BABO_PUSH_REPORTS=1`, pushed it to the repository's `reports` branch). Tell the owner the step name and the `FAIL:` line. Do not retry, skip, improvise or fix anything yourself.
4. Steps marked **OWNER** need the owner (the person whose machine this is). Say: "Please run this step and tell me when it is done", show the command, and wait.
5. **HOST** means a normal terminal on the machine itself. **BOX** means inside the distrobox the town runs in (an Arch box). Its name is BOXNAME below: ask the owner, and if it isn't `babo`, put `BATON_BOX=BOXNAME` in front of every `bash install/run.sh` command.
   - There is **one copy of the code: `~/babo` inside the box.** The box has its own home folder, so on the host that same folder has a different path (for example `~/DistroBoxes/BOXNAME/home/babo`). Inside the box, `echo "$HOME/babo"` prints it. Call it CODE. Never use a `~/babo` on the host.
   - If you are on the host, run BOX commands as: `distrobox enter BOXNAME -- bash -lc 'cd ~/babo && <command>'`
   - If you are inside the box, run HOST commands as: `distrobox-host-exec bash -lc "cd $HOME/babo && <command>"` (double quotes, so `$HOME` is the box's home, which the host can reach).
6. Every step is safe to run again.

## Steps

**0. Get the code** — BOX

```
[ -d ~/babo/.git ] || git clone https://github.com/bgrimes-me/babo ~/babo
cd ~/babo && git pull --ff-only
```

Expected: no error. Otherwise: tell the owner the error and stop.

**1. Look at the machine** — HOST (run it from inside the box like this)

```
distrobox-host-exec bash -lc "cd $HOME/babo && bash install/run.sh 00-recon"
```

If it says it found no llama-server, run it again with the address added: `distrobox-host-exec bash -lc "cd $HOME/babo && BATON_LLAMA_URL=http://127.0.0.1:8080 bash install/run.sh 00-recon"`.

Expected last line: `OK: recon finished, report written to …`. Otherwise: stop (rule 3).

**2. Check the box** — BOX

```
cd ~/babo && bash install/run.sh 01-box-check
```

Expected: `OK: the box has git, uv, python … and FTS5`.
If it says `FAIL: missing in the box: …`, go to step 2b. Any other `FAIL:`: stop (rule 3).

**2b. Install packages** — BOX, **OWNER** (uses sudo)

```
cd ~/babo && bash install/run.sh host-packages
```

Expected: `OK: packages installed`. Then run step 2 again.

**3. Install Babo** — BOX

```
cd ~/babo && bash install/run.sh 02-install-babo
```

Expected: `OK: babo installed at …`. It may take several minutes. A note saying the WebUI build failed is fine as long as the last line is `OK:`.

**4. Write the config** — BOX

```
cd ~/babo && bash install/run.sh 03-config
```

Expected: `OK: config written (llama-server …, model …, n_ctx …)`.

**5. Found the town** — BOX

```
cd ~/babo && BATON_OWNER="NAME" bash install/run.sh 04-found-town
```

NAME is what the town will call the owner: ask them ("What should the town call you?"). Expected: a table with one resident, the mayor, then `OK: the town is founded`. Other residents are founded later, when the Mayor asks and the owner approves.

**6. Write the services** — BOX

```
cd ~/babo && bash install/run.sh 05-units
```

Expected: `OK: units written to …; next: ask the owner …`.

**7. Start the services** — HOST, **OWNER**

The owner runs this in a host terminal, from CODE (rule 5). Run `echo "$HOME/babo"` in the box and give the owner the command with that folder filled in:

```
cd CODE && bash install/run.sh host-services
```

Expected: `OK: services installed and running; linger is on`.

**8. Check everything** — BOX

```
cd ~/babo && bash install/run.sh 07-doctor
```

Expected: a table where every row is PASS (a WARN on "shell privacy" is acceptable), then `OK: doctor passed: the town is working`.

**9. Done**

Tell the owner: "Babo is installed. Doctor passed. You can talk to the Mayor with `babo` in the box." Then stop.

## Starting over

To start again from a fresh install (for example in a new box, or to try a clean town): stop the services on the host (`systemctl --user stop babo-queue babo-gateway babo-upkeep`), then in the box run `babo reset` (add `--keep-config` to keep models, keys and channels). It packs the town, the residents' opencode data and Babo's home into one archive in `~/babo-backups` before removing them. Then follow steps 4 to 9 above (skip step 4 if the config was kept).

## Upgrading an existing install

The same rules apply. Everything is kept: the town, its git history, the residents' memory and your settings. Step 4 also removes what earlier versions left behind (obsolete settings, old config backups, stale folders in the checkout) and lists each thing it removed; step 7 removes Babo services that are no longer part of it.

1. Get the latest code — BOX: `cd ~/babo && git pull --ff-only`
2. Reinstall — BOX: `cd ~/babo && bash install/run.sh 02-install-babo`
3. Refresh the config — BOX: `cd ~/babo && bash install/run.sh 03-config`. This also finds opencode and records where it is.
4. Bring the town's files up to date and tidy — BOX: `cd ~/babo && bash install/run.sh 04-found-town`. Files nobody has edited (charter, house rules, roles, `.gitignore`) are updated; everything else is left alone.
5. Write the services — BOX: `cd ~/babo && bash install/run.sh 05-units`
6. Start the services — HOST, **OWNER**: as in step 7 above, with CODE = the box's `$HOME/babo`.
7. Check everything — BOX: `cd ~/babo && bash install/run.sh 07-doctor`

What an upgrade changes, and what it doesn't:

- **The owner's name and the install steps' names.** The town now calls its owner by name (a town founded before keeps the name its charter used); `babo found --owner NAME` changes it. The owner's steps are now `host-packages` and `host-services`; the old `squid-…` names still work for one more release. Install reports stay on the machine unless `BABO_PUSH_REPORTS=1` is set.
- **Residents other than the Mayor now run in opencode.** Install it in the box if it isn't there (`command -v opencode`, otherwise `curl -fsSL https://opencode.ai/install | bash`); it needs opencode.ai's opencode 2 (`opencode --version` says 2.x; `opencode upgrade` updates an opencode 1), and the doctor starts it to check. If another opencode comes first on your PATH, point Babo at the right one with `baton.opencode.bin` in `~/.babo/config.json`. Residents already in the town stay, with their memory; they work in opencode from their next session.
- **The night timer is replaced by the upkeep service.** Step 6 removes `babo-night.timer` and starts `babo-upkeep`. A town from before keeps its history; its night logs stay in `state/night-logs/`.
- **Profiles.** Every agent keeps exactly the tools its office had until you change its profile (`babo profile`, or the Town page's Profiles tab). Web search is off for everyone until you allow it.
- **New towns start with the Mayor alone.** An existing town keeps the residents it has.
