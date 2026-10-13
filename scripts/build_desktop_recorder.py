"""Build a Windows desktop download without credentials, models or meeting data."""
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--windowed", "--onedir",
                    "--name", "MeetingRecorder", "--paths", str(ROOT), "--specpath", str(ROOT / "build"),
                    "--collect-data", "soundcard", "--hidden-import", "soundcard.mediafoundation",
                    "--collect-all", "_soundfile_data", "--copy-metadata", "soundcard", "--copy-metadata", "numpy",
                    "--copy-metadata", "soundfile", "--copy-metadata", "cffi", "--copy-metadata", "audioop-lts",
                    "--copy-metadata", "pyinstaller", str(ROOT / "gui" / "desktop_recorder.py")], cwd=ROOT, check=True)
    destination = ROOT / "dist" / "MeetingRecorder"
    shutil.copyfile(ROOT / "docs" / "desktop-recorder.txt", destination / "README.txt")
    license_path = Path(sys.base_prefix) / "LICENSE.txt"
    if license_path.is_file():
        shutil.copyfile(license_path, destination / "Python-LICENSE.txt")
    shutil.make_archive(str(ROOT / "dist" / "MeetingRecorder-Windows"), "zip", ROOT / "dist", "MeetingRecorder")
    print("Desktop download ready in dist/MeetingRecorder-Windows.zip")


if __name__ == "__main__":
    main()
