import os
import sys
#os.environ["QT_DEBUG_PLUGINS"] = "1"
import subprocess

# ==========================================
# APPLE SILICON COCOA AUTO-UNQUARANTINE & PATHING
# ==========================================
if sys.platform == "darwin":
    try:
        import PySide6

        pyside_dir = os.path.dirname(PySide6.__file__)

        # 1. Silently strip Apple Gatekeeper quarantines
        subprocess.run(["xattr", "-cr", pyside_dir], check=False, capture_output=True)

        # 2. Force hardware acceleration
        os.environ['QT_MAC_WANTS_LAYER'] = '1'

        # 3. Explicitly feed PySide6 its own GPS coordinates
        os.environ['QT_PLUGIN_PATH'] = os.path.join(pyside_dir, 'Qt', 'plugins')
        os.environ['QT_QPA_PLATFORM_PLUGIN_PATH'] = os.path.join(pyside_dir, 'Qt', 'plugins', 'platforms')
    except Exception as e:
        # Logger isn't configured yet at module import time — use stderr.
        print(f"[BOOT] PySide6 quarantine/path setup failed: {e}", file=sys.stderr)
# ==========================================

import socket
import threading
import time
import json
import signal
import logging
import queue
import http.server
import socketserver

from pythonosc import dispatcher, osc_server, udp_client
from PySide6.QtWidgets import QApplication
from titan_engine import RenderEngine
from titan_gui import TitanQtGUI
from titan_watchdog import TitanWatchdog
from titan_calibration import AudioCalibrator

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

log_file_path = os.path.join(BASE_DIR, "titan_debug.log")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.FileHandler(log_file_path),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("TitanEngine")
logger.info("=== TITAN ENGINE BOOT SEQUENCE INITIATED ===")


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError as e:
        logger.warning(f"get_local_ip() failed, falling back to 127.0.0.1: {e}")
        return "127.0.0.1"


def build_sacn_packet(universe, data, sequence, priority=100, source_name="Titan Engine", preview=False):
    flags_length = 0x7000 | (110 + len(data))
    pdu_flags_length = 0x7000 | (88 + len(data))
    dmp_flags_length = 0x7000 | (10 + len(data))

    name_bytes = source_name.encode('utf-8')[:63]
    name_padded = name_bytes + b'\x00' * (64 - len(name_bytes))
    opts = 0x80 if preview else 0x00

    header = bytearray([
        0x00, 0x10, 0x00, 0x00, 0x41, 0x53, 0x43, 0x2d, 0x45, 0x31, 0x2e, 0x31, 0x37, 0x00, 0x00, 0x00,
        (flags_length >> 8) & 0xFF, flags_length & 0xFF, 0x00, 0x00, 0x00, 0x04,
        0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88, 0x99, 0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff, 0x00,
        (pdu_flags_length >> 8) & 0xFF, pdu_flags_length & 0xFF, 0x00, 0x00, 0x00, 0x02
    ])
    header += name_padded
    header += bytearray([
        priority & 0xFF, 0x00, 0x00, sequence & 0xFF, opts,
        (universe >> 8) & 0xFF, universe & 0xFF,
        (dmp_flags_length >> 8) & 0xFF, dmp_flags_length & 0xFF,
        0x02, 0xa1, 0x00, 0x00, 0x00, 0x01,
        ((len(data) + 1) >> 8) & 0xFF, (len(data) + 1) & 0xFF, 0x00
    ])
    return header + bytearray(data)


params = {
    "hip": 150.0, "lop": 3000.0, "env": 1024.0,
    "test_on": 0, "sweep_on": 0, "test_freq": 1000.0, "test_db": -12.0,
    "expand": 1.0, "floor": 0.0, "ceiling": 100.0, "gamma": 1.0, "scale": 1.0, "eq_tilt": 0.0,
    "timing_mode": "Static", "atk_c": 20.0, "rel_c": 150.0, "atk_e": 50.0, "rel_e": 500.0,
    "time_gamma": 1.0,
    "freq_width": 1.0, "jitter_on": 1, "jitter_thresh": 0.05, "jitter_amount": 5.0,
    "dmx_smooth_on": 1, "smooth_size": 3,
    "protocol": "Art-Net", "net_mode": "Unicast", "sacn_priority": 100,
    "adv_net": 0, "art_net": 0, "art_sub": 0,
    "sacn_src": "Titan Engine", "sacn_preview": 0,
    "art_ip": "127.0.0.1", "art_port": 6454, "osc_in_port": 5005,
    "monitor_fps": 30, "monitor_univ": 0,
    "drive": 1.0, "knee": 0.1, "led_gamma": 2.5, "ctrl_univ": 15,
    "master_inhibitive": 1.0, "input_trim": 1.0, "noise_gate": 0.02,
    "glitch_digi_amt": 0.0, "glitch_digi_block": 4.0, "glitch_ana_amt": 0.0,
    "glitch_ana_tear": 10.0, "glitch_ana_noise": 0.5,
    "link_all_dynamics": 1,
    "color_r": 255.0, "color_g": 255.0, "color_b": 255.0, "color_w": 0.0,
    "od_thresh": 0.8, "od_desat": 0.5, "od_glitch": 0.5,
    "skew": 0.0, "width": 1.0,
    "dimmer_atk": 20, "dimmer_rel": 20,
    "bg_dimmer": 1.0, "bg_r": 0.0, "bg_g": 0.0, "bg_b": 0.0, "bg_w": 0.0,
    "master_ana_force_on": 0, "master_ana_force_off": 0,
    "master_digi_force_on": 0, "master_digi_force_off": 0,
    "master_od_force_on": 0, "master_od_force_off": 0,
    "preset_ch": 512,
    "remote_on": 1,  # 1 = Enabled, 0 = Disabled
    "artnet_offset": 1,
    "mute": 0,
    # Persisted Pure Data audio input device ID. None = PD default.
    "pd_audio_dev": None,
    # Persisted Pure Data audio output device ID. Needed for BlackHole-input
    # rigs so PD doesn't accidentally output back into the loopback.
    "pd_audio_dev_out": None,
    # Web remote-control server port (default 9000).
    "web_port": 9000,
}

params["num_fixtures"] = 1

