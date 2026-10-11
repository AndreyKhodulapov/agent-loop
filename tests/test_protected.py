import json
import os

import pytest

from agent_loop import protected

SETTINGS = '{"permissions": {"allow": ["Bash(make *)"]}}'
RULE = "Use the project logger.\n"


@pytest.fixture
def repo(tmp_path):
    """A working directory with project settings and a rule, saved as the start of a run."""
    path = tmp_path / "repo"
    (path / ".claude" / "rules").mkdir(parents=True)
    (path / ".claude" / "settings.json").write_text(SETTINGS)
    (path / ".claude" / "rules" / "style.md").write_text(RULE)
    (path / "app.py").write_text("x = 1\n")
    protected.save(path, tmp_path / "run")
    return path


def restore(repo):
    return protected.restore(repo, repo.parent / "run")


def plant_hook(repo):
    (repo / ".claude" / "settings.json").write_text('{"hooks": {}}')


def plant_local_settings(repo):
    (repo / ".claude" / "settings.local.json").write_text('{"hooks": {}}')


def plant_mcp(repo):
    (repo / ".mcp.json").write_text('{"mcpServers": {}}')


def plant_hook_script(repo):
    (repo / ".claude" / "hooks").mkdir()
    (repo / ".claude" / "hooks" / "fmt.sh").write_text("curl evil\n")


def plant_skill(repo):
    (repo / ".claude" / "skills" / "fmt").mkdir(parents=True)
    (repo / ".claude" / "skills" / "fmt" / "SKILL.md").write_text("---\nhooks: {}\n---\n")


def link_settings(repo):
    (repo / ".claude" / "settings.json").unlink()
    (repo / ".claude" / "settings.json").symlink_to(repo.parent / "elsewhere.json")


def link_rules(repo):
    (repo / ".claude" / "rules" / "more").symlink_to(repo.parent)


def fifo_instead_of_rule(repo):
    (repo / ".claude" / "rules" / "style.md").unlink()
    os.mkfifo(repo / ".claude" / "rules" / "style.md")


def directory_instead_of_settings(repo):
    (repo / ".claude" / "settings.json").unlink()
    (repo / ".claude" / "settings.json" / "x").mkdir(parents=True)


def remove_rules(repo):
    (repo / ".claude" / "rules" / "style.md").unlink()
    (repo / ".claude" / "rules").rmdir()


@pytest.mark.parametrize(
    ("change", "restored"),
    [
        (plant_hook, [".claude/settings.json"]),
        (plant_local_settings, [".claude/settings.local.json"]),
        (plant_mcp, [".mcp.json"]),
        (plant_hook_script, [".claude/hooks/fmt.sh"]),
        (plant_skill, [".claude/skills/fmt/SKILL.md"]),
        (link_settings, [".claude/settings.json"]),
        (link_rules, [".claude/rules/more"]),
        (fifo_instead_of_rule, [".claude/rules/style.md"]),
        (directory_instead_of_settings, [".claude/settings.json"]),
        (remove_rules, [".claude/rules/style.md"]),
    ],
    ids=lambda v: getattr(v, "__name__", None),
)
def test_changes_are_put_back(repo, change, restored):
    change(repo)

    assert restore(repo) == restored
    # Nothing differs from the copy any more.
    assert restore(repo) == []
    assert (repo / ".claude" / "settings.json").read_text() == SETTINGS
    assert (repo / ".claude" / "rules" / "style.md").read_text() == RULE


def test_other_files_are_not_protected(repo):
    (repo / "app.py").write_text("x = 2\n")
    (repo / ".claude" / "output-styles").mkdir()
    (repo / ".claude" / "output-styles" / "terse.md").write_text("be terse\n")

    assert restore(repo) == []
    assert (repo / "app.py").read_text() == "x = 2\n"


def test_copy_is_taken_once_per_run(repo):
    (repo / ".mcp.json").write_text('{"mcpServers": {}}')

    with pytest.raises(FileExistsError):
        protected.save(repo, repo.parent / "run")

    assert restore(repo) == [".mcp.json"]


def test_symlink_of_the_operator_is_kept(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    (tmp_path / "shared.json").write_text(SETTINGS)
    (repo / ".claude" / "settings.json").symlink_to(tmp_path / "shared.json")
    protected.save(repo, tmp_path / "run")

    assert protected.restore(repo, tmp_path / "run") == []
    (repo / ".claude" / "settings.json").unlink()
    assert protected.restore(repo, tmp_path / "run") == [".claude/settings.json"]
    assert os.readlink(repo / ".claude" / "settings.json") == str(tmp_path / "shared.json")


@pytest.mark.parametrize("name", protected.SETTINGS_FILES)
def test_repository_with_project_hooks_is_refused(tmp_path, name):
    hook = {"type": "command", "command": "make fmt"}
    (tmp_path / ".claude").mkdir()
    (tmp_path / name).write_text(
        json.dumps({"hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [hook]}]}})
    )

    with pytest.raises(protected.ProjectHooksError, match="has hooks"):
        protected.save(tmp_path, tmp_path / "run")

    assert not (tmp_path / "run").exists()


def test_empty_hooks_are_allowed(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text('{"hooks": {}}')

    protected.save(tmp_path, tmp_path / "run")

    assert protected.restore(tmp_path, tmp_path / "run") == []


def test_restore_without_a_copy_fails(tmp_path):
    (tmp_path / ".mcp.json").write_text("{}")

    with pytest.raises(FileNotFoundError):
        protected.restore(tmp_path, tmp_path / "run")

    assert (tmp_path / ".mcp.json").exists()
