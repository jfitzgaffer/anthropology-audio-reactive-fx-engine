# Changelog

All notable changes to Titan Engine are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Dates are ISO 8601.

## [Unreleased]

### [2026-04-24] - Panic fade, DMX lock popup, spinbox wheel lock, QR remote, device collision warning, port field removal

#### Added
- **Panic blackout now fades instead of cutting.** `app_state` gained
  `panic_level` / `panic_target` / `panic_last_tick`. `compute_audio_thread`
  slews `panic_level` toward `panic_target` each frame using the master fade
  timings — `dimmer_rel` ms for the ramp to black, `dimmer_atk` ms for the
  ramp back up — so the panic cue feels like a scripted dimmer fade instead
  of an abrupt cut. Every output byte (both the per-universe `buffers` and
  the control-universe `fb_payload`) is scaled by `panic_level`; we
  short-circuit when the level is near 1.0 so normal operation pays zero
  cost. Both the GUI button and the web-remote panic toggle drive the same
  state so the fade plays the same way from either origin.
- **DMX remote lock popup.** When DMX remote control is enabled, touching a
  DMX-driven slider/spinbox/checkbox now opens a dialog explaining that
  external DMX is in control and offering an inline *Enable DMX Remote
  Control* checkbox that toggles the lock without forcing the user to hunt
  through the Control menu. New `_DmxLockFilter` intercepts MouseButtonPress,
  MouseButtonDblClick, Wheel, and KeyPress events on locked widgets, swallows
  MouseMove/Release so drags don't register, and surfaces the popup.
  Re-entry guarded so rapid clicks don't stack dialogs.
- **Grayed-out rendering for DMX-locked widgets.** Rather than
  `setEnabled(False)` (which would block the click → popup handoff), each
  locked widget gets a `QGraphicsOpacityEffect` at 35% opacity. The effect
  is cached per-widget so toggling remote control on/off doesn't leak
  effect objects.
- **Scannable QR code for the Web Remote URL.** Clicking the blue
  `Web Remote: http://…` link in the Status box now opens a dialog
  containing a QR code the director can point a phone camera at. The QR is
  generated in-memory via the `qrcode` Python module (newly added
  dependency) and rendered as a `QPixmap`; when the module isn't importable
  the dialog gracefully falls back to plain text plus an install hint.
- **Audio device collision warning.** `_on_audio_device_changed` and
  `_on_audio_output_device_changed` now run `_check_audio_device_collision()`
  which warns via `QMessageBox.warning` when PD's input and output are
  pointed at the same physical device. De-duped per (in_id, out_id) pair so
  the modal doesn't re-open when flipping between other devices. The one
  legitimate use-case (BlackHole/Soundflower loopback) is called out in the
  message.

#### Fixed
- **Scroll wheel no longer nudges spinboxes either.** The wheel filter is
  now also installed on every `QSpinBox` and `QDoubleSpinBox` via
  `findChildren`, so hovering over a value and scrolling past it no longer
  changes the value. `StrongFocus` policy applied to all three widget
  families as a belt-and-braces backup.
- **Stale "Base Mix" caption removed from Dynamics tab.** The cleanup list
  in `__init__` was looking for `lbl_base_mix` while the Designer name is
  `label_base_mix`, so the caption was never actually hidden. Fixed by
  including both spellings in the hide list.

#### Changed
- **Art-Net port field removed from the GUI.** The Art-Net spec fixes the
  wire port at 6454 (and sACN at 5568); an editable field only ever caused
  support tickets. `spin_art_port` and its "Port:" label are hidden at
  runtime in `_setup_protocol_ui`, and `main_v5.01.py` now always sends to
  port 6454 regardless of any legacy value stored in
  `params["art_port"]`/autosave.

### [2026-04-24] - Slider scroll fix, 44 Hz FPS cap, independent calibration phases, Network Config tab

#### Fixed
- **Scroll wheel no longer accidentally moves sliders.** A `_SliderWheelFilter`
  event filter is installed on every `QSlider` in `_make_tabs_scrollable`.
  When the mouse wheel fires on a slider, the event is marked `ignored` and
  propagates up to the enclosing `QScrollArea`, which scrolls the pane as the
  user intended. Sliders still respond to wheel when they own keyboard focus
  (`StrongFocus` policy preserved).
