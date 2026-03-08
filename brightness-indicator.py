#!/usr/bin/env python3
import atexit
from collections import deque
import fcntl
import faulthandler
import json
import logging
import os
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3 as AppIndicator
from gi.repository import GLib, Gtk

try:
    from evdev import InputDevice, ecodes, list_devices
except Exception:
    InputDevice = None
    ecodes = None
    list_devices = None

APP_NAME = "brightness-indicator"


def get_runtime_dir() -> Path:
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        return Path(xdg_runtime)

    run_user = Path(f"/run/user/{os.getuid()}")
    if run_user.exists():
        return run_user

    return Path("/tmp")


def get_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    if base:
        state_dir = Path(base) / APP_NAME
    else:
        state_dir = Path.home() / ".local" / "state" / APP_NAME
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def acquire_singleton_lock(lock_path: Path):
    lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        return None

    os.ftruncate(lock_fd, 0)
    os.write(lock_fd, str(os.getpid()).encode("ascii", "ignore"))
    return lock_fd


class BrightnessIndicator:
    KEY_MIN_GAP_SECONDS = 0.20
    DISPLAY_CACHE_TTL_SECONDS = 300
    BRIGHTNESS_SETTLE_SECONDS = 3.0
    BRIGHTNESS_MATCH_TOLERANCE = 2
    CONTROL_QUIET_WINDOW_SECONDS = 6.0
    MEASURED_LABEL_MIN_DELTA = 2
    STARTUP_LABEL_RETRY_SECONDS = (1, 3)
    STARTUP_RETRY_INTERVAL_SECONDS = 1.0
    STARTUP_WARN_EVERY_ATTEMPTS = 5

    def __init__(self, lock_fd, state_dir: Path):
        self.log = logging.getLogger(APP_NAME)
        self.lock_fd = lock_fd
        self.main_thread_id = threading.get_ident()

        self.step_percent = int(os.environ.get("BRIGHTNESS_STEP", "10"))
        self.step_percent = max(1, min(30, self.step_percent))

        self.ddc_lock = threading.Lock()
        self.ddc_cmd_prefix = ["ddcutil"]
        self.ddc_detected_prefix = False

        self.ddc_displays = []
        self.supported_displays = []
        self.last_display_refresh = 0.0

        self.brightness_queue = deque()
        self.apply_cond = threading.Condition()
        self.apply_worker_stop = threading.Event()

        self.key_listener_stop = threading.Event()

        self.last_control_event = 0.0
        self.desired_brightness = None
        self.desired_set_at = 0.0
        self.last_set_value = None
        self.has_real_reading = False

        self.shutdown_started = False

        self.state_path = state_dir / "state.json"
        self.legacy_state_path = Path(f"/run/user/{os.getuid()}/brightness-indicator-state.json")

        self.indicator = AppIndicator.Indicator.new(
            "brightness-control",
            "display-brightness-symbolic",
            AppIndicator.IndicatorCategory.HARDWARE,
        )
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.indicator.set_label("--%", "100%")

        self.menu = Gtk.Menu()
        self.create_menu()
        self.indicator.set_menu(self.menu)

        self.log.info("app started")

        self.load_state_cache()
        if self.last_set_value is not None:
            self.update_indicator_label(self.last_set_value)

        # Force label update again once GTK main loop is fully running.
        GLib.idle_add(self.ensure_startup_label)
        for sec in self.STARTUP_LABEL_RETRY_SECONDS:
            GLib.timeout_add_seconds(sec, self.ensure_startup_label)

        self.start_bootstrap_worker()
        self.start_apply_worker()
        self.start_key_listener()

        GLib.timeout_add_seconds(5, self.refresh_brightness_label)
        GLib.timeout_add_seconds(2, self.health_check)

    def create_menu(self):
        presets = [100, 75, 50, 25, 0]

        for value in presets:
            item = Gtk.MenuItem(label=f"Set {value}%")
            item.connect("activate", lambda _w, v=value: self.set_brightness(v))
            self.menu.append(item)

        self.menu.append(Gtk.SeparatorMenuItem())

        refresh_item = Gtk.MenuItem(label="Refresh")
        refresh_item.connect("activate", lambda _w: self.load_current_brightness())
        self.menu.append(refresh_item)

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", self.quit_app)
        self.menu.append(quit_item)

        self.menu.show_all()

    def health_check(self):
        if self.shutdown_started:
            return False

        if getattr(self, "apply_worker_thread", None) is None or not self.apply_worker_thread.is_alive():
            self.log.error("apply worker thread not alive, restarting")
            self.start_apply_worker()

        if InputDevice is not None:
            if getattr(self, "key_listener_thread", None) is None or not self.key_listener_thread.is_alive():
                self.log.error("key listener thread not alive, restarting")
                self.start_key_listener()

        return True

    def detect_ddc_prefix(self):
        if self.ddc_detected_prefix:
            return

        prefer_sudo = os.environ.get("BRIGHTNESS_USE_SUDO", "0") == "1"
        candidates = [["sudo", "-n", "ddcutil"], ["ddcutil"]] if prefer_sudo else [["ddcutil"], ["sudo", "-n", "ddcutil"]]

        for candidate in candidates:
            try:
                result = subprocess.run(candidate + ["--version"], capture_output=True, text=True, timeout=2)
                if result.returncode == 0:
                    self.ddc_cmd_prefix = candidate
                    self.ddc_detected_prefix = True
                    self.log.info("using ddc command: %s", " ".join(candidate))
                    return
            except Exception:
                continue

        self.ddc_cmd_prefix = ["ddcutil"]
        self.ddc_detected_prefix = True
        self.log.warning("failed to validate ddcutil command prefix, defaulting to 'ddcutil'")

    def run_ddcutil(self, args, display_id=None, timeout=5):
        self.detect_ddc_prefix()
        command = list(self.ddc_cmd_prefix)
        if display_id is not None:
            command.extend(["--display", str(display_id)])
        command.extend(args)

        with self.ddc_lock:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)

            # Auto-fallback if direct ddcutil lacks permissions.
            if command[0] == "ddcutil" and result.returncode != 0:
                stderr_lower = (result.stderr or "").lower()
                if "permission denied" in stderr_lower or "not permitted" in stderr_lower:
                    sudo_cmd = ["sudo", "-n", "ddcutil"]
                    retry_cmd = sudo_cmd + command[1:]
                    retry = subprocess.run(retry_cmd, capture_output=True, text=True, timeout=timeout)
                    if retry.returncode == 0:
                        self.ddc_cmd_prefix = sudo_cmd
                        self.log.info("switched to sudo -n ddcutil")
                        return retry

            return result

    def load_state_cache(self):
        for path in (self.state_path, self.legacy_state_path):
            try:
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                brightness = data.get("brightness")
                displays = data.get("displays", [])
                parsed = None
                if isinstance(brightness, int):
                    parsed = brightness
                elif isinstance(brightness, float):
                    parsed = int(round(brightness))
                elif isinstance(brightness, str) and brightness.strip().isdigit():
                    parsed = int(brightness.strip())
                if parsed is not None:
                    self.last_set_value = max(0, min(100, parsed))
                if isinstance(displays, list):
                    self.ddc_displays = [str(d) for d in displays if str(d).isdigit()]
                    self.supported_displays = list(self.ddc_displays)
                self.log.info(
                    "state cache loaded from %s: brightness=%s displays=%s",
                    path,
                    self.last_set_value,
                    ",".join(self.ddc_displays) if self.ddc_displays else "none",
                )
                return
            except Exception:
                continue

    def ensure_startup_label(self):
        if self.has_real_reading:
            return False
        if self.last_set_value is not None:
            self.update_indicator_label(self.last_set_value)
            self.log.info("startup label ensured: %s%%", self.last_set_value)
        return False

    def save_state_cache(self):
        payload = {
            "brightness": self.last_set_value,
            "displays": self.ddc_displays,
            "updated": int(time.time()),
        }
        tmp_path = self.state_path.with_suffix(".tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp_path, self.state_path)
        except Exception:
            pass

    def start_bootstrap_worker(self):
        t = threading.Thread(target=self.thread_guard("bootstrap", self.bootstrap_worker_loop), daemon=True)
        t.start()
        self.bootstrap_thread = t

    def bootstrap_worker_loop(self):
        attempt = 0
        while not self.shutdown_started and not self.has_real_reading:
            force_discovery = (attempt == 0) or (attempt % 10 == 0)
            ok = self.load_current_brightness(force_discovery=force_discovery, context="startup")
            if ok:
                return
            attempt += 1
            if attempt % self.STARTUP_WARN_EVERY_ATTEMPTS == 0:
                self.log.warning("startup brightness read pending: attempt=%s", attempt)
            time.sleep(self.STARTUP_RETRY_INTERVAL_SECONDS)

    def discover_ddc_displays(self, force=False):
        now = time.monotonic()
        if not force and self.ddc_displays and (now - self.last_display_refresh) < self.DISPLAY_CACHE_TTL_SECONDS:
            return list(self.ddc_displays)

        displays = []
        try:
            result = self.run_ddcutil(["detect"], timeout=5)
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("Display "):
                        parts = stripped.split()
                        if len(parts) > 1 and parts[1].isdigit():
                            displays.append(parts[1])
        except Exception:
            pass

        if displays:
            self.ddc_displays = displays
            self.supported_displays = list(displays)
            self.last_display_refresh = now
            self.save_state_cache()
            self.log.info("ddc displays detected=%s", ",".join(self.ddc_displays))

        return list(self.ddc_displays)

    def get_display_brightness(self, display_id):
        try:
            result = self.run_ddcutil(["getvcp", "10"], display_id=display_id, timeout=2)
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if "current value" in line:
                        parts = line.split("current value =")
                        if len(parts) > 1:
                            return int(parts[1].split(",")[0].strip())
            else:
                self.log.debug(
                    "getvcp failed display=%s rc=%s stderr=%s",
                    display_id,
                    result.returncode,
                    (result.stderr or "").strip(),
                )
        except Exception:
            pass
        return None

    def get_current_brightness(self, force_discovery=False, context="periodic"):
        displays = self.discover_ddc_displays(force=force_discovery)
        if not displays:
            self.log.debug("brightness read [%s]: no displays", context)
            return None

        candidates = self.supported_displays or displays
        for display_id in candidates:
            value = self.get_display_brightness(display_id)
            if value is not None:
                self.log.info("brightness read [%s]: display=%s value=%s", context, display_id, value)
                return value
        self.log.debug("brightness read [%s]: no readable display", context)
        return None

    def update_indicator_label(self, value):
        if threading.get_ident() != self.main_thread_id:
            GLib.idle_add(self.update_indicator_label, value)
            return False

        try:
            self.indicator.set_label(f"{value}%", "100%")
        except Exception:
            self.log.exception("failed to update indicator label")
        return False

    def handle_detected_brightness(self, current):
        first_real_read = False
        if not self.has_real_reading:
            self.has_real_reading = True
            first_real_read = True
            self.log.info("first real brightness read=%s%%", current)

        now = time.monotonic()
        if self.desired_brightness is not None:
            if abs(current - self.desired_brightness) <= self.BRIGHTNESS_MATCH_TOLERANCE:
                self.desired_brightness = None
            elif (now - self.desired_set_at) < self.BRIGHTNESS_SETTLE_SECONDS:
                return
            elif (now - self.last_control_event) < self.CONTROL_QUIET_WINDOW_SECONDS:
                # Keep showing requested value for a short quiet window after key/menu control.
                return
            else:
                self.desired_brightness = None

        if (not first_real_read) and self.last_set_value is not None:
            if abs(current - self.last_set_value) < self.MEASURED_LABEL_MIN_DELTA:
                return

        self.update_indicator_label(current)
        self.last_set_value = current
        self.save_state_cache()

    def load_current_brightness(self, force_discovery=False, context="periodic"):
        current = self.get_current_brightness(force_discovery=force_discovery, context=context)
        if current is not None:
            self.handle_detected_brightness(current)
            return True
        return False

    def refresh_brightness_label(self):
        if self.shutdown_started:
            return False

        if not self.has_real_reading:
            self.load_current_brightness(force_discovery=True, context="startup-fallback")
            return True

        now = time.monotonic()
        if (now - self.last_control_event) < self.CONTROL_QUIET_WINDOW_SECONDS:
            return True

        with self.apply_cond:
            if self.brightness_queue:
                return True

        self.load_current_brightness(context="periodic")
        return True

    def apply_brightness_now(self, value):
        value = max(0, min(100, int(value)))
        try:
            displays = self.discover_ddc_displays(force=not self.ddc_displays)
            if not displays:
                self.log.warning("no DDC displays found when setting brightness")
                return False

            candidates = self.supported_displays or displays
            success_count = 0

            for display_id in candidates:
                result = self.run_ddcutil(
                    ["--sleep-multiplier", "0.25", "--noverify", "setvcp", "10", str(value)],
                    display_id=display_id,
                    timeout=3,
                )
                if result.returncode == 0:
                    success_count += 1
                else:
                    self.log.debug(
                        "setvcp failed display=%s rc=%s stderr=%s",
                        display_id,
                        result.returncode,
                        (result.stderr or "").strip(),
                    )

            if success_count > 0:
                self.last_set_value = value
                self.save_state_cache()
                return True

            self.log.warning("set brightness failed on all displays")
            return False
        except Exception:
            self.log.exception("apply_brightness_now crashed")
            return False

    def request_apply_brightness(self, value):
        with self.apply_cond:
            self.last_control_event = time.monotonic()
            self.brightness_queue.append(int(value))
            self.apply_cond.notify()

    def set_brightness(self, value):
        value = max(0, min(100, int(value)))

        self.last_control_event = time.monotonic()
        self.desired_brightness = value
        self.desired_set_at = self.last_control_event
        self.last_set_value = value

        self.update_indicator_label(value)
        self.request_apply_brightness(value)

    def start_apply_worker(self):
        self.apply_worker_stop.clear()
        t = threading.Thread(target=self.thread_guard("apply-worker", self.apply_worker_loop), daemon=True)
        t.start()
        self.apply_worker_thread = t
        self.log.info("apply worker started")

    def apply_worker_loop(self):
        while not self.apply_worker_stop.is_set():
            with self.apply_cond:
                while not self.brightness_queue and not self.apply_worker_stop.is_set():
                    self.apply_cond.wait(timeout=1.0)

                if self.apply_worker_stop.is_set():
                    break

                value = self.brightness_queue.popleft()

            ok = self.apply_brightness_now(value)
            if ok:
                GLib.idle_add(self.update_indicator_label, value)

    def step_brightness(self, direction):
        current = self.last_set_value
        if current is None:
            current = self.get_current_brightness()
        if current is None:
            return False

        if direction == "up":
            target = min(100, current + self.step_percent)
        else:
            target = max(0, current - self.step_percent)

        if target != current:
            self.set_brightness(target)

        return False

    def start_key_listener(self):
        if InputDevice is None or ecodes is None or list_devices is None:
            self.log.warning("evdev unavailable, keyboard brightness listener disabled")
            return

        self.key_listener_stop.clear()
        t = threading.Thread(target=self.thread_guard("key-listener", self.key_listener_loop), daemon=True)
        t.start()
        self.key_listener_thread = t
        self.log.info("keyboard brightness listener started")

    def thread_guard(self, name, fn):
        def wrapped():
            try:
                fn()
            except Exception:
                self.log.exception("thread crashed: %s", name)

        return wrapped

    def discover_key_devices(self, existing):
        watch_codes = {
            ecodes.KEY_BRIGHTNESSUP,
            ecodes.KEY_BRIGHTNESSDOWN,
            ecodes.KEY_KBDILLUMUP,
            ecodes.KEY_KBDILLUMDOWN,
        }

        paths = set(list_devices())
        opened = dict(existing)

        for path in list(opened.keys()):
            if path not in paths:
                try:
                    opened[path].close()
                except Exception:
                    pass
                opened.pop(path, None)

        for path in sorted(paths):
            if path in opened:
                continue

            try:
                device = InputDevice(path)
                keys = device.capabilities().get(ecodes.EV_KEY, [])
                if any(code in watch_codes for code in keys):
                    opened[path] = device
                    self.log.info("watching input device: %s (%s)", path, device.name)
                else:
                    device.close()
            except Exception:
                continue

        return opened

    def key_listener_loop(self):
        up_codes = {ecodes.KEY_BRIGHTNESSUP, ecodes.KEY_KBDILLUMUP}
        down_codes = {ecodes.KEY_BRIGHTNESSDOWN, ecodes.KEY_KBDILLUMDOWN}
        last_action_at = {"up": 0.0, "down": 0.0}

        devices = {}

        while not self.key_listener_stop.is_set():
            try:
                devices = self.discover_key_devices(devices)
            except Exception:
                time.sleep(1)
                continue

            readers = list(devices.values())
            if not readers:
                time.sleep(2)
                continue

            try:
                ready, _, _ = select.select(readers, [], [], 2)
            except Exception:
                continue

            if not ready:
                continue

            for dev in ready:
                try:
                    for event in dev.read():
                        if event.type != ecodes.EV_KEY or event.value != 1:
                            continue

                        direction = None
                        if event.code in up_codes:
                            direction = "up"
                        elif event.code in down_codes:
                            direction = "down"

                        if direction is None:
                            continue

                        now = time.monotonic()
                        if now - last_action_at[direction] < self.KEY_MIN_GAP_SECONDS:
                            continue

                        last_action_at[direction] = now
                        self.last_control_event = now
                        self.log.info("brightness key detected: %s", direction)
                        GLib.idle_add(self.step_brightness, direction)
                except OSError:
                    continue
                except Exception:
                    continue

    def request_shutdown(self, reason):
        if self.shutdown_started:
            return False

        self.shutdown_started = True
        self.log.warning("shutdown requested: %s", reason)
        GLib.idle_add(self.quit_app, None)
        return False

    def quit_app(self, _widget):
        if self.apply_worker_stop.is_set() and self.key_listener_stop.is_set():
            Gtk.main_quit()
            return

        self.log.info("app quit requested")

        self.key_listener_stop.set()
        self.apply_worker_stop.set()
        with self.apply_cond:
            self.apply_cond.notify_all()

        if self.lock_fd is not None:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
                os.close(self.lock_fd)
            except Exception:
                pass
            self.lock_fd = None

        Gtk.main_quit()


APP_REF = {"instance": None}


def install_signal_handlers():
    logger = logging.getLogger(APP_NAME)

    def _handler(signum, _frame):
        sig_name = signal.Signals(signum).name
        logger.warning("received signal: %s", sig_name)
        app = APP_REF.get("instance")
        if app is None:
            os._exit(128 + signum)
        app.request_shutdown(f"signal:{sig_name}")

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
        try:
            signal.signal(sig, _handler)
        except Exception:
            continue


def configure_logging(state_dir: Path):
    log_path = state_dir / "app.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s pid=%(process)d %(message)s",
        handlers=[logging.FileHandler(log_path), logging.StreamHandler(sys.stderr)],
    )


def configure_faulthandler(state_dir: Path):
    crash_path = state_dir / "crash.log"
    crash_fp = None
    try:
        crash_fp = crash_path.open("a", buffering=1, encoding="utf-8")
        faulthandler.enable(crash_fp)
    except Exception:
        crash_fp = None
    return crash_fp


def main():
    state_dir = get_state_dir()
    configure_logging(state_dir)
    crash_fp = configure_faulthandler(state_dir)

    runtime_dir = get_runtime_dir()
    lock_path = runtime_dir / f"{APP_NAME}.lock"
    lock_fd = acquire_singleton_lock(lock_path)
    if lock_fd is None:
        logging.getLogger(APP_NAME).info("another instance is running, exiting")
        return 0

    install_signal_handlers()

    logger = logging.getLogger(APP_NAME)

    @atexit.register
    def _on_exit():
        logger.info("process exiting")

    try:
        app = BrightnessIndicator(lock_fd=lock_fd, state_dir=state_dir)
        APP_REF["instance"] = app
        Gtk.main()
        logger.info("gtk main loop exited")
        return 0
    except Exception:
        logger.exception("fatal crash in main thread")
        raise
    finally:
        if crash_fp is not None:
            try:
                crash_fp.flush()
                crash_fp.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
