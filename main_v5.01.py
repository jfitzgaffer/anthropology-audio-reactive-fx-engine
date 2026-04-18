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

from pythonosc import dispatcher, osc_server, udp_client
from PySide6.QtWidgets import QApplication
from titan_engine import RenderEngine
from titan_gui import TitanQtGUI
from titan_watchdog import TitanWatchdog

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
    # Panic Blackout flag. Toggled by the GUI's PANIC BLACKOUT button.
    # When True, sender_thread overwrites every outgoing payload with zeros
    # so fixtures actively go dark — we can't just halt transmission because
    # most DMX consoles hold their last frame when the packet stream stops.
    "panic_blackout": False,
}

engine = RenderEngine()
net_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
net_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
net_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
try:
    net_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
except OSError as e:
    logger.warning(f"Could not set IP_MULTICAST_TTL on net_sock: {e}. sACN multicast may be limited to local link.")

pd_client = udp_client.SimpleUDPClient("127.0.0.1", 5006)

send_queue = queue.Queue(maxsize=1)


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
    app_state["pd_last_time"] = time.time()

    app_state["audio_frame_count"] += 1
    now = time.time()
    if now - app_state["last_fps_time"] >= 1.0:
        app_state["current_fps"] = app_state["audio_frame_count"] / (now - app_state["last_fps_time"])
        app_state["audio_frame_count"] = 0
        app_state["last_fps_time"] = now

    if now - app_state.get("preset_text_time", 0) < 3.0:
        pass
    elif now - app_state.get("last_ctrl_time", 0) < 3.5:
        # We are actively receiving DMX! Are we listening or ignoring?
        if int(params.get("remote_on", 1)) == 0:
            app_state["osc_in_text"] = "⚫ IGNORED (Lock ON)"
        else:
            app_state["osc_in_text"] = "🟢 RX (DMX)"
    else:
        app_state["osc_in_text"] = "🔴 WAIT"

    buffers = engine.process_audio(total_db, bass_db, treble_db, params)

    # Panic Blackout: zero the engine's published buffers in-place so BOTH
    # the GUI's DMX grid preview (which reads engine.get_snapshot()) and
    # the network sender (which reuses this same dict reference) see the
    # blackout. We don't halt transmission — consoles would hold the last
    # rendered frame; we actively blast zeros so fixtures go dark.
    if app_state.get("panic_blackout"):
        zero = bytes(512)
        for u in buffers:
            buffers[u] = zero

    if time.time() - app_state["packet_reset_time"] > 60.0:
        app_state["art_packets"] = 0
        app_state["packet_reset_time"] = time.time()

    if app_state["artnet_active"]:
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

        # Panic Blackout also flattens the fallback universe (U14), which
        # carries the control/master layer. Done here rather than skipping
        # the build above so one flag covers every outgoing channel.
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

    artnet_ctrl_thread = threading.Thread(target=artnet_listener_thread, daemon=True)
    artnet_ctrl_thread.start()

    dmx_sender_thread = threading.Thread(target=sender_thread, daemon=True, name="DMXSender")
    dmx_sender_thread.start()

    callbacks = {
        "send_osc": notify_pd,
        "toggle_artnet": toggle_artnet,
        "toggle_remote": toggle_remote,
        "update_ctrl_cache": update_ctrl_cache,
        # The GUI's Audio tab uses this to populate the device dropdown and
        # to relaunch PD when the user picks a different interface.
        "watchdog": watchdog,
        # Called after any PD relaunch (device change) to re-sync filter,
        # oscillator, and mute state from `params` into the fresh PD process.
        "push_pd_init": push_pd_init_params,
    }
    gui = TitanQtGUI(params, slider_cfg, engine, app_state, callbacks)

    # Push init params shortly after launch so PD has opened its OSC listener
    # by the time the messages arrive (PD's own loadbang + `delay 500` enables
    # DSP ~500 ms after the patch loads; we wait a bit longer to be safe).
    from PySide6.QtCore import QTimer
    QTimer.singleShot(1500, push_pd_init_params)

    sys.exit(app.exec())