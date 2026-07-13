"""
gui/api_client.py - 負責與 FastAPI 後端 (Port 8001) 進行 HTTP 通訊
"""
import threading
import requests
from pathlib import Path

# 後端 URL（與 backend/main.py 的啟動設定一致）
BACKEND_URL = "http://127.0.0.1:8001"


class MeetingAPIClient:
    """
    封裝所有對後端 API 的呼叫，提供 callback 機制讓 Tkinter 在主線程中更新 UI。
    所有網路請求都在背景執行緒中執行，不會凍結 GUI。
    """

    def upload_audio(self, audio_path: str, meeting_title: str,
                     on_success, on_error):
        """
        非同步上傳音檔：在背景執行緒中執行，完成後呼叫 callback。

        Args:
            audio_path: 音檔本地路徑
            meeting_title: 會議標題（傳給後端）
            on_success: 回傳 job_id 的 callback (job_id: str)
            on_error: 回傳錯誤訊息的 callback (error: str)
        """
        def _worker():
            try:
                p = Path(audio_path)
                with open(p, "rb") as f:
                    resp = requests.post(
                        f"{BACKEND_URL}/upload-media",
                        files={"file": (p.name, f, "audio/wav")},
                        data={"title": meeting_title},
                        timeout=30,
                    )
                resp.raise_for_status()
                job_id = resp.json()["job_id"]
                on_success(job_id)
            except Exception as e:
                on_error(str(e))

        threading.Thread(target=_worker, daemon=True).start()

    def get_status(self, job_id: str, on_success, on_error):
        """
        查詢任務狀態（同步版本，呼叫者自行在 after() 中使用）。

        Args:
            job_id: 任務 ID
            on_success: 回傳狀態字典的 callback (data: dict)
            on_error: 回傳錯誤訊息的 callback (error: str)
        """
        def _worker():
            try:
                resp = requests.get(
                    f"{BACKEND_URL}/status/{job_id}",
                    timeout=10,
                )
                resp.raise_for_status()
                on_success(resp.json())
            except Exception as e:
                on_error(str(e))

        threading.Thread(target=_worker, daemon=True).start()
