# Transcode agent

Linux only. The agent uses Python's standard library and host ffmpeg/ffprobe;
`src/transcode_agent` never imports Anime Discovery. The worker's Local mode
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

## API

Every request uses `Authorization: Bearer TOKEN`.
`GET /health` returns `encoders`, `busy`, and `free_bytes`.
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