- **Engine no longer wastes CPU at 80-90 Hz.** A smaller PD envelope window
  (`env=512`) makes Pure Data emit OSC frames faster than Art-Net / sACN can
  consume them (protocol hard ceiling: 44 Hz). `compute_audio_thread` now
  enforces a `1/44 s` minimum inter-frame interval and silently discards
  surplus frames. The DMX wire rate is unchanged; only redundant Python-side
  DSP calls are eliminated.
- **Auto Calibrate no longer suggests `env=512`**; the envelope window is left
  at the user's current setting to avoid triggering the above problem.

#### Changed
- **Calibration phases are now independent.** The single "Auto Calibrate"
  button is replaced by two side-by-side buttons in the Input Stage header:
  *📊 Measure Noise* and *🎙️ Voice & Apply*. Each phase can be triggered
  individually. Voice capture runs with a stored noise result if one exists;
  if not, it warns the user and falls back to a conservative 40 dB default so
  voice capture is never blocked by a missing noise measurement. A status
  label below the buttons shows the last noise floor reading.
- **"Control Universe" spinbox moved to Network Config tab.** Previously on
  the Remote Control tab alongside preset mappings, it now lives in the
  Network Output Settings grid with the other networking controls
  (protocol, mode, sACN priority, QLC+ offset). The original widget in
  the Remote Control tab is hidden; `_link_widgets` is redirected to the
  new widget automatically.
- **Output tab renamed to "Network Config".** The "5. Output" tab in
  `settings_tabs` is renamed to "5. Network Config" at runtime so the tab
  label matches its contents.

### [2026-04-20] - Auto-calibration wizard + scrollable settings panes + main-branch rule

#### Added
- **Two-phase Auto Calibrate wizard.** New button at the top of the
  "1. Input Stage" group walks the user through a silent noise
  measurement (6 s) followed by a voice measurement (12 s), then
  proposes values for `floor`, `ceiling`, `input_trim`, `noise_gate`,
  `hip`, `lop`, `expand`, `knee`, `env`, and `drive`. The analysis
  works in Pd's positive-dB `rmstodb` scale (noise floor at P90,
  voice ceiling at P99), auto-bumps `input_trim` when the voice peaks
  too low, and shifts floor/ceiling by the applied dB gain so the
  post-trim signal still lands in range. Bad runs (too few samples,
  std-dev > 8 dB on noise, fewer than 30% active voice frames)
  prompt the user to retry without losing the prior phase's data.
  Live progress dialogs show elapsed time, sample count, and current
  `total / bass / treble` dB readings. Applying the results writes
  into `params`, syncs every slider/spinbox, and pushes the PD-side
  `hip` / `lop` / `env` / `input_trim` over OSC so Pure Data picks
  them up immediately. Files: `titan_calibration.py` (new),
  `titan_gui.py::_setup_calibrate_button` and the `_cal_*` wizard
  methods, `main_v5.01.py` (construct one global `AudioCalibrator`,
  tap `handle_audio` with `calibrator.feed(...)`, expose via
  `callbacks["calibrator"]`).

#### Fixed
- **Input Stage sliders no longer compress or hide the Auto
  Calibrate button.** Every `QGroupBox` in `titan_layout.ui` has a
  hard-coded `maximumSize` height (120 or 300 px); adding the
  calibrate button pushed the Input Stage past its 300-px cap and
  Qt responded by squeezing every row inside. Fix in
  `titan_gui.py::_make_tabs_scrollable` (called at the end of
  `__init__`): drops the height cap on every `QGroupBox` via
  `setMaximumHeight(16777215)` and wraps each tab page under
  `settings_tabs` in a `QScrollArea` (widget-resizable, no frame).
  When the window is tall enough, panes size naturally; when it
  shrinks, a vertical scroll bar appears automatically so no
  control is ever hidden.

