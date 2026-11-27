from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "run_worker.py"
SPEC = importlib.util.spec_from_file_location("delegate_run_worker", SCRIPT_PATH)
assert SPEC and SPEC.loader
rw = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rw)

# One fake CLI for all three workers; it dispatches on its own file name and FAKE_MODE.
FAKE_WORKER = r'''
import json, os, sys, time
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
mode = os.environ.get("FAKE_MODE", "ok")
log = os.environ.get("FAKE_ARGV_LOG")
if log:
    with open(log, "a") as handle:
        handle.write(json.dumps([name, *args]) + "\n")
resumed = "resume" in args or "--resume" in args or "--conversation" in args
if mode == "denylog":
    sys.stderr.write('a tool required the "command" permission that headless mode cannot prompt for\n')
if mode == "hang":
    if os.fork() == 0:
        os.setsid()  # escapes the process group and keeps stdout/stderr open
        Path(os.environ["FAKE_GRANDCHILD_PID"]).write_text(str(os.getpid()))
        time.sleep(60)
        os._exit(0)
    time.sleep(60)
Path("feature.txt").write_text("resumed\n" if resumed else "first\n")
message = "" if mode == "empty" else (
    "Did the work.\n\n**Status:** DONE\n\n**Outcome:** Added feature.txt; check passed.\n\n**Next step:** none\n"
)
if name == "codex":
    print(json.dumps({"type": "thread.started", "thread_id": "thread-1"}))
    print(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": "pytest -q"}}))
    Path(args[args.index("-o") + 1]).write_text(message)
    sys.exit(3 if mode == "fail" else 0)
if name == "claude":
    sys.stdin.read()
    if mode == "malformed":
        print("not json")
        sys.exit(0)
    print(json.dumps({
        "result": message,
        "session_id": "sess-1",
        "is_error": mode == "budget",
        "subtype": "error_max_budget_usd" if mode == "budget" else "success",
        "stop_reason": "refusal" if mode == "refusal" else "end_turn",
    }))
    sys.exit(0)
if name == "agy":
    assert any(a.startswith("-p=") for a in args)
    denied = [{"action": "command(npm)"}] if mode == "denied" else []
    print(json.dumps({
        "response": "" if denied else message,
        "conversation_id": "conv-2" if resumed else "conv-1",
        "denied_actions": denied,
    }))
'''


def run_git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    root = tmp_path.resolve()
    repo = root / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("hello\n")
    run_git(repo, "add", "README.md")
    run_git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "init")  # pii:allow
    (repo / ".git" / "info" / "exclude").write_text(".delegate-code/\n")
    task = root / "task"
    task.mkdir()
    (task / "brief.md").write_text("Add feature.txt.\n")
    (task / "fix.md").write_text("Also say resumed.\n")
    bin_dir = root / "bin"
    bin_dir.mkdir()
    for name in rw.WORKERS:
        fake = bin_dir / name
        fake.write_text(f"#!{sys.executable}\n{FAKE_WORKER}")
        fake.chmod(0o755)
    monkeypatch.setenv("FAKE_ARGV_LOG", str(root / "argv.log"))
    monkeypatch.setattr(rw, "TERM_GRACE_SECONDS", 0.5)
    return {"root": root, "repo": repo, "task": task, "bin": bin_dir}


def call(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict]:
    code = rw.main(list(argv))
    out = capsys.readouterr().out.strip().splitlines()
    return code, json.loads(out[-1])


def start(env: dict[str, Path], capsys, worker: str = "codex", slug: str = "feat", *extra: str) -> tuple[int, dict]:
    return call(
        capsys,
        "start",
        "--slug", slug,
        "--worker", worker,
        "--brief-file", str(env["task"] / "brief.md"),
        "--task-dir", str(env["task"]),
        "--repo-root", str(env["repo"]),
        "--binary", str(env["bin"] / worker),
        *extra,
    )


# ---------------------------------------------------------------- command building


