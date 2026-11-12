# Transcode agent

Linux only. The agent uses Python's standard library and host ffmpeg/ffprobe;
`src/transcode_agent` never imports AniKeepOrNot. The worker's Local mode
uses the same `encode.py` implementation. All traffic uses HTTP over TCP.

## Install: from export to a working agent

The agent is one file, `adm-agent.pyz`. The agent machine needs Linux, systemd,
Python 3.11+ and ffmpeg/ffprobe (with libx265 and libopus; `hevc_vaapi` and
`mesa-va-drivers` for GPU encoding). No Git, checkout, database or AniKeepOrNot
installation is needed there.

**1. Export it** on the AniKeepOrNot server (or any checkout):

```sh
adm agent pack --output ./adm-agent.pyz
```

**2. Copy it** to the agent machine and check it arrived intact. Nothing copies
it automatically; use a channel you trust:

```sh
scp adm-agent.pyz adm-agent.pyz.sha256 agent-host:
ssh agent-host 'sha256sum -c adm-agent.pyz.sha256'
```

**3. Install it.** The account that runs the command decides the kind of
install; it is announced before anything changes.

- System service (dedicated agent hosts): `sudo ./adm-agent.pyz agent install`
  prints "Installing as root: system service (runs as transcode)". It creates
  the `transcode` account (added to the GPU device's group), installs
  `/opt/transcode-agent/adm-agent.pyz`, `/etc/transcode-agent.env` (0600) and
  `/etc/systemd/system/transcode-agent.service`, enables and starts it. On
  Debian/Ubuntu it can install missing packages (asked, or `--install-deps`).
- User service (no root): `./adm-agent.pyz agent install` prints
  "Installing as alice: user service (systemctl --user)". Files go to
  `~/.local/share/anikeepornot/transcode-agent/`,
  `~/.config/anikeepornot/transcode-agent/transcode-agent.env` and
  `~/.config/systemd/user/transcode-agent.service` (XDG variables are honoured).
  It never uses sudo or changes groups: if you cannot open the GPU device it
  says which `usermod` an administrator can run, or choose CPU only. Run it from
  a real login (desktop or ssh as that user, not `sudo -u`/`su`), which has a
  systemd user manager. A user service runs only while you are logged in unless
  lingering is on: `sudo loginctl enable-linger alice` (the installer reports
  the current setting; it never changes it).

It asks for the listen address (default `0.0.0.0:8787`), encoders, GPU device
and temporary folder; `--yes` with `--listen`, `--encoders`, `--vaapi-device`
and `--tmp` asks nothing. Reruns keep every setting and the token. The install
only reports success after the agent answers its authenticated health check.

**4. Pair it** in AniKeepOrNot: Settings > Transcode > Remote. Enter the
address the installer printed (for example `192.168.1.50:8787`) and the token,
then Save changes. Either:

- show the agent's token and paste it into the Token field:
  `sudo ./adm-agent.pyz agent token` (user install: no sudo), or
- click Generate in Settings, then on the agent run
  `sudo ./adm-agent.pyz agent token --set` and paste it (asked without echo).

The two must be identical. Settings never shows a saved token again; the token
is only in the agent's environment file and AniKeepOrNot's secrets store.

**5. Check it.** The service bar shows `agents 1/1` in green. Its tooltip names
any problem per agent: no token set, token rejected, unreachable, or needs an
update. On the agent:

```sh
./adm-agent.pyz agent status                      # installed version, service, health
sudo systemctl status transcode-agent                    # user install: systemctl --user status transcode-agent
sudo journalctl -u transcode-agent                       # user install: journalctl --user -u transcode-agent
```

A test video, run on the agent (`TOKEN` from `agent token`):

```sh
ffmpeg -v error -y -f lavfi -i testsrc2=size=1280x720:rate=24 -f lavfi -i sine -t 5 -c:v libx264 -c:a flac sample.mkv
A="Authorization: Bearer $TOKEN"; U=http://localhost:8787
ID=$(curl -s -H "$A" -H "X-Sha256: $(sha256sum sample.mkv | cut -d' ' -f1)" -H "X-Video-Kbps: 1000" \
  --data-binary @sample.mkv $U/jobs | python3 -c 'import sys,json;print(json.load(sys.stdin)["job_id"])')
until curl -s -H "$A" $U/jobs/$ID | grep -qE '"state": "(done|failed)"'; do sleep 1; done
curl -s -H "$A" $U/jobs/$ID; curl -s -X DELETE -H "$A" $U/jobs/$ID
```

It should end `"state": "done"` with `"encoder": "gpu"` (or `"cpu"`).

**Crashes and reboots.** The unit restarts the agent 5 seconds after a crash
(up to 5 times in 5 minutes) and starts it at boot (user installs: with
lingering). Stopping or restarting it cancels a running encode; ffmpeg stops
with it. Jobs are not continued after a restart: the agent forgets them, and
AniKeepOrNot sends the video again within its three-attempt budget.

**Updating.** Export the new version, copy and check it as above, then:

```sh
sudo ./adm-agent.pyz agent upgrade --from ./adm-agent.pyz   # user install: no sudo
sudo /opt/transcode-agent/adm-agent.pyz agent rollback       # back to the previous version
```

The new archive is checked before the service stops. Upgrade waits while an
encode runs (`--interrupt` to go ahead), keeps the settings, token and the
previous archive, and puts the previous one back if the new one does not pass
its health check. Run upgrade, rollback, status and token as the account that
installed the agent; the other kind of install is refused with an explanation.

**Network.** AniKeepOrNot must reach the agent's port. Tokens and videos travel
over plain HTTP, so keep the agent on a trusted network or put HTTPS in front
of it. The installer does not change firewalls or routers.

Environment settings:

| Variable | Default | Meaning |
|---|---|---|
| TRANSCODE_LISTEN | 0.0.0.0:8787 | Listening host:port |
| TRANSCODE_TOKEN | required | Bearer token on every route |
| TRANSCODE_ENCODERS | gpu,cpu | Allowed encoders; GPU always tried first |
| TRANSCODE_VAAPI_DEVICE | /dev/dri/renderD128 | VAAPI render node |
| TRANSCODE_TMP | /var/tmp (user install: ~/.cache/anikeepornot) | Parent of the agent's private work folder |

Only `<TRANSCODE_TMP>/transcode-agent/` is used and wiped at startup. Sibling
files survive, including when the configured parent is a home or data folder.
Use a separate temp parent for each agent/Local worker on the same host.
Uploads need at least 1.5 times their Content-Length free; insufficient space
returns 507 before the body is read. Finished jobs expire after one hour.
One encode runs at a time. Keep this service on a trusted LAN or use HTTPS
through a reverse proxy; a bearer token does not encrypt a plain HTTP transfer.

## Running the agent

`adm agent run` (or just `adm agent`) runs the agent in the foreground with the
environment settings below; `adm agent --version` prints its agent version and
build revision, and `adm agent --help` lists the settings. `python -m
src.transcode_agent` is the same command. Stopping it (SIGTERM/SIGINT) cancels
any encode, so no ffmpeg process outlives the agent.

`adm server agent ...` is the older spelling and still works, in `adm` and in the
archive. The installed unit's `ExecStart` keeps it (`adm-agent.pyz server agent
run`) because every archive version understands it, so a rollback to an older
archive still starts.

## One-file agent archive

`adm agent pack --output ./adm-agent.pyz` builds the agent as one
executable file with Python's `zipapp`, from the installation or checkout it
runs in; no network or CI is needed. It contains only the agent modules
(unchanged), the archive entry point, the service and environment templates and
`build.json` (agent version, source revision, per-file SHA-256). Credentials,
settings, databases, media, `.git` and tests are never read. A source revision
ending in `+modified` means the packed agent files had uncommitted changes.

`adm-agent.pyz.sha256` is written next to it: `sha256sum -c adm-agent.pyz.sha256`
on the agent machine confirms the copy arrived intact. It does not prove who
built the file; transfer it over a channel you trust (for example `scp`).

The agent machine needs Python 3.11+ and ffmpeg/ffprobe, nothing else:
`./adm-agent.pyz agent --version` and `./adm-agent.pyz agent --help`
work anywhere; only `agent` commands are in the archive.

## Agent versions

Each agent reports `agent_version` in `/health`. The worker uses only agents
whose version equals its own `AGENT_VERSION` (src/transcode_agent/__init__.py);
the build revision is shown for diagnostics only. The version is checked by the
periodic health check and again right before every upload, so a worker upgrade
never relies on an older answer.

- An older agent (including one from before versions existed, which reports
  none) gets no new work; the service bar and the waiting job say "Agent
  pc:8787 is version 1; this worker requires version 2. Update this agent."
- A newer agent says to update AniKeepOrNot instead.
- Other compatible agents keep working in their configured order. When none
  can, the job waits; a version refusal never uploads and never spends one of
  the three encode attempts.
- An encode already running is not interrupted by a health check.
- After the agent is updated, the next health check and the waiting job pick it
  up again without restarting the worker. Nothing updates or restarts agents
  automatically.

Bump `AGENT_VERSION` only for changes that need agents and workers updated
together (the HTTP or encode contract).

The service bar also names each agent's problem: no token set, token rejected
(the saved token must match `TRANSCODE_TOKEN`), unreachable, or needs an update.

## API

Every request uses `Authorization: Bearer TOKEN`.
`GET /health` returns `encoders`, `busy`, `free_bytes`, `agent_version` and `build_revision`.
`POST /jobs` accepts the file body with `Content-Length`, `X-Sha256`, and
`X-Video-Kbps`. `X-Max-Audio` describes the worker's computed audio budget;
audio encoding itself follows the stream rules below. A hash mismatch returns
422, a busy agent 409, and insufficient disk space 507. Success returns 201
with `job_id`. Poll `GET /jobs/{id}` for state, progress (0–100), encoder and
error. `GET /jobs/{id}/result` streams MKV and its `X-Sha256`.
`DELETE /jobs/{id}` cancels/drains encoding and deletes the job. Unknown IDs
return 404, including after an agent restart.

The encoder preserves stream order, subtitles, attachments, metadata and flags.
Lossy AAC/Opus/AC3/E-AC3/MP3 audio is copied; other audio becomes Opus at
128 kbps for stereo or 256 kbps for multichannel. Video becomes HEVC Main10:
VAAPI bitrate mode, falling back to two-pass libx265 medium only when allowed.
Output duration and audio/subtitle/attachment counts are validated.

## Proxmox LXC

Pass `/dev/dri/renderD128` through just as in the existing Ollama LXC: allow
its device major/minor in the container's cgroup2 device rules, bind-mount the
render node, and map the host render group's GID so `transcode` can access it.
The exact GID and device minor come from the host (`ls -ln /dev/dri`). Both
containers share the GPU. Ollama with keep-alive uses 10–11 GB of the 12 GB;
configure `gpu,cpu` so allocation failures fall back to CPU. Give the LXC all
cores: LXC cores are shared, not reserved.

For a gaming VM taking exclusive GPU ownership, install a Proxmox hookscript
that stops both the transcoder and Ollama LXCs in `pre-start` and starts them
in `post-stop`. Replace IDs with the actual container IDs:

```sh
#!/bin/sh
set -eu
case "$2" in
  pre-start) pct stop TRANSCODER_ID; pct stop OLLAMA_ID ;;
  post-stop) pct start OLLAMA_ID; pct start TRANSCODER_ID ;;