#### Changed
- **CLAUDE.md — new rule: main-branch default, no worktrees unless
  asked.** After a round-trip where code changes landed in a Claude
  worktree branch, had to be merged into `main`, and left PyCharm
  blind until the merge completed, the agent is now explicitly
  instructed to edit in the primary working directory on `main` by
  default. Worktrees are only used when the operator asks for one.

### [2026-04-20] - Web remote: panic/mute GUI sync + 30 Hz bidirectional tracking

#### Fixed
- **Panic and Mute buttons no longer lag the web remote.** Pressing
  PANIC BLACKOUT or MUTE in the browser updated `app_state` /
  `params` correctly (DMX output went dark, PD muted) but the Qt
  window's button face stayed in its old state — the user had no
  visual confirmation from the main rig computer that the command
  had landed. Root cause: the Qt button's `setText` /
  `setStyleSheet` / `setChecked` only ran inside their local click
  handlers, which the HTTP thread never invokes.
  Fix in `titan_gui.py::refresh_logic` (runs every 33 ms): mirror
  `app_state["panic_blackout"]` onto `self.btn_panic`
  (text + style) and `params["mute"]` onto `self.ui.chk_mute`
  (with `blockSignals` wrapping `setChecked` so the sync doesn't
  re-enter the mute-toggled handler and bounce the value back to
  PD). Cheap diff check on `_last_panic_visual` avoids redundant
  Qt paint ops on every tick.

#### Changed
- **Web-remote slider tracking is now ~30 Hz in both directions.**
  Previously sliders felt "choppy": finger drags on the phone only
  POSTed on release (`onchange`), and the browser only polled
  `/api/state` every 500 ms, so the Qt window updated in two big
  jumps per drag instead of a smooth sweep.
  In `main_v5.01.py::_WEB_HTML`:
    - Slider `input` listener now POSTs every value change,
      throttled per-key to 33 ms (≈30 Hz) via a `lastSent`
      timestamp — matches the Art-Net frame cadence and Qt's
      `refresh_logic` QTimer. The existing `change` listener
      still fires on release to guarantee the final settled
      value is authoritative even if it falls inside the throttle
      window.
    - Poll interval dropped from **500 ms → 33 ms** and wrapped
      in a re-entrancy guard (`polling` boolean) so slow networks
      or backgrounded tabs skip ticks instead of piling up
      in-flight fetches.
    - `dragUntil` suppression window reduced from **1000 ms →
      250 ms** — well above the new 33 ms poll interval, well
      below human reaction time, so polls no longer wait a full
      second after a drag ends before the web UI will accept a
      value from another control surface.

---

### [2026-04-20] - Fix UnicodeEncodeError on web page load (lone UTF-16 surrogates)

#### Fixed
- **`main_v5.01.py:469`** — every `GET /` to the web remote was returning
  500 because `_WEB_HTML.encode('utf-8')` raised
  `UnicodeEncodeError: 'utf-8' codec can't encode characters in
  position 8823-8824: surrogates not allowed`. The audio-status line
  used `'\ud83d\udfe2 LIVE'` and `'\ud83d\udd34 NO INPUT'` — the
  UTF-16 surrogate-pair encoding of 🟢 and 🔴. Python strings are
  Unicode codepoints, not UTF-16 code units, so `\ud83d` on its own
  is a *lone* high surrogate (U+D83D) — a reserved codepoint that's
  illegal in UTF-8 by design (RFC 3629 §3).
  Replaced both with the 8-hex-digit astral form: `\U0001F7E2` and
  `\U0001F534`. These resolve to single codepoints in the BMP
  overflow range (U+1F000..U+1FFFF) and encode cleanly as 4-byte
  UTF-8 sequences. Verified: HTML now encodes to 10 204 bytes
  without error; `ord()` on the emoji characters returns the real
  codepoint, not a surrogate.

---

### [2026-04-20] - Web remote: full slider bank + main-GUI sync + audio LIVE status

