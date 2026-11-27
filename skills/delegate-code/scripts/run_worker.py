#!/usr/bin/env python3
"""Run and manage one delegate-code worker in its own git worktree (Code Mode runner)."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path


WORKERS = ("codex", "claude", "agy")
SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._:-]{0,127}$")
MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
PROGRAM_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._+-]{0,63}$")
FINGERPRINT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_TIMEOUT_SECONDS = 1800
TERM_GRACE_SECONDS = 2
STATUS_EXIT_CODES = {
    "ok": 0,
    "failed": 1,
    "empty": 65,
    "permission": 77,
    "refusal": 78,
    "timeout": 124,
}
ERROR_EXIT_CODE = 2
PERMISSION_DENIAL_MARKERS = ('required the "command" permission', "headless mode cannot prompt")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class RunnerError(RuntimeError):
    """Raised when a command would break the delegate-code contract."""


# ---------------------------------------------------------------- validation


def check_slug(slug: str) -> str:
    # A slug ending in .run or .review would collide with another slug's run note or review folder.
    if not SLUG.fullmatch(slug) or ".." in slug or slug.endswith((".run", ".review")):
        raise RunnerError(
            f"invalid --slug {slug!r}: use letters, digits, '.', '_' or '-' (max 64, no '..', "
            "not ending in .run or .review)"
        )
    return slug


def check_token(value: str, label: str) -> str:
    if not SAFE_TOKEN.fullmatch(value):
        raise RunnerError(f"invalid {label} {value!r}")
    return value


def check_allow_tool(entry: str) -> str:
    if not entry or len(entry) > 256 or entry.startswith("-") or any(ord(c) < 32 or ord(c) == 127 for c in entry):
        raise RunnerError(f"invalid --allow-tool entry {entry!r}")
    return entry


def inside(path: Path, base: Path) -> Path:
    """Return the resolved path, or fail if it (or a symlink in it) escapes base."""
    resolved = path.resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise RunnerError(f"path escapes {base}: {path}")
    return resolved


def require_repo_root(repo_root: Path) -> Path:
    resolved = repo_root.expanduser().resolve()
    result = git(resolved, "rev-parse", "--show-toplevel", check=False)
    if result.returncode != 0:
        raise RunnerError(f"not a Git repository: {resolved}")
    if Path(result.stdout.strip()).resolve() != resolved:
        raise RunnerError(f"--repo-root must be the repository top level, got {resolved}")
    return resolved


def resolve_binary(worker: str, binary: str | None) -> str:
    # shutil.which never sees shell aliases or functions, so a wrapper cannot widen access.
    found = shutil.which(binary or worker)
    if found is None:
        raise RunnerError(f"worker binary not found: {binary or worker}")
    return found


# ---------------------------------------------------------------- git and files


def git(cwd: Path, *args: str, check: bool = True, input: bytes | None = None, text: bool = True):
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        check=False,
        input=input,
        text=text and input is None,
    )
    if check and result.returncode != 0:
        stderr = result.stderr if isinstance(result.stderr, str) else result.stderr.decode(errors="replace")
        raise RunnerError(f"git {' '.join(args)} failed: {stderr.strip()}")
    return result


class Layout:
    """Paths for one slug under the repository's ignored .delegate-code/ folder."""

    def __init__(self, repo_root: Path, slug: str) -> None:
        self.repo = repo_root
        self.base = inside(repo_root / ".delegate-code", repo_root)
        self.slug = check_slug(slug)
        # Keep the unresolved path so a symlinked worktree entry is still visible as a symlink.
        self.worktree = self.base / slug
        inside(self.worktree, self.base)
        self.note = self.base / f"{slug}.run"
        self.review = self.base / f"{slug}.review"
        self.branch = f"delegate-code/{slug}"

    def branch_exists(self) -> bool:
        return git(self.repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{self.branch}", check=False).returncode == 0


def require_registered_worktree(layout: Layout) -> None:
    """Fail unless the slug's path is a real linked worktree on delegate-code/<slug>."""
    if layout.worktree.is_symlink():
        raise RunnerError(f"worktree path is a symlink: {layout.worktree}")
    if not layout.worktree.is_dir():
        raise RunnerError(f"worktree missing: {layout.worktree}")
    target = layout.worktree.resolve()
    if target == layout.repo:
        raise RunnerError(f"worktree path is the repository root: {layout.worktree}")
    listing = git(layout.repo, "worktree", "list", "--porcelain").stdout
    for block in listing.split("\n\n"):
        fields = dict(line.partition(" ")[::2] for line in block.splitlines() if line)
        if fields.get("worktree") and Path(fields["worktree"]).resolve() == target:
            if fields.get("branch") != f"refs/heads/{layout.branch}":
                raise RunnerError(f"worktree {layout.worktree} is not on {layout.branch}")
            return
    raise RunnerError(f"not a registered git worktree: {layout.worktree}")


def prepare_output(path: Path, append: bool = False) -> None:
    """Never write through a pre-existing hard link or special file in $TASK."""
    if append and path.exists():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise RunnerError(f"refusing to append to a linked or special file: {path}")
        return
    path.unlink(missing_ok=True)


def atomic_write(directory: Path, name: str, data: str | bytes) -> Path:
    """Write directory/name via rename so a planted symlink is replaced, never followed."""
    target = directory / name
    if target.parent.resolve() != directory.resolve():
        raise RunnerError(f"refusing to write outside {directory}: {name}")
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data.encode("utf-8") if isinstance(data, str) else data)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target


