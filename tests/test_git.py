import subprocess
import sys

import pytest

from agent_loop import git


def show(repo, rev, path):
    return subprocess.run(
        ["git", "show", f"{rev}:{path}"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def ls_tree(repo, rev):
    return git.git(repo, "ls-tree", "-r", "--name-only", rev).split()


def test_untracked_lists_new_files_but_not_ignored_ones(repo):
    (repo / ".gitignore").write_text("*.log\n")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "new file.py").write_text("")
    (repo / "debug.log").write_text("")

    assert sorted(git.untracked(repo)) == [".gitignore", "pkg/new file.py"]


def test_exclude_adds_a_pattern_once(repo):
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text("*.tmp")
    (repo / ".loop").mkdir()
    (repo / ".loop" / "handoff.md").write_text("")

    git.exclude(repo, "/.loop/")
    git.exclude(repo, "/.loop/")

    assert exclude.read_text() == "*.tmp\n/.loop/\n"
    assert git.untracked(repo) == []


def test_save_wip_keeps_the_step_work_and_leaves_the_tree_alone(repo):
    (repo / ".env").write_text("TOKEN=1\n")
    before = git.untracked(repo)
    head = git.head(repo)
    (repo / "app.py").write_text("x = 2\n")
    (repo / "old.py").unlink()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "[new].py").write_text("z = 3\n")
    git.git(repo, "add", "app.py")

    commit = git.save_wip(repo, git.wip_ref("3f2a", 2), before, head)

    assert git.git(repo, "rev-parse", "refs/loop/wip/3f2a/2").strip() == commit
    assert git.git(repo, "rev-parse", f"{commit}^").strip() == head
    assert sorted(ls_tree(repo, commit)) == ["app.py", "pkg/[new].py"]
    assert show(repo, commit, "app.py") == "x = 2\n"
    assert git.head(repo) == head
    assert git.git(repo, "diff", "--cached", "--name-only").split() == ["app.py"]
    assert (repo / ".env").is_file()
    assert sorted(git.untracked(repo)) == [".env", "pkg/[new].py"]


def test_drop_work_puts_the_tree_back_to_head_and_keeps_the_files_of_the_operator(repo):
    (repo / ".env").write_text("TOKEN=1\n")
    before = git.untracked(repo)
    (repo / "app.py").write_text("x = 2\n")
    (repo / "old.py").unlink()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "new.py").write_text("")
    (repo / "staged.py").write_text("")
    git.git(repo, "add", "staged.py", "app.py")

    git.drop_work(repo, git.new_files(repo, git.head(repo), before))

    assert (repo / "app.py").read_text() == "x = 1\n"
    assert (repo / "old.py").read_text() == "y = 2\n"
    assert git.git(repo, "status", "--porcelain") == "?? .env\n"


def test_new_files_are_the_ones_the_step_has_added(repo):
    (repo / ".env").write_text("TOKEN=1\n")
    (repo / "notes.txt").write_text("")
    before = git.untracked(repo)
    (repo / "new.py").write_text("")
    (repo / "staged.py").write_text("")
    git.git(repo, "add", "staged.py", "notes.txt")

    assert git.new_files(repo, git.head(repo), before) == ["new.py", "staged.py"]


def test_save_wip_without_changes_keeps_the_head_tree(repo):
    commit = git.save_wip(repo, git.wip_ref("3f2a", 1), [], git.head(repo))

    assert git.git(repo, "rev-parse", f"{commit}^{{tree}}") == git.git(
        repo, "rev-parse", "HEAD^{tree}"
    )


def test_commit_takes_the_work_and_leaves_the_rest_in_the_tree(repo):
    (repo / ".env").write_text("TOKEN=1\n")
    head = git.head(repo)
    (repo / "app.py").write_text("x = 2\n")
    (repo / "old.py").unlink()
    (repo / "new.py").write_text("z = 3\n")
    (repo / "secret.pem").write_text("key\n")
    git.git(repo, "add", "app.py", "secret.pem")

    commit = git.commit(repo, head, ["new.py"], "feat(app): x")

    assert git.head(repo) == commit
    assert git.branch(repo) == "main"
    assert git.git(repo, "rev-parse", f"{commit}^").strip() == head
    assert git.git(repo, "log", "-1", "--format=%s|%an").strip() == "feat(app): x|Test"
    assert sorted(ls_tree(repo, commit)) == ["app.py", "new.py"]
    assert show(repo, commit, "app.py") == "x = 2\n"
    # The index is the commit, so the file left out is untracked again.
    assert git.git(repo, "status", "--porcelain").splitlines() == ["?? .env", "?? secret.pem"]


