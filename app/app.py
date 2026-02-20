import os
import json
import subprocess
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ------------------------------------------------------------
# Audio-Pi Web Backend (FastAPI)
# - Serves static UI at /
# - Provides REST API for:
#     - State/status
#     - Volume control (auto-detects mixer/card, supports Pi headphone jack)
#     - Service control (via sudo allowlist)
#     - Multiroom mode toggle (server/client/off)
#     - Wi-Fi scan/connect (nmcli)
#
# IMPORTANT:
#   - Your systemd service runs as user: audio-pi
#   - You MUST have sudoers rules allowing audio-pi to run:
#       /usr/bin/systemctl (start/stop/restart/enable/disable)
#       /usr/bin/nmcli
#       /usr/bin/amixer
#       /usr/sbin/reboot
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
# Helpers
# -----------------------
def run(cmd: List[str], check: bool = False) -> subprocess.CompletedProcess:
    """
    Run a command without shell, capturing output.
    """
    return subprocess.run(cmd, text=True, capture_output=True, check=check)


def sh(cmd: str) -> str:
    """
    Run a shell command (only used for simple parsing).
    """
    try:
        return subprocess.check_output(cmd, shell=True, text=True, stderr=subprocess.STDOUT).strip()
    except subprocess.CalledProcessError as e:
        return (e.output or "").strip()


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


def systemctl(args: List[str]) -> Dict[str, Any]:
    """
    Run systemctl via sudo (audio-pi user needs sudoers allowlist).
    """
    p = run(["sudo", "/usr/bin/systemctl"] + args)
    ok = (p.returncode == 0)
    return {"ok": ok, "stdout": (p.stdout or "").strip(), "stderr": (p.stderr or "").strip(), "code": p.returncode}


def service_status(unit: str) -> str:
    p = run(["/usr/bin/systemctl", "is-active", unit])
    return (p.stdout or "").strip() or "unknown"


def service_enabled(unit: str) -> str:
    p = run(["/usr/bin/systemctl", "is-enabled", unit])
    return (p.stdout or "").strip() or "unknown"


# -----------------------
# ALSA volume detection
# -----------------------
# Cache detected mixer/card so UI is responsive
_DETECTED: Dict[str, Any] = {"card": None, "mixer": None}


def list_cards() -> List[int]:
    """
    Return ALSA card numbers present (best effort).
    """
    out = sh("aplay -l 2>/dev/null | sed -n 's/^card \\([0-9]\\+\\):.*/\\1/p' | sort -n | uniq")
    cards = []
    for line in out.splitlines():
        try:
            cards.append(int(line.strip()))
        except Exception:
            pass
    return cards or [0]


def list_mixers(card: int) -> List[str]:
    """
    Return simple mixer control names for a given ALSA card.
    """
    out = sh(f"amixer -c {card} scontrols 2>/dev/null")
    names = []
    for line in out.splitlines():
        # Example: Simple mixer control 'PCM',0
        if "'" in line:
            parts = line.split("'")
            if len(parts) >= 2:
                names.append(parts[1])
    return names


def detect_mixer() -> dict:
    """
    Prefer bcm2835 Headphones card if present, then fall back.
    """
    if _DETECTED["card"] is not None and _DETECTED["mixer"] is not None:
        return _DETECTED

    preferred_mixers = ["PCM", "Headphone", "Master", "Digital"]

    # Find card numbers + names
    # Example aplay -l line: "card 0: Headphones [bcm2835 Headphones], device 0: ..."
    cards_info = sh("aplay -l 2>/dev/null | sed -n 's/^card \\([0-9]\\+\\): \\([^[]\\+\\)\\[\\([^]]\\+\\)\\].*/\\1|\\3/p'").splitlines()
    # cards_info -> ["0|bcm2835 Headphones", "1|vc4-hdmi-0", ...]

    # Prefer headphone card if present
    preferred_cards = []
    for line in cards_info:
        try:
            num_str, name = line.split("|", 1)
            num = int(num_str.strip())
            if "Headphones" in name or "bcm2835" in name:
                preferred_cards.append(num)
        except:
            pass

    # Then add all other cards as fallback
    for c in list_cards():
        if c not in preferred_cards:
            preferred_cards.append(c)

    # Now pick the first preferred mixer on the best card
    for card in preferred_cards:
        mixers = list_mixers(card)
        for m in preferred_mixers:
            if m in mixers:
                _DETECTED["card"] = card
                _DETECTED["mixer"] = m
                return _DETECTED

    # Final fallback
    _DETECTED["card"] = preferred_cards[0] if preferred_cards else 0
    _DETECTED["mixer"] = "Master"
    return _DETECTED

    preferred_mixers = ["PCM", "Headphone", "Master", "Digital"]
    for card in list_cards():
        mixers = list_mixers(card)
        for m in preferred_mixers:
            if m in mixers:
                _DETECTED["card"] = card
                _DETECTED["mixer"] = m
                return _DETECTED

    # Fallback: card 0, Master
    _DETECTED["card"] = 0
    _DETECTED["mixer"] = "Master"
    return _DETECTED


def _get_hw_pcm() -> int:
    out = sh("amixer -c 0 get PCM | grep -oE '[0-9]+%' | head -n1 | tr -d '%'")
    try:
        return int(out)
    except Exception:
        return 0

def _ui_to_hw(ui: int) -> int:
    ui = max(0, min(100, ui))
    if ui == 0:
        return 0
    # 1..100 -> 70..100 (spread across full slider)
    return 70 + int((ui / 100) * 30)

def _hw_to_ui(hw: int) -> int:
    hw = max(0, min(100, hw))
    if hw == 0:
        return 0
    if hw <= 70:
        return 1
    return int(((hw - 70) / 30) * 100)

def get_volume_percent() -> int:
    return _hw_to_ui(_get_hw_pcm())

def set_volume_percent(value: int) -> int:
    ui = max(0, min(100, value))
    hw = _ui_to_hw(ui)
    if ui == 0:
        run(["sudo", "/usr/bin/amixer", "-c", "0", "set", "PCM", "0%", "mute"])
    else:
        run(["sudo", "/usr/bin/amixer", "-c", "0", "set", "PCM", f"{hw}%", "unmute"])
    return ui


# -----------------------
# Routes
# -----------------------
@app.get("/")
def root():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


@app.get("/api/state")
def state():
    cfg = load_config()
    det = detect_mixer()

    return {
        "device_name": cfg.get("device_name", "Audio-Pi"),
        "volume": get_volume_percent(),
        "mixer": {"card": det["card"], "control": det["mixer"]},
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
        }
    }


@app.get("/api/wifi/scan")
def wifi_scan():
    # nmcli output is easiest in terse mode
    p = run(["sudo", "/usr/bin/nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"])
    if p.returncode != 0:
        return {"ok": False, "error": (p.stderr or p.stdout or "").strip(), "networks": []}

    networks = []
    for line in (p.stdout or "").splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[0]:
            networks.append({"ssid": parts[0], "signal": parts[1], "security": parts[2]})

    # de-dupe by SSID, keep strongest signal
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
