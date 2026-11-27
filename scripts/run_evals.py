#!/usr/bin/env python3
"""Opt-in local runner for skill evals and with/without-skill ablations.

Not run in CI. Sends each case in skills/<name>/tests/eval.yaml to a local CLI in
read-only mode, checks rubric.must_mention and rubric.must_not_mention, and saves
expectations and trajectory for a human or second-opinion judge to grade.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = ("claude", "codex", "agy")
CASE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class EvalError(Exception):
    pass


def list_skills(root: Path = ROOT) -> list[str]:
    return sorted(p.parent.parent.name for p in (root / "skills").glob("*/tests/eval.yaml"))


def load_cases(skill: str, case: str | None = None, root: Path = ROOT) -> list[dict]:
    path = root / "skills" / skill / "tests" / "eval.yaml"
    if skill not in list_skills(root):
        raise EvalError(f"unknown skill or missing tests/eval.yaml: {skill}")
    cases = yaml.safe_load(path.read_text())["cases"]
    for c in cases:
        # Case names become file names, so they must not carry path segments.
        if not CASE_NAME.fullmatch(str(c.get("name", ""))) or ".." in c["name"]:
            raise EvalError(f"invalid case name in {skill}: {c.get('name')!r}")
    if case is not None:
        cases = [c for c in cases if c["name"] == case]
        if not cases:
            raise EvalError(f"unknown case for {skill}: {case}")
    return cases


def grade(text: str, rubric: dict | None) -> dict:
    """Deterministic checks only; expectations and trajectory are surfaced for a judge."""
    rubric = rubric or {}
    lower = text.lower()
    missing = [s for s in rubric.get("must_mention", []) if s.lower() not in lower]
    forbidden = [s for s in rubric.get("must_not_mention", []) if s.lower() in lower]
    result = {
        "passed": not missing and not forbidden,
        "missing_mentions": missing,
        "forbidden_mentions": forbidden,
        "expectations": rubric.get("expectations", []),
    }
    if "trajectory" in rubric:
        result["trajectory"] = {
            "in_order": rubric["trajectory"].get("in_order", []),
            "forbidden": rubric["trajectory"].get("forbidden", []),
        }
    return result


def build_prompt(case_prompt: str, skill_text: str | None) -> str:
    if skill_text is None:
        return case_prompt
    return f"Use this skill as context.\n\n<skill>\n{skill_text}\n</skill>\n\n{case_prompt}"


def build_command(provider: str, binary: str, prompt: str, timeout_seconds: int) -> list[str]:
    if provider == "claude":
        return [binary, "-p", "--permission-mode", "plan", "--output-format", "json", prompt]
    if provider == "codex":
        return [binary, "exec", "--sandbox", "read-only", "--ephemeral", prompt]
    if provider == "agy":
        return [binary, "--mode", "plan", "--sandbox", "--print-timeout", f"{timeout_seconds}s",
                "--output-format", "text", f"-p={prompt}"]
    raise EvalError(f"unsupported provider: {provider}")


def run_command(command: list[str], cwd: Path, timeout_seconds: int) -> dict:
    # Own process group and no stdin: a timeout kills the CLI and every child it started.
    process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        return {"status": "ok" if process.returncode == 0 else "failed",
                "returncode": process.returncode, "stdout": stdout, "stderr": stderr}
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            # A detached child still holds the pipes open; give up on the rest of the output.
            stdout = stderr = ""
            for pipe in (process.stdout, process.stderr):
                pipe.close()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        return {"status": "timeout", "returncode": 124, "stdout": stdout or "", "stderr": stderr or ""}


def interpret(provider: str, run: dict) -> tuple[str, str]:
    """Return (response text, status); empty, error and refusal responses never count as ok."""
    answer, status = run["stdout"], run["status"]
    if provider == "claude":
        try:
            data = json.loads(answer)
        except ValueError:
            data = None
        if isinstance(data, dict):
            answer = str(data.get("result") or "")
            if status == "ok" and data.get("stop_reason") == "refusal":
                status = "refusal"
            elif status == "ok" and data.get("is_error") is True:
                status = "failed"
    if status == "ok" and not answer.strip():
        status = "empty"
    return answer, status


def require_safe_output(output_dir: Path, root: Path = ROOT, inside_repo: bool = False) -> Path:
    """Inside the repository, outputs must land in a Git-ignored directory such as tmp/."""
    resolved = output_dir.expanduser().resolve()
    if not resolved.is_relative_to(root.resolve()):
        if inside_repo:
            raise EvalError(f"default output directory escapes the repository: {resolved}")
        return resolved
    relative = resolved.relative_to(root.resolve())
    ignored = subprocess.run(["git", "-C", str(root), "check-ignore", "--quiet", "--", str(relative)],
                             capture_output=True, check=False)
    if ignored.returncode != 0:
        raise EvalError(f"output directory is not ignored by Git: {relative}; refusing to write")
    return resolved


def allocate_unique(base: Path, name: str) -> Path:
    """Atomically create base/name, or base/name-2, -3, ... if it already exists."""
    base.mkdir(parents=True, exist_ok=True)
    for n in itertools.count(1):
        candidate = base / (name if n == 1 else f"{name}-{n}")
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            continue


def prepare_output_dir(output_dir: Path | None, root: Path = ROOT) -> Path:
    if output_dir is None:
        base = require_safe_output(root / "tmp" / "eval-runs", root, inside_repo=True)
        return allocate_unique(base, datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    resolved = require_safe_output(output_dir, root)
    if resolved.exists() and any(resolved.iterdir()):
        raise EvalError(f"output directory is not empty; refusing to overwrite: {resolved}")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def write_inside(output_dir: Path, name: str, text: str) -> None:
    path = (output_dir / name).resolve()
    if not path.is_relative_to(output_dir.resolve()):
        raise EvalError(f"output file escapes the output directory: {name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def run_evals(args: argparse.Namespace, root: Path = ROOT) -> dict:
    cases = load_cases(args.skill, args.case, root)
    skill_text = (root / "skills" / args.skill / "SKILL.md").read_text()
    variants = {"with_skill": skill_text}
    if args.ablation:
        variants["without_skill"] = None
    if args.dry_run:
        for case in cases:
            for variant, text in variants.items():
                print(f"## {case['name']} [{variant}]\n{build_prompt(case['prompt'], text)}")
        return {}
    if not args.provider:
        raise EvalError("--provider is required unless --list or --dry-run is used")
    binary = args.binary or shutil.which(args.provider)
    if not binary:
        raise EvalError(f"provider binary not found: {args.provider}")
    output_dir = prepare_output_dir(args.output_dir, root)
    summary = {"skill": args.skill, "provider": args.provider, "ablation": args.ablation, "results": []}
    blind_key = {}
    for case in cases:
        answers = {}
        for variant, text in variants.items():
            prompt = build_prompt(case["prompt"], text)
            run = run_command(build_command(args.provider, binary, prompt, args.timeout_seconds),
                              root, args.timeout_seconds)
            answer, status = interpret(args.provider, run)
            answers[variant] = answer
            result = {"case": case["name"], "variant": variant, "status": status,
                      "returncode": run["returncode"], **grade(answer, case.get("rubric"))}
            if status != "ok":
                result["passed"] = False
            stem = f"{case['name']}.{variant}"
            write_inside(output_dir, f"{stem}.json", json.dumps(
                {**result, "prompt": prompt, "response": answer, "stderr": run["stderr"]}, indent=2))
            write_inside(output_dir, f"{stem}.md", f"# {case['name']} [{variant}]\n\n{answer}\n")
            summary["results"].append(result)
        if args.ablation:
            order = random.sample(list(variants), 2)
            blind_key[case["name"]] = dict(zip("AB", order))
            for label, variant in zip("AB", order):
                write_inside(output_dir, f"blind/{case['name']}.{label}.md", answers[variant])
    if args.ablation:
        write_inside(output_dir, "blind/key.json", json.dumps(blind_key, indent=2))
    write_inside(output_dir, "summary.json", json.dumps(summary, indent=2))
    write_inside(output_dir, "summary.md", render_summary(summary))
    print(f"wrote {output_dir}")
    return summary


def render_summary(summary: dict) -> str:
    lines = [f"# Eval run: {summary['skill']} via {summary['provider']}", ""]
    for r in summary["results"]:
        lines += [f"## {r['case']} [{r['variant']}]: {r['status']}, deterministic "
                  f"{'pass' if r['passed'] else 'fail'}", ""]
        lines += [f"- Missing mention: {m}" for m in r["missing_mentions"]]
        lines += [f"- Forbidden mention: {m}" for m in r["forbidden_mentions"]]
        lines += [f"- [ ] Expectation: {e}" for e in r["expectations"]]
        if "trajectory" in r:
            lines += [f"- [ ] In order {i}: {s}" for i, s in enumerate(r["trajectory"]["in_order"], 1)]
            lines += [f"- [ ] Forbidden step: {s}" for s in r["trajectory"]["forbidden"]]
        lines.append("")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skill")
    parser.add_argument("--list", action="store_true", help="list skills and case names")
    parser.add_argument("--case")
    parser.add_argument("--provider", choices=PROVIDERS)
    parser.add_argument("--ablation", action="store_true", help="also run each case without the skill")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--binary", help="provider binary path; defaults to shutil.which(provider)")
    args = parser.parse_args(argv)
    if not args.list and not args.skill:
        parser.error("--skill is required unless --list is used")
    return args


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    args = parse_args(argv)
    try:
        if args.list:
            for skill in [args.skill] if args.skill else list_skills(root):
                print(skill)
                for case in load_cases(skill, args.case, root):
                    print(f"  {case['name']}")
            return 0
        summary = run_evals(args, root)
    except EvalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0 if all(r["passed"] for r in summary.get("results", [])) else 1


if __name__ == "__main__":
    sys.exit(main())
