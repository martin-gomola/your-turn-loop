#!/usr/bin/env python3
"""Run one bounded, read-only external-model review and preserve an audit artifact."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path


COMPLETION_AUTHORITY = (
    "This read-only review is already authorized. Begin immediately; do not "
    "present a plan and do not ask for confirmation. Return the completed "
    "review in this response. Do not modify files or external systems."
)
PROTOCOL_MARKERS = (
    "shall i proceed",
    "should i proceed",
    "would you like me to proceed",
    "would you like me to continue",
    "do you want me to proceed",
    "please confirm",
    "once you confirm",
    "waiting for confirmation",
)
PERMISSION_DENIAL_MARKERS = (
    'required the "command" permission',
    "headless mode cannot prompt",
)
BINARIES = {"claude": "claude", "gemini": "agy", "codex": "codex"}
SESSION_ID = re.compile(r"[0-9a-fA-F-]{8,64}")
MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
STATUS_EXIT_CODES = {
    "ok": 0,
    "empty": 65,
    "protocol": 66,
    "permission": 77,
    "refusal": 78,
    "failed": 1,
    "timeout": 124,
}


class AdvisorError(RuntimeError):
    """Raised when the advisor cannot be run without violating the contract."""


# Headless Antigravity auto-denies terminal commands, so asking for file tools up front saves the wasted first attempt.
GEMINI_READ_ONLY = (
    "Read files with your native read-only file tools and use the supplied context; do not run "
    "terminal commands. Where evidence would need a command, state the uncertainty instead."
)


def with_completion_authority(prompt: str, provider: str = "") -> str:
    prefix = f"{COMPLETION_AUTHORITY} {GEMINI_READ_ONLY}" if provider == "gemini" else COMPLETION_AUTHORITY
    return f"{prefix}\n\n{prompt.strip()}"


def classify_result(returncode: int, stdout: str, stderr: str, timed_out: bool) -> str:
    if timed_out:
        return "timeout"
    lowered_stderr = stderr.lower()
    if all(marker in lowered_stderr for marker in PERMISSION_DENIAL_MARKERS):
        return "permission"
    if returncode != 0:
        return "failed"
    normalized = stdout.strip()
    if not normalized:
        return "empty"
    lowered = normalized.lower()
    if len(normalized) <= 2_000 and any(
        marker in lowered for marker in PROTOCOL_MARKERS
    ):
        return "protocol"
    return "ok"


def build_command(
    provider: str,
    binary: str,
    timeout_seconds: int,
    prompt: str,
    resume: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> list[str]:
    if provider == "claude":
        # The session is saved so a later call can continue it with --resume <session_id>.
        return [
            binary,
            "-p",
            "--permission-mode",
            "plan",
            "--output-format",
            "json",
            *(["--model", model] if model else []),
            *(["--effort", effort] if effort else []),
            *(["--resume", resume] if resume else []),
            prompt,
        ]
    if resume:
        raise AdvisorError("--resume is supported only for the claude provider")
    if provider == "gemini":
        return [
            binary,
            "--mode",
            "plan",
            "--sandbox",
            "--print-timeout",
            f"{timeout_seconds}s",
            "--output-format",
            "text",
            *(["--model", model] if model else []),
            *(["--effort", effort] if effort else []),
            f"-p={prompt}",
        ]
    if provider == "codex":
        # Plain-text mode prints only the final message on stdout; progress goes to stderr.
        return [
            binary,
            "exec",
            "--sandbox",
            "read-only",
            "--ephemeral",
            *(["--model", model] if model else []),
            *(["-c", f'model_reasoning_effort="{effort}"'] if effort else []),
            prompt,
        ]
    raise AdvisorError(f"unsupported provider: {provider}")


def artifact_path(repo_root: Path, provider: str, task: str, now: datetime) -> Path:
    words = re.findall(r"[a-z0-9]+", task.lower())[:8]
    slug = "-".join(words)[:64].strip("-") or "review"
    timestamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return repo_root / ".second-opinion" / f"second-opinion-{provider}-{slug}-{timestamp}.md"


def require_repo_root(repo_root: Path) -> Path:
    resolved = repo_root.expanduser().resolve()
    result = subprocess.run(
        ["git", "-C", str(resolved), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AdvisorError(f"not a Git repository: {resolved}")
    actual = Path(result.stdout.strip()).resolve()
    if actual != resolved:
        raise AdvisorError(f"repo root must be {actual}, got {resolved}")
    return resolved


def require_ignored_artifact(repo_root: Path, candidate: Path) -> None:
    try:
        relative = candidate.resolve().relative_to(repo_root.resolve())
    except ValueError as exc:
        raise AdvisorError(f"artifact escapes repository root: {candidate}") from exc
    result = subprocess.run(
        ["git", "-C", str(repo_root), "check-ignore", "--quiet", "--", str(relative)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AdvisorError(
            f"artifact path is not ignored by Git: {relative}; refusing to write review output. "
            "Add .second-opinion/ to .gitignore or .git/info/exclude with the user's approval"
        )


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def parse_claude_json(stdout: str) -> tuple[str, str | None, bool, bool]:
    """Return (answer text, session id, is_error, refused) from `claude -p --output-format json`; raw stdout if not JSON."""
    try:
        data = json.loads(stdout)
    except ValueError:
        return stdout, None, False, False
    if not isinstance(data, dict):
        return stdout, None, False, False
    refused = data.get("stop_reason") == "refusal" or data.get("subtype") == "refusal"
    return str(data.get("result") or ""), data.get("session_id"), data.get("is_error") is True, refused


def run_once(
    command: list[str], repo_root: Path, timeout_seconds: int, provider: str = ""
) -> dict[str, object]:
    # Own process group and no stdin: a timeout kills the CLI and every child it
    # started, and the CLI can never block waiting for terminal input.
    process = subprocess.Popen(
        command,
        cwd=repo_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        timed_out = False
        returncode = process.returncode
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
        stdout, stderr = _text(stdout), _text(stderr)
        timed_out = True
        returncode = 124
    session_id = None
    is_error = refused = False
    if provider == "claude":
        stdout, session_id, is_error, refused = parse_claude_json(stdout)
    status = classify_result(returncode, stdout, stderr, timed_out)
    if refused and status != "timeout":
        status = "refusal"
    elif is_error and status != "timeout":
        status = "failed"
    return {
        "status": status,
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "session_id": session_id,
    }


def _unique_artifact(candidate: Path) -> Path:
    if not candidate.exists():
        return candidate
    for suffix in range(2, 1_000):
        alternate = candidate.with_name(f"{candidate.stem}-{suffix}{candidate.suffix}")
        if not alternate.exists():
            return alternate
    raise AdvisorError(f"could not allocate unique artifact path beside {candidate}")


def _display_command(command: list[str]) -> str:
    prompt_arg = (
        "-p=<final prompt>" if command[-1].startswith("-p=") else "<final prompt>"
    )
    return shlex.join([*command[:-1], prompt_arg])


def write_artifact(
    candidate: Path,
    task: str,
    provider: str,
    prompt: str,
    command: list[str],
    attempts: list[dict[str, object]],
) -> Path:
    path = _unique_artifact(candidate)
    path.parent.mkdir(parents=True, exist_ok=True)
    final = attempts[-1]
    action = {
        "ok": "Verify material advisor claims before acting on them.",
        "timeout": "Review partial output or rerun explicitly with a larger timeout.",
        "empty": "Inspect stderr and CLI authentication; no completed opinion was returned.",
        "protocol": "The bounded protocol retry did not produce a completed opinion.",
        "permission": (
            "A headless command permission blocked the completed opinion. Inspect stderr and run "
            "interactively to identify the exact command; change permissions only with explicit "
            "user authorization. Do not add a broad allow rule or use a dangerous bypass."
        ),
        "failed": "Inspect stderr and resolve the reported CLI failure before rerunning.",
        "refusal": (
            "The provider's safety classifier declined the request. Check the prompt for "
            "requests to write out internal reasoning; do not retry the same prompt unchanged."
        ),
    }[str(final["status"])]
    sections = [
        "# External Advisor Audit",
        "",
        "## Original User Task",
        "",
        task,
        "",
        "## Provider and Final Prompt",
        "",
        f"Provider: `{provider}`",
        "",
        prompt,
        "",
        "## Command Shape",
        "",
        f"`{_display_command(command)}`",
        "",
        "## Execution Summary",
        "",
        f"Status: `{final['status']}`",
        f"Attempts: `{len(attempts)}`",
        f"Final exit status: `{final['returncode']}`",
        *([f"Session: `{final['session_id']}`"] if final.get("session_id") else []),
        "",
        "## Action Items",
        "",
        action,
    ]
    for index, attempt in enumerate(attempts, start=1):
        sections.extend(
            [
                "",
                f"## Attempt {index}",
                "",
                f"Status: `{attempt['status']}`",
                f"Exit status: `{attempt['returncode']}`",
                "",
                "### Raw stdout",
                "",
                "`````text",
                str(attempt["stdout"]),
                "`````",
                "",
                "### Raw stderr",
                "",
                "`````text",
                str(attempt["stderr"]),
                "`````",
            ]
        )
    path.write_text("\n".join(sections).rstrip() + "\n", encoding="utf-8")
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=tuple(BINARIES), required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--task-file", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--protocol-retries", type=int, choices=(0, 1), default=1)
    parser.add_argument("--resume", help="continue an earlier claude session by its session_id")
    parser.add_argument("--model", help="optional provider model name or alias")
    parser.add_argument("--effort", choices=EFFORT_LEVELS, help="optional provider reasoning effort level")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.timeout_seconds <= 0:
            raise AdvisorError("timeout must be greater than zero")
        repo_root = require_repo_root(args.repo_root)
        task = args.task_file.read_text(encoding="utf-8").strip()
        requested_prompt = args.prompt_file.read_text(encoding="utf-8").strip()
        if not task or not requested_prompt:
            raise AdvisorError("task and prompt files must not be empty")
        if args.resume and not SESSION_ID.fullmatch(args.resume):
            raise AdvisorError("--resume must be a session_id from an earlier second-opinion result")
        if args.model and not MODEL_NAME.fullmatch(args.model):
            raise AdvisorError("--model must be a simple model name or alias")
        binary_name = BINARIES[args.provider]
        binary = shutil.which(binary_name)
        if binary is None:
            raise AdvisorError(f"required provider binary is missing: {binary_name}")

        candidate = artifact_path(repo_root, args.provider, task, datetime.now(UTC))
        require_ignored_artifact(repo_root, candidate)
        final_prompt = with_completion_authority(requested_prompt, args.provider)
        command = build_command(
            args.provider,
            binary,
            args.timeout_seconds,
            final_prompt,
            args.resume,
            args.model,
            args.effort,
        )
        attempts = [run_once(command, repo_root, args.timeout_seconds, args.provider)]
        # A retry continues the same conversation, so the caller ends up with one session id.
        session = attempts[-1]["session_id"] or args.resume
        if attempts[-1]["status"] == "protocol" and args.protocol_retries == 1:
            retry_prompt = (
                "Protocol correction: return the completed read-only review now. "
                "Do not ask questions or request confirmation.\n\n" + final_prompt
            )
            command = build_command(
                args.provider,
                binary,
                args.timeout_seconds,
                retry_prompt,
                session,
                args.model,
                args.effort,
            )
            attempts.append(run_once(command, repo_root, args.timeout_seconds, args.provider))
        elif attempts[-1]["status"] == "permission":
            retry_prompt = (
                "Permission recovery: return the completed read-only review now without running "
                "terminal commands or requesting new permissions. Use native read-only file tools "
                "and the supplied context only. If evidence is unavailable, state the uncertainty "
                "instead of invoking a command. Do not modify permission settings and do not use "
                "a dangerous permission bypass.\n\n" + final_prompt
            )
            command = build_command(
                args.provider,
                binary,
                args.timeout_seconds,
                retry_prompt,
                session,
                args.model,
                args.effort,
            )
            attempts.append(run_once(command, repo_root, args.timeout_seconds, args.provider))

        path = write_artifact(
            candidate, task, args.provider, final_prompt, command, attempts
        )
        final = attempts[-1]
        print(
            json.dumps(
                {
                    "status": final["status"],
                    "provider": args.provider,
                    "artifact": str(path),
                    "attempts": len(attempts),
                    "exit_status": final["returncode"],
                    "session_id": final["session_id"] or session,
                },
                sort_keys=True,
            )
        )
        return STATUS_EXIT_CODES[str(final["status"])]
    except (AdvisorError, OSError, UnicodeError) as exc:
        print(
            json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True),
            file=sys.stderr,
        )
        return STATUS_EXIT_CODES["failed"]


if __name__ == "__main__":
    raise SystemExit(main())
