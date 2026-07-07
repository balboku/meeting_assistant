"""
=============================================================================
AI 語音會議助理 (AI Voice Meeting Assistant)
=============================================================================
版本:    1.0.0
作者:    資深 Python 後端工程師
描述:    讀取本地音檔，透過 Google Gemini API 原生音訊處理能力，
         一次性生成完整逐字稿與結構化會議記錄，並輸出為 Markdown 檔案。

技術堆疊:
    - Python 3.10+
    - google-genai (Gemini SDK)
    - python-dotenv

使用方式:
    python meeting_assistant.py --audio path/to/your/audio.mp3
    python meeting_assistant.py --audio path/to/your/audio.mp3 --output my_output_folder
=============================================================================
"""

import os
import sys
import time
import re
import argparse
import logging
from pathlib import Path
from datetime import datetime

from backend.logging_utils import configure_utf8_logging

# --- 第三方套件 ---
try:
    from google import genai
    from google.genai import types
except ImportError:
    print("❌ 錯誤：找不到 google-genai 套件。")
    print("   請執行: pip install google-genai")
    sys.exit(1)

try:
    from dotenv import load_dotenv
except ImportError:
    print("❌ 錯誤：找不到 python-dotenv 套件。")
    print("   請執行: pip install python-dotenv")
    sys.exit(1)


# =============================================================================
# 常數設定區
# =============================================================================

# 使用的 Gemini 模型（支援原生音訊理解）
GEMINI_MODEL = "gemini-3.1-flash-lite"

# 支援的音訊格式
SUPPORTED_AUDIO_FORMATS = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".webm": "audio/webm",
}

# 檔案上傳後的最長等待時間（秒）
MAX_UPLOAD_WAIT_SECONDS = 120

# 每次輪詢等待時間（秒）
POLLING_INTERVAL_SECONDS = 3

MULTILINGUAL_TRANSCRIPT_POLICY = """
【多語言處理規則】
- 摘要、決議與待辦事項仍統一使用繁體中文。
- 完整逐字稿需忠實呈現語言切換，不要把所有發言一律翻成同一種語言。
- 英文發言請保留英文原文；若句子較長，請在同段後方補上繁體中文翻譯，例如 `（中譯：...）`。
- 中文國語發言請以繁體中文轉寫。
- 台語發言請標記為 `[台語]`，並以繁體中文做語意轉寫；不要硬湊不確定的台語漢字。
- 台語聽不清楚時，請在對應位置標記 `[台語音訊不清晰]`。
- 人名、公司名、產品名、技術名詞與英文縮寫請盡量保留原文；必要時在後方補中文說明。
""".strip()


SPEAKER_DIFFERENTIATION_POLICY = """
【發言者辨識規則】
- 目標是分辨「不同聲音」，不是猜測真實姓名；除非音訊中明確自我介紹或互稱姓名，否則一律使用匿名標籤。
- 使用固定格式 **[發言者 A]**：、**[發言者 B]**：、**[發言者 C]**：；同一個聲音再次出現時必須沿用相同標籤。
- 聽到新的不同聲音時，依序新增下一個標籤；不要把不同人的發言合併成同一位。
- 若一小段無法判斷是誰，但可辨識內容，標示為 **[發言者不明]**：；不要為了填滿而硬分派。
- 若多人同時說話，標示為 **[多人重疊]**：並盡量轉寫可辨識內容。
""".strip()


DOMAIN_TERMINOLOGY_POLICY = """
【久方醫材研發術語表】
- 「佳世達」為正確名稱，英文可標為 Qisda；請勿寫成「加斯達」、「嘉士達」或 Jasta。
- IEC 62304 為醫療器材軟體生命週期流程標準；請勿寫成 IEC 6304 或 IC6304。
- 研發、製造、品保討論中常見「治具、放電治具、自製治具、品保、品管、機械老化、頻率/振幅、內徑固定塊」；請勿寫成「字句、自句、平保、平寶、氣械、政府」等語音誤聽。
- ISO 13485、FDA eSTAR、URA、SRS、SDS、SAD、SIS、traceability matrix、DHF、DMR、P4/P5/P6、Q0/Q4 請保留原文或常用縮寫。
- 久方生技 / Maxima Biotech 的研發會議若提及供應商、法規、設計階段、驗證報告與送件時程，摘要與待辦需保留日期、負責人與風險。
""".strip()


