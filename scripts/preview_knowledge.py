"""Run isolated UI fixtures on localhost: never loads the production database or API key."""
from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIXTURES = ROOT / "tmp" / "knowledge-preview"
FIXTURES.mkdir(parents=True, exist_ok=True)
os.environ["DB_PATH"] = str(FIXTURES / "meetings.db")
os.environ["GEMINI_API_KEY"] = "preview-not-a-credential"
os.environ["MEETING_AUTH_ENABLED"] = "0"
os.environ["APP_API_KEY"] = ""
os.environ["MEETING_ALLOW_CLOUD"] = "0"
os.environ["MEETING_OFFSITE_BACKUP_DIR"] = ""

from backend import database
from backend.company_vocabulary import propose, review, list_terms
database.init_db()
with database.get_db() as conn:
    count=conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]
if not count:
    for index,(title,body) in enumerate([
        ("測試會議 · Mercury 設計確認", "[01:20] **[發言者 A]**：Mercury 的 S-ring 驗證樣品需要 12 件。\n\n[02:10] **[發言者 B]**：We will review the results on Friday."),
        ("測試會議 · Mercury 追蹤", "[03:10] **[發言者 A]**：Mercury 驗證已完成第一階段，第二階段尚待確認。"),
    ]):
        path=FIXTURES/f"fixture-{index}.md"
        path.write_text("## 討論摘要 (Discussion Summary)\n測試資料\n\n## 完整逐字稿 (Verbatim Transcript)\n"+body,encoding="utf-8")
        database.save_meeting(title,"2026/10/13","synthetic.wav",str(path),summary="虛構的 UI 驗證資料")
if not list_terms():
    term=propose("S-ring",["S rain"],"虛構零件名稱，用於介面驗證。","preview-admin")
    review(term,"approved",1,"preview-admin")
from backend.main import app
import uvicorn
if __name__ == "__main__":
    # No worker/lifespan: UI verification cannot launch formal background jobs.
    uvicorn.run(app,host="127.0.0.1",port=8018,lifespan="off")
