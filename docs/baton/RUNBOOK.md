# Baton install runbook

For the installing agent. Follow the steps in order. Do exactly what each step says.

## Rules

1. Run each step with `bash install/run.sh <step>`. Do not run the step scripts directly. Do not edit any script.
2. A step succeeded only if its last line starts with `OK:`.
3. If the last line starts with `FAIL:`, **stop**. `run.sh` has already saved the full output in `install/reports/` and pushed it to the `reports` branch. Tell Squid the step name and the `FAIL:` line. Do not retry, skip, improvise or fix anything yourself.
4. Steps marked **SQUID** need Squid. Say: "Squid, please run this step and tell me when it is done", show the command, and wait.
5. **HOST** means a normal terminal on the Bazzite machine. **BOX** means inside the `arch` distrobox.
   - If you are on the host, run BOX commands as: `distrobox enter arch -- bash -lc 'cd ~/nanobot && <command>'`
   - If you are inside the box, run HOST commands as: `distrobox-host-exec bash -lc 'cd ~/nanobot && <command>'`
6. Every step is safe to run again.

## Steps

**0. Get the code** — HOST

```
[ -d ~/nanobot/.git ] || git clone https://github.com/bgrimes-me/nanobot ~/nanobot
cd ~/nanobot && git pull --ff-only
```

Expected: no error. Otherwise: tell Squid the error and stop.

**1. Look at the machine** — HOST

```
cd ~/nanobot && bash install/run.sh 00-recon
```

Expected last line: `OK: recon finished, report written to …`. The report is pushed to the `reports` branch even when it passes. Otherwise: stop (rule 3).

**2. Check the box** — BOX

```
cd ~/nanobot && bash install/run.sh 01-box-check
```

Expected: `OK: the box has git, uv, python … and FTS5`.
If it says `FAIL: missing in the box: …`, go to step 2b. Any other `FAIL:`: stop (rule 3).

**2b. Install packages** — BOX, **SQUID** (uses sudo)

```
cd ~/nanobot && bash install/run.sh squid-01-packages
```

Expected: `OK: packages installed`. Then run step 2 again.

**3. Install nanobot with Baton** — BOX

```
cd ~/nanobot && bash install/run.sh 02-install-nanobot
```

Expected: `OK: nanobot installed at …`. It may take several minutes. A note saying the WebUI build failed is fine as long as the last line is `OK:`.

**4. Write the config** — BOX

```
cd ~/nanobot && bash install/run.sh 03-config
```

Expected: `OK: config written (llama-server …, model …, n_ctx …)`.

**5. Found the town** — BOX

```
cd ~/nanobot && bash install/run.sh 04-found-town
```

Expected: a table of three residents (mayor, archivist, inspector), then `OK: the town is founded`.

**6. Write the services** — BOX

```
cd ~/nanobot && bash install/run.sh 05-units
```

Expected: `OK: units written to …; next: ask Squid …`.

**7. Start the services** — HOST, **SQUID**

```
cd ~/nanobot && bash install/run.sh squid-06-host-services
```

Expected: `OK: services installed and running; linger is on`.

**8. Check everything** — BOX

```
cd ~/nanobot && bash install/run.sh 07-doctor
```

Expected: a table where every row is PASS (a WARN on "shell privacy" is acceptable), then `OK: doctor passed: the town is working`.

**9. Done**

Tell Squid: "Baton is installed. Doctor passed. You can talk to the Mayor with `nanobot` in the box." Then stop.
