"""Tiny always-on-top overlay showing CPU / RAM / GPU usage, network
throughput and free disk space.

- The overlay ignores the mouse (clicks pass through to whatever is below).
- Clicking the tray icon (taskbar, bottom-right) opens a settings popup:
  show/hide the overlay for a while, game mode (RAM / GPU / NET only), edit mode (drag to move), reset position,
  opacity, run at startup, language (Korean / English), exit. Right-click the
  icon for quick toggles. In edit mode, double-click the overlay (or press
  Done in the popup) to finish.
- Settings live in perf_overlay.json next to this script ("drives" picks
  which drives to show).

Requires: psutil, pystray, pillow. GPU stats use nvml.dll, which ships with
the NVIDIA driver.
"""
import ctypes
import json
import os
import queue
import sys
import time
import tkinter as tk
import winreg

import psutil
import pystray
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "perf_overlay.json")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "PerfOverlay"
INTERVAL_MS = 1000

BG = "#111418"
FG = "#e8e8e8"
DIM = "#8a9099"
TRACK = "#262b32"
ACCENT = "#4da3ff"
ACCENT_HOVER = "#6ab4ff"
PANEL = "#1b1f25"
BTN = "#2a3038"
BTN_HOVER = "#353c46"

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOPMOST = 0x00000008
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10

STRINGS = {
    "ko": {
        "show": "오버레이 표시",
        "show_sub": "끄면 다시 켤 때까지 숨김",
        "game": "게임 모드",
        "game_sub": "RAM · GPU · 네트워크만 한 줄로 표시",
        "position": "위치",
        "move": "위치 편집",
        "done": "완료",
        "top_right": "우측 상단으로",
        "move_hint": "오버레이를 드래그해서 옮기세요. 더블클릭해도 완료됩니다.",
        "opacity": "투명도",
        "startup": "Windows 시작 시 실행",
        "language": "언어",
        "quit": "종료",
        "settings": "설정",
        "move_mode": "위치 편집 모드",
        "hidden_tip": "Perf Overlay (숨김)",
        "game_tip": "[게임 모드]",
        "threads": "{n} 스레드",
        "disk_used": "{pct}% 사용 / {total}",
        "disk_free": "{free} 여유",
    },
    "en": {
        "show": "Show overlay",
        "show_sub": "Hidden until you turn it back on",
        "game": "Game mode",
        "game_sub": "Just RAM · GPU · network on one line",
        "position": "Position",
        "move": "Move",
        "done": "Done",
        "top_right": "Snap to top-right",
        "move_hint": "Drag the overlay to move it. Double-click it to finish.",
        "opacity": "Opacity",
        "startup": "Start with Windows",
        "language": "Language",
        "quit": "Quit",
        "settings": "Settings",
        "move_mode": "Move mode",
        "hidden_tip": "Perf Overlay (hidden)",
        "game_tip": "[Game mode]",
        "threads": "{n} threads",
        "disk_used": "{pct}% of {total}",
        "disk_free": "{free} free",
    },
}
LANGUAGES = [("ko", "한국어"), ("en", "English")]


def default_language():
    """Korean if Windows' UI language is Korean (primary language id 0x12), else English."""
    try:
        return "ko" if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x12 else "en"
    except Exception:
        return "en"


def load_color(pct):
    if pct >= 85:
        return "#ff5c5c"
    if pct >= 60:
        return "#ffb84d"
    return "#4cd964"


class Gpu:
    """NVIDIA GPU utilization and VRAM via NVML."""

    class _Util(ctypes.Structure):
        _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

    class _Mem(ctypes.Structure):
        _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong),
                    ("used", ctypes.c_ulonglong)]

    def __init__(self):
        self.handle = None
        try:
            self.nvml = ctypes.WinDLL("nvml.dll")
            if self.nvml.nvmlInit_v2() == 0:
                h = ctypes.c_void_p()
                if self.nvml.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(h)) == 0:
                    self.handle = h
        except OSError:
            pass

    def read(self):
        """Returns (util %, used GB, total GB), or None if unavailable."""
        if self.handle is None:
            return None
        util, mem = self._Util(), self._Mem()
        if self.nvml.nvmlDeviceGetUtilizationRates(self.handle, ctypes.byref(util)) != 0:
            return None
        self.nvml.nvmlDeviceGetMemoryInfo(self.handle, ctypes.byref(mem))
        return util.gpu, mem.used / 2**30, mem.total / 2**30


