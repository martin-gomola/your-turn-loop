import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

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