MEDICAL_DEVICE_RND_ANALYSIS_POLICY = """
【醫材研發會議判讀規則】
- 討論摘要需依「專案/議題」分組，每點包含目前狀態、卡點/風險、下一步/期限；FDA、IEC、ISO、QMS、設計移轉、驗證與送件內容不可簡化成一般進度描述。
- 最終決議只放已確認的日期、做法、採用/不採用、責任分工或風險處置；追蹤目標、個人建議、教學說明與背景知識不得列為決議。
- 待辦事項只放可驗收行動；任務描述要能被完成與檢查，避免「處理文件」「撰寫軟體工程」等大包任務，應拆成 SRS、SDS、SAD、traceability matrix、驗證計畫、RA 法規導入單等具體輸出物。
- 若逐字稿出現系統提示、雜訊過濾、片段缺漏或聽不清，需在討論摘要第一段加入「逐字稿品質註記」，標示可能缺漏與需複核。
""".strip()


TERMINOLOGY_REPLACEMENTS = (
    ("加斯達", "佳世達"),
    ("嘉士達", "佳世達"),
    ("Jasta", "Qisda"),
    ("平保", "品保"),
    ("平寶", "品保"),
    ("平管", "品管"),
    ("氣械老化", "機械老化"),
    ("頻率政府", "頻率振幅"),
    ("內型固定塊", "內徑固定塊"),
    ("內心固定塊", "內徑固定塊"),
    ("IEC 6304", "IEC 62304"),
    ("IEC6304", "IEC 62304"),
    ("IC 6304", "IEC 62304"),
    ("IC6304", "IEC 62304"),
)


TERMINOLOGY_REGEX_REPLACEMENTS = (
    (r"(?<!文件)(?<!文字)(?<!條文)字句", "治具"),
    (r"自製具", "自製治具"),
    (r"自句", "治具"),
)


def normalize_domain_terms(text: str) -> str:
    if not text:
        return ""
    for source, target in TERMINOLOGY_REPLACEMENTS:
        text = text.replace(source, target)
    for pattern, target in TERMINOLOGY_REGEX_REPLACEMENTS:
        text = re.sub(pattern, target, text)
    return text


# =============================================================================
# 日誌設定區
# =============================================================================

def setup_logger() -> logging.Logger:
    """
    設定並回傳應用程式日誌記錄器。
    同時輸出至終端機（含顏色）與日誌檔案。

    Returns:
        logging.Logger: 已設定完成的 logger 實例
    """
    configure_utf8_logging(level=logging.INFO)
    logger = logging.getLogger("MeetingAssistant")
    logger.setLevel(logging.DEBUG)

    # 防止重複添加 handler
    if logger.handlers:
        return logger

    # 格式器
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 終端機 Handler（INFO 以上）
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger


logger = setup_logger()


# =============================================================================
# 核心功能函數區
# =============================================================================

def validate_audio_file(audio_path: Path) -> str:
    """
    驗證音檔是否存在且格式受支援。

    Args:
        audio_path: 音檔的 Path 物件

    Returns:
        str: 對應的 MIME type

    Raises:
        FileNotFoundError: 檔案不存在時
        ValueError: 檔案格式不受支援時
    """
    if not audio_path.exists():
        raise FileNotFoundError(f"找不到音檔：{audio_path}")

    if not audio_path.is_file():
        raise ValueError(f"指定路徑不是檔案：{audio_path}")

    suffix = audio_path.suffix.lower()
    if suffix not in SUPPORTED_AUDIO_FORMATS:
        supported = ", ".join(SUPPORTED_AUDIO_FORMATS.keys())
        raise ValueError(
            f"不支援的音檔格式：'{suffix}'。\n"
            f"支援的格式：{supported}"
        )

    mime_type = SUPPORTED_AUDIO_FORMATS[suffix]
    logger.info(f"✅ 音檔驗證通過：{audio_path.name} (MIME: {mime_type})")
    return mime_type


