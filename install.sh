#!/usr/bin/env bash
set -euo pipefail

echo "=== Audio-Pi installer ==="
echo "This will install Snapcast + AirPlay (Shairport) and set Bluetooth discoverable on boot."
echo

# 1) Packages
sudo apt update
sudo apt install -y snapserver snapclient shairport-sync bluez pulseaudio-utils

# 2) Force analog audio default (Pi 3.5mm)
# Ensure analog is enabled + HDMI audio ignored
CFG="/boot/firmware/config.txt"
sudo touch "$CFG"
sudo grep -q '^dtparam=audio=on' "$CFG" || echo 'dtparam=audio=on' | sudo tee -a "$CFG" >/dev/null
sudo grep -q '^hdmi_ignore_edid_audio=1' "$CFG" || echo 'hdmi_ignore_edid_audio=1' | sudo tee -a "$CFG" >/dev/null

# 3) Make Shairport output to stdout (for Snapserver process source)
SHAIR="/etc/shairport-sync.conf"
if [ -f "$SHAIR" ]; then
  sudo sed -i 's/output_backend *= *"alsa"/output_backend = "stdout"/' "$SHAIR" || true
  sudo sed -i 's/output_backend *= *"pipe"/output_backend = "stdout"/' "$SHAIR" || true
  # If no output_backend line exists, append it
  grep -q 'output_backend' "$SHAIR" || echo 'output_backend = "stdout";' | sudo tee -a "$SHAIR" >/dev/null
else
  echo "WARN: /etc/shairport-sync.conf not found (package layout may differ)."
fi

# 4) Configure Snapserver to run shairport-sync as its source
SNAPCONF="/etc/snapserver.conf"
sudo mkdir -p /etc
sudo touch "$SNAPCONF"
# Remove existing [stream] section to avoid duplicates (simple approach)
sudo awk '
  BEGIN{skip=0}
  /^\[stream\]/{skip=1; next}
  /^\[.*\]/{if(skip==1){skip=0}}
  {if(skip==0) print}
' "$SNAPCONF" | sudo tee "$SNAPCONF.tmp" >/dev/null
sudo mv "$SNAPCONF.tmp" "$SNAPCONF"

cat <<'EOF' | sudo tee -a "$SNAPCONF" >/dev/null

[stream]
source = process:///usr/bin/shairport-sync?name=AirPlay&sampleformat=44100:16:2
EOF

# 5) Bluetooth discoverable + pairable on boot
sudo mkdir -p /usr/local/bin
cat <<'EOF' | sudo tee /usr/local/bin/audio-pi-bt.sh >/dev/null
#!/bin/bash
sleep 5
bluetoothctl power on
bluetoothctl agent on
bluetoothctl default-agent
bluetoothctl pairable on
bluetoothctl discoverable on
EOF
sudo chmod +x /usr/local/bin/audio-pi-bt.sh

cat <<'EOF' | sudo tee /etc/systemd/system/audio-pi-bt.service >/dev/null
[Unit]
Description=Audio-Pi Bluetooth Auto Setup
After=bluetooth.service

[Service]
ExecStart=/usr/local/bin/audio-pi-bt.sh
Type=oneshot

[Install]
WantedBy=multi-user.target
EOF

# 6) Make bluetooth discoverable not time out
BTCONF="/etc/bluetooth/main.conf"
if [ -f "$BTCONF" ]; then
  sudo sed -i 's/^#\?DiscoverableTimeout.*/DiscoverableTimeout = 0/' "$BTCONF" || true
fi

# 7) Enable services
sudo systemctl enable bluetooth
sudo systemctl enable audio-pi-bt.service
sudo systemctl enable shairport-sync
sudo systemctl enable snapserver
sudo systemctl enable snapclient

# 8) Restart services now
sudo systemctl restart bluetooth || true
sudo systemctl restart audio-pi-bt.service || true
sudo systemctl restart shairport-sync || true
sudo systemctl restart snapserver || true
sudo systemctl restart snapclient || true

echo
echo "=== Done. Rebooting to apply audio config changes... ==="
sudo reboot