def test_codex_commands_restricted_full_and_resume(tmp_path: Path) -> None:
    task = tmp_path
    last = str(task.resolve() / "last.md")
    assert rw.build_command("codex", "/bin/codex", full_access=False, task=task) == [
        "/bin/codex", "exec", "--sandbox", "workspace-write", "--json", "-o", last, "-",
    ]
    full = rw.build_command("codex", "/bin/codex", full_access=True, task=task, model="gpt-5", effort="high")
    assert full == [
        "/bin/codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "--json", "-o", last,
        "--model", "gpt-5", "-c", 'model_reasoning_effort="high"', "-",
    ]
    resumed = rw.build_command("codex", "/bin/codex", full_access=False, task=task, resume_id="thread-1")
    assert resumed == [
        "/bin/codex", "exec", "resume", "-c", 'sandbox_mode="workspace-write"', "--json", "-o", last, "thread-1", "-",
    ]
    assert "--dangerously-bypass-approvals-and-sandbox" in rw.build_command(
        "codex", "/bin/codex", full_access=True, task=task, resume_id="thread-1"
    )
    with pytest.raises(rw.RunnerError, match="only for claude"):
        rw.build_command("codex", "/bin/codex", full_access=False, task=task, max_budget_usd=2)
    with pytest.raises(rw.RunnerError, match="not supported for codex"):
        rw.build_command("codex", "/bin/codex", full_access=False, task=task, allow_tools=["Bash(npm test)"])


def test_claude_commands_restricted_full_budget_and_resume(tmp_path: Path) -> None:
    restricted = rw.build_command(
        "claude", "/bin/claude", full_access=False, task=tmp_path,
        allow_tools=["Bash(npm test)", "Bash(npm run lint)"], model="opus", effort="medium", max_budget_usd=2,
    )
    assert restricted == [
        "/bin/claude", "-p", "--permission-mode", "acceptEdits",
        "--allowedTools", "Bash(npm test)", "--allowedTools", "Bash(npm run lint)",
        "--output-format", "json", "--model", "opus", "--effort", "medium", "--max-budget-usd", "2",
    ]
    full = rw.build_command("claude", "/bin/claude", full_access=True, task=tmp_path, resume_id="sess-1")
    assert full == [
        "/bin/claude", "-p", "--permission-mode", "bypassPermissions", "--output-format", "json", "--resume", "sess-1",
    ]
    with pytest.raises(rw.RunnerError, match="restricted mode"):
        rw.build_command("claude", "/bin/claude", full_access=True, task=tmp_path, allow_tools=["Bash(ls)"])


def test_agy_commands_restricted_full_and_resume(tmp_path: Path) -> None:
    restricted = rw.build_command(
        "agy", "/bin/agy", full_access=False, task=tmp_path, timeout_seconds=900,
        prompt="Do it", allow_tools=["npm", "command(pytest)"], effort="low",
    )
    assert restricted == [
        "/bin/agy", "--mode", "accept-edits", "--sandbox",
        "--settings", '{"permissions":{"allow":["command(npm)","command(pytest)"]}}',
        "--print-timeout", "900s", "--output-format", "json", "--effort", "low", "-p=Do it",
    ]
    full = rw.build_command("agy", "/bin/agy", full_access=True, task=tmp_path, prompt="Fix", resume_id="conv-1")
    assert full == [
        "/bin/agy", "--conversation", "conv-1", "--dangerously-skip-permissions",
        "--print-timeout", "1800s", "--output-format", "json", "-p=Fix",
    ]
    with pytest.raises(rw.RunnerError, match="only for claude"):
        rw.build_command("agy", "/bin/agy", full_access=False, task=tmp_path, max_budget_usd=1)
    with pytest.raises(rw.RunnerError, match="program name"):
        rw.build_command("agy", "/bin/agy", full_access=False, task=tmp_path, allow_tools=["npm test; rm"])