def task_file(task: Path, name: str) -> Path:
    return inside(task / name, task)


def check_task_dir(task: Path, repo_root: Path) -> Path:
    resolved = task.expanduser().resolve()
    if not resolved.is_dir():
        raise RunnerError(f"--task-dir is not a directory: {task}")
    if resolved.is_relative_to(repo_root):
        raise RunnerError(f"--task-dir must be outside the repository: {resolved}")
    return resolved


def write_note(layout: Layout, note: dict[str, str]) -> None:
    lines = []
    for key, value in note.items():
        if "\n" in value or "\r" in value:
            raise RunnerError(f"run note value for {key} contains a newline")
        lines.append(f"{key}={value}")
    atomic_write(layout.base, layout.note.name, "\n".join(lines) + "\n")


def read_note(layout: Layout) -> dict[str, str] | None:
    if not layout.note.is_file():
        return None
    note = {}
    for line in layout.note.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            note[key.strip()] = value.strip()
    return note


def now_utc() -> str:
    return datetime.now(UTC).strftime(TIME_FORMAT)


# ---------------------------------------------------------------- commands


def build_command(
    worker: str,
    binary: str,
    *,
    full_access: bool,
    task: Path,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    prompt: str = "",
    allow_tools: list[str] | tuple[str, ...] = (),
    model: str | None = None,
    effort: str | None = None,
    max_budget_usd: float | None = None,
    resume_id: str | None = None,
) -> list[str]:
    if model and not MODEL_NAME.fullmatch(model):
        raise RunnerError(f"invalid --model {model!r}")
    if effort and effort not in EFFORT_LEVELS:
        raise RunnerError(f"invalid --effort {effort!r}")
    if max_budget_usd is not None and worker != "claude":
        raise RunnerError(f"--max-budget-usd is supported only for claude; {worker} has no spend flag, the timeout is its only cap")
    if max_budget_usd is not None and max_budget_usd <= 0:
        raise RunnerError("--max-budget-usd must be greater than zero")
    if allow_tools and full_access:
        raise RunnerError("--allow-tool applies only to restricted mode")
    if allow_tools and worker == "codex":
        raise RunnerError("--allow-tool is not supported for codex; workspace-write already allows commands in the worktree")
    if resume_id:
        check_token(resume_id, "session id")
    model_args = ["--model", model] if model else []

    if worker == "codex":
        if resume_id:
            access = ["--dangerously-bypass-approvals-and-sandbox"] if full_access else ["-c", 'sandbox_mode="workspace-write"']
            head = ["exec", "resume", *access]
        else:
            access = ["--dangerously-bypass-approvals-and-sandbox"] if full_access else ["--sandbox", "workspace-write"]
            head = ["exec", *access]
        return [
            binary,
            *head,
            "--json",
            "-o",
            str(task_file(task, "last.md")),
            *model_args,
            *(["-c", f'model_reasoning_effort="{effort}"'] if effort else []),
            *([resume_id] if resume_id else []),
            "-",
        ]
    if worker == "claude":
        if full_access:
            access = ["--permission-mode", "bypassPermissions"]
        else:
            access = ["--permission-mode", "acceptEdits"]
            for entry in allow_tools:
                access += ["--allowedTools", check_allow_tool(entry)]
        return [
            binary,
            "-p",
            *access,
            "--output-format",
            "json",
            *model_args,
            *(["--effort", effort] if effort else []),
            *(["--max-budget-usd", f"{max_budget_usd:g}"] if max_budget_usd is not None else []),
            *(["--resume", resume_id] if resume_id else []),
        ]
    if worker == "agy":
        if full_access:
            access = ["--dangerously-skip-permissions"]
        else:
            access = ["--mode", "accept-edits", "--sandbox"]
            if allow_tools:
                rules = [agy_rule(entry) for entry in allow_tools]
                access += ["--settings", json.dumps({"permissions": {"allow": rules}}, separators=(",", ":"))]
        return [
            binary,
            *(["--conversation", resume_id] if resume_id else []),
            *access,
            "--print-timeout",
            f"{timeout_seconds}s",
            "--output-format",
            "json",
            *model_args,
            *(["--effort", effort] if effort else []),
            f"-p={prompt}",
        ]
    raise RunnerError(f"unsupported worker: {worker}")


