import os
import json
import re
import math
import subprocess
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ------------------------------------------------------------
# Audio-Pi Web Backend (FastAPI)
#
# Features:
# - Serves static UI at /
# - REST API:
#     - /api/state                    : device name, volume, service status
#     - /api/volume/{value}           : set volume 0..100 (dB-scaled for sane slider)
#     - /api/service/{name}/{action}  : start/stop/restart services (sudo allowlist)
#     - /api/multiroom/{mode}         : server/client/off snapcast toggles (optional)
#     - /api/wifi/scan                : scan networks (nmcli)
#     - /api/wifi/connect             : connect to network (nmcli)
#     - /api/device-name              : save friendly device name
#     - /api/reboot                   : reboot device
#
# Audio notes:
# - Pi headphone output volume is very non-linear in "%".
# - This backend sets volume in dB on ALSA card 0 "PCM" to give a usable 0..100 slider.
#
# Permissions:
# - systemd service runs as User=audio-pi (per your setup)
# - You MUST have sudoers allowlist for:
#     /usr/bin/systemctl
#     /usr/bin/nmcli
#     /usr/bin/amixer
#     /usr/sbin/reboot
#
# ------------------------------------------------------------

BASE_DIR = os.path.dirname(__file__)
CONFIG_PATH = os.environ.get("AUDIO_PI_CONFIG", "/etc/audio-pi/config.json")

app = FastAPI(title="Audio-Pi")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


# -----------------------
# Models
# -----------------------
class WifiConnect(BaseModel):
    ssid: str
    password: Optional[str] = None


class DeviceName(BaseModel):
    name: str


