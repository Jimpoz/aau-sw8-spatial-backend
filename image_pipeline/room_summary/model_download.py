import os
from pathlib import Path

from .runtime_env import configure_runtime_env

configure_runtime_env()

from ultralytics import YOLO


YOLO11_DETECTION_ASSETS = (
    "yolo11n.pt",
    "yolo11s.pt",
    "yolo11m.pt",
    "yolo11l.pt",
    "yolo11x.pt",
)


def download_yolo11_detection_model(
    model_name: str,
    output_dir: str | Path,
) -> Path:
    if model_name not in YOLO11_DETECTION_ASSETS:
        available = ", ".join(YOLO11_DETECTION_ASSETS)
        raise ValueError(
            f"Unsupported YOLO11 detection model '{model_name}'. Available models: {available}."
        )

    output_directory = Path(output_dir)
    output_directory.mkdir(parents=True, exist_ok=True)
    output_path = output_directory / model_name

    if output_path.exists():
        return output_path

    original_cwd = Path.cwd()
    try:
        os.chdir(output_directory)
        YOLO(model_name)
    except Exception as exc:
        raise FileNotFoundError(
            f"Automatic download of {model_name} failed: {exc}"
        ) from exc
    finally:
        os.chdir(original_cwd)

    if not output_path.exists():
        raise FileNotFoundError(
            f"Automatic download completed without creating the expected file: {output_path}"
        )

    return output_path


def ensure_model_path(model_path: str | Path) -> Path:
    resolved_path = Path(model_path)
    if resolved_path.exists():
        return resolved_path

    if resolved_path.name in YOLO11_DETECTION_ASSETS:
        return download_yolo11_detection_model(
            model_name=resolved_path.name,
            output_dir=resolved_path.parent,
        )

    return resolved_path
