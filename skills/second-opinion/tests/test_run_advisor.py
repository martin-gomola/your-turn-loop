from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "run_advisor.py"
SPEC = importlib.util.spec_from_file_location("ask_run_advisor", SCRIPT_PATH)
assert SPEC and SPEC.loader
run_advisor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(run_advisor)


def test_classifies_success_empty_protocol_permission_and_failure() -> None:
    assert (
        run_advisor.classify_result(0, "Finding: missing timeout handling.", "", False)
        == "ok"
    )
    assert run_advisor.classify_result(0, "  \n", "", False) == "empty"
    assert (
        run_advisor.classify_result(
            0,
            "",
            'a tool required the "command" permission that headless mode cannot prompt for',
            False,
        )
        == "permission"
    )
    assert (
        run_advisor.classify_result(0, "I can review this. Shall I proceed?", "", False)
        == "protocol"
    )
    assert run_advisor.classify_result(2, "partial output", "", False) == "failed"
    assert run_advisor.classify_result(0, "partial output", "", True) == "timeout"


def test_builds_read_only_provider_commands() -> None:
    claude = run_advisor.build_command("claude", "/bin/claude", 600, "Review this")
    assert claude == [
        "/bin/claude",
        "-p",
        "--permission-mode",
        "plan",
        "--output-format",
        "json",
        "Review this",
    ]
    resumed = run_advisor.build_command("claude", "/bin/claude", 600, "Next", "abc-123")
    assert resumed[-3:] == ["--resume", "abc-123", "Next"]
    with pytest.raises(run_advisor.AdvisorError, match="only for the claude"):
        run_advisor.build_command("gemini", "/bin/agy", 600, "Next", "abc-123")

    gemini = run_advisor.build_command("gemini", "/bin/agy", 600, "Review this")
    assert gemini == [
        "/bin/agy",
        "--mode",
        "plan",
        "--sandbox",
        "--print-timeout",
        "600s",
        "--output-format",
        "text",
        "-p=Review this",
    ]

    codex = run_advisor.build_command("codex", "/bin/codex", 600, "Review this")
    assert codex == [
        "/bin/codex",
        "exec",
        "--sandbox",
        "read-only",
        "--ephemeral",
        "Review this",
    ]
    with pytest.raises(run_advisor.AdvisorError, match="only for the claude"):
        run_advisor.build_command("codex", "/bin/codex", 600, "Next", "abc-123")


def test_display_command_preserves_attached_gemini_prompt_flag() -> None:
    command = run_advisor.build_command("gemini", "/bin/agy", 600, "Review this")

    assert run_advisor._display_command(command).endswith("'-p=<final prompt>'")


def test_artifact_path_is_repo_local_and_stable() -> None:
    timestamp = datetime(2026, 8, 25, 14, 30, tzinfo=UTC)
    path = run_advisor.artifact_path(
        Path("/repo"), "claude", "Review auth flow", timestamp
    )
    assert path == Path(
        "/repo/.second-opinion/second-opinion-claude-review-auth-flow-20260825T143000Z.md"
    )


