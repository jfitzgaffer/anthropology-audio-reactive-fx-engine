# Titan Engine - Project Context & Rules

## Project Overview
Titan Engine is a standalone, audio-reactive DMX lighting engine built for macOS (Apple Silicon arm64). It listens to live microphone audio, processes it through a DSP engine, and outputs synchronized lighting data via Art-Net and sACN protocols. 

## Technology Stack
* **Language:** Python 3.14
* **GUI Framework:** PySide6 (Qt)
* **Audio DSP Engine:** Pure Data (Pd-0.55-0.app)
* **Networking:** `python-osc` (for internal DSP communication), native `socket` (for DMX output)
* **Plotting/Monitors:** `pyqtgraph`

## Core Architecture & Open Questions
We are currently using a **Watchdog Subprocess Architecture**, but I am completely open to architectural refactoring if a better, more professional path exists. 

**How it currently works:**
1. Python acts as the master controller and GUI.
2. The `TitanWatchdog` class (`titan_watchdog.py`) silently boots Pure Data in the background using `subprocess.Popen` with the `-nogui` flag.
3. Pure Data handles CoreAudio hardware interfacing and raw DSP math.
4. Pure Data transmits the calculated audio envelopes back to Python over localhost UDP (Port 5005) using OSC.
5. Python catches the OSC data, updates the PySide6 UI, applies user modulators, and translates it into 512-channel DMX universes broadcasted over UDP (Port 6454 / 5568).
6. **Lifecycle Safety:** Python aggressively assassinates the Pure Data subprocess (`killall -9 pd`) on startup and shutdown to prevent zombie CoreAudio locks.

**Why we built it this way (Context for Claude):**
* It isolated the Python GUI from audio driver crashes. If the USB mic unplugs, the Watchdog just kills the frozen Pure Data process and hot-swaps to a new one instantly without crashing the UI.
* We ran into issues trying to compile `libpd` (via `pylibpd`) natively on Apple Silicon (`arm64`), so the invisible subprocess was a pragmatic workaround.

**Your Role as Architect:** Please evaluate this approach. If you believe compiling `libpd` directly into Python (or taking another approach entirely) is better for a production-ready application, make your case. I want you to step back, look at the overall system, and help me decide if we should make foundational architectural changes before we proceed further.

## UI/UX & Coding Standards
* **The Dynamic Builder Pattern:** The PySide6 GUI (`titan_gui.py`) is massive. Always look for opportunities to condense repetitive UI elements into dynamic layout builders (e.g., `_build_control_box()`). 
* **Dark Mode Native:** The UI relies heavily on deep grays (`#2b2b2b`, `#111111`) with vibrant neon accents (Green: `#00ff66`, Yellow: `#ffff00`, Red: `#ff5555`). Always maintain this styling when adding new widgets.
* **Non-Blocking Execution:** The GUI must remain highly responsive. All network listeners (OSC and Art-Net) must run on separate `threading.Thread` daemons. 
* **State Management:** The GUI uses a master `self.params` dictionary to track user settings, which is automatically saved/loaded to `titan_default.json`. UI elements must quietly sync to this dictionary without creating infinite feedback loops.

# Titan Engine — Project Notes & Fix List

## Project overview

Titan Engine is a custom audio-reactive DMX engine for film/stage lighting. Live audio enters via **Pure Data** (`audio_input.pd`), which performs band-split + RMS envelope extraction and emits the three-band level over OSC to a **Python** orchestrator. The Python side (`main_v5.01.py`) maps those levels through the `RenderEngine` into DMX pixel values, then transmits them over **Art-Net or sACN** to physical fixtures. A **PySide6/Qt GUI** (`titan_gui.py` + `titan_layout.ui`) provides live control and monitoring. A lighting console (QLC+, MA, etc.) can remote-control the engine by sending Art-Net back into the "Control Universe" (default U15).

**Primary user is a gaffer, not a programmer.** Favor clarity, safety rails, and graceful failure over clever abstractions. Never introduce a change that could blackout a live show without a user-visible confirmation.

## File map

| File | Role |
|---|---|
| `main_v5.01.py` | Orchestrator. OSC server, Art-Net/sACN sender, control-universe listener, signal handlers, watchdog boot. |
| `titan_engine.py` | `RenderEngine` — DSP/mapping from audio level → per-pixel DMX values. Called once per incoming audio frame. |
| `titan_gui.py` | `TitanQtGUI` — all UI wiring, preset/patch persistence, troubleshooter dialogs, DMX grid view. |
| `titan_widgets.py` | `FixturePatchWidget`, `DMXGridOverlay` — custom Qt widgets. |
| `titan_watchdog.py` | `TitanWatchdog` — launches/kills the headless Pure Data subprocess and scans audio devices. |
| `titan_layout.ui` | Qt Designer layout loaded at runtime via `QUiLoader`. |
| `audio_input.pd` | Pure Data patch for audio input, band-split (hip/lop), RMS→dB, OSC send. |

