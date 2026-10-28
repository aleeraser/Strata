#!/usr/bin/env python3
"""
Strata System Tray Indicator for GNOME / Linux
Built with PySide6 for native StatusNotifierItem / AppIndicator support.
Allows releasing VRAM/RAM (unload), restarting, stopping, and switching models.
Includes live RAM pre-check and memory footprints for model variants.
"""

import os
import sys
import json
import signal
import threading
import subprocess
import urllib.request
import urllib.error
import fcntl

from PySide6.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu, QMessageBox
)
from PySide6.QtGui import (
    QIcon, QPixmap, QPainter, QColor, QBrush, QPen, QAction, QActionGroup, QCursor
)
from PySide6.QtCore import (
    QTimer, Qt, QObject, Signal, Slot
)

STRATA_DIR = os.path.expanduser("~/git/Strata")
CURRENT_LINK = os.path.join(STRATA_DIR, "run-current.sh")
SWITCH_SCRIPT = os.path.expanduser("~/.local/bin/strata-switch")
LOG_PATH = "/tmp/strata.log"
USER_ICON_DIR = os.path.expanduser("~/.local/share/strata-tray/icons")
REPO_ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
ICON_DIR = USER_ICON_DIR if os.path.isdir(USER_ICON_DIR) else REPO_ICON_DIR

# Model definitions and memory footprints (Weights + Pinned KV Cache)
MODEL_VARIANTS = [
    {
        "id": "nvfp4-512k",
        "name": "Qwen 3.8 Flash Next (NVFP4 - 512K)",
        "ram_gib": 69.5,
        "weights_gib": 63.3,
        "kv_gib": 6.2,
        "group": "NVFP4"
    },
    {
        "id": "nvfp4-256k",
        "name": "Qwen 3.8 Flash Next (NVFP4 - 256K)",
        "ram_gib": 66.4,
        "weights_gib": 63.3,
        "kv_gib": 3.1,
        "group": "NVFP4"
    },
    {
        "id": "iq3_s-1m",
        "name": "Qwen 3.8 Flash Next (IQ3_S - 1M)",
        "ram_gib": 54.4,
        "weights_gib": 42.0,
        "kv_gib": 12.4,
        "group": "IQ3_S"
    },
    {
        "id": "iq3_s-512k",
        "name": "Qwen 3.8 Flash Next (IQ3_S - 512K)",
        "ram_gib": 48.2,
        "weights_gib": 42.0,
        "kv_gib": 6.2,
        "group": "IQ3_S"
    },
    {
        "id": "iq3_s-256k",
        "name": "Qwen 3.8 Flash Next (IQ3_S - 256K)",
        "ram_gib": 45.1,
        "weights_gib": 42.0,
        "kv_gib": 3.1,
        "group": "IQ3_S"
    },
]

MODEL_MAP = {m["id"]: m for m in MODEL_VARIANTS}


