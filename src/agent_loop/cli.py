from __future__ import annotations

import argparse
import contextlib
import logging
import signal
from collections.abc import Callable
from pathlib import Path

from agent_loop import agent, config, git, implement, log, recovery, runner, selfcheck
from agent_loop.commands import CommandError
from agent_loop.store import DB_FILE, Store, StoreError

logger = logging.getLogger(__name__)

STUBS = {
    "plan": "plan a task with the planner and the plan critic",
    "approve": "approve the plan and freeze it",
    "answer": "answer the questions of a run waiting for a human",
    "status": "show runs and their state",
    "stats": "show metrics of finished runs",
}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    log.setup(args.verbose)
    flags = {
        "doc_lang": args.doc_lang,
        "model_planner": args.model_planner,
        "model_implementer": args.model_implementer,
        "model_critic": args.model_critic,
    }
    try:
        cfg = config.load(Path.cwd(), flags, profile=args.profile)
    except config.ConfigError as exc:
        logger.error("config: %s", exc)
        return 2
    _exit_on_termination()
    # Every command that works with runs first pauses the ones that died, so a run is never
    # left as running without an orchestrator.
    if args.command in (*STUBS, "run", "resume"):
        _reap(cfg)
    return args.handler(args, cfg)


def _exit_on_termination() -> None:
    # SystemExit unwinds the stack, so `agent.Process` terminates the session on the way out;
    # the default action would leave the session running in its own process group.
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, _exit)


def _exit(signum: int, frame: object) -> None:
    raise SystemExit(128 + signum)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loop", description="Development loop on top of claude CLI"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug output")
    parser.add_argument("--profile", type=Path, help="repository profile, loop.toml")
    parser.add_argument(
        "--doc-lang", choices=config.DOC_LANGS, help="language of docs and comments"
    )
    parser.add_argument("--model-planner")
    parser.add_argument("--model-implementer")
    parser.add_argument("--model-critic")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    for name, summary in STUBS.items():
        commands.add_parser(name, help=summary).set_defaults(handler=_stub(name))
    run = commands.add_parser("run", help="run the tasks of a cards file on the current branch")
    run.add_argument(
        "tasks", type=Path, help="JSON list of title, goal, criteria, files, tests, decisions"
    )
    run.set_defaults(handler=_run)
    resume = commands.add_parser("resume", help="resume a paused run")
    resume.add_argument(
        "run_id", nargs="?", help="the run to resume, the last paused one by default"
    )
    resume.set_defaults(handler=_resume)
    commands.add_parser(
        "config", help="show effective settings and where each came from"
    ).set_defaults(handler=_show_config)
    commands.add_parser(
        "selfcheck", help="check that the installed claude CLI matches the adapter"
    ).set_defaults(handler=_selfcheck)
    commands.add_parser(
        "gc", help="pause runs that died without cleanup and stop their sessions"
    ).set_defaults(handler=_gc)
    return parser


def _stub(name: str) -> Callable[[argparse.Namespace, config.Config], int]:
    def handler(args: argparse.Namespace, cfg: config.Config) -> int:
        logger.error("%s: not implemented yet", name)
        return 1

    return handler


def _run(args: argparse.Namespace, cfg: config.Config) -> int:
    return _work(cfg, lambda: runner.start(cfg, args.tasks))


def _resume(args: argparse.Namespace, cfg: config.Config) -> int:
    return _work(cfg, lambda: runner.resume(cfg, args.run_id))


def _work(cfg: config.Config, run: Callable[[], str | None]) -> int:
    try:
        paused = run()
    # A broken profile, a failed `install` or `up`, git or the store failing under the run stop
    # the run as well as a refusal to start.
    except (
        runner.RunError,
        agent.AgentError,
        config.ConfigError,
        CommandError,
        git.GitError,
        StoreError,
    ) as exc:
        logger.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        logger.warning("stopped by the operator")
        return 130
    finally:
        # A run that has stopped on the way is paused the way a dead one is.
        _reap(cfg)
    if paused is None:
        return 0
    # The operator has stopped the run, in a session or outside it.
    return 130 if paused == implement.INTERRUPT else 1


def _show_config(args: argparse.Namespace, cfg: config.Config) -> int:
    print(f"home        {cfg.home}")
    print(f"repository  {cfg.repo_root or '-'}")
    print(f"profile     {cfg.profile or '-'}")
    print()
    rows = config.describe(cfg)
    width = max(len(name) for name, _, _ in rows)
    for name, value, source in rows:
        print(f"{name:<{width}}  {value or '-'}  ({source})")
    return 0


def _selfcheck(args: argparse.Namespace, cfg: config.Config) -> int:
    with log.scope(stage="selfcheck"):
        logger.info(
            "three short sessions on %s, raw streams in %s",
            selfcheck.MODEL,
            cfg.home / selfcheck.DIR,
        )
        try:
            version = selfcheck.run(cfg.home)
        except agent.AgentError as exc:
            logger.error("failed: %s", exc)
            return 1
        logger.info("ok, claude %s", version)
    return 0


def _gc(args: argparse.Namespace, cfg: config.Config) -> int:
    if not _reap(cfg):
        logger.info("no dead runs")
    return 0


def _reap(cfg: config.Config) -> list[str]:
    with contextlib.closing(Store(cfg.home / DB_FILE)) as db:
        return recovery.reap(db, cfg.home)