# -----------------------
# Command helpers
# -----------------------
def run(cmd: List[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, check=check)


def sh(cmd: str) -> str:
    try:
        return subprocess.check_output(cmd, shell=True, text=True, stderr=subprocess.STDOUT).strip()
    except subprocess.CalledProcessError as e:
        return (e.output or "").strip()


def systemctl(args: List[str]) -> Dict[str, Any]:
    """
    Run systemctl via sudo (audio-pi user needs sudoers allowlist).
    """
    p = run(["sudo", "/usr/bin/systemctl"] + args)
    ok = (p.returncode == 0)
    return {
        "ok": ok,
        "stdout": (p.stdout or "").strip(),
        "stderr": (p.stderr or "").strip(),
        "code": p.returncode,
    }


def service_status(unit: str) -> str:
    p = run(["/usr/bin/systemctl", "is-active", unit])
    return (p.stdout or "").strip() or "unknown"


def service_enabled(unit: str) -> str:
    p = run(["/usr/bin/systemctl", "is-enabled", unit])
    return (p.stdout or "").strip() or "unknown"


# -----------------------
# Config helpers
# -----------------------
def load_config() -> Dict[str, Any]:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# -----------------------
# Volume (dB-scaled) - ALSA card 0, mixer PCM
# -----------------------
# On your Pi, we confirmed:
#   card 0: Headphones (bcm2835 Headphones)
#   mixer control: PCM
#
# Using dB provides a smooth 0..100 slider vs the non-linear % curve.
_PCM_DB_CACHE: Dict[str, Optional[int]] = {"min": None, "max": None}  # 0.01 dB units from ALSA


def _pcm_db_limits() -> (int, int):
    """
    Parse ALSA PCM limits:
      "Limits: Playback -10239 - 400"
    returns: (min, max) in 0.01 dB units
    """
    out = sh("amixer -c 0 get PCM 2>/dev/null | grep -m1 'Limits:' || true")
    m = re.search(r"Limits:\s*Playback\s*(-?\d+)\s*-\s*(-?\d+)", out)
    if not m:
        # safe fallback: -50.00 dB .. +4.00 dB
        return (-5000, 400)
    return (int(m.group(1)), int(m.group(2)))


def _ensure_pcm_db_cache():
    if _PCM_DB_CACHE["min"] is None or _PCM_DB_CACHE["max"] is None:
        mn, mx = _pcm_db_limits()
        _PCM_DB_CACHE["min"], _PCM_DB_CACHE["max"] = mn, mx


def _get_pcm_db_value() -> Optional[float]:
    """
    Returns current PCM level in dB if available (e.g. 4.00), else None.
    """
    out = sh(
        "amixer -c 0 get PCM 2>/dev/null "
        "| grep -oE '\\[-?[0-9]+\\.[0-9]+dB\\]' | head -n1 | tr -d '[]dB' || true"
    )
    try:
        return float(out)
    except Exception:
        return None


def _get_pcm_percent_value() -> Optional[int]:
    out = sh("amixer -c 0 get PCM 2>/dev/null | grep -oE '[0-9]+%' | head -n1 | tr -d '%' || true")
    try:
        return int(out)
    except Exception:
        return None


def get_volume_percent() -> int:
    """
    Returns UI volume 0..100 (based on dB mapping).
    """
    db = _get_pcm_db_value()
    if db is None:
        # percent fallback (still returns something)
        p = _get_pcm_percent_value()
        return int(p) if p is not None else 0

    # Use a nice user-facing range: -50dB..+4dB
    min_db = -50.0
    max_db = 4.0
    db = max(min_db, min(max_db, db))
    ui = int(round((db - min_db) / (max_db - min_db) * 100))
    return max(0, min(100, ui))


def set_volume_percent(value: int) -> int:
    """
    Sets volume using dB for a smooth slider.
    """
    value = max(0, min(100, value))

    # Tune these if you want:
    min_db = -50.0   # quieter floor (use -60.0 if you want quieter minimum)
    max_db = 4.0     # matches your observed 100% = +4dB

    if value == 0:
        run(["sudo", "/usr/bin/amixer", "-c", "0", "set", "PCM", "mute"])
        return 0

    # Gamma curve: makes low-end more usable (0.6 gives more resolution at low volumes)
    x = value / 100.0
    gamma = 0.6
    x = math.pow(x, gamma)

    target_db = min_db + x * (max_db - min_db)
    run(["sudo", "/usr/bin/amixer", "-c", "0", "set", "PCM", f"{target_db:.2f}dB", "unmute"])
    return value


# -----------------------
# Routes
# -----------------------
@app.get("/")
def root():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


@app.get("/api/state")
def state():
    cfg = load_config()
    return {
        "device_name": cfg.get("device_name", "Audio-Pi"),
        "volume": get_volume_percent(),
        "audio": {"card": 0, "mixer": "PCM"},
        "services": {
            "bluetooth": service_status("bluetooth"),
            "airplay": service_status("shairport-sync"),
            "spotify": service_status("raspotify"),
            "snapserver": service_status("snapserver"),
            "snapclient": service_status("snapclient"),
        },
        "enabled": {
            "bluetooth": service_enabled("bluetooth"),
            "airplay": service_enabled("shairport-sync"),
            "spotify": service_enabled("raspotify"),
            "snapserver": service_enabled("snapserver"),
            "snapclient": service_enabled("snapclient"),
        }
    }


@app.post("/api/volume/{value}")
def api_set_volume(value: int):
    return {"ok": True, "volume": set_volume_percent(value)}


@app.post("/api/service/{name}/{action}")
def api_service(name: str, action: str):
    valid_actions = {"start", "stop", "restart"}
    if action not in valid_actions:
        raise HTTPException(status_code=400, detail="Invalid action")

    mapping = {
        "bluetooth": "bluetooth",
        "airplay": "shairport-sync",
        "spotify": "raspotify",
        "snapserver": "snapserver",
        "snapclient": "snapclient",
    }

    unit = mapping.get(name)
    if not unit:
        raise HTTPException(status_code=400, detail="Invalid service")

    res = systemctl([action, unit])
    return {"ok": res["ok"], "status": service_status(unit), "detail": res}


@app.post("/api/multiroom/{mode}")
def api_multiroom(mode: str):
    """
    mode:
      - server: enable snapserver, disable snapclient
      - client: enable snapclient, disable snapserver
      - off: disable both
    """
    if mode not in {"server", "client", "off"}:
        raise HTTPException(status_code=400, detail="mode must be server, client, or off")

    if mode == "server":
        systemctl(["enable", "--now", "snapserver"])
        systemctl(["disable", "--now", "snapclient"])
    elif mode == "client":
        systemctl(["enable", "--now", "snapclient"])
        systemctl(["disable", "--now", "snapserver"])
    else:
        systemctl(["disable", "--now", "snapserver"])
        systemctl(["disable", "--now", "snapclient"])

    return {
        "ok": True,
        "mode": mode,
        "snapserver": service_status("snapserver"),
        "snapclient": service_status("snapclient"),
        "enabled": {
            "snapserver": service_enabled("snapserver"),
            "snapclient": service_enabled("snapclient"),
        },
    }


@app.get("/api/wifi/scan")
def wifi_scan():
    p = run(["sudo", "/usr/bin/nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"])
    if p.returncode != 0:
        return {"ok": False, "error": (p.stderr or p.stdout or "").strip(), "networks": []}

    networks = []
    for line in (p.stdout or "").splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[0]:
            networks.append({"ssid": parts[0], "signal": parts[1], "security": parts[2]})

    # de-dupe by SSID, keep strongest
    best: Dict[str, Dict[str, str]] = {}
    for n in networks:
        ssid = n["ssid"]
        try:
            sig = int(n["signal"])
        except Exception:
            sig = 0
        if ssid not in best or sig > int(best[ssid].get("signal", "0")):
            best[ssid] = n

    return {"ok": True, "networks": sorted(best.values(), key=lambda x: int(x.get("signal", "0")), reverse=True)}


@app.post("/api/wifi/connect")
def wifi_connect(data: WifiConnect):
    args = ["sudo", "/usr/bin/nmcli", "dev", "wifi", "connect", data.ssid]
    if data.password:
        args += ["password", data.password]

    p = run(args)
    ok = (p.returncode == 0)
    return {"ok": ok, "result": (p.stdout or p.stderr or "").strip(), "code": p.returncode}


@app.post("/api/device-name")
def set_device_name(data: DeviceName):
    cfg = load_config()
    cfg["device_name"] = data.name
    save_config(cfg)
    return {"ok": True, "device_name": data.name}


@app.post("/api/reboot")
def reboot():
    p = run(["sudo", "/usr/sbin/reboot"])
    return {"ok": p.returncode == 0, "code": p.returncode}
