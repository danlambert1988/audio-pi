#!/usr/bin/env bash
set -euo pipefail

echo "=== Audio-Pi installer ==="
echo "Installs Snapcast + AirPlay (Shairport) and makes Bluetooth discoverable on boot."
echo

# 1) Packages
sudo apt update
sudo apt install -y snapserver snapclient shairport-sync bluez pulseaudio-utils rfkill

# Android Wi-Fi casting support (DLNA/UPnP renderer)
sudo apt install -y gmediarender || sudo apt install -y gmrender-resurrect

sudo systemctl enable gmediarender || sudo systemctl enable gmrender-resurrect
sudo systemctl restart gmediarender || sudo systemctl restart gmrender-resurrect || true

# Try to set a friendly name (works on many builds)
sudo sh -c 'printf "GMRENDER_FRIENDLY_NAME=Audio-Pi\n" > /etc/default/gmediarender' || true
sudo systemctl restart gmediarender || sudo systemctl restart gmrender-resurrect || true

# 2) Force analog audio default (Pi 3.5mm)
CFG="/boot/firmware/config.txt"
sudo touch "$CFG"
sudo grep -q '^dtparam=audio=on' "$CFG" || echo 'dtparam=audio=on' | sudo tee -a "$CFG" >/dev/null
sudo grep -q '^hdmi_ignore_edid_audio=1' "$CFG" || echo 'hdmi_ignore_edid_audio=1' | sudo tee -a "$CFG" >/dev/null

# 3) Make Shairport output to stdout (for Snapserver process source)
SHAIR="/etc/shairport-sync.conf"
if [ -f "$SHAIR" ]; then
  sudo sed -i 's/output_backend *= *"alsa"/output_backend = "stdout"/' "$SHAIR" || true
  sudo sed -i 's/output_backend *= *"pipe"/output_backend = "stdout"/' "$SHAIR" || true
  grep -q 'output_backend' "$SHAIR" || echo 'output_backend = "stdout";' | sudo tee -a "$SHAIR" >/dev/null
else
  echo "WARN: /etc/shairport-sync.conf not found (package layout may differ)."
fi

# 4) Configure Snapserver to run shairport-sync as its source
SNAPCONF="/etc/snapserver.conf"
sudo touch "$SNAPCONF"
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

# 5) Bluetooth: ensure service enabled + unblock + keep discoverable
BTCONF="/etc/bluetooth/main.conf"
if [ -f "$BTCONF" ]; then
  sudo sed -i 's/^#\?DiscoverableTimeout.*/DiscoverableTimeout = 0/' "$BTCONF" || true
  # (Optional) Force name if you want:
  # sudo sed -i 's/^#\?Name.*/Name = Audio-Pi/' "$BTCONF" || true
fi

sudo systemctl enable bluetooth
sudo systemctl start bluetooth || true

# Unblock Bluetooth if the OS boots it blocked (common on some Pi images)
sudo rfkill unblock bluetooth || true

# 6) Robust Bluetooth boot script
sudo mkdir -p /usr/local/bin
cat <<'EOF' | sudo tee /usr/local/bin/audio-pi-bt.sh >/dev/null
#!/bin/bash
set -e

# Give the stack time to come up
sleep 6

# Unblock in case rfkill defaulted to blocked
if command -v rfkill >/dev/null 2>&1; then
  rfkill unblock bluetooth || true
fi

# Wait briefly for controller to appear
for i in {1..10}; do
  bluetoothctl list | grep -q "Controller" && break
  sleep 1
done

# Power + pairing + discoverable
bluetoothctl power on || true
bluetoothctl agent on || true
bluetoothctl default-agent || true
bluetoothctl pairable on || true
bluetoothctl discoverable on || true

exit 0
EOF
sudo chmod +x /usr/local/bin/audio-pi-bt.sh

cat <<'EOF' | sudo tee /etc/systemd/system/audio-pi-bt.service >/dev/null
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

sudo systemctl daemon-reload
sudo systemctl enable audio-pi-bt.service

# 7) Enable streaming services
sudo systemctl enable shairport-sync
sudo systemctl enable snapserver
sudo systemctl enable snapclient

# 8) Restart services now (best effort)
sudo systemctl restart bluetooth || true
sudo systemctl restart audio-pi-bt.service || true
sudo systemctl restart shairport-sync || true
sudo systemctl restart snapserver || true
sudo systemctl restart snapclient || true

echo
echo "=== Done. Rebooting to apply audio config changes... ==="
sudo reboot