## Runtime topology

```
Audio hardware
   │  (PortAudio)
   ▼
Pure Data  (-nogui, headless, launched by Watchdog)
   │  OSC /audio/bands → 127.0.0.1:5005
   ▼
main_v5.01.py  handle_audio()            ← runs on OSC thread
   │  engine.process_audio()
   ▼
RenderEngine                             ← produces dmx_buffers[u] = bytearray(512)
   │  Art-Net / sACN packet build
   ▼
UDP socket → fixtures

Lighting console ──Art-Net→ UDP 6454 ──→ artnet_listener_thread ──→ process_control_universe()
                                                                          │
                                                                          └─> writes params[…]
```

## Known bugs (fix in this order)

### HIGH

8. **Thread-safety: none.** `handle_audio` on the OSC thread writes `engine.dmx_buffers` and `engine.scope_*` while the Qt thread reads them at 30 Hz in `refresh_logic`. GIL keeps it mostly OK but `deque`→`list(…)` can observe a half-updated deque. Add a `threading.Lock` around the buffer swap, or double-buffer.

9. **Audio processing runs on the OSC receive thread.** All DSP + all network sends happen inside `handle_audio`. Any `sendto()` stall blocks audio intake. Move network output to its own thread with a single-slot queue (latest frame wins).

10. **Hard-coded 44.1 kHz assumption.** `titan_engine.py` line 83 uses `env / 44100.0`. macOS default is 48 kHz → timings are ~8% off. Force PD sample rate with `-r 44100` on launch, or read the actual rate.

11. **`dmx_buffers` grows unbounded.** A typoed universe number (e.g. 9000) permanently lives in the dict and is cleared every frame. Cap at a sane max, or prune when fixtures change.

12. **`cmb_net_mode` gets garbage writes during protocol swap.** `titan_gui.py:1093` `_on_protocol_change` clears and repopulates the combo; each intermediate state fires `currentTextChanged` → `params["net_mode"]` takes transient bad values. Wrap in `blockSignals(True/False)`.

13. **Preset-load race on protocol/net_mode.** `titan_gui.py:1468`. Setting protocol clears net_mode items; the subsequent net_mode set silently fails because items aren't re-populated yet. Block signals, repopulate, then set.

### MEDIUM

14. **Noise gate has no hysteresis.** Engine line 62: hard threshold on `norm_level`. Signals riding the threshold chatter. Add separate open/close thresholds.

15. **Watchdog's zombie sweep is too broad.** `titan_watchdog.py:18` does `killall -9 pd`, which kills unrelated PD sessions. Use `pkill -f audio_input.pd`.

16. **`get_pd_audio_devices` relies on a 500 ms sleep.** Misses output on slow launches → "No devices found" in the dialog. Poll stdout with a timeout, or add `-nodac -noadc`.

17. **`signal.signal` with `sys.exit(0)` can crash Qt on SIGINT/SIGTERM.** Use `QApplication.quit()` and rely on `aboutToQuit`.

18. **Log file never rotates.** `main_v5.01.py:42`. Swap `FileHandler` for `RotatingFileHandler`.

19. **Double `chk.setChecked` block.** `titan_gui.py:1585–1588` and `1591–1594` are identical. Delete one.

20. **`_build_fixture_ui` assigns `f{i}_uni` twice** (lines 681 and 697). Dead assignment.

21. **PD default cutoffs disagree with Python defaults** (PD: hip 200/lop 2500; Python: hip 150/lop 3000). ~1 s mismatch window at boot until `resync_pd` fires. Align defaults or move the resync to fire after the first audio frame arrives.

22. **`get_local_ip`, `_on_adv_net_toggle`, `chk_sacn_preview` — dead code.** Remove or wire up.

23. **Duplicate imports in `titan_widgets.py`** lines 1–4 and 6–8.

## Inefficiencies / refactor targets

27. **Per-pixel Python loops in the engine.** Fine for small counts; for >64 pixels vectorize with `numpy` (attack/release, gamma, clamp).

28. **DMX buffer clear allocates.** `self.dmx_buffers[u][:] = b'\x00' * 512` creates a new bytes object. Keep a pre-allocated zero buffer, or use `ctypes.memset`.

29. **Feedback universe sent every frame** (`main_v5.01.py:287+`). Mostly static data. Send on change + a 1 Hz heartbeat.

30. **Pixel-mode DMX grid calls `setStyleSheet` on every cell every frame.** Very slow. Mirror the `last_rendered_dmx` diff-check used in Raw mode.

31. **`json.dump` of full params on audio-device change** runs on the GUI thread (`titan_gui.py:306`). Move to a background thread, or debounce.

32. **Bare `getattr(self.ui, …)` sweeps in `_link_widgets`/`apply_changes`** iterate the full param dict including `fN_*` keys. Cheap but wasteful; skip per-fixture keys.

## Feature backlog (prioritized for a non-technical operator)