#### Added
- **`WEB_SLIDERS` config + dynamic slider bank in the web remote.** 21 new
  sliders covering every Audio Input, Audio Mapping, and Dynamics knob the
  gaffer is likely to want on a phone during a show:
    - Master: `master_inhibitive`
    - Audio Input: `input_trim`, `noise_gate`, `drive`, `floor`, `ceiling`,
      `expand`, `hip`, `lop`
    - Audio Mapping: `gamma`, `eq_tilt`, `knee`, `scale`
    - Dynamics: `atk_c`, `rel_c`, `atk_e`, `rel_e`, `time_gamma`,
      `jitter_thresh`, `jitter_amount`, `smooth_size`
  `WEB_SLIDERS` is a list of dicts in `main_v5.01.py` — one source of truth
  for section header, label, min/max, step, log-scale flag, and a JS
  formatter expression. It's `json.dumps`'d into the HTML template via a
  `__SLIDERS_JSON__` placeholder so the Python literal never has to quote
  a JS array by hand. The client builds the slider DOM from this array.
- **Log-scale mapping on `hip`, `lop`, `scale`, `atk_c`, `rel_c`, `atk_e`,
  `rel_e`.** Linear range inputs on 20..20000 Hz were unusable (90 % of
  the travel was >2 kHz); the JS now maps slider travel logarithmically
  for any entry with `log:true`.
- **Drag-lock (`dragUntil` map) on the client.** `oninput` sets a 1-second
  suppression per-key so the 500 ms `/api/state` poll doesn't fight the
  user's finger while they're dragging.
- **`"param"` command** in the web handler. `POST /api/command
  {"cmd":"param","key":"input_trim","value":2.5}`. Clamps to the
  slider's declared min/max, respects integer vs float semantics (step
  ≥ 1 → `int(round(v))`), and forwards to PD via `notify_pd()` which
  internally filters by `PD_INIT_PARAMS` so only `hip`, `lop`, `env`,
  `input_trim`, etc. actually leave the process.
- **`WEB_PARAM_WHITELIST`** (derived from `WEB_SLIDERS`) — anything not
  in this set is rejected with a WARN log. Prevents a compromised or
  malformed client from writing arbitrary keys like `ctrl_univ` or
  `preset_ch` that would break live output.

#### Changed
- **Main Qt GUI now tracks web-remote changes.** `titan_gui.py::refresh_logic`
  already had a params→widgets sync block gated on `"🟢" in osc_in_text`
  (designed for QLC+ remote DMX). The gate now also fires when the web
  handler set `app_state["web_sync_latch"] = True`. The flag is consumed
  with `dict.pop(…, False)` so the sync runs exactly once per web
  command; CPython dict ops are atomic so no lock is needed. Moving
  the Master Dimmer on the phone now physically moves the Master
  slider in the Qt window.
- **Audio status in the web UI is now pipeline-accurate.** Previously it
  read `app_state["osc_in_text"]`, which is the DMX-remote status
  (defaults to `🔴 WAIT` when no console is sending Art-Net). The web
  now shows `🟢 LIVE` when `pd_last_time` is < 500 ms old, `🔴 NO
  INPUT` otherwise. This matches what the gaffer means by "is audio
  flowing", independent of any DMX remote console.
- **FPS and Art-Net packet-count rows removed** from the web status
  block per the "gaffer, not a programmer" directive in CLAUDE.md.
  They were adding cognitive load without telling the user anything
  they could act on.

---

### [2026-04-20] - Web remote: fix broken JS (preset-button escape swallowed by Python)

