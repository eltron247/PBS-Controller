# Architecture and trust boundaries

```text
Windows WebView2 ── OpenSSH local port forward ─────┐
                                                   ▼
Browser ── SSH tunnel or authenticated TLS proxy ── 127.0.0.1:8765 on PBS
                                                     FastAPI (pbs-controller user)
                                                     token-authenticated REST
                                                        ├─ read log
                                                        ├─ SQLite run history
                                                        └─ sudo exact systemctl start/stop
                                                                    │
                                                                    ▼
                                                        systemd root service
                                                                    │
                                                            truenas-backup.sh
                                                                    │
                                                           TrueNAS → PBS transfer
```

No privileged process runs on Windows. The GUI is a WebView2 shell around the same web UI served on PBS; there is one frontend codebase. The API binds to loopback. Simply loading the UI never starts a backup.

## API

- `GET /api/status`: current systemd state, runtime/exit status, and latest controller run.
- `POST /api/start`: starts only `pbs-truenas-backup.service`. Conflict on an active unit or an in-flight controller start.
- `POST /api/stop`: requires `{"confirm":true}`, records `stopping`, and queues a nonblocking stop of only that unit. Systemd's 45-second stop timeout runs independently of the HTTP request. A rejected command restores the previous history state and returns an error; an accepted stop ultimately records `interrupted`.
- `GET /api/log?offset=N&file_id=ID`: bounded 64-KiB read with byte offset; detects rotation/truncation and resumes after reconnect.
- `GET /api/log/download`: latest controller run's log segment at time of request.
- `GET /api/history`: SQLite controller-initiated run history.

Every `/api/*` endpoint requires a bearer token, compared as UTF-8 bytes so malformed/non-ASCII credentials return 401. There is no arbitrary shell execution API. The only privileged argv are `/usr/bin/sudo -n /usr/bin/systemctl start --no-block pbs-truenas-backup.service` and `/usr/bin/sudo -n /usr/bin/systemctl stop --no-block pbs-truenas-backup.service`.

The frontend uses `textContent` for server-provided strings rather than rendering HTML. Console chunks are appended as text nodes and trimmed from the front above 500,000 characters, retaining an omission marker and auto-follow behavior. No permissive CORS policy. All HTTP responses carry a strict same-origin CSP (scripts and styles are external), `X-Content-Type-Options: nosniff`, and `Referrer-Policy: no-referrer`.

## Recovery behavior

The backup runs under a root systemd unit and survives controller/browser/SSH disconnections. On reconnect the backend reads the actual systemd state and stored run metadata; the client polls current log bytes every second, approximating `tail -f` without an always-open WebSocket. A different client can attach while the same backup is running.

A daemon reconciliation thread starts in FastAPI lifespan, runs once per second independently of client polls, retries read failures, and is joined on shutdown. It only reads systemd and updates history. SQLite connections commit/roll back and close after each operation. Existing history is migrated using guarded `ALTER TABLE ... ADD COLUMN` statements.

Each row records the observed `InvocationID` and `ExecMainStartTimestamp`. An unbound row only accepts an invocation whose start is at or after its request time and within the named 60-second startup grace. Retained timestamps/results from earlier runs cannot complete a new row. Never-activated rows fail with "Unit did not start" at the grace deadline. Bound rows require the same start timestamp and, when available, the same invocation ID; the start timestamp remains usable if systemd clears the ID on inactivity. Replaced/lost invocation metadata produces `failed` with an unavailable-outcome message and no invented finish time or exit code. Systemd retains only its latest invocation, so a controller outage or a very fast manual restart can lose the original outcome; unobserved concurrent CLI/controller starts cannot be distinguished perfectly. Use one start path at a time.

`finished_at` uses the matching invocation's `ExecMainExitTimestamp`, never the next poll time. Realtime timestamps use the microsecond UTC format from [`systemctl --timestamp=us+utc`](https://www.freedesktop.org/software/systemd/man/252/systemctl.html#--timestamp=). `ExecMainCode=1` exposes `exit_code`; codes 2/3 expose `signal` and `signal_name` (for example, SIGTERM), with `exit_code` null. Both the status card and history render this distinction.

The Windows wrapper drains SSH stderr continuously into a bounded 64-KiB tail. A supervisor retries an exited tunnel on the same local port with exponential backoff (1–30 seconds), resetting after a stable connection. Shutdown closes the tunnel and worker. Config prompts and saved settings share host/user/port validation, and SSH receives `--` before its destination. Prefer a restricted tunnel-only account; root configurations remain supported.

The controller cannot detect a legacy `nohup` run that bypasses systemd. **All new starts must use the same systemd unit** for reliable duplicate protection. The unit-local `flock` invocation is not a lock shared with legacy nohup calls.

## V1 limitations

- No fake progress percentages, throughput or ETA; if the existing script does not log progress, only text and elapsed time are available.
- Only controller-created run history is stored. Manually started systemd runs appear in current state and logs but are not inserted into history.
- `completed` means exit code 0; actual PBS datastore verification remains separate.
- No automatic schedule and no Windows MSI installer yet. GitHub Actions supplies a standalone EXE artifact.
- This is a job controller, not a replacement for the native PBS management interface.
