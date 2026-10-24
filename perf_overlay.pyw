"""Tiny always-on-top overlay showing CPU / RAM / GPU usage, network
throughput and free disk space.

- The overlay ignores the mouse (clicks pass through to whatever is below).
- Everything is controlled from the tray icon (taskbar, bottom-right):
  edit mode (drag to move), reset position, opacity, run at startup, exit.
  Double-clicking the tray icon toggles edit mode.
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

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000


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


def tray_image():
    """64x64 icon: three bars of rising height."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, 63, 63), radius=12, fill=BG)
    for i, (h, color) in enumerate([(22, "#4cd964"), (34, "#ffb84d"), (46, "#ff5c5c")]):
        x = 10 + i * 16
        d.rectangle((x, 54 - h, x + 11, 54), fill=color)
    return img


class Overlay:
    def __init__(self):
        self.cfg = {"x": None, "y": None, "alpha": 0.85, "drives": ["C:\\", "D:\\"]}
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                self.cfg.update(json.load(f))
        except (OSError, ValueError):
            pass

        self.gpu = Gpu()
        self.net = Net()
        self.drives = [d for d in self.cfg["drives"] if os.path.exists(d)]
        psutil.cpu_percent(None, percpu=True)  # first call always returns 0; prime it
        self.editing = False
        self.calls = queue.Queue()  # tray thread -> Tk thread

        self.root = root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", self.cfg["alpha"])
        root.configure(bg=BG)

        s = root.winfo_fpixels("1i") / 96  # DPI scale factor
        self.s = s
        self.w, self.row_h, self.pad = round(230 * s), round(30 * s), round(10 * s)
        rows = ["CPU", "RAM", "GPU", "NET"] + [d.rstrip("\\") for d in self.drives]
        self.ncpu = psutil.cpu_count() or 1
        core_h, core_gap = round(18 * s), round(4 * s)
        strip_h = round(4 * s) + core_h + core_gap  # per-thread strip under the CPU row
        self.h = self.pad * 2 + self.row_h * len(rows) - round(6 * s) + strip_h

        self.canvas = c = tk.Canvas(root, width=self.w, height=self.h, bg=BG,
                                    highlightthickness=0, bd=0)
        c.pack()
        label_font = ("Segoe UI Semibold", 9)
        detail_font = ("Segoe UI", 8)
        value_font = ("Consolas", 10, "bold")
        self.items = {}
        self.cores = []  # (bar id, x0, x1, top, bottom) per logical processor
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
        bw = max(3, round(3 * s))
        self.edit_border = c.create_rectangle(bw // 2, bw // 2, self.w - bw // 2 - 1,
                                              self.h - bw // 2 - 1, outline=ACCENT,
                                              width=bw, state="hidden")

        self._place_initial()
        root.update_idletasks()
        self.hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        self._round_corners()
        self._apply_click_through()

        for w in (root, c):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", lambda e: self._save())

        self._build_tray()
        self._pump_calls()
        self._tick()

    # ---- placement -------------------------------------------------------

    def _top_right(self):
        l, t, r, b = work_area()
        m = round(12 * self.s)
        return r - self.w - m, t + m

    def _place_initial(self):
        x, y = self.cfg["x"], self.cfg["y"]
        vl, vt, vr, vb = virtual_screen()
        # Fall back to top-right if unset or the saved spot is now off-screen
        # (e.g. a monitor was unplugged).
        if x is None or y is None or not (vl <= x <= vr - 40 and vt <= y <= vb - 20):
            x, y = self._top_right()
        self.root.geometry(f"{self.w}x{self.h}+{x}+{y}")

    def reset_position(self):
        x, y = self._top_right()
        self.root.geometry(f"+{x}+{y}")
        self._save()

    def _round_corners(self):
        try:  # Windows 11: DWMWA_WINDOW_CORNER_PREFERENCE = 33, DWMWCP_ROUND = 2
            pref = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(self.hwnd, 33, ctypes.byref(pref), 4)
        except Exception:
            pass

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

    def toggle_edit(self):
        self.editing = not self.editing
        self.canvas.itemconfigure(self.edit_border,
                                  state="normal" if self.editing else "hidden")
        self._apply_click_through()
        if not self.editing:
            self._save()

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

        def alpha_item(a):
            return pystray.MenuItem(f"{int(a * 100)}%", call(self.set_alpha, a), radio=True,
                                    checked=lambda item: abs(self.cfg["alpha"] - a) < 1e-6)

        menu = pystray.Menu(
            pystray.MenuItem("위치 편집 모드", call(self.toggle_edit), default=True,
                             checked=lambda item: self.editing),
            pystray.MenuItem("우측 상단으로 이동", call(self.reset_position)),
            pystray.MenuItem("투명도", pystray.Menu(*(alpha_item(a) for a in (1.0, 0.85, 0.7, 0.5)))),
            pystray.MenuItem("Windows 시작 시 실행", call(self.toggle_startup),
                             checked=lambda item: self._startup_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("종료", call(self.quit)),
        )
        self.tray = pystray.Icon("PerfOverlay", tray_image(), "Perf Overlay", menu)
        self.tray.run_detached()

    def _pump_calls(self):
        while not self.calls.empty():
            fn, args = self.calls.get_nowait()
            fn(*args)
        self.root.after(100, self._pump_calls)

    def set_alpha(self, a):
        self.cfg["alpha"] = a
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

    def toggle_startup(self):
        enable = not self._startup_enabled()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, self._startup_command())
            else:
                winreg.DeleteValue(k, RUN_NAME)
        self.tray.update_menu()

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

    def _tick(self):
        per = psutil.cpu_percent(None, percpu=True)
        cpu = sum(per) / len(per)
        self._set_row("CPU", cpu, f"{len(per)} threads")
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
            self._set_row(name, u.percent, f"{u.percent:.0f}% of {fmt_size(u.total)}",
                          f"{fmt_size(u.free)} free")

        self.tray.title = f"CPU {cpu:.0f}% · RAM {vm.percent:.0f}% · GPU {gpu_txt}"
        self.root.attributes("-topmost", True)  # re-assert in case another window took it
        self.root.after(INTERVAL_MS, self._tick)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    try:  # per-monitor DPI awareness for crisp text and correct coordinates
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    Overlay().run()
