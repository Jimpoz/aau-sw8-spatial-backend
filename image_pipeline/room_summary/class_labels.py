from pathlib import Path


def load_class_labels(config_path: Path | None) -> dict[int, str]:
    if config_path is None or not config_path.exists():
        return {}

    overrides: dict[int, str] = {}
    next_index = 0

    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", maxsplit=1)[0].strip()
        if not line:
            continue

        for separator in ("=", ":"):
            if separator in line:
                left, right = line.split(separator, maxsplit=1)
                if left.strip().isdigit():
                    class_index = int(left.strip())
                    overrides[class_index] = right.strip()
                    next_index = max(next_index, class_index + 1)
                    break
        else:
            while next_index in overrides:
                next_index += 1
            overrides[next_index] = line
            next_index += 1

    return overrides


def resolve_class_label(
    model_names: object,
    class_id: int,
    overrides: dict[int, str],
) -> str:
    if class_id in overrides:
        return overrides[class_id]
    if isinstance(model_names, dict):
        return str(model_names.get(class_id, class_id))
    if isinstance(model_names, list) and 0 <= class_id < len(model_names):
        return str(model_names[class_id])
    return str(class_id)
