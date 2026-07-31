import os
import sys
import time
import platform
import threading
import webbrowser
import subprocess
import socket
import secrets
from pathlib import Path
from typing import Optional
from urllib.parse import quote

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - direct use before dependencies are installed
    load_dotenv = None

from backend.access_tokens import create_access_token


def configure_stdio_encoding() -> None:
    """Keep Windows redirected startup logs from crashing on Unicode output."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if not stream or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


configure_stdio_encoding()


ROOT_DIR = Path(__file__).resolve().parent
if load_dotenv:
    load_dotenv(ROOT_DIR / ".env")

SERVER_HOST = os.getenv("MEETING_ASSISTANT_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("MEETING_ASSISTANT_PORT", "8001"))
SUPERVISOR_TOKEN_FILE = ROOT_DIR / "logs" / "supervisor.token"
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


def _run_taskkill(pid):
    return subprocess.run(
        ["taskkill", "/PID", str(pid), "/F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _terminate_pid(pid):
    if pid == os.getpid():
        return

    command = _process_command(pid)
    if command and not _is_meeting_assistant_process(command):
        print(f"⚠️  Port {SERVER_PORT} 被其他程式使用，略過 PID {pid}。")
        return

    print(f"🧹 偵測到舊的會議助理服務（PID {pid}），正在關閉...")
    if platform.system() == "Windows":
        _run_taskkill(pid)
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


def _env_int(name, default, minimum=0, maximum=86400):
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = int(default)
    return min(max(value, int(minimum)), int(maximum))


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
        bootstrap_token = create_access_token(
            api_key,
            "bootstrap",
            ttl_seconds=5 * 60,
        )
        return f"{url}?bootstrap_token={quote(bootstrap_token, safe='')}"
    return url


def print_access_urls():
    local_url = f"http://127.0.0.1:{SERVER_PORT}/history"
    print("\n==================================================")
    print("🌐 網頁入口")
    print("==================================================")
    print(f"本機瀏覽器：{local_url}")

    lan_host = (
        os.getenv("MEETING_ASSISTANT_SHARE_HOST", "").strip()
        or local_lan_ip()
    )
    if not lan_host:
        print("手機 / 平板：無法自動判斷本機 Wi-Fi IP，請確認 Mac 與手機在同一個網路。")
        return

    if _env_flag("MEETING_ASSISTANT_TRUST_LOCAL_NETWORK", default=False):
        print(f"手機 / 平板：{mobile_history_url(lan_host, SERVER_PORT)}")
        print("同 Wi-Fi / 信任本機網段可直接開啟。")
    else:
        api_key = os.getenv("APP_API_KEY", "").strip()
        print(f"手機 / 平板：{mobile_history_url(lan_host, SERVER_PORT, api_key)}")
        print("目前已停用信任本機網段，手機網址使用五分鐘有效的登入連結。")


def open_browser():
    """等待兩秒後自動開啟瀏覽器"""
    time.sleep(2)
    url = f"http://127.0.0.1:{SERVER_PORT}/history"
    print(f"\n🌐 正在開啟瀏覽器前往網頁介面: {url}\n")
    webbrowser.open(url)


def run_server_supervisor(
    command,
    *,
    owns_supervisor=lambda: True,
    process_factory=subprocess.Popen,
    sleep=time.sleep,
):
    """Run Uvicorn with bounded restart/backoff while this launch owns the token."""
    auto_restart = _env_flag("MEETING_ASSISTANT_AUTO_RESTART", default=True)
    max_restarts = _env_int(
        "MEETING_ASSISTANT_MAX_RESTARTS",
        5,
        minimum=0,
        maximum=100,
    )
    base_delay = _env_int(
        "MEETING_ASSISTANT_RESTART_DELAY_SECONDS",
        2,
        minimum=1,
        maximum=60,
    )
    stable_seconds = _env_int(
        "MEETING_ASSISTANT_RESTART_RESET_SECONDS",
        300,
        minimum=10,
        maximum=86400,
    )
    restart_count = 0
    while owns_supervisor():
        started_at = time.monotonic()
        process = process_factory(command)
        try:
            while True:
                polled = process.poll()
                if polled is not None:
                    return_code = int(polled)
                    break
                if not owns_supervisor():
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    return 0
                sleep(1)
        except KeyboardInterrupt:
            if process.poll() is None:
                process.terminate()
            raise
        runtime_seconds = time.monotonic() - started_at
        if return_code == 0 or not auto_restart or not owns_supervisor():
            return return_code
        if runtime_seconds >= stable_seconds:
            restart_count = 0
        if restart_count >= max_restarts:
            print(f"❌ Uvicorn 已連續異常結束 {restart_count + 1} 次，停止自動重啟。")
            return return_code
        delay = min(60, base_delay * (2 ** restart_count))
        restart_count += 1
        print(
            f"⚠️  Uvicorn 異常結束（code={return_code}），"
            f"{delay} 秒後進行第 {restart_count}/{max_restarts} 次重啟。"
        )
        sleep(delay)
    return 0


if __name__ == "__main__":
    print("==================================================")
    print("🚀 啟動 AI 語音會議助理...")
    print("==================================================")

    ensure_app_api_key()
    SUPERVISOR_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    supervisor_token = secrets.token_hex(16)
    SUPERVISOR_TOKEN_FILE.write_text(supervisor_token, encoding="ascii")

    def owns_supervisor_token():
        try:
            return (
                SUPERVISOR_TOKEN_FILE.read_text(encoding="ascii").strip()
                == supervisor_token
            )
        except OSError:
            return False

    terminate_existing_server(SERVER_PORT)
    print_access_urls()

    # 互動啟動預設開啟瀏覽器；排程器/服務可設 0 避免桌面干擾。
    if _env_flag("MEETING_ASSISTANT_OPEN_BROWSER", default=True):
        threading.Thread(target=open_browser, daemon=True).start()

    # 在前景啟動 FastAPI 伺服器
    try:
        run_server_supervisor([
            sys.executable,
            "-m",
            "uvicorn",
            "backend.main:app",
            "--host",
            SERVER_HOST,
            "--port",
            str(SERVER_PORT),
        ], owns_supervisor=owns_supervisor_token)
    except KeyboardInterrupt:
        print("\n伺服器已關閉。")
    finally:
        if owns_supervisor_token():
            SUPERVISOR_TOKEN_FILE.unlink(missing_ok=True)
