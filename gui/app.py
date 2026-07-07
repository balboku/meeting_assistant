"""
gui/app.py - 桌面錄音 GUI 主程式
=============================================================================
使用 Tkinter 建立輕量級桌面視窗介面，整合：
  - AudioRecorder (sounddevice)：麥克風錄音
  - MeetingAPIClient (requests)：與 FastAPI 後端溝通
=============================================================================
執行方式：
    python3 gui/app.py
（後端 backend/main.py 必須已在 Port 8001 執行）
=============================================================================
"""

import os
import subprocess
import tempfile
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

from gui.api_client import MeetingAPIClient
from gui.recorder import AudioRecorder

# ─── 配色與字體常數 ──────────────────────────────────────────────────────────
BG_DARK       = "#1a1b26"
BG_CARD       = "#24283b"
BG_CARD_LIGHT = "#2f3349"
ACCENT_RED    = "#f7768e"
ACCENT_GREEN  = "#9ece6a"
ACCENT_BLUE   = "#7aa2f7"
ACCENT_YELLOW = "#e0af68"
TEXT_PRIMARY  = "#c0caf5"
TEXT_DIM      = "#565f89"
FONT_TITLE    = ("Helvetica", 20, "bold")
FONT_LABEL    = ("Helvetica", 13)
FONT_STATUS   = ("Helvetica", 14, "bold")
FONT_SMALL    = ("Helvetica", 10)
FONT_TIMER    = ("Helvetica", 24, "bold")

POLL_INTERVAL_MS  = 3000   # 每 3 秒輪詢一次任務狀態
OUTPUT_DIR = Path(__file__).parent.parent / "output"


# =============================================================================
# 主視窗應用程式
# =============================================================================

