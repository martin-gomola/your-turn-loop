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
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = ("claude", "codex", "agy")
CASE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
METRIC_KEYS = ("turns", "cost_usd", "input_tokens", "cache_write_tokens", "cache_read_tokens",
               "output_tokens")


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
        return [binary, "-p", "--permission-mode", "plan", "--output-format", "stream-json", "--verbose",
                "--strict-mcp-config", "--no-session-persistence", prompt]
    if provider == "codex":
        return [binary, "exec", "--sandbox", "read-only", "--ephemeral", "--json", prompt]
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


def json_events(stdout: str) -> list[dict] | None:
    """JSONL objects, or one (possibly pretty-printed) JSON object; None for plain text."""
    events = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    if not events:
        try:
            data = json.loads(stdout)
        except ValueError:
            return None
        events = [data] if isinstance(data, dict) else []
    return events or None


def first_word(command) -> str:
    words = " ".join(command).split() if isinstance(command, list) else str(command or "").split()
    return words[0][:40] if words else ""


def is_event(provider: str, event: dict) -> bool:
    kind = event.get("type")
    if provider == "claude":
        return kind in ("system", "assistant", "user", "result") or (kind is None and "result" in event)
    return isinstance(kind, str) and (kind.startswith(("thread.", "turn.", "item.")) or kind == "error")


def add(total: int | float | None, value) -> int | float | None:
    return total if not isinstance(value, (int, float)) else (total or 0) + value


def parse_output(provider: str, stdout: str) -> tuple[str, dict, dict]:
    """Return (response text, error flags, metrics without wall_seconds) from provider stdout."""
    metrics = {k: None for k in METRIC_KEYS}
    metrics["tools"] = []
    flags = {}
    events = json_events(stdout) if provider in ("claude", "codex") else None
    # A JSON line in a plain-text answer is not an event stream; keep the raw text then.
    if events is not None and not any(is_event(provider, e) for e in events):
        events = None
    if events is None:
        return stdout, flags, metrics
    answer = ""
    for e in events:
        kind = e.get("type")
        if provider == "claude" and kind == "assistant":
            for item in (e.get("message") or {}).get("content") or []:
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    name = str(item.get("name") or "?")
                    word = first_word((item.get("input") or {}).get("command")) if name == "Bash" else ""
                    metrics["tools"].append(f"Bash:{word}" if word else name)
        elif provider == "claude" and kind in ("result", None):
            # kind None is the single-object `--output-format json` shape.
            answer = str(e.get("result") or "")
            flags = {"refusal": e.get("stop_reason") == "refusal", "error": e.get("is_error") is True}
            metrics["turns"] = e.get("num_turns")
            metrics["cost_usd"] = e.get("total_cost_usd")
            usage = e.get("usage") or {}
            for key, source in (("input_tokens", "input_tokens"),
                                ("cache_write_tokens", "cache_creation_input_tokens"),
                                ("cache_read_tokens", "cache_read_input_tokens"),
                                ("output_tokens", "output_tokens")):
                metrics[key] = usage.get(source)
        elif provider == "codex" and kind == "item.completed":
            item = e.get("item") or {}
            if item.get("type") == "agent_message":
                answer = str(item.get("text") or "")
            elif item.get("type") == "command_execution":
                word = first_word(item.get("command"))
                metrics["tools"].append(f"command_execution:{word}" if word else "command_execution")
            elif item.get("type") not in ("reasoning", None):
                metrics["tools"].append(str(item.get("tool") or item["type"]))
        elif provider == "codex" and kind == "turn.completed":
            metrics["turns"] = (metrics["turns"] or 0) + 1
            usage = e.get("usage") or {}
            for key, source in (("input_tokens", "input_tokens"), ("cache_read_tokens", "cached_input_tokens"),
                                ("output_tokens", "output_tokens")):
                metrics[key] = add(metrics[key], usage.get(source))
        elif provider == "codex" and kind == "turn.failed":
            flags["error"] = True
    return answer, flags, metrics


