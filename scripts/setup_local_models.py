"""Explicit administrator install of public CPU models; never runs during requests."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
ASSETS = {
    "segmentation": BASE + "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
    "embedding": BASE + "speaker-recongition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx",
    "whisper": BASE + "asr-models/sherpa-onnx-whisper-base.tar.bz2",
}
SHA256 = {
    "segmentation": "24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488",
    "embedding": "1a331345f04805badbb495c775a6ddffcdd1a732567d5ec8b3d5749e3c7a5e4b",
    "whisper": "911b2083efd7c0dca2ac3b358b75222660dc09fb716d64fbfc417ba6c99ff3de",
}
SELECT = {"segmentation": {"model.onnx": "segmentation.onnx", "LICENSE": "segmentation-LICENSE"},
          "whisper": {"base-encoder.int8.onnx": "whisper-encoder.onnx",
                      "base-decoder.int8.onnx": "whisper-decoder.onnx", "base-tokens.txt": "whisper-tokens.txt"}}
LICENSES = {
    "whisper-LICENSE": ("https://raw.githubusercontent.com/openai/whisper/main/LICENSE",
                        "b5d65a59060e68c4ff940e1eddfa6f94b2d68fdf58ed7f4dd57721c997e35e9d"),
    "embedding-LICENSE": ("https://raw.githubusercontent.com/modelscope/3D-Speaker/main/LICENSE",
                          "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"),
}


def install_licenses(directory):
    for name, (url, digest) in LICENSES.items():
        target = Path(directory) / name
        data = target.read_bytes() if target.exists() else urllib.request.urlopen(url, timeout=30).read()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError(f"License checksum mismatch: {name}")
        if not target.exists():
            with target.open("xb") as stream:
                stream.write(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(__file__).resolve().parents[1] / "models")
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for name, url in ASSETS.items():
        print(f"Downloading {name} from official sherpa-onnx release...", flush=True)
        with tempfile.TemporaryDirectory(prefix="meeting-model-") as folder:
            archive = Path(folder) / "download"
            urllib.request.urlretrieve(url, archive)
            with archive.open("rb") as stream:
                manifest[name] = {"url": url, "sha256": hashlib.file_digest(stream, "sha256").hexdigest()}
            if manifest[name]["sha256"] != SHA256[name]:
                raise ValueError(f"Checksum mismatch for {name}; refusing installation")
            if name == "embedding":
                target = args.directory / "embedding.onnx"
                if target.exists():
                    raise FileExistsError(f"Refusing to overwrite {target}")
                shutil.copyfile(archive, target)
            else:
                found = set()
                with tarfile.open(archive) as bundle:
                    for member in bundle.getmembers():
                        base = Path(member.name).name
                        if member.isfile() and base in SELECT[name]:
                            target = args.directory / SELECT[name][base]
                            with bundle.extractfile(member) as source, target.open("xb") as destination:
                                shutil.copyfileobj(source, destination)
                            found.add(base)
                if found != set(SELECT[name]):
                    raise ValueError(f"Missing expected {name} files: {set(SELECT[name]) - found}")
    install_licenses(args.directory)
    (args.directory / "installed-models.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("Local models installed. Keep bundled licenses with distributed copies.")


if __name__ == "__main__":
    main()
