import subprocess
from pathlib import Path

import pytest

from agent_loop import config

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def home(tmp_path):
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "service"
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


def load(cwd, home, flags=None, profile=None, **env):
    return config.load(cwd, flags, profile=profile, environ={"LOOP_HOME": str(home), **env})


def test_defaults_without_any_source(tmp_path, home):
    cfg = load(tmp_path, home)

    assert cfg.settings == config.Settings()
    assert cfg.repo_root is None
    assert cfg.profile is None
    assert cfg.repo == config.Profile()
    assert set(cfg.sources.values()) == {"default"}


def test_env_example_is_valid(tmp_path, home):
    (home / ".env").write_text((ROOT / ".env.example").read_text())

    cfg = load(tmp_path, home)

    assert cfg.settings.doc_lang == "ru"
    assert cfg.settings.knowledge_dir == Path.home() / (
        "andrey_personal_projects/MY-KNOWLEDGE-DATABASE/USEFUL_JOB_NOTES/agent_runs"
    )


def test_layers_override_in_order(repo, home):
    (home / ".env").write_text("LOOP_DOC_LANG=ru\nLOOP_MODEL_CRITIC=sonnet\nLOOP_FIX_ATTEMPTS=5\n")
    (repo / "loop.toml").write_text('model_critic = "fable"\nfix_attempts = 4\n')

    cfg = load(repo, home, flags={"fix_attempts": 2, "doc_lang": None}, LOOP_DOC_LANG="en")

    assert cfg.settings.doc_lang == "en"
    assert cfg.sources["doc_lang"] == "environment"
    assert cfg.settings.model_critic == "fable"
    assert cfg.sources["model_critic"] == str(repo / "loop.toml")
    assert cfg.settings.fix_attempts == 2
    assert cfg.sources["fix_attempts"] == "flag"


def test_profile_from_knowledge_dir(repo, home, tmp_path):
    knowledge = tmp_path / "agent_runs"
    (knowledge / repo.name).mkdir(parents=True)
    (knowledge / repo.name / "CLAUDE.md").write_text("rules")
    profile = knowledge / repo.name / "loop.toml"
    profile.write_text('context_file = "CLAUDE.md"\n[commands]\nlint = "flake8"\n')
    (home / ".env").write_text(f"LOOP_KNOWLEDGE_DIR={knowledge}\n")

    cfg = load(repo / ".", home)

    assert cfg.profile == profile
    assert cfg.settings.context_file == knowledge / repo.name / "CLAUDE.md"


def test_repository_profile(repo, home):
    (repo / "loop.toml").write_text(
        'doc_lang = "ru"\n'
        'deps_files = ["requirements*.txt", "uv.lock"]\n'
        'secret_patterns = ["*.pem"]\n'
        "[commands]\n"
        'fmt = ""\n'
        'lint = "flake8"\n'
        'collect = "pytest --collect-only -q"\n'
        'up = "docker compose -p {run_id} up -d --wait"\n'
        "[timeouts]\n"
        "lint = 120\n"
        "[env]\n"
        'POSTGRES_PORT = "{free_port}"\n'
    )

    cfg = load(repo, home)

    assert cfg.settings.doc_lang == "ru"
    assert cfg.repo == config.Profile(
        commands={
            "lint": "flake8",
            "collect": "pytest --collect-only -q",
            "up": "docker compose -p {run_id} up -d --wait",
        },
        timeouts={"lint": 120},
        env={"POSTGRES_PORT": "{free_port}"},
        deps_files=("requirements*.txt", "uv.lock"),
        secret_patterns=("*.pem",),
    )


def test_two_profiles_for_one_repo_are_rejected(repo, home, tmp_path):
    knowledge = tmp_path / "agent_runs"
    (knowledge / repo.name).mkdir(parents=True)
    (knowledge / repo.name / "loop.toml").write_text("")
    (repo / "loop.toml").write_text("")
    (home / ".env").write_text(f"LOOP_KNOWLEDGE_DIR={knowledge}\n")

    with pytest.raises(config.ConfigError, match="two profiles"):
        load(repo, home)