def test_commit_does_not_move_a_branch_that_has_moved(repo):
    checkpoint = git.head(repo)
    git.git(repo, "commit", "-q", "--allow-empty", "-m", "operator's commit")
    moved = git.head(repo)
    (repo / "app.py").write_text("x = 2\n")

    with pytest.raises(git.GitError, match="update-ref"):
        git.commit(repo, checkpoint, [], "feat(app): x")

    assert git.head(repo) == moved


def test_words_in_finds_whole_words_outside_the_file(repo):
    (repo / ".gitignore").write_text("*.log\n")
    (repo / "notes.md").write_text("RATE is from `orders`\n")
    (repo / "debug.log").write_text("TAX\n")
    (repo / "orders.py").write_text("RATE = 2\nTAX = 1\nRATES = 3\nLIMIT = 4\n")

    found = git.words_in(repo, ["RATE", "TAX", "RATES", "LIMIT", "x"], "orders.py")

    # `x` is in the tracked app.py, `RATE` in the untracked notes.md; debug.log is ignored.
    assert found == {"RATE", "x"}
    assert git.words_in(repo, ["LIMIT"], "orders.py") == set()


def test_strings_in_finds_strings_in_the_files_of_a_commit(repo):
    (repo / "db.py").write_text('URL = "postgresql://localhost"\n')
    git.git(repo, "add", "db.py")
    git.git(repo, "commit", "-q", "-m", "db")
    (repo / "app.py").write_text("x = 'hunter2-long'\n")

    found = git.strings_in(repo, ["postgres", "hunter2-long", "-v"], "HEAD")

    # Inside a word too; the working tree is not looked at.
    assert found == {"postgres"}
    assert git.strings_in(repo, ["postgres"], "HEAD~1") == set()


def test_git_error_carries_stderr(repo):
    with pytest.raises(git.GitError, match="rev-parse nope"):
        git.git(repo, "rev-parse", "nope")


def test_save_wip_goes_on_top_of_the_checkpoint(repo):
    checkpoint = git.head(repo)
    (repo / "app.py").write_text("x = 2\n")
    git.git(repo, "commit", "-q", "--allow-empty", "-m", "operator's commit")

    commit = git.save_wip(repo, git.wip_ref("3f2a", 1), [], checkpoint)

    assert git.git(repo, "rev-parse", f"{commit}^").strip() == checkpoint
    assert show(repo, commit, "app.py") == "x = 2\n"


def test_git_of_the_loop_runs_no_code_from_the_repository(repo, tmp_path):
    # What an agent could plant from its sandbox: hooks, an fsmonitor and diff drivers run by the
    # loop's git.
    marker = tmp_path / "ran"
    script = f"#!{sys.executable}\nimport pathlib; pathlib.Path({str(marker)!r}).touch()\n"
    for name in ("post-index-change", "reference-transaction"):
        hook = repo / ".git" / "hooks" / name
        hook.write_text(script)
        hook.chmod(0o755)
    fsmonitor = tmp_path / "fsmonitor"
    fsmonitor.write_text(script)
    fsmonitor.chmod(0o755)
    for key, value in (
        ("core.fsmonitor", str(fsmonitor)),
        ("diff.external", str(fsmonitor)),
        ("diff.planted.textconv", str(fsmonitor)),
        ("diff.planted.command", str(fsmonitor)),
    ):
        subprocess.run(["git", "config", key, value], cwd=repo, check=True)
    (repo / ".gitattributes").write_text("*.py diff=planted\n")
    (repo / "app.py").write_text("x = 2\n")

    git.save_wip(repo, git.wip_ref("3f2a", 1), [], git.head(repo))
    git.diff(repo, git.head(repo), [".gitattributes"], "--numstat")
    git.diff(repo, git.head(repo), [".gitattributes"], "-U0")
    git.commit(repo, git.head(repo), [".gitattributes"], "feat(app): x")
    git.words_in(repo, ["x"], "old.py")

    assert not marker.exists()
    # The planted code is live for a plain git.
    subprocess.run(["git", "update-ref", "refs/loop/probe", "HEAD"], cwd=repo, check=True)
    assert marker.exists()
