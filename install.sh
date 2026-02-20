#!/usr/bin/env bash
set -euo pipefail

echo "=== Audio-Pi full installer ==="
echo "Installs: Web UI + Snapcast (multiroom) + AirPlay (Shairport) + Spotify Connect + DLNA renderer"
echo "Also configures Bluetooth to be discoverable on boot."
echo

# -----------------------
# Settings
# -----------------------
APP_USER="audio-pi"
APP_DIR="/opt/audio-pi"
CFG_DIR="/etc/audio-pi"
CFG_FILE="${CFG_DIR}/config.json"
SUDOERS_FILE="/etc/sudoers.d/audio-pi"
NGINX_SITE_AVAIL="/etc/nginx/sites-available/audio-pi"
NGINX_SITE_ENABLED="/etc/nginx/sites-enabled/audio-pi"
WEB_SERVICE="/etc/systemd/system/audio-pi-web.service"

REBOOT_REQUIRED=0

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "Please run as root: sudo $0"
    exit 1
  fi
}

log() { echo -e "\n\033[1;32m==>\033[0m $*"; }

# -----------------------
# 0) Preconditions
# -----------------------
require_root

# Determine script directory (repo root expected)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# -----------------------
# 1) Create service user
# -----------------------
if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  log "Creating service user: ${APP_USER}"
  useradd -r -m -s /usr/sbin/nologin "${APP_USER}"
fi

# -----------------------
# 2) Install packages
# -----------------------
log "Installing packages"
apt-get update -y

# Core + Web
apt-get install -y \
  git rsync curl ca-certificates jq \
  python3 python3-venv python3-pip \
  nginx \
  alsa-utils \
  mpd mpc \
  rfkill

# Audio features
apt-get install -y \
  snapserver snapclient \
  shairport-sync \
  bluetooth bluez bluez-tools || true

# Spotify Connect (optional)
apt-get install -y raspotify || true

# DLNA/UPnP renderer (Android casting)
apt-get install -y gmediarender || apt-get install -y gmrender-resurrect

# Wi-Fi config from UI (optional but requested)
apt-get install -y network-manager || true

# -----------------------
# 3) Enable services
# -----------------------
log "Enabling base services"
systemctl enable --now mpd || true
systemctl enable --now bluetooth || true
systemctl enable --now shairport-sync || true
systemctl enable --now snapserver || true
systemctl enable --now snapclient || true

# Spotify Connect (if installed)
systemctl enable --now raspotify 2>/dev/null || true

# DLNA renderer (if installed)
systemctl enable --now gmediarender 2>/dev/null || true
systemctl enable --now gmrender-resurrect 2>/dev/null || true

# Set friendly name for DLNA renderer if supported
log "Configuring DLNA friendly name (best effort)"
if [[ -f /etc/default/gmediarender ]]; then
  printf "GMRENDER_FRIENDLY_NAME=Audio-Pi\n" > /etc/default/gmediarender || true
  systemctl restart gmediarender 2>/dev/null || true
  systemctl restart gmrender-resurrect 2>/dev/null || true
fi

# -----------------------
# 4) Audio firmware config (DON'T disable HDMI)
# -----------------------
log "Ensuring onboard audio is enabled (safe default)"
CFG="/boot/firmware/config.txt"
touch "$CFG"

if ! grep -q '^dtparam=audio=on' "$CFG"; then
  echo 'dtparam=audio=on' >> "$CFG"
  REBOOT_REQUIRED=1
fi

# Remove any previous forced HDMI audio disable (if present from earlier experiments)
if grep -q '^hdmi_ignore_edid_audio=1' "$CFG"; then
  sed -i '/^hdmi_ignore_edid_audio=1/d' "$CFG" || true
  REBOOT_REQUIRED=1
fi

# -----------------------
# 5) Shairport -> stdout for Snapserver process source
# -----------------------
log "Configuring Shairport to output to stdout for Snapcast"
SHAIR="/etc/shairport-sync.conf"
if [[ -f "$SHAIR" ]]; then
  sed -i 's/output_backend *= *"alsa"/output_backend = "stdout"/' "$SHAIR" || true
  sed -i 's/output_backend *= *"pipe"/output_backend = "stdout"/' "$SHAIR" || true
  if ! grep -q 'output_backend' "$SHAIR"; then
    echo 'output_backend = "stdout";' >> "$SHAIR"
  fi
else
  echo "WARN: ${SHAIR} not found (package layout may differ)."
fi

# -----------------------
# 6) Snapserver stream definition (AirPlay)
# -----------------------
log "Configuring Snapserver stream"
SNAPCONF="/etc/snapserver.conf"
touch "$SNAPCONF"

# Remove existing [stream] blocks (keep other sections)
awk '
  BEGIN{skip=0}
  /^\[stream\]/{skip=1; next}
  /^\[.*\]/{if(skip==1){skip=0}}
  {if(skip==0) print}
' "$SNAPCONF" > "${SNAPCONF}.tmp"
mv "${SNAPCONF}.tmp" "$SNAPCONF"

cat <<'EOF' >> "$SNAPCONF"

[stream]
source = process:///usr/bin/shairport-sync?name=AirPlay&sampleformat=44100:16:2
EOF

# -----------------------
# 7) Bluetooth: discoverable/pairable on boot
# -----------------------
log "Configuring Bluetooth discoverable on boot"
BTCONF="/etc/bluetooth/main.conf"
if [[ -f "$BTCONF" ]]; then
  sed -i 's/^#\?DiscoverableTimeout.*/DiscoverableTimeout = 0/' "$BTCONF" || true
fi

rfkill unblock bluetooth || true

mkdir -p /usr/local/bin
cat <<'EOF' > /usr/local/bin/audio-pi-bt.sh
#!/bin/bash
set -e

