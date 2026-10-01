"""Local MP3/WAV playback and Windows global hotkeys; no extra packages."""

import ctypes
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk


class MusicPlayer:
    def __init__(self, folder):
        self.folder = Path(folder).expanduser().resolve()
        self.tracks = []
        self.index = -1
        self.opened = False
        self.playing = False
        self.length = self.position = 0
        self.volume = 50
        self.error = ""
        self.alias = f"forza_music_{id(self)}"
        self.api = None
        if sys.platform == "win32":
            from ctypes import wintypes as w
            self.api = ctypes.WinDLL("winmm")
            self.api.mciSendStringW.argtypes = [w.LPCWSTR, w.LPWSTR, w.UINT, w.HWND]
            self.api.mciSendStringW.restype = w.DWORD
            self.api.mciGetErrorStringW.argtypes = [w.DWORD, w.LPWSTR, w.UINT]
            self.api.mciGetErrorStringW.restype = w.BOOL
        self.refresh()

    def command(self, command):
        if self.api is None:
            raise RuntimeError("음악 재생은 Windows에서 지원합니다")
        result = ctypes.create_unicode_buffer(512)
        code = self.api.mciSendStringW(command, result, len(result), None)
        if code:
            self.api.mciGetErrorStringW(code, result, len(result))
            raise RuntimeError(result.value or f"음악 재생 오류 {code}")
        return result.value

    @property
    def title(self):
        return self.tracks[self.index].stem if self.index >= 0 else "MUSIC 폴더에 MP3 / WAV 파일을 넣어주세요"

    def close_track(self):
        if self.opened:
            try:
                self.command(f"close {self.alias}")
            finally:
                self.opened = self.playing = False
                self.length = self.position = 0

    def refresh(self):
        self.close_track()
        self.index = -1
        self.tracks = []
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            self.tracks = sorted((p for p in self.folder.iterdir()
                                  if p.is_file() and p.suffix.lower() in (".mp3", ".wav")),
                                 key=lambda p: p.name.casefold())
            self.error = ""
        except OSError as exc:
            self.error = f"MUSIC 폴더를 읽을 수 없습니다: {exc}"

    def load(self, index):
        if not self.tracks:
            return
        self.close_track()
        self.index = index % len(self.tracks)
        path = self.tracks[self.index]
        self.command(f'open "{path}" type mpegvideo alias {self.alias}')
        self.opened = True
        try:
            self.command(f"set {self.alias} time format milliseconds")
            self.length = int(self.command(f"status {self.alias} length"))
            self.set_volume(self.volume)
            self.command(f"play {self.alias} from 0")
            self.playing = True
            self.error = ""
        except (RuntimeError, ValueError):
            self.close_track()
            raise

    def toggle(self):
        if not self.opened:
            self.load(max(0, self.index))
        elif self.playing:
            self.command(f"pause {self.alias}")
            self.playing = False
        else:
            self.command(f"play {self.alias} from {self.position}")
            self.playing = True

    def step(self, amount):
        self.load((self.index + amount) if self.index >= 0 else (0 if amount > 0 else -1))

    def seek(self, ratio):
        if self.opened and self.length:
            position = min(self.length - 1, max(0, int(self.length * ratio)))
            was_playing = self.playing
            self.command(f"seek {self.alias} to {position}")
            if was_playing:
                self.command(f"play {self.alias}")
            self.position = position

    def set_volume(self, value):
        value = min(100, max(0, int(float(value))))
        if self.opened:
            self.command(f"setaudio {self.alias} volume to {value * 10}")
        self.volume = value

    def update(self):
        if not self.opened:
            return
        self.position = int(self.command(f"status {self.alias} position"))
        mode = self.command(f"status {self.alias} mode")
        if self.playing and mode == "stopped":
            # An errored next track stops here, rather than looping through bad files.
            self.playing = False
            self.step(1)


class GlobalHotkeys:
    """Own message thread: Tk must not consume WM_HOTKEY before we see it."""
    KEYS = [(ord("P"), "toggle"), (0x25, "previous"), (0x27, "next"),
            (0x26, "louder"), (0x28, "quieter")]

    def __init__(self):
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.thread = None
        if sys.platform == "win32":
            self.thread = threading.Thread(target=self.run, name="music-hotkeys", daemon=True)
            self.thread.start()

    def run(self):
        from ctypes import wintypes as w
        api = ctypes.WinDLL("user32", use_last_error=True)
        api.RegisterHotKey.argtypes = [w.HWND, ctypes.c_int, w.UINT, w.UINT]
        api.RegisterHotKey.restype = w.BOOL
        api.UnregisterHotKey.argtypes = [w.HWND, ctypes.c_int]
        api.UnregisterHotKey.restype = w.BOOL
        api.PeekMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT]
        api.PeekMessageW.restype = w.BOOL
        registered = {}
        try:
            for key_id, (vk, action) in enumerate(self.KEYS, 1):
                if api.RegisterHotKey(None, key_id, 0x4003, vk):  # Ctrl+Alt, no repeats
                    registered[key_id] = action
                else:
                    self.events.put(("warning", "일부 음악 단축키 등록 실패 · 화면 버튼 사용"))
            message = w.MSG()
            while not self.stop.is_set():
                while api.PeekMessageW(ctypes.byref(message), None, 0x0312, 0x0312, 1):
                    action = registered.get(message.wParam)
                    if action:
                        self.events.put(("action", action))
                self.stop.wait(0.03)
        finally:
            for key_id in registered:
                api.UnregisterHotKey(None, key_id)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=1)


