# PBS Controller

A small, self-hosted controller for an existing TrueNAS → Proxmox Backup Server ingest script. Run the backup from a Windows desktop window **or** a web browser, and watch the same live console in the same window. The backup continues if either UI disconnects.

> **Status:** Initial implementation. Not installed on your PBS host; review and test the service integration before production use.

## What it does

- Starts `/usr/local/sbin/truenas-backup.sh` through a dedicated systemd service. **Does not modify the backup script or its transfer logic.**
- Shows running/idle/failed status, elapsed time, exit status, and real-time `/var/log/run-pbs-job.log`.
- Supports safe duplicate-start prevention, confirmed stop, reconnect, run history, and log download.
- Shared HTML/JavaScript interface for web and a Windows WebView2 desktop wrapper.
- Controller API requires an access token and listens on **127.0.0.1 only** by default.

## Install on PBS (Debian/Proxmox Backup Server)

**Do not install or start a second backup while your old nohup job is still running.** Wait for it to finish, confirm the existing backup works, and make a copy of the script/configuration.

```sh
git clone https://github.com/eltron247/PBS-Controller.git
cd PBS-Controller
sudo bash deploy/install-pbs.sh
```

The installer creates `pbs-controller` as an unprivileged service account, installs the Python backend, the backup systemd unit, a narrow sudoers rule for starting/stopping only that unit, and a random API token. **It never starts a backup during installation.** The token is in `/etc/pbs-controller/controller.env` (readable by root and the service group); the installer also displays it once.

Check service status:

```sh
systemctl status pbs-controller.service
systemctl status pbs-truenas-backup.service
```

## Browser

From a Windows terminal (with an SSH key configured):

```powershell
ssh -N -L 8765:127.0.0.1:8765 root@pbs
```

Then visit `http://127.0.0.1:8765`, enter the token from the installer, and use the GUI. The SSH process must stay connected while you use the browser. For access from a phone or elsewhere on the network, configure an **authenticated HTTPS reverse proxy** to `127.0.0.1:8765` on the PBS host. Do not bind the controller API directly to a public/network interface without TLS and access controls.

## Windows desktop application

```powershell
py -m pip install -r desktop/requirements.txt
py desktop/app.py
```

The first run creates `%APPDATA%\\PBS-Controller\\config.json` and opens an embedded window. Edit the config with your PBS SSH hostname, username, and optional identity key path. The desktop launcher uses Windows OpenSSH to build an SSH tunnel to the same localhost-only web service (and verifies SSH host keys normally); the remote API token is entered into the UI. Recommended: configure SSH-key authentication and open a regular SSH session once to accept/verify the host fingerprint.

To build an EXE on Windows:

```powershell
py -m pip install pyinstaller -r desktop/requirements.txt
py -m PyInstaller --onefile --windowed --name PBS-Controller desktop/app.py
```

GitHub Actions also builds a downloadable Windows EXE artifact on changes to `main`.

## CLI fallback

After installation, use **the same systemd unit** from SSH:

```sh
sudo systemctl start --no-block pbs-truenas-backup.service
tail -f /var/log/run-pbs-job.log
```

Using the old `nohup ... & disown` command *bypasses* systemd's duplicate-run protection. Do not run it alongside controller-managed jobs.

## Logs, history, and progress

The GUI streams appended console text, using byte offsets to resume after reconnect, and exposes server-observed run state and exit status. It **does not fabricate** percentage, throughput, or ETA. If the original script does not emit those details, the console is the authoritative progress source. Recent controller-initiated run history is stored in `/var/lib/pbs-controller/history.sqlite3`; it is not a history of every past backup.

The existing log file is preserved and appended to. If you configure log rotation, use `create 0640 root pbs-controller` and ensure the controller account can read new log files. Check log size periodically.

## Security / constraints

- Access token is a randomly generated bearer secret. Never paste it into an issue or commit it. It is stored only in browser session storage, not a URL.
- The FastAPI service runs unprivileged. Privileged actions are limited to two allowlisted systemctl commands; no arbitrary shell execution API exists.
- Service `KillMode=control-group` stops child processes on confirmed stop. This **can interrupt and invalidate an in-progress backup**; use only when necessary.
- A backup being marked `completed` means the script exited 0, **not** that PBS snapshot verification has run.
- Do not assume this guards against unrelated scripts invoking the same backup path outside systemd. Migrate all manual starts to the systemd unit.
- The systemd unit is configured to run as root with `HOME=/root`, matching the existing root-shell workflow; validate that the script's credentials and environment work as expected before relying on it.

See [architecture](docs/ARCHITECTURE.md) and [deployment notes](docs/DEPLOYMENT.md).
