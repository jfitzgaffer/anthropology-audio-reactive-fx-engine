"""Auto-calibration for the audio signal chain.

Two-phase wizard: first measure ambient noise with the actor silent, then
measure the actor's voice. From the captured dB samples we derive floor,
ceiling, input_trim, noise_gate, hip, lop, expand, knee, and env so the
voice fills the dynamic range that drives DMX output.

The module is UI-agnostic and engine-agnostic: it exposes a single
AudioCalibrator object that receives OSC audio frames via feed() and
returns a result dict via compute_*_result(). The GUI is responsible for
driving the phase transitions and applying the results to params.
"""

import logging
import math
import threading
import time

import numpy as np

logger = logging.getLogger("TitanEngine")


PHASE_IDLE = "idle"
PHASE_NOISE = "noise"
PHASE_NOISE_DONE = "noise_done"
PHASE_VOICE = "voice"
PHASE_VOICE_DONE = "voice_done"


class AudioCalibrator:
    """Collects audio dB samples across two phases and derives suggested params.

    Thread-safety: feed() is called from the OSC receiver thread, while
    start_*/compute_*/snapshot are called from the Qt event-loop thread. A
    single lock guards the shared sample buffers.
    """

    def __init__(self, params, noise_duration=6.0, voice_duration=12.0):
        self.params = params
        self.noise_duration = float(noise_duration)
        self.voice_duration = float(voice_duration)

        self._lock = threading.Lock()
        self.phase = PHASE_IDLE
        self._phase_start = 0.0
        self._phase_end = 0.0

        self._noise_total = []
        self._noise_bass = []
        self._noise_treble = []
        self._voice_total = []
        self._voice_bass = []
        self._voice_treble = []

        self._last_frame = (None, None, None)

        self.noise_result = None
        self.voice_result = None

    # ------------------------------------------------------------------
    # Public control
    # ------------------------------------------------------------------

    def start_noise_phase(self):
        with self._lock:
            self._noise_total.clear()
            self._noise_bass.clear()
            self._noise_treble.clear()
            self.phase = PHASE_NOISE
            self._phase_start = time.time()
            self._phase_end = self._phase_start + self.noise_duration
        logger.info(f"Calibration: noise phase started ({self.noise_duration:.1f}s).")

    def start_voice_phase(self):
        with self._lock:
            self._voice_total.clear()
            self._voice_bass.clear()
            self._voice_treble.clear()
            self.phase = PHASE_VOICE
            self._phase_start = time.time()
            self._phase_end = self._phase_start + self.voice_duration
        logger.info(f"Calibration: voice phase started ({self.voice_duration:.1f}s).")

    def cancel(self):
        with self._lock:
            self.phase = PHASE_IDLE
        logger.info("Calibration: cancelled.")

    @property
    def is_capturing(self):
        return self.phase in (PHASE_NOISE, PHASE_VOICE)

    # ------------------------------------------------------------------
    # OSC callback — invoked from the audio thread
    # ------------------------------------------------------------------

    def feed(self, total_db, bass_db, treble_db):
        with self._lock:
            self._last_frame = (float(total_db), float(bass_db), float(treble_db))

            if self.phase == PHASE_NOISE:
                self._noise_total.append(float(total_db))
                self._noise_bass.append(float(bass_db))
                self._noise_treble.append(float(treble_db))
                if time.time() >= self._phase_end:
                    self.phase = PHASE_NOISE_DONE

            elif self.phase == PHASE_VOICE:
                self._voice_total.append(float(total_db))
                self._voice_bass.append(float(bass_db))
                self._voice_treble.append(float(treble_db))
                if time.time() >= self._phase_end:
                    self.phase = PHASE_VOICE_DONE

    # ------------------------------------------------------------------
    # Progress snapshot for UI
    # ------------------------------------------------------------------

    def snapshot(self):
        """Return a dict of live capture state for the progress UI."""
        with self._lock:
            now = time.time()
            elapsed = max(0.0, now - self._phase_start) if self._phase_start else 0.0
            remaining = max(0.0, self._phase_end - now) if self._phase_end else 0.0
            duration = self.noise_duration if self.phase in (PHASE_NOISE, PHASE_NOISE_DONE) else self.voice_duration
            samples = (
                len(self._noise_total) if self.phase in (PHASE_NOISE, PHASE_NOISE_DONE)
                else len(self._voice_total) if self.phase in (PHASE_VOICE, PHASE_VOICE_DONE)
                else 0
            )
            return {
                "phase": self.phase,
                "elapsed": elapsed,
                "remaining": remaining,
                "duration": duration,
                "samples": samples,
                "last_total": self._last_frame[0],
                "last_bass": self._last_frame[1],
                "last_treble": self._last_frame[2],
            }

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------

    def compute_noise_result(self):
        """Analyze the silent-room samples. Stored on self.noise_result.

        Returns a dict with keys:
            ok (bool), reason (str if not ok),
            noise_floor_db, noise_std,
            suggested_hip.
        """
        with self._lock:
            total = list(self._noise_total)
            bass = list(self._noise_bass)
            treble = list(self._noise_treble)

        if len(total) < 10:
            self.noise_result = {
                "ok": False,
                "reason": f"Too few samples captured ({len(total)}). Is audio flowing?",
            }
            return self.noise_result

        total_arr = np.asarray(total, dtype=np.float32)
        bass_arr = np.asarray(bass, dtype=np.float32)
        treble_arr = np.asarray(treble, dtype=np.float32)

        noise_floor = float(np.percentile(total_arr, 90))
        noise_std = float(np.std(total_arr))

        if noise_std > 8.0:
            self.noise_result = {
                "ok": False,
                "reason": (
                    f"Noise level varies too much (std={noise_std:.1f} dB). "
                    "Ensure the room is quiet and nothing is tapping the mic, then retry."
                ),
                "noise_floor_db": noise_floor,
                "noise_std": noise_std,
            }
            return self.noise_result

        bass_p90 = float(np.percentile(bass_arr, 90))
        treble_p90 = float(np.percentile(treble_arr, 90))
        bass_prominence = bass_p90 - treble_p90

        if bass_prominence > 10.0:
            suggested_hip = 200.0
        elif bass_prominence > 5.0:
            suggested_hip = 150.0
        else:
            suggested_hip = 100.0

        self.noise_result = {
            "ok": True,
            "noise_floor_db": noise_floor,
            "noise_std": noise_std,
            "bass_prominence": bass_prominence,
            "suggested_hip": suggested_hip,
            "n_samples": len(total),
        }
        logger.info(
            f"Calibration noise: floor={noise_floor:.1f} dB, std={noise_std:.2f}, "
            f"bass_prominence={bass_prominence:.1f} dB, hip={suggested_hip:.0f} Hz, "
            f"n={len(total)}"
        )
        return self.noise_result

    def compute_voice_result(self):
        """Analyze the voice samples relative to the noise floor.

        Depends on a successful compute_noise_result() having populated
        self.noise_result.

        Returns a dict with:
            ok (bool), reason (str if not ok), params (dict of suggested param values),
            diagnostics (dict of computed statistics for display).
        """
        if not self.noise_result or not self.noise_result.get("ok"):
            return {"ok": False, "reason": "Noise phase did not complete successfully."}

        with self._lock:
            total = list(self._voice_total)
            bass = list(self._voice_bass)
            treble = list(self._voice_treble)

        if len(total) < 20:
            return {
                "ok": False,
                "reason": f"Too few voice samples captured ({len(total)}).",
            }

        total_arr = np.asarray(total, dtype=np.float32)
        bass_arr = np.asarray(bass, dtype=np.float32)
        treble_arr = np.asarray(treble, dtype=np.float32)

        noise_floor = float(self.noise_result["noise_floor_db"])
        active_mask = total_arr > (noise_floor + 6.0)
        active_frac = float(np.sum(active_mask)) / max(1, total_arr.size)

        if active_frac < 0.30:
            return {
                "ok": False,
                "reason": (
                    f"Only {active_frac * 100:.0f}% of frames had vocal energy above "
                    f"the noise floor. Ask the actor to speak louder/closer and retry."
                ),
                "active_frac": active_frac,
            }

        active_total = total_arr[active_mask]
        active_bass = bass_arr[active_mask]
        active_treble = treble_arr[active_mask]

        voice_peak = float(np.percentile(active_total, 99))
        voice_p95 = float(np.percentile(active_total, 95))
        voice_p25 = float(np.percentile(active_total, 25))
        voice_range = voice_p95 - voice_p25

        # input_trim: if the voice peak is low, bump the PD-side preamp so it
        # lands near 90 dB on PD's rmstodb scale (amplitude 1.0 = 100 dB, so 90
        # leaves ~10 dB of headroom before clipping). Shift the floor/ceiling
        # by the same dB so they still target the post-trim signal.
        #
        # The engine's floor/ceiling sliders run 0..120 on the same rmstodb
        # scale — higher = louder — so floor stays above noise and ceiling
        # sits just above voice peak.
        current_trim = float(self.params.get("input_trim", 1.0))
        current_trim = max(1e-3, current_trim)
        db_shift = 0.0
        suggested_trim = current_trim
        target_peak = 90.0
        if voice_peak < (target_peak - 10.0):
            needed_db = target_peak - voice_peak
            gain_linear = 10.0 ** (needed_db / 20.0)
            suggested_trim = max(0.05, min(5.0, current_trim * gain_linear))
            db_shift = 20.0 * math.log10(suggested_trim / current_trim)

        suggested_floor = max(0.0, min(120.0, (noise_floor + 3.0) + db_shift))
        suggested_ceiling = max(0.0, min(120.0, (voice_peak + 2.0) + db_shift))

        # Treble/bass ratio tells us whether raising the lowpass will help.
        # Guard against bass ≤ 0 by using linear sums instead of ratios of dB.
        bass_lin = np.power(10.0, active_bass / 20.0)
        treble_lin = np.power(10.0, active_treble / 20.0)
        treble_ratio = float(np.mean(treble_lin / (bass_lin + treble_lin + 1e-12)))

        if treble_ratio > 0.30:
            suggested_lop = 6000.0
        else:
            suggested_lop = 4000.0

        if voice_range > 20.0:
            suggested_expand = 1.5
        elif voice_range > 10.0:
            suggested_expand = 1.2
        else:
            suggested_expand = 1.0

        suggested_hip = float(self.noise_result.get("suggested_hip", 150.0))

        params_out = {
            "floor": round(suggested_floor, 1),
            "ceiling": round(suggested_ceiling, 1),
            "input_trim": round(suggested_trim, 2),
            "noise_gate": 0.02,
            "hip": round(suggested_hip, 0),
            "lop": round(suggested_lop, 0),
            "expand": round(suggested_expand, 2),
            "knee": 0.05,
            "env": 512.0,
            "drive": 1.0,
        }

        diagnostics = {
            "noise_floor_db": noise_floor,
            "voice_peak_db": voice_peak,
            "voice_p95_db": voice_p95,
            "voice_p25_db": voice_p25,
            "voice_range_db": voice_range,
            "active_frac": active_frac,
            "treble_ratio": treble_ratio,
            "db_shift_applied": db_shift,
            "n_voice_samples": len(total),
        }

        result = {"ok": True, "params": params_out, "diagnostics": diagnostics}
        self.voice_result = result
        logger.info(
            f"Calibration voice: peak={voice_peak:.1f} dB, active={active_frac * 100:.0f}%, "
            f"range={voice_range:.1f} dB, treble_ratio={treble_ratio:.2f}, "
            f"trim={suggested_trim:.2f}, floor={suggested_floor:.1f}, "
            f"ceiling={suggested_ceiling:.1f}"
        )
        return result
