#!/usr/bin/env bash
# Run as root on PBS. Does not launch the backup job.
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/install-pbs.sh" >&2
  exit 1
fi

SRC="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
APP=/opt/pbs-controller
ENV_DIR=/etc/pbs-controller
STATE=/var/lib/pbs-controller
LOG=/var/log/run-pbs-job.log
BACKUP=/usr/local/sbin/truenas-backup.sh
UNIT_DIR=/etc/systemd/system
SUDOERS=/etc/sudoers.d/pbs-controller

for cmd in python3 sudo visudo systemctl flock; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Missing '$cmd'. On Debian/PBS install prerequisites, e.g.: apt-get install python3-venv sudo util-linux" >&2
    exit 1
  fi
done
if [[ ! -x "$BACKUP" ]]; then
  echo "Expected executable backup script at $BACKUP. No service files installed." >&2
  exit 1
fi
if [[ ! -e "$LOG" ]]; then
  install -m 0640 -o root -g root /dev/null "$LOG"
fi

if ! id -u pbs-controller >/dev/null 2>&1; then
  useradd --system --user-group --home-dir "$STATE" --shell /usr/sbin/nologin pbs-controller
fi
install -d -m 0755 -o root -g root "$APP"
install -d -m 0750 -o root -g pbs-controller "$ENV_DIR"
install -d -m 0750 -o pbs-controller -g pbs-controller "$STATE"
# Preserve prior log; grant read permission to controller group.
chgrp pbs-controller "$LOG"
chmod 0640 "$LOG"

if [[ ! -e "$ENV_DIR/controller.env" ]]; then
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(36))')"
  printf 'PBS_CONTROLLER_TOKEN=%s\n' "$TOKEN" > "$ENV_DIR/controller.env"
  chown root:pbs-controller "$ENV_DIR/controller.env"
  chmod 0640 "$ENV_DIR/controller.env"
  echo
  echo "Controller access token (save this securely):"
  echo "$TOKEN"
  echo
else
  echo "Preserving existing controller token at $ENV_DIR/controller.env"
fi
chown root:pbs-controller "$ENV_DIR/controller.env"
chmod 0640 "$ENV_DIR/controller.env"

# Replace code completely, dropping stale files and checkout ownership/modes.
rm -rf -- "$APP/controller" "$APP/web"
for tree in controller web; do
  install -d -m 0755 -o root -g root "$APP/$tree"
  while IFS= read -r -d '' dir; do
    install -d -m 0755 -o root -g root "$APP/$tree/${dir#"$SRC/$tree/"}"
  done < <(find "$SRC/$tree" -mindepth 1 -type d -name __pycache__ -prune -o -type d -print0)
  while IFS= read -r -d '' file; do
    install -m 0644 -o root -g root "$file" "$APP/$tree/${file#"$SRC/$tree/"}"
  done < <(find "$SRC/$tree" -type d -name __pycache__ -prune -o -type f ! -name '*.pyc' ! -name '*.pyo' -print0)
done
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install --disable-pip-version-check -r "$SRC/server-requirements.txt"

install -m 0644 "$SRC/deploy/pbs-controller.service" "$UNIT_DIR/pbs-controller.service"
install -m 0644 "$SRC/deploy/pbs-truenas-backup.service" "$UNIT_DIR/pbs-truenas-backup.service"

# Limit the unprivileged API to these exact two root commands. No shell wildcard.
cat > "$SUDOERS" <<'EOF'
pbs-controller ALL=(root) NOPASSWD: /usr/bin/systemctl start --no-block pbs-truenas-backup.service, /usr/bin/systemctl stop --no-block pbs-truenas-backup.service
EOF
chmod 0440 "$SUDOERS"
if ! visudo -cf "$SUDOERS"; then
  rm -f "$SUDOERS"
  echo "Invalid sudoers configuration; removed file" >&2
  exit 1
fi

systemctl daemon-reload
systemctl enable pbs-controller.service
systemctl restart pbs-controller.service
echo
echo "Installed. Controller listens only at http://127.0.0.1:8765 on PBS."
echo "No backup has been started."
echo "Check: systemctl status pbs-controller.service"
echo "For web access from a workstation: ssh -N -T -L 8765:127.0.0.1:8765 pbs-tunnel@pbs"
if [[ -z "${TOKEN:-}" ]]; then
  echo "To view the existing token as root: cat $ENV_DIR/controller.env"
fi
