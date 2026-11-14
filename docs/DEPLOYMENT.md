# Deployment and validation

## Prerequisites

- PBS/Debian with systemd and working executable `/usr/local/sbin/truenas-backup.sh`.
- Python 3 + venv, sudo and util-linux (`flock`): `apt-get install python3-venv sudo util-linux git`.
- Script credentials and mount paths accessible when run by root via systemd.
- Windows OpenSSH client with key/agent-based authentication, WebView2 Runtime for desktop.

## Handoff from nohup

The existing command is:

```sh
nohup /usr/local/sbin/truenas-backup.sh > /var/log/run-pbs-job.log 2>&1 & disown
tail -f /var/log/run-pbs-job.log
```

**Wait until the last legacy transfer completes.** Do not use `nohup` alongside unit-managed jobs. The installer does not stop existing jobs or change `truenas-backup.sh`.

## Install

```sh
git clone https://github.com/eltron247/PBS-Controller.git
cd PBS-Controller
sudo bash deploy/install-pbs.sh
```

This places the server under `/opt/pbs-controller`, secret under `/etc/pbs-controller/controller.env`, service units under `/etc/systemd/system`, and run history at `/var/lib/pbs-controller/history.sqlite3`. Installed `controller` and `web` trees are replaced completely, excluding Python caches, with root:root ownership, directories 0755 and files 0644. The installer preserves `/var/log/run-pbs-job.log`, gives the controller group read access, enables and restarts the API service. **It does not start or stop a backup.** The token is printed only for first-time install; retrieve it later as root from the environment file.

The sudoers rule matches these exact two commands (including `--no-block` for stop):

```sudoers
pbs-controller ALL=(root) NOPASSWD: /usr/bin/systemctl start --no-block pbs-truenas-backup.service, /usr/bin/systemctl stop --no-block pbs-truenas-backup.service
```

Check API before any backup:

```sh
systemctl status pbs-controller.service
curl -fsS http://127.0.0.1:8765/healthz
journalctl -u pbs-controller.service -n 100 --no-pager
```

## Windows and browser access

The desktop app connects via host-key-checked Windows OpenSSH and renders the same UI using WebView2. First run asks for host/user/key path (default user `pbs-tunnel`) and saves `%APPDATA%\\PBS-Controller\\config.json`. Enter the token inside the UI. If connection fails, run the tunnel command below in a terminal to verify the key and host fingerprint against a trusted source. A tunnel-only key intentionally cannot open a remote shell. Existing configs using root continue to work.

For browsers on another computer, create a tunnel:

```sh
ssh -N -T -L 8765:127.0.0.1:8765 pbs-tunnel@pbs
```

Open `http://127.0.0.1:8765`. For phones or permanent LAN use, put an authenticated HTTPS reverse proxy in front of loopback. Do not expose an unencrypted bearer-token API to the LAN/WAN.

While the desktop window is open, SSH stderr is drained into a bounded memory tail. If SSH exits, the launcher retries on the same local port with 1–30 second backoff. The UI's polling reconnects when SSH and the controller return. Closing the window stops supervision and SSH, leaving the backup running.

## Tunnel-only SSH account

Have an administrator provision a dedicated account such as `pbs-tunnel` and a separate SSH key. Do not give this account sudo privileges or membership in `pbs-controller`; obtain the API token separately through an administrator. The installer deliberately does not create SSH users. Put the dedicated public key in that account's `~/.ssh/authorized_keys` with these options on the **same line**:

```text
restrict,port-forwarding,permitopen="127.0.0.1:8765",command="/bin/false" ssh-ed25519 AAAA... workstation-pbs-tunnel
```

Use directory mode 0700 for `.ssh`, file mode 0600 for `authorized_keys`, and account ownership. The forced command denies shell/command sessions; `ssh -N -T` uses forwarding without a session. To restrict the entire account to local forwarding (including other keys), add an appropriate `Match User pbs-tunnel` block to sshd configuration with `AllowTcpForwarding local`, `PermitOpen 127.0.0.1:8765`, `AllowAgentForwarding no`, `X11Forwarding no`, `PermitTTY no`, and `ForceCommand /bin/false`. Validate sshd configuration before reloading it. Confirm a tunnel works and shell access is rejected. Root SSH remains supported where explicitly configured, but is not the default or recommended tunnel identity.

## First backup validation

1. Confirm the old nohup job has finished.
2. Start the backup from the GUI and watch output appear in `/var/log/run-pbs-job.log`.
3. Close the GUI, reopen it, and verify the current run and console resume.
4. Confirm status and exit code/signal: `systemctl show pbs-truenas-backup.service -p ActiveState -p Result -p ExecMainCode -p ExecMainStatus -p InvocationID -p ExecMainExitTimestamp`. `ExecMainCode=1` means an exit code; 2/3 mean a signal number. The UI displays signals such as SIGTERM separately.
5. Independently verify the snapshot in PBS. Process exit 0 alone is not archival integrity verification.

## Operations

- Backend logs: `journalctl -u pbs-controller.service -f`
- Transfer log: `tail -f /var/log/run-pbs-job.log`
- Manual start: `systemctl start --no-block pbs-truenas-backup.service`
- Manual stop: `systemctl stop --no-block pbs-truenas-backup.service` (queues a stop that interrupts transfer). GUI stop records `stopping` before queuing it, then `interrupted` when the unit ends. Systemd allows up to 45 seconds before killing remaining processes; the HTTP request does not wait for that timeout.
- Rotate token by replacing `PBS_CONTROLLER_TOKEN` with a fresh 32+-character random token in `/etc/pbs-controller/controller.env`, then restarting `pbs-controller.service`.
- Update source via `git pull` and rerun `sudo bash deploy/install-pbs.sh`. This replaces stale code and executes `systemctl enable pbs-controller.service` then `systemctl restart pbs-controller.service`. Existing token and SQLite history are preserved with additive schema migration. Restarting the controller does not affect a running backup in the separate unit.
- If configuring logrotate, ensure the new log file is readable by group `pbs-controller` (`create 0640 root pbs-controller`). Verify rotation behavior with a test run.
- To disable GUI without changing the backup script: `systemctl disable --now pbs-controller.service`.

History is refreshed once per second while the controller is running, without requiring a browser. Rows track invocation identity/start time and use systemd's exit timestamp for completion. A start that never activates within 60 seconds is failed as "Unit did not start". If a later manual invocation replaces the tracked metadata during a controller outage or observation gap, history records an unavailable outcome with no guessed finish time/exit code. Avoid concurrent manual and controller start requests; history cannot reconstruct every invocation from systemd's latest-only metadata. Signals and exit codes are distinct in both status and history.