def agy_rule(entry: str) -> str:
    match = re.fullmatch(r"command\((.*)\)", check_allow_tool(entry))
    program = match.group(1) if match else entry
    if not PROGRAM_NAME.fullmatch(program):
        raise RunnerError(f"--allow-tool for agy must be a program name or command(<program>): {entry!r}")
    return f"command({program})"


def stop_group(process: subprocess.Popen) -> None:
    """SIGTERM the worker's process group, then SIGKILL, then a bounded reap."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        if sig == signal.SIGTERM:
            try:
                process.wait(timeout=TERM_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                pass
    try:
        process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        # A detached child may still hold pipes; close ours so the runner can never hang.
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe:
                pipe.close()


def run_watched(
    command: list[str],
    cwd: Path,
    stdin_path: Path | None,
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: int,
    on_start=None,
    log_mode: str = "wb",
) -> tuple[int, bool]:
    """Run with output in files (visible while running); return (exit code, timed_out)."""
    stdin = open(stdin_path, "rb") if stdin_path else subprocess.DEVNULL
    try:
        with open(stdout_path, "wb") as out, open(stderr_path, log_mode) as err:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdin=stdin,
                stdout=out,
                stderr=err,
                start_new_session=True,
                shell=False,
            )
    finally:
        if stdin_path:
            stdin.close()
    if on_start:
        on_start(process.pid)
    try:
        return process.wait(timeout=timeout_seconds), False
    except subprocess.TimeoutExpired:
        stop_group(process)
        return 124, True


# ---------------------------------------------------------------- results


def final_message_path(worker: str, task: Path) -> Path:
    return task_file(task, "last.md" if worker == "codex" else "result.json")


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def codex_thread_id(events: str) -> str | None:
    for line in events.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "thread.started" and event.get("thread_id"):
            return str(event["thread_id"])
    return None


def read_result(worker: str, task: Path) -> dict[str, object]:
    """Extract the final message, session id and error signals from the worker's output files."""
    result = {"message": "", "session_id": None, "malformed": False, "error": False, "refused": False, "denied": []}
    if worker == "codex":
        result["message"] = read_text(task_file(task, "last.md"))
        result["session_id"] = codex_thread_id(read_text(task_file(task, "events.jsonl")))
        return result
    raw = read_text(task_file(task, "result.json"))
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        result["malformed"] = True
        return result
    if worker == "claude":
        subtype = str(data.get("subtype") or "")
        result.update(
            message=str(data.get("result") or ""),
            session_id=data.get("session_id"),
            error=data.get("is_error") is True or subtype.startswith("error"),
            refused=data.get("stop_reason") == "refusal",
        )
    else:
        denied = data.get("denied_actions")
        result.update(
            message=str(data.get("response") or ""),
            session_id=data.get("conversation_id"),
            denied=denied if isinstance(denied, list) else [],
        )
    if result["session_id"] is not None:
        result["session_id"] = str(result["session_id"])
    return result