class MusicPanel(tk.Frame):
    def __init__(self, parent, folder):
        super().__init__(parent, bg="#14191f", padx=12, pady=5)
        self.player = MusicPlayer(folder)
        self.hotkeys = GlobalHotkeys()
        self.warning = ""
        self.dragging = False
        self.last_update = 0
        self.columnconfigure(0, weight=1)
        self.title_label = tk.Label(self, text=self.player.title, anchor="w", bg="#14191f", width=1,
                                    fg="#f3f5f7", font=("Malgun Gothic", 12, "bold"))
        self.title_label.grid(row=0, column=0, sticky="ew")
        self.time_label = tk.Label(self, bg="#14191f", fg="#89929f", text="0:00 / 0:00")
        self.time_label.grid(row=0, column=1, sticky="e")
        self.progress = tk.Scale(self, from_=0, to=1000, orient="horizontal", showvalue=False,
                                 bg="#14191f", troughcolor="#242a32", highlightthickness=0,
                                 bd=0, sliderlength=12, width=8, activebackground="#d5fa45")
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.progress.bind("<ButtonPress-1>", self.begin_seek)
        self.progress.bind("<B1-Motion>", self.move_seek)
        self.progress.bind("<ButtonRelease-1>", self.end_seek)
        controls = tk.Frame(self, bg="#14191f")
        controls.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.buttons = []
        for label, action in [("이전", "previous"), ("재생", "toggle"),
                              ("다음", "next"), ("새로고침", "refresh")]:
            button = tk.Button(controls, text=label, command=lambda a=action: self.action(a),
                               bg="#242a32", fg="#f3f5f7", activebackground="#39434f",
                               activeforeground="#d5fa45", bd=0, padx=10, takefocus=False)
            button.pack(side="left", padx=(0, 5))
            self.buttons.append(button)
        self.volume_label = tk.Label(controls, text="음량", bg="#14191f", fg="#89929f")
        self.volume_label.pack(side="left", padx=5)
        self.volume = tk.Scale(controls, from_=0, to=100, orient="horizontal", length=100,
                               bg="#14191f", fg="#89929f", troughcolor="#242a32",
                               highlightthickness=0, bd=0, showvalue=False, width=8)
        self.volume.set(self.player.volume)
        self.volume.configure(command=lambda v: self.safe(lambda: self.player.set_volume(v)))
        self.volume.pack(side="left")
        self.note = tk.Label(self, anchor="w", bg="#14191f", fg="#89929f", width=1)
        self.note.grid(row=3, column=0, columnspan=2, sticky="ew")

    def resize(self, scale, width):
        # Give the title, seek bar and controls more vertical breathing room.
        self.configure(pady=max(4, int(8 * scale)))
        small = -max(9, int(11 * scale))
        for widget in [self.note, self.time_label, self.volume_label, *self.buttons]:
            widget.configure(font=("Malgun Gothic", small))
        self.title_label.configure(font=("Malgun Gothic", -max(12, int(21 * scale)), "bold"))
        self.title_label.grid_configure(pady=(0, max(2, int(4 * scale))))
        self.progress.configure(width=max(10, int(14 * scale)))
        self.progress.grid_configure(pady=(0, max(2, int(4 * scale))))
        self.note.grid_configure(pady=(max(2, int(4 * scale)), 0))
        for button in self.buttons:
            button.configure(padx=max(3, int(10 * scale)), pady=max(1, int(4 * scale)))
        self.volume.configure(length=max(45, int(100 * scale)))

    def safe(self, callback):
        try:
            self.player.error = ""
            callback()
        except (OSError, RuntimeError, ValueError) as exc:
            self.player.error = str(exc)
            self.player.playing = False

    def action(self, action):
        actions = {"toggle": self.player.toggle, "previous": lambda: self.player.step(-1),
                   "next": lambda: self.player.step(1), "refresh": self.player.refresh,
                   "louder": lambda: self.player.set_volume(self.player.volume + 5),
                   "quieter": lambda: self.player.set_volume(self.player.volume - 5)}
        self.safe(actions[action])
        self.volume.set(self.player.volume)
        self.last_update = 0

    def begin_seek(self, event):
        self.dragging = True
        return self.move_seek(event)

    def move_seek(self, event):
        self.progress.set(min(1000, max(0, (event.x - 6) / max(1, self.progress.winfo_width() - 12) * 1000)))
        return "break"

    def end_seek(self, event):
        self.move_seek(event)
        self.safe(lambda: self.player.seek(self.progress.get() / 1000))
        self.dragging = False
        return "break"

    def update_player(self, now):
        while True:
            try:
                kind, value = self.hotkeys.events.get_nowait()
            except queue.Empty:
                break
            if kind == "action":
                self.action(value)
            else:
                self.warning = value
        if now - self.last_update < 0.25:
            return
        self.last_update = now
        if not self.player.error:
            self.safe(self.player.update)
        p = self.player
        self.title_label.configure(text=p.title)
        def timestamp(ms):
            seconds = max(0, ms // 1000)
            return f"{seconds // 60}:{seconds % 60:02d}"
        self.time_label.configure(text=f"{timestamp(p.position)} / {timestamp(p.length)}")
        self.buttons[1].configure(text="일시정지" if p.playing else "재생")
        if not self.dragging:
            self.progress.set(1000 * p.position / p.length if p.length else 0)
        note = p.error or self.warning or "Ctrl+Alt: P 재생/일시정지 · ← 이전 · → 다음 · ↑↓ 음량"
        self.note.configure(text=note, fg="#ff7979" if p.error else "#89929f")

    def close(self):
        self.hotkeys.close()
        try:
            self.player.close_track()
        except RuntimeError:
            pass