#### Fixed
- **`main_v5.01.py:382`** — the entire web remote was dead on arrival because
  the generated `<script>` block had a JavaScript parse error. The source read
  ```python
  html+='<div class="pbtn'+cls+'" onclick="sendCmd(\'preset\','+i+')">'+nm+'</div>';
  ```
  and was embedded inside a Python triple-double-quoted string (`_WEB_HTML`).
  Python interprets `\'` as an escape even inside `"""..."""` — it strips the
  backslash and leaves a bare `'`. So the browser actually received
  ```js
  html+='<div class="pbtn'+cls+'" onclick="sendCmd('preset','+i+')">'+nm+'</div>';
  ```
  which contains the invalid token sequence `'" onclick="sendCmd(' preset ','`
  — a string literal directly juxtaposed with an identifier, with no
  operator. V8 (Chrome), JavaScriptCore (Safari), and SpiderMonkey
  (Firefox) all halt the entire `<script>` block on this SyntaxError,
  meaning `poll()` never ran, `setInterval` never armed, and the inline
  `onclick="sendCmd('panic',!panic)"` handlers reference a `sendCmd`
  symbol that was never defined — so every button press was a no-op.
  User symptom: status values stuck on em-dash, buttons do nothing,
  server log shows no `Web remote cmd:` lines despite clicks.

  Fix: change `\'preset\'` to `\\'preset\\'` in the Python source so the
  emitted JS contains `\'preset\'` as a JavaScript escape sequence
  inside the outer single-quoted string. Verified by re-executing the
  `_WEB_HTML = """…"""` literal in an isolated namespace — the
  browser now receives a parseable concatenation whose five string
  pieces all close cleanly.

  How this was missed: the bug is invisible to `curl` and to the Python
  linter. The HTML bytes are valid UTF-8, the HTTP status is 200, and
  Python has no way to know the emitted string will be parsed as JS. It
  only manifests when a real JS engine tries to evaluate the page.

---

### [2026-04-20] - Web remote: fix silent mute + surface request errors

#### Fixed
- **`main_v5.01.py:449`** — Web remote's MUTE button sent OSC to PD with the
  wrong key. `_handle()` called `notify_pd("/mute", ...)` with a leading
  slash, but `notify_pd` whitelists bare names (`PD_INIT_PARAMS =
  ["hip", "lop", "env", "test_freq", "test_db", "test_on", "sweep_on",
  "input_trim", "mute"]`) and prepends the slash itself via
  `f"/{name}"`. The whitelist check `if name in PD_INIT_PARAMS` saw
  `"/mute"` ≠ `"mute"` and silently dropped the message. PD never
  received the mute, so the web button's state flipped in the UI but
  audio kept flowing. Now passes `"mute"` (no slash). `params["mute"]`
  mirror was already correct — this was strictly an OSC-to-PD loss.

#### Changed
- **`do_GET` and `do_POST` now wrap their handlers in `try/except`** with
  `logger.exception(...)` + `send_response(500)`. Previously any handler
  error propagated up into `BaseHTTPRequestHandler` and — combined with
  the `log_message(self, fmt, *args): pass` suppression one line up —
  produced a silently-failing request with no log trail. Any future
  500-class bug in `_state()`, `_handle()`, or the JSON round-trip will
  now print a full traceback to the engine log.
- **`do_POST` logs every command** at INFO level as `Web remote cmd:
  {'cmd': '…', 'value': …}` so the operator can confirm button presses
  are arriving from the phone/tablet. Paired with the web server's
  existing "Web remote UI: http://…" boot banner this gives end-to-end
  visibility.
- **`_handle` now logs unknown commands** with
  `logger.warning(f"Web remote: unknown command {cmd!r}")`. Previously
  a typo in the HTML's `sendCmd('typo', ...)` would return an empty
  state update with no hint anything was wrong.

---

### [2026-04-20] - Fix SyntaxError in run_osc_server logging block

#### Fixed
- **`main_v5.01.py:929–949`**: `run_osc_server()` would not parse
  (`SyntaxError: unmatched ']'` at line 933). A four-line
  `if app_state.get("port_conflict_osc"): logger.info(...) else:
  logger.info(...)` block that belonged inside the `try:` had been
  split in half and relocated between the `audio_thread = Thread(...)`
  and `audio_thread.start()` lines, truncated mid-f-string and leaving
  `'osc_in_port']}")` as an orphan token. Restored the full `if/else`
  block in its correct position immediately after
  `ThreadingOSCUDPServer(...)`, and removed the displaced fragment from
  between the two `audio_thread` lines. File now parses cleanly
  (`python -c "import ast; ast.parse(open('main_v5.01.py').read())"`).

---

### [2026-04-18] - Fixture preview strip, ArtPoll network discovery, web remote UI