slider_cfg = {
    "hip": {"min": 20, "max": 20000, "scale_type": "Logarithmic", "step": 10},
    "lop": {"min": 20, "max": 20000, "scale_type": "Logarithmic", "step": 10},
    "expand": {"min": 1.0, "max": 4.0, "scale_type": "Linear", "step": 0.1},
    "env": {"min": 128, "max": 4096, "scale_type": "Linear", "step": 128},
    "test_freq": {"min": 20, "max": 20000, "scale_type": "Logarithmic", "step": 100},
    "test_db": {"min": -60, "max": 0, "scale_type": "Linear", "step": 1.0},
    "floor": {"min": 0, "max": 120, "scale_type": "Linear", "step": 1.0},
    "ceiling": {"min": 0, "max": 120, "scale_type": "Linear", "step": 1.0},
    "gamma": {"min": 0.1, "max": 5.0, "scale_type": "Linear", "step": 0.1},
    "scale": {"min": 1.0, "max": 10.0, "scale_type": "Logarithmic", "step": 0.1},
    "eq_tilt": {"min": -1.0, "max": 1.0, "scale_type": "Linear", "step": 0.05},
    "atk_c": {"min": 1, "max": 1000, "scale_type": "Logarithmic", "step": 10},
    "rel_c": {"min": 1, "max": 2000, "scale_type": "Logarithmic", "step": 50},
    "atk_e": {"min": 1, "max": 1000, "scale_type": "Logarithmic", "step": 10},
    "rel_e": {"min": 1, "max": 2000, "scale_type": "Logarithmic", "step": 50},
    "time_gamma": {"min": 0.1, "max": 5.0, "scale_type": "Linear", "step": 0.1},
    "jitter_thresh": {"min": 0.01, "max": 0.2, "scale_type": "Linear", "step": 0.01},
    "jitter_amount": {"min": 1.0, "max": 20.0, "scale_type": "Linear", "step": 0.5},
    "smooth_size": {"min": 1, "max": 10, "scale_type": "Linear", "step": 1},
    "freq_width": {"min": 0.0, "max": 5.0, "scale_type": "Linear", "step": 0.1},
    "drive": {"min": 1.0, "max": 5.0, "scale_type": "Linear", "step": 0.1},
    "knee": {"min": 0.0, "max": 0.5, "scale_type": "Linear", "step": 0.01},
    "led_gamma": {"min": 1.0, "max": 4.0, "scale_type": "Linear", "step": 0.1},
    "ctrl_univ": {"min": 0, "max": 15, "scale_type": "Linear", "step": 1},
    "master_inhibitive": {"min": 0.0, "max": 1.0, "scale_type": "Linear", "step": 0.01},
    "input_trim": {"min": 0.0, "max": 5.0, "scale_type": "Linear", "step": 0.1},
    "noise_gate": {"min": 0.0, "max": 0.5, "scale_type": "Linear", "step": 0.01},
    "glitch_digi_amt": {"min": 0.0, "max": 1.0, "scale_type": "Linear", "step": 0.01},
    "glitch_digi_block": {"min": 2, "max": 40, "scale_type": "Linear", "step": 1},
    "glitch_ana_amt": {"min": 0.0, "max": 1.0, "scale_type": "Linear", "step": 0.01},
    "glitch_ana_tear": {"min": 1, "max": 100, "scale_type": "Linear", "step": 1},
    "glitch_ana_noise": {"min": 0.0, "max": 1.0, "scale_type": "Linear", "step": 0.01},
    "color_r": {"min": 0, "max": 255, "scale_type": "Linear", "step": 1},
    "color_g": {"min": 0, "max": 255, "scale_type": "Linear", "step": 1},
    "color_b": {"min": 0, "max": 255, "scale_type": "Linear", "step": 1},
    "color_w": {"min": 0, "max": 255, "scale_type": "Linear", "step": 1},
    "od_thresh": {"min": 0.0, "max": 1.0, "scale_type": "Linear", "step": 0.01},
    "od_desat": {"min": 0.0, "max": 1.0, "scale_type": "Linear", "step": 0.01},
    "od_glitch": {"min": 0.0, "max": 1.0, "scale_type": "Linear", "step": 0.01},
    "skew": {"min": -1.0, "max": 1.0, "scale_type": "Linear", "step": 0.05},
    "width": {"min": 0.0, "max": 1.0, "scale_type": "Linear", "step": 0.01},
    "dimmer_atk": {"min": 1, "max": 2000, "scale_type": "Logarithmic", "step": 10},
    "dimmer_rel": {"min": 1, "max": 5000, "scale_type": "Logarithmic", "step": 50},
    "bg_dimmer": {"min": 0.0, "max": 1.0, "scale_type": "Linear", "step": 0.01},
    "bg_r": {"min": 0, "max": 255, "scale_type": "Linear", "step": 1},
    "bg_g": {"min": 0, "max": 255, "scale_type": "Linear", "step": 1},
    "bg_b": {"min": 0, "max": 255, "scale_type": "Linear", "step": 1},
    "bg_w": {"min": 0, "max": 255, "scale_type": "Linear", "step": 1},
}

DEFAULT_FILE = os.path.join(BASE_DIR, "titan_default.json")

if os.path.exists(DEFAULT_FILE):
    try:
        with open(DEFAULT_FILE, "r") as f:
            saved_defaults = json.load(f)
            # Remove the strict filter so all dynamic patch layers load!
            params.update(saved_defaults)
    except (OSError, json.JSONDecodeError) as e:
        logger.error(f"Failed to load default config '{DEFAULT_FILE}': {e}. Using built-in defaults.")

params["art_ip"] = str(params.get("art_ip", "127.0.0.1")).strip()

app_state = {
    "pd_last_time": 0.0, "art_packets": 0, "packet_reset_time": time.time(),
    # Set to True so it immediately starts blasting data on launch
    "osc_in_text": "🔴 WAIT", "artnet_active": True,
    "last_ctrl_dmx": [0] * 512, "preset_map": {},
    "gui_lock_time": 0.0, "sacn_seq": {u: 0 for u in range(20)},
    "audio_frame_count": 0, "current_fps": 0.0, "last_fps_time": time.time(),
    "last_ctrl_time": 0.0, "preset_text_time": 0.0,
    "port_conflict_artnet": False, "port_conflict_osc": False, "send_error": None,
    # Tracks (offset, universe) pairs we've already warned about so the
    # offset-collision log doesn't spam every audio frame.
    "offset_warned": set(),
    # ArtPoll discovery results. None = scan in progress, [] = scan done (consumed), list = new results.
    "discovered_nodes": [],
    # Web remote server address (set by start_web_server after bind succeeds).
    "web_ip": None, "web_port": None,
    # Panic Blackout flag. Toggled by the GUI's PANIC BLACKOUT button.
    # When True, sender_thread overwrites every outgoing payload with zeros
    # so fixtures actively go dark — we can't just halt transmission because
    # most DMX consoles hold their last frame when the packet stream stops.
    "panic_blackout": False,
}

engine = RenderEngine()
calibrator = AudioCalibrator(params)
net_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
net_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
net_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
try:
    net_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
except OSError as e:
    logger.warning(f"Could not set IP_MULTICAST_TTL on net_sock: {e}. sACN multicast may be limited to local link.")

pd_client = udp_client.SimpleUDPClient("127.0.0.1", 5006)

send_queue = queue.Queue(maxsize=1)
audio_queue = queue.Queue(maxsize=1)


def _build_artnet_packet(universe, payload, art_net_val, art_sub_val):
    port_addr = (art_net_val << 8) | (art_sub_val << 4) | (universe & 0x0F)
    header = bytearray(b'Art-Net\x00\x00\x50\x00\x0e\x00\x00')
    header.append(port_addr & 0xFF)
    header.append((port_addr >> 8) & 0x7F)
    header.append((len(payload) >> 8) & 0xFF)
    header.append(len(payload) & 0xFF)
    return header + bytearray(payload)


def _dest_ip_for(u, net_mode, is_sacn, base_ip):
    if net_mode == "Broadcast" and not is_sacn:
        return "255.255.255.255"
    if net_mode == "Multicast" and is_sacn:
        return f"239.255.{(u >> 8) & 0xFF}.{u & 0xFF}"
    return base_ip


