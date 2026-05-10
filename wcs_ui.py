#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WCS Control Panel
Wireless Charging Station — management interface with live power monitoring.
"""

from __future__ import annotations

import collections
import json
import os
import queue
import random
import re
import threading
import time
from datetime import datetime
import tkinter as tk
from tkinter import messagebox, ttk

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import matplotlib.ticker as ticker

import pygame
import serial
from serial.tools import list_ports

# ── Config ─────────────────────────────────────────────────────────────────────
USERS_FILE   = "users.json"
DEFAULT_PORT = "COM4"
BAUD_RATE    = 115200

TAP_NFC_AUDIO_PATH        = r"audio/tappnfc.mp3"
CHARGING_START_AUDIO_PATH = r"audio/chargingstart.mp3"
CHARGING_STOP_AUDIO_PATH  = r"audio/chargingstops.mp3"

PLOT_WINDOW_S = 60          # seconds of history shown in the live plot
INA_RE = re.compile(r"INA V=([-\d.]+) I=([-\d.]+)")

# ── Colour palette ─────────────────────────────────────────────────────────────
# Charcoal-slate base + amber accent — feels like an engineering tool, not a dashboard template
BG       = "#12151a"
SURFACE  = "#1a1e27"
SURFACE2 = "#252a36"
BORDER   = "#2e3442"

ACCENT   = "#e8a020"    # amber  — power / electricity
SUCCESS  = "#2bbb85"    # teal-green
WARNING  = "#e07b39"    # burnt orange
DANGER   = "#d94f4f"    # muted red
INFO     = "#5baef0"    # steel blue
DIM      = "#3a4256"    # very muted

TEXT     = "#d4dae8"
MUTED    = "#596177"
WHITE    = "#eef1f8"

TOPUP_C  = "#7b6ee8"    # slate-purple
CHARGE_C = "#2a7fd4"    # cobalt blue

# Plot
PLOT_BG = "#0d1015"
PLOT_V  = ACCENT        # voltage trace — amber
PLOT_I  = SUCCESS       # current trace — teal

# Font shortcuts
F_MONO   = ("Consolas", 10)
F_LABEL  = ("Segoe UI", 8, "bold")
F_BODY   = ("Segoe UI", 10)
F_BOLD   = ("Segoe UI", 10, "bold")
F_HEADER = ("Segoe UI", 16, "bold")
F_TINY   = ("Segoe UI", 8)


# ── User persistence ───────────────────────────────────────────────────────────

def load_users() -> dict[str, int]:
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {str(k): int(v) for k, v in data.items()}
        except (json.JSONDecodeError, ValueError, KeyError):
            pass
    return {}


def save_users(users: dict[str, int]) -> None:
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2, ensure_ascii=False)


def generate_unique_id(existing_ids: set[int]) -> int:
    while True:
        candidate = random.randint(1_000_000, 9_999_999)
        if candidate not in existing_ids:
            return candidate


def list_serial_ports() -> list[str]:
    return [p.device for p in list_ports.comports()]


# ── Application ────────────────────────────────────────────────────────────────

class WCSApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()

        self.title("WCS Control Panel")
        self.geometry("1260x800")
        self.minsize(900, 640)
        self.configure(bg=BG)

        # State
        self.users: dict[str, int] = load_users()
        self.serial_conn: serial.Serial | None = None
        self.msg_queue: queue.Queue[str] = queue.Queue()
        self.charging_active = False

        # Rolling plot buffers (240 samples @ 0.5 s = 2 min max stored)
        self._t0: float | None = None
        self._plot_time:    collections.deque[float] = collections.deque(maxlen=240)
        self._plot_voltage: collections.deque[float] = collections.deque(maxlen=240)
        self._plot_current: collections.deque[float] = collections.deque(maxlen=240)
        self._plot_power:   collections.deque[float] = collections.deque(maxlen=240)

        # Audio
        self.audio_channel: pygame.mixer.Channel | None = None
        self.sounds: dict[str, pygame.mixer.Sound | None] = {}

        self._init_audio()
        self._apply_style()
        self._build_ui()
        self._refresh_user_list()
        self._poll_queue()
        self._schedule_plot()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ─────────────────────────────────────────────────────────────────────────
    # Audio
    # ─────────────────────────────────────────────────────────────────────────

    def _init_audio(self) -> None:
        try:
            pygame.mixer.init()
            self.audio_channel = pygame.mixer.Channel(0)
            self.sounds = {
                "tap":   self._load_sound(TAP_NFC_AUDIO_PATH),
                "start": self._load_sound(CHARGING_START_AUDIO_PATH),
                "stop":  self._load_sound(CHARGING_STOP_AUDIO_PATH),
            }
            if self.sounds["tap"] and self.audio_channel:
                self.audio_channel.play(self.sounds["tap"], loops=-1)
        except Exception as exc:
            print(f"[Audio] init failed: {exc}")
            self.sounds = {"tap": None, "start": None, "stop": None}

    def _load_sound(self, path: str) -> pygame.mixer.Sound | None:
        if not os.path.exists(path):
            return None
        try:
            return pygame.mixer.Sound(path)
        except pygame.error:
            return None

    def _play(self, key: str, loops: int = 0) -> None:
        if self.audio_channel and self.sounds.get(key):
            self.audio_channel.stop()
            self.audio_channel.play(self.sounds[key], loops=loops)

    def _resume_idle(self) -> None:
        if not self.charging_active:
            self._play("tap", loops=-1)

    # ─────────────────────────────────────────────────────────────────────────
    # TTK style
    # ─────────────────────────────────────────────────────────────────────────

    def _apply_style(self) -> None:
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("T.Treeview",
                    background=SURFACE2, foreground=TEXT,
                    fieldbackground=SURFACE2, borderwidth=0,
                    rowheight=28, font=F_BODY)
        s.configure("T.Treeview.Heading",
                    background=BORDER, foreground=MUTED,
                    font=F_LABEL, relief=tk.FLAT)
        s.map("T.Treeview",
              background=[("selected", ACCENT)],
              foreground=[("selected", BG)])
        s.configure("TCombobox",
                    fieldbackground=SURFACE2, background=SURFACE2,
                    foreground=TEXT, selectbackground=ACCENT,
                    selectforeground=BG, arrowcolor=MUTED)
        s.map("TCombobox",
              fieldbackground=[("readonly", SURFACE2)],
              background=[("readonly", SURFACE2)])

    # ─────────────────────────────────────────────────────────────────────────
    # Layout
    # ─────────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self._build_header()
        self._build_topup_strip()
        body = tk.Frame(self, bg=BG)
        body.pack(fill=tk.BOTH, expand=True, padx=10)
        body.columnconfigure(0, weight=0, minsize=320)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        self._build_left(body)
        self._build_right(body)
        self._build_actionbar()

    # ── Top-up strip ──────────────────────────────────────────────────────────

    def _build_topup_strip(self) -> None:
        strip = tk.Frame(self, bg=SURFACE2, height=42)
        strip.pack(fill=tk.X, padx=10, pady=(0, 0))
        strip.pack_propagate(False)

        tk.Label(strip, text="TOP UP", bg=SURFACE2, fg=MUTED,
                 font=F_LABEL, padx=14).pack(side=tk.LEFT)

        for pts in (50, 100, 150):
            tk.Button(strip, text=f"+ {pts} pts",
                      bg=TOPUP_C, fg=WHITE, relief=tk.FLAT,
                      font=("Segoe UI", 9, "bold"),
                      padx=12, pady=0,
                      activebackground=TOPUP_C, activeforeground=WHITE,
                      bd=0, highlightthickness=0, cursor="hand2",
                      command=lambda p=pts: self._cmd_topup(p)
                      ).pack(side=tk.LEFT, padx=(0, 6), pady=8)

        tk.Label(strip, text="tap a user first, then a card",
                 bg=SURFACE2, fg=DIM, font=F_TINY).pack(side=tk.LEFT, padx=6)

    # ── Header ────────────────────────────────────────────────────────────────

    def _build_header(self) -> None:
        hdr = tk.Frame(self, bg=SURFACE, height=52)
        hdr.pack(fill=tk.X)
        hdr.pack_propagate(False)

        # Amber left-edge accent stripe
        tk.Frame(hdr, bg=ACCENT, width=5).pack(side=tk.LEFT, fill=tk.Y)

        tk.Label(hdr, text="WCS", bg=SURFACE, fg=ACCENT,
                 font=("Segoe UI", 14, "bold"), padx=14).pack(side=tk.LEFT)
        tk.Label(hdr, text="Control Panel", bg=SURFACE, fg=MUTED,
                 font=("Segoe UI", 11)).pack(side=tk.LEFT)

        # Right: connection controls
        conn = tk.Frame(hdr, bg=SURFACE)
        conn.pack(side=tk.RIGHT, padx=14)

        self.status_dot = tk.Label(conn, text="●", bg=SURFACE, fg=DANGER,
                                   font=("Segoe UI", 18))
        self.status_dot.pack(side=tk.RIGHT, padx=(4, 0))

        self.connect_btn = self._btn(conn, "Connect",
                                     bg=SUCCESS, fg=BG,
                                     cmd=self._toggle_connection)
        self.connect_btn.pack(side=tk.RIGHT, padx=(6, 4))

        self._btn(conn, "⟳", bg=SURFACE2, fg=MUTED,
                  cmd=self._refresh_ports, padx=8).pack(side=tk.RIGHT, padx=2)

        self.port_var = tk.StringVar(value=DEFAULT_PORT)
        ports = list_serial_ports()
        if ports and DEFAULT_PORT not in ports:
            self.port_var.set(ports[0])
        self.port_cb = ttk.Combobox(conn, textvariable=self.port_var,
                                    values=ports, width=9, state="readonly")
        self.port_cb.pack(side=tk.RIGHT, padx=4)

    # ── Left panel: users ─────────────────────────────────────────────────────

    def _build_left(self, parent: tk.Frame) -> None:
        left = tk.Frame(parent, bg=SURFACE)
        left.grid(row=0, column=0, sticky=tk.NSEW, padx=(0, 8), pady=(10, 10))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(3, weight=1)
        left.rowconfigure(6, weight=2)

        self._sec(left, "REGISTER USER").grid(row=0, column=0,
                                               sticky=tk.W, padx=14, pady=(12, 6))

        # Register card
        reg = tk.Frame(left, bg=SURFACE2, padx=12, pady=10)
        reg.grid(row=1, column=0, sticky=tk.EW, padx=10, pady=(0, 12))
        reg.columnconfigure(0, weight=1)

        self.name_var = tk.StringVar()
        entry = tk.Entry(reg, textvariable=self.name_var,
                         bg=BG, fg=TEXT, insertbackground=ACCENT,
                         relief=tk.FLAT, font=F_BODY,
                         highlightthickness=1,
                         highlightbackground=BORDER,
                         highlightcolor=ACCENT, bd=6)
        entry.grid(row=0, column=0, sticky=tk.EW, padx=(0, 6), ipady=5)
        entry.bind("<Return>", lambda _: self._register_user())

        self._btn(reg, "Add", bg=ACCENT, fg=BG,
                  cmd=self._register_user).grid(row=0, column=1, ipady=5)

        tk.Label(reg, text="Then tap the NFC card on the reader",
                 bg=SURFACE2, fg=MUTED, font=F_TINY,
                 wraplength=210).grid(row=1, column=0, columnspan=2,
                                      sticky=tk.W, pady=(6, 0))

        self._sec(left, "REGISTERED USERS").grid(row=2, column=0,
                                                   sticky=tk.W, padx=14, pady=(0, 4))

        # Treeview
        tw = tk.Frame(left, bg=BORDER, padx=1, pady=1)
        tw.grid(row=3, column=0, sticky=tk.NSEW, padx=10, pady=(0, 6))
        tw.rowconfigure(0, weight=1)
        tw.columnconfigure(0, weight=1)

        self.users_tree = ttk.Treeview(tw, columns=("name", "id"),
                                        show="headings", style="T.Treeview",
                                        selectmode="browse")
        self.users_tree.heading("name", text="Name")
        self.users_tree.heading("id", text="ID")
        self.users_tree.column("name", width=135, anchor=tk.W)
        self.users_tree.column("id", width=88, anchor=tk.CENTER)
        sb = ttk.Scrollbar(tw, orient=tk.VERTICAL,
                           command=self.users_tree.yview)
        self.users_tree.configure(yscrollcommand=sb.set)
        self.users_tree.grid(row=0, column=0, sticky=tk.NSEW)
        sb.grid(row=0, column=1, sticky=tk.NS)

        self._btn(left, "Remove Selected", bg=SURFACE2, fg=DANGER,
                  cmd=self._delete_user, padx=0
                  ).grid(row=4, column=0, sticky=tk.EW,
                         padx=10, pady=(0, 10), ipady=6)

        # ── Activity log ──────────────────────────────────────────────────────
        log_hdr = tk.Frame(left, bg=SURFACE)
        log_hdr.grid(row=5, column=0, sticky=tk.EW, padx=14, pady=(4, 4))
        self._sec(log_hdr, "ACTIVITY LOG").pack(side=tk.LEFT)
        self._btn(log_hdr, "Clear", bg=SURFACE2, fg=MUTED,
                  cmd=self._clear_log, padx=10).pack(side=tk.RIGHT)

        log_wrap = tk.Frame(left, bg=PLOT_BG, padx=1, pady=1)
        log_wrap.grid(row=6, column=0, sticky=tk.NSEW, padx=10, pady=(0, 10))
        log_wrap.rowconfigure(0, weight=1)
        log_wrap.columnconfigure(0, weight=1)

        self.log_text = tk.Text(log_wrap, bg=PLOT_BG, fg=TEXT,
                                font=F_MONO, relief=tk.FLAT,
                                state=tk.DISABLED, wrap=tk.WORD,
                                insertbackground=TEXT,
                                selectbackground=ACCENT,
                                padx=10, pady=8)
        log_sb = ttk.Scrollbar(log_wrap, orient=tk.VERTICAL,
                               command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_sb.set)
        self.log_text.grid(row=0, column=0, sticky=tk.NSEW)
        log_sb.grid(row=0, column=1, sticky=tk.NS)

        self.log_text.tag_configure("ts",     foreground=DIM)
        self.log_text.tag_configure("ok",     foreground=SUCCESS)
        self.log_text.tag_configure("err",    foreground=DANGER)
        self.log_text.tag_configure("card",   foreground=INFO)
        self.log_text.tag_configure("relay",  foreground=WARNING)
        self.log_text.tag_configure("system", foreground=ACCENT)
        self.log_text.tag_configure("bal",    foreground=TOPUP_C)
        self.log_text.tag_configure("ina",    foreground=DIM)
        self.log_text.tag_configure("normal", foreground=TEXT)

    # ── Right panel: live plot ────────────────────────────────────────────────

    def _build_right(self, parent: tk.Frame) -> None:
        right = tk.Frame(parent, bg=BG)
        right.grid(row=0, column=1, sticky=tk.NSEW, pady=(10, 10))
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        self._build_plot(right)

    def _build_plot(self, parent: tk.Frame) -> None:
        panel = tk.Frame(parent, bg=SURFACE)
        panel.grid(row=0, column=0, sticky=tk.NSEW)
        panel.rowconfigure(1, weight=1)
        panel.columnconfigure(0, weight=1)

        hdr = tk.Frame(panel, bg=SURFACE)
        hdr.grid(row=0, column=0, sticky=tk.EW, padx=14, pady=(10, 4))
        self._sec(hdr, "LIVE POWER").pack(side=tk.LEFT)

        self.i_label = tk.Label(hdr, text="I  —", bg=SURFACE,
                                fg=PLOT_I, font=F_BOLD)
        self.i_label.pack(side=tk.RIGHT, padx=(0, 20))
        self.v_label = tk.Label(hdr, text="V  —", bg=SURFACE,
                                fg=PLOT_V, font=F_BOLD)
        self.v_label.pack(side=tk.RIGHT, padx=(0, 8))
        self.p_label = tk.Label(hdr, text="P  —", bg=SURFACE,
                                fg=ACCENT, font=("Segoe UI", 13, "bold"))
        self.p_label.pack(side=tk.RIGHT, padx=(0, 24))

        self.fig = Figure(figsize=(1, 1), dpi=96,
                          facecolor=PLOT_BG, edgecolor=PLOT_BG)
        self.fig.subplots_adjust(left=0.08, right=0.97,
                                  top=0.95, bottom=0.09)

        self.ax_p = self.fig.add_subplot(111)
        self._style_axes()
        self._draw_placeholder()

        self.canvas = FigureCanvasTkAgg(self.fig, master=panel)
        w = self.canvas.get_tk_widget()
        w.configure(bg=PLOT_BG, highlightthickness=0)
        w.grid(row=1, column=0, sticky=tk.NSEW, padx=14, pady=(0, 10))

    # ── Action bar ────────────────────────────────────────────────────────────

    def _build_actionbar(self) -> None:
        bar = tk.Frame(self, bg=SURFACE)
        bar.pack(fill=tk.X, padx=10, pady=(0, 10))
        tk.Frame(bar, bg=BORDER, height=1).pack(fill=tk.X)

        inner = tk.Frame(bar, bg=SURFACE)
        inner.pack(fill=tk.X, padx=14, pady=10)

        def div() -> None:
            tk.Frame(inner, bg=BORDER, width=1).pack(
                side=tk.LEFT, fill=tk.Y, padx=14, pady=2)

        g2 = tk.Frame(inner, bg=SURFACE)
        g2.pack(side=tk.LEFT)
        self._sec(g2, "SET CHARGE").pack(anchor=tk.W, pady=(0, 4))
        row2 = tk.Frame(g2, bg=SURFACE)
        row2.pack()
        for pts in (50, 100, 150):
            self._btn(row2, f"{pts} pts", bg=CHARGE_C, fg=WHITE,
                      cmd=lambda p=pts: self._cmd_select(p)
                      ).pack(side=tk.LEFT, padx=(0, 4))

        div()

        g3 = tk.Frame(inner, bg=SURFACE)
        g3.pack(side=tk.LEFT)
        self._sec(g3, "UTILITY").pack(anchor=tk.W, pady=(0, 4))
        row3 = tk.Frame(g3, bg=SURFACE)
        row3.pack()
        self._btn(row3, "Balance", bg=INFO, fg=BG,
                  cmd=self._cmd_balance).pack(side=tk.LEFT, padx=(0, 4))
        self._btn(row3, "Cancel", bg=SURFACE2, fg=MUTED,
                  cmd=self._cmd_cancel).pack(side=tk.LEFT)

    # ─────────────────────────────────────────────────────────────────────────
    # Widget helpers
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _btn(parent: tk.Widget, text: str, bg: str, fg: str,
             cmd=None, padx: int = 14) -> tk.Button:
        return tk.Button(parent, text=text, bg=bg, fg=fg,
                         relief=tk.FLAT, font=F_BOLD,
                         padx=padx, pady=6,
                         activebackground=bg, activeforeground=fg,
                         command=cmd, cursor="hand2",
                         bd=0, highlightthickness=0)

    @staticmethod
    def _sec(parent: tk.Widget, text: str) -> tk.Label:
        return tk.Label(parent, text=text,
                        bg=parent.cget("bg"), fg=MUTED, font=F_LABEL)

    # ─────────────────────────────────────────────────────────────────────────
    # Serial
    # ─────────────────────────────────────────────────────────────────────────

    def _toggle_connection(self) -> None:
        if self.serial_conn and self.serial_conn.is_open:
            self._disconnect()
        else:
            self._connect()

    def _connect(self) -> None:
        port = self.port_var.get().strip()
        if not port:
            messagebox.showerror("No port", "Select a serial port first.")
            return
        try:
            self.serial_conn = serial.Serial(port, BAUD_RATE, timeout=1)
            self.connect_btn.config(text="Disconnect", bg=DANGER, fg=WHITE)
            self.status_dot.config(fg=SUCCESS)
            self._log(f"Connected to {port} @ {BAUD_RATE} baud", "system")
            threading.Thread(target=self._reader_thread, daemon=True).start()
        except serial.SerialException as exc:
            messagebox.showerror("Connection failed", str(exc))

    def _disconnect(self) -> None:
        if self.serial_conn:
            try:
                self.serial_conn.close()
            except Exception:
                pass
            self.serial_conn = None
        self.connect_btn.config(text="Connect", bg=SUCCESS, fg=BG)
        self.status_dot.config(fg=DANGER)
        self._log("Disconnected.", "system")

    def _refresh_ports(self) -> None:
        ports = list_serial_ports()
        self.port_cb.configure(values=ports)
        if ports:
            if self.port_var.get() not in ports:
                self.port_var.set(ports[0])
            self._log(f"Ports: {', '.join(ports)}", "system")
        else:
            self._log("No serial ports detected.", "err")

    def _reader_thread(self) -> None:
        while self.serial_conn and self.serial_conn.is_open:
            try:
                raw = self.serial_conn.readline()
                if raw:
                    line = raw.decode(errors="ignore").strip()
                    if line:
                        self.msg_queue.put(line)
            except serial.SerialException:
                self.msg_queue.put("__DISCONNECTED__")
                break
            except Exception:
                break

    def _poll_queue(self) -> None:
        try:
            while True:
                self._handle_line(self.msg_queue.get_nowait())
        except queue.Empty:
            pass
        self.after(50, self._poll_queue)

    # ─────────────────────────────────────────────────────────────────────────
    # Incoming line handling
    # ─────────────────────────────────────────────────────────────────────────

    def _handle_line(self, line: str) -> None:
        if line == "__DISCONNECTED__":
            self.connect_btn.config(text="Connect", bg=SUCCESS, fg=BG)
            self.status_dot.config(fg=DANGER)
            self._log("Lost connection to Arduino.", "err")
            self.serial_conn = None
            return

        upper = line.upper()

        # INA219 telemetry — feed plot, log dimly, skip verbose log
        ina_m = INA_RE.search(line)
        if ina_m:
            v = float(ina_m.group(1))
            i = float(ina_m.group(2))
            p = v * i / 1000.0          # watts
            t = time.monotonic()
            if self._t0 is None:
                self._t0 = t
            self._plot_time.append(t - self._t0)
            self._plot_voltage.append(v)
            self._plot_current.append(i)
            self._plot_power.append(p)
            self.v_label.config(text=f"V  {v:.2f}")
            self.i_label.config(text=f"I  {i:.0f} mA")
            self.p_label.config(text=f"P  {p:.2f} W")
            return   # keep log clean; plot is the display for this data

        display = self._annotate_name(line)

        if upper.startswith("ERR"):
            tag = "err"
        elif upper.startswith("OK"):
            tag = "ok"
        elif upper.startswith("BAL "):
            tag = "bal"
        elif "UID" in upper or upper.startswith("CARD"):
            tag = "card"
        elif upper.startswith("RELAY"):
            tag = "relay"
            if "RELAY_ON" in upper:
                self.charging_active = True
                self._play("start")
            elif "RELAY_OFF" in upper:
                self.charging_active = False
                self._play("stop")
                self.after(2000, self._resume_idle)
        elif upper.startswith("NEW "):
            tag = "card"
        else:
            tag = "normal"

        if "CARD_DETECTED" in upper and not self.charging_active:
            self.charging_active = True
            self._play("start")

        self._log(display, tag)

    def _annotate_name(self, line: str) -> str:
        m = re.search(r"\bid\s*=\s*(\d+)", line, re.IGNORECASE)
        if m:
            card_id = int(m.group(1))
            id_to_name = {v: k for k, v in self.users.items()}
            name = id_to_name.get(card_id)
            if name:
                return f"{line}  \u2192  {name}"
        return line

    # ─────────────────────────────────────────────────────────────────────────
    # Commands
    # ─────────────────────────────────────────────────────────────────────────

    def _send(self, cmd: str) -> None:
        if not self.serial_conn or not self.serial_conn.is_open:
            self._log("Not connected.", "err")
            return
        try:
            self.serial_conn.write((cmd + "\n").encode())
            self._log(f"> {cmd}", "system")
        except serial.SerialException as exc:
            self._log(f"Send error: {exc}", "err")

    def _cmd_topup(self, pts: int) -> None:
        name = self._selected_name()
        if name:
            self._log(f"Topping up {name} +{pts} pts — tap card …", "system")
        self._send(f"TOPUP {pts}")

    def _cmd_select(self, pts: int) -> None:
        self._send(f"SELECT {pts}")

    def _cmd_balance(self) -> None:
        name = self._selected_name()
        if name:
            self._log(f"Balance check for {name} — tap card …", "system")
        self._send("BALANCE")

    def _cmd_cancel(self) -> None:
        self._send("CANCEL")

    def _selected_name(self) -> str | None:
        sel = self.users_tree.selection()
        if sel:
            return str(self.users_tree.item(sel[0])["values"][0])
        return None

    # ─────────────────────────────────────────────────────────────────────────
    # User management
    # ─────────────────────────────────────────────────────────────────────────

    def _register_user(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showwarning("Missing name", "Enter a name first.")
            return
        if name.lower() in {k.lower() for k in self.users}:
            messagebox.showwarning("Duplicate", f'"{name}" is already registered.')
            return
        new_id = generate_unique_id(set(self.users.values()))
        self.users[name] = new_id
        save_users(self.users)
        self._refresh_user_list()
        self.name_var.set("")
        self._log(f'Registered "{name}" \u2192 ID {new_id}  — tap their card now', "ok")
        self._send(f"REGISTER {new_id}")

    def _delete_user(self) -> None:
        sel = self.users_tree.selection()
        if not sel:
            messagebox.showinfo("Nothing selected", "Select a user to delete.")
            return
        name = str(self.users_tree.item(sel[0])["values"][0])
        if messagebox.askyesno("Confirm remove",
                               f'Remove "{name}" from the local registry?\n'
                               f'(Does not erase the NFC card itself.)'):
            self.users.pop(name, None)
            save_users(self.users)
            self._refresh_user_list()
            self._log(f'Removed "{name}" from registry.', "system")

    def _refresh_user_list(self) -> None:
        for r in self.users_tree.get_children():
            self.users_tree.delete(r)
        for name, uid in sorted(self.users.items(), key=lambda x: x[0].lower()):
            self.users_tree.insert("", tk.END, values=(name, uid))

    # ─────────────────────────────────────────────────────────────────────────
    # Log
    # ─────────────────────────────────────────────────────────────────────────

    def _log(self, message: str, tag: str = "normal") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{ts}] ", "ts")
        self.log_text.insert(tk.END, f"{message}\n", tag)
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _clear_log(self) -> None:
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)

    # ─────────────────────────────────────────────────────────────────────────
    # Live plot
    # ─────────────────────────────────────────────────────────────────────────

    def _style_axes(self) -> None:
        self.ax_p.set_facecolor(PLOT_BG)
        self.ax_p.tick_params(colors=MUTED, labelsize=10)
        self.ax_p.spines[:].set_color(BORDER)
        self.ax_p.spines["top"].set_visible(False)
        self.ax_p.spines["right"].set_visible(False)
        self.ax_p.spines["left"].set_color(ACCENT)
        self.ax_p.set_ylabel("Power  (W)", color=ACCENT, fontsize=11)
        self.ax_p.yaxis.label.set_color(ACCENT)
        self.ax_p.tick_params(axis="y", colors=ACCENT, labelsize=10)
        self.ax_p.set_xlabel("seconds ago", color=MUTED, fontsize=10)
        self.ax_p.tick_params(axis="x", colors=MUTED, labelsize=10)

    def _draw_placeholder(self) -> None:
        self.ax_p.text(0.5, 0.5, "Waiting for INA data…",
                       transform=self.ax_p.transAxes,
                       ha="center", va="center",
                       color=DIM, fontsize=12, style="italic")

    def _schedule_plot(self) -> None:
        self._update_plot()
        self.after(500, self._schedule_plot)

    def _update_plot(self) -> None:
        if not self._plot_power:
            return

        times  = list(self._plot_time)
        powers = list(self._plot_power)

        now_t  = times[-1]
        cutoff = now_t - PLOT_WINDOW_S
        pts = [(t, p) for t, p in zip(times, powers) if t >= cutoff]
        if not pts:
            return

        ts, ps = zip(*pts)
        xs = [t - now_t for t in ts]    # 0 = now, negative = past

        self.ax_p.cla()
        self._style_axes()

        self.ax_p.plot(xs, ps, color=ACCENT, linewidth=2.2, antialiased=True)
        self.ax_p.fill_between(xs, ps, alpha=0.14, color=ACCENT)
        self.ax_p.set_xlim(-PLOT_WINDOW_S, 0)
        p_range = max(ps) - min(ps)
        p_margin = max(0.05, p_range * 0.20 + 0.02)
        self.ax_p.set_ylim(max(0.0, min(ps) - p_margin), max(ps) + p_margin)
        self.ax_p.yaxis.set_major_formatter(ticker.FormatStrFormatter("%.2f"))
        self.ax_p.grid(axis="y", color=BORDER, linewidth=0.6, alpha=0.8)
        self.ax_p.grid(axis="x", color=BORDER, linewidth=0.4, alpha=0.4)

        self.canvas.draw_idle()

    # ─────────────────────────────────────────────────────────────────────────
    # Shutdown
    # ─────────────────────────────────────────────────────────────────────────

    def _on_close(self) -> None:
        self._disconnect()
        try:
            pygame.mixer.quit()
        except Exception:
            pass
        self.destroy()


def main() -> None:
    app = WCSApp()
    app.mainloop()


if __name__ == "__main__":
    main()
