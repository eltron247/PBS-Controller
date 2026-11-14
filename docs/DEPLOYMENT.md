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

This places the server under `/opt/pbs-controller`, secret under `/etc/pbs-controller/controller.env`, service units under `/etc/systemd/system`, and run history at `/var/lib/pbs-controller/history.sqlite3`. The installer preserves `/var/log/run-pbs-job.log`, gives the controller group read access, and enables the API service. **It does not start a backup.** The token is printed only for first-time install; retrieve it later as root from the environment file.

Check API before any backup:

```sh
systemctl status pbs-controller.service
curl -fsS http://127.0.0.1:8765/healthz
journalctl -u pbs-controller.service -n 100 --no-pager
```

## Windows and browser access

The desktop app connects via host-key-checked Windows OpenSSH and renders the same UI using WebView2. First run asks for host/user/key path and saves `%APPDATA%\\PBS-Controller\\config.json`. Enter the token inside the UI. If connection fails, first connect normally using `ssh root@pbs` to verify the fingerprint and SSH key.

For browsers on another computer, create a tunnel:

```sh
ssh -N -L 8765:127.0.0.1:8765 root@pbs
```

Open `http://127.0.0.1:8765`. For phones or permanent LAN use, put an authenticated HTTPS reverse proxy in front of loopback. Do not expose an unencrypted bearer-token API to the LAN/WAN.

## First backup validation

1. Confirm the old nohup job has finished.
2. Start the backup from the GUI and watch output appear in `/var/log/run-pbs-job.log`.
3. Close the GUI, reopen it, and verify the current run and console resume.
4. Confirm status and exit code: `systemctl show pbs-truenas-backup.service -p ActiveState -p Result -p ExecMainStatus`.
5. Independently verify the snapshot in PBS. Process exit 0 alone is not archival integrity verification.

## Operations

- Backend logs: `journalctl -u pbs-controller.service -f`
- Transfer log: `tail -f /var/log/run-pbs-job.log`
- Manual start: `systemctl start --no-block pbs-truenas-backup.service`
- Manual stop: `systemctl stop pbs-truenas-backup.service` (interrupts transfer)
- Rotate token by replacing `PBS_CONTROLLER_TOKEN` with a fresh 32+-character random token in `/etc/pbs-controller/controller.env`, then restarting `pbs-controller.service`.
- Update source via `git pull` and rerun the installer. Existing controller token and SQLite history are preserved.
- If configuring logrotate, ensure the new log file is readable by group `pbs-controller` (`create 0640 root pbs-controller`). Verify rotation behavior with a test run.
- To disable GUI without changing the backup script: `systemctl disable --now pbs-controller.service`.
