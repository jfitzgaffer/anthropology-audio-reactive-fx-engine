import subprocess
import os
import time
import sys  # <-- NEW: make sure sys is imported!


class TitanWatchdog:
    def __init__(self, pd_patch_name="audio_input.pd"):
        self.pd_patch_name = pd_patch_name
        self.pd_process = None

        # --- NEW: THE ZOMBIE SWEEPER ---
        # Automatically assassinate any leftover PD processes from previous crashes
        print("⚡️ Watchdog: Sweeping memory for zombie processes...")
        if sys.platform == "win32":
            os.system("taskkill /F /IM pd.exe 2>nul")
        else:
            os.system("killall -9 pd 2>/dev/null")
            os.system("killall -9 Pd 2>/dev/null")

        time.sleep(0.5)  # Give the OS a half-second to clear the memory
        # -------------------------------

        # We check the standard macOS application paths for Pure Data
        possible_paths = [
            "/Applications/Pd-0.56-2.app/Contents/Resources/bin/pd",
            "/Applications/Pd.app/Contents/Resources/bin/pd"
        ]

        self.pd_executable = next((path for path in possible_paths if os.path.exists(path)), None)

        if not self.pd_executable:
            print("WARNING: Could not find Pure Data in the Applications folder.")

    def start_engine(self, device_id=None, output_device_id=None):
        """Kills any frozen PD instance, then starts a fresh one instantly."""
        self.stop_engine()

        if not self.pd_executable:
            print("Cannot start engine: Pure Data executable not found.")
            return

        print("⚡️ Watchdog: Launching Pure Data in the background...")

        # The base command: run without GUI, open the specified patch
        cmd = [self.pd_executable, "-nogui"]

        # If the user selected a specific audio interface, inject it here
        if device_id is not None:
            cmd.extend(["-audioindev", str(device_id)])

        # Output device — critical when using BlackHole or another loopback
        # for INPUT. Without this, PD picks the system default output, which
        # on a BlackHole-as-input rig is often BlackHole itself — so the tone
        # and any monitor signal vanish into the virtual loopback and never
        # reach real speakers.
        if output_device_id is not None:
            cmd.extend(["-audiooutdev", str(output_device_id)])

        # Add the patch file to the end of the command
        cmd.append(self.pd_patch_name)

        # Launch PD completely invisibly
        self.pd_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,  # Hides PD's terminal spam
            stderr=subprocess.DEVNULL
        )
        print("⚡️ Watchdog: Pure Data engine is active.")

    def stop_engine(self):
        """Assasinates the PD background process."""
        if self.pd_process and self.pd_process.poll() is None:
            print("⚡️ Watchdog: Terminating Pure Data process...")
            self.pd_process.terminate()

            # Give it a second to close gracefully, then force kill if stubborn
            try:
                self.pd_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.pd_process.kill()

            self.pd_process = None

    def get_pd_audio_devices(self):
        if not self.pd_executable:
            return ""

        print("⚡️ Watchdog: Scanning for audio hardware...", flush=True)

        try:
            # We use Popen with a shorter timeout strategy
            process = subprocess.Popen(
                [self.pd_executable, "-nogui", "-listdev"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            # Give it exactly 1 second to respond. If it doesn't, we kill it.
            try:
                out, err = process.communicate(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                out, err = process.communicate()

            return str(out) + "\n" + str(err)
        except Exception as e:
            print(f"⚠️ Watchdog: Scan Error: {e}", flush=True)
            return ""