def upload_audio_file(client: genai.Client, audio_path: Path, mime_type: str) -> types.File:
    """
    將本地音檔上傳至 Gemini File API，並等待處理完成。

    Args:
        client: genai.Client 實例
        audio_path: 音檔的 Path 物件
        mime_type: 音檔的 MIME type 字串

    Returns:
        types.File: 已上傳並處理完成的 File 物件

    Raises:
        RuntimeError: 上傳失敗或處理逾時時
    """
    file_size_mb = audio_path.stat().st_size / (1024 * 1024)
    logger.info(f"📤 開始上傳音檔（{file_size_mb:.2f} MB）...")

    try:
        uploaded_file = client.files.upload(
            file=str(audio_path),
            config=types.UploadFileConfig(display_name=audio_path.name, mime_type=mime_type)
        )
    except Exception as e:
        raise RuntimeError(f"音檔上傳失敗：{e}") from e

    logger.info(f"   上傳成功，雲端 URI: {uploaded_file.uri}")
    logger.info("⏳ 等待 Gemini 處理音檔...")

    # 輪詢等待音檔處理完成
    elapsed = 0
    while not uploaded_file.state or uploaded_file.state.name == "PROCESSING":
        if elapsed >= MAX_UPLOAD_WAIT_SECONDS:
            raise RuntimeError(
                f"音檔處理逾時（超過 {MAX_UPLOAD_WAIT_SECONDS} 秒）"
            )
        time.sleep(POLLING_INTERVAL_SECONDS)
        elapsed += POLLING_INTERVAL_SECONDS

        # 重新取得最新狀態
        uploaded_file = client.files.get(name=uploaded_file.name)
        logger.info(f"   處理中... ({elapsed}/{MAX_UPLOAD_WAIT_SECONDS}s)")

    # 確認最終狀態
    if uploaded_file.state.name == "FAILED":
        raise RuntimeError(
            f"Gemini 音檔處理失敗（狀態：{uploaded_file.state.name}）"
        )

    logger.info(f"✅ 音檔處理完成（狀態：{uploaded_file.state.name}）")
    return uploaded_file


def build_meeting_prompt() -> str:
    """
    建構會議分析的核心 Prompt。
    使用明確的結構化指令，要求模型扮演「專業高階秘書」角色。

    Returns:
        str: 完整的 Prompt 字串
    """
    return f"""
# 角色設定
你是一位擁有 15 年經驗的國際企業專業高階秘書（Executive Secretary），
精通會議記錄、商業寫作與多語言溝通。你的任務是分析上方的音訊會議內容，
並生成一份格式完整、語意精確的專業會議記錄文件。

# 輸出要求
請嚴格按照以下四個區塊輸出，使用 **繁體中文**，並保持 Markdown 格式：

{MULTILINGUAL_TRANSCRIPT_POLICY}

{SPEAKER_DIFFERENTIATION_POLICY}

{DOMAIN_TERMINOLOGY_POLICY}

{MEDICAL_DEVICE_RND_ANALYSIS_POLICY}

---

## 📋 一、會議摘要 (Executive Summary)

請依專案或議題分組，整理本次會議的：
- 目前狀態
- 卡點/風險
- 下一步/期限

---

## ✅ 二、重要決議 (Key Decisions)

請條列式列出本次會議所達成的所有明確決議，格式如下：
- **[決議編號]** 決議內容（若有投票或共識，請標註）

若無明確決議，請標註「本次會議無正式決議」。
不要把追蹤目標、背景說明或教學內容列為決議。

---

## 📌 三、待辦事項 (Action Items)

請以表格呈現所有被提及的任務、負責人與期限：

| # | 任務描述 | 負責人 | 期限 | 優先級 |
|---|---------|--------|------|--------|
| 1 | [任務內容] | [姓名/部門] | [日期或「未定」] | 高/中/低 |

若音訊中未明確提及負責人或期限，請填入「未明確指定」或「未提及」。
若只能辨識到匿名發言者，負責人請保留匿名標籤，例如「發言者 A」，不要自行推測姓名。
若任務過大，請拆成可驗收的文件、測試、追蹤或會議安排項目。

---

## 📝 四、完整逐字稿 (Verbatim Transcript)

請提供完整、逐字的會議逐字稿，格式要求如下：
- **若能辨識不同聲音**：使用 `[發言者 A]`、`[發言者 B]` 等匿名標記，並讓同一聲音持續使用同一標籤
- **若音訊中明確聽到姓名或職稱**：才可使用 `[講者名稱/角色]：` 前綴
- **加上時間戳記**（若音訊長度允許，建議每隔 30-60 秒標記一次）：使用 `[00:00]` 格式
- 保留重要的語氣詞、停頓標記（如「嗯...」「這個...」），以確保逐字稿的真實性
- 若有專業術語、英文縮寫，請保留原文並在後方加上中文說明（如 `KPI（關鍵績效指標）`）

格式範例：
```
[00:00] [發言者 A]：好，那我們開始今天的會議...
[00:15] [發言者 B]：好的，我先報告一下上週的進度...
```

---

> ⚠️ **重要提示**：
> - 若音訊品質不佳導致部分內容無法辨識，請在對應位置標記 `[音訊不清晰]`
> - 逐字稿應盡量完整，不要省略或摘要化
> - 所有時間與日期請轉換為 `YYYY/MM/DD` 格式（若有提及）
"""


