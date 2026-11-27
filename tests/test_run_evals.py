import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("run_evals", ROOT / "scripts/run_evals.py")
run_evals = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_evals)


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = run_evals.main(argv)
    return code, out.getvalue(), err.getvalue()


class RunEvalsTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.tmp = Path(workspace.name)

    def fake_cli(self, body):
        path = self.tmp / "fake-cli"
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)
        return str(path)

    def test_lists_and_filters_real_cases(self):
        self.assertIn("delegate-code", run_evals.list_skills())
        names = [c["name"] for c in run_evals.load_cases("delegate-code")]
        self.assertIn("scoped_codex_delegation", names)
        only = run_evals.load_cases("delegate-code", "scoped_codex_delegation")
        self.assertEqual([c["name"] for c in only], ["scoped_codex_delegation"])
        code, out, _ = run_main(["--list"])
        self.assertEqual(code, 0)
        self.assertIn("second-opinion\n  claude_named_provider", out)

    def test_dry_run_prints_prompts_without_provider(self):
        code, out, _ = run_main(["--skill", "delegate-code", "--case", "scoped_codex_delegation",
                                 "--ablation", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("[with_skill]", out)
        self.assertIn("[without_skill]", out)
        self.assertIn("<skill>", out)

    def test_grade_must_mention_and_must_not_mention(self):
        rubric = {"must_mention": ["Codex Exec", "worktree"], "must_not_mention": ["--dangerously"]}
        passed = run_evals.grade("Run codex exec in a WORKTREE.", rubric)
        self.assertTrue(passed["passed"])
        failed = run_evals.grade("Use --dangerously-bypass in place.", rubric)
        self.assertFalse(failed["passed"])
        self.assertEqual(failed["missing_mentions"], ["Codex Exec", "worktree"])
        self.assertEqual(failed["forbidden_mentions"], ["--dangerously"])
        self.assertTrue(run_evals.grade("anything", None)["passed"])

    def test_ablation_with_fake_cli_writes_both_variants_and_trajectory(self):
        binary = self.fake_cli('case "$*" in *"<skill>"*) echo "with skill: codex exec";; '
                               '*) echo "bare answer";; esac')
        out_dir = self.tmp / "run"
        code, _, err = run_main(["--skill", "delegate-code", "--case", "scoped_codex_delegation",
                                 "--provider", "codex", "--binary", binary, "--ablation",
                                 "--output-dir", str(out_dir)])
        self.assertEqual(code, 0, err)
        for variant in ("with_skill", "without_skill"):
            data = json.loads((out_dir / f"scoped_codex_delegation.{variant}.json").read_text())
            self.assertEqual(data["status"], "ok")
            self.assertIn("in_order", data["trajectory"])
            self.assertIn("forbidden", data["trajectory"])
            self.assertTrue((out_dir / f"scoped_codex_delegation.{variant}.md").exists())
        with_skill = json.loads((out_dir / "scoped_codex_delegation.with_skill.json").read_text())
        self.assertIn("with skill", with_skill["response"])
        without = json.loads((out_dir / "scoped_codex_delegation.without_skill.json").read_text())
        self.assertEqual(without["response"].strip(), "bare answer")
        key = json.loads((out_dir / "blind" / "key.json").read_text())["scoped_codex_delegation"]
        self.assertEqual(sorted(key.values()), ["with_skill", "without_skill"])
        for label, variant in key.items():
            blind = (out_dir / "blind" / f"scoped_codex_delegation.{label}.md").read_text()
            self.assertEqual(blind, {"with_skill": with_skill, "without_skill": without}[variant]["response"])
            self.assertNotIn("skill]", blind)
            self.assertNotIn("<skill>", blind)
        summary = json.loads((out_dir / "summary.json").read_text())
        self.assertEqual([r["variant"] for r in summary["results"]], ["with_skill", "without_skill"])
        report = (out_dir / "summary.md").read_text()
        self.assertIn("In order 1: write brief.md outside the repository", report)
        self.assertIn("Forbidden step:", report)

    def test_reused_output_dir_never_overwrites_first_run(self):
        out_dir = self.tmp / "run"
        argv = ["--skill", "delegate-code", "--case", "scoped_codex_delegation", "--provider", "codex",
                "--output-dir", str(out_dir)]
        code, _, err = run_main(argv + ["--binary", self.fake_cli("echo first")])
        self.assertEqual(code, 0, err)
        code, _, err = run_main(argv + ["--binary", self.fake_cli("echo second")])
        self.assertEqual(code, 2)
        self.assertIn("not empty", err)
        first = json.loads((out_dir / "scoped_codex_delegation.with_skill.json").read_text())
        self.assertEqual(first["response"].strip(), "first")

    def test_default_dirs_are_allocated_uniquely(self):
        first = run_evals.allocate_unique(self.tmp / "eval-runs", "20260101T000000Z")
        second = run_evals.allocate_unique(self.tmp / "eval-runs", "20260101T000000Z")
        third = run_evals.allocate_unique(self.tmp / "eval-runs", "20260101T000000Z")
        self.assertEqual([first.name, second.name, third.name],
                         ["20260101T000000Z", "20260101T000000Z-2", "20260101T000000Z-3"])

    def test_path_bearing_case_names_are_rejected(self):
        for name in ("../escape", "sub/case", ".hidden", "a..b"):
            with self.subTest(name=name):
                root = self.tmp / "repo"
                tests = root / "skills" / "evil" / "tests"
                tests.mkdir(parents=True, exist_ok=True)
                (tests / "eval.yaml").write_text(json.dumps({"cases": [{"name": name, "prompt": "x"}]}))
                with self.assertRaisesRegex(run_evals.EvalError, "invalid case name"):
                    run_evals.load_cases("evil", root=root)
        with self.assertRaisesRegex(run_evals.EvalError, "escapes"):
            run_evals.write_inside(self.tmp / "run", "../outside.md", "x")

    def test_symlinked_default_dir_outside_repo_is_refused(self):
        root, outside = self.tmp / "repo", self.tmp / "outside"
        (root / "tmp").mkdir(parents=True)
        outside.mkdir()
        (root / "tmp" / "eval-runs").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(run_evals.EvalError, "escapes the repository"):
            run_evals.prepare_output_dir(None, root)
        self.assertEqual(list(outside.iterdir()), [])

    def test_empty_or_error_responses_fail(self):
        binary = self.fake_cli("exit 0")
        code, _, err = run_main(["--skill", "delegate-code", "--case", "scoped_codex_delegation",
                                 "--provider", "codex", "--binary", binary,
                                 "--output-dir", str(self.tmp / "run")])
        self.assertEqual(code, 1, err)
        data = json.loads((self.tmp / "run" / "scoped_codex_delegation.with_skill.json").read_text())
        self.assertEqual(data["status"], "empty")
        self.assertFalse(data["passed"])
        ok = {"status": "ok", "stdout": ""}
        self.assertEqual(run_evals.interpret("claude", {**ok, "stdout": '{"result": "x", "is_error": true}'}),
                         ("x", "failed"))
        self.assertEqual(run_evals.interpret("claude", {**ok, "stdout": '{"result": "no", "stop_reason": "refusal"}'}),
                         ("no", "refusal"))
        self.assertEqual(run_evals.interpret("claude", {**ok, "stdout": '{"result": "fine"}'}), ("fine", "ok"))

    def test_timeout_cleanup_is_bounded_when_child_holds_pipes(self):
        # The child calls setsid, so killpg on the CLI group misses it and it keeps the pipes open.
        detach = f'"{sys.executable}" -c "import os, time; os.setsid(); time.sleep(30)"'
        binary = self.fake_cli(f"{detach} & sleep 30")
        start = time.monotonic()
        result = run_evals.run_command([binary], self.tmp, 1)
        self.assertEqual(result["status"], "timeout")
        self.assertLess(time.monotonic() - start, 10)

    def test_timeout_kills_process_and_reports_timeout(self):
        binary = self.fake_cli("echo partial; sleep 30")
        result = run_evals.run_command([binary], self.tmp, 1)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["returncode"], 124)
        self.assertIn("partial", result["stdout"])

    def test_each_variant_runs_in_isolated_workspace_without_eval_rubrics(self):
        binary = self.fake_cli(
            'echo "cwd=$(pwd -P)"; '
            '[ -f .claude/skills/delegate-code/SKILL.md ] && echo has-skill; '
            '[ -f .claude/skills/delegate-code/references/providers.md ] && echo has-references; '
            '[ -e .claude/skills/delegate-code/tests ] && echo leaked-tests; '
            'echo "entries=$(ls -A | wc -l | tr -d " ")"')
        out_dir = self.tmp / "run"
        code, _, err = run_main(["--skill", "delegate-code", "--case", "scoped_codex_delegation",
                                 "--provider", "claude", "--binary", binary, "--ablation",
                                 "--output-dir", str(out_dir)])
        self.assertEqual(code, 0, err)
        cwds = []
        for variant in ("with_skill", "without_skill"):
            response = json.loads((out_dir / f"scoped_codex_delegation.{variant}.json").read_text())["response"]
            cwd = Path(response.split("cwd=")[1].splitlines()[0])
            cwds.append(cwd)
            self.assertFalse(cwd.resolve().is_relative_to(ROOT.resolve()), cwd)
            self.assertTrue(cwd.name.startswith("ytl-eval-"), cwd)
            self.assertFalse(cwd.exists(), "workspace must be cleaned up")
            self.assertNotIn("leaked-tests", response)
            if variant == "with_skill":
                self.assertIn("has-skill", response)
                self.assertIn("has-references", response)
            else:
                self.assertNotIn("has-skill", response)
                self.assertIn("entries=0", response)
        self.assertNotEqual(cwds[0], cwds[1])

    def test_provider_commands_request_structured_output(self):
        claude = run_evals.build_command("claude", "claude", "hi", 5)
        for flag in ("stream-json", "--verbose", "--strict-mcp-config", "--no-session-persistence"):
            self.assertIn(flag, claude)
        self.assertIn("--json", run_evals.build_command("codex", "codex", "hi", 5))

    def claude_stream(self, result):
        return "\n".join(json.dumps(e) for e in [
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Looking."},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "SKILL.md"}},
                {"type": "tool_use", "name": "Bash", "input": {"command": "git worktree add x"}}]}},
            {"type": "user", "message": {"content": []}},
            {"type": "result", "num_turns": 3, "total_cost_usd": 0.0125,
             "usage": {"input_tokens": 10, "cache_creation_input_tokens": 200,
                       "cache_read_input_tokens": 3000, "output_tokens": 40}, **result}])

    def test_claude_stream_json_metrics_totals_and_summary_table(self):
        stream = self.tmp / "stream.jsonl"
        stream.write_text(self.claude_stream({"result": "codex exec in a worktree", "is_error": False}))
        binary = self.fake_cli(f'cat "{stream}"')
        out_dir = self.tmp / "run"
        code, _, err = run_main(["--skill", "delegate-code", "--case", "scoped_codex_delegation",
                                 "--provider", "claude", "--binary", binary, "--ablation",
                                 "--output-dir", str(out_dir)])
        self.assertEqual(code, 0, err)
        data = json.loads((out_dir / "scoped_codex_delegation.with_skill.json").read_text())
        self.assertEqual(data["response"], "codex exec in a worktree")
        m = data["metrics"]
        self.assertIsInstance(m["wall_seconds"], float)
        self.assertEqual({k: m[k] for k in run_evals.METRIC_KEYS},
                         {"turns": 3, "cost_usd": 0.0125, "input_tokens": 10, "cache_write_tokens": 200,
                          "cache_read_tokens": 3000, "output_tokens": 40})
        self.assertEqual(m["tools"], ["Read", "Bash:git"])
        summary = json.loads((out_dir / "summary.json").read_text())
        self.assertEqual(summary["results"][0]["metrics"], m)
        self.assertEqual(set(summary["totals"]), {"with_skill", "without_skill"})
        total = summary["totals"]["without_skill"]
        self.assertEqual((total["passed"], total["total"], total["turns"], total["cost_usd"],
                          total["cache_read_tokens"]), (1, 1, 3, 0.0125, 3000))
        report = (out_dir / "summary.md").read_text()
        self.assertTrue(report.splitlines()[2].startswith("| Variant | Passed | Wall s | Turns | Cost USD"))
        self.assertIn("| with_skill | 1/1 |", report)
        self.assertIn("| 3 | 0.0125 | 10 | 200 | 3000 | 40 |", report)
        self.assertIn("- Tools: Read, Bash:git", report)
        self.assertIn("In order 1:", report)

    def test_claude_stream_json_refusal_error_and_empty(self):
        ok = {"status": "ok"}
        cases = [({"result": "no", "stop_reason": "refusal"}, ("no", "refusal")),
                 ({"result": "x", "is_error": True}, ("x", "failed")),
                 ({"result": ""}, ("", "empty"))]
        for result, expected in cases:
            with self.subTest(result=result):
                self.assertEqual(run_evals.interpret("claude", {**ok, "stdout": self.claude_stream(result)}),
                                 expected)
        no_result = '{"type": "system", "subtype": "init"}\n'
        self.assertEqual(run_evals.interpret("claude", {**ok, "stdout": no_result}), ("", "empty"))
        pretty = json.dumps({"result": "fine", "num_turns": 2}, indent=2)
        self.assertEqual(run_evals.interpret("claude", {**ok, "stdout": pretty}), ("fine", "ok"))
        self.assertEqual(run_evals.parse_output("claude", pretty)[2]["turns"], 2)

    def test_codex_jsonl_metrics_and_plain_text_fallback(self):
        lines = [
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "reasoning", "text": "thinking"}},
            {"type": "item.completed", "item": {"type": "command_execution",
                                                "command": "bash -lc 'ls -la'", "exit_code": 0}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "draft"}},
            {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 60,
                                                 "output_tokens": 7}},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "final answer"}},
            {"type": "turn.completed", "usage": {"input_tokens": 50, "cached_input_tokens": 40,
                                                 "output_tokens": 3}},
        ]
        stdout = "\n".join(json.dumps(e) for e in lines)
        answer, _, m = run_evals.parse_output("codex", stdout)
        self.assertEqual(answer, "final answer")
        self.assertEqual((m["turns"], m["input_tokens"], m["cache_read_tokens"], m["output_tokens"],
                          m["cost_usd"], m["cache_write_tokens"]), (2, 150, 100, 10, None, None))
        self.assertEqual(m["tools"], ["command_execution:bash"])
        self.assertEqual(run_evals.interpret("codex", {"status": "ok", "stdout": stdout}),
                         ("final answer", "ok"))
        answer, _, m = run_evals.parse_output("codex", "plain answer\n")
        self.assertEqual(answer, "plain answer\n")
        self.assertEqual(m, {**{k: None for k in run_evals.METRIC_KEYS}, "tools": []})
        jsonl = self.tmp / "codex.jsonl"
        jsonl.write_text(stdout)
        out_dir = self.tmp / "run"
        code, _, err = run_main(["--skill", "delegate-code", "--case", "scoped_codex_delegation",
                                 "--provider", "codex", "--binary", self.fake_cli(f'cat "{jsonl}"'),
                                 "--output-dir", str(out_dir)])
        self.assertEqual(code, 0, err)
        summary = json.loads((out_dir / "summary.json").read_text())
        self.assertEqual(list(summary["totals"]), ["with_skill"])
        self.assertEqual(summary["totals"]["with_skill"]["input_tokens"], 150)
        self.assertEqual(summary["results"][0]["metrics"]["turns"], 2)

    def make_repo(self):
        root = self.tmp / "repo"
        skill = root / "skills" / "demo"
        (skill / "tests").mkdir(parents=True)
        (skill / "references").mkdir()
        (skill / "SKILL.md").write_text("demo skill")
        (skill / "references" / "ok.md").write_text("ok")
        (skill / "tests" / "eval.yaml").write_text(json.dumps({"cases": [{"name": "c1", "prompt": "hi"}]}))
        return root, skill

    def run_repo(self, root, binary, *extra):
        out, err = io.StringIO(), io.StringIO()
        argv = ["--skill", "demo", "--provider", "codex", "--binary", binary, *extra]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run_evals.main(argv, root)
        return code, err.getvalue()

    def test_symlinks_in_skill_tree_are_refused_before_any_run(self):
        outside = self.tmp / "secret.txt"
        outside.write_text("secret")
        marker = self.tmp / "ran"
        binary = self.fake_cli(f'touch "{marker}"; echo answer')
        for link, target in (("references/leak.md", outside), ("rubric.yaml", Path("tests/eval.yaml"))):
            with self.subTest(link=link):
                root, skill = self.make_repo()
                (skill / link).symlink_to(target)
                out_dir = self.tmp / "run"
                code, err = self.run_repo(root, binary, "--output-dir", str(out_dir))
                self.assertEqual(code, 2)
                self.assertIn("symlink in skill tree", err)
                self.assertFalse(marker.exists())
                self.assertFalse(out_dir.exists())
                shutil.rmtree(root)

    def test_explicit_output_dir_is_claimed_exclusively(self):
        out_dir = self.tmp / "run"
        out_dir.mkdir()
        claimed = run_evals.prepare_output_dir(out_dir)
        self.assertTrue((claimed / "summary.json").exists())
        with self.assertRaisesRegex(run_evals.EvalError, "not empty or already claimed|not empty"):
            run_evals.prepare_output_dir(out_dir)
        # The claim itself is O_EXCL: even when the emptiness check is raced past, the loser fails.
        with mock.patch.object(Path, "iterdir", return_value=iter(())):
            with self.assertRaisesRegex(run_evals.EvalError, "already claimed"):
                run_evals.prepare_output_dir(out_dir)
        root, _ = self.make_repo()
        marker = self.tmp / "ran"
        code, err = self.run_repo(root, self.fake_cli(f'touch "{marker}"; echo answer'),
                                  "--output-dir", str(out_dir))
        self.assertEqual(code, 2)
        self.assertFalse(marker.exists())

    def test_workspace_inside_repo_via_tmpdir_is_refused(self):
        root, _ = self.make_repo()
        inside = root / "tmp"
        inside.mkdir()
        marker = self.tmp / "ran"
        binary = self.fake_cli(f'touch "{marker}"; echo answer')
        with mock.patch.dict(os.environ, {"TMPDIR": str(inside)}), mock.patch.object(tempfile, "tempdir", None):
            code, err = self.run_repo(root, binary, "--output-dir", str(self.tmp / "run"))
        self.assertEqual(code, 2)
        self.assertIn("inside the repository", err)
        self.assertFalse(marker.exists())
        self.assertEqual(list(inside.iterdir()), [])

    def test_relative_binary_path_survives_workspace_cwd(self):
        root, _ = self.make_repo()
        relative = os.path.relpath(self.fake_cli("echo relative answer"))
        self.assertFalse(Path(relative).is_absolute())
        out_dir = self.tmp / "run"
        code, err = self.run_repo(root, relative, "--output-dir", str(out_dir))
        self.assertEqual(code, 0, err)
        data = json.loads((out_dir / "c1.with_skill.json").read_text())
        self.assertEqual(data["response"].strip(), "relative answer")

    def test_non_event_json_stays_plain_text(self):
        ok = {"status": "ok"}
        bare = '{"answer": "done"}\n'
        self.assertEqual(run_evals.interpret("codex", {**ok, "stdout": bare}), (bare, "ok"))
        prose = 'Here is the config:\n{"key": "value"}\nApply it.\n'
        self.assertEqual(run_evals.interpret("codex", {**ok, "stdout": prose}), (prose, "ok"))
        self.assertEqual(run_evals.interpret("claude", {**ok, "stdout": prose}), (prose, "ok"))
        failed = "\n".join(json.dumps(e) for e in [
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "partial"}},
            {"type": "turn.failed", "error": {"message": "boom"}}])
        self.assertEqual(run_evals.interpret("codex", {**ok, "stdout": failed}), ("partial", "failed"))

    def test_rejects_unknown_skill_and_case(self):
        code, _, err = run_main(["--skill", "no-such-skill", "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("unknown skill", err)
        code, _, err = run_main(["--skill", "delegate-code", "--case", "nope", "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("unknown case", err)

    def test_requires_provider_and_ignored_output_dir(self):
        code, _, err = run_main(["--skill", "delegate-code"])
        self.assertEqual(code, 2)
        self.assertIn("--provider is required", err)
        with self.assertRaisesRegex(run_evals.EvalError, "not ignored"):
            run_evals.require_safe_output(ROOT / "docs" / "eval-runs")
        self.assertEqual(run_evals.require_safe_output(ROOT / "tmp" / "eval-runs" / "x"),
                         (ROOT / "tmp" / "eval-runs" / "x").resolve())


if __name__ == "__main__":
    unittest.main()
