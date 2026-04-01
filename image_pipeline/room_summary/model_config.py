from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ModelProfile:
    name: str
    model_path: Path
    class_config_path: Path | None
    confidence_threshold: float


@dataclass(frozen=True, slots=True)
class ModelSelection:
    profile_name: str | None
    model_path: Path
    class_config_path: Path | None
    confidence_threshold: float


def _resolve_profile_name(
    profiles: dict[str, ModelProfile],
    active_profile: str | None,
    requested_profile: str | None,
) -> str | None:
    selected_profile_name = (requested_profile or active_profile or "").strip() or None
    if selected_profile_name is None:
        return None

    if selected_profile_name not in profiles:
        available_profiles = ", ".join(sorted(profiles)) or "none"
        raise ValueError(
            f"Unknown model profile '{selected_profile_name}'. "
            f"Available profiles: {available_profiles}."
        )

    return selected_profile_name


def resolve_model_selection(
    config_path: str | Path | None,
    requested_profile: str | None,
    fallback_model_path: str | Path | None,
    fallback_confidence_threshold: float,
) -> ModelSelection:
    profiles, active_profile = load_model_profiles(config_path)
    selected_profile_name = _resolve_profile_name(
        profiles=profiles,
        active_profile=active_profile,
        requested_profile=requested_profile,
    )

    if selected_profile_name is not None:
        selected_profile = profiles[selected_profile_name]
        return ModelSelection(
            profile_name=selected_profile.name,
            model_path=selected_profile.model_path,
            class_config_path=selected_profile.class_config_path,
            confidence_threshold=selected_profile.confidence_threshold,
        )

    if fallback_model_path is None:
        raise FileNotFoundError("No YOLO model path or model profile could be resolved.")

    return ModelSelection(
        profile_name=None,
        model_path=Path(fallback_model_path),
        class_config_path=None,
        confidence_threshold=fallback_confidence_threshold,
    )


def load_model_profiles(
    config_path: str | Path | None,
) -> tuple[dict[str, ModelProfile], str | None]:
    if config_path is None:
        return {}, None

    resolved_config_path = Path(config_path)
    if not resolved_config_path.exists():
        return {}, None

    parser = ConfigParser()
    parser.read(resolved_config_path, encoding="utf-8")

    profiles: dict[str, ModelProfile] = {}
    for section_name in parser.sections():
        if section_name.lower() == "selection":
            continue

        model_path = parser.get(section_name, "path", fallback="").strip()
        if not model_path:
            continue

        resolved_path = Path(model_path)
        if not resolved_path.is_absolute():
            resolved_path = (resolved_config_path.parent / resolved_path).resolve()

        class_config_value = parser.get(section_name, "classes", fallback="").strip()
        class_config_path: Path | None = None
        if class_config_value:
            class_config_path = Path(class_config_value)
            if not class_config_path.is_absolute():
                class_config_path = (
                    resolved_config_path.parent / class_config_path
                ).resolve()

        confidence_threshold = parser.getfloat(
            section_name,
            "confidence",
            fallback=0.25,
        )
        profiles[section_name] = ModelProfile(
            name=section_name,
            model_path=resolved_path,
            class_config_path=class_config_path,
            confidence_threshold=confidence_threshold,
        )

    active_profile = parser.get("selection", "active", fallback="").strip() or None
    return profiles, active_profile


def resolve_config_profile(
    config_path: str | Path,
    requested_profile: str | None = None,
) -> ModelProfile:
    profiles, active_profile = load_model_profiles(config_path)
    selected_profile_name = _resolve_profile_name(
        profiles=profiles,
        active_profile=active_profile,
        requested_profile=requested_profile,
    )

    if selected_profile_name is None:
        raise ValueError(
            "No model profile selected in the config. Set [selection] active = <profile>."
        )

    return profiles[selected_profile_name]
