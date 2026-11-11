# Work items: ADM server agent distributed as one .pyz

Status: proposed implementation plan, revised to use ADM and zipapp. These commands are proposed; they are not implemented yet. No running installation is changed by this document.

## Outcome and proposed operator flow

The agent is an ADM command: `adm server agent`. Keep the existing encoder and HTTP modules maintainable in the repository; package the required code into one executable `adm-agent.pyz` on demand. A single deployed archive replaces the earlier proposal to combine every function into one Python source file.

On the AniKeepOrNot/build machine:

```sh
adm server agent pack --output ./adm-agent.pyz
```

On the agent machine, after transferring/downloading that artifact:

```sh
sudo ./adm-agent.pyz server agent install
```

The install command asks for the required settings, installs the archive and a systemd service, checks authenticated health and explains dashboard pairing. The normal service is managed with:

```sh
sudo systemctl status transcode-agent
sudo journalctl -u transcode-agent
```

For installation without root, support:

```sh
./adm-agent.pyz server agent install
systemctl --user status transcode-agent
journalctl --user -u transcode-agent
```

Installation scope is automatic: effective UID 0 selects a system service; any other effective UID selects that user's service. There is no installer `--user` option. Before changes, print the resolved account and scope, for example "Installing as alice: user service (systemctl --user)" or "Installing as root: system service (runs as transcode)".

User mode installs into the current user's directories and runs under their identity. It never uses sudo, creates a system account or modifies system units. The system installation remains available for dedicated agent hosts.

Systemd internally launches the installed archive with `server agent`, which runs the HTTP listener. The operator does not manually run Python or manage source files. A suitable host Python interpreter, FFmpeg/FFprobe and systemd are still required: zipapp packages Python code, not those host programs.

The intake continues importing the existing encoder module directly for local encoding and received-result validation. The archive includes that same module unchanged. There is one maintained encoder implementation; different installations still have explicit versions and need normal updates.

The worker checks each remote agent's reported version on startup, on normal health refreshes and before submitting a new video. An incompatible agent is not used; the dashboard says which agent needs updating and which version is required. Version checking does not automatically update or restart remote machines.

## Existing code to reuse

- `bin/adm` and `src/anime/api/adm.py`: add the `server agent` command family while preserving existing commands and lazy imports.
- `src/transcode_agent/{encode,server,__main__}.py`: reuse the existing encoder, HTTP service and startup/configuration behavior. Do not concatenate or rewrite them just to obtain one downloadable file.
- `bin/anime-discovery-install`: reuse applicable dependency, installation, download/retry, permissions and recovery behavior. Avoid a second independent installation algorithm. Extract narrowly scoped shared helpers only where necessary.
- `deploy/transcode-agent/transcode-agent.service` and `.env.example`: update the existing unit and configuration examples for the archive entry point.
- `src/anime/services/transcode.py`: preserve local/remote selection, HTTP client, persisted attempt budget and shared validation.
- Settings -> Transcode -> Remote already has per-agent addresses, masked token entry, save-token and configured-token state. Reuse its credentials API and secrets store.

## Rules

- Branch from and target `develop`; keep `main` for releases. Include `[skip ci]` in commits and PR descriptions. No GitHub CI runs or new CI packaging requirement.
- Use Python's standard-library `zipapp` to build on demand, with an executable Python shebang and a small root `__main__.py` entry point. No PyInstaller, pip build environment or new packaging dependency.
- One deployable agent archive does not mean including the entire application. Include the ADM agent entry point, agent modules and only their required shared helpers/resources.
- Importing encoder/command modules must not start a listener, create/wipe a workspace or launch FFmpeg. The ADM agent command must not require the main API or its Unix admin socket to be running.
- Agent-only installations do not install a dashboard, main application worker, database, migrations, Sonarr or media integrations. No full repository checkout or Git is required on the target machine.
- Build from an explicit installed/source revision, recording that revision in the archive. Exclude credentials, environment files, databases, media, .git, caches and test data using an allowlist.
- Production installation/restarts are not authorized by this plan. Use temporary paths, fake service/package commands and disposable test instances.
- Run the full local test batch once implementation is finished; fix failures and rerun affected checks as necessary. Preserve optional VAAPI hardware coverage and skip it when unavailable.

