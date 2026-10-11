"""Effective settings: defaults, then `~/.agent-loop/.env`, the repository profile, CLI flags.

Variables already set in the process environment override the same keys from the `.env` file.
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
import typing
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from pathlib import Path

DEFAULT_HOME = "~/.agent-loop"
PROFILE_NAME = "loop.toml"
DOC_LANGS = ("ru", "en")
EFFORTS = ("low", "medium", "high", "xhigh", "max")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    doc_lang: str = "en"
    model_planner: str = "opus"
    model_implementer: str = "opus"
    model_critic: str = "opus"
    effort_implementer: str = "high"
    effort_critic: str = "max"
    knowledge_dir: Path | None = None
    context_file: Path | None = None
    ctx_soft_tokens: int = 120_000
    ctx_hard_tokens: int = 150_000
    reserve_5h: float = 0.10
    reserve_7d: float = 0.05
    start_allowance_5h: float = 0.2
    step_margin: float = 0.03
    diff_soft_lines: int = 1000
    diff_hard_lines: int = 1500
    fix_attempts: int = 3
    review_every: int = 2
    review_rounds: int = 2
    plan_critic_rounds: int = 2
    step_timeout_min: int = 60


@dataclass(frozen=True)
class Config:
    home: Path
    repo_root: Path | None
    profile: Path | None
    settings: Settings
    sources: dict[str, str]


_FIELD_KINDS = {
    name: Path if Path in typing.get_args(hint) else hint
    for name, hint in typing.get_type_hints(Settings).items()
}
_INLINE_COMMENT = re.compile(r"\s+#.*$")


def env_key(name: str) -> str:
    return f"LOOP_{name.upper()}"


def parse_env_file(path: Path) -> dict[str, str]:
    values = {}
    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ")
        key, sep, value = line.partition("=")
        if not sep:
            raise ConfigError(f"{path}:{lineno}: expected KEY=VALUE")
        value = value.strip()
        if value[:1] in ("'", '"'):
            end = value.find(value[0], 1)
            if end == -1:
                raise ConfigError(f"{path}:{lineno}: unterminated quote")
            value = value[1:end]
        else:
            value = _INLINE_COMMENT.sub("", value)
        values[key.strip()] = value
    return values


def repo_root(cwd: Path) -> Path | None:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True
    )
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.strip())


def find_profile(
    explicit: Path | None, root: Path | None, knowledge_dir: Path | None
) -> Path | None:
    if explicit is not None:
        if not explicit.is_file():
            raise ConfigError(f"profile not found: {explicit}")
        return explicit
    if root is None:
        return None
    candidates = [root / PROFILE_NAME]
    if knowledge_dir is not None:
        candidates.append(knowledge_dir / root.name / PROFILE_NAME)
    found = [path for path in candidates if path.is_file()]
    if len(found) > 1:
        raise ConfigError(f"two profiles for one repository, keep one: {found[0]}, {found[1]}")
    return found[0] if found else None


def load(
    cwd: Path,
    flags: Mapping[str, object] | None = None,
    profile: Path | None = None,
    environ: Mapping[str, str] = os.environ,
) -> Config:
    home = Path(environ.get("LOOP_HOME", DEFAULT_HOME)).expanduser()
    values: dict[str, object] = {}
    sources: dict[str, str] = {}

    def apply(layer: Mapping[str, object], source: str, base_dir: Path) -> None:
        for name, raw in layer.items():
            values[name] = _coerce(name, raw, base_dir, source)
            sources[name] = source

    env_file = home / ".env"
    if env_file.is_file():
        apply(_env_layer(parse_env_file(env_file), env_file), str(env_file), env_file.parent)
    process_env = {
        name: environ[env_key(name)] for name in _FIELD_KINDS if env_key(name) in environ
    }
    apply(process_env, "environment", cwd)

    root = repo_root(cwd)
    profile_path = find_profile(profile, root, values.get("knowledge_dir"))
    if profile_path is not None:
        apply(_profile_layer(profile_path), str(profile_path), profile_path.parent)

    apply({name: value for name, value in (flags or {}).items() if value is not None}, "flag", cwd)

    settings = Settings(**values)
    _validate(settings)
    for name in _FIELD_KINDS:
        sources.setdefault(name, "default")
    return Config(home, root, profile_path, settings, sources)


def describe(config: Config) -> list[tuple[str, str, str]]:
    return [
        (name, "" if value is None else str(value), config.sources[name])
        for name, value in asdict(config.settings).items()
    ]


def _env_layer(raw: dict[str, str], path: Path) -> dict[str, str]:
    by_key = {env_key(name): name for name in _FIELD_KINDS}
    unknown = sorted(set(raw) - set(by_key))
    if unknown:
        raise ConfigError(f"{path}: unknown keys {', '.join(unknown)}")
    return {by_key[key]: value for key, value in raw.items()}


def _profile_layer(path: Path) -> dict[str, object]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if "knowledge_dir" in data:
        raise ConfigError(
            f"{path}: knowledge_dir is set only in .env, it is used to find the profile"
        )
    # Tables ([commands], [timeouts], [env]) belong to the repository profile, not to settings.
    return {name: value for name, value in data.items() if name in _FIELD_KINDS}


def _coerce(name: str, raw: object, base_dir: Path, source: str) -> object:
    kind = _FIELD_KINDS[name]
    if isinstance(raw, str):
        raw = raw.strip()
    try:
        if kind is Path:
            if raw == "":
                return None
            path = Path(str(raw)).expanduser()
            return path if path.is_absolute() else base_dir / path
        if kind is str:
            if not isinstance(raw, str):
                raise ValueError("expected a string")
            return raw
        if isinstance(raw, bool):
            raise ValueError("expected a number")
        if kind is int:
            if isinstance(raw, str):
                return int(raw.replace("_", ""))
            if not isinstance(raw, int):
                raise ValueError("expected an integer")
            return raw
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{source}: {name}={raw!r}: {exc}") from exc


def _validate(settings: Settings) -> None:
    errors = []
    if settings.doc_lang not in DOC_LANGS:
        errors.append(f"doc_lang must be one of {', '.join(DOC_LANGS)}")
    for name in ("effort_implementer", "effort_critic"):
        if getattr(settings, name) not in EFFORTS:
            errors.append(f"{name} must be one of {', '.join(EFFORTS)}")
    for name in ("model_planner", "model_implementer", "model_critic"):
        if not getattr(settings, name):
            errors.append(f"{name} is empty")
    for f in fields(settings):
        value = getattr(settings, f.name)
        if _FIELD_KINDS[f.name] is int and value <= 0:
            errors.append(f"{f.name} must be positive")
        if _FIELD_KINDS[f.name] is float and not 0 <= value < 1:
            errors.append(f"{f.name} must be in [0, 1)")
    if settings.ctx_soft_tokens >= settings.ctx_hard_tokens:
        errors.append("ctx_soft_tokens must be less than ctx_hard_tokens")
    if settings.diff_soft_lines >= settings.diff_hard_lines:
        errors.append("diff_soft_lines must be less than diff_hard_lines")
    if settings.context_file is not None and not settings.context_file.is_file():
        errors.append(f"context_file not found: {settings.context_file}")
    if errors:
        raise ConfigError("; ".join(errors))
