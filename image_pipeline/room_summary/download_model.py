import argparse
from pathlib import Path

from .model_config import resolve_config_profile
from .model_download import (
    YOLO11_DETECTION_ASSETS,
    download_yolo11_detection_model,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download an official Ultralytics YOLO11 detection model."
    )
    parser.add_argument(
        "--config",
        default="modelConfig.cfg",
        help="Model config file used to resolve the selected profile.",
    )
    parser.add_argument(
        "--profile",
        help="Optional profile name from the config. Defaults to [selection] active.",
    )
    parser.add_argument(
        "--model",
        choices=YOLO11_DETECTION_ASSETS,
        help="Optional direct YOLO11 detection asset override.",
    )
    parser.add_argument(
        "--output-dir",
        help="Optional directory override for direct --model downloads.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.model:
        output_dir = Path(args.output_dir or "models")
        model_path = download_yolo11_detection_model(
            model_name=args.model,
            output_dir=output_dir,
        )
        print(model_path)
        return

    selected_profile = resolve_config_profile(
        config_path=Path(args.config),
        requested_profile=args.profile,
    )
    if selected_profile.model_path.name not in YOLO11_DETECTION_ASSETS:
        available_models = ", ".join(YOLO11_DETECTION_ASSETS)
        raise ValueError(
            f"Profile '{selected_profile.name}' points to unsupported model asset "
            f"'{selected_profile.model_path.name}'. Supported YOLO11 assets: {available_models}."
        )

    model_path = download_yolo11_detection_model(
        model_name=selected_profile.model_path.name,
        output_dir=selected_profile.model_path.parent,
    )
    print(model_path)


if __name__ == "__main__":
    main()