#### Added
- **Fixture preview strip.** A horizontal row of color swatches above the
  DMX grid (`titan_gui.py::_setup_fixture_preview`), one box per active
  fixture. Each box shows the fixture's name, universe, and a live
  RGBW-blended color rectangle driven by the actual DMX output bytes.
  Updates at 30 Hz in `refresh_logic` via `_refresh_fixture_preview`.
  Rebuilds automatically whenever `rebuild_dmx_grid` runs (patch change,
  mode change, column resize). Dark when output is < 4 per channel.

- **ArtPoll network discovery.** "Network Discovery (ArtPoll)" group box
  added to the Network tab (`titan_gui.py::_setup_artpoll_ui`). "Scan
  Network" button broadcasts a 14-byte ArtPoll packet to
  255.255.255.255:6454, collects ArtPollReply packets for 3 seconds in a
  daemon thread, and populates a 3-column table (IP, Short Name, Long
  Name). Scan runs off the Qt thread; results land in
  `app_state["discovered_nodes"]` and are consumed by
  `_update_artpoll_table` in the next `refresh_logic` tick.
  `artpoll_scan(timeout)` in `main_v5.01.py` is exposed via callbacks.

- **Web remote-control UI.** Minimal HTTP server started at boot
  (`main_v5.01.py::start_web_server`, stdlib `http.server` +
  `socketserver.ThreadingTCPServer`, no new dependencies). Default port
  9000 (configurable via `params["web_port"]`). Serves a dark mobile-
  responsive single-page app at `/`. API:
  - `GET /api/state` — JSON with mute, panic, dimmer, FPS, audio status,
    Art-Net packet count, preset map.
  - `POST /api/command` — JSON body `{cmd, value}`. Commands:
    `mute` (0/1), `panic` (true/false), `dimmer` (0-255),
    `preset` (slot 1-10).
  Page polls `/api/state` every 500 ms and reflects live state.
  Preset buttons glow green when a preset file is mapped to that slot.
  The server's LAN IP and port appear in the Status box "Web Remote:" row
  and are logged at startup. Also readable by copying from the GUI.

---

### [2026-04-18] - Decouple DSP from OSC receive thread (compute_audio_thread)

#### Changed
- **`handle_audio` is now a minimal OSC receive stub.** It updates
  FPS counters and `pd_last_time`, then puts the raw
  `(total_db, bass_db, treble_db)` tuple onto a new `audio_queue`
  (maxsize=1, latest-frame-wins) and returns. Previously it ran
  `engine.process_audio()`, built a 90-line `fb_payload`, and
  constructed the `cfg` dict synchronously — blocking the UDP socket
  while DSP ran. Under CPU load (thermal throttle, GC, anything else
  on the machine) that could fill the OS UDP receive buffer and drop
  audio frames silently.
- **New `compute_audio_thread` daemon thread** drains `audio_queue` and
  does all the work that `handle_audio` used to do: `osc_in_text`
  status, `engine.process_audio()`, panic blackout, packet-reset
  counter, `cfg`/`fb_payload` construction, and `send_queue.put_nowait`.
  Wrapped in `except Exception: logger.exception(...)` so a DSP crash
  doesn't take down the whole engine.
- **Thread topology** is now:
  `OSC recv → audio_queue → AudioCompute → send_queue → DMXSender`
  The Art-Net/DMX control listener and Qt GUI thread are unchanged.
- **`CLAUDE.md` Known Bugs #9** removed (audio processing on OSC recv
  thread). **#8** updated: the GIL-guarded scope deque race is still
  technically open; moving the three `scope_*.append()` calls inside
  the `buf_lock` block in `titan_engine.py` would close it.

---

### [2026-04-18] - Deduplicate preset whitelist, packet send, and PD RMS envelope

#### Changed
- **Preset whitelist consolidated.** The 8-line list of effect-preset
  param keys lived twice in `titan_gui.py` (once in `save_preset`, once
  in `load_preset`). Promoted to a class constant
  `TitanQtGUI.PRESET_WHITELIST` (tuple). Both call sites now reference
  it. Adding/removing a preset key is now a one-place edit and the two
  paths can never drift.
