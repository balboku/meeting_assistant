import os
import sys
import time
import platform
import threading
import webbrowser
import subprocess
import shutil
import socket
import secrets
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - direct use before dependencies are installed
    load_dotenv = None

from backend.ngrok_status import DEFAULT_NGROK_API_URL, get_ngrok_status


ROOT_DIR = Path(__file__).resolve().parent
if load_dotenv:
    load_dotenv(ROOT_DIR / ".env")

SERVER_HOST = os.getenv("MEETING_ASSISTANT_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("MEETING_ASSISTANT_PORT", "8001"))
LINE_WEBHOOK_PATH = "/line-webhook"
LINE_WEBHOOK_ENDPOINT_API = "https://api.line.me/v2/bot/channel/webhook/endpoint"
LINE_WEBHOOK_TEST_API = "https://api.line.me/v2/bot/channel/webhook/test"
NGROK_PID_FILE = ROOT_DIR / "logs" / "ngrok.pid"
NGROK_LOG_FILE = ROOT_DIR / "logs" / "ngrok.log"
PLACEHOLDER_APP_API_KEYS = {"change_me_to_a_long_random_value", "your_app_api_key_here"}


def _parse_posix_listener_pids(output):
    pids = []
    for line in output.splitlines():
        line = line.strip()
        if line.isdigit() and int(line) not in pids:
            pids.append(int(line))
    return pids


def _parse_windows_listener_pids(output, port):
    pids = []
    suffix = f":{port}"
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        local_address, state, pid = parts[1], parts[-2].upper(), parts[-1]
        if state == "LISTENING" and local_address.endswith(suffix) and pid.isdigit():
            pid_value = int(pid)
            if pid_value not in pids:
                pids.append(pid_value)
    return pids


def _listening_pids(port):
    if platform.system() == "Windows":
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            check=False,
        )
        return _parse_windows_listener_pids(result.stdout, port)

    result = subprocess.run(
        ["lsof", f"-tiTCP:{port}", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
        check=False,
    )
    return _parse_posix_listener_pids(result.stdout)


def _process_command(pid):
    if platform.system() == "Windows":
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter \"ProcessId = {pid}\").CommandLine",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip()

    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def _is_meeting_assistant_process(command):
    return "backend.main:app" in command


def _is_ngrok_process(command):
    command = command.lower()
    return "ngrok" in command and " http" in f" {command}"


def _terminate_pid(pid):
    if pid == os.getpid():
        return

    command = _process_command(pid)
    if command and not _is_meeting_assistant_process(command):
        print(f"⚠️  Port {SERVER_PORT} 被其他程式使用，略過 PID {pid}。")
        return

    print(f"🧹 偵測到舊的會議助理服務（PID {pid}），正在關閉...")
    if platform.system() == "Windows":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], check=False)
        return

    subprocess.run(["kill", "-TERM", str(pid)], check=False)
    time.sleep(0.5)
    still_running = subprocess.run(
        ["kill", "-0", str(pid)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if still_running.returncode == 0:
        subprocess.run(["kill", "-KILL", str(pid)], check=False)


def terminate_existing_server(port):
    """Stop an already-running Meeting Assistant server on the target port."""
    for pid in _listening_pids(port):
        _terminate_pid(pid)


def _env_flag(name, default=True):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def ensure_app_api_key() -> str:
    """Ensure remote browser sessions have a usable API key for this launch."""
    configured = os.getenv("APP_API_KEY", "").strip()
    if configured and configured not in PLACEHOLDER_APP_API_KEYS:
        return configured

    generated = secrets.token_urlsafe(24)
    os.environ["APP_API_KEY"] = generated
    print("🔐 未設定 APP_API_KEY，已產生本次啟動用的手機 / 遠端存取 key。")
    return generated


def local_lan_ip() -> Optional[str]:
    """Return the LAN IP other devices on the same Wi-Fi can usually reach."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return None


def mobile_history_url(host: str, port: int, api_key: str = "") -> str:
    url = f"http://{host}:{port}/history"
    if api_key:
        return f"{url}?api_key={quote(api_key, safe='')}"
    return url


def public_history_url(public_url: str, api_key: str = "") -> str:
    url = f"{public_url.rstrip('/')}/history"
    if api_key:
        return f"{url}?api_key={quote(api_key, safe='')}"
    return url


def print_access_urls():
    local_url = f"http://127.0.0.1:{SERVER_PORT}/history"
    print("\n==================================================")
    print("🌐 網頁入口")
    print("==================================================")
    print(f"本機瀏覽器：{local_url}")

    lan_ip = local_lan_ip()
    if not lan_ip:
        print("手機 / 平板：無法自動判斷本機 Wi-Fi IP，請確認 Mac 與手機在同一個網路。")
        return

    if _env_flag("MEETING_ASSISTANT_TRUST_LOCAL_NETWORK", default=True):
        print(f"手機 / 平板：{mobile_history_url(lan_ip, SERVER_PORT)}")
        print("同 Wi-Fi / 信任本機網段可直接開啟；ngrok 公開網址仍會使用 api_key。")
    else:
        api_key = os.getenv("APP_API_KEY", "").strip()
        print(f"手機 / 平板：{mobile_history_url(lan_ip, SERVER_PORT, api_key)}")
        print("目前已停用信任本機網段，手機網址需帶 api_key。")


def _line_api_headers():
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not token:
        return None
    return {"Authorization": f"Bearer {token}"}


def configured_line_webhook_endpoint(silent=False) -> Optional[str]:
    """Read the current LINE webhook endpoint so ngrok can reuse its domain."""
    headers = _line_api_headers()
    if not headers:
        return None

    try:
        response = requests.get(LINE_WEBHOOK_ENDPOINT_API, headers=headers, timeout=8)
        response.raise_for_status()
        endpoint = (response.json().get("endpoint") or "").strip()
        return endpoint or None
    except requests.RequestException as exc:
        if not silent:
            print(f"⚠️  無法讀取 LINE Webhook Endpoint：{exc}")
    except ValueError as exc:
        if not silent:
            print(f"⚠️  LINE Webhook Endpoint 回傳格式無法解析：{exc}")
    return None


def _public_url_from_endpoint(endpoint: str) -> str:
    endpoint = endpoint.strip().rstrip("/")
    if endpoint.endswith(LINE_WEBHOOK_PATH):
        return endpoint[: -len(LINE_WEBHOOK_PATH)].rstrip("/")
    return endpoint


def resolve_ngrok_public_url() -> Optional[str]:
    """Prefer an explicit static ngrok URL, otherwise reuse LINE's configured endpoint."""
    explicit = (os.getenv("MEETING_ASSISTANT_NGROK_URL") or os.getenv("NGROK_URL") or "").strip()
    if explicit:
        return _public_url_from_endpoint(explicit)

    endpoint = configured_line_webhook_endpoint()
    if endpoint and endpoint.startswith("https://"):
        return _public_url_from_endpoint(endpoint)

    return None


def _terminate_process_pid(pid):
    if pid == os.getpid():
        return

    if platform.system() == "Windows":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], check=False)
        return

    subprocess.run(["kill", "-TERM", str(pid)], check=False)
    time.sleep(0.5)
    still_running = subprocess.run(
        ["kill", "-0", str(pid)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if still_running.returncode == 0:
        subprocess.run(["kill", "-KILL", str(pid)], check=False)


def terminate_existing_ngrok():
    """Stop the ngrok process previously started by this script."""
    if not NGROK_PID_FILE.exists():
        return

    try:
        pid = int(NGROK_PID_FILE.read_text(encoding="utf-8").strip())
    except ValueError:
        NGROK_PID_FILE.unlink(missing_ok=True)
        return

    command = _process_command(pid)
    if command and not _is_ngrok_process(command):
        print(f"⚠️  logs/ngrok.pid 指向非 ngrok 程式，略過 PID {pid}。")
        return

    print(f"🧹 偵測到舊的 ngrok tunnel（PID {pid}），正在關閉...")
    _terminate_process_pid(pid)
    NGROK_PID_FILE.unlink(missing_ok=True)


def wait_for_ngrok_status(port, timeout_seconds=10):
    deadline = time.monotonic() + timeout_seconds
    status = None
    api_url = os.getenv("MEETING_ASSISTANT_NGROK_API_URL", DEFAULT_NGROK_API_URL)
    while time.monotonic() <= deadline:
        status = get_ngrok_status(expected_port=port, api_url=api_url)
        if status.get("running"):
            return status
        time.sleep(0.5)
    return status or {
        "running": False,
        "public_url": None,
        "webhook_url": None,
        "message": "ngrok 尚未回報 tunnel 狀態",
    }


def start_ngrok_tunnel(port, wait_for_status=True):
    """Start ngrok for the local backend if it is available and enabled."""
    if not _env_flag("MEETING_ASSISTANT_NGROK", default=True):
        print("ℹ️  MEETING_ASSISTANT_NGROK=0，略過 ngrok 自動啟動。")
        return None

    if not shutil.which("ngrok"):
        print("⚠️  找不到 ngrok 指令；LINE Webhook 需要公開 HTTPS，請先安裝 ngrok。")
        return None

    terminate_existing_ngrok()

    public_url = resolve_ngrok_public_url()
    command = ["ngrok", "http", str(port)]
    if public_url:
        command.append(f"--url={public_url}")
    command.extend(["--log=stdout", "--log-format=logfmt"])

    NGROK_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    popen_kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": None,
        "stderr": subprocess.STDOUT,
    }
    if platform.system() == "Windows" and hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    try:
        with NGROK_LOG_FILE.open("ab") as log_file:
            popen_kwargs["stdout"] = log_file
            process = subprocess.Popen(command, **popen_kwargs)
    except OSError as exc:
        print(f"⚠️  ngrok 啟動失敗：{exc}")
        return None

    NGROK_PID_FILE.write_text(str(process.pid), encoding="utf-8")
    print(f"🌐 已啟動 ngrok（PID {process.pid}），log：{NGROK_LOG_FILE}")

    if not public_url:
        print("⚠️  目前未設定固定 ngrok URL；請把 ngrok 產生的 /line-webhook URL 更新到 LINE Developers Console。")

    if wait_for_status:
        status = wait_for_ngrok_status(port)
        if status.get("running"):
            print(f"✅ ngrok 已連線：{status.get('webhook_url')}")
        else:
            print(f"⚠️  ngrok 尚未就緒：{status.get('message')}")

    return process


def stop_started_ngrok(process):
    if process is None:
        return

    if process.poll() is None:
        print("🧹 正在關閉本次啟動的 ngrok tunnel...")
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    if NGROK_PID_FILE.exists() and NGROK_PID_FILE.read_text(encoding="utf-8").strip() == str(process.pid):
        NGROK_PID_FILE.unlink(missing_ok=True)


def _local_server_ready(port, timeout_seconds=20):
    deadline = time.monotonic() + timeout_seconds
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() <= deadline:
        try:
            response = requests.get(url, timeout=2)
            if response.status_code < 500:
                return True
        except requests.RequestException:
            pass
        time.sleep(1)
    return False


def report_line_webhook_status(port):
    """Print LINE/ngrok status after uvicorn has had time to start."""
    if not _env_flag("MEETING_ASSISTANT_NGROK", default=True):
        return

    _local_server_ready(port)
    status = wait_for_ngrok_status(port, timeout_seconds=5)
    print("\n==================================================")
    print("🔎 LINE/ngrok 狀態")
    print("==================================================")
    print(f"ngrok：{status.get('message')}")
    if status.get("webhook_url"):
        print(f"Webhook URL：{status['webhook_url']}")
    if status.get("public_url") and os.getenv("APP_API_KEY", "").strip():
        print(
            "手機 / ngrok 網頁："
            f"{public_history_url(status['public_url'], os.getenv('APP_API_KEY', '').strip())}"
        )

    headers = _line_api_headers()
    if not headers:
        print("LINE：未設定 LINE_CHANNEL_ACCESS_TOKEN，略過 LINE webhook test。")
        return

    endpoint = configured_line_webhook_endpoint(silent=True)
    if endpoint:
        print(f"LINE Console endpoint：{endpoint}")

    try:
        response = requests.post(LINE_WEBHOOK_TEST_API, headers=headers, timeout=10)
        payload = response.json()
        success = payload.get("success")
        reason = payload.get("reason") or payload.get("detail") or response.text
        if response.status_code == 200 and success:
            print("LINE webhook test：✅ 成功")
        else:
            print(f"LINE webhook test：⚠️  未通過（HTTP {response.status_code}，{reason}）")
    except (requests.RequestException, ValueError) as exc:
        print(f"LINE webhook test：⚠️  無法執行（{exc}）")


def open_browser():
    """等待兩秒後自動開啟瀏覽器"""
    time.sleep(2)
    url = f"http://127.0.0.1:{SERVER_PORT}/history"
    print(f"\n🌐 正在開啟瀏覽器前往網頁介面: {url}\n")
    webbrowser.open(url)

if __name__ == "__main__":
    print("==================================================")
    print("🚀 啟動 AI 語音會議助理...")
    print("==================================================")

    ensure_app_api_key()
    terminate_existing_server(SERVER_PORT)
    ngrok_process = start_ngrok_tunnel(SERVER_PORT)
    print_access_urls()

    # 啟動執行緒準備開啟瀏覽器
    threading.Thread(target=open_browser, daemon=True).start()
    threading.Thread(target=report_line_webhook_status, args=(SERVER_PORT,), daemon=True).start()

    # 在前景啟動 FastAPI 伺服器
    try:
        subprocess.run([
            sys.executable,
            "-m",
            "uvicorn",
            "backend.main:app",
            "--host",
            SERVER_HOST,
            "--port",
            str(SERVER_PORT),
        ], check=False)
    except KeyboardInterrupt:
        print("\n伺服器已關閉。")
    finally:
        stop_started_ngrok(ngrok_process)