def classify(returncode: int, timed_out: bool, result: dict[str, object], log: str = "") -> str:
    if timed_out or returncode == 124:
        return "timeout"
    lowered = log.lower()
    if result.get("denied") or all(marker in lowered for marker in PERMISSION_DENIAL_MARKERS):
        return "permission"
    if result.get("refused"):
        return "refusal"
    if returncode != 0 or result.get("malformed") or result.get("error"):
        return "failed"
    if not str(result.get("message") or "").strip():
        return "empty"
    return "ok"


def handoff_fields(message: str) -> dict[str, str | None]:
    """The worker's Status, Outcome and Next step lines; None when absent (unverified)."""
    fields = {}
    for key, label in (("worker_status", "Status"), ("outcome", "Outcome"), ("next_step", "Next step")):
        # [ \t]* (not \s*) so an empty field cannot swallow the next line.
        matches = re.findall(rf"^[ \t]*\*\*{label}:\*\*[ \t]*(.+?)[ \t]*$", message, re.MULTILINE)
        fields[key] = matches[-1] if matches else None
    return fields


# ---------------------------------------------------------------- run / resume


def execute(layout: Layout, note: dict[str, str], command: list[str], stdin_path: Path | None, timeout: int, log_mode: str) -> dict[str, object]:
    worker, task = note["worker"], Path(note["task"])
    stdout_name = "events.jsonl" if worker == "codex" else "result.json"
    log_path = task_file(task, "worker.log")
    append = log_mode == "ab"
    # Unlinking first also drops a stale last.md, so an old message cannot pass as this run's answer.
    for name in ("last.md", stdout_name):
        prepare_output(task_file(task, name))
    prepare_output(log_path, append=append)
    log_offset = log_path.stat().st_size if append and log_path.exists() else 0
    note.update(started=now_utc(), pid="", status="starting", timeout_seconds=str(timeout))
    write_note(layout, note)

    def started(pid: int) -> None:
        note.update(pid=str(pid), status="running")
        write_note(layout, note)

    try:
        returncode, timed_out = run_watched(
            command,
            layout.worktree,
            stdin_path,
            task_file(task, stdout_name),
            task_file(task, "worker.log"),
            timeout,
            started,
            log_mode,
        )
    except OSError as exc:
        note["status"] = "failed"
        write_note(layout, note)
        raise RunnerError(f"could not start {worker}: {exc}") from exc
    result = read_result(worker, task)
    with open(log_path, "rb") as handle:  # only this invocation's stderr, not an earlier run's denial
        handle.seek(log_offset)
        log = handle.read().decode("utf-8", errors="replace")
    status = classify(returncode, timed_out, result, log)
    if result["session_id"]:
        note["session_id"] = check_token(str(result["session_id"]), "session id from worker output")
    note["status"] = status
    write_note(layout, note)
    return {
        "status": status,
        "exit_code": returncode,
        "worker": worker,
        "access": note["access"],
        "slug": layout.slug,
        "session_id": note.get("session_id") or None,
        "worktree": str(layout.worktree),
        "branch": layout.branch,
        "final_message_path": str(final_message_path(worker, task)),
        "worker_log_path": str(task_file(task, "worker.log")),
        **({"denied_actions": result["denied"]} if result["denied"] else {}),
        **handoff_fields(str(result["message"])),
    }