- **`sender_thread` packet build/send extracted.** `main_v5.01.py` now
  has a `_send_universe(out_u, payload, dest_ip, ...)` helper that
  encapsulates protocol selection (sACN sequence increment + builder vs.
  Art-Net builder), the `socket.sendto`, and the success/error return.
  The two near-identical 12-line blocks in `sender_thread` (main loop
  and feedback universe) collapsed to 4 and 3 lines respectively. No
  behavior change — counters and warnings still happen at the call site
  because they differ between the main and feedback paths.
- **`audio_input.pd`: three inline copies of `custom_env` extracted into
  a `custom_env.pd` abstraction file.** Each copy was a 19-line
  inlet/`*~`/`lop~`/`snapshot~`/`sqrt`/`rmstodb`/outlet chain — the
  RMS-to-dB envelope detector used for the three audio bands (full,
  low-pass, high-pass). Replaced each with a single `[custom_env]`
  object. Object indices in the parent canvas are preserved (`#X
  restore` and `#X obj <abstraction>` both consume one slot), so all
  existing `#X connect` lines remain valid.

#### Removed
- ~57 lines of duplicate Pd code, ~12 lines of duplicate Python preset
  whitelist, ~14 lines of duplicate Python packet build/send logic.
  CLAUDE.md refactor targets #24, #25, #26 closed.

---

### [2026-04-18] - Bare-except cleanup

#### Changed
- **`titan_gui.py:993` and `titan_gui.py:1943`**: narrowed bare `except:`
  to `except TypeError:`. Both were the same QSpinBox fallback pattern
  (try `setValue(float(val))`, fall back to `setValue(int(val))` for
  integer-only spinboxes). Narrowing means any non-TypeError — a
  widget-lifetime bug, a C++ deletion, a value-out-of-range — will now
  surface instead of being silently swallowed. Other sibling blocks in
  the same file (lines 1659, 1725, 1748, 2285) already used
  `except TypeError:`, so this just brings the two stragglers in line.

- **`CLAUDE.md` Known Bugs → HIGH #7** removed. The "silent `except: pass`
  everywhere" bug pointed at line numbers that have since been
  refactored (e.g. `main_v5.01.py:184` is now in a dict literal, and
  the titan_gui.py line numbers all now contain unrelated code). A
  full repo audit confirms no remaining silent swallowers:
  - all `except Exception` blocks log via `logger.error/warning/exception`
  - `queue.Full` / `queue.Empty` `pass` branches in `handle_audio` are
    the intentional latest-frame-wins single-slot-queue idiom
  - type-coercion helpers (`_coerce_dev_id`, `_parse_pd_device_list`)
    correctly swallow `TypeError`/`ValueError` as a control-flow tool

---

### [2026-04-18] - Remote-Control status honors GUI lock; CLAUDE.md CRITICAL cleared

#### Fixed
- **Remote-Control status no longer lies while the GUI-lock window is
  active.** `process_control_universe` in `main_v5.01.py` was writing
  `app_state["last_ctrl_time"] = time.time()` at the top of the function,
  before the 1.5 s `gui_lock_time` early-return. That made the middle-
  section status display "🟢 RX (DMX)" even when the incoming frame was
  being ignored because the user was actively dragging a slider. Moved
  the assignment to after the early return so the timestamp only
  advances on frames we actually apply.

#### Changed
- **`CLAUDE.md` Known Bugs → CRITICAL section** is now empty and has been
  removed. Bug #3 (`artnet_offset=1` U0/U1 collapse) was already fixed by
  the `offset_warned` skip-and-log guard in `sender_thread`. Bug #5
  (remote-control status lie) fixed in this entry. All six original
  CRITICAL bugs are now resolved.

---

### [2026-04-18] - Audio output device selection, scope legend, status-dot popup

#### Added
- **Audio output device selector.** New dropdown in the Audio Input Device
  group box (`titan_gui.py`) for picking PD's output device. Persists to
  `params["pd_audio_dev_out"]` and passes `-audiooutdev <id>` to PD on
  boot and every device change. Fixes the BlackHole-as-input case where
  PD would default its output to BlackHole and never reach real speakers.
