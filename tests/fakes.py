"""A fake `claude` for tests and the stream shapes it prints."""

import json

# Shapes taken from a real `claude -p --output-format stream-json --verbose` run on 2.1.296.
INIT = {
    "type": "system",
    "subtype": "init",
    "session_id": "{session_id}",
    "model": "claude-haiku-5-5",
    "claude_code_version": "2.1.296",
    "permissionMode": "dontAsk",
    "tools": ["Bash", "Read"],
}
ASSISTANT = {
    "type": "assistant",
    "parent_tool_use_id": None,
    "session_id": "{session_id}",
    "message": {
        "content": [{"type": "text", "text": "pong"}],
        "usage": {
            "input_tokens": 4,
            "cache_creation_input_tokens": 5232,
            "cache_read_input_tokens": 13060,
            "output_tokens": 5,
        },
    },
}
SUBAGENT = {**ASSISTANT, "parent_tool_use_id": "toolu_01"}
RATE_LIMIT = {
    "type": "rate_limit_event",
    "session_id": "{session_id}",
    "rate_limit_info": {
        "status": "allowed",
        "rateLimitType": "five_hour",
        "unifiedWindows": {
            "five_hour": {"utilization": 0.03, "resetsAt": 1791734400},
            "seven_day": {"utilization": 0.64, "resetsAt": 1791813600},
        },
    },
}
USER = {"type": "user", "session_id": "{session_id}", "message": {"content": []}}
RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "terminal_reason": "completed",
    "result": "pong",
    "num_turns": 3,
    "total_cost_usd": 0.0049,
    "permission_denials": [],
    "session_id": "{session_id}",
}

# A scenario is chosen by the session: "resume" for --resume, "hooked" for a new session whose
# --settings have hooks (if the scenario has it), "new" otherwise. Besides events, it may hold
# commands: "sleep" (30 s), "sleep:<seconds>", "interrupt" (Ctrl+C to the loop, then wait for its
# stop file), "tool:<name>" (ask the PreToolUse hook from --settings about a call; its output goes
# to hooks.jsonl), "handoff" (write .loop/handoff.md like an agent asked to stop),
# "project-hook" (run the SessionStart hook of .claude/settings.json in the working directory, as
# a CLI that does not ignore the project settings would) and "ignore-term" (ignore SIGTERM from
# now on).
FAKE_CLAUDE = """#!{python}
import json, os, signal, subprocess, sys, time
from pathlib import Path

here = Path(__file__).parent
argv = sys.argv[1:]
if argv == ["--version"]:
    print("2.1.296 (Claude Code)")
    sys.exit(0)
flag = "--resume" if "--resume" in argv else "--session-id"
session_id = argv[argv.index(flag) + 1]
settings = Path(argv[argv.index("--settings") + 1])
# An interactive session reads the terminal, not the prompt.
call = {
    "argv": argv,
    "prompt": sys.stdin.read() if "-p" in argv else None,
    "env": dict(os.environ),
    "cwd": os.getcwd(),
    "pid": os.getpid(),
    "pgid": os.getpgid(0),
}
if "--append-system-prompt-file" in argv:
    call["system"] = Path(argv[argv.index("--append-system-prompt-file") + 1]).read_text()
with open(here / "calls.jsonl", "a") as calls:
    calls.write(json.dumps(call) + "\\n")
scenario = json.loads((here / "scenario.json").read_text())
print(scenario.get("stderr", ""), file=sys.stderr)
if flag == "--resume":
    kind = "resume"
elif "hooked" in scenario and "hooks" in json.loads(settings.read_text()):
    kind = "hooked"
else:
    kind = "new"
for item in scenario[kind]:
    if item == "sleep":
        time.sleep(30)
    elif isinstance(item, str) and item.startswith("sleep:"):
        time.sleep(float(item[len("sleep:"):]))
    elif item == "ignore-term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    elif item == "project-hook":
        project = json.loads(Path(".claude/settings.json").read_text())
        subprocess.run(project["hooks"]["SessionStart"][0]["hooks"][0]["command"], shell=True)
    elif item == "handoff":
        Path(".loop").mkdir(exist_ok=True)
        Path(".loop/handoff.md").write_text("done: nothing")
    elif item == "interrupt":
        os.kill(os.getppid(), signal.SIGINT)
        stop = settings.parent / "stop"
        deadline = time.monotonic() + 10
        while not stop.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
    elif isinstance(item, str) and item.startswith("tool:"):
        hooks = json.loads(settings.read_text())["hooks"]["PreToolUse"][0]["hooks"]
        hook_call = {"tool_name": item[len("tool:"):], "tool_input": {}, "cwd": os.getcwd()}
        proc = subprocess.run(
            hooks[0]["command"],
            shell=True,
            input=json.dumps(hook_call),
            capture_output=True,
            text=True,
            check=True,
        )
        with open(here / "hooks.jsonl", "a") as out:
            out.write(json.dumps(json.loads(proc.stdout) if proc.stdout else None) + "\\n")
    else:
        print(json.dumps(item).replace("{session_id}", session_id), flush=True)
sys.exit(scenario.get("exit", 0))
"""


def read_calls(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def start_stream(run_dir, step_id, session_id):
    """Write the `init` a session starts its stream with, for a test that replaces `step.run`."""
    run_dir.mkdir(parents=True, exist_ok=True)
    init = json.dumps(INIT).replace("{session_id}", session_id)
    (run_dir / f"step-{step_id}.jsonl").write_text(init + "\n")
