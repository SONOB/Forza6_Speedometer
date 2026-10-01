#!/usr/bin/env python3
"""FH6 second-monitor speedometer. Python 3.10+, standard library only.

Packet source: https://support.forza.net/hc/en-us/articles/51744149102611
FH6: 324-byte little-endian packet; Speed is an F32 at byte 256, in m/s.
"""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass
import math
from pathlib import Path
import socket
import struct
import sys
import threading
import time


DEFAULT_PORT = 5301
PACKET_SIZE = 324
STALE_SECONDS = 1.5


@dataclass(frozen=True)
class Telemetry:
    driving: bool
    speed_kmh: float
    rpm: float
    max_rpm: float


def parse_packet(packet: bytes) -> Telemetry:
    """Reject unexpected layouts instead of accidentally displaying wrong speeds."""
    if len(packet) != PACKET_SIZE:
        raise ValueError(f"패킷 크기 {len(packet)} bytes / FH6 예상 크기 {PACKET_SIZE} bytes")
    race_on = struct.unpack_from("<i", packet, 0)[0]
    if race_on not in (0, 1):
        raise ValueError("FH6 데이터 형식이 아닙니다 (IsRaceOn)")
    if not race_on:
        return Telemetry(False, 0.0, 0.0, 0.0)
    max_rpm, _idle_rpm, rpm = struct.unpack_from("<fff", packet, 8)
    speed_mps = struct.unpack_from("<f", packet, 256)[0]
    if not all(math.isfinite(v) for v in (speed_mps, rpm, max_rpm)):
        raise ValueError("속도 또는 RPM에 유효하지 않은 값이 있습니다")
    if abs(speed_mps) > 1000 or not (0 <= rpm <= 100_000 and 0 <= max_rpm <= 100_000):
        raise ValueError("속도 또는 RPM 범위를 확인하세요")
    return Telemetry(True, abs(speed_mps) * 3.6, rpm, max_rpm)


@dataclass(frozen=True)
class Snapshot:
    status: str
    sample: Telemetry | None = None
    detail: str = ""