- **Inline audio status indicator** next to the "Audio Input Device" group
  box title (`lbl_stat_audio_inline`). Mirrors the middle-section Status
  box indicator each frame so the user can see live/stalled audio state
  while picking a device.
- **Audio graph legend.** `pyqtgraph` `addLegend()` on the scope plot,
  labeling the three traces: "Audio In" (orange), "Center" (cyan),
  "Edge" (magenta).

#### Changed
- **Frequency sweep now works independently of Test Tone checkbox.**
  `audio_input.pd` now routes `(test_on OR sweep_on)` through an OR gate
  (`[f] + [t b f] + [+ 0] + [> 0]`, objects 103–106) into the existing
  tone/mic gate logic. Turning sweep on alone plays an audible sweep;
  turning test tone on alone plays a fixed tone; both on plays a sweep.
- **Test Tone status row** moved to sit directly below the Remote Control
  row (above the HLine + perf stats) in `_setup_performance_monitors`.
- **Clicking the middle-section audio status dot** (`lbl_stat_audio`) now
  opens a quick Audio Input Device picker dialog instead of a text-only
  troubleshooting dialog. The popup's combo drives the main combo, reusing
  the existing restart-PD flow.
- **`_parse_pd_device_list`** now accepts a `section` kwarg
  (`"input"` | `"output"`) and shares a new `_populate_device_combo`
  helper. Rescan refreshes both input and output combos in one pass.

#### Removed
- **`_show_audio_troubleshoot`** — deleted. Replaced by the device-picker
  popup; troubleshooting guidance remains accessible via the Help menu's
  Audio Mapping & Setup Guide.

---

### [2026-04-18] - Phase 4 Operational Safety + PD-init sync

#### Added
- **PANIC BLACKOUT button** in the Status box above the Master fader
  (`titan_gui.py::_setup_panic_button`). Large red clickable button that
  toggles `app_state["panic_blackout"]`. Active state shows black
  background + red text + "CLICK TO RESTORE" label.
- **60-second silent autosave** (`titan_gui.py::_autosave`). `QTimer`
  writes `self.params` to `titan_autosave.json` once per minute so the
  gaffer can recover state after a crash without overwriting the saved
  default patch.
- **Confirm-before-quit dialog** (`titan_gui.py::eventFilter`). When the
  user closes the main window while `app_state["artnet_active"]` is True,
  a `QMessageBox.question` blocks the close with
  "Output is active. Are you sure you want to quit?".
- **`PD_INIT_PARAMS` constant + `push_pd_init_params()`** in
  `main_v5.01.py`. Re-sends every PD-relevant parameter to the fresh PD
  process on boot and after every device change (1.5 s deferred via
  `QTimer.singleShot`). Fixes "test tone plays nothing" / "mute does
  nothing" / "switching input device kills audio until I wiggle a control"
  symptoms that appeared because PD's filter/oscillator/mute state reset
  on every Watchdog relaunch.
- **`push_pd_init` callback** exposed through the GUI's callbacks dict so
  `_on_audio_device_changed` can re-sync PD state post-restart.

#### Fixed
- **Panic Blackout now zeros both the GUI's DMX grid preview and the
  Art-Net wire.** Earlier attempt zeroed only in `sender_thread`, which
  meant `engine.get_snapshot()` (read by the GUI) still returned live
  values. Now zeros `buffers` in-place inside `handle_audio`, mutating
  the shared `engine.published_buffers` dict. Also zeros `fb_payload`
  (fallback universe 14 / control-master layer).
- **Input trim gain slider no longer requires mute toggling to take
  effect.** `audio_input.pd`: added `[f]` (obj 101) + `[t b f]` (obj 102)
  so slider drags flow through the multiplier's HOT inlet immediately,
  and mute toggles re-emit the stored trim so the `*` node fires a fresh
  product on either trigger. Previously trim fed the cold inlet only.

---

### [2026-04-18] - Docs

#### Changed
- **`CLAUDE.md`**: added explicit rule to print each new CHANGELOG entry
  as plaintext in chat whenever the changelog is updated.

#### Added
- **`CHANGELOG.md`**: created, back-filled with all uncommitted work
  since commit `54ebf4a`.
