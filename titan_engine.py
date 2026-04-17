import time
import random
from collections import deque


def safe_float(val, default=0.0):
    try:
        return float(val)
    except:
        return default


class RenderEngine:
    def __init__(self, max_pixels_per_fix=512):
        self.max_pixels_per_fix = max_pixels_per_fix
        self.audio_latency_ms = 0.0
        self.dsp_latency_ms = 0.0

        self.fix_state = []
        self.dmx_buffers = {u: bytearray(512) for u in range(16)}
        self.pixel_buffer = [0.0] * max_pixels_per_fix
        self.shifted_buffer = [0.0] * max_pixels_per_fix

        self.scope_audio = deque([0.0] * 200, maxlen=200)
        self.scope_center = deque([0.0] * 200, maxlen=200)
        self.scope_edge = deque([0.0] * 200, maxlen=200)

        self.preset_mask_time = 0.0

    def _ensure_fixture_capacity(self, count):
        while len(self.fix_state) < count:
            self.fix_state.append({
                "brightness_peak": [0.0] * self.max_pixels_per_fix,
                "history": [deque(maxlen=10) for _ in range(self.max_pixels_per_fix)],
                "last_pixels": [0.0] * self.max_pixels_per_fix,
                "stutter_count": 0,
                "prev_audio_level": 0.0,
                "smoothed_dimmer": 0.0
            })

    def _ensure_universe_capacity(self, univ):
        if univ not in self.dmx_buffers:
            self.dmx_buffers[univ] = bytearray(512)

    def _get_dyn(self, params, f_idx, param_name, default):
        if int(params.get("link_all_dynamics", 0)) == 1:
            return safe_float(params.get(param_name, default))
        val = params.get(f"f{f_idx}_{param_name}")
        if val is not None:
            return safe_float(val)
        return safe_float(params.get(param_name, default))

    def process_audio(self, total_db, bass_db, treble_db, params):
        for u in self.dmx_buffers.keys():
            self.dmx_buffers[u][:] = b'\x00' * 512

        t_start = time.perf_counter()

        rng = max(0.1, safe_float(params.get("ceiling", 100)) - safe_float(params.get("floor", 0)))
        norm_level = max(0.0, min(1.0, (total_db - safe_float(params.get("floor", 0))) / rng))
        gate_thresh = safe_float(params.get("noise_gate", 0.02))
        if norm_level < gate_thresh: norm_level = 0.0

        drive = safe_float(params.get("drive", 1.0))
        driven_level = max(0.0, min(1.0, norm_level * drive))

        # --- NEW: The Seamless Freeze ---
        if not hasattr(self, "last_good_level"):
            self.last_good_level = 0.0

        if time.time() < getattr(self, "preset_mask_time", 0.0):
            # Freeze the lights at their exact current brightness instead of dropping to black
            driven_level = self.last_good_level
        else:
            # Save the valid level so it is ready for the next preset change
            self.last_good_level = driven_level


        b_lin = pow(10, bass_db / 20.0)
        t_lin = pow(10, treble_db / 20.0)
        treble_ratio = t_lin / (b_lin + t_lin + 0.0001)

        pd_buffer_ms = (safe_float(params.get("env", 1024)) / 44100.0) * 1000.0
        master_inhib = safe_float(params.get("master_inhibitive", 1.0))
        led_gamma = safe_float(params.get("led_gamma", 2.5))

        master_ana_force_on = int(params.get("master_ana_force_on", 0)) == 1
        master_ana_force_off = int(params.get("master_ana_force_off", 0)) == 1
        master_digi_force_on = int(params.get("master_digi_force_on", 0)) == 1
        master_digi_force_off = int(params.get("master_digi_force_off", 0)) == 1
        master_od_force_on = int(params.get("master_od_force_on", 0)) == 1
        master_od_force_off = int(params.get("master_od_force_off", 0)) == 1

        num_fixtures = int(safe_float(params.get("num_fixtures", 1)))
        self._ensure_fixture_capacity(num_fixtures)

        global_audio_scope_val = 0.0

        chain_info = {}
        current_chain_fixtures = []

        for f_idx in range(num_fixtures):
            fix_num = f_idx + 1
            p_count = min(int(safe_float(params.get(f"f{fix_num}_pix", 16))), self.max_pixels_per_fix)
            is_extend = int(params.get(f"f{fix_num}_extend", 0)) == 1

            if not is_extend or not current_chain_fixtures:
                if current_chain_fixtures:
                    total_p = sum(info["p_count"] for info in current_chain_fixtures)
                    curr_start = 0
                    for info in current_chain_fixtures:
                        chain_info[info["f_idx"]] = {"start": curr_start, "total": total_p}
                        curr_start += info["p_count"]
                current_chain_fixtures = [{"f_idx": f_idx, "p_count": p_count}]
            else:
                current_chain_fixtures.append({"f_idx": f_idx, "p_count": p_count})

        if current_chain_fixtures:
            total_p = sum(info["p_count"] for info in current_chain_fixtures)
            curr_start = 0
            for info in current_chain_fixtures:
                chain_info[info["f_idx"]] = {"start": curr_start, "total": total_p}
                curr_start += info["p_count"]

        for f_idx in range(num_fixtures):
            fix_num = f_idx + 1
            if int(params.get(f"f{fix_num}_active", 0)) == 0:
                continue

            p_count = min(int(params.get(f"f{fix_num}_pix", 16)), self.max_pixels_per_fix)
            p_foot = int(params.get(f"f{fix_num}_foot", 4))
            univ = int(params.get(f"f{fix_num}_uni", 0))
            s_addr = int(params.get(f"f{fix_num}_addr", 1)) - 1
            is_flipped = int(params.get(f"f{fix_num}_flip", 0)) == 1

            self._ensure_universe_capacity(univ)
            f_ratios = [0.0, 0.0, 0.0, 0.0]

            if int(params.get(f"f{fix_num}_align", 0)) == 1:
                for i in range(p_count):
                    for c in range(min(p_foot, 4)):
                        abs_ch = s_addr + (i * p_foot) + c
                        t_univ = univ + (abs_ch // 512)
                        local_ch = abs_ch % 512
                        self._ensure_universe_capacity(t_univ)
                        self.dmx_buffers[t_univ][local_ch] = 255
                continue

            f_state = self.fix_state[f_idx]
            expand = self._get_dyn(params, fix_num, "expand", 1.0)
            gamma = self._get_dyn(params, fix_num, "gamma", 1.0)
            eq_tilt = self._get_dyn(params, fix_num, "eq_tilt", 0.0)
            freq_width = self._get_dyn(params, fix_num, "freq_width", 1.0)
            knee = self._get_dyn(params, fix_num, "knee", 0.1)
            scale_val = self._get_dyn(params, fix_num, "scale", 1.0)
            time_gamma = self._get_dyn(params, fix_num, "time_gamma", 1.0)
            skew_val = self._get_dyn(params, fix_num, "skew", 0.0)
            width_val = self._get_dyn(params, fix_num, "width", 1.0)

            atk_c = self._get_dyn(params, fix_num, "atk_c", 20)
            rel_c = self._get_dyn(params, fix_num, "rel_c", 150)
            atk_e = self._get_dyn(params, fix_num, "atk_e", 50)
            rel_e = self._get_dyn(params, fix_num, "rel_e", 500)
            fix_dimmer = self._get_dyn(params, fix_num, "dimmer", 1.0)

            dimmer_atk_ms = self._get_dyn(params, fix_num, "dimmer_atk", 50.0)
            dimmer_rel_ms = self._get_dyn(params, fix_num, "dimmer_rel", 500.0)

            f_r = self._get_dyn(params, fix_num, "color_r", 255.0)
            f_g = self._get_dyn(params, fix_num, "color_g", 255.0)
            f_b = self._get_dyn(params, fix_num, "color_b", 255.0)
            f_w = self._get_dyn(params, fix_num, "color_w", 0.0)

            f_ratios = [f_r / 255.0, f_g / 255.0, f_b / 255.0, f_w / 255.0]

            od_thresh = self._get_dyn(params, fix_num, "od_thresh", 0.8)
            od_desat = self._get_dyn(params, fix_num, "od_desat", 0.5)
            od_glitch = self._get_dyn(params, fix_num, "od_glitch", 0.5)

            dmx_smooth = int(self._get_dyn(params, fix_num, "dmx_smooth_on", 1)) == 1
            smooth_size = max(1, int(self._get_dyn(params, fix_num, "smooth_size", 3)))

            audio_level = pow(driven_level, expand)
            if f_idx == 0: global_audio_scope_val = audio_level

            f_od_en = int(params.get(f"f{fix_num}_od_en", 0)) == 1
            od_en = (f_od_en or master_od_force_on) and not master_od_force_off

            od_factor = 0.0
            if od_en and audio_level > od_thresh and od_thresh < 1.0:
                od_factor = (audio_level - od_thresh) / max(0.001, 1.0 - od_thresh)

            delta = audio_level - f_state["prev_audio_level"]
            f_state["prev_audio_level"] = audio_level
            jitter_mult = 1.0

            if int(self._get_dyn(params, fix_num, "jitter_on", 1)) == 1:
                motion = abs(delta)
                thresh = self._get_dyn(params, fix_num, "jitter_thresh", 0.05)
                if motion < thresh:
                    jitter_amt = self._get_dyn(params, fix_num, "jitter_amount", 5.0)
                    jitter_mult = 1.0 + ((1.0 - (motion / max(0.001, thresh))) * jitter_amt)

            gamma_shift = (0.5 - treble_ratio + eq_tilt) * (freq_width * 5.0)
            dynamic_gamma = max(0.1, gamma + gamma_shift)

            skew_val = max(-0.999, min(0.999, skew_val))
            center_norm = 0.5 + (skew_val * 0.5)

            chain_start = chain_info[f_idx]["start"]
            chain_total = chain_info[f_idx]["total"]

            target_dimmer = master_inhib * fix_dimmer
            dimmer_atk_coef = min(1.0, pd_buffer_ms / max(1.0, dimmer_atk_ms * jitter_mult))
            dimmer_rel_coef = min(1.0, pd_buffer_ms / max(1.0, dimmer_rel_ms * jitter_mult))
            current_dimmer = f_state.get("smoothed_dimmer", target_dimmer)

            f_state["smoothed_dimmer"] = current_dimmer + ((target_dimmer - current_dimmer) * (
                dimmer_atk_coef if target_dimmer > current_dimmer else dimmer_rel_coef))

            for i in range(p_count):
                global_p = chain_start + i

                if chain_total > 1:
                    norm_p = global_p / (chain_total - 1)
                else:
                    norm_p = 0.5

                if norm_p < center_norm:
                    dist_norm = (center_norm - norm_p) / center_norm
                else:
                    dist_norm = (norm_p - center_norm) / (1.0 - center_norm)

                dist_norm = dist_norm / max(0.001, width_val)

                threshold = pow(dist_norm, dynamic_gamma)

                diff = audio_level - threshold
                if diff < -knee:
                    target_01 = 0.0
                elif diff > knee:
                    target_01 = diff
                else:
                    target_01 = (diff + knee) ** 2 / (4 * max(0.001, knee))

                target = max(0.0, min(255.0, target_01 * 255.0 * scale_val))

                if chain_total > 1:
                    blend = pow(min(1.0, dist_norm), time_gamma)
                    atk_ms = atk_c + (atk_e - atk_c) * blend
                    rel_ms = rel_c + (rel_e - rel_c) * blend
                else:
                    atk_ms, rel_ms = atk_c, rel_c

                pk_atk = min(1.0, pd_buffer_ms / max(1.0, atk_ms * jitter_mult))
                pk_rel = min(1.0, pd_buffer_ms / max(1.0, rel_ms * jitter_mult))

                f_state["brightness_peak"][i] += (target - f_state["brightness_peak"][i]) * (
                    pk_atk if target > f_state["brightness_peak"][i] else pk_rel)

                raw_val = f_state["brightness_peak"][i]

                if dmx_smooth:
                    f_state["history"][i].append(raw_val)
                    hist_len = len(f_state["history"][i])
                    use_len = min(hist_len, smooth_size)
                    val = sum(list(f_state["history"][i])[-use_len:]) / use_len
                else:
                    val = raw_val

                self.pixel_buffer[i] = val

            f_digi_en = int(params.get(f"f{fix_num}_glitch_digi", 0)) == 1
            od_digi_boost = od_factor * od_glitch
            digi_en = (f_digi_en or master_digi_force_on or od_digi_boost > 0.0) and not master_digi_force_off

            base_digi = self._get_dyn(params, fix_num, "glitch_digi_amt", 0.0)
            digi_amt = min(1.0, base_digi + od_digi_boost)

            if digi_en and digi_amt > 0.0 and p_count > 0:
                if f_state["stutter_count"] <= 0 and random.random() < (digi_amt * 0.015):
                    f_state["stutter_count"] = random.randint(2, 5)

                if f_state["stutter_count"] > 0:
                    for i in range(p_count): self.pixel_buffer[i] = f_state["last_pixels"][i]
                    f_state["stutter_count"] -= 1
                else:
                    if random.random() < (digi_amt * 0.85):
                        block_size = max(2, int(self._get_dyn(params, fix_num, "glitch_digi_block", 4.0)))
                        for i in range(0, p_count, block_size):
                            sample_val = self.pixel_buffer[i]
                            for j in range(block_size):
                                if i + j < p_count: self.pixel_buffer[i + j] = sample_val

            f_ana_en = int(params.get(f"f{fix_num}_glitch_ana", 0)) == 1
            od_ana_boost = od_factor * od_glitch
            ana_en = (f_ana_en or master_ana_force_on or od_ana_boost > 0.0) and not master_ana_force_off

            base_ana = self._get_dyn(params, fix_num, "glitch_ana_amt", 0.0)
            ana_amt = min(1.0, base_ana + od_ana_boost)

            if ana_en and ana_amt > 0.0 and p_count > 0:
                if random.random() < (ana_amt * 0.8):
                    tear_width = max(1, int(self._get_dyn(params, fix_num, "glitch_ana_tear", 10.0)))
                    tear_start = random.randint(0, max(0, p_count - tear_width))
                    offset = random.randint(-3, 3)
                    for i in range(tear_width):
                        src_idx = tear_start + i - offset
                        if 0 <= src_idx < p_count:
                            self.shifted_buffer[i] = self.pixel_buffer[src_idx]
                        else:
                            self.shifted_buffer[i] = 0.0
                    for i in range(tear_width): self.pixel_buffer[tear_start + i] = self.shifted_buffer[i]

                noise_vol = self._get_dyn(params, fix_num, "glitch_ana_noise", 0.5) * ana_amt * 255.0
                for i in range(p_count):
                    if random.random() < 0.3:
                        self.pixel_buffer[i] += random.uniform(-noise_vol, noise_vol)
                        self.pixel_buffer[i] = max(0.0, min(255.0, self.pixel_buffer[i]))

            for i in range(p_count): f_state["last_pixels"][i] = self.pixel_buffer[i]

            for i in range(p_count):
                buffer_read_idx = (p_count - 1 - i) if is_flipped else i
                final_val = float(self.pixel_buffer[buffer_read_idx])

                pixel_hotness = od_factor * (final_val / 255.0) * od_desat

                if f_idx == 0:
                    if i == int(p_count / 2): self.scope_center.append(final_val / 255.0)
                    if i == 0: self.scope_edge.append(final_val / 255.0)

                intensity_mult = (final_val / 255.0) * f_state["smoothed_dimmer"]

                bg_dim = self._get_dyn(params, fix_num, "bg_dimmer", params.get("bg_dimmer", 1.0))
                bg_r = self._get_dyn(params, fix_num, "bg_r", params.get("bg_r", 0.0))
                bg_g = self._get_dyn(params, fix_num, "bg_g", params.get("bg_g", 0.0))
                bg_b = self._get_dyn(params, fix_num, "bg_b", params.get("bg_b", 0.0))
                bg_w = self._get_dyn(params, fix_num, "bg_w", params.get("bg_w", 0.0))
                bg_colors = [bg_r, bg_g, bg_b, bg_w]

                for c in range(min(p_foot, 4)):
                    raw_val = 255.0 * f_ratios[c] * intensity_mult
                    desat_val = raw_val + ((255.0 * intensity_mult - raw_val) * pixel_hotness)
                    effect_val = int(255.0 * ((desat_val / 255.0) ** led_gamma))

                    bg_val = int(bg_colors[c] * bg_dim)
                    final_merged_val = max(effect_val, bg_val)

                    abs_ch = s_addr + (i * p_foot) + c
                    t_univ = univ + (abs_ch // 512)
                    local_ch = abs_ch % 512

                    self._ensure_universe_capacity(t_univ)
                    self.dmx_buffers[t_univ][local_ch] = max(0, min(255, final_merged_val))

        self.scope_audio.append(global_audio_scope_val)
        self.audio_latency_ms = pd_buffer_ms
        self.dsp_latency_ms = (time.perf_counter() - t_start) * 1000.0

        return self.dmx_buffers