def artpoll_scan(timeout=3.0):
    """Broadcast ArtPoll and collect ArtPollReply packets for `timeout` seconds.
    Returns list of dicts: {"ip", "short_name", "long_name"}."""
    ARTPOLL = (
        b'Art-Net\x00'  # ID
        b'\x00\x20'     # OpPoll LE = 0x2000
        b'\x00\x0e'     # Protocol version 14
        b'\x02'         # TalkToMe: unicast replies
        b'\x00'         # Priority: dp_low
    )
    results = []
    seen_ips = set()
    scan_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        scan_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        scan_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        scan_sock.settimeout(0.3)
        scan_sock.bind(("", 0))
        scan_sock.sendto(ARTPOLL, ("255.255.255.255", 6454))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, (ip, _) = scan_sock.recvfrom(1024)
                if len(data) < 108 or data[:8] != b'Art-Net\x00':
                    continue
                if (data[8] | (data[9] << 8)) != 0x2100:  # OpPollReply
                    continue
                if ip in seen_ips:
                    continue
                seen_ips.add(ip)
                short = data[26:44].split(b'\x00')[0].decode('utf-8', errors='replace').strip()
                long_ = data[44:108].split(b'\x00')[0].decode('utf-8', errors='replace').strip()
                results.append({"ip": ip, "short_name": short, "long_name": long_})
            except socket.timeout:
                pass
    except Exception as e:
        logger.warning(f"ArtPoll scan error: {e}")
    finally:
        scan_sock.close()
    logger.info(f"ArtPoll scan complete: {len(results)} node(s) found")
    return results


# Web-remote slider config. Each entry drives one slider in the browser AND
# gets added to WEB_PARAM_WHITELIST on the server. `log:true` turns the slider
# into a log-scale mapping on the client (20..20000 Hz is unusable on a
# linear range input because 90% of the travel covers values above 2kHz).
# `fmt` is a JS snippet run on the current value to produce the display
# text next to the label.
WEB_SLIDERS = [
    {"section": "Master",        "key": "master_inhibitive", "label": "Master Dimmer",   "min": 0.0,  "max": 1.0,   "step": 0.01, "log": False, "fmt": "(v*100).toFixed(0)+'%'"},
    {"section": "Audio Input",   "key": "input_trim",        "label": "Input Trim",      "min": 0.0,  "max": 5.0,   "step": 0.1,  "log": False, "fmt": "v.toFixed(1)+'x'"},
    {"section": "Audio Input",   "key": "noise_gate",        "label": "Noise Gate",      "min": 0.0,  "max": 0.5,   "step": 0.01, "log": False, "fmt": "v.toFixed(2)"},
    {"section": "Audio Input",   "key": "drive",             "label": "Drive",           "min": 1.0,  "max": 5.0,   "step": 0.1,  "log": False, "fmt": "v.toFixed(1)+'x'"},
    {"section": "Audio Input",   "key": "floor",             "label": "Floor",           "min": 0,    "max": 120,   "step": 1,    "log": False, "fmt": "v.toFixed(0)+' dB'"},
    {"section": "Audio Input",   "key": "ceiling",           "label": "Ceiling",         "min": 0,    "max": 120,   "step": 1,    "log": False, "fmt": "v.toFixed(0)+' dB'"},
    {"section": "Audio Input",   "key": "expand",            "label": "Expand",          "min": 1.0,  "max": 4.0,   "step": 0.1,  "log": False, "fmt": "v.toFixed(1)+'x'"},
    {"section": "Audio Input",   "key": "hip",               "label": "Highpass",        "min": 20,   "max": 20000, "step": 1,    "log": True,  "fmt": "v>=1000?(v/1000).toFixed(1)+' kHz':v.toFixed(0)+' Hz'"},
    {"section": "Audio Input",   "key": "lop",               "label": "Lowpass",         "min": 20,   "max": 20000, "step": 1,    "log": True,  "fmt": "v>=1000?(v/1000).toFixed(1)+' kHz':v.toFixed(0)+' Hz'"},
    {"section": "Audio Mapping", "key": "gamma",             "label": "Gamma",           "min": 0.1,  "max": 5.0,   "step": 0.1,  "log": False, "fmt": "v.toFixed(1)"},
    {"section": "Audio Mapping", "key": "eq_tilt",           "label": "EQ Tilt",         "min": -1.0, "max": 1.0,   "step": 0.05, "log": False, "fmt": "(v>0?'+':'')+v.toFixed(2)"},
    {"section": "Audio Mapping", "key": "knee",              "label": "Knee",            "min": 0.0,  "max": 0.5,   "step": 0.01, "log": False, "fmt": "v.toFixed(2)"},
    {"section": "Audio Mapping", "key": "scale",             "label": "Scale",           "min": 1.0,  "max": 10.0,  "step": 0.1,  "log": True,  "fmt": "v.toFixed(1)+'x'"},
    {"section": "Dynamics",      "key": "atk_c",             "label": "Attack (Center)", "min": 1,    "max": 1000,  "step": 1,    "log": True,  "fmt": "v.toFixed(0)+' ms'"},
    {"section": "Dynamics",      "key": "rel_c",             "label": "Release (Center)","min": 1,    "max": 2000,  "step": 1,    "log": True,  "fmt": "v.toFixed(0)+' ms'"},
    {"section": "Dynamics",      "key": "atk_e",             "label": "Attack (Edge)",   "min": 1,    "max": 1000,  "step": 1,    "log": True,  "fmt": "v.toFixed(0)+' ms'"},
    {"section": "Dynamics",      "key": "rel_e",             "label": "Release (Edge)",  "min": 1,    "max": 2000,  "step": 1,    "log": True,  "fmt": "v.toFixed(0)+' ms'"},
    {"section": "Dynamics",      "key": "time_gamma",        "label": "Time Gamma",      "min": 0.1,  "max": 5.0,   "step": 0.1,  "log": False, "fmt": "v.toFixed(1)"},
    {"section": "Dynamics",      "key": "jitter_thresh",     "label": "Jitter Threshold","min": 0.01, "max": 0.2,   "step": 0.01, "log": False, "fmt": "v.toFixed(2)"},
    {"section": "Dynamics",      "key": "jitter_amount",     "label": "Jitter Amount",   "min": 1.0,  "max": 20.0,  "step": 0.5,  "log": False, "fmt": "v.toFixed(1)"},
    {"section": "Dynamics",      "key": "smooth_size",       "label": "Smooth Size",     "min": 1,    "max": 10,    "step": 1,    "log": False, "fmt": "v.toFixed(0)"},
]

# Keys the web `_handle()` is allowed to write, derived from WEB_SLIDERS above
# so there's a single source of truth. Anything not in this set is rejected
# with a WARN log.
WEB_PARAM_WHITELIST = {s["key"] for s in WEB_SLIDERS}

# JSON-serialized config that gets injected into the HTML and read by the
# client-side JS to build the slider DOM.
_WEB_SLIDERS_JSON = json.dumps(WEB_SLIDERS)