class Net:
    """Throughput of the busiest active adapter (e.g. 이더넷), loopback excluded."""

    def __init__(self):
        self.nic = None
        self.speed = 0  # link speed, Mbps
        self.last = None

    def _pick(self):
        stats = psutil.net_if_stats()
        io = psutil.net_io_counters(pernic=True)
        live = [n for n, s in stats.items()
                if s.isup and s.speed > 0 and n in io and "loopback" not in n.lower()]
        nic = max(live, key=lambda n: io[n].bytes_recv + io[n].bytes_sent, default=None)
        self.nic, self.speed, self.last = nic, stats[nic].speed if nic else 0, None

    def read(self):
        """Returns (down B/s, up B/s, link Mbps), or None if no adapter is up."""
        io = psutil.net_io_counters(pernic=True)
        if self.nic not in io:
            self._pick()
            if self.nic is None:
                return None
        c, now = io[self.nic], time.monotonic()
        prev, self.last = self.last, (c.bytes_recv, c.bytes_sent, now)
        if prev is None:
            return 0.0, 0.0, self.speed
        dt = max(now - prev[2], 1e-6)
        return (c.bytes_recv - prev[0]) / dt, (c.bytes_sent - prev[1]) / dt, self.speed


def fmt_rate(bps):
    for unit in ("B/s", "KB/s", "MB/s"):
        if bps < 1024:
            return f"{bps:.0f} {unit}" if unit == "B/s" else f"{bps:.1f} {unit}"
        bps /= 1024
    return f"{bps:.1f} GB/s"


def fmt_rate_short(bps):
    """At most 4 characters: 512K, 2.1M, 85M."""
    k = bps / 1024
    if k < 999.5:
        return f"{k:.0f}K"
    m = k / 1024
    return f"{m:.1f}M" if m < 9.95 else f"{m:.0f}M"


def fmt_size(nbytes):
    gb = nbytes / 2**30
    return f"{gb / 1024:.1f} TB" if gb >= 1000 else f"{gb:.0f} GB"


def work_area():
    """Primary monitor's work area (excludes the taskbar) as (left, top, right, bottom)."""
    class RECT(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                    ("r", ctypes.c_long), ("b", ctypes.c_long)]
    rc = RECT()
    ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rc), 0)  # SPI_GETWORKAREA
    return rc.l, rc.t, rc.r, rc.b


def virtual_screen():
    """Bounding box of all monitors as (left, top, right, bottom)."""
    gsm = ctypes.windll.user32.GetSystemMetrics
    x, y = gsm(76), gsm(77)
    return x, y, x + gsm(78), y + gsm(79)