def test_rejects_invalid_model_effort_slug_and_session() -> None:
    with pytest.raises(rw.RunnerError, match="--model"):
        rw.build_command("claude", "/bin/claude", full_access=False, task=Path("."), model="bad model")
    with pytest.raises(rw.RunnerError, match="--effort"):
        rw.build_command("claude", "/bin/claude", full_access=False, task=Path("."), effort="huge")
    with pytest.raises(rw.RunnerError, match="session id"):
        rw.build_command("claude", "/bin/claude", full_access=False, task=Path("."), resume_id="-rm")
    for slug in ("../x", "a..b", "-x", "a/b", "", "x" * 65, "foo.review", "foo.run"):
        with pytest.raises(rw.RunnerError, match="--slug"):
            rw.check_slug(slug)


# ---------------------------------------------------------------- start preconditions


def test_start_rejects_unignored_delegate_dir(env, capsys) -> None:
    (env["repo"] / ".git" / "info" / "exclude").write_text("")
    code, out = start(env, capsys)
    assert code == rw.ERROR_EXIT_CODE and "not ignored" in out["error"]
    assert not (env["repo"] / ".delegate-code").exists()


def test_start_rejects_invalid_slug_and_task_inside_repo(env, capsys) -> None:
    code, out = start(env, capsys, "codex", "../escape")
    assert code == rw.ERROR_EXIT_CODE and "--slug" in out["error"]
    inner = env["repo"] / "tmp-task"
    inner.mkdir()
    code, out = call(
        capsys, "start", "--slug", "feat", "--worker", "codex",
        "--brief-file", str(env["task"] / "brief.md"), "--task-dir", str(inner),
        "--repo-root", str(env["repo"]), "--binary", str(env["bin"] / "codex"),
    )
    assert code == rw.ERROR_EXIT_CODE and "outside the repository" in out["error"]
    assert run_git(env["repo"], "branch", "--list", "delegate-code/*") == ""


