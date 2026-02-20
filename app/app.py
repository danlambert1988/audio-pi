import os
import json
import subprocess
from typing import Optional
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = os.path.dirname(__file__)
CONFIG_PATH = os.environ.get("AUDIO_PI_CONFIG", "/etc/audio-pi/config.json")

app = FastAPI()
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

def sh(cmd: str) -> str:
    try:
        return subprocess.check_output(cmd, shell=True, text=True, stderr=subprocess.STDOUT).strip()
    except subprocess.CalledProcessError as e:
        return (e.output or "").strip()

def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_config(data: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def service_status(name: str) -> str:
    out = sh(f"systemctl is-active {name} || true")
    return out or "unknown"

def service_action(name: str, action: str) -> str:
    return sh(f"sudo systemctl {action} {name}")

def get_volume_percent() -> int:
    out = sh("amixer get Master | grep -oE '[0-9]+%' | head -n1 | tr -d '%'")
    try:
        return int(out)
    except Exception:
        return 0

class WifiConnect(BaseModel):
    ssid: str
    password: Optional[str] = None

class DeviceName(BaseModel):
    name: str

@app.get("/")
def root():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))

@app.get("/api/state")
def state():
    cfg = load_config()
    return {
        "device_name": cfg.get("device_name", "Audio-Pi"),
        "volume": get_volume_percent(),
        "services": {
            "snapserver": service_status("snapserver"),
            "snapclient": service_status("snapclient"),
            "bluetooth": service_status("bluetooth"),
            "airplay": service_status("shairport-sync"),
            "spotify": service_status("raspotify"),
            "mpd": service_status("mpd"),
        },
    }

@app.post("/api/volume/{value}")
def set_volume(value: int):
    value = max(0, min(100, value))
    sh(f"amixer set Master {value}%")
    return {"ok": True, "volume": get_volume_percent()}

@app.post("/api/service/{name}/{action}")
def control_service(name: str, action: str):
    valid_actions = {"start", "stop", "restart"}
    if action not in valid_actions:
        return {"ok": False, "error": "invalid action"}

    mapping = {
        "snapserver": "snapserver",
        "snapclient": "snapclient",
        "bluetooth": "bluetooth",
        "airplay": "shairport-sync",
        "spotify": "raspotify",
        "mpd": "mpd",
    }

    unit = mapping.get(name)
    if not unit:
        return {"ok": False, "error": "invalid service"}

    service_action(unit, action)
    return {"ok": True, "status": service_status(unit)}

@app.post("/api/multiroom/{mode}")
def set_multiroom(mode: str):
    if mode not in {"server", "client"}:
        return {"ok": False, "error": "mode must be server or client"}

    if mode == "server":
        sh("sudo systemctl enable --now snapserver")
        sh("sudo systemctl disable --now snapclient")
    else:
        sh("sudo systemctl enable --now snapclient")
        sh("sudo systemctl disable --now snapserver")

    return {"ok": True, "snapserver": service_status("snapserver"), "snapclient": service_status("snapclient")}

@app.get("/api/wifi/scan")
def wifi_scan():
    # Using shell here is fine for scan; it returns lots of lines.
    out = sh("sudo nmcli -t -f SSID,SIGNAL,SECURITY dev wifi list || true")
    networks = []
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[0]:
            networks.append({"ssid": parts[0], "signal": parts[1], "security": parts[2]})
    return {"ok": True, "networks": networks}

@app.post("/api/wifi/connect")
def wifi_connect(data: WifiConnect):
    args = ["sudo", "nmcli", "dev", "wifi", "connect", data.ssid]
    if data.password:
        args += ["password", data.password]
    try:
        out = subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()
        return {"ok": True, "result": out}
    except subprocess.CalledProcessError as e:
        return {"ok": False, "result": (e.output or "").strip()}

@app.post("/api/device-name")
def set_device_name(data: DeviceName):
    cfg = load_config()
    cfg["device_name"] = data.name
    save_config(cfg)
    return {"ok": True}
