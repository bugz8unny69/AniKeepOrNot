# Transcode agent

Linux only. The agent uses Python's standard library and host ffmpeg/ffprobe;
`src/transcode_agent` never imports AniKeepOrNot. The worker's Local mode
uses the same `encode.py` implementation. All traffic uses HTTP over TCP.

## Install

On Debian/Ubuntu install `python3 ffmpeg mesa-va-drivers`, create a system user
`transcode`, and copy `src/transcode_agent` under `/opt/transcode-agent/src/`.
Install `deploy/transcode-agent/transcode-agent.service` under
`/etc/systemd/system/` and its env example as `/etc/transcode-agent.env` (0600).
Set a long random `TRANSCODE_TOKEN`, shared with the worker. Enable the service
with `systemctl enable --now transcode-agent`. No Python packages are needed.
Check `ffmpeg -encoders` for `hevc_vaapi`, `libx265`, and `libopus`.

Environment settings:

| Variable | Default | Meaning |
|---|---|---|
| TRANSCODE_LISTEN | 0.0.0.0:8787 | Listening host:port |
| TRANSCODE_TOKEN | required | Bearer token on every route |
| TRANSCODE_ENCODERS | gpu,cpu | Allowed encoders; GPU always tried first |
| TRANSCODE_VAAPI_DEVICE | /dev/dri/renderD128 | VAAPI render node |
| TRANSCODE_TMP | /var/tmp | Parent of the agent's private work folder |

Only `<TRANSCODE_TMP>/transcode-agent/` is used and wiped at startup. Sibling
files survive, including when the configured parent is a home or data folder.
Use a separate temp parent for each agent/Local worker on the same host.
Uploads need at least 1.5 times their Content-Length free; insufficient space
returns 507 before the body is read. Finished jobs expire after one hour.
One encode runs at a time. Keep this service on a trusted LAN or use HTTPS
through a reverse proxy; a bearer token does not encrypt a plain HTTP transfer.

## Running the agent

`adm server agent` runs the agent in the foreground with the environment
settings below; `adm server agent --version` prints its agent version and build
revision, and `adm server agent --help` lists the settings. `python -m
src.transcode_agent` is the same command. Stopping it (SIGTERM/SIGINT) cancels
any encode, so no ffmpeg process outlives the agent.

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
token and shows the exact `TRANSCODE_TOKEN=...` line to put in
`/etc/transcode-agent.env` on that agent (then `systemctl restart
transcode-agent`); a token from the agent can also be pasted instead. Save
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