| Order | Item | Depends on |
|---|---|---|
| 0 | Existing output-validation bug | Bug item already added by the user |
| 1 | ADM server agent command | None |
| 1a | Agent version reporting and worker compatibility gate | 1 |
| 2 | On-demand minimal .pyz export | 1, 1a |
| 3 | Installation, systemd recovery and safe updates | 2 |
| 4 | Clear artifact delivery, dashboard pairing and guide | 2, 3 |
| 5 | End-to-end acceptance and final local batch | All implementation items |

## 0. Existing bug: require valid video in the result

Link to the bug work item already added rather than duplicate its implementation. The validator currently accepts an audio-only MKV for a source containing video.

Acceptance:

- Reject output missing required video, and validate intended HEVC/Main10 properties as appropriate to the existing encoder contract.
- Preserve duration, audio, subtitle and attachment checks.
- Test an actual audio-only MKV as a negative case. Local encoding, remote agent and receiving intake use the same corrected validator.

## 1. ADM server agent command

Acceptance:

- `adm server agent` runs the existing authenticated HTTP agent using its environment configuration. Refactor the existing startup function for reuse rather than maintain two server launchers.
- `adm server agent --help` documents configuration, host dependencies and the service-managed workflow; existing ADM commands remain compatible.
- Keep direct imports of encoder/probe/validation functions for Local mode and intake validation. Agent execution has no main-application database, configuration or admin-socket requirement.
- Preserve the HTTP routes, one active encode, progress, GPU-first/CPU fallback, workspace ownership and cleanup contract.
- Include explicit version/revision output so the installed agent can be compared with the intake/build version.

## 1a. Versioned agents and worker update-required checks

Acceptance:

- Define one canonical, monotonically increasing `AGENT_VERSION` for agent compatibility. Bump it when an agent change requires deployed agents/workers to be updated together; unrelated dashboard/application changes do not bump it. Keep the source/build revision separately for diagnostics.
- Include `agent_version` and `build_revision` in the existing authenticated `GET /health` response. CLI version output, archive metadata and health must agree; do not infer compatibility from Git SHA equality.
- The worker uses its bundled `AGENT_VERSION` as the required agent version. Start with an exact compatibility match to keep the rule simple: equal versions can run; older agents need an agent update; newer agents need a worker update. No capability-negotiation framework is needed.
- Check compatibility on worker startup/first agent use, in the existing periodic health check, and immediately before a new upload. A worker upgrade therefore checks existing agents again without relying on old cached health.
- An older agent, including the current implementation that reports no version, is excluded from new work and shown as needing an update. Authentication/network failures retain their existing separate explanations. Malformed version metadata fails closed with an actionable diagnostic.
- Reuse existing service health/status and dashboard service-bar/Remote-agent controls. Show a sentence such as: "Agent pc:8787 is version 1; this worker requires version 2. Update this agent." For an agent newer than the worker, explain that the worker needs updating instead.
- Surface a persistent configured-agent update indication using existing status/notification facilities; do not hide a reachable but incompatible agent as simply offline. Do not emit a new notification every poll.
- Other compatible agents continue receiving work in configured order. If none are compatible/available, the job waits with an update-required explanation; version refusal must not consume an encode attempt or become a generic failed transcode.
- An incompatibility discovered during health refresh does not kill an already accepted encode. Use the existing result validation/recovery rules for work in progress and block subsequent submissions.
- After an operator upgrades the agent and its health reports the required version, the existing health refresh clears the update-required state and resumes eligible queued work without a worker restart.
- No automatic agent deployment, token rotation or service restart follows from checking versions. The operator uses the explicit archive upgrade workflow.

## 2. Build a minimal executable archive on demand

Acceptance:

- `adm server agent pack --output PATH` builds the archive from the current selected installation/source revision using `zipapp`; no network fetch or CI run is necessary to create it.
- The root entry point routes the archive's `server agent` commands to the same command handlers used by normal ADM. Do not duplicate the parser/business logic in generated bootstrap code.
- Copy the canonical agent modules into the archive without rewriting or combining their implementations. Include only required ADM dispatch/helpers plus the maintained service/environment templates needed for installation.
- The archive's help is appropriate to the included command set. Unsupported main-application commands fail clearly rather than attempting missing imports or asking for an application database.
- `./adm-agent.pyz server agent` works outside the repository with only host Python and FFmpeg/FFprobe. Set executable permissions and a documented interpreter/minimum Python version.
- Package metadata records `AGENT_VERSION`, source revision and artifact contents/version; provide an integrity digest for transfer/update verification. Do not claim a digest from the same untrusted source authenticates an artifact.
- Test the archive inventory to prove secrets, configuration, database files and unnecessary application/test modules are absent. Do not ZIP the whole working directory.