esac
```

If both services share one LXC, stop/start that container once. Attach the
script to the gaming VM using Proxmox's `hookscript` option on snippet-enabled
storage. While the containers are stopped, failed health checks make the worker
skip those agents. Verify host GPU binding with your existing passthrough setup.

On a Linux gaming PC allow `gpu` only and disable sleep. Wake-on-LAN is outside
this work. Do not use Windows agents.

## AniKeepOrNot settings and staged batches

Settings → Transcode defaults to Off, preserving ordinary verification/staging.
Local uses the same encoder and has its own temp path, device and encoder
allowlist. Remote uses the displayed agent order; Add, Up and Down edit that
order. Each agent has an Address and a Token field. Generate creates a random
token and shows it as a `TRANSCODE_TOKEN=...` line to copy; give it to the agent
with `adm-agent.pyz agent token --set`. Or paste the token the agent
shows with `adm-agent.pyz agent token`. Save
changes stores the agent list and its tokens together. Tokens are kept in the
existing private secrets store and are never returned by the API. Target size defaults to decimal 500 MB per
24 minutes, scaled by duration. Audio is budgeted before video and 1% is
reserved for container overhead. Videos already under target are unchanged. Private transcoded outputs use the
existing Publisher move path within staging, avoiding a second full video copy;
the torrent source and its normal post-publication cleanup remain unchanged.
The worker spends at most three encode attempts per file; Retry resets that
budget. Unavailable, busy, and disk-full agents cause a wait. Agent status
appears in the existing service bar only in Remote mode.

New installs use the current schemas. Existing installs require the normal
explicit settings upgrade to v17 and database migrations 072/073 under
maintenance. These changes do not deploy or migrate a running installation.

`adm transcode staged [--series ANIDB_ID] [--dry-run] [--yes]` queues a
one-way batch and exits. Without `--yes` it asks for confirmation. Dry run only
lists videos over the current target and estimates savings. A second batch is
refused while one is active. `adm transcode status` and the Jobs page show the
same counters. The ordinary worker runs the batch in the background, reusing
the same agents/slots, with pipeline work taking priority. The batch's single
row stores a JSON progress list and rename receipts to recover across restarts;
it creates no per-file jobs or job-history rows. Each result is published by
an atomic rename, then the staging manifest, receipt, cleanup file identity,
and resource size are updated together. MP4 sources become MKV paths, retaining
their episode NFO. Keep/Reject work prevents replacement of affected files.
A completed batch sends one bell notification with a link to Jobs and failed
file names. Lower the target and queue another batch to shrink staged files
again. Originals are not recoverable.