def test_start_rejects_symlinked_delegate_dir_and_task_file(env, capsys, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (env["task"] / "last.md").symlink_to(outside / "stolen.md")
    code, out = start(env, capsys)
    assert code == rw.ERROR_EXIT_CODE and "escapes" in out["error"]
    (env["task"] / "last.md").unlink()
    (env["repo"] / ".delegate-code").symlink_to(outside)
    code, out = start(env, capsys)
    assert code == rw.ERROR_EXIT_CODE and "escapes" in out["error"]


def test_start_rejects_existing_branch_and_budget_for_codex(env, capsys) -> None:
    code, out = start(env, capsys, "codex", "feat", "--max-budget-usd", "2")
    assert code == rw.ERROR_EXIT_CODE and "only for claude" in out["error"]
    run_git(env["repo"], "branch", "delegate-code/feat")
    code, out = start(env, capsys)
    assert code == rw.ERROR_EXIT_CODE and "already exists" in out["error"]


# ---------------------------------------------------------------- end to end


def test_codex_start_resume_bundle_apply_cleanup(env, capsys) -> None:
    repo = env["repo"]
    code, out = start(env, capsys)
    assert code == 0, out
    assert out["status"] == "ok" and out["session_id"] == "thread-1"
    assert (out["worker_status"], out["next_step"]) == ("DONE", "none")
    assert out["outcome"].startswith("Added feature.txt")
    worktree = repo / ".delegate-code" / "feat"
    assert out["worktree"] == str(worktree) and out["branch"] == "delegate-code/feat"
    note = rw.read_note(rw.Layout(repo, "feat"))
    assert note["worker"] == "codex" and note["access"] == "restricted" and note["status"] == "ok"
    assert note["task"] == str(env["task"]) and note["pid"].isdigit() and note["timeout_seconds"] == "1800"
    argv = json.loads((env["root"] / "argv.log").read_text().splitlines()[0])
    assert argv[1:4] == ["exec", "--sandbox", "workspace-write"]

    code, out = call(capsys, "resume", "--slug", "feat", "--fix-file", str(env["task"] / "fix.md"),
                     "--repo-root", str(repo), "--binary", str(env["bin"] / "codex"), "--effort", "high")
    assert code == 0 and out["status"] == "ok"
    argv = json.loads((env["root"] / "argv.log").read_text().splitlines()[1])
    assert argv[1:5] == ["exec", "resume", "-c", 'sandbox_mode="workspace-write"']
    assert argv[-2:] == ["thread-1", "-"] and 'model_reasoning_effort="high"' in argv

    code, status = call(capsys, "status", "--repo-root", str(repo))
    entry = status["delegations"][0]
    assert entry["state"] == "finished" and entry["changed_files"] == ["?? feature.txt"]
    assert entry["last_event"] == {"type": "item.completed", "summary": "pytest -q"}

    code, bundle = call(capsys, "bundle", "--slug", "feat", "--repo-root", str(repo))
    assert code == 0 and bundle["changed_files"] == ["feature.txt"] and bundle["main_checkout_dirty"] is False
    review = repo / ".delegate-code" / "feat.review"
    assert (review / "brief.md").read_text() == "Add feature.txt.\n"
    assert "**Status:** DONE" in (review / "final.md").read_text()
    assert "+resumed" in (review / "diff.patch").read_text()

    code, out = call(capsys, "apply", "--slug", "feat", "--expected-fingerprint", bundle["fingerprint"],
                     "--repo-root", str(repo))
    assert code == rw.ERROR_EXIT_CODE and "--confirm" in out["error"]

    (worktree / "feature.txt").write_text("changed after review\n")
    code, out = call(capsys, "apply", "--slug", "feat", "--expected-fingerprint", bundle["fingerprint"],
                     "--confirm", "--repo-root", str(repo))
    assert code == rw.ERROR_EXIT_CODE and "fingerprint mismatch" in out["error"]
    assert not (repo / "feature.txt").exists()

    code, bundle = call(capsys, "bundle", "--slug", "feat", "--repo-root", str(repo))
    code, out = call(capsys, "apply", "--slug", "feat", "--expected-fingerprint", bundle["fingerprint"],
                     "--confirm", "--repo-root", str(repo))
    assert code == 0 and out["status"] == "applied"
    assert (repo / "feature.txt").read_text() == "changed after review\n"
    assert worktree.is_dir()  # apply never removes the worktree

    code, out = call(capsys, "cleanup", "--slug", "feat", "--repo-root", str(repo))
    assert code == rw.ERROR_EXIT_CODE and "--confirm" in out["error"]
    code, out = call(capsys, "cleanup", "--slug", "feat", "--confirm", "--repo-root", str(repo))
    assert code == 0 and out["status"] == "cleaned"
    assert not worktree.exists() and not review.exists()
    assert not (repo / ".delegate-code" / "feat.run").exists()
    assert run_git(repo, "branch", "--list", "delegate-code/feat") == ""


def test_agy_resume_updates_conversation_and_denied_is_permission(env, capsys, monkeypatch) -> None:
    code, out = start(env, capsys, "agy", "agy-task", "--allow-tool", "npm")
    assert code == 0 and out["session_id"] == "conv-1"
    argv = json.loads((env["root"] / "argv.log").read_text().splitlines()[0])
    assert "--sandbox" in argv and '{"permissions":{"allow":["command(npm)"]}}' in argv
    assert argv[-1] == "-p=Add feature.txt.\n"

    monkeypatch.setenv("FAKE_MODE", "denied")
    code, out = call(capsys, "resume", "--slug", "agy-task", "--fix-file", str(env["task"] / "fix.md"),
                     "--repo-root", str(env["repo"]), "--binary", str(env["bin"] / "agy"), "--allow-tool", "pytest")
    assert code == rw.STATUS_EXIT_CODES["permission"] and out["status"] == "permission"
    assert out["session_id"] == "conv-2" and out["denied_actions"]
    argv = json.loads((env["root"] / "argv.log").read_text().splitlines()[1])
    assert argv[1:3] == ["--conversation", "conv-1"]
    assert '{"permissions":{"allow":["command(npm)","command(pytest)"]}}' in argv
    note = rw.read_note(rw.Layout(env["repo"], "agy-task"))
    assert note["session_id"] == "conv-2" and note["status"] == "permission"


def test_resume_requires_run_note_and_session(env, capsys) -> None:
    code, out = call(capsys, "resume", "--slug", "nope", "--fix-file", str(env["task"] / "fix.md"),
                     "--repo-root", str(env["repo"]))
    assert code == rw.ERROR_EXIT_CODE and "run note missing" in out["error"]


@pytest.mark.parametrize(
    ("worker", "mode", "expected"),
    [
        ("claude", "ok", "ok"),
        ("claude", "empty", "empty"),
        ("claude", "refusal", "refusal"),
        ("claude", "budget", "failed"),
        ("claude", "malformed", "failed"),
        ("codex", "fail", "failed"),
        ("codex", "empty", "empty"),
        ("agy", "denied", "permission"),
    ],
)
def test_start_classifies_run_status(env, capsys, monkeypatch, worker, mode, expected) -> None:
    monkeypatch.setenv("FAKE_MODE", mode)
    code, out = start(env, capsys, worker)
    assert out["status"] == expected
    assert code == rw.STATUS_EXIT_CODES[expected]
    if expected != "ok":
        assert code != 0
    if mode == "empty":
        assert out["worker_status"] is None and out["outcome"] is None and out["next_step"] is None


def test_timeout_kills_group_and_never_hangs_on_detached_child(env, capsys, monkeypatch) -> None:
    grandchild = env["root"] / "grandchild.pid"
    monkeypatch.setenv("FAKE_MODE", "hang")
    monkeypatch.setenv("FAKE_GRANDCHILD_PID", str(grandchild))
    began = time.monotonic()
    try:
        code, out = start(env, capsys, "claude", "slow", "--timeout-seconds", "1")
    finally:
        for _ in range(50):
            if grandchild.exists() and grandchild.read_text():
                try:
                    os.kill(int(grandchild.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                break
            time.sleep(0.1)
    assert time.monotonic() - began < 15
    assert out["status"] == "timeout" and code == 124 and out["exit_code"] == 124


def test_classify_precedence() -> None:
    assert rw.classify(0, True, {"message": "done"}) == "timeout"
    assert rw.classify(124, False, {"message": "done"}) == "timeout"
    assert rw.classify(0, False, {"message": "", "denied": [1]}) == "permission"
    log = 'tool required the "command" permission that headless mode cannot prompt for'
    assert rw.classify(0, False, {"message": ""}, log) == "permission"
    assert rw.classify(0, False, {"message": "no", "refused": True}) == "refusal"
    assert rw.classify(2, False, {"message": "done"}) == "failed"
    assert rw.classify(0, False, {"message": "  \n"}) == "empty"
    assert rw.classify(0, False, {"message": "done"}) == "ok"


def test_handoff_fields_absent_and_present() -> None:
    assert rw.handoff_fields("no ending") == {"worker_status": None, "outcome": None, "next_step": None}
    fields = rw.handoff_fields("**Status:** BLOCKED\n\n**Outcome:** x.\n\n**Next step:** answer: y")
    assert fields == {"worker_status": "BLOCKED", "outcome": "x.", "next_step": "answer: y"}


# ---------------------------------------------------------------- status and cleanup states


def write_note(repo: Path, slug: str, task: Path, **values: str) -> None:
    base = repo / ".delegate-code"
    base.mkdir(exist_ok=True)
    note = {"worker": "codex", "task": str(task), "access": "restricted", "session_id": "",
            "started": rw.now_utc(), "pid": "", "status": "running", "timeout_seconds": "1800", **values}
    (base / f"{slug}.run").write_text("".join(f"{k}={v}\n" for k, v in note.items()))


def test_status_states_and_cleanup_refusals(env, capsys, tmp_path: Path) -> None:
    repo = env["repo"]
    sleeper = subprocess.Popen(["sleep", "30"])
    dead = subprocess.Popen(["true"])
    dead.wait()
    finished_task = tmp_path / "finished"
    finished_task.mkdir()
    (finished_task / "last.md").write_text("**Status:** DONE\n")
    empty_task = tmp_path / "empty"
    empty_task.mkdir()
    try:
        write_note(repo, "live", empty_task, pid=str(sleeper.pid))
        write_note(repo, "done", finished_task, pid=str(dead.pid))
        write_note(repo, "old", empty_task, pid=str(dead.pid), started="2020-01-01T00:00:00Z", timeout_seconds="1")
        write_note(repo, "young", empty_task, pid=str(dead.pid))
        write_note(repo, "failed", empty_task, pid=str(dead.pid), status="failed")
        (repo / ".delegate-code" / "orphan").mkdir()
        before = sorted(p.name for p in (repo / ".delegate-code").iterdir())

        code, out = call(capsys, "status", "--repo-root", str(repo))
        states = {d["slug"]: d["state"] for d in out["delegations"]}
        assert states == {"live": "running", "done": "finished", "old": "abandoned",
                          "young": "unknown", "failed": "unknown", "orphan": "unknown"}
        failed = next(d for d in out["delegations"] if d["slug"] == "failed")
        assert failed["run_status"] == "failed"  # recorded status never promotes the state
        assert sorted(p.name for p in (repo / ".delegate-code").iterdir()) == before  # read-only

        code, out = call(capsys, "status", "--slug", "done", "--repo-root", str(repo))
        assert [d["slug"] for d in out["delegations"]] == ["done"]

        for slug, state in (("live", "running"), ("orphan", "unknown"), ("young", "unknown"), ("failed", "unknown")):
            code, out = call(capsys, "cleanup", "--slug", slug, "--confirm", "--repo-root", str(repo))
            assert code == rw.ERROR_EXIT_CODE and f"state is {state}" in out["error"]
        assert (repo / ".delegate-code" / "orphan").is_dir()
        assert (repo / ".delegate-code" / "live.run").is_file()

        code, out = call(capsys, "cleanup", "--slug", "old", "--confirm", "--repo-root", str(repo))
        assert code == 0 and out["previous_state"] == "abandoned"
        assert not (repo / ".delegate-code" / "old.run").exists()
    finally:
        sleeper.kill()
        sleeper.wait()


# ---------------------------------------------------------------- QA regressions


def test_review_slug_rejected_on_cli(env, capsys) -> None:
    code, out = start(env, capsys, "codex", "foo.review")
    assert code == rw.ERROR_EXIT_CODE and "--slug" in out["error"]


def test_symlinked_slug_cannot_clean_up_another_worktree(env, capsys) -> None:
    repo = env["repo"]
    assert start(env, capsys, "codex", "beta")[0] == 0
    base = repo / ".delegate-code"
    (base / "alpha").symlink_to(base / "beta")
    (base / "alpha.run").write_text((base / "beta.run").read_text())
    code, out = call(capsys, "cleanup", "--slug", "alpha", "--confirm", "--repo-root", str(repo))
    assert code == rw.ERROR_EXIT_CODE and "symlink" in out["error"]
    assert (base / "beta").is_dir() and run_git(repo, "branch", "--list", "delegate-code/beta").strip()


def test_plain_directory_cannot_apply_or_stage_main_checkout(env, capsys) -> None:
    repo = env["repo"]
    (repo / ".delegate-code" / "foo").mkdir(parents=True)
    (repo / "README.md").write_text("main checkout edit\n")
    code, out = call(capsys, "apply", "--slug", "foo", "--expected-fingerprint", "0" * 40,
                     "--confirm", "--repo-root", str(repo))
    assert code == rw.ERROR_EXIT_CODE and "not a registered git worktree" in out["error"]
    assert run_git(repo, "diff", "--cached", "--name-only") == ""


def test_patch_comes_from_verified_tree_not_the_index(env, capsys) -> None:
    repo = env["repo"]
    assert start(env, capsys)[0] == 0
    layout = rw.Layout(repo, "feat")
    tree = rw.fingerprint(layout)
    before = rw.tree_patch(layout, tree)
    (layout.worktree / "sneaky.txt").write_text("added after review\n")
    run_git(layout.worktree, "add", "-A")
    assert rw.tree_patch(layout, tree) == before and b"sneaky" not in before
    assert rw.tree_files(layout, tree) == ["feature.txt"]


def test_capped_claude_resume_requires_explicit_budget(env, capsys) -> None:
    repo = env["repo"]
    assert start(env, capsys, "claude", "capped", "--max-budget-usd", "2")[0] == 0
    assert rw.read_note(rw.Layout(repo, "capped"))["max_budget_usd"] == "2"
    resume = ["resume", "--slug", "capped", "--fix-file", str(env["task"] / "fix.md"),
              "--repo-root", str(repo), "--binary", str(env["bin"] / "claude")]
    code, out = call(capsys, *resume)
    assert code == rw.ERROR_EXIT_CODE and "remaining budget" in out["error"]
    code, out = call(capsys, *resume, "--max-budget-usd", "0.5")
    assert code == 0 and out["status"] == "ok"
    argv = json.loads((env["root"] / "argv.log").read_text().splitlines()[-1])
    assert argv[argv.index("--max-budget-usd") + 1] == "0.5"


def test_hard_linked_outputs_never_truncate_external_files(env, capsys) -> None:
    external_log = env["root"] / "external.log"
    external_events = env["root"] / "external.jsonl"
    external_log.write_text("keep me\n")
    external_events.write_text("keep me too\n")
    os.link(external_log, env["task"] / "worker.log")
    os.link(external_events, env["task"] / "events.jsonl")
    code, out = start(env, capsys)
    assert code == 0, out
    assert external_log.read_text() == "keep me\n"
    assert external_events.read_text() == "keep me too\n"


def test_resume_ignores_earlier_permission_denial_in_log(env, capsys, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "denylog")
    code, out = start(env, capsys, "claude")
    assert out["status"] == "permission"
    monkeypatch.setenv("FAKE_MODE", "ok")
    code, out = call(capsys, "resume", "--slug", "feat", "--fix-file", str(env["task"] / "fix.md"),
                     "--repo-root", str(env["repo"]), "--binary", str(env["bin"] / "claude"))
    assert code == 0 and out["status"] == "ok"
    assert "headless mode cannot prompt" in (env["task"] / "worker.log").read_text()  # kept, not reclassified


def test_invalid_cli_arguments_return_json_error(capsys) -> None:
    code = rw.main(["start", "--slug", "x", "--worker", "codex", "--brief-file", "b",
                    "--task-dir", "t", "--effort", "huge"])
    out = json.loads(capsys.readouterr().out.strip())
    assert code == rw.ERROR_EXIT_CODE and out["status"] == "error" and "--effort" in out["error"]


def test_empty_handoff_field_does_not_swallow_next_line() -> None:
    fields = rw.handoff_fields("**Status:** DONE\n**Outcome:**\n**Next step:** none\n")
    assert fields == {"worker_status": "DONE", "outcome": None, "next_step": "none"}


def test_start_existing_worktree_after_baseline(env, capsys) -> None:
    repo = env["repo"]
    run_git(repo, "worktree", "add", "-q", "-b", "delegate-code/feat", ".delegate-code/feat", "HEAD")
    code, out = start(env, capsys)
    assert code == rw.ERROR_EXIT_CODE and "--existing-worktree" in out["error"]
    code, out = start(env, capsys, "codex", "feat", "--existing-worktree")
    assert code == 0 and out["status"] == "ok"
    assert (repo / ".delegate-code" / "feat" / "feature.txt").is_file()
    code, out = start(env, capsys, "codex", "feat", "--existing-worktree")
    assert code == rw.ERROR_EXIT_CODE and "run note already exists" in out["error"]


def test_existing_worktree_must_be_registered_on_its_branch(env, capsys) -> None:
    repo = env["repo"]
    (repo / ".delegate-code" / "plain").mkdir(parents=True)
    code, out = start(env, capsys, "codex", "plain", "--existing-worktree")
    assert code == rw.ERROR_EXIT_CODE and "not a registered git worktree" in out["error"]
    run_git(repo, "worktree", "add", "-q", "-b", "other", ".delegate-code/wrong", "HEAD")
    code, out = start(env, capsys, "codex", "wrong", "--existing-worktree")
    assert code == rw.ERROR_EXIT_CODE and "not on delegate-code/wrong" in out["error"]
    assert not (repo / ".delegate-code" / "wrong.run").exists()