def get_mem_available_gib():
    """Reads exact available RAM in GiB from /proc/meminfo."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    kb = int(line.split()[1])
                    return round(kb / (1024 * 1024), 1)
    except Exception:
        pass
    return 0.0


def send_notification(title, message, icon_name="strata-standby"):
    icon_path = os.path.join(ICON_DIR, f"{icon_name}.png")
    cmd = ["notify-send", "-a", "Strata", title, message]
    if os.path.exists(icon_path):
        cmd.extend(["-i", icon_path])
    try:
        subprocess.Popen(cmd)
    except Exception:
        pass


def make_icon(color_hex, filled=True):
    pix = QPixmap(64, 64)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    color = QColor(color_hex)

    painter.setBrush(Qt.NoBrush)
    painter.setPen(QPen(color, 5))
    painter.drawEllipse(6, 6, 52, 52)

    if filled:
        painter.setBrush(QBrush(color))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(18, 18, 28, 28)

    painter.end()
    return QIcon(pix)


class StatusWorker(QObject):
    status_ready = Signal(dict)

    def fetch(self):
        def task():
            # 1. Systemd active check
            try:
                p = subprocess.run(
                    ["systemctl", "--user", "is-active", "strata.service"],
                    capture_output=True,
                    text=True,
                    timeout=1.5
                )
                is_active = (p.stdout.strip() == "active")
            except Exception:
                is_active = False

            # Detect active model variant from symlink
            variant = "iq3_s-1m"
            try:
                if os.path.islink(CURRENT_LINK):
                    tgt = os.path.basename(os.path.realpath(CURRENT_LINK))
                    if "nvfp4-256k" in tgt:
                        variant = "nvfp4-256k"
                    elif "nvfp4" in tgt:
                        variant = "nvfp4-512k"
                    elif "iq3_s-256k" in tgt or "iq3-256k" in tgt:
                        variant = "iq3_s-256k"
                    elif "iq3_s-512k" in tgt or "iq3-512k" in tgt:
                        variant = "iq3_s-512k"
                    elif "iq3" in tgt:
                        variant = "iq3_s-1m"
            except Exception:
                pass

            avail_ram = get_mem_available_gib()

            if not is_active:
                self.status_ready.emit({
                    "state": "STOPPED",
                    "variant": variant,
                    "model": "None",
                    "loaded": False,
                    "in_flight": 0,
                    "avail_ram": avail_ram
                })
                return

            # 2. Strata API status
            try:
                req = urllib.request.Request("http://127.0.0.1:8080/v1/status")
                with urllib.request.urlopen(req, timeout=1.2) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    loaded = bool(data.get("loaded", False))
                    model = data.get("model", "strata-model")
                    activity = data.get("activity", {})
                    in_flight = int(activity.get("in_flight", 0))

                    self.status_ready.emit({
                        "state": "ACTIVE" if loaded else "STANDBY",
                        "variant": variant,
                        "model": model,
                        "loaded": loaded,
                        "in_flight": in_flight,
                        "avail_ram": avail_ram
                    })
            except Exception:
                self.status_ready.emit({
                    "state": "STARTING",
                    "variant": variant,
                    "model": "Booting...",
                    "loaded": False,
                    "in_flight": 0,
                    "avail_ram": avail_ram
                })

        threading.Thread(target=task, daemon=True).start()


class StrataTrayApp(QObject):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.state = "UNKNOWN"
        self.variant = "iq3_s-1m"
        self.in_flight = 0
        self.avail_ram = get_mem_available_gib()
        self.busy = False

        # Icons with system theme fallback
        self.icon_active = QIcon.fromTheme("strata-active", QIcon(os.path.join(ICON_DIR, "strata-active.png")))
        self.icon_standby = QIcon.fromTheme("strata-standby", QIcon(os.path.join(ICON_DIR, "strata-standby.png")))
        self.icon_stopped = QIcon.fromTheme("strata-stopped", QIcon(os.path.join(ICON_DIR, "strata-stopped.png")))
        self.icon_busy = QIcon.fromTheme("strata-busy", QIcon(os.path.join(ICON_DIR, "strata-busy.png")))

        # Tray Icon
        self.tray = QSystemTrayIcon(self.icon_standby, app)
        self.tray.setToolTip("Strata Inference Server")
        self.tray.activated.connect(self.on_tray_activated)

        # Build Menu
        self.build_menu()

        # Status Worker
        self.worker = StatusWorker()
        self.worker.status_ready.connect(self.apply_status)

        # Polling Timer (every 3 seconds)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll_status)
        self.timer.start(3000)

        # Initial poll
        self.poll_status()
        self.tray.show()

    def build_menu(self):
        self.menu = QMenu()

        # Status Header
        self.act_status = QAction("Strata: Initializing...", self.menu)
        font = self.act_status.font()
        font.setBold(True)
        self.act_status.setFont(font)
        self.act_status.setEnabled(False)
        self.menu.addAction(self.act_status)

        # Details Header
        self.act_details = QAction("Checking state...", self.menu)
        self.act_details.setEnabled(False)
        self.menu.addAction(self.act_details)

        self.menu.addSeparator()

        # Primary Action (Unload / Load / Start)
        self.act_primary = QAction("Release VRAM (Unload)", self.menu)
        self.act_primary.triggered.connect(self.on_primary_action)
        self.menu.addAction(self.act_primary)

        # Restart Service
        self.act_restart = QAction("Restart Service (Lazy Mode)", self.menu)
        self.act_restart.triggered.connect(self.on_restart_service)
        self.menu.addAction(self.act_restart)

        # Stop / Start Service
        self.act_toggle = QAction("Stop Strata Service", self.menu)
        self.act_toggle.triggered.connect(self.on_toggle_service)
        self.menu.addAction(self.act_toggle)

        self.menu.addSeparator()

        # Switch Model Submenu
        self.switch_menu = self.menu.addMenu("Switch Active Model")
        self.switch_menu.aboutToShow.connect(self.update_switch_menu_labels)

        self.action_group = QActionGroup(self.menu)
        self.action_group.setExclusive(True)

        self.model_actions = {}
        last_group = None
        for m in MODEL_VARIANTS:
            if last_group and last_group != m["group"]:
                self.switch_menu.addSeparator()
            last_group = m["group"]

            act = QAction(m["name"], self.switch_menu)
            act.setCheckable(True)
            act.triggered.connect(lambda checked=False, mid=m["id"]: self.on_model_action_triggered(mid))
            self.action_group.addAction(act)
            self.switch_menu.addAction(act)
            self.model_actions[m["id"]] = act

        self.switch_menu.addSeparator()
        self.act_mem_info = QAction("Available RAM: Checking...", self.switch_menu)
        self.act_mem_info.setEnabled(False)
        self.switch_menu.addAction(self.act_mem_info)

        self.menu.addSeparator()

        # Logs & Quit
        self.act_logs = QAction("View Live Logs (/tmp/strata.log)", self.menu)
        self.act_logs.triggered.connect(self.view_logs)
        self.menu.addAction(self.act_logs)

        self.act_quit = QAction("Quit Tray Icon", self.menu)
        self.act_quit.triggered.connect(self.app.quit)
        self.menu.addAction(self.act_quit)

        self.update_switch_menu_labels()
        self.tray.setContextMenu(self.menu)

    def update_switch_menu_labels(self):
        """Updates model actions with live RAM pre-check and memory footprint."""
        avail = get_mem_available_gib()
        self.avail_ram = avail
        is_active = (self.state == "ACTIVE")
        current_meta = MODEL_MAP.get(self.variant, {})
        current_footprint = current_meta.get("ram_gib", 0.0) if is_active else 0.0
        effective_avail = avail + current_footprint

        for m in MODEL_VARIANTS:
            act = self.model_actions[m["id"]]
            req = m["ram_gib"]

            if is_active and m["id"] == self.variant:
                label = f"{m['name']}   [~{req} GB — ● Currently Loaded]"
            elif effective_avail >= req:
                margin = round(effective_avail - req, 1)
                if is_active:
                    diff = round(req - current_footprint, 1)
                    if diff < 0:
                        label = f"{m['name']}   [~{req} GB — ✓ Fits (frees {-diff:.1f} GB, +{margin:.1f} GB margin)]"
                    elif diff > 0:
                        label = f"{m['name']}   [~{req} GB — ✓ Fits (+{diff:.1f} GB net, +{margin:.1f} GB margin)]"
                    else:
                        label = f"{m['name']}   [~{req} GB — ✓ Fits (same size, +{margin:.1f} GB margin)]"
                else:
                    label = f"{m['name']}   [~{req} GB — ✓ Fits (+{margin:.1f} GB)]"
            else:
                shortfall = round(req - effective_avail, 1)
                label = f"{m['name']}   [~{req} GB — ⚠️ Needs +{shortfall:.1f} GB more free]"

            act.setText(label)

        if is_active:
            self.act_mem_info.setText(f"Available RAM: {avail:.1f} GiB  |  Current Model in RAM: ~{current_footprint:.1f} GB")
        else:
            self.act_mem_info.setText(f"System Available RAM: {avail:.1f} GiB  (Standby: 0 GB used)")

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.update_switch_menu_labels()
            self.menu.popup(QCursor.pos())

    def poll_status(self):
        if not self.busy:
            self.worker.fetch()

    @Slot(dict)
    def apply_status(self, info):
        self.state = info["state"]
        self.variant = info["variant"]
        self.in_flight = info["in_flight"]
        self.avail_ram = info.get("avail_ram", self.avail_ram)
        self.update_switch_menu_labels()

        # Update checked radio
        for mid, act in self.model_actions.items():
            act.blockSignals(True)
            act.setChecked(mid == self.variant)
            act.blockSignals(False)

        current_meta = MODEL_MAP.get(self.variant, {
            "name": self.variant,
            "ram_gib": 50.0
        })
        current_name = current_meta["name"]
        current_footprint = current_meta["ram_gib"]

        if self.state == "ACTIVE":
            self.tray.setIcon(self.icon_active)
            self.tray.setToolTip(f"Strata: Running (Model in VRAM)\nModel: {current_name} (~{current_footprint} GB)\nAvailable RAM: {self.avail_ram:.1f} GiB")
            self.act_status.setText("● Strata: Running (Model in VRAM)")
            details = f"Active: {current_name} [~{current_footprint} GB]"
            if self.in_flight > 0:
                details += f" [Generating: {self.in_flight}]"
            self.act_details.setText(details)
            self.act_primary.setText("Release VRAM / Unload Model")
            self.act_primary.setEnabled(True)
            self.act_restart.setEnabled(True)
            self.act_toggle.setText("Stop Strata Service")
            self.act_toggle.setEnabled(True)

        elif self.state == "STANDBY":
            self.tray.setIcon(self.icon_standby)
            self.tray.setToolTip(f"Strata: Standby (0 GB VRAM used)\nReady on query: {current_name}\nAvailable RAM: {self.avail_ram:.1f} GiB")
            self.act_status.setText("○ Strata: Standby (0 GB VRAM used)")
            self.act_details.setText(f"Standby: {current_name} [~{current_footprint} GB]")
            self.act_primary.setText("Preload Model into VRAM")
            self.act_primary.setEnabled(True)
            self.act_restart.setEnabled(True)
            self.act_toggle.setText("Stop Strata Service")
            self.act_toggle.setEnabled(True)

        elif self.state == "STARTING":
            self.tray.setIcon(self.icon_busy)
            self.tray.setToolTip("Strata: Booting / Transitioning...")
            self.act_status.setText("⏳ Strata: Booting...")
            self.act_details.setText("Server initializing...")
            self.act_primary.setEnabled(False)
            self.act_restart.setEnabled(True)
            self.act_toggle.setText("Stop Strata Service")
            self.act_toggle.setEnabled(True)

        elif self.state == "STOPPED":
            self.tray.setIcon(self.icon_stopped)
            self.tray.setToolTip(f"Strata: Service Stopped\nConfigured: {current_name}")
            self.act_status.setText("✕ Strata: Service Stopped")
            self.act_details.setText(f"Configured: {current_name} [~{current_footprint} GB]")
            self.act_primary.setText("Start Strata Service")
            self.act_primary.setEnabled(True)
            self.act_restart.setEnabled(False)
            self.act_toggle.setText("Start Strata Service")
            self.act_toggle.setEnabled(True)

    def on_primary_action(self):
        if self.state == "ACTIVE":
            self.execute_async(
                self._action_unload,
                "Releasing VRAM & RAM...",
                "Strata",
                "VRAM & RAM released. Model will reload on next query.",
                "strata-standby"
            )
        elif self.state == "STANDBY":
            # RAM Pre-check before manual preload
            target_meta = MODEL_MAP.get(self.variant, {"ram_gib": 50.0, "name": self.variant})
            avail = get_mem_available_gib()
            if avail < target_meta["ram_gib"]:
                shortfall = round(target_meta["ram_gib"] - avail, 1)
                reply = QMessageBox.warning(
                    None,
                    "Low Available RAM Warning",
                    f"<b>{target_meta['name']}</b> requires ~<b>{target_meta['ram_gib']} GiB</b> of RAM.<br><br>"
                    f"Your system currently has only <b>{avail:.1f} GiB</b> available (short by <b>{shortfall:.1f} GiB</b>).<br><br>"
                    f"Preloading now may trigger swap thrashing or an OOM kill.<br><br>"
                    f"Do you want to proceed anyway?",
                    QMessageBox.Cancel | QMessageBox.Ok,
                    QMessageBox.Cancel
                )
                if reply != QMessageBox.Ok:
                    return

            self.execute_async(
                self._action_load,
                "Preloading model into VRAM...",
                "Strata",
                "Model preloaded into VRAM.",
                "strata-active"
            )
        elif self.state == "STOPPED":
            self.execute_async(
                self._action_start,
                "Starting Strata...",
                "Strata",
                "Strata service started in Standby mode.",
                "strata-standby"
            )

    def on_restart_service(self):
        self.execute_async(
            self._action_restart,
            "Restarting service in lazy mode...",
            "Strata",
            "Strata restarted in Standby mode. Memory released.",
            "strata-standby"
        )

    def on_toggle_service(self):
        if self.state == "STOPPED":
            self.execute_async(
                self._action_start,
                "Starting Strata...",
                "Strata",
                "Strata service started (Standby mode).",
                "strata-standby"
            )
        else:
            self.execute_async(
                self._action_stop,
                "Stopping Strata...",
                "Strata",
                "Strata service stopped. All resources freed.",
                "strata-stopped"
            )

    def on_model_action_triggered(self, target_id):
        if target_id == self.variant:
            return

        target_meta = MODEL_MAP.get(target_id)
        if not target_meta:
            return

        # RAM Pre-check taking into account whether current model is loaded
        avail = get_mem_available_gib()
        req = target_meta["ram_gib"]
        is_active = (self.state == "ACTIVE")
        current_meta = MODEL_MAP.get(self.variant, {})
        current_footprint = current_meta.get("ram_gib", 0.0) if is_active else 0.0
        effective_avail = avail + current_footprint

        if effective_avail < req:
            shortfall = round(req - effective_avail, 1)
            extra_msg = ""
            if is_active:
                extra_msg = (
                    f"<br><i>(Note: Even after releasing ~{current_footprint:.1f} GiB currently occupied "
                    f"by {current_meta.get('name', self.variant)}, your system will still be short.)</i><br>"
                )
            reply = QMessageBox.warning(
                None,
                "Low Available RAM Warning",
                f"<b>{target_meta['name']}</b> requires approximately <b>{req} GiB</b> of RAM.<br><br>"
                f"Effective available memory for switching: <b>{effective_avail:.1f} GiB</b> "
                f"({avail:.1f} GiB free + ~{current_footprint:.1f} GiB released from current model).<br>"
                f"Short by <b>{shortfall:.1f} GiB</b>.{extra_msg}<br>"
                f"If you send queries while available memory is insufficient, the Linux OOM killer is likely to terminate Strata or other active applications.<br><br>"
                f"Do you want to switch to this model anyway?",
                QMessageBox.Cancel | QMessageBox.Ok,
                QMessageBox.Cancel
            )
            if reply != QMessageBox.Ok:
                # Revert selection
                for mid, act in self.model_actions.items():
                    act.blockSignals(True)
                    act.setChecked(mid == self.variant)
                    act.blockSignals(False)
                return

        def do_switch():
            subprocess.run([SWITCH_SCRIPT, target_id], check=True)

        self.execute_async(
            do_switch,
            f"Switching to {target_meta['name']}...",
            "Strata",
            f"Switched to {target_meta['name']} [~{req} GB]. Ready in Standby mode.",
            "strata-standby"
        )

    def execute_async(self, fn, busy_msg, notif_title, notif_msg, notif_icon):
        self.busy = True
        self.tray.setIcon(self.icon_busy)
        self.act_status.setText(f"⏳ {busy_msg}")
        self.act_primary.setEnabled(False)
        self.act_restart.setEnabled(False)
        self.act_toggle.setEnabled(False)

        def run():
            try:
                fn()
                send_notification(notif_title, notif_msg, notif_icon)
            except Exception as e:
                send_notification("Strata Error", f"Action failed: {e}", "strata-stopped")
            finally:
                self.busy = False
                self.worker.fetch()

        threading.Thread(target=run, daemon=True).start()

    def _action_unload(self):
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:8080/unload",
                data=b"{}",
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                if resp.status in (200, 204):
                    return
        except Exception:
            pass
        subprocess.run(["systemctl", "--user", "restart", "strata.service"], check=True, timeout=10)

    def _action_load(self):
        req = urllib.request.Request(
            "http://127.0.0.1:8080/load",
            data=b"{}",
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            pass

    def _action_restart(self):
        subprocess.run(["systemctl", "--user", "restart", "strata.service"], check=True, timeout=15)

    def _action_stop(self):
        subprocess.run(["systemctl", "--user", "stop", "strata.service"], check=True, timeout=10)

    def _action_start(self):
        subprocess.run(["systemctl", "--user", "start", "strata.service"], check=True, timeout=10)

    def view_logs(self):
        cmd = [
            "gnome-terminal",
            "--title=Strata Inference Logs",
            "--",
            "bash", "-c", f"echo '--- Tailing {LOG_PATH} (Ctrl+C to close) ---'; tail -n 50 -f {LOG_PATH}"
        ]
        try:
            subprocess.Popen(cmd)
        except Exception as e:
            send_notification("Error", f"Could not launch terminal: {e}")


def acquire_instance_lock():
    lock_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}" or "/tmp"
    lock_path = os.path.join(lock_dir, "strata-tray.lock")
    try:
        lock_file = open(lock_path, "w")
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_file
    except (BlockingIOError, OSError):
        print("Another instance of strata-tray is already running. Exiting.")
        sys.exit(0)


def main():
    lock = acquire_instance_lock()
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    tray_app = StrataTrayApp(app)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
