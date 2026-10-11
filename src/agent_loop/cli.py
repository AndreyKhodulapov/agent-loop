from __future__ import annotations

import argparse
import logging
from collections.abc import Callable
from pathlib import Path

from agent_loop import config, log

logger = logging.getLogger(__name__)

STUBS = {
    "plan": "plan a task with the planner and the plan critic",
    "approve": "approve the plan and freeze it",
    "run": "run the approved plan",
    "resume": "resume a paused run",
    "answer": "answer the questions of a run waiting for a human",
    "status": "show runs and their state",
    "stats": "show metrics of finished runs",
    "selfcheck": "check that the installed claude CLI matches the adapter",
    "gc": "stop leftovers of runs that died without cleanup",
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
    return args.handler(args, cfg)


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
    commands.add_parser(
        "config", help="show effective settings and where each came from"
    ).set_defaults(handler=_show_config)
    return parser


def _stub(name: str) -> Callable[[argparse.Namespace, config.Config], int]:
    def handler(args: argparse.Namespace, cfg: config.Config) -> int:
        logger.error("%s: not implemented yet", name)
        return 1

    return handler


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