def read_input(path: Path, label: str) -> str:
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise RunnerError(f"{label} is empty: {path}")
    return text


def cmd_start(args: argparse.Namespace) -> dict[str, object]:
    repo = require_repo_root(args.repo_root)
    layout = Layout(repo, args.slug)
    if args.timeout_seconds <= 0:
        raise RunnerError("--timeout-seconds must be greater than zero")
    allow_tools = [check_allow_tool(entry) for entry in args.allow_tool]
    task = check_task_dir(args.task_dir, repo)
    brief = read_input(args.brief_file, "--brief-file")
    binary = resolve_binary(args.worker, args.binary)
    # Validate every flag before touching the repository.
    command = build_command(
        args.worker,
        binary,
        full_access=args.full_access,
        task=task,
        timeout_seconds=args.timeout_seconds,
        prompt=brief,
        allow_tools=allow_tools,
        model=args.model,
        effort=args.effort,
        max_budget_usd=args.max_budget_usd,
    )
    if git(repo, "check-ignore", "-q", ".delegate-code/x", check=False).returncode != 0:
        raise RunnerError(".delegate-code/ is not ignored by Git; add it to .git/info/exclude or .gitignore with the user's approval")
    if layout.note.exists() or layout.note.is_symlink():
        raise RunnerError(f"run note already exists: {layout.note}")
    if args.existing_worktree:
        # The caller created the worktree in step 4 and ran the baseline check in it.
        require_registered_worktree(layout)
    else:
        if os.path.lexists(layout.worktree):
            raise RunnerError(f"{layout.worktree} already exists; pass --existing-worktree to use a fresh worktree you created")
        if layout.branch_exists():
            raise RunnerError(f"branch {layout.branch} already exists")
    brief_path = task_file(task, "brief.md")
    if args.brief_file.resolve() != brief_path:
        atomic_write(task, "brief.md", brief)
    if not args.existing_worktree:
        git(repo, "worktree", "add", "-b", layout.branch, str(layout.worktree), "HEAD")
    note = {
        "worker": args.worker,
        "task": str(task),
        "access": "full" if args.full_access else "restricted",
        "session_id": "",
        "model": args.model or "",
        "effort": args.effort or "",
        "allow_tools": json.dumps(allow_tools),
        "max_budget_usd": "" if args.max_budget_usd is None else f"{args.max_budget_usd:g}",
    }
    stdin_path = None if args.worker == "agy" else brief_path
    return execute(layout, note, command, stdin_path, args.timeout_seconds, "wb")


def load_note(layout: Layout) -> tuple[dict[str, str], Path]:
    note = read_note(layout)
    if note is None:
        raise RunnerError(f"run note missing: {layout.note}")
    if note.get("worker") not in WORKERS or note.get("access") not in ("restricted", "full") or not note.get("task"):
        raise RunnerError(f"run note is incomplete or invalid: {layout.note}")
    return note, check_task_dir(Path(note["task"]), layout.repo)