_WEB_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>Titan Engine Remote</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#111;color:#eee;font-family:-apple-system,BlinkMacSystemFont,sans-serif;padding:16px;max-width:600px;margin:0 auto;padding-bottom:60px}
h1{font-size:20px;margin-bottom:16px;color:#aaa;text-align:center;letter-spacing:2px}
h2{font-size:12px;color:#00ff66;margin:22px 0 10px;text-transform:uppercase;letter-spacing:1.5px;border-bottom:1px solid #222;padding-bottom:4px}
.status{background:#1a1a1a;border-radius:8px;padding:12px;margin-bottom:12px}
.row{display:flex;justify-content:space-between;align-items:center;padding:3px 0;font-size:14px}
.val{font-family:monospace;font-weight:bold}
.green{color:#00ff66}.yellow{color:#ff0}.red{color:#ff5555}.gray{color:#555}
.btn{display:block;width:100%;padding:20px;margin:8px 0;border-radius:10px;font-size:18px;font-weight:bold;border:none;cursor:pointer;transition:opacity .1s;-webkit-tap-highlight-color:transparent}
.btn:active{opacity:.7}
.btn-off{background:#222;color:#ccc;border:1px solid #333}
.btn-panic{background:#cc0000;color:#fff}
.btn-restore{background:#ff8800;color:#fff}
.btn-mute{background:#333;color:#ff8c00;border:1px solid #ff8c00}
.btn-muted{background:#ff8c00;color:#111}
.slider-row{margin:10px 0;background:#161616;border-radius:6px;padding:8px 10px}
.slider-row label{font-size:13px;color:#aaa;display:flex;justify-content:space-between;margin-bottom:4px}
.slider-row label .lv{color:#00ff66;font-family:monospace;font-weight:bold}
input[type=range]{width:100%;height:36px;accent-color:#00ff66;cursor:pointer}
.presets{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin:8px 0}
.pbtn{padding:14px 0;border-radius:6px;background:#1a1a1a;color:#555;border:1px solid #2a2a2a;font-size:13px;cursor:pointer;text-align:center;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;-webkit-tap-highlight-color:transparent}
.pbtn.live{background:#1a2a1a;color:#00ff66;border-color:#3a5a3a}
.pbtn:active{opacity:.7}
</style>
</head>
<body>
<h1>&#9889; TITAN ENGINE</h1>
<div class="status">
  <div class="row"><span>Audio</span><span class="val" id="s-audio">&#x2014;</span></div>
</div>
<button id="btn-panic" class="btn btn-off" onclick="sendCmd('panic',!panic)">PANIC BLACKOUT</button>
<button id="btn-mute" class="btn btn-mute" onclick="sendCmd('mute',!mute)">MUTE</button>
<div id="slider-container"></div>
<h2>Presets</h2>
<div class="presets" id="pgrid"></div>
<script>
var panic=false,mute=false;
// Injected server-side; one entry per exposed slider.
var SLIDERS=__SLIDERS_JSON__;
// Per-key "don't overwrite me, I'm dragging" deadlines (ms since epoch).
var dragUntil={};
// Cache of last server value per key so unnecessary DOM writes are skipped.
var lastVal={};

// Log-scale helpers: slider travel 0..1000 maps to [min..max] logarithmically
// so hip/lop (20..20000) are usable on a phone.
function toSliderPos(cfg,v){
  if(cfg.log){
    var lmin=Math.log(cfg.min),lmax=Math.log(cfg.max);
    return Math.round(1000*(Math.log(Math.max(cfg.min,v))-lmin)/(lmax-lmin));
  }
  return Math.round(1000*(v-cfg.min)/(cfg.max-cfg.min));
}
function fromSliderPos(cfg,p){
  if(cfg.log){
    var lmin=Math.log(cfg.min),lmax=Math.log(cfg.max);
    return Math.exp(lmin+(p/1000)*(lmax-lmin));
  }
  return cfg.min+(p/1000)*(cfg.max-cfg.min);
}
function snap(cfg,v){
  var s=cfg.step||0.01;
  return Math.round(v/s)*s;
}

function sendCmd(cmd,val,key){
  var body={cmd:cmd,value:val};
  if(key)body.key=key;
  fetch('/api/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
  .then(r=>r.json()).then(apply).catch(function(){});
}

function buildSliders(){
  var cont=document.getElementById('slider-container');
  var lastSection='';
  var html='';
  for(var i=0;i<SLIDERS.length;i++){
    var c=SLIDERS[i];
    if(c.section!==lastSection){
      html+='<h2>'+c.section+'</h2>';
      lastSection=c.section;
    }
    html+='<div class="slider-row" id="row-'+c.key+'">'
       +'<label><span>'+c.label+'</span><span class="lv" id="lv-'+c.key+'">\u2014</span></label>'
       +'<input type="range" id="sl-'+c.key+'" min="0" max="1000" step="1" value="0" '
       +'data-key="'+c.key+'" data-idx="'+i+'"></div>';
  }
  cont.innerHTML=html;
  // Wire up each slider's input/change handlers. oninput fires during drag
  // for live label updates + drag-lock; onchange fires on release for the
  // network POST.
  for(var i=0;i<SLIDERS.length;i++){
    (function(cfg){
      var sl=document.getElementById('sl-'+cfg.key);
      var lv=document.getElementById('lv-'+cfg.key);
      // Per-key last-send timestamp for the 30Hz throttle on `input` events.
      var lastSent=0;
      sl.addEventListener('input',function(){
        var v=snap(cfg,fromSliderPos(cfg,+this.value));
        lv.textContent=eval(cfg.fmt);
        // Suppress server overwrites for 250ms after last user input —
        // well above the 33ms poll interval, well below human perception.
        var now=Date.now();
        dragUntil[cfg.key]=now+250;
        // Throttle `input`-driven POSTs to ~30Hz so the Python GUI
        // tracks the drag in real time instead of only on release.
        if(now-lastSent>=33){
          lastSent=now;
          sendCmd('param',v,cfg.key);
        }
      });
      sl.addEventListener('change',function(){
        // Always send the final value on release, even if it's within the
        // 33ms throttle window — guarantees the server ends up with the
        // value the user actually settled on.
        var v=snap(cfg,fromSliderPos(cfg,+this.value));
        sendCmd('param',v,cfg.key);
      });
    })(SLIDERS[i]);
  }
}

function apply(s){
  if(!s)return;
  panic=!!s.panic; mute=s.mute>0;
  var pb=document.getElementById('btn-panic');
  pb.className='btn '+(panic?'btn-restore':'btn-panic');
  pb.textContent=panic?'CLICK TO RESTORE':'PANIC BLACKOUT';
  var mb=document.getElementById('btn-mute');
  mb.className='btn '+(mute?'btn-muted':'btn-mute');
  mb.textContent=mute?'MUTED \u2014 TAP TO RESTORE':'MUTE';
  // Audio status: green LIVE when PD frames arrived < 500ms ago, red NO INPUT otherwise.
  var ael=document.getElementById('s-audio');
  ael.textContent=s.audio_ok?'\U0001F7E2 LIVE':'\U0001F534 NO INPUT';
  ael.className='val '+(s.audio_ok?'green':'red');
  // Update every slider from the server's param dict, unless the user is
  // currently dragging it (dragUntil check).
  var now=Date.now();
  if(s.params){
    for(var i=0;i<SLIDERS.length;i++){
      var c=SLIDERS[i];
      var v=s.params[c.key];
      if(v===undefined||v===null)continue;
      if((dragUntil[c.key]||0)>now)continue;
      if(lastVal[c.key]===v)continue;
      lastVal[c.key]=v;
      var sl=document.getElementById('sl-'+c.key);
      var lv=document.getElementById('lv-'+c.key);
      if(sl){
        var pos=toSliderPos(c,v);
        if(+sl.value!==pos)sl.value=pos;
      }
      if(lv){
        // eval is safe here — cfg.fmt is authored by us in Python, not user input.
        lv.textContent=(function(v){return eval(c.fmt);})(v);
      }
    }
  }
  // Presets grid.
  var pg=document.getElementById('pgrid');
  var html='';
  for(var i=1;i<=10;i++){
    var nm=s.presets&&s.presets[i]?s.presets[i].replace(/.*[\\\\/]/,'').replace('.json',''):i;
    var cls=s.presets&&s.presets[i]?' live':'';
    html+='<div class="pbtn'+cls+'" onclick="sendCmd(\\'preset\\','+i+')">'+nm+'</div>';
  }
  pg.innerHTML=html;
}

// Re-entrancy guard: if the previous poll hasn't finished (slow network,
// backgrounded tab), skip this tick instead of piling up in-flight requests.
var polling=false;
function poll(){
  if(polling)return;
  polling=true;
  fetch('/api/state').then(function(r){return r.json();}).then(function(s){
    polling=false; apply(s);
  }).catch(function(){polling=false;});
}
buildSliders();
// 33ms == ~30Hz. Matches Qt's refresh_logic QTimer in titan_gui.py so
// both windows update at the same cadence during drags.
poll(); setInterval(poll,33);
</script>
</body>
</html>"""

# Inject the slider config into the HTML template. Done after the literal so
# the JSON doesn't have to survive Python's triple-quote string escapes.
_WEB_HTML = _WEB_HTML.replace("__SLIDERS_JSON__", _WEB_SLIDERS_JSON)


def start_web_server(port=9000):
    """Start a minimal HTTP remote-control server. No external deps — stdlib only."""
    def _make_handler(params, app_state, notify_pd):
        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt, *args): pass  # suppress per-request stdout noise

            def do_GET(self):
                try:
                    if self.path in ('/', '/index.html'):
                        self._html(_WEB_HTML)
                    elif self.path == '/api/state':
                        self._json(self._state())
                    else:
                        self.send_response(404); self.end_headers()
                except Exception:
                    logger.exception(f"Web GET {self.path} failed")
                    try:
                        self.send_response(500); self.end_headers()
                    except Exception:
                        pass

            def do_POST(self):
                try:
                    if self.path == '/api/command':
                        n = int(self.headers.get('Content-Length', 0))
                        body = json.loads(self.rfile.read(n))
                        logger.info(f"Web remote cmd: {body}")
                        self._handle(body)
                        self._json(self._state())
                    else:
                        self.send_response(404); self.end_headers()
                except Exception:
                    logger.exception(f"Web POST {self.path} failed")
                    try:
                        self.send_response(500); self.end_headers()
                    except Exception:
                        pass

            def _html(self, text):
                data = text.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', len(data))
                self.end_headers(); self.wfile.write(data)

            def _json(self, obj):
                data = json.dumps(obj).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', len(data))
                self.end_headers(); self.wfile.write(data)

            def _state(self):
                pm = app_state.get("preset_map", {})
                # Audio is considered live if PD emitted an /audio/bands frame
                # within the last 500 ms. compute_audio_thread writes
                # pd_last_time = time.time() on every incoming frame.
                audio_ok = (time.time() - app_state.get("pd_last_time", 0)) < 0.5
                # Snapshot every param the web UI renders a slider for.
                # Single pass, so no chance of partial/torn reads across
                # sliders when used with the drag-lock on the client.
                web_params = {k: params.get(k) for k in WEB_PARAM_WHITELIST}
                return {
                    "mute": int(params.get("mute", 0)),
                    "panic": bool(app_state.get("panic_blackout", False)),
                    "audio_ok": audio_ok,
                    "params": web_params,
                    "presets": {int(k): str(v) for k, v in pm.items()},
                }

            def _handle(self, body):
                cmd, val = body.get("cmd"), body.get("value")
                # `web_sync_latch` = True tells the Qt refresh_logic to push
                # the new params into the visual sliders/spinboxes on its
                # next 33 ms tick. Set by every mutating branch below so the
                # main GUI stays in lockstep with the web remote.
                if cmd == "mute":
                    params["mute"] = 1 if val else 0
                    # notify_pd whitelists bare names (no leading slash) and
                    # prepends the slash itself. Passing "/mute" caused the
                    # whitelist check `if name in PD_INIT_PARAMS` to fail
                    # silently, so PD never actually muted.
                    notify_pd("mute", int(bool(val)))
                    app_state["web_sync_latch"] = True
                elif cmd == "panic":
                    app_state["panic_blackout"] = bool(val)
                    app_state["web_sync_latch"] = True
                elif cmd == "dimmer":
                    # Legacy 0-255 dimmer path (kept for backward compat with
                    # older cached versions of the HTML). New HTML uses the
                    # generic `param` command with key="master_inhibitive".
                    params["master_inhibitive"] = max(0.0, min(1.0, int(val) / 255.0))
                    app_state["web_sync_latch"] = True
                elif cmd == "param":
                    key = body.get("key")
                    if key not in WEB_PARAM_WHITELIST:
                        logger.warning(f"Web remote: rejected param write to {key!r} (not in whitelist)")
                        return
                    # Clamp to the slider's declared min/max so a malformed
                    # client can't push e.g. input_trim=999 or hip=-5.
                    cfg = next((s for s in WEB_SLIDERS if s["key"] == key), None)
                    if cfg is None:
                        return
                    try:
                        v = float(val)
                    except (TypeError, ValueError):
                        logger.warning(f"Web remote: bad value for {key!r}: {val!r}")
                        return
                    v = max(cfg["min"], min(cfg["max"], v))
                    # Preserve int semantics for step>=1 integer params
                    # (smooth_size, floor, ceiling, hip, lop, atk_*, rel_*).
                    params[key] = int(round(v)) if cfg.get("step", 0.01) >= 1 else v
                    # Forward to PD if it's on the init-params whitelist
                    # (hip, lop, env, test_freq, test_db, input_trim, mute).
                    # notify_pd internally filters by PD_INIT_PARAMS so this
                    # is safe to call for every key.
                    notify_pd(key, params[key])
                    app_state["web_sync_latch"] = True
                elif cmd == "preset":
                    f = app_state.get("preset_map", {}).get(int(val))
                    if f:
                        app_state["pending_preset"] = f
                        app_state["web_sync_latch"] = True
                else:
                    logger.warning(f"Web remote: unknown command {cmd!r}")
        return _Handler

    class _Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    def _run():
        handler = _make_handler(params, app_state, notify_pd)
        try:
            with _Server(("", port), handler) as httpd:
                local_ip = get_local_ip()
                logger.info(f"Web remote UI: http://{local_ip}:{port}  (also http://localhost:{port})")
                app_state["web_port"] = port
                app_state["web_ip"] = local_ip
                httpd.serve_forever()
        except OSError as e:
            logger.warning(f"Web server could not start on port {port}: {e}")

    threading.Thread(target=_run, daemon=True, name="WebServer").start()


def _send_universe(out_u, payload, dest_ip, target_port, is_sacn, *,
                   priority, src_name, is_preview, art_net_val, art_sub_val):
    """Build the protocol packet for `out_u` and ship it. Returns (ok, err_str)."""
    if is_sacn:
        app_state["sacn_seq"][out_u] = (app_state["sacn_seq"].get(out_u, 0) + 1) % 256
        packet = build_sacn_packet(out_u, payload, app_state["sacn_seq"][out_u],
                                   priority, src_name, is_preview)
    else:
        packet = _build_artnet_packet(out_u, payload, art_net_val, art_sub_val)

    try:
        net_sock.sendto(packet, (dest_ip, target_port))
        return True, None
    except Exception as e:
        return False, str(e)


def sender_thread():
    logger.info("DMX sender thread running.")
    while True:
        try:
            job = send_queue.get()
            cfg = job["cfg"]
            buffers = job["buffers"]
            fb_payload = job["fb_payload"]

            is_sacn = cfg["is_sacn"]
            target_port = cfg["target_port"]
            base_ip = cfg["base_ip"]
            net_mode = cfg["net_mode"]
            priority = cfg["priority"]
            src_name = cfg["src_name"]
            is_preview = cfg["is_preview"]
            art_net_val = cfg["art_net_val"]
            art_sub_val = cfg["art_sub_val"]
            offset = cfg["offset"]

            for u, payload in buffers.items():
                # The QLC+ "1 vs 0" compatibility shift subtracts `offset` from
                # the patched universe at send time. Without a guard, U0 and U1
                # both clamp to wire-universe 0 and the second one silently
                # overwrites the first. Skip the underflow case and warn once.
                if offset and u < offset:
                    key = (offset, u)
                    if key not in app_state["offset_warned"]:
                        app_state["offset_warned"].add(key)
                        logger.warning(
                            f"Universe {u} cannot be sent while artnet_offset={offset} is enabled "
                            f"(would collide with universe {offset} on wire-universe 0). "
                            f"Re-patch this fixture to U{offset} or higher, or disable the offset."
                        )
                    continue

                dest_ip = _dest_ip_for(u, net_mode, is_sacn, base_ip)
                out_u = u - offset
                ok, err = _send_universe(out_u, payload, dest_ip, target_port, is_sacn,
                                         priority=priority, src_name=src_name, is_preview=is_preview,
                                         art_net_val=art_net_val, art_sub_val=art_sub_val)
                if ok:
                    app_state["art_packets"] += 1
                    app_state["send_error"] = None
                else:
                    app_state["send_error"] = err
                    logger.warning(f"Network Blocked! Reason: {err} | Target IP: '{dest_ip}'")

            fb_univ = 14
            fb_dest = _dest_ip_for(fb_univ, net_mode, is_sacn, base_ip)
            fb_univ_out = max(0, fb_univ - offset)
            ok, err = _send_universe(fb_univ_out, fb_payload, fb_dest, target_port, is_sacn,
                                     priority=priority, src_name=src_name, is_preview=is_preview,
                                     art_net_val=art_net_val, art_sub_val=art_sub_val)
            if not ok:
                app_state["send_error"] = err

        except Exception:
            logger.exception("sender_thread error")


def handle_audio(unused_addr, total_db, bass_db, treble_db):
    """OSC receive callback — stays minimal so the UDP socket never backs up."""
    # Auto-calibration tap: the AudioCalibrator only buffers samples while a
    # calibration phase is active. feed() is a no-op in the idle/done states.
    if calibrator.is_capturing:
        calibrator.feed(total_db, bass_db, treble_db)

    now = time.time()
    app_state["pd_last_time"] = now
    app_state["audio_frame_count"] += 1
    if now - app_state["last_fps_time"] >= 1.0:
        app_state["current_fps"] = app_state["audio_frame_count"] / (now - app_state["last_fps_time"])
        app_state["audio_frame_count"] = 0
        app_state["last_fps_time"] = now

    try:
        audio_queue.put_nowait((total_db, bass_db, treble_db))
    except queue.Full:
        try:
            audio_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            audio_queue.put_nowait((total_db, bass_db, treble_db))
        except queue.Full:
            pass


_DMX_MAX_HZ = 44          # Art-Net / sACN hard ceiling per the spec
_MIN_FRAME_INTERVAL = 1.0 / _DMX_MAX_HZ   # ~22.7 ms
_last_compute_time = 0.0  # module-level so the thread function can update it


def compute_audio_thread():
    """Drains audio_queue, runs DSP, builds the send job, queues it for the sender.

    Frames that arrive faster than 44 Hz are silently dropped here — PD can
    produce 80-90 fps when env is small (e.g. 512 samples), but the DMX wire
    protocol can't consume more than 44 frames/s, so processing the extra
    frames only wastes CPU.
    """
    global _last_compute_time
    while True:
        try:
            total_db, bass_db, treble_db = audio_queue.get()

            now = time.time()

            # Rate-limit to 44 Hz: skip frames that arrive too quickly.
            if now - _last_compute_time < _MIN_FRAME_INTERVAL:
                continue
            _last_compute_time = now
            if now - app_state.get("preset_text_time", 0) < 3.0:
                pass
            elif now - app_state.get("last_ctrl_time", 0) < 3.5:
                if int(params.get("remote_on", 1)) == 0:
                    app_state["osc_in_text"] = "⚫ IGNORED (Lock ON)"
                else:
                    app_state["osc_in_text"] = "🟢 RX (DMX)"
            else:
                app_state["osc_in_text"] = "🔴 WAIT"

            buffers = engine.process_audio(total_db, bass_db, treble_db, params)

            if app_state.get("panic_blackout"):
                zero = bytes(512)
                for u in buffers:
                    buffers[u] = zero

            if now - app_state["packet_reset_time"] > 60.0:
                app_state["art_packets"] = 0
                app_state["packet_reset_time"] = now

            if not app_state["artnet_active"]:
                continue

            protocol = params.get("protocol", "Art-Net")
            is_sacn = protocol == "sACN"
            cfg = {
                "is_sacn": is_sacn,
                "net_mode": params.get("net_mode", "Unicast"),
                "base_ip": str(params.get("art_ip", "127.0.0.1")).strip(),
                "target_port": 5568 if is_sacn else int(params.get("art_port", 6454)),
                "priority": int(params.get("sacn_priority", 100)),
                "src_name": str(params.get("sacn_src", "Titan Engine")),
                "is_preview": int(params.get("sacn_preview", 0)) == 1,
                "art_net_val": int(params.get("art_net", 0)),
                "art_sub_val": int(params.get("art_sub", 0)),
                "offset": 1 if int(params.get("artnet_offset", 1)) == 1 else 0,
            }

            fb_payload = [0] * 512
            fb_payload[0] = int(float(params.get("master_inhibitive", 1.0)) * 255)
            fb_payload[1] = int(params.get("color_r", 255.0))
            fb_payload[2] = int(params.get("color_g", 255.0))
            fb_payload[3] = int(params.get("color_b", 255.0))
            fb_payload[4] = int(params.get("color_w", 0.0))

            fb_payload[5] = 255 if int(params.get("master_ana_force_on", 0)) else 0
            fb_payload[6] = 255 if int(params.get("master_digi_force_on", 0)) else 0
            fb_payload[7] = 255 if int(params.get("master_od_force_on", 0)) else 0

            fb_payload[8] = int(((params.get("skew", 0.0) + 1.0) / 2.0) * 255.0)
            fb_payload[9] = int(params.get("width", 1.0) * 255.0)
            fb_payload[10] = int(params.get("glitch_ana_amt", 0.0) * 255.0)
            fb_payload[11] = int(params.get("glitch_digi_amt", 0.0) * 255.0)
            fb_payload[12] = int(params.get("od_glitch", 0.0) * 255.0)

            fb_payload[13] = int(float(params.get("bg_dimmer", 1.0)) * 255)
            fb_payload[14] = int(params.get("bg_r", 0.0))
            fb_payload[15] = int(params.get("bg_g", 0.0))
            fb_payload[16] = int(params.get("bg_b", 0.0))
            fb_payload[17] = int(params.get("bg_w", 0.0))

            num_fixes = int(params.get("num_fixtures", 1))
            for f_idx in range(num_fixes):
                base_ch = 18 + (f_idx * 13)
                if base_ch + 12 < 512:
                    fix_num = f_idx + 1
                    fb_payload[base_ch + 0] = int(float(params.get(f"f{fix_num}_dimmer", 1.0)) * 255)
                    fb_payload[base_ch + 1] = int(params.get(f"f{fix_num}_color_r", params.get("color_r", 255.0)))
                    fb_payload[base_ch + 2] = int(params.get(f"f{fix_num}_color_g", params.get("color_g", 255.0)))
                    fb_payload[base_ch + 3] = int(params.get(f"f{fix_num}_color_b", params.get("color_b", 255.0)))
                    fb_payload[base_ch + 4] = int(params.get(f"f{fix_num}_color_w", params.get("color_w", 0.0)))
                    fb_payload[base_ch + 5] = 255 if int(params.get(f"f{fix_num}_glitch_ana", 0)) else 0
                    fb_payload[base_ch + 6] = 255 if int(params.get(f"f{fix_num}_glitch_digi", 0)) else 0
                    fb_payload[base_ch + 7] = 255 if int(params.get(f"f{fix_num}_od_en", 0)) else 0

                    fb_payload[base_ch + 8] = int(
                        float(params.get(f"f{fix_num}_bg_dimmer", params.get("bg_dimmer", 1.0))) * 255)
                    fb_payload[base_ch + 9] = int(params.get(f"f{fix_num}_bg_r", params.get("bg_r", 0.0)))
                    fb_payload[base_ch + 10] = int(params.get(f"f{fix_num}_bg_g", params.get("bg_g", 0.0)))
                    fb_payload[base_ch + 11] = int(params.get(f"f{fix_num}_bg_b", params.get("bg_b", 0.0)))
                    fb_payload[base_ch + 12] = int(params.get(f"f{fix_num}_bg_w", params.get("bg_w", 0.0)))

            fb_payload[510] = 0  # (Previously Base Mix)

            if app_state.get("panic_blackout"):
                fb_payload = [0] * 512

            job = {"buffers": buffers, "fb_payload": fb_payload, "cfg": cfg}
            try:
                send_queue.put_nowait(job)
            except queue.Full:
                try:
                    send_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    send_queue.put_nowait(job)
                except queue.Full:
                    pass

        except Exception:
            logger.exception("compute_audio_thread error")


def get_preset_slot(val):
    if val < 26:
        return 0
    elif val < 51:
        return 1
    elif val < 76:
        return 2
    elif val < 101:
        return 3
    elif val < 126:
        return 4
    elif val < 151:
        return 5
    elif val < 176:
        return 6
    elif val < 201:
        return 7
    elif val < 226:
        return 8
    elif val < 251:
        return 9
    else:
        return 10


def process_control_universe(dmx):
    last_dmx = app_state.get("last_ctrl_dmx", [0] * 512)

    if time.time() - app_state.get("gui_lock_time", 0) < 1.5:
        app_state["last_ctrl_dmx"] = list(dmx)
        return

    app_state["last_ctrl_time"] = time.time()
    num_fixes = int(params.get("num_fixtures", 1))

    if len(dmx) >= 18:
        if dmx[0] != last_dmx[0]: params["master_inhibitive"] = dmx[0] / 255.0
        if dmx[1] != last_dmx[1]: params["color_r"] = float(dmx[1])
        if dmx[2] != last_dmx[2]: params["color_g"] = float(dmx[2])
        if dmx[3] != last_dmx[3]: params["color_b"] = float(dmx[3])
        if dmx[4] != last_dmx[4]: params["color_w"] = float(dmx[4])

        if dmx[5] != last_dmx[5]:
            params["master_ana_force_off"] = 1 if 0 < dmx[5] < 85 else 0
            params["master_ana_force_on"] = 1 if dmx[5] > 170 else 0
        if dmx[6] != last_dmx[6]:
            params["master_digi_force_off"] = 1 if 0 < dmx[6] < 85 else 0
            params["master_digi_force_on"] = 1 if dmx[6] > 170 else 0
        if dmx[7] != last_dmx[7]:
            params["master_od_force_off"] = 1 if 0 < dmx[7] < 85 else 0
            params["master_od_force_on"] = 1 if dmx[7] > 170 else 0

        if dmx[8] != last_dmx[8]: params["skew"] = ((dmx[8] / 255.0) * 2.0) - 1.0
        if dmx[9] != last_dmx[9]: params["width"] = dmx[9] / 255.0
        if dmx[10] != last_dmx[10]: params["glitch_ana_amt"] = dmx[10] / 255.0
        if dmx[11] != last_dmx[11]: params["glitch_digi_amt"] = dmx[11] / 255.0
        if dmx[12] != last_dmx[12]: params["od_glitch"] = dmx[12] / 255.0

        if dmx[13] != last_dmx[13]: params["bg_dimmer"] = dmx[13] / 255.0
        if dmx[14] != last_dmx[14]: params["bg_r"] = float(dmx[14])
        if dmx[15] != last_dmx[15]: params["bg_g"] = float(dmx[15])
        if dmx[16] != last_dmx[16]: params["bg_b"] = float(dmx[16])
        if dmx[17] != last_dmx[17]: params["bg_w"] = float(dmx[17])

    for f_idx in range(num_fixes):
        base_ch = 18 + (f_idx * 13)
        fix_num = f_idx + 1
        if len(dmx) > base_ch + 12:
            if dmx[base_ch] != last_dmx[base_ch]: params[f"f{fix_num}_dimmer"] = dmx[base_ch] / 255.0
            if dmx[base_ch + 1] != last_dmx[base_ch + 1]: params[f"f{fix_num}_color_r"] = float(dmx[base_ch + 1])
            if dmx[base_ch + 2] != last_dmx[base_ch + 2]: params[f"f{fix_num}_color_g"] = float(dmx[base_ch + 2])
            if dmx[base_ch + 3] != last_dmx[base_ch + 3]: params[f"f{fix_num}_color_b"] = float(dmx[base_ch + 3])
            if dmx[base_ch + 4] != last_dmx[base_ch + 4]: params[f"f{fix_num}_color_w"] = float(dmx[base_ch + 4])
            if dmx[base_ch + 5] != last_dmx[base_ch + 5]: params[f"f{fix_num}_glitch_ana"] = 1 if dmx[
                                                                                                      base_ch + 5] > 127 else 0
            if dmx[base_ch + 6] != last_dmx[base_ch + 6]: params[f"f{fix_num}_glitch_digi"] = 1 if dmx[
                                                                                                       base_ch + 6] > 127 else 0
            if dmx[base_ch + 7] != last_dmx[base_ch + 7]: params[f"f{fix_num}_od_en"] = 1 if dmx[
                                                                                                 base_ch + 7] > 127 else 0

            if dmx[base_ch + 8] != last_dmx[base_ch + 8]: params[f"f{fix_num}_bg_dimmer"] = dmx[base_ch + 8] / 255.0
            if dmx[base_ch + 9] != last_dmx[base_ch + 9]: params[f"f{fix_num}_bg_r"] = float(dmx[base_ch + 9])
            if dmx[base_ch + 10] != last_dmx[base_ch + 10]: params[f"f{fix_num}_bg_g"] = float(dmx[base_ch + 10])
            if dmx[base_ch + 11] != last_dmx[base_ch + 11]: params[f"f{fix_num}_bg_b"] = float(dmx[base_ch + 11])
            if dmx[base_ch + 12] != last_dmx[base_ch + 12]: params[f"f{fix_num}_bg_w"] = float(dmx[base_ch + 12])

    preset_ch = int(params.get("preset_ch", 512)) - 1
    if len(dmx) > preset_ch:
        val = dmx[preset_ch]
        last_val = last_dmx[preset_ch] if len(last_dmx) > preset_ch else 0
        slot = get_preset_slot(val)
        last_slot = get_preset_slot(last_val)

        if slot != 0 and slot != last_slot:
            preset_file = app_state.get("preset_map", {}).get(str(slot))
            if preset_file:
                app_state["pending_preset"] = preset_file
                app_state["osc_in_text"] = f"🟢 SLOT {slot}: {preset_file}"
                app_state["preset_text_time"] = time.time()
                logger.info(f"Loaded preset slot {slot}: {preset_file}")

    app_state["last_ctrl_dmx"] = list(dmx)


def artnet_listener_thread():
    while True:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        # --- Tell macOS to share the port with QLC+ ---
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if sys.platform == "darwin":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)

        try:
            sock.bind(("", 6454))
            if app_state.get("port_conflict_artnet"):
                logger.info("Port 6454 freed! Resuming DMX Control listener.")
            else:
                logger.info("Listening for DMX Control on Port 6454")

            app_state["port_conflict_artnet"] = False

            # --- Now actually listen for incoming packets ---
            while not app_state["port_conflict_artnet"]:
                data, addr = sock.recvfrom(1024)

                # Check if this is a valid Art-Net DMX packet
                if len(data) > 18 and data[0:8] == b'Art-Net\x00':
                    opcode = data[8] | (data[9] << 8)
                    if opcode == 0x5000:  # Art-DMX OpCode
                        # Extract the Universe (Little Endian bytes 14 & 15)
                        incoming_univ = data[14] | (data[15] << 8)

                        # Only process if it matches our Control Universe
                        if incoming_univ == int(params.get("ctrl_univ", 15)):
                            if int(params.get("remote_on", 1)) == 1:
                                dmx_data = list(data[18:])
                                process_control_universe(dmx_data)

        except OSError:
            if not app_state.get("port_conflict_artnet"):
                logger.error("Port 6454 conflict! DMX control listener blocked.")
            app_state["port_conflict_artnet"] = True
            time.sleep(2)
        finally:
            sock.close()


PD_INIT_PARAMS = ["hip", "lop", "env", "test_freq", "test_db",
                  "test_on", "sweep_on", "input_trim", "mute"]


def notify_pd(name, val):
    if name in PD_INIT_PARAMS:
        pd_client.send_message(f"/{name}", float(val))


def push_pd_init_params():
    """Re-send every PD-relevant parameter in the current `params` dict.

    PD's filter/oscillator/mute state resets whenever the Watchdog relaunches
    the engine (boot, audio device change, crash recovery). Without this push,
    the new PD process runs with patch-hardcoded defaults and the user's
    current GUI state diverges silently from what PD is actually doing — the
    symptom is "test tone plays nothing", "mute does nothing", or "switching
    input device kills audio until I wiggle a control".
    """
    for p in PD_INIT_PARAMS:
        try:
            pd_client.send_message(f"/{p}", float(params.get(p, 0)))
        except Exception as e:
            logger.warning(f"push_pd_init_params: failed to send /{p}: {e}")


def toggle_artnet():
    app_state["artnet_active"] = not app_state["artnet_active"]
    status = "ON" if app_state["artnet_active"] else "OFF"
    logger.info(f"Network Output toggled {status}")

def toggle_remote():
    params["remote_on"] = 0 if int(params.get("remote_on", 1)) == 1 else 1
    status = "ENABLED" if params["remote_on"] else "DISABLED"
    logger.info(f"DMX Remote Control {status}")


def update_ctrl_cache(ch, val):
    if len(app_state["last_ctrl_dmx"]) > ch: app_state["last_ctrl_dmx"][ch] = int(val)



watchdog = None


def cleanup_pd():
    logger.info("Shutting down Titan Brain...")
    if watchdog is not None:
        try:
            watchdog.stop_engine()
        except Exception as e:
            logger.warning(f"Watchdog stop_engine raised during shutdown: {e}")
    if sys.platform == "win32":
        os.system("taskkill /F /IM pd.exe 2>nul")
    else:
        os.system("pkill -9 -f audio_input.pd 2>/dev/null")
    logger.info("Shutdown complete. See you next time!")
    os._exit(0)  # <--- THIS IS CRITICAL


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.aboutToQuit.connect(cleanup_pd)

    # --- NEW: If PyCharm kills the app, drop the guillotine IMMEDIATELY ---
    signal.signal(signal.SIGINT, lambda sig, frame: cleanup_pd())
    signal.signal(signal.SIGTERM, lambda sig, frame: cleanup_pd())

    # Launch Pure Data invisibly via the Watchdog. The Watchdog handles zombie
    # sweeping on construction and applies the -nogui flag so no PD window
    # ever appears. If the user previously selected a specific audio input
    # device, pass that device_id in so the engine boots on the right hardware.
    watchdog = TitanWatchdog(pd_patch_name=os.path.join(BASE_DIR, "audio_input.pd"))

    def _coerce_dev_id(v):
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    saved_dev_int = _coerce_dev_id(params.get("pd_audio_dev"))
    saved_dev_out_int = _coerce_dev_id(params.get("pd_audio_dev_out"))
    watchdog.start_engine(device_id=saved_dev_int, output_device_id=saved_dev_out_int)

    disp = dispatcher.Dispatcher()
    disp.map("/audio/bands", handle_audio)


    def run_osc_server():
        while True:
            try:
                server = osc_server.ThreadingOSCUDPServer(("127.0.0.1", int(params["osc_in_port"])), disp)
                if app_state.get("port_conflict_osc"):
                    logger.info(f"Port {params['osc_in_port']} freed! Resuming Audio Data listener.")
                else:
                    logger.info(f"Listening for Audio Data on Port {params['osc_in_port']}")

                app_state["port_conflict_osc"] = False
                server.serve_forever()

            except OSError:
                if not app_state.get("port_conflict_osc"):
                    logger.error(f"Port {params['osc_in_port']} conflict! Audio input listener blocked.")
                app_state["port_conflict_osc"] = True
                time.sleep(2)


    audio_thread = threading.Thread(target=run_osc_server, daemon=True)
    audio_thread.start()

    compute_thread = threading.Thread(target=compute_audio_thread, daemon=True, name="AudioCompute")
    compute_thread.start()

    artnet_ctrl_thread = threading.Thread(target=artnet_listener_thread, daemon=True)
    artnet_ctrl_thread.start()

    dmx_sender_thread = threading.Thread(target=sender_thread, daemon=True, name="DMXSender")
    dmx_sender_thread.start()

    web_port = int(params.get("web_port", 9000))
    start_web_server(port=web_port)

    callbacks = {
        "send_osc": notify_pd,
        "toggle_artnet": toggle_artnet,
        "toggle_remote": toggle_remote,
        "update_ctrl_cache": update_ctrl_cache,
        "watchdog": watchdog,
        "push_pd_init": push_pd_init_params,
        "artpoll_scan": artpoll_scan,
        # Auto-calibration — the GUI drives phase start/stop and reads
        # snapshot() for progress UI.
        "calibrator": calibrator,
    }
    gui = TitanQtGUI(params, slider_cfg, engine, app_state, callbacks)

    # Push init params shortly after launch so PD has opened its OSC listener
    # by the time the messages arrive (PD's own loadbang + `delay 500` enables
    # DSP ~500 ms after the patch loads; we wait a bit longer to be safe).
    from PySide6.QtCore import QTimer
    QTimer.singleShot(1500, push_pd_init_params)

    sys.exit(app.exec())