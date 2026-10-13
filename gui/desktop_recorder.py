"""Ordinary Windows recorder: local files first, upload through the user's browser session."""
from pathlib import Path
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox
import webbrowser

from gui.dual_recorder import DualRecorder, available_devices


class DesktopRecorder(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("會議助理 · 桌面收音")
        self.geometry("620x540")
        self.minsize(460, 490)
        self.recorder = DualRecorder(Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "MeetingAssistant" / "recordings")
        self.events = queue.Queue()
        self.busy = False
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="收錄線上同仁與自己的聲音", font=("Microsoft JhengHei", 16, "bold")).pack(anchor="w")
        ttk.Label(frame, text="錄音儲存在這台電腦；完成後透過瀏覽器上傳。", wraplength=540).pack(anchor="w", pady=(6, 16))
        device_error = None
        try:
            self.devices = available_devices()
        except Exception as exc:
            self.devices = {"speakers": [], "microphones": [], "default_speaker": None, "default_microphone": None}
            device_error = f"無法讀取音訊裝置：{exc}。請確認耳機／喇叭連線後重開程式。"
        self.speaker = self._device_choice(frame, "同仁聲音播放裝置（系統音訊）", "speakers", "default_speaker")
        self.microphone = self._device_choice(frame, "自己的麥克風", "microphones", "default_microphone")
        self.include_mic = tk.BooleanVar(value=True)
        self.mic_toggle = ttk.Checkbutton(frame, text="同時收錄自己的麥克風", variable=self.include_mic)
        self.mic_toggle.pack(anchor="w", pady=8)
        self.levels = {}
        for name, label in [("system", "系統音訊"), ("microphone", "麥克風")]:
            ttk.Label(frame, text=label).pack(anchor="w")
            meter = ttk.Progressbar(frame, maximum=1)
            meter.pack(fill="x", pady=(3, 8))
            self.levels[name] = meter
        self.status = ttk.Label(frame, text=device_error or "請確認播放裝置，再開始錄音。", wraplength=540)
        self.status.pack(anchor="w", pady=10)
        self.record = ttk.Button(frame, text="開始錄音", command=self.toggle)
        self.record.pack(fill="x", pady=5)
        self.open_folder = ttk.Button(frame, text="開啟錄音資料夾", command=self.show_files, state="disabled")
        self.open_folder.pack(fill="x", pady=5)
        ttk.Button(frame, text="開啟會議系統上傳", command=lambda: webbrowser.open(
            os.getenv("MEETING_SERVER_URL", "http://192.168.20.62:8001/history"))).pack(fill="x", pady=5)
        self.after(200, self.poll)
        self.protocol("WM_DELETE_WINDOW", self.close)

    def _device_choice(self, parent, title, key, default):
        ttk.Label(parent, text=title).pack(anchor="w")
        combo = ttk.Combobox(parent, values=[d["name"] for d in self.devices[key]], state="readonly")
        combo.pack(fill="x", pady=(4, 10))
        if self.devices[key]:
            index = next((i for i, d in enumerate(self.devices[key]) if d["id"] == self.devices[default]), 0)
            combo.current(index)
        return combo

    def toggle(self):
        if self.busy:
            self.record.configure(state="disabled", text="正在儲存…")
            threading.Thread(target=self.finish, daemon=True).start()
            return
        try:
            if self.speaker.current() < 0:
                raise ValueError("找不到播放裝置，請接上喇叭或耳機後重開程式。")
            mic = None
            if self.include_mic.get():
                if self.microphone.current() < 0:
                    raise ValueError("找不到麥克風，請取消同時收錄或接上麥克風。")
                mic = self.devices["microphones"][self.microphone.current()]["id"]
            self.recorder.start(speaker_id=self.devices["speakers"][self.speaker.current()]["id"], microphone_id=mic)
            self.busy = True
            self.record.configure(text="停止並儲存")
            for widget in (self.speaker, self.microphone, self.mic_toggle):
                widget.configure(state="disabled")
        except Exception as exc:
            self.status.configure(text=str(exc))

    def finish(self):
        try:
            state = self.recorder.stop()
            if state["errors"]:
                raise RuntimeError("；".join(state["errors"]))
            path = self.recorder.mix()
            self.events.put(f"已儲存 {path.name}，請開啟系統上傳。")
        except Exception as exc:
            self.events.put(f"原始錄音已保留。合併未完成：{exc}")

    def poll(self):
        try:
            message = self.events.get_nowait()
            self.busy = False
            self.status.configure(text=message)
            self.record.configure(state="normal", text="開始新錄音")
            self.open_folder.configure(state="normal")
            self.speaker.configure(state="readonly")
            self.microphone.configure(state="readonly")
            self.mic_toggle.configure(state="normal")
        except queue.Empty:
            pass
        state = self.recorder.snapshot()
        for name, meter in self.levels.items():
            meter["value"] = state["tracks"].get(name, {}).get("peak", 0)
        if self.busy and str(self.record["state"]) != "disabled":
            if state["errors"]:
                self.toggle()
            elif not state["tracks"].get("system", {}).get("last_signal"):
                self.status.configure(text="錄音中，尚未偵測到同仁聲音。請讓對方說話並確認系統音量表有反應。")
            else:
                self.status.configure(text="錄音中；系統音訊與麥克風各自保存。")
        self.after(200, self.poll)

    def show_files(self):
        if self.recorder.folder:
            os.startfile(self.recorder.folder)

    def close(self):
        if self.busy:
            messagebox.showinfo("錄音中", "請先停止並儲存錄音，再關閉視窗。")
            return
        self.destroy()


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        # Import and device-enumeration smoke test; never activate recording.
        import audioop
        available_devices()
    else:
        DesktopRecorder().mainloop()