def cmd_resume(args: argparse.Namespace) -> dict[str, object]:
    repo = require_repo_root(args.repo_root)
    layout = Layout(repo, args.slug)
    if args.timeout_seconds <= 0:
        raise RunnerError("--timeout-seconds must be greater than zero")
    note, task = load_note(layout)
    session = note.get("session_id")
    if not session:
        raise RunnerError(f"run note has no session_id to resume: {layout.note}")
    check_token(session, "session id in run note")
    require_registered_worktree(layout)
    if note.get("max_budget_usd") and args.max_budget_usd is None:
        raise RunnerError(
            f"this task was started with --max-budget-usd {note['max_budget_usd']}; "
            "pass --max-budget-usd with the remaining budget for the correction"
        )
    if pid_alive(note.get("pid", "")):
        raise RunnerError(f"worker for {layout.slug} is still running (pid {note['pid']})")
    fix = read_input(args.fix_file, "--fix-file")
    try:
        stored = json.loads(note.get("allow_tools") or "[]")
    except ValueError as exc:
        raise RunnerError("run note allow_tools is not valid JSON") from exc
    allow_tools = list(dict.fromkeys([*map(check_allow_tool, stored), *map(check_allow_tool, args.allow_tool)]))
    model = args.model or note.get("model") or None
    effort = args.effort or note.get("effort") or None
    command = build_command(
        note["worker"],
        resolve_binary(note["worker"], args.binary),
        full_access=note["access"] == "full",
        task=task,
        timeout_seconds=args.timeout_seconds,
        prompt=fix,
        allow_tools=allow_tools,
        model=model,
        effort=effort,
        max_budget_usd=args.max_budget_usd,
        resume_id=session,
    )
    note.update(model=model or "", effort=effort or "", allow_tools=json.dumps(allow_tools))
    if args.max_budget_usd is not None:
        note["max_budget_usd"] = f"{args.max_budget_usd:g}"
    stdin_path = None if note["worker"] == "agy" else args.fix_file.resolve()
    return execute(layout, note, command, stdin_path, args.timeout_seconds, "ab")


# ---------------------------------------------------------------- status


