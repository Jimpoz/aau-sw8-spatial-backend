import os
from pathlib import Path


def configure_runtime_env() -> None:
    cache_root = Path(
        os.getenv("ROOM_SUMMARY_CACHE_DIR", str(Path(".cache").resolve()))
    )
    cache_root.mkdir(parents=True, exist_ok=True)

    matplotlib_dir = Path(
        os.getenv("MPLCONFIGDIR", str((cache_root / "matplotlib").resolve()))
    )
    ultralytics_dir = Path(
        os.getenv("YOLO_CONFIG_DIR", str((cache_root / "ultralytics").resolve()))
    )
    torch_dir = Path(
        os.getenv("TORCH_HOME", str((cache_root / "torch").resolve()))
    )
    hf_dir = Path(
        os.getenv("HF_HOME", str((cache_root / "huggingface").resolve()))
    )
    clip_dir = Path(
        os.getenv("OPEN_CLIP_CACHE", str((cache_root / "open_clip").resolve()))
    )

    matplotlib_dir.mkdir(parents=True, exist_ok=True)
    ultralytics_dir.mkdir(parents=True, exist_ok=True)
    torch_dir.mkdir(parents=True, exist_ok=True)
    hf_dir.mkdir(parents=True, exist_ok=True)
    clip_dir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("MPLCONFIGDIR", str(matplotlib_dir))
    os.environ.setdefault("YOLO_CONFIG_DIR", str(ultralytics_dir))
    os.environ.setdefault("TORCH_HOME", str(torch_dir))
    os.environ.setdefault("HF_HOME", str(hf_dir))
    os.environ.setdefault("OPEN_CLIP_CACHE", str(clip_dir))
