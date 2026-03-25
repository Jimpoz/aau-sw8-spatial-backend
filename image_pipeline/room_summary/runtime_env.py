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

    matplotlib_dir.mkdir(parents=True, exist_ok=True)
    ultralytics_dir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("MPLCONFIGDIR", str(matplotlib_dir))
    os.environ.setdefault("YOLO_CONFIG_DIR", str(ultralytics_dir))