sleep 6

if command -v rfkill >/dev/null 2>&1; then
  rfkill unblock bluetooth || true
fi

for i in {1..10}; do
  bluetoothctl list | grep -q "Controller" && break
  sleep 1
done

bluetoothctl power on || true
bluetoothctl agent on || true
bluetoothctl default-agent || true
bluetoothctl pairable on || true
bluetoothctl discoverable on || true
exit 0
EOF
chmod +x /usr/local/bin/audio-pi-bt.sh

cat <<'EOF' > /etc/systemd/system/audio-pi-bt.service
[Unit]
Description=Audio-Pi Bluetooth Auto Setup
After=bluetooth.service
Wants=bluetooth.service

[Service]
ExecStart=/usr/local/bin/audio-pi-bt.sh
Type=oneshot
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now audio-pi-bt.service

# -----------------------
# 8) Install the Web UI app from repo
# Expect repo contains ./app (FastAPI) and ./scripts (helpers)
# -----------------------
log "Installing Audio-Pi Web UI files"
mkdir -p "${APP_DIR}" "${CFG_DIR}"

if [[ ! -d "${SCRIPT_DIR}/app" ]]; then
  echo "ERROR: Expected '${SCRIPT_DIR}/app' folder in your repo."
  echo "Create it with app.py, requirements.txt, and static/ files."
  exit 1
fi

rsync -a --delete "${SCRIPT_DIR}/app/" "${APP_DIR}/app/"

if [[ -d "${SCRIPT_DIR}/scripts" ]]; then
  rsync -a --delete "${SCRIPT_DIR}/scripts/" "${APP_DIR}/scripts/"
  chmod +x "${APP_DIR}/scripts/"*.sh 2>/dev/null || true
fi

chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

# Default config if missing
if [[ ! -f "${CFG_FILE}" ]]; then
  log "Writing default config"
  cat <<'JSON' > "${CFG_FILE}"
{
  "device_name": "Audio-Pi",
  "default_volume": 35,
  "features": {
    "airplay": true,
    "spotify": true,
    "bluetooth": true,
    "multiroom": true,
    "wifi_config": true
  },
  "multiroom": {
    "mode": "server",
    "snapcast_latency_ms": 100
  }
}
JSON
  chmod 644 "${CFG_FILE}"
fi

# Python venv + requirements
log "Creating Python venv + installing dependencies"
python3 -m venv "${APP_DIR}/venv"
"${APP_DIR}/venv/bin/pip" install --upgrade pip
"${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/app/requirements.txt"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}/venv"

# -----------------------
# 9) Sudoers rules for web UI control
# -----------------------
log "Installing sudoers rules for Audio-Pi Web UI"
cat <<'SUDO' > "${SUDOERS_FILE}"
audio-pi ALL=(root) NOPASSWD: \
  /bin/systemctl start mpd, /bin/systemctl stop mpd, /bin/systemctl restart mpd, \
  /bin/systemctl start bluetooth, /bin/systemctl stop bluetooth, /bin/systemctl restart bluetooth, \
  /bin/systemctl start shairport-sync, /bin/systemctl stop shairport-sync, /bin/systemctl restart shairport-sync, \
  /bin/systemctl start raspotify, /bin/systemctl stop raspotify, /bin/systemctl restart raspotify, \
  /bin/systemctl start snapserver, /bin/systemctl stop snapserver, /bin/systemctl restart snapserver, \
  /bin/systemctl start snapclient, /bin/systemctl stop snapclient, /bin/systemctl restart snapclient, \
  /usr/bin/nmcli, \
  /usr/bin/amixer, \
  /usr/sbin/reboot
SUDO
chmod 440 "${SUDOERS_FILE}"

# -----------------------
# 10) systemd service for Web UI
# -----------------------
log "Installing systemd service: audio-pi-web"
cat <<EOF > "${WEB_SERVICE}"
[Unit]
Description=Audio-Pi Web UI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}/app
Environment=AUDIO_PI_CONFIG=${CFG_FILE}
ExecStart=${APP_DIR}/venv/bin/uvicorn app:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now audio-pi-web.service

# -----------------------
# 11) nginx reverse proxy :80 -> :8080
# -----------------------
log "Configuring nginx reverse proxy (port 80 -> 8080)"
rm -f /etc/nginx/sites-enabled/default || true

cat <<'NG' > "${NGINX_SITE_AVAIL}"
server {
  listen 80 default_server;
  listen [::]:80 default_server;

  location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  }
}
NG

ln -sf "${NGINX_SITE_AVAIL}" "${NGINX_SITE_ENABLED}"
nginx -t
systemctl enable --now nginx
systemctl restart nginx

# -----------------------
# 12) Set default volume from config (best effort)
# -----------------------
log "Setting default volume"
VOL="$(jq -r '.default_volume // 35' "${CFG_FILE}" 2>/dev/null || echo 35)"
amixer -q set Master "${VOL}%" || true

# -----------------------
# 13) Restart key services (best effort)
# -----------------------
log "Restarting services"
systemctl restart bluetooth || true
systemctl restart audio-pi-bt.service || true
systemctl restart shairport-sync || true
systemctl restart snapserver || true
systemctl restart snapclient || true
systemctl restart mpd || true
systemctl restart audio-pi-web.service || true

# -----------------------
# Done
# -----------------------
IP="$(hostname -I | awk '{print $1}')"
echo
echo "=== Install complete ==="
echo "Open: http://${IP}/ (or http://${IP}:8080/)"
echo "Web service: sudo systemctl status audio-pi-web.service"
echo

if [[ "${REBOOT_REQUIRED}" == "1" ]]; then
  echo "A reboot is required to apply audio firmware changes."
  echo "Rebooting now..."
  reboot
fi