def test_requires_artifact_path_to_be_gitignored(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    candidate = tmp_path / ".second-opinion" / "result.md"

    with pytest.raises(run_advisor.AdvisorError, match="not ignored"):
        run_advisor.require_ignored_artifact(tmp_path, candidate)

    (tmp_path / ".gitignore").write_text(".second-opinion/\n", encoding="utf-8")
    run_advisor.require_ignored_artifact(tmp_path, candidate)


def test_completion_prefix_requires_immediate_read_only_result() -> None:
    prompt = run_advisor.with_completion_authority("Review PLAN.md")
    assert "already authorized" in prompt
    assert "Begin immediately" in prompt
    assert "do not ask for confirmation" in prompt
    assert prompt.endswith("Review PLAN.md")
    assert "terminal commands" not in prompt


def test_gemini_prompt_asks_for_file_tools_on_the_first_attempt() -> None:
    prompt = run_advisor.with_completion_authority("Review PLAN.md", "gemini")
    assert "native read-only file tools" in prompt
    assert "do not run terminal commands" in prompt
    assert prompt.endswith("Review PLAN.md")


def test_main_retries_protocol_once_and_writes_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(".second-opinion/\n", encoding="utf-8")
    task_file = tmp_path / "task.txt"
    prompt_file = tmp_path / "prompt.txt"
    task_file.write_text("Review auth flow", encoding="utf-8")
    prompt_file.write_text("Inspect auth.py and return findings.", encoding="utf-8")

    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    fake_counter = tmp_path.parent / f"{tmp_path.name}-counter"
    fake_claude = binary_dir / "claude"
    fake_claude.write_text(
        "#!/bin/sh\n"
        'if [ ! -f "$ASK_FAKE_COUNTER" ]; then\n'
        '  touch "$ASK_FAKE_COUNTER"\n'
        '  echo \'{"result": "I can inspect this. Shall I proceed?", "session_id": "aaaa1111"}\'\n'
        "else\n"
        '  case "$*" in *"--resume aaaa1111"*) ;; *) exit 3 ;; esac\n'
        '  echo \'{"result": "Finding: auth.py needs a timeout.", "session_id": "aaaa1111"}\'\n'
        "fi\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)
    monkeypatch.setenv("ASK_FAKE_COUNTER", str(fake_counter))
    monkeypatch.setenv("PATH", f"{binary_dir}{os.pathsep}{os.environ['PATH']}")

    exit_status = run_advisor.main(
        [
            "--provider",
            "claude",
            "--repo-root",
            str(tmp_path),
            "--task-file",
            str(task_file),
            "--prompt-file",
            str(prompt_file),
            "--timeout-seconds",
            "10",
        ]
    )

    assert exit_status == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["attempts"] == 2
    assert payload["session_id"] == "aaaa1111"
    artifact = Path(payload["artifact"])
    content = artifact.read_text(encoding="utf-8")
    assert "Session: `aaaa1111`" in content
    assert "Status: `protocol`" in content
    assert "Status: `ok`" in content
    assert "Shall I proceed?" in content
    assert "Finding: auth.py needs a timeout." in content


def test_main_retries_headless_permission_once_without_changing_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(".second-opinion/\n", encoding="utf-8")
    task_file = tmp_path / "task.txt"
    prompt_file = tmp_path / "prompt.txt"
    task_file.write_text("Review auth flow", encoding="utf-8")
    prompt_file.write_text("Inspect auth.py and return findings.", encoding="utf-8")

    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    fake_counter = tmp_path.parent / f"{tmp_path.name}-permission-counter"
    fake_agy = binary_dir / "agy"
    fake_agy.write_text(
        "#!/bin/sh\n"
        'if [ ! -f "$ASK_FAKE_COUNTER" ]; then\n'
        '  touch "$ASK_FAKE_COUNTER"\n'
        '  echo \'jetski: no output produced - a tool required the "command" permission '
        "that headless mode cannot prompt for, so it was auto-denied.' >&2\n"
        "else\n"
        '  case "$*" in\n'
        '    *"Permission recovery: return the completed read-only review now without running '
        'terminal commands"*) echo "Finding: use a bounded timeout." ;;\n'
        '    *) echo "missing permission recovery prompt" >&2; exit 2 ;;\n'
        "  esac\n"
        "fi\n",
        encoding="utf-8",
    )
    fake_agy.chmod(0o755)
    monkeypatch.setenv("ASK_FAKE_COUNTER", str(fake_counter))
    monkeypatch.setenv("PATH", f"{binary_dir}{os.pathsep}{os.environ['PATH']}")

    exit_status = run_advisor.main(
        [
            "--provider",
            "gemini",
            "--repo-root",
            str(tmp_path),
            "--task-file",
            str(task_file),
            "--prompt-file",
            str(prompt_file),
            "--timeout-seconds",
            "10",
        ]
    )

    assert exit_status == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["attempts"] == 2
    artifact = Path(payload["artifact"])
    content = artifact.read_text(encoding="utf-8")
    assert "Status: `permission`" in content
    assert "Status: `ok`" in content
    assert "auto-denied" in content
    assert "Finding: use a bounded timeout." in content
    assert not (tmp_path / ".gemini").exists()


def test_main_stops_after_second_permission_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(".second-opinion/\n", encoding="utf-8")
    task_file = tmp_path / "task.txt"
    prompt_file = tmp_path / "prompt.txt"
    task_file.write_text("Review auth flow", encoding="utf-8")
    prompt_file.write_text("Inspect auth.py and return findings.", encoding="utf-8")

    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    fake_agy = binary_dir / "agy"
    fake_agy.write_text(
        "#!/bin/sh\n"
        "echo 'a tool required the \"command\" permission that headless mode cannot prompt for' "
        ">&2\n",
        encoding="utf-8",
    )
    fake_agy.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary_dir}{os.pathsep}{os.environ['PATH']}")

    exit_status = run_advisor.main(
        [
            "--provider",
            "gemini",
            "--repo-root",
            str(tmp_path),
            "--task-file",
            str(task_file),
            "--prompt-file",
            str(prompt_file),
            "--timeout-seconds",
            "10",
        ]
    )

    assert exit_status == 77
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "permission"
    assert payload["attempts"] == 2
    artifact = Path(payload["artifact"])
    content = artifact.read_text(encoding="utf-8")
    assert content.count("Status: `permission`") == 3
    assert "explicit user authorization" in content
    assert "Do not add a broad allow rule" in content
    assert not (tmp_path / ".gemini").exists()


def test_timeout_kills_child_processes_and_closes_stdin(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    script = tmp_path / "slow-cli"
    script.write_text(
        "#!/bin/sh\n"
        f'sleep 30 & echo $! > "{pid_file}"\n'
        "read line && echo got-stdin\n"
        "echo partial\n"
        "wait\n",
        encoding="utf-8",
    )
    script.chmod(0o755)

    result = run_advisor.run_once([str(script)], tmp_path, 2)

    assert result["status"] == "timeout"
    assert "got-stdin" not in result["stdout"]
    assert "partial" in result["stdout"]
    child = int(pid_file.read_text(encoding="utf-8"))
    deadline = datetime.now(UTC).timestamp() + 5
    while datetime.now(UTC).timestamp() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            return
    pytest.fail("child process survived the timeout")


def test_non_claude_command_with_json_argument_is_not_parsed(tmp_path: Path) -> None:
    script = tmp_path / "fake-cli"
    script.write_text('#!/bin/sh\necho \'{"result": "parsed", "session_id": "aaaa1111"}\'\n', encoding="utf-8")
    script.chmod(0o755)

    result = run_advisor.run_once([str(script), "--format", "json"], tmp_path, 10, "codex")

    assert result["status"] == "ok"
    assert '"result": "parsed"' in result["stdout"]
    assert result["session_id"] is None


def test_claude_json_is_error_with_zero_exit_is_failed(tmp_path: Path) -> None:
    script = tmp_path / "fake-claude"
    script.write_text(
        '#!/bin/sh\necho \'{"is_error": true, "subtype": "error_max_budget_usd", '
        '"result": "Budget exceeded", "session_id": "bbbb2222"}\'\n',
        encoding="utf-8",
    )
    script.chmod(0o755)

    result = run_advisor.run_once([str(script)], tmp_path, 10, "claude")

    assert result["returncode"] == 0
    assert result["status"] == "failed"
    assert result["stdout"] == "Budget exceeded"
    assert result["session_id"] == "bbbb2222"


def test_main_rejects_malformed_resume_id(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "t.txt").write_text("x", encoding="utf-8")
    args = ["--provider", "claude", "--repo-root", str(tmp_path), "--task-file", str(tmp_path / "t.txt"),
            "--prompt-file", str(tmp_path / "t.txt"), "--resume", "x; rm -rf"]
    assert run_advisor.main(args) == 1