def pid_alive(pid: str) -> bool:
    if not pid.isdigit() or int(pid) <= 0:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def last_codex_event(events_path: Path) -> dict[str, str] | None:
    try:
        with open(events_path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 65536))
            tail = handle.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return None
    for line in reversed(tail.splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        changes = item.get("changes") if isinstance(item.get("changes"), list) else []
        summary = (
            item.get("command")
            or ", ".join(str(c.get("path")) for c in changes if isinstance(c, dict))
            or str(item.get("text") or "")[:160]
            or item.get("type")
            or ""
        )
        return {"type": str(event.get("type") or ""), "summary": str(summary)}
    return None


def inspect(layout: Layout) -> dict[str, object]:
    """Read-only state of one delegation: running, finished, abandoned or unknown."""
    note = read_note(layout)
    worktree_exists = layout.worktree.is_dir()
    try:
        require_registered_worktree(layout)
        registered = True
    except RunnerError:
        registered = False
    entry: dict[str, object] = {
        "slug": layout.slug,
        "worktree_exists": worktree_exists,
        "registered_worktree": registered,
        "note": note is not None,
    }
    if registered:
        changed = git(layout.worktree, "--no-optional-locks", "status", "--short", check=False).stdout
        entry["changed_files"] = [line for line in changed.splitlines() if line.strip()]
    if note is None:
        entry["state"] = "unknown"
        return entry
    worker = note.get("worker", "")
    entry.update(worker=worker, run_status=note.get("status") or None, session_id=note.get("session_id") or None)
    elapsed = None
    try:
        started = datetime.strptime(note.get("started", ""), TIME_FORMAT).replace(tzinfo=UTC)
        elapsed = int((datetime.now(UTC) - started).total_seconds())
    except ValueError:
        pass
    entry["elapsed_seconds"] = elapsed
    has_final = False
    if worker in WORKERS and note.get("task"):
        try:
            task = Path(note["task"]).resolve()
            final = final_message_path(worker, task)
            has_final = final.is_file() and final.stat().st_size > 0
            if worker == "codex":
                entry["last_event"] = last_codex_event(task_file(task, "events.jsonl"))
        except (RunnerError, OSError):
            pass
    timeout = int(note["timeout_seconds"]) if note.get("timeout_seconds", "").isdigit() else DEFAULT_TIMEOUT_SECONDS
    if pid_alive(note.get("pid", "")):
        state = "running"
    elif has_final:
        state = "finished"
    elif elapsed is not None and elapsed > timeout:
        state = "abandoned"
    else:
        state = "unknown"
    entry["state"] = state
    return entry


def cmd_status(args: argparse.Namespace) -> dict[str, object]:
    repo = require_repo_root(args.repo_root)
    base = inside(repo / ".delegate-code", repo)
    if args.slug:
        slugs = [check_slug(args.slug)]
    else:
        names = set()
        if base.is_dir():
            for child in base.iterdir():
                if child.name.endswith(".run") and child.is_file():
                    names.add(child.name[:-4])
                elif child.is_dir() and not child.name.endswith(".review"):
                    names.add(child.name)
        slugs = sorted(name for name in names if SLUG.fullmatch(name) and ".." not in name)
    return {"status": "ok", "delegations": [inspect(Layout(repo, slug)) for slug in slugs]}


# ---------------------------------------------------------------- bundle / apply / cleanup


def fingerprint(layout: Layout) -> str:
    # Verified first, so `git add -A` can never run in (and stage) the main checkout.
    require_registered_worktree(layout)
    git(layout.worktree, "add", "-A")
    return git(layout.worktree, "write-tree").stdout.strip()


def tree_patch(layout: Layout, tree: str) -> bytes:
    """Patch from immutable objects: later index edits cannot change what the hash covers."""
    return git(layout.worktree, "diff", "--binary", "HEAD", tree, text=False).stdout


def tree_files(layout: Layout, tree: str) -> list[str]:
    return [line for line in git(layout.worktree, "diff", "--name-only", "HEAD", tree).stdout.splitlines() if line]


def cmd_bundle(args: argparse.Namespace) -> dict[str, object]:
    repo = require_repo_root(args.repo_root)
    layout = Layout(repo, args.slug)
    note, task = load_note(layout)
    brief = read_text(args.brief_file) if args.brief_file else read_text(task_file(task, "brief.md"))
    if not brief.strip():
        raise RunnerError("brief is missing or empty; pass --brief-file")
    tree = fingerprint(layout)
    diff = tree_patch(layout, tree)
    if layout.review.is_symlink() or (layout.review.exists() and not layout.review.is_dir()):
        raise RunnerError(f"review path is not a plain directory: {layout.review}")
    layout.review.mkdir(exist_ok=True)
    review = inside(layout.review, layout.base)
    atomic_write(review, "brief.md", brief)
    atomic_write(review, "final.md", str(read_result(note["worker"], task)["message"]))
    atomic_write(review, "diff.patch", diff)
    dirty = git(repo, "status", "--porcelain").stdout.strip() != ""
    return {
        "status": "ok",
        "slug": layout.slug,
        "fingerprint": tree,
        "review_dir": str(review),
        "changed_files": tree_files(layout, tree),
        "main_checkout_dirty": dirty,
    }


def cmd_apply(args: argparse.Namespace) -> dict[str, object]:
    if not args.confirm:
        raise RunnerError("apply changes the main checkout; rerun with --confirm after the user approves")
    if not FINGERPRINT.fullmatch(args.expected_fingerprint):
        raise RunnerError("--expected-fingerprint must be a git tree hash")
    repo = require_repo_root(args.repo_root)
    layout = Layout(repo, args.slug)
    actual = fingerprint(layout)
    if actual != args.expected_fingerprint:
        raise RunnerError(
            f"fingerprint mismatch: expected {args.expected_fingerprint}, worktree is {actual}; "
            "review the new changes before applying. Main checkout not modified"
        )
    patch = tree_patch(layout, actual)
    if not patch.strip():
        raise RunnerError("worktree has no changes to apply")
    git(repo, "apply", "--check", input=patch)
    git(repo, "apply", input=patch)
    return {"status": "applied", "slug": layout.slug, "fingerprint": actual, "changed_files": tree_files(layout, actual)}


def cmd_cleanup(args: argparse.Namespace) -> dict[str, object]:
    if not args.confirm:
        raise RunnerError("cleanup removes the worktree and branch; rerun with --confirm after the user approves")
    repo = require_repo_root(args.repo_root)
    layout = Layout(repo, args.slug)
    state = inspect(layout)["state"]
    if state not in ("finished", "abandoned"):
        raise RunnerError(f"refusing to clean up {layout.slug}: state is {state}")
    removed = []
    if os.path.lexists(layout.worktree):
        require_registered_worktree(layout)
        git(repo, "worktree", "remove", "--force", str(layout.worktree))
        removed.append(str(layout.worktree))
    if layout.note.is_file() or layout.note.is_symlink():
        layout.note.unlink()
        removed.append(str(layout.note))
    if layout.review.is_symlink():
        layout.review.unlink()
        removed.append(str(layout.review))
    elif layout.review.is_dir():
        shutil.rmtree(inside(layout.review, layout.base))
        removed.append(str(layout.review))
    if layout.branch_exists():
        git(repo, "branch", "-D", layout.branch)
        removed.append(layout.branch)
    return {"status": "cleaned", "slug": layout.slug, "previous_state": state, "removed": removed}


# ---------------------------------------------------------------- CLI


class JsonArgumentParser(argparse.ArgumentParser):
    """Route invalid arguments through the JSON error reply instead of argparse's exit."""

    def error(self, message: str):
        raise RunnerError(message)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = JsonArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, slug_required: bool = True) -> None:
        p.add_argument("--slug", required=slug_required)
        p.add_argument("--repo-root", type=Path, default=Path.cwd())

    def worker_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--allow-tool", action="append", default=[], help="restricted-mode Claude --allowedTools entry or agy program")
        p.add_argument("--model")
        p.add_argument("--effort", choices=EFFORT_LEVELS)
        p.add_argument("--max-budget-usd", type=float, help="claude only")
        p.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
        p.add_argument("--binary", help="explicit worker binary path (default: resolved on PATH)")

    start = sub.add_parser("start", help="create the worktree and run the worker")
    common(start)
    start.add_argument("--worker", choices=WORKERS, required=True)
    start.add_argument("--brief-file", type=Path, required=True)
    start.add_argument("--task-dir", type=Path, required=True)
    start.add_argument("--full-access", action="store_true")
    start.add_argument(
        "--existing-worktree",
        action="store_true",
        help="use the registered worktree already created on delegate-code/<slug> (after the baseline check)",
    )
    worker_flags(start)

    resume = sub.add_parser("resume", help="send one correction to the same session")
    common(resume)
    resume.add_argument("--fix-file", type=Path, required=True)
    worker_flags(resume)

    status = sub.add_parser("status", help="read-only state of delegations")
    common(status, slug_required=False)

    bundle = sub.add_parser("bundle", help="write the review bundle and fingerprint")
    common(bundle)
    bundle.add_argument("--brief-file", type=Path)

    apply = sub.add_parser("apply", help="apply the reviewed diff to the main checkout")
    common(apply)
    apply.add_argument("--expected-fingerprint", required=True)
    apply.add_argument("--confirm", action="store_true")

    cleanup = sub.add_parser("cleanup", help="remove the worktree, run note, review bundle and branch")
    common(cleanup)
    cleanup.add_argument("--confirm", action="store_true")
    return parser.parse_args(argv)


COMMANDS = {
    "start": cmd_start,
    "resume": cmd_resume,
    "status": cmd_status,
    "bundle": cmd_bundle,
    "apply": cmd_apply,
    "cleanup": cmd_cleanup,
}


def main(argv: list[str] | None = None) -> int:
    command = "args"
    try:
        args = parse_args(argv)
        command = args.command
        output = COMMANDS[command](args)
    except (RunnerError, OSError, UnicodeError) as exc:
        print(f"run_worker {command}: {exc}", file=sys.stderr)
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True))
        return ERROR_EXIT_CODE
    print(json.dumps(output, sort_keys=True))
    return STATUS_EXIT_CODES.get(str(output["status"]), 0)


if __name__ == "__main__":
    raise SystemExit(main())
