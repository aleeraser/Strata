#!/usr/bin/env python3
"""
Strata System Tray Indicator for GNOME / Linux
Built with PySide6 for native StatusNotifierItem / AppIndicator support.
Allows releasing VRAM/RAM (unload), restarting, stopping, and switching models.
"""

import os
import sys
import json
import signal
import threading
import subprocess
import urllib.request
import urllib.error

from PySide6.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu
)
from PySide6.QtGui import (
    QIcon, QPixmap, QPainter, QColor, QBrush, QPen, QAction, QActionGroup, QFont, QCursor
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
            variant = "nvfp4"
            try:
                if os.path.islink(CURRENT_LINK):
                    tgt = os.path.basename(os.path.realpath(CURRENT_LINK))
                    if "iq3" in tgt:
                        variant = "iq3_s"
            except Exception:
                pass

            if not is_active:
                self.status_ready.emit({
                    "state": "STOPPED",
                    "variant": variant,
                    "model": "None",
                    "loaded": False,
                    "in_flight": 0
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
                        "in_flight": in_flight
                    })
            except Exception:
                self.status_ready.emit({
                    "state": "STARTING",
                    "variant": variant,
                    "model": "Booting...",
                    "loaded": False,
                    "in_flight": 0
                })

        threading.Thread(target=task, daemon=True).start()


class StrataTrayApp(QObject):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.state = "UNKNOWN"
        self.variant = "nvfp4"
        self.in_flight = 0
        self.busy = False

        # Icons with system theme names for AppIndicator / StatusNotifierItem
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
        self.action_group = QActionGroup(self.menu)
        self.action_group.setExclusive(True)

        self.act_nvfp4 = QAction("Qwen 3.8 Flash Next (NVFP4 - 512K)", self.switch_menu)
        self.act_nvfp4.setCheckable(True)
        self.act_nvfp4.triggered.connect(lambda: self.switch_model("nvfp4"))
        self.action_group.addAction(self.act_nvfp4)
        self.switch_menu.addAction(self.act_nvfp4)

        self.act_iq3 = QAction("Qwen 3.8 Flash Next (IQ3_S - 1M)", self.switch_menu)
        self.act_iq3.setCheckable(True)
        self.act_iq3.triggered.connect(lambda: self.switch_model("iq3_s"))
        self.action_group.addAction(self.act_iq3)
        self.switch_menu.addAction(self.act_iq3)

        self.menu.addSeparator()

        # Logs & Quit
        self.act_logs = QAction("View Live Logs (/tmp/strata.log)", self.menu)
        self.act_logs.triggered.connect(self.view_logs)
        self.menu.addAction(self.act_logs)

        self.act_quit = QAction("Quit Tray Icon", self.menu)
        self.act_quit.triggered.connect(self.app.quit)
        self.menu.addAction(self.act_quit)

        self.tray.setContextMenu(self.menu)

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            # Left click: show context menu
            self.menu.popup(QCursor.pos())

    def poll_status(self):
        if not self.busy:
            self.worker.fetch()

    @Slot(dict)
    def apply_status(self, info):
        self.state = info["state"]
        self.variant = info["variant"]
        self.in_flight = info["in_flight"]

        # Update checked radio
        self.act_nvfp4.blockSignals(True)
        self.act_iq3.blockSignals(True)
        self.act_nvfp4.setChecked(self.variant == "nvfp4")
        self.act_iq3.setChecked(self.variant == "iq3_s")
        self.act_nvfp4.blockSignals(False)
        self.act_iq3.blockSignals(False)

        variant_label = "NVFP4 (512K)" if self.variant == "nvfp4" else "IQ3_S (1M)"

        if self.state == "ACTIVE":
            self.tray.setIcon(self.icon_active)
            self.tray.setToolTip(f"Strata: Running (Model in VRAM)\nModel: {variant_label}")
            self.act_status.setText("● Strata: Running (Model in VRAM)")
            details = f"Active Model: {variant_label}"
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
            self.tray.setToolTip(f"Strata: Standby (0 GB VRAM used)\nReady on query: {variant_label}")
            self.act_status.setText("○ Strata: Standby (0 GB VRAM used)")
            self.act_details.setText(f"Active: {variant_label} (Loads on next query)")
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
            self.tray.setToolTip("Strata: Service Stopped")
            self.act_status.setText("✕ Strata: Service Stopped")
            self.act_details.setText(f"Configured: {variant_label}")
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

    def switch_model(self, target_variant):
        if target_variant == self.variant:
            return
        pretty = "NVFP4 (512K)" if target_variant == "nvfp4" else "IQ3_S (1M)"

        def do_switch():
            subprocess.run([SWITCH_SCRIPT, target_variant], check=True)

        self.execute_async(
            do_switch,
            f"Switching to {pretty}...",
            "Strata",
            f"Switched to {pretty}. Ready in Standby mode.",
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


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    tray_app = StrataTrayApp(app)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
