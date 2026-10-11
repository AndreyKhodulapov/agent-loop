import subprocess

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


def test_save_wip_keeps_the_step_work_and_leaves_the_tree_alone(repo):
    (repo / ".env").write_text("TOKEN=1\n")
    before = git.untracked(repo)
    head = git.head(repo)
    (repo / "app.py").write_text("x = 2\n")
    (repo / "old.py").unlink()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "[new].py").write_text("z = 3\n")
    git.git(repo, "add", "app.py")

    commit = git.save_wip(repo, git.wip_ref("3f2a", 2), before)

    assert git.git(repo, "rev-parse", "refs/loop/wip/3f2a/2").strip() == commit
    assert git.git(repo, "rev-parse", f"{commit}^").strip() == head
    assert sorted(ls_tree(repo, commit)) == ["app.py", "pkg/[new].py"]
    assert show(repo, commit, "app.py") == "x = 2\n"
    assert git.head(repo) == head
    assert git.git(repo, "diff", "--cached", "--name-only").split() == ["app.py"]
    assert (repo / ".env").is_file()
    assert sorted(git.untracked(repo)) == [".env", "pkg/[new].py"]


def test_save_wip_without_changes_keeps_the_head_tree(repo):
    commit = git.save_wip(repo, git.wip_ref("3f2a", 1), [])

    assert git.git(repo, "rev-parse", f"{commit}^{{tree}}") == git.git(
        repo, "rev-parse", "HEAD^{tree}"
    )


def test_git_error_carries_stderr(repo):
    with pytest.raises(git.GitError, match="rev-parse nope"):
        git.git(repo, "rev-parse", "nope")