class MeetingAssistantApp(tk.Tk):
    """AI 語音會議助理 - 桌面主視窗"""

    def __init__(self):
        super().__init__()
        self.title("🎙 AI 語音會議助理")
        self.resizable(False, False)
        self.configure(bg=BG_DARK)

        # 模組初始化
        self.recorder    = AudioRecorder(samplerate=44100, channels=1)
        self.api_client  = MeetingAPIClient()

        # 應用程式狀態
        self.state_label     = "IDLE"   # IDLE / RECORDING / UPLOADING / PROCESSING / DONE / ERROR
        self.job_id          = None
        self.output_path     = None
        self.recording_secs  = 0
        self.timer_job       = None
        self._tmp_file       = None    # tempfile.NamedTemporaryFile

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ─── UI 建構 ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        # 外層 padding 容器
        outer = tk.Frame(self, bg=BG_DARK, padx=28, pady=24)
        outer.pack(fill="both", expand=True)

        # ── 標題 ──
        tk.Label(outer, text="🎙 AI 語音會議助理", font=FONT_TITLE,
                 bg=BG_DARK, fg=ACCENT_BLUE).pack(pady=(0, 4))
        tk.Label(outer, text="由 Gemini 3.1 Flash-Lite 驅動", font=FONT_SMALL,
                 bg=BG_DARK, fg=TEXT_DIM).pack()

        sep = tk.Frame(outer, height=1, bg=BG_CARD_LIGHT)
        sep.pack(fill="x", pady=16)

        # ── 會議標題輸入 ──
        title_frame = tk.Frame(outer, bg=BG_DARK)
        title_frame.pack(fill="x", pady=(0, 16))
        tk.Label(title_frame, text="📝 會議名稱", font=FONT_LABEL,
                 bg=BG_DARK, fg=TEXT_PRIMARY).pack(anchor="w")
        self.entry_title = tk.Entry(
            title_frame, font=FONT_LABEL,
            bg=BG_CARD, fg=TEXT_PRIMARY, insertbackground=TEXT_PRIMARY,
            relief="flat", bd=8,
        )
        self.entry_title.insert(0, f"會議_{datetime.now().strftime('%m%d_%H%M')}")
        self.entry_title.pack(fill="x", pady=(4, 0), ipady=6)

        # ── 計時器 ──
        self.lbl_timer = tk.Label(outer, text="00:00", font=FONT_TIMER,
                                   bg=BG_DARK, fg=TEXT_DIM)
        self.lbl_timer.pack(pady=(0, 12))

        # ── 主按鈕 ──
        self.btn_record = tk.Button(
            outer, text="⏺  開始錄音",
            font=("Helvetica", 15, "bold"),
            bg=ACCENT_RED, fg="#ffffff",
            activebackground="#ff9e64", activeforeground="#ffffff",
            relief="flat", bd=0, padx=24, pady=12, cursor="hand2",
            command=self._toggle_recording,
        )
        self.btn_record.pack(fill="x")

        sep2 = tk.Frame(outer, height=1, bg=BG_CARD_LIGHT)
        sep2.pack(fill="x", pady=16)

        # ── 狀態區域 ──
        status_frame = tk.Frame(outer, bg=BG_CARD, bd=0)
        status_frame.pack(fill="x")

        inner = tk.Frame(status_frame, bg=BG_CARD, padx=16, pady=14)
        inner.pack(fill="x")

        self.lbl_status = tk.Label(
            inner, text="⚪  就緒，請輸入會議名稱後開始錄音",
            font=FONT_STATUS, bg=BG_CARD, fg=TEXT_DIM,
            wraplength=340, justify="left",
        )
        self.lbl_status.pack(anchor="w")

        # ── 進度條 ──
        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        # 初始不顯示

        # ── 開啟輸出按鈕（完成時顯示）──
        self.btn_open = tk.Button(
            outer, text="📄 開啟會議記錄",
            font=FONT_LABEL,
            bg=ACCENT_GREEN, fg="#1a1b26",
            activebackground="#b9f27c",
            relief="flat", bd=0, padx=12, pady=8, cursor="hand2",
            command=self._open_output,
        )
        # 初始不顯示

        # ── 版本說明 ──
        tk.Label(outer, text="後端 API: http://127.0.0.1:8001", font=FONT_SMALL,
                 bg=BG_DARK, fg=TEXT_DIM).pack(pady=(12, 0))

    # ─── 錄音控制 ────────────────────────────────────────────────────────────

    def _toggle_recording(self):
        if self.state_label in ("UPLOADING", "PROCESSING"):
            return  # 上傳 / 處理中禁止重複操作
        if self.state_label == "RECORDING":
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self):
        title = self.entry_title.get().strip() or f"會議_{datetime.now().strftime('%m%d_%H%M')}"

        # 建立暫存 wav 檔
        self._tmp_file = tempfile.NamedTemporaryFile(
            suffix=".wav", prefix="meeting_", delete=False
        )
        tmp_path = self._tmp_file.name
        self._tmp_file.close()  # soundfile 自行開啟

        self.recorder.start_recording(tmp_path)
        self.state_label = "RECORDING"
        self.recording_secs = 0
        self.btn_open.pack_forget()
        self.progress.pack_forget()

        self._update_ui_recording()
        self._tick_timer()

    def _stop_recording(self):
        if self.timer_job:
            self.after_cancel(self.timer_job)
            self.timer_job = None

        self.recorder.stop_recording()
        self.state_label = "UPLOADING"
        self._update_ui_uploading()

        title = self.entry_title.get().strip()
        audio_path = self._tmp_file.name

        self.api_client.upload_audio(
            audio_path=audio_path,
            meeting_title=title,
            on_success=self._on_upload_success,
            on_error=self._on_error,
        )

    # ─── 計時器 ──────────────────────────────────────────────────────────────

    def _tick_timer(self):
        minutes, seconds = divmod(self.recording_secs, 60)
        self.lbl_timer.config(text=f"{minutes:02d}:{seconds:02d}", fg=ACCENT_RED)
        self.recording_secs += 1
        self.timer_job = self.after(1000, self._tick_timer)

    # ─── API 回呼 ─────────────────────────────────────────────────────────────

    def _on_upload_success(self, job_id: str):
        self.job_id = job_id
        self.state_label = "PROCESSING"
        # Tkinter after() 必須在主執行緒呼叫，透過 self.after(0,...) 排入主循環
        self.after(0, self._update_ui_processing)
        self.after(POLL_INTERVAL_MS, self._poll_status)

    def _poll_status(self):
        if self.state_label != "PROCESSING":
            return
        self.api_client.get_status(
            job_id=self.job_id,
            on_success=self._on_status_update,
            on_error=self._on_error,
        )

    def _on_status_update(self, data: dict):
        status = data.get("status", "")
        if status == "done":
            self.output_path = data.get("output_path")
            self.after(0, self._update_ui_done)
        elif status == "failed":
            detail = data.get("error_detail", "未知錯誤")
            self.after(0, lambda: self._on_error(detail))
        elif status == "cancelled":
            self.after(0, lambda: self._on_error("任務已取消"))
        else:
            # 仍在處理中，繼續輪詢
            self.after(POLL_INTERVAL_MS, self._poll_status)

    def _on_error(self, error: str):
        self.state_label = "ERROR"
        self.after(0, lambda: self._update_ui_error(error))

    # ─── UI 狀態更新 ──────────────────────────────────────────────────────────

    def _update_ui_recording(self):
        self.btn_record.config(text="⏹  停止錄音", bg=ACCENT_YELLOW)
        self.lbl_status.config(text="🔴  錄音中，請開始說話...", fg=ACCENT_RED)
        self.entry_title.config(state="disabled")

    def _update_ui_uploading(self):
        self.btn_record.config(text="⏺  開始錄音", bg=ACCENT_RED, state="disabled")
        self.lbl_status.config(text="📤  上傳音檔至 AI 後端中...", fg=ACCENT_YELLOW)
        self.lbl_timer.config(text="00:00", fg=TEXT_DIM)
        self.progress.pack(fill="x", pady=(12, 0))
        self.progress.start(10)

    def _update_ui_processing(self):
        self.lbl_status.config(text="⚙️   Gemini 語音辨識 & 摘要生成中...", fg=ACCENT_BLUE)

    def _update_ui_done(self):
        self.state_label = "DONE"
        self.progress.stop()
        self.progress.pack_forget()
        self.btn_record.config(state="normal")
        self.entry_title.config(state="normal")
        self.lbl_status.config(text="✅  會議記錄生成完成！", fg=ACCENT_GREEN)
        self.btn_open.pack(fill="x", pady=(12, 0))
        # 清理暫存音檔
        if self._tmp_file and os.path.exists(self._tmp_file.name):
            try:
                os.unlink(self._tmp_file.name)
            except Exception:
                pass

    def _update_ui_error(self, error: str):
        self.progress.stop()
        self.progress.pack_forget()
        self.btn_record.config(state="normal")
        self.entry_title.config(state="normal")
        self.lbl_status.config(text=f"❌  錯誤：{error}", fg=ACCENT_RED)
        messagebox.showerror("處理失敗", f"發生錯誤：\n{error}")

    # ─── 輔助功能 ─────────────────────────────────────────────────────────────

    def _open_output(self):
        if self.output_path and os.path.exists(self.output_path):
            # 跨平台開啟檔案
            subprocess.run(["open", self.output_path])
        else:
            messagebox.showwarning("找不到檔案", f"無法找到輸出檔案：\n{self.output_path}")

    def _on_close(self):
        if self.state_label == "RECORDING":
            self.recorder.stop_recording()
        self.destroy()


# =============================================================================
# 主程式入口
# =============================================================================

if __name__ == "__main__":
    app = MeetingAssistantApp()

    # 設定視窗置中於螢幕
    app.update_idletasks()
    w, h = 420, 460
    x = (app.winfo_screenwidth()  // 2) - (w // 2)
    y = (app.winfo_screenheight() // 2) - (h // 2)
    app.geometry(f"{w}x{h}+{x}+{y}")

    app.mainloop()
