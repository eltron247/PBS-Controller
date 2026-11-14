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
- `POST /api/stop`: requires `{"confirm":true}` and stops only that unit.
- `GET /api/log?offset=N&file_id=ID`: bounded 64-KiB read with byte offset; detects rotation/truncation and resumes after reconnect.
- `GET /api/log/download`: latest controller run's log segment at time of request.
- `GET /api/history`: SQLite controller-initiated run history.

Every `/api/*` endpoint requires a bearer token. There is no arbitrary shell execution API. The frontend uses `textContent` for server-provided strings rather than rendering HTML. No permissive CORS policy.

## Recovery behavior

The backup runs under a root systemd unit and survives controller/browser/SSH disconnections. On reconnect the backend reads the actual systemd state and stored run metadata; the client polls current log bytes every second, approximating `tail -f` without an always-open WebSocket. A different client can attach while the same backup is running.

The controller cannot detect a legacy `nohup` run that bypasses systemd. **All new starts must use the same systemd unit** for reliable duplicate protection. The unit-local `flock` invocation is not a lock shared with legacy nohup calls.

## V1 limitations

- No fake progress percentages, throughput or ETA; if the existing script does not log progress, only text and elapsed time are available.
- Only controller-created run history is stored. Manually started systemd runs appear in current state and logs but are not inserted into history.
- `completed` means exit code 0; actual PBS datastore verification remains separate.
- No automatic schedule and no Windows MSI installer yet. GitHub Actions supplies a standalone EXE artifact.
- This is a job controller, not a replacement for the native PBS management interface.