## 3. Archive installation, service recovery and safe updates

Acceptance:

- `sudo ./adm-agent.pyz server agent install` works without ADM or AniKeepOrNot already installed. Reuse appropriate existing installer code through narrowly scoped helpers; keep one agent installation implementation.
- Check Python, systemd, FFmpeg/FFprobe and selected encoders first. Offer explicit dependency installation on supported Debian/Ubuntu hosts and actionable missing-dependency messages elsewhere.
- Ask for port/listen address, CPU/GPU choice, GPU device when relevant and temporary parent. Support unattended configuration without putting tokens in process arguments.
- Generate a strong initial token unless supplied securely; preserve it and all existing configuration on reruns. Expose it only through an explicit operator-facing pairing step, never routine logs/diagnostics.
- Detect scope from the effective process UID: root installs a system service; non-root installs a user service. Do not add an installer `--user` or `--system` switch, prompt the operator to choose scope, or automatically elevate a non-root installation.
- Resolve the actual account using the operating system's effective UID/account database, not the untrusted `$USER` environment variable. `sudo`/root execution selects system installation even if `$USER`, `$HOME` or `SUDO_USER` refers to the original login. For non-root installation, use the resolved account's home as the fallback for user paths, respecting valid XDG directory settings.
- Before making changes, announce the resolved installer account, system/user service scope, runtime account and destination paths. User-facing examples: "Installing as alice: user service (systemctl --user)"; "Installing as root: system service (runs as transcode)".
- Persist the detected scope/owner and use it consistently for status, health, upgrades, rollback and service control. Do not silently fall back between user and system services.
- System mode creates the dedicated unprivileged account and GPU permissions, installs the archive/protected environment file and system unit, then enables/starts it and verifies authenticated local health.
- User mode requires no root or sudo. Install the archive under `$XDG_DATA_HOME/anikeepornot/transcode-agent` (default `~/.local/share/anikeepornot/transcode-agent`), protected environment/configuration under `$XDG_CONFIG_HOME/anikeepornot/transcode-agent` (default `~/.config/anikeepornot/transcode-agent`), and the unit under `$XDG_CONFIG_HOME/systemd/user/transcode-agent.service` (default `~/.config/systemd/user/transcode-agent.service`). Keep previous-version/update state inside this user's agent installation.
- Generate the unit from the same maintained template with scope-appropriate settings. User units omit system-only `User=`, `Group=` and `SupplementaryGroups=` directives and use `WantedBy=default.target`; system units retain their dedicated account/system boot target. Resolve configured paths correctly rather than assume shell environment expansion inside systemd unit fields.
- User mode runs `systemctl --user daemon-reload`, `systemctl --user enable --now transcode-agent.service` and an authenticated health check. Detect a missing user manager/session bus and report how to run installation from a valid user session; do not report success when service startup cannot run.
- Explain that enabling a user unit starts it with the user manager; operation at machine boot and after logout additionally requires user lingering where not already enabled. Report the current linger state and provide `loginctl enable-linger USER` as an explicit administrator/policy-dependent setup step. Do not silently change linger settings or promise boot persistence without verifying them.
- User mode uses the invoking user's existing GPU permissions. Check render-device access and explain any administrator action needed for group/device access; do not silently modify system groups, udev rules or device permissions. Explicit CPU-only operation remains available.
- User mode checks host dependencies but does not invoke privileged package installation. Give actionable administrator dependency-install instructions if Python/FFmpeg/VAAPI support is missing.
- Apply the same automatic identity/scope detection to agent upgrade/rollback and use the appropriate systemctl commands. Verify the installed scope/owner before changes; if an existing artifact belongs to the other scope, explain which account must run the command rather than migrate scope or guess. Account for configured port/workspace collisions if both service scopes exist.
- The unit launches the archive with `server agent`; configure `Restart=on-failure`, a restart delay and an explicit start-limit policy matching the intended crash-recovery behavior.
- Stopping/restarting the service drains or terminates FFmpeg children. No child survives a stop/crash unnoticed.
- After a crash/restart, old job IDs are unknown under the existing contract. Intake reconciles and retries within its existing persisted budget; do not promise in-place encode continuation.
- Provide `server agent upgrade --from PATH` for a newly delivered archive and `server agent rollback`, using the same installation helpers. Agent version/configuration state remains separate from the main application installation.
- Validate replacement code/revision before stopping; preserve token/settings/permissions, use atomic installation and retain the previous working archive.
- Planned upgrades refuse/wait on an active encode unless an explicit interruption option is used. On failed restart/health verification restore the prior archive and report recovery outcome.
- Interrupted downloads/installation cannot destroy the working version or silently rotate credentials. Do not report successful installation/upgrade until authenticated health succeeds.