- **Simple Mode vs Pro Mode.** Hide 80 % of sliders behind a toggle. Simple shows: Input, Floor (sensitivity), Color, Brightness, Speed, Response Style preset.
- **Auto-calibrate levels.** 20 s of audio → auto-set floor at 20th percentile, ceiling at 95th.
- **Input headroom meter** with green/yellow/red zones next to Input Trim.
- **Autosave restore prompt on next launch.** Write path is done (`titan_autosave.json` via 60 s `QTimer`); still need a startup check that offers to load the autosave if newer than the saved patch.
- **Preset thumbnails + friendly names** (add `description` field to preset JSON; small scope-shape PNG optional).
- **Keyboard shortcuts:** Space = mute, B = blackout, T = test tone, 1–9 = preset slot.
- **Virtual fixture preview strip** (extension of the existing DMX grid).
- **Undo / revert last slider** (Cmd-Z).
- **Art-Net discovery (`ArtPoll`)** — find consoles & fixtures on the subnet.
- **Show bundle** — single "Save Show As…" covering patch + presets + slot map.
- **Remote phone/tablet UI** (`aiohttp` + single HTML page): mute, blackout, preset, master dimmer.
- **Network Health tab:** pps sent, pps received, last error per destination, rolling latency graph.
- **Beat detection / tap tempo** for rhythmic preset cycling.
- **Record/replay DMX output** for venue tech checks without audio.

## Conventions & gotchas

- **Param dict is the single source of truth.** `params[…]` is written by the GUI, by the control-universe listener, and by preset loads. All three paths must stay consistent. If you add a new param, remember to add it to `slider_cfg` if it needs a slider, and to `notify_pd`'s whitelist if PD cares about it.
- **Universe offset.** `artnet_offset=1` is the QLC+ "1 vs 0" compatibility shift. It's applied at send-time, not patch-time. Don't double-apply.
- **`gui_lock_time`** is a 1.5 s window after any local slider move during which incoming remote DMX is ignored, to prevent the console from fighting the user. Don't remove.
- **`preset_mask_time`** freezes the engine at the current brightness for 250 ms during preset loads to avoid a black-flash. Preserve this behavior on any preset-load refactor.
- **The Control Universe map (U15 / channels 1–18 global + 13 per fixture) is documented in `titan_gui.py:1227`.** Keep the doc and the code in lockstep — users rely on it for QLC+ patching.
- **Pure Data is launched headless.** Don't assume a visible window for debugging; use the log pane.
- **Never remove a bare `except` without adding a `logger.warning(…)` in its place** — some of these are catching real errors that should be visible, but silently swallowing them is worse than crashing in production.

## Out-of-scope / do not touch without discussion

- The Art-Net and sACN packet layouts (`build_sacn_packet`, the Art-Net header bytes). These are spec-correct; refactor for deduplication, not for "cleanup".
- The 18-channel global + 13-per-fixture control-universe mapping. Changing this breaks every QLC+ show file users have saved.
- The `preset_ch` slot-thresholds in `get_preset_slot` (25-count buckets). Standard lighting convention; users depend on it.

## CRITICAL AGENT RULES: CODE MODIFICATION

* **Mandatory Self-Review:** After you edit any file, you MUST run `git diff` in the terminal to review your own changes *before* you declare the task finished.
* **The Retention Rule:** Look specifically at the red `-` lines in your diff. If you removed classes, imports, or functions that I did not explicitly ask you to remove, you must immediately revert or fix the file to restore them.
* **No Lazy Truncation:** Never use comments like `# ... rest of code here ...` to skip writing out the full file.

## CRITICAL AGENT RULES: VERSION CONTROL & DOCUMENTATION

* **The Changelog Mandate:** Before you finalize any task or execute a `git commit`, you MUST update the `CHANGELOG.md` file in the root directory. If the file does not exist, create it.
* **Changelog Formatting:** Use the standard "Keep a Changelog" format. Create a new header for your current task using the format `### [Current Date] - Task Summary`. Categorize your specific edits under bulleted lists labeled: `Added`, `Changed`, `Fixed`, or `Removed`.
* **Print the Changelog Entry in Chat:** Every time you update `CHANGELOG.md`, also print the new entry verbatim as plaintext in your chat response so I can read it without opening the file. Print only the entry you just added, not the entire changelog history.
* **Pre-Commit Analysis:** You must run `git diff --cached` (or `git diff` if unstaged) to mathematically analyze your exact changes before writing your commit message.
* **Exhaustive Commit Messages:** When you run `git commit`, you must use the `-m` flag to generate a comprehensive, multi-line commit message. The first line must be a concise summary. The following lines must be a detailed bulleted list of every feature, fix, and architectural shift you just analyzed in the diff.
* **Mandatory Testing Protocol:** After completing a task and committing the code, you MUST provide me with a clear, step-by-step guide on how to manually test your changes. Specify exactly which GUI elements to click, which sliders to move, or which terminal outputs/DMX channels to monitor to verify the new logic behaves perfectly.