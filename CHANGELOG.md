# Changelog

All notable changes to Titan Engine are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Dates are ISO 8601.

## [Unreleased]

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