def interpret(provider: str, run: dict) -> tuple[str, str]:
    """Return (response text, status); empty, error and refusal responses never count as ok."""
    answer, flags, _ = parse_output(provider, run["stdout"])
    status = run["status"]
    if status == "ok" and flags.get("refusal"):
        status = "refusal"
    elif status == "ok" and flags.get("error"):
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
        resolved = allocate_unique(base, datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    else:
        resolved = require_safe_output(output_dir, root)
        if resolved.exists() and any(resolved.iterdir()):
            raise EvalError(f"output directory is not empty; refusing to overwrite: {resolved}")
        resolved.mkdir(parents=True, exist_ok=True)
    # Exclusive claim: of two runs that both saw an empty directory, only one creates the placeholder.
    try:
        os.close(os.open(resolved / "summary.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
    except FileExistsError:
        raise EvalError(f"output directory is not empty or already claimed; refusing to overwrite: "
                        f"{resolved}") from None
    return resolved


def skill_ignore(directory: str, names: list[str]) -> list[str]:
    """copytree ignore hook: skip tests/ and __pycache__/, refuse any symlink that would be copied."""
    ignored = [n for n in names if n in ("tests", "__pycache__")]
    for name in names:
        if name not in ignored and os.path.islink(os.path.join(directory, name)):
            raise EvalError(f"symlink in skill tree; refusing to copy: {os.path.join(directory, name)}")
    return ignored


def check_skill_tree(skill_dir: Path) -> None:
    """Fail before any case runs if the skill copy would follow a symlink."""
    if skill_dir.is_symlink():
        raise EvalError(f"skill directory is a symlink; refusing to copy: {skill_dir}")
    for directory, dirs, files in os.walk(skill_dir):
        skipped = skill_ignore(directory, dirs + files)
        dirs[:] = [d for d in dirs if d not in skipped]


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
    if os.sep in binary or (os.altsep and os.altsep in binary):
        # The CLI runs with cwd=workspace, so a relative path must be anchored to the caller's cwd.
        binary = str(Path(binary).expanduser().resolve())
    skill_dir = root / "skills" / args.skill
    check_skill_tree(skill_dir)
    output_dir = prepare_output_dir(args.output_dir, root)
    summary = {"skill": args.skill, "provider": args.provider, "ablation": args.ablation, "results": []}
    blind_key = {}
    for case in cases:
        answers = {}
        for variant, text in variants.items():
            prompt = build_prompt(case["prompt"], text)
            # A fresh workspace outside the repo, so without_skill cannot find skills/ or repo config.
            with tempfile.TemporaryDirectory(prefix="ytl-eval-") as workspace:
                if Path(workspace).resolve().is_relative_to(root.resolve()):
                    raise EvalError(f"temporary workspace is inside the repository (check TMPDIR): "
                                    f"{workspace}")
                if text is not None:
                    # Skill references/ and scripts/ resolve, but tests/eval.yaml never reaches the model.
                    shutil.copytree(skill_dir, Path(workspace) / ".claude" / "skills" / args.skill,
                                    ignore=skill_ignore)
                start = time.monotonic()
                run = run_command(build_command(args.provider, binary, prompt, args.timeout_seconds),
                                  Path(workspace), args.timeout_seconds)
                wall = round(time.monotonic() - start, 3)
            answer, status = interpret(args.provider, run)
            metrics = {"wall_seconds": wall, **parse_output(args.provider, run["stdout"])[2]}
            answers[variant] = answer
            result = {"case": case["name"], "variant": variant, "status": status,
                      "returncode": run["returncode"], **grade(answer, case.get("rubric")),
                      "metrics": metrics}
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
    summary["totals"] = totals(summary["results"], variants)
    write_inside(output_dir, "summary.json", json.dumps(summary, indent=2))
    write_inside(output_dir, "summary.md", render_summary(summary))
    print(f"wrote {output_dir}")
    return summary


def totals(results: list[dict], variants) -> dict:
    """Per-variant sums; a metric stays None when no run of that variant reported it."""
    out = {}
    for variant in variants:
        rows = [r for r in results if r["variant"] == variant]
        total = {"passed": sum(r["passed"] for r in rows), "total": len(rows),
                 "wall_seconds": round(sum(r["metrics"]["wall_seconds"] for r in rows), 3)}
        for key in METRIC_KEYS:
            total[key] = None
            for r in rows:
                total[key] = add(total[key], r["metrics"][key])
        if total["cost_usd"] is not None:
            total["cost_usd"] = round(total["cost_usd"], 6)
        out[variant] = total
    return out


def show(value) -> str:
    return "-" if value is None else str(value)


def render_summary(summary: dict) -> str:
    lines = [f"# Eval run: {summary['skill']} via {summary['provider']}", "",
             "| Variant | Passed | Wall s | Turns | Cost USD | Input | Cache write | Cache read | Output |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for variant, t in summary["totals"].items():
        lines.append(f"| {variant} | {t['passed']}/{t['total']} | {t['wall_seconds']} | "
                     + " | ".join(show(t[k]) for k in METRIC_KEYS) + " |")
    lines.append("")
    for r in summary["results"]:
        m = r["metrics"]
        lines += [f"## {r['case']} [{r['variant']}]: {r['status']}, deterministic "
                  f"{'pass' if r['passed'] else 'fail'}", "",
                  f"- Metrics: wall {m['wall_seconds']}s, turns {show(m['turns'])}, cost {show(m['cost_usd'])}, "
                  f"tokens in {show(m['input_tokens'])} / cache write {show(m['cache_write_tokens'])} / "
                  f"cache read {show(m['cache_read_tokens'])} / out {show(m['output_tokens'])}",
                  f"- Tools: {', '.join(m['tools']) or 'none'}"]
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