class TelemetryState:
    """A single latest reading: a slow display never accumulates old frames."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sample = None
        self._last_valid = None
        self._last_invalid = None
        self._error = ""
        self._fatal = ""

    def accept(self, packet: bytes, now: float | None = None):
        now = time.monotonic() if now is None else now
        try:
            sample = parse_packet(packet)
        except ValueError as exc:
            with self._lock:
                self._last_invalid = now
                self._error = str(exc)
            return
        with self._lock:
            self._sample = sample
            self._last_valid = now

    def fail(self, detail: str):
        with self._lock:
            self._fatal = detail

    def snapshot(self, now: float | None = None) -> Snapshot:
        now = time.monotonic() if now is None else now
        with self._lock:
            if self._fatal:
                return Snapshot("error", detail=self._fatal)
            if self._last_valid is not None and now - self._last_valid < STALE_SECONDS:
                return Snapshot("live" if self._sample.driving else "paused", self._sample)
            if (self._last_invalid is not None
                    and (self._last_valid is None or self._last_invalid > self._last_valid)
                    and now - self._last_invalid < STALE_SECONDS):
                return Snapshot("invalid", detail=self._error)
            if self._last_valid is None:
                return Snapshot("waiting")
            return Snapshot("stale")


class Receiver:
    def __init__(self, host: str, port: int):
        self.state = TelemetryState()
        self._stop = threading.Event()
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Deliberately no SO_REUSEADDR: a competing receiver must fail clearly.
            if sys.platform == "win32":
                self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            self._socket.bind((host, port))
            self._socket.settimeout(0.2)
        except OSError:
            self._socket.close()
            raise
        self.port = self._socket.getsockname()[1]
        self._thread = threading.Thread(target=self._run, name="forza-udp", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                packet, _address = self._socket.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError as exc:
                if not self._stop.is_set():
                    self.state.fail(f"데이터 수신 오류: {exc}")
                break
            self.state.accept(packet)

    def close(self):
        self._stop.set()
        self._thread.join(timeout=0.5)
        self._socket.close()


@dataclass(frozen=True)
class Monitor:
    x: int
    y: int
    width: int
    height: int
    primary: bool = False


def enable_dpi_awareness():
    if sys.platform == "win32":
        try:
            fn = ctypes.windll.user32.SetProcessDpiAwarenessContext
            fn.argtypes = [ctypes.c_void_p]
            fn.restype = ctypes.c_int
            fn(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2, before creating Tk.
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()


def get_monitors(root) -> list[Monitor]:
    if sys.platform == "win32":
        from ctypes import wintypes as w

        class MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", w.DWORD), ("monitor", w.RECT),
                        ("work", w.RECT), ("flags", w.DWORD)]

        user32 = ctypes.windll.user32
        result = []
        callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HANDLE, w.HDC,
                                           ctypes.POINTER(w.RECT), w.LPARAM)
        user32.GetMonitorInfoW.argtypes = [w.HANDLE, ctypes.POINTER(MonitorInfo)]
        user32.GetMonitorInfoW.restype = w.BOOL
        user32.EnumDisplayMonitors.argtypes = [w.HDC, ctypes.POINTER(w.RECT), callback_type, w.LPARAM]
        user32.EnumDisplayMonitors.restype = w.BOOL

        @callback_type
        def visit(handle, _hdc, _rect, _data):
            info = MonitorInfo()
            info.cbSize = ctypes.sizeof(info)
            if user32.GetMonitorInfoW(handle, ctypes.byref(info)):
                r = info.monitor
                if r.right > r.left and r.bottom > r.top:
                    result.append(Monitor(r.left, r.top, r.right - r.left,
                                          r.bottom - r.top, bool(info.flags & 1)))
            return True

        if user32.EnumDisplayMonitors(None, None, visit, 0) and result:
            # Primary first, then the remaining screens in desktop position order.
            return sorted(result, key=lambda m: (not m.primary, m.x, m.y))
    return [Monitor(0, 0, root.winfo_screenwidth(), root.winfo_screenheight(), True)]


def window_rect(monitor: Monitor, fullscreen: bool) -> tuple[int, int, int, int]:
    if fullscreen:
        return monitor.x, monitor.y, monitor.width, monitor.height
    width = min(1100, int(monitor.width * 0.86))
    height = min(730, int(monitor.height * 0.82))
    return (monitor.x + (monitor.width - width) // 2,
            monitor.y + (monitor.height - height) // 2, width, height)


def place_window(root, rect):
    x, y, width, height = rect
    root.geometry(f"{width}x{height}")
    root.update_idletasks()
    if sys.platform == "win32":
        from ctypes import wintypes as w
        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [w.HWND, w.UINT]
        user32.GetAncestor.restype = w.HWND
        user32.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, w.UINT]
        user32.SetWindowPos.restype = w.BOOL
        hwnd = user32.GetAncestor(root.winfo_id(), 2)  # GA_ROOT: Tk's outer window.
        # Win32 uses absolute desktop coordinates, including monitors left of zero.
        if not user32.SetWindowPos(hwnd, None, x, y, width, height, 0x0014):
            raise OSError("모니터 위치를 설정할 수 없습니다")
    else:
        root.geometry(f"{width}x{height}{x:+d}{y:+d}")


class Speedometer:
    GAUGE_MAX = 400
    BG = "#0a0c0f"
    TEXT = "#f3f5f7"
    MUTED = "#89929f"
    LINE = "#242a32"
    ACCENT = "#d5fa45"

    def __init__(self, root, args, receiver):
        import tkinter as tk
        self.root, self.args, self.receiver = root, args, receiver
        self.monitors = get_monitors(root)
        self.monitor_index = min(args.monitor - 1, len(self.monitors) - 1)
        self.fullscreen = not args.windowed and (len(self.monitors) > 1 or args.fullscreen)
        self.closed = False
        self._layout_size = None
        self._last_render = None
        self._paint_job = None
        self.text_ids = {}
        self.bar_ids = []
        self.font = "Malgun Gothic" if sys.platform == "win32" else "Helvetica"
        self.digit_font = "Bahnschrift" if sys.platform == "win32" else "Helvetica Neue"

        root.title("Forza Horizon 6 | 세컨 모니터 속도계")
        root.configure(bg=self.BG)
        root.minsize(600, 430)
        self.canvas = tk.Canvas(root, bg=self.BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        from music_player import MusicPanel
        self.music_panel = MusicPanel(self.canvas, args.music_dir)
        self.canvas.bind("<Configure>", lambda _event: self.render(force=True))
        root.bind("<F11>", self.toggle_fullscreen)
        root.bind("<Escape>", self.leave_fullscreen)
        root.bind("<m>", self.next_monitor)
        root.bind("<M>", self.next_monitor)
        root.bind("<Control-q>", lambda _event: self.close())
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.position()
        self.tick()

    def position(self):
        # Borderless fullscreen covers only the selected monitor; never spans both.
        self.root.overrideredirect(self.fullscreen)
        place_window(self.root, window_rect(self.monitors[self.monitor_index], self.fullscreen))

    def toggle_fullscreen(self, _event=None):
        if not self.fullscreen:
            # If the user dragged the window, fill the monitor it is now on.
            x = self.root.winfo_rootx() + self.root.winfo_width() // 2
            y = self.root.winfo_rooty() + self.root.winfo_height() // 2
            self.monitors = get_monitors(self.root)
            for i, monitor in enumerate(self.monitors):
                if monitor.x <= x < monitor.x + monitor.width and monitor.y <= y < monitor.y + monitor.height:
                    self.monitor_index = i
                    break
            self.monitor_index = min(self.monitor_index, len(self.monitors) - 1)
        self.fullscreen = not self.fullscreen
        self.position()
        self.render(force=True)

    def leave_fullscreen(self, _event=None):
        if self.fullscreen:
            self.toggle_fullscreen()

    def next_monitor(self, _event=None):
        self.monitors = get_monitors(self.root)
        self.monitor_index = (self.monitor_index + 1) % len(self.monitors)
        self.position()
        self.render(force=True)

    def current(self):
        return self.receiver.state.snapshot()

    def layout(self, width, height):
        c = self.canvas
        c.delete("all")
        self.text_ids = {}
        self.bar_ids = []
        s = min(width / 1100, height / 730)
        self.scale = s
        left, right = 48 * s, width - 48 * s

        def text(key, x, y, value, size, color=None, anchor="center", bold=False, family=None):
            item = c.create_text(x, y, text=value, fill=color or self.TEXT, anchor=anchor,
                                 font=(family or self.font, -max(9, int(size * s)), "bold" if bold else "normal"))
            self.text_ids[key] = item
            return item

        text("brand", left, 47 * s, "FH6  /  LIVE SPEED", 16, anchor="w", bold=True)
        text("status", right, 47 * s, "", 14, anchor="e", bold=True)
        c.create_line(left, 84 * s, right, 84 * s, fill=self.LINE)
        self.music_panel.resize(s, right - left)
        # Anchor the bottom of the music bar just above the dial, even on tall screens.
        music_y = height - 447 * s
        c.create_window(width / 2, music_y, window=self.music_panel,
                        width=right - left, anchor="s")
        # A 270-degree dial leaves the bottom open for the scale caption.
        cx, cy, radius = width * 0.28, height - 230 * s, 205 * s
        self.gauge_geometry = (cx, cy, radius)
        bounds = (cx - radius, cy - radius, cx + radius, cy + radius)
        c.create_arc(*bounds, start=-45, extent=270, style="arc",
                     outline=self.LINE, width=8 * s)
        self.gauge_arc = c.create_arc(*bounds, start=225, extent=0, style="arc",
                                      outline=self.ACCENT, width=8 * s)
        for value in range(0, self.GAUGE_MAX + 1, 10):
            major = value % 40 == 0
            angle = math.radians(225 - 270 * value / self.GAUGE_MAX)
            inner = radius - (25 if major else 15) * s
            outer = radius - 7 * s
            c.create_line(cx + inner * math.cos(angle), cy - inner * math.sin(angle),
                          cx + outer * math.cos(angle), cy - outer * math.sin(angle),
                          fill=self.TEXT if major else self.MUTED, width=(2 if major else 1) * s)
            if major:
                label_radius = radius - 47 * s
                text(f"tick_{value}", cx + label_radius * math.cos(angle),
                     cy - label_radius * math.sin(angle), str(value), 17,
                     self.MUTED, family=self.digit_font)
        text("dial_unit", cx, cy + 70 * s, "km/h", 16, self.MUTED)
        text("dial_range", cx, cy + 168 * s, "0–400 km/h", 12, self.MUTED)
        self.needle = c.create_polygon(0, 0, 0, 0, 0, 0, fill=self.ACCENT,
                                       outline="", state="hidden")
        c.create_oval(cx - 10 * s, cy - 10 * s, cx + 10 * s, cy + 10 * s,
                      fill=self.BG, outline=self.MUTED, width=3 * s)
        digital_x = width * 0.745
        text("eyebrow", digital_x, cy - 155 * s, "VEHICLE SPEED", 13, self.MUTED)
        text("speed", digital_x, cy - 45 * s, "—", 165, bold=True, family=self.digit_font)
        text("unit", digital_x, cy + 55 * s, "km/h", 26, self.ACCENT)
        text("hint", width / 2, 112 * s, "", 13, self.MUTED)
        c.itemconfigure(self.text_ids["hint"], width=width - 80 * s)

        # Keep RPM alongside the dial so all instruments sit at the bottom.
        top = height - 142 * s
        x1, x2 = width * 0.54, right
        c.create_line(x1, top, x2, top, fill=self.LINE)
        text("rpm_label", x1, top + 28 * s, "ENGINE", 11, self.MUTED, anchor="w")
        text("rpm", x2, top + 28 * s, "— rpm", 23, anchor="e", bold=True)
        for i in range(36):
            start = x1 + (x2 - x1) * i / 36
            end = x1 + (x2 - x1) * (i + 1) / 36 - 5 * s
            self.bar_ids.append(c.create_rectangle(start, top + 54 * s, end, top + 79 * s,
                                                   fill=self.LINE, outline=""))
        text("footer", left, height - 27 * s, "", 11, self.MUTED, anchor="w")
        text("exit", right, height - 27 * s, "종료", 11, self.MUTED, anchor="e")
        c.tag_bind(self.text_ids["exit"], "<Button-1>", lambda _event: self.close())
        self._layout_size = (width, height)

    def render(self, force=False):
        if self.closed:
            return
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        if width < 10 or height < 10:
            return
        if self._layout_size != (width, height):
            self.layout(width, height)
            force = True
        snap = self.current()
        active = snap.status == "live"
        sample = snap.sample if active else None
        status_map = {
            "live": ("●  실시간 수신", self.ACCENT, ""),
            "waiting": ("○  연결 대기", self.MUTED, f"게임 Data Out 켜기  ·  IP {self.args.host}  ·  포트 {self.args.port}"),
            "paused": ("●  주행 대기", "#ffbd69", "주행을 시작하면 속도가 표시됩니다"),
            "stale": ("○  수신 중단", "#ffbd69", "메뉴·일시정지 상태를 확인하고 게임으로 돌아가 주행하세요"),
            "invalid": ("●  데이터 형식 확인", "#ff7979", snap.detail),
            "error": ("●  연결 오류", "#ff7979", snap.detail),
        }
        label, color, hint = status_map[snap.status]
        speed = str(int(sample.speed_kmh + 0.5)) if sample else "—"
        rpm = f"{sample.rpm:,.0f} rpm" if sample else "— rpm"
        ratio = min(1.0, sample.rpm / sample.max_rpm) if sample and sample.max_rpm > 0 else 0
        lit = round(36 * ratio)
        footer = f"F11 전체 화면  ·  Esc 창 모드  ·  M 화면 이동     |     화면 {self.monitor_index + 1}/{len(self.monitors)}"
        gauge_speed = min(self.GAUGE_MAX, max(0, sample.speed_kmh)) if sample else None
        rendered = (label, speed, rpm, lit, hint, footer, gauge_speed)
        if not force and rendered == self._last_render:
            return
        self._last_render = rendered
        c = self.canvas
        if gauge_speed is None:
            c.itemconfigure(self.needle, state="hidden")
            c.itemconfigure(self.gauge_arc, extent=0)
        else:
            cx, cy, radius = self.gauge_geometry
            angle = math.radians(225 - 270 * gauge_speed / self.GAUGE_MAX)
            dx, dy = math.cos(angle), -math.sin(angle)
            half_width, tail = 5 * self.scale, 20 * self.scale
            c.coords(self.needle,
                     cx + dx * (radius - 32 * self.scale), cy + dy * (radius - 32 * self.scale),
                     cx - dx * tail - dy * half_width, cy - dy * tail + dx * half_width,
                     cx - dx * tail + dy * half_width, cy - dy * tail - dx * half_width)
            c.itemconfigure(self.needle, state="normal")
            c.itemconfigure(self.gauge_arc, extent=-270 * gauge_speed / self.GAUGE_MAX)
        c.itemconfigure(self.text_ids["status"], text=label, fill=color)
        c.itemconfigure(self.text_ids["speed"], text=speed, fill=self.TEXT if active else "#4a535f")
        c.itemconfigure(self.text_ids["rpm"], text=rpm)
        c.itemconfigure(self.text_ids["hint"], text=hint)
        c.itemconfigure(self.text_ids["footer"], text=footer)
        for i, item in enumerate(self.bar_ids):
            fill = ("#ff7979" if i >= 31 else self.ACCENT) if i < lit else self.LINE
            c.itemconfigure(item, fill=fill)

    def tick(self):
        if not self.closed:
            self.music_panel.update_player(time.monotonic())
            self.render()
            self._paint_job = self.root.after(33, self.tick)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self._paint_job:
            self.root.after_cancel(self._paint_job)
        self.music_panel.close()
        if self.receiver:
            self.receiver.close()
        self.root.destroy()


def port_number(value):
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("포트는 1~65535 사이여야 합니다")
    if 5200 <= port <= 5300:
        raise argparse.ArgumentTypeError("5200~5300은 게임에서 사용하는 범위입니다. 5301 등을 사용하세요")
    return port


def main():
    parser = argparse.ArgumentParser(description="Forza Horizon 6 세컨 모니터 속도계")
    parser.add_argument("--port", type=port_number, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1", help="UDP 수신 주소 (같은 PC: 127.0.0.1)")
    parser.add_argument("--monitor", type=int, default=2, help="1=주 모니터, 2=첫 보조 모니터")
    parser.add_argument("--music-dir", type=Path, default=Path(__file__).resolve().parent / "MUSIC",
                        help="음악 폴더 (기본: 속도계 폴더 안 MUSIC, MP3/WAV)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--windowed", action="store_true", help="창 모드로 시작")
    group.add_argument("--fullscreen", action="store_true", help="모니터가 하나여도 전체 화면으로 시작")
    args = parser.parse_args()
    if args.monitor < 1:
        parser.error("--monitor는 1 이상이어야 합니다")
    import tkinter as tk
    from tkinter import messagebox
    enable_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    receiver = None
    try:
        receiver = Receiver(args.host, args.port)
    except OSError as exc:
        messagebox.showerror("속도계 연결 오류",
                             f"{args.host}:{args.port}에서 데이터를 받을 수 없습니다.\n\n"
                             "이미 켜진 속도계나 같은 포트를 사용하는 프로그램을 종료하세요.\n"
                             "포트를 바꿀 경우 게임 설정도 같은 값으로 맞추세요.\n\n"
                             f"오류: {exc}", parent=root)
        root.destroy()
        return 1
    try:
        app = Speedometer(root, args, receiver)
        root.deiconify()
        root.after(100, app.position)
        root.mainloop()
    finally:
        if receiver:
            receiver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