def generate_meeting_notes(
    client: genai.Client,
    uploaded_file: types.File,
    prompt: str
) -> str:
    """
    呼叫 Gemini API，以音檔與 Prompt 生成會議記錄。

    Args:
        client: genai.Client 實例
        uploaded_file: 已上傳的 File 物件
        prompt: 會議記錄生成的指令 Prompt

    Returns:
        str: Gemini 回傳的完整文字內容

    Raises:
        RuntimeError: API 呼叫失敗或回傳內容異常時
    """
    logger.info("🤖 正在呼叫 Gemini API 生成會議記錄...")
    logger.info(f"   使用模型：{GEMINI_MODEL}")

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[uploaded_file, prompt],
            config=types.GenerateContentConfig(
                temperature=0.2,        # 低溫度確保輸出穩定且精確
                top_p=0.95,
                max_output_tokens=66536  # 允許長篇逐字稿輸出
            )
        )
    except Exception as e:
        raise RuntimeError(f"Gemini API 呼叫失敗：{e}") from e

    # 安全性過濾檢查
    if not response.candidates:
        raise RuntimeError("API 未回傳有效內容（可能被安全過濾器攔截或發生未知錯誤）")

    # 提取文字內容
    candidate = response.candidates[0]
    if candidate.finish_reason.name != "STOP":
        raise RuntimeError(
            f"內容生成未正常完成（結束原因：{candidate.finish_reason.name}）"
        )

    content = normalize_domain_terms(response.text)

    if not content or not content.strip():
        raise RuntimeError("API 回傳內容為空")

    logger.info("✅ 會議記錄生成成功！")
    return content


def save_output_file(
    content: str,
    audio_filename: str,
    output_dir: Path
) -> Path:
    """
    將生成的會議記錄儲存為 Markdown 檔案。

    Args:
        content: 要儲存的文字內容
        audio_filename: 原始音檔的檔名（用於命名輸出檔）
        output_dir: 輸出目錄的 Path 物件

    Returns:
        Path: 儲存完成的檔案路徑

    Raises:
        IOError: 檔案寫入失敗時
    """
    # 確保輸出目錄存在
    output_dir.mkdir(parents=True, exist_ok=True)

    # 生成輸出檔名（使用原始音檔名 + 時間戳記）
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    audio_stem = Path(audio_filename).stem  # 取不含副檔名的檔名
    output_filename = f"meeting_notes_{audio_stem}_{timestamp}.md"
    output_path = output_dir / output_filename

    # 組合完整的 Markdown 文件（含 YAML Frontmatter）
    frontmatter = f"""---
title: 會議記錄 - {audio_stem}
date: {datetime.now().strftime("%Y/%m/%d %H:%M:%S")}
source_audio: {audio_filename}
generated_by: AI 語音會議助理 (Gemini {GEMINI_MODEL})
---

"""
    full_content = frontmatter + content

    try:
        output_path.write_text(full_content, encoding="utf-8")
    except IOError as e:
        raise IOError(f"檔案寫入失敗：{e}") from e

    logger.info(f"💾 會議記錄已儲存至：{output_path}")
    return output_path


def cleanup_remote_file(client: genai.Client, uploaded_file: types.File) -> None:
    """
    刪除 Gemini File API 上的暫存音檔，釋放雲端資源。

    Args:
        client: genai.Client 實例
        uploaded_file: 要刪除的 File 物件
    """
    try:
        client.files.delete(name=uploaded_file.name)
        logger.info(f"🗑️  已清除雲端暫存音檔：{uploaded_file.name}")
    except Exception as e:
        # 清理失敗不應中斷程式，只記錄警告
        logger.warning(f"⚠️  雲端音檔清理失敗（可能需要手動刪除）：{e}")


# =============================================================================
# 主程式入口
# =============================================================================