def tray_image(game_mode=False, hidden=False):
    """64x64 icon: three bars of rising height, blue in game mode, grey when hidden."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, 63, 63), radius=12, fill=BG)
    if hidden:
        colors = [DIM] * 3
    elif game_mode:
        colors = [ACCENT] * 3
    else:
        colors = ["#4cd964", "#ffb84d", "#ff5c5c"]
    for i, (h, color) in enumerate(zip((22, 34, 46), colors)):
        x = 10 + i * 16
        d.rectangle((x, 54 - h, x + 11, 54), fill=color)
    return img


def round_corners(hwnd):
    try:  # Windows 11: DWMWA_WINDOW_CORNER_PREFERENCE = 33, DWMWCP_ROUND = 2
        pref = ctypes.c_int(2)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), 4)
    except Exception:
        pass


class Slider:
    """Flat horizontal slider drawn on a canvas; snaps to `step`."""

    def __init__(self, parent, s, lo, hi, step, value, on_change):
        self.lo, self.hi, self.step, self.value, self.on_change = lo, hi, step, value, on_change
        self.h, self.r = round(20 * s), round(7 * s)
        self.c = tk.Canvas(parent, height=self.h, bg=PANEL, highlightthickness=0, bd=0,
                           cursor="hand2")
        self.c.bind("<Configure>", lambda e: self._draw())
        self.c.bind("<Button-1>", self._drag)
        self.c.bind("<B1-Motion>", self._drag)

    def _draw(self):
        c, r, y = self.c, self.r, self.h / 2
        w = c.winfo_width()
        x = r + (w - 2 * r) * (self.value - self.lo) / (self.hi - self.lo)
        c.delete("all")
        c.create_line(r, y, w - r, y, fill=TRACK, width=r * 0.6, capstyle="round")
        c.create_line(r, y, x, y, fill=ACCENT, width=r * 0.6, capstyle="round")
        c.create_oval(x - r, y - r, x + r, y + r, fill=FG, outline="")

    def _drag(self, e):
        w, r = self.c.winfo_width(), self.r
        f = min(1.0, max(0.0, (e.x - r) / max(1, w - 2 * r)))
        v = round((self.lo + f * (self.hi - self.lo)) / self.step) * self.step
        if v != self.value:
            self.value = v
            self._draw()
            self.on_change(v)


class Toggle:
    """Windows-style on/off switch drawn on a canvas."""

    def __init__(self, parent, s, value, on_change):
        self.value, self.on_change = value, on_change
        self.w, self.h = round(40 * s), round(20 * s)
        self.c = tk.Canvas(parent, width=self.w, height=self.h, bg=PANEL,
                           highlightthickness=0, bd=0, cursor="hand2")
        self.c.bind("<Button-1>", self._click)
        self._draw()

    def _draw(self):
        c, w, h = self.c, self.w, self.h
        r = h / 2
        col = ACCENT if self.value else TRACK
        c.delete("all")
        c.create_oval(0, 0, h, h, fill=col, outline="")
        c.create_oval(w - h, 0, w, h, fill=col, outline="")
        c.create_rectangle(r, 0, w - r, h, fill=col, outline="")
        kx, kr = (w - r if self.value else r), r - 4
        c.create_oval(kx - kr, r - kr, kx + kr, r + kr, fill=FG if self.value else DIM,
                      outline="")

    def _click(self, e):
        self.value = not self.value
        self._draw()
        self.on_change(self.value)

    def set(self, value):
        if value != self.value:
            self.value = value
            self._draw()


class SettingsPopup:
    """Flyout-style settings window shown above the tray, like Windows' own flyouts.

    Closes on Esc, the ✕ button, or when it loses focus (except while in edit
    mode, so it can stay open as the Done button while the overlay is dragged).
    """

    def __init__(self, overlay):
        self.o = o = overlay
        s, t = o.s, o.t
        self.win = win = tk.Toplevel(o.root)
        win.withdraw()  # stay hidden until placed
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=PANEL, highlightthickness=1, highlightbackground="#323843")

        pad = round(16 * s)
        body = tk.Frame(win, bg=PANEL, padx=pad, pady=round(12 * s))
        body.pack(fill="both", expand=True)

        head = tk.Frame(body, bg=PANEL)
        head.pack(fill="x")
        tk.Label(head, text="Perf Overlay", bg=PANEL, fg=FG,
                 font=("Segoe UI Semibold", 11)).pack(side="left")
        close = tk.Label(head, text="✕", bg=PANEL, fg=DIM, font=("Segoe UI", 10),
                         cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self.close())
        close.bind("<Enter>", lambda e: close.configure(fg=FG))
        close.bind("<Leave>", lambda e: close.configure(fg=DIM))

        self.show_toggle = self._toggle_row(body, t("show"), t("show_sub"),
                                            o.visible, o.set_visible)
        self.game_toggle = self._toggle_row(body, t("game"), t("game_sub"),
                                            o.cfg["game_mode"], o.set_game_mode)

        self._section(body, t("position"))
        self.pos_row = row = tk.Frame(body, bg=PANEL)
        row.pack(fill="x")
        row.columnconfigure((0, 1), weight=1, uniform="b")
        self.edit_btn = self._button(row, "", o.toggle_edit)
        self.edit_btn.grid(row=0, column=0, sticky="ew", padx=(0, round(4 * s)))
        self._button(row, t("top_right"), o.reset_position).grid(
            row=0, column=1, sticky="ew", padx=(round(4 * s), 0))
        self.hint = tk.Label(body, text=t("move_hint"), bg=PANEL, fg=DIM, font=("Segoe UI", 8),
                             anchor="w", justify="left", wraplength=round(255 * s))

        alpha_head = self._section(body, t("opacity"))
        self.alpha_lbl = tk.Label(alpha_head, text="", bg=PANEL, fg=FG, font=("Consolas", 9))
        self.alpha_lbl.pack(side="right")
        self.slider = Slider(body, s, 30, 100, 5, round(o.cfg["alpha"] * 100), self._alpha)
        self.slider.c.pack(fill="x")
        self._alpha_text()

        startup = tk.Frame(body, bg=PANEL)
        startup.pack(fill="x", pady=(round(14 * s), 0))
        tk.Label(startup, text=t("startup"), bg=PANEL, fg=FG,
                 font=("Segoe UI", 9)).pack(side="left")
        Toggle(startup, s, o._startup_enabled(), o.set_startup).c.pack(side="right")

        lang = tk.Frame(body, bg=PANEL)
        lang.pack(fill="x", pady=(round(12 * s), 0))
        tk.Label(lang, text=t("language"), bg=PANEL, fg=FG,
                 font=("Segoe UI", 9)).pack(side="left")
        seg = tk.Frame(lang, bg=PANEL)
        seg.pack(side="right")
        for code, name in LANGUAGES:
            b = self._button(seg, name, lambda c=code: o.set_language(c), padx=round(8 * s))
            if code == o.cfg["lang"]:
                b.base = ACCENT
                b.configure(bg=ACCENT, fg="#0b0d10")
            b.pack(side="left", padx=(round(4 * s), 0))

        tk.Frame(body, bg="#2a2f36", height=1).pack(fill="x", pady=round(12 * s))
        foot = tk.Frame(body, bg=PANEL)
        foot.pack(fill="x")
        self._button(foot, t("quit"), o.quit, padx=round(18 * s)).pack(side="right")

        self.refresh()
        round_corners(ctypes.windll.user32.GetParent(win.winfo_id()))

        win.bind("<Escape>", lambda e: self.close())
        win.bind("<FocusOut>", lambda e: win.after(80, self._close_if_unfocused))
        win.deiconify()
        win.lift()
        win.focus_force()

    def _toggle_row(self, parent, title, subtitle, value, on_change):
        s = self.o.s
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x", pady=(round(14 * s), 0))
        text = tk.Frame(row, bg=PANEL)
        text.pack(side="left")
        tk.Label(text, text=title, bg=PANEL, fg=FG,
                 font=("Segoe UI Semibold", 10)).pack(anchor="w")
        tk.Label(text, text=subtitle, bg=PANEL, fg=DIM, font=("Segoe UI", 8)).pack(anchor="w")
        toggle = Toggle(row, s, value, on_change)
        toggle.c.pack(side="right")
        return toggle

    def _section(self, parent, text):
        f = tk.Frame(parent, bg=PANEL)
        f.pack(fill="x", pady=(round(14 * self.o.s), round(6 * self.o.s)))
        tk.Label(f, text=text, bg=PANEL, fg=DIM, font=("Segoe UI", 8)).pack(side="left")
        return f

    def _button(self, parent, text, command, padx=None):
        s = self.o.s
        b = tk.Label(parent, text=text, bg=BTN, fg=FG, font=("Segoe UI", 9), cursor="hand2",
                     padx=padx or round(10 * s), pady=round(5 * s))
        b.base = BTN
        b.bind("<Button-1>", lambda e: command())
        b.bind("<Enter>", lambda e: b.configure(bg=ACCENT_HOVER if b.base == ACCENT else BTN_HOVER))
        b.bind("<Leave>", lambda e: b.configure(bg=b.base))
        return b

    def refresh(self):
        """Sync the controls with state that may also change from the tray menu."""
        self.show_toggle.set(self.o.visible)
        self.game_toggle.set(self.o.cfg["game_mode"])
        b = self.edit_btn
        b.base = ACCENT if self.o.editing else BTN
        b.configure(text=self.o.t("done" if self.o.editing else "move"), bg=b.base,
                    fg="#0b0d10" if self.o.editing else FG)
        # Only take up space while moving.
        if self.o.editing:
            self.hint.pack(fill="x", pady=(round(4 * self.o.s), 0), after=self.pos_row)
        else:
            self.hint.pack_forget()
        self._place()

    def _place(self):
        """Anchor the bottom-right corner just above the taskbar; height follows content."""
        s, win = self.o.s, self.win
        win.update_idletasks()
        w, h = round(290 * s), win.winfo_reqheight()
        l, t, r, b = work_area()
        m = round(12 * s)
        win.geometry(f"{w}x{h}+{r - w - m}+{b - h - m}")

    def _alpha(self, pct):
        self.o.set_alpha(pct / 100)
        self._alpha_text()

    def _alpha_text(self):
        self.alpha_lbl.configure(text=f"{self.slider.value}%")

    def _close_if_unfocused(self):
        if self.o.editing or not self.win.winfo_exists():
            return
        try:
            focused = self.win.focus_get()
        except (KeyError, tk.TclError):
            focused = None
        if focused is None or focused.winfo_toplevel() is not self.win:
            self.close()

    def close(self):
        if self.win.winfo_exists():
            self.win.destroy()
        self.o.popup = None
        self.o.popup_closed_at = time.monotonic()


class Overlay:
    def __init__(self):
        self.cfg = {"x": None, "y": None, "alpha": 0.85, "drives": ["C:\\", "D:\\"],
                    "game_mode": False, "lang": default_language()}
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                self.cfg.update(json.load(f))
        except (OSError, ValueError):
            pass
        if self.cfg["lang"] not in STRINGS:
            self.cfg["lang"] = "en"

        self.gpu = Gpu()
        self.net = Net()
        self.drives = [d for d in self.cfg["drives"] if os.path.exists(d)]
        psutil.cpu_percent(None, percpu=True)  # first call always returns 0; prime it
        self.editing = False
        self.visible = True  # deliberately not saved: hiding is only "for a while"
        self.popup = None
        self.popup_closed_at = 0.0
        self.calls = queue.Queue()  # tray thread -> Tk thread

        self.root = root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", self.cfg["alpha"])
        root.configure(bg=BG)

        s = root.winfo_fpixels("1i") / 96  # DPI scale factor
        self.s = s
        self.full_w, self.row_h, self.pad = round(230 * s), round(30 * s), round(10 * s)
        self.ncpu = psutil.cpu_count() or 1
        self.canvas = tk.Canvas(root, bg=BG, highlightthickness=0, bd=0)
        self.canvas.pack()
        self._build_rows()

        self._place_initial()
        root.update_idletasks()
        self.hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        round_corners(self.hwnd)
        self._apply_click_through()

        # Bound on the toplevel only: its bindtag also covers the canvas.
        root.bind("<ButtonPress-1>", self._drag_start)
        root.bind("<B1-Motion>", self._drag_move)
        root.bind("<ButtonRelease-1>", lambda e: self._save())
        root.bind("<Double-Button-1>", lambda e: self.editing and self.toggle_edit())

        self._build_tray()
        self._pump_calls()
        self._tick()

    # ---- language --------------------------------------------------------

    def t(self, key, **fmt):
        return STRINGS[self.cfg["lang"]][key].format(**fmt)

    def set_language(self, lang):
        if lang == self.cfg["lang"]:
            return
        self.cfg["lang"] = lang
        self._save()
        if self.visible:
            self._update()
        self.tray.update_menu()
        if self.popup:  # rebuild it in the new language
            self.popup.close()
            self.popup = SettingsPopup(self)

    # ---- layout ----------------------------------------------------------

    def _build_rows(self):
        """(Re)draw the layout for the current mode and resize the window to fit."""
        c = self.canvas
        c.delete("all")
        self.items, self.cores, self.compact = {}, [], {}
        if self.cfg["game_mode"]:
            self._build_compact()
        else:
            self._build_full()
        c.configure(width=self.w, height=self.h)
        bw = max(3, round(3 * self.s))
        self.edit_border = c.create_rectangle(bw // 2, bw // 2, self.w - bw // 2 - 1,
                                              self.h - bw // 2 - 1, outline=ACCENT, width=bw,
                                              state="normal" if self.editing else "hidden")
        self.root.geometry(f"{self.w}x{self.h}")  # keeps the current position

    def _build_compact(self):
        """Game mode: one line of numbers, e.g.  RAM 65%  GPU 99%  ↓2.1M ↑5K"""
        s, c = self.s, self.canvas
        self.h = round(22 * s)
        ty = self.h / 2
        label_font = ("Segoe UI Semibold", 7)
        value_font = ("Consolas", 9, "bold")
        x = round(8 * s)

        def label(text):
            nonlocal x
            i = c.create_text(x, ty, text=text, fill=DIM, font=label_font, anchor="w")
            x = c.bbox(i)[2] + round(2 * s)

        def value(key, widest, gap, align="e"):
            # The slot is sized for the widest text, so nothing shifts as values change.
            nonlocal x
            i = c.create_text(x, ty, text=widest, font=value_font, anchor="w")
            right = c.bbox(i)[2]
            if align == "e":
                c.coords(i, right, ty)
            c.itemconfigure(i, text="--", fill=FG, anchor=align)
            self.compact[key] = i
            x = right + round(gap * s)

        label("RAM")
        value("RAM", "100%", 8)
        label("GPU")
        value("GPU", "100%", 8)
        label("↓")
        value("down", "999M", 4, align="w")
        label("↑")
        value("up", "999M", 0, align="w")
        self.w = x + round(8 * s)

    def _build_full(self):
        s, c = self.s, self.canvas
        self.w = self.full_w
        rows = ["CPU", "RAM", "GPU", "NET"] + [d.rstrip("\\") for d in self.drives]
        core_h, core_gap = round(18 * s), round(4 * s)
        strip_h = round(4 * s) + core_h + core_gap  # per-thread strip under the CPU row
        self.h = self.pad * 2 + self.row_h * len(rows) - round(6 * s) + strip_h

        label_font = ("Segoe UI Semibold", 9)
        detail_font = ("Segoe UI", 8)
        value_font = ("Consolas", 10, "bold")
        offset = 0
        for i, name in enumerate(rows):
            y = self.pad + i * self.row_h + offset
            ty = y + round(8 * s)
            by0, by1 = y + round(18 * s), y + round(22 * s)
            x0, x1 = self.pad, self.w - self.pad
            c.create_text(x0, ty, text=name, fill=FG, font=label_font, anchor="w")
            detail = c.create_text(x1 - round(48 * s), ty, text="", fill=DIM,
                                   font=detail_font, anchor="e")
            value = c.create_text(x1, ty, text="--", fill=FG, font=value_font, anchor="e")
            c.create_rectangle(x0, by0, x1, by1, fill=TRACK, width=0)
            bar = c.create_rectangle(x0, by0, x0, by1, fill=load_color(0), width=0)
            self.items[name] = (detail, value, bar, x0, x1, by0, by1, ty)
            if name == "CPU":
                top = by1 + round(4 * s)
                bottom = top + core_h
                gap = max(1, round(2 * s))
                cw = (x1 - x0 - gap * (self.ncpu - 1)) / self.ncpu
                for k in range(self.ncpu):
                    cx0 = x0 + k * (cw + gap)
                    c.create_rectangle(cx0, top, cx0 + cw, bottom, fill=TRACK, width=0)
                    cb = c.create_rectangle(cx0, bottom, cx0 + cw, bottom,
                                            fill=load_color(0), width=0)
                    self.cores.append((cb, cx0, cx0 + cw, top, bottom))
                offset = strip_h

    def set_game_mode(self, on):
        """Game mode shrinks the overlay to one line: RAM / GPU / NET."""
        right, y = self.root.winfo_x() + self.w, self.root.winfo_y()
        self.cfg["game_mode"] = on
        self._build_rows()
        # Keep the right edge fixed so a top-right overlay stays in the corner.
        self.root.geometry(f"+{right - self.w}+{y}")
        self.root.update_idletasks()  # apply the move so _save() reads the new position
        self._update()
        self._save()
        self.tray.icon = tray_image(on, not self.visible)
        self.tray.update_menu()
        if self.popup:
            self.popup.refresh()

    # ---- placement -------------------------------------------------------

    def _top_right(self):
        l, t, r, b = work_area()
        m = round(12 * self.s)
        return r - self.w - m, t + m

    def _place_initial(self):
        x, y = self.cfg["x"], self.cfg["y"]
        vl, vt, vr, vb = virtual_screen()
        if x is None or y is None:
            x, y = self._top_right()
        # Keep the whole overlay on screen (e.g. after a monitor was unplugged).
        x = min(max(x, vl), vr - self.w)
        y = min(max(y, vt), vb - self.h)
        self.root.geometry(f"{self.w}x{self.h}+{x}+{y}")

    def reset_position(self):
        x, y = self._top_right()
        self.root.geometry(f"+{x}+{y}")
        self.root.update_idletasks()
        self._save()

    # ---- click-through / edit mode ---------------------------------------

    def _apply_click_through(self):
        """Outside edit mode the window is transparent to the mouse and never takes focus."""
        u = ctypes.windll.user32
        style = u.GetWindowLongW(self.hwnd, GWL_EXSTYLE) | WS_EX_LAYERED | WS_EX_TOOLWINDOW
        if self.editing:
            style &= ~(WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
        else:
            style |= WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
        u.SetWindowLongW(self.hwnd, GWL_EXSTYLE, style)

    def set_visible(self, on):
        """Hide by fading to fully transparent: the window stays click-through and
        never has to be re-shown (which could steal focus from the popup)."""
        if not on and self.editing:
            self.toggle_edit()
        self.visible = on
        self.root.attributes("-alpha", self.cfg["alpha"] if on else 0.0)
        self._apply_click_through()
        if on:
            self._update()
        self.tray.icon = tray_image(self.cfg["game_mode"], not on)
        self.tray.update_menu()
        if self.popup:
            self.popup.refresh()

    def toggle_edit(self):
        if not self.visible and not self.editing:
            return
        self.editing = not self.editing
        self.canvas.itemconfigure(self.edit_border,
                                  state="normal" if self.editing else "hidden")
        self._apply_click_through()
        if not self.editing:
            self._save()
        if self.popup:
            self.popup.refresh()
        self.tray.update_menu()

    def _drag_start(self, e):
        self._dx = e.x_root - self.root.winfo_x()
        self._dy = e.y_root - self.root.winfo_y()

    def _drag_move(self, e):
        if self.editing:
            self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    # ---- tray ------------------------------------------------------------

    def _build_tray(self):
        def call(fn, *args):
            # pystray callbacks run on its own thread; hand work to the Tk thread.
            return lambda icon, item: self.calls.put((fn, args))

        def text(key):
            return lambda item: self.t(key)  # re-read on update_menu()

        # The default item is what a left click on the tray icon triggers.
        menu = pystray.Menu(
            pystray.MenuItem(text("settings"), call(self.toggle_settings), default=True),
            pystray.MenuItem(text("show"), lambda icon, item: self.calls.put(
                (self.set_visible, (not self.visible,))),
                checked=lambda item: self.visible),
            pystray.MenuItem(text("game"), lambda icon, item: self.calls.put(
                (self.set_game_mode, (not self.cfg["game_mode"],))),
                checked=lambda item: self.cfg["game_mode"]),
            pystray.MenuItem(text("move_mode"), call(self.toggle_edit),
                             checked=lambda item: self.editing),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(text("quit"), call(self.quit)),
        )
        self.tray = pystray.Icon("PerfOverlay", tray_image(self.cfg["game_mode"]),
                                 "Perf Overlay", menu)
        self.tray.run_detached()

    def toggle_settings(self):
        if self.popup:
            self.popup.close()
        # Clicking the tray icon steals focus first, which already closed the
        # popup; don't reopen it from that same click.
        elif time.monotonic() - self.popup_closed_at > 0.4:
            self.popup = SettingsPopup(self)

    def _pump_calls(self):
        while not self.calls.empty():
            fn, args = self.calls.get_nowait()
            fn(*args)
        self.root.after(100, self._pump_calls)

    def set_alpha(self, a):
        self.cfg["alpha"] = a
        if self.visible:
            self.root.attributes("-alpha", a)
            self._apply_click_through()
        self._save()

    def _startup_command(self):
        exe = sys.executable
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pyw):
            exe = pyw
        return f'"{exe}" "{os.path.abspath(__file__)}"'

    def _startup_enabled(self):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.QueryValueEx(k, RUN_NAME)
            return True
        except OSError:
            return False

    def set_startup(self, enable):
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, self._startup_command())
            elif self._startup_enabled():
                winreg.DeleteValue(k, RUN_NAME)

    def quit(self):
        self.tray.stop()
        self.root.destroy()

    def _save(self):
        self.cfg["x"], self.cfg["y"] = self.root.winfo_x(), self.root.winfo_y()
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=2)
        except OSError:
            pass

    # ---- update loop -----------------------------------------------------

    def _set_row(self, name, pct, detail="", value=None):
        """pct drives the bar; value replaces the default "NN%" text on the right."""
        d, v, bar, x0, x1, by0, by1, ty = self.items[name]
        c = self.canvas
        if pct is None:
            c.itemconfigure(v, text="N/A", fill=DIM)
            c.coords(bar, x0, by0, x0, by1)
        else:
            pct = max(0.0, min(100.0, pct))
            c.itemconfigure(v, text=value or f"{pct:3.0f}%", fill=FG)
            c.coords(bar, x0, by0, x0 + (x1 - x0) * pct / 100, by1)
            c.itemconfigure(bar, fill=load_color(pct))
        c.itemconfigure(d, text=detail)
        # Keep the detail text just left of the value, whose width varies.
        c.coords(d, c.bbox(v)[0] - round(8 * self.s), ty)

    def _update_compact(self):
        c, k = self.canvas, self.compact
        # Plain white while fine; only warn colors (>=60%) stand out.
        tint = lambda pct: FG if pct < 60 else load_color(pct)

        vm = psutil.virtual_memory()
        c.itemconfigure(k["RAM"], text=f"{vm.percent:.0f}%", fill=tint(vm.percent))

        g = self.gpu.read()
        if g is None:
            c.itemconfigure(k["GPU"], text="N/A", fill=DIM)
            gpu_txt = "N/A"
        else:
            c.itemconfigure(k["GPU"], text=f"{g[0]}%", fill=tint(g[0]))
            gpu_txt = f"{g[0]}%"

        down, up, _ = self.net.read() or (0.0, 0.0, 0)
        c.itemconfigure(k["down"], text=fmt_rate_short(down))
        c.itemconfigure(k["up"], text=fmt_rate_short(up))

        self.tray.title = (f"{self.t('game_tip')} RAM {vm.percent:.0f}% · GPU {gpu_txt} · "
                           f"↓ {fmt_rate(down)} ↑ {fmt_rate(up)}")

    def _tick(self):
        if self.visible:
            self._update()
            self._keep_topmost()
        else:
            self.tray.title = self.t("hidden_tip")  # nothing measured while hidden
        self.root.after(INTERVAL_MS, self._tick)

    def _keep_topmost(self):
        """Re-pin only if something actually dropped us out of the topmost band.

        Re-raising on every tick would cover topmost windows opened later, such
        as the Win+Shift+S capture screen, making screenshots look broken.
        """
        u = ctypes.windll.user32
        if not u.GetWindowLongW(self.hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST:
            u.SetWindowPos(self.hwnd, ctypes.c_void_p(HWND_TOPMOST), 0, 0, 0, 0,
                           SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def _update(self):
        """Refresh what's shown; game mode doesn't measure CPU or disks at all."""
        if self.cfg["game_mode"]:
            self._update_compact()
            return
        per = psutil.cpu_percent(None, percpu=True)
        cpu = sum(per) / len(per)
        self._set_row("CPU", cpu, self.t("threads", n=len(per)))
        for (cb, cx0, cx1, top, bottom), pct in zip(self.cores, per):
            self.canvas.coords(cb, cx0, bottom - (bottom - top) * pct / 100, cx1, bottom)
            self.canvas.itemconfigure(cb, fill=load_color(pct))

        vm = psutil.virtual_memory()
        self._set_row("RAM", vm.percent,
                      f"{vm.used / 2**30:.1f} / {vm.total / 2**30:.1f} GB")

        g = self.gpu.read()
        if g is None:
            self._set_row("GPU", None)
            gpu_txt = "N/A"
        else:
            util, used, total = g
            self._set_row("GPU", util, f"VRAM {used:.1f} / {total:.0f} GB")
            gpu_txt = f"{util}%"

        net = self.net.read()
        if net is None:
            self._set_row("NET", None)
        else:
            down, up, mbps = net
            pct = max(down, up) * 8 / (mbps * 1e6) * 100 if mbps else 0
            self._set_row("NET", pct, f"↑ {fmt_rate(up)}", f"↓ {fmt_rate(down)}")

        for drive in self.drives:
            name = drive.rstrip("\\")
            try:
                u = psutil.disk_usage(drive)
            except OSError:
                self._set_row(name, None)
                continue
            self._set_row(name, u.percent,
                          self.t("disk_used", pct=f"{u.percent:.0f}", total=fmt_size(u.total)),
                          self.t("disk_free", free=fmt_size(u.free)))

        self.tray.title = f"CPU {cpu:.0f}% · RAM {vm.percent:.0f}% · GPU {gpu_txt}"

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    try:  # per-monitor DPI awareness for crisp text and correct coordinates
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    Overlay().run()