## 4. Artifact delivery, pairing and complete operator guide

Acceptance:

- Document the complete path from `adm server agent pack` to the target machine: a concrete file-transfer example, or an explicit versioned download location when published. Do not assume the archive appears on the agent machine automatically.
- Distribution stays explicit: on-demand export is supported without creating a release or triggering GitHub CI. Automated SSH deployment, an embedded dashboard HTTP file server and fleet management are outside this item.
- Settings -> Transcode -> Remote clearly shows adding an agent address and entering/saving its matching token, with configured/not-configured state.
- The guide explains that the saved dashboard token must match the agent's `TRANSCODE_TOKEN` for that address. Reopening Settings does not expose the saved secret in plaintext.
- Reuse the service bar for availability/authentication failure and version/update-required feedback, using actionable sentences that include installed/required agent versions where applicable.
- Cover export, transfer, system and user installation, pairing, healthy operation, a test video, scope-correct service status/logs, reboot/crash recovery, user lingering, export of a new version, upgrade and rollback.
- Keep the systemd unit and environment example maintained in the repository and included in the artifact; avoid separately maintained embedded template copies.
- State the reachable network port and trusted-network/HTTPS requirement for bearer tokens. No automatic firewall/router changes.

## 5. End-to-end acceptance and final local verification

Acceptance:

- Build and run the archive from a directory outside the repository with repository paths removed from the import environment and no third-party Python packages.
- Verify agent command/help/version parity between normal ADM and archive execution, plus clear handling of unsupported commands.
- Exercise authenticated loopback HTTP upload, progress, result checksum/download, shared validation and deletion with synthetic media.
- Test matching, older, newer, missing and malformed agent versions; rechecking after a worker update; no upload/attempt consumed on mismatch; failover to a compatible agent; waiting when all require updates; and automatic eligibility recovery after an agent upgrade. Check that repeated polls do not flood notifications and existing accepted work is not canceled by a health refresh.
- Disposable systemd-enabled environments verify system and user enablement, deliberate crash/restart, child cleanup, interrupted-job recovery and failed-update rollback. For user mode, verify current-user execution, XDG/default paths, scope-correct unit directives, missing user-manager errors, linger messaging and logout/boot persistence when lingering is available. Verify automatic selection for root and non-root, misleading `$USER`/sudo environment values, account/scope announcement before changes, upgrade scope/owner checks and absence of installer scope flags. Prove user installation invokes no sudo/system-account/system-service mutations. Distinguish actual systemd verification from mocked service-command tests.
- Installer tests cover fresh setup, rerun, preserved token/configuration, missing dependencies, interrupted transfer/install, unhealthy startup, upgrade and rollback using temporary destinations/fake system commands.
- Reuse dashboard credentials fixtures for pairing and masked-token behavior. Preserve optional real VAAPI testing and skip without usable hardware.
- Run `PYTHONPATH=. pytest src/anime/tests/` and `node --test src/anime/tests/dashboard_browser.test.cjs` after implementation is complete.
- Record tested platform, source/artifact revision, archive inventory, resulting installed paths and validation results. Report unavailable systemd/GPU checks explicitly; production deployment is not needed for code/PR completion.

## Packaging reference

Python standard-library zipapp: https://docs.python.org/3/library/zipapp.html