def test_explicit_profile_wins(repo, home, tmp_path):
    (repo / "loop.toml").write_text('doc_lang = "ru"\n')
    explicit = tmp_path / "other.toml"
    explicit.write_text('doc_lang = "en"\n')

    cfg = load(repo, home, profile=explicit)

    assert cfg.profile == explicit
    assert cfg.settings.doc_lang == "en"


def test_missing_explicit_profile(repo, home, tmp_path):
    with pytest.raises(config.ConfigError, match="profile not found"):
        load(repo, home, profile=tmp_path / "nope.toml")


def test_knowledge_dir_in_profile_is_rejected(repo, home):
    (repo / "loop.toml").write_text('knowledge_dir = "/tmp"\n')

    with pytest.raises(config.ConfigError, match="knowledge_dir"):
        load(repo, home)


def test_unknown_key_in_env_file(tmp_path, home):
    (home / ".env").write_text("LOOP_DOC_LNG=ru\n")

    with pytest.raises(config.ConfigError, match="LOOP_DOC_LNG"):
        load(tmp_path, home)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("LOOP_DOC_LANG=de\n", "doc_lang must be one of"),
        ("LOOP_EFFORT_CRITIC=extreme\n", "effort_critic"),
        ("LOOP_FIX_ATTEMPTS=many\n", "fix_attempts"),
        ("LOOP_FIX_ATTEMPTS=0\n", "fix_attempts must be positive"),
        ("LOOP_RESERVE_5H=1.5\n", "reserve_5h must be in"),
        ("LOOP_CTX_SOFT_TOKENS=200000\n", "ctx_soft_tokens must be less"),
        ("LOOP_DIFF_HARD_LINES=900\n", "diff_soft_lines must be less"),
        ("LOOP_MODEL_PLANNER=\n", "model_planner is empty"),
        ("LOOP_CONTEXT_FILE=missing.md\n", "context_file not found"),
        ("LOOP_DOC_LANG\n", "expected KEY=VALUE"),
        ("LOOP_DOC_LANG='ru\n", "unterminated quote"),
    ],
)
def test_invalid_env_file(tmp_path, home, content, message):
    (home / ".env").write_text(content)

    with pytest.raises(config.ConfigError, match=message):
        load(tmp_path, home)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("fix_attempts = 1.5\n", "expected an integer"),
        ("fix_attempts = true\n", "expected a number"),
        ("doc_lang = 1\n", "expected a string"),
        ("doc_lang = \n", "loop.toml"),
        ("doc_lnag = 'ru'\n", "unknown keys doc_lnag"),
        ("[commands]\nbuild = 'make'\n", "unknown command build"),
        ("[timeouts]\nlnit = 60\n", "unknown command lnit"),
        ("[commands]\nlint = 1\n", "commands.lint=1: expected str"),
        ("[timeouts]\nlint = '60'\n", "expected int"),
        ("[timeouts]\nlint = true\n", "expected int"),
        ("[timeouts]\nlint = 0\n", "timeouts.lint must be positive"),
        ("[env]\nPORT = 5433\n", "env.PORT=5433: expected str"),
        ("commands = 'lint'\n", "commands must be a table"),
        ("deps_files = 'uv.lock'\n", "deps_files must be a list of strings"),
        ("secret_patterns = [1]\n", "secret_patterns must be a list of strings"),
    ],
)
def test_invalid_profile(repo, home, content, message):
    (repo / "loop.toml").write_text(content)

    with pytest.raises(config.ConfigError, match=message):
        load(repo, home)


def test_parse_env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# comment\n"
        "\n"
        "export LOOP_A=1\n"
        "LOOP_B = two words   # trailing comment\n"
        'LOOP_C="quoted # not a comment"\n'
        "LOOP_D='single'\n"
        "LOOP_E=a#b\n"
    )

    assert config.parse_env_file(path) == {
        "LOOP_A": "1",
        "LOOP_B": "two words",
        "LOOP_C": "quoted # not a comment",
        "LOOP_D": "single",
        "LOOP_E": "a#b",
    }


def test_numbers_from_env(tmp_path, home):
    (home / ".env").write_text("LOOP_CTX_HARD_TOKENS=160_000\nLOOP_RESERVE_7D=0\n")

    cfg = load(tmp_path, home)

    assert cfg.settings.ctx_hard_tokens == 160_000
    assert cfg.settings.reserve_7d == 0.0