def parse_arguments() -> argparse.Namespace:
    """
    解析命令列參數。

    Returns:
        argparse.Namespace: 解析完成的參數物件
    """
    parser = argparse.ArgumentParser(
        description="AI 語音會議助理 - 將音訊自動轉換為結構化會議記錄",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用範例:
  python meeting_assistant.py --audio meeting.mp3
  python meeting_assistant.py --audio recording.wav --output ./reports
  python meeting_assistant.py --audio conf.m4a --output ./output --model gemini-3.1-flash-lite

支援的音檔格式: .mp3, .wav, .m4a, .aac, .ogg, .flac, .webm
        """
    )

    parser.add_argument(
        "--audio",
        type=str,
        required=True,
        help="本地音檔的路徑（必填）"
    )

    parser.add_argument(
        "--output",
        type=str,
        default="output",
        help="輸出目錄路徑（預設：./output）"
    )

    parser.add_argument(
        "--model",
        type=str,
        default=GEMINI_MODEL,
        help=f"使用的 Gemini 模型（預設：{GEMINI_MODEL}）"
    )

    return parser.parse_args()


def main() -> None:
    """
    主程式流程控制：
    1. 載入環境變數與 API Key
    2. 驗證音檔
    3. 上傳音檔至 Gemini
    4. 生成會議記錄
    5. 儲存輸出檔案
    6. 清理雲端資源
    """
    # -------------------------------------------------------------------------
    # 步驟 0：初始化設定
    # -------------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("  🎙️  AI 語音會議助理 v1.0.0")
    print("  Powered by Google Gemini API")
    print("=" * 60 + "\n")

    # 載入 .env 環境變數（從腳本所在目錄往上找）
    script_dir = Path(__file__).parent
    dotenv_path = script_dir / ".env"
    load_dotenv(dotenv_path=dotenv_path)

    # 取得 API Key
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logger.error(
            "❌ 找不到 GEMINI_API_KEY 環境變數。\n"
            "   請複製 .env.example 為 .env 並填入你的 API Key。\n"
            "   或執行：export GEMINI_API_KEY='your_key_here'"
        )
        sys.exit(1)

    # 解析命令列參數
    args = parse_arguments()
    audio_path = Path(args.audio).resolve()
    output_dir = Path(args.output)

    # -------------------------------------------------------------------------
    # 步驟 1：初始化 Gemini SDK
    # -------------------------------------------------------------------------
    try:
        client = genai.Client(api_key=api_key)
        global GEMINI_MODEL
        GEMINI_MODEL = args.model
        logger.info(f"🔧 Gemini SDK 初始化成功（模型：{args.model}）")
    except Exception as e:
        logger.error(f"❌ Gemini SDK 初始化失敗：{e}")
        sys.exit(1)

    # -------------------------------------------------------------------------
    # 步驟 2：驗證音檔
    # -------------------------------------------------------------------------
    try:
        mime_type = validate_audio_file(audio_path)
    except (FileNotFoundError, ValueError) as e:
        logger.error(f"❌ 音檔驗證失敗：{e}")
        sys.exit(1)

    # -------------------------------------------------------------------------
    # 步驟 3：上傳音檔（含資源清理保護）
    # -------------------------------------------------------------------------
    uploaded_file = None
    output_path = None

    try:
        uploaded_file = upload_audio_file(client, audio_path, mime_type)

        # ---------------------------------------------------------------------
        # 步驟 4：生成會議記錄
        # ---------------------------------------------------------------------
        prompt = build_meeting_prompt()
        meeting_notes = generate_meeting_notes(client, uploaded_file, prompt)

        # ---------------------------------------------------------------------
        # 步驟 5：儲存輸出檔案
        # ---------------------------------------------------------------------
        output_path = save_output_file(
            content=meeting_notes,
            audio_filename=audio_path.name,
            output_dir=output_dir
        )

    except (RuntimeError, IOError) as e:
        logger.error(f"❌ 執行過程發生錯誤：{e}")
        # 即使失敗也要清理雲端資源
        if uploaded_file is not None:
            logger.info("🔄 正在執行錯誤後資源清理...")
            cleanup_remote_file(client, uploaded_file)
        sys.exit(1)

    except KeyboardInterrupt:
        logger.warning("\n⚠️  使用者中斷執行")
        if uploaded_file is not None:
            logger.info("🔄 正在清理雲端資源...")
            cleanup_remote_file(client, uploaded_file)
        sys.exit(130)

    finally:
        # ---------------------------------------------------------------------
        # 步驟 6：清理雲端資源（正常流程）
        # ---------------------------------------------------------------------
        if uploaded_file is not None and output_path is not None:
            cleanup_remote_file(client, uploaded_file)

    # -------------------------------------------------------------------------
    # 完成！顯示結果摘要
    # -------------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("  ✅ 會議記錄生成完成！")
    print("=" * 60)
    print(f"  📄 輸出檔案：{output_path.resolve()}")
    print(f"  📦 檔案大小：{output_path.stat().st_size / 1024:.1f} KB")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
