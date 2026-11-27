# Advisor Runner, Command Shapes and Audit Artifact

Load this reference when inspecting provider CLI flags, recovery behavior, or the audit artifact layout for `second-opinion`.

## Provider Command Shapes

`scripts/run_advisor.py` invokes these read-only commands through a native process API, omitting `--model` and `--effort` unless passed to the runner:

```bash
claude -p --permission-mode plan --output-format json \
  [--model <model>] [--effort <level>] [--resume <session_id>] "<final prompt>"
```

```bash
agy --mode plan --sandbox --print-timeout 600s \
  --output-format text [--model <model>] [--effort <level>] -p="<final prompt>"
```

```bash
codex exec --sandbox read-only --ephemeral \
  [--model <model>] [-c model_reasoning_effort="<level>"] "<final prompt>"
```

Never replace the runner with `eval`, interpolate the prompt into a shell program, enable dangerous permission bypasses, or allow the advisor to write to the workspace. Remove only the temporary task and prompt files after the runner exits.

## Status Classification and Recovery

The runner prints a JSON result and returns one of these statuses:

- `ok`: completed non-empty response.
- `timeout`: the bounded execution expired; partial output is preserved.
- `empty`: the CLI exited successfully without a response.
- `protocol`: the response only requested confirmation or permission.
- `permission`: Antigravity auto-denied a command because headless mode could not prompt.
- `refusal`: Claude's safety classifier declined the request (`stop_reason: "refusal"` in its JSON). A common benign cause is a prompt asking it to write out its internal reasoning; remove that and rerun only on the user's request.
- `failed`: missing dependency, unsafe artifact path, non-zero CLI exit, or another execution failure.

Recovery boundaries:

- On `protocol`, the runner retries exactly once with a corrective non-interactive prefix. Do not answer the advisor's confirmation request.
- For `gemini`, the runner asks for native read-only file tools and no terminal commands in the first prompt, because headless Antigravity denies commands.
- On `permission`, the runner retries exactly once with a command-free correction: use native read-only file tools and supplied context, state uncertainty where evidence is unavailable, and return the completed opinion without requesting permissions. Recovery must not edit Antigravity settings, add allow rules, switch providers, drop sandbox mode, or use `--dangerously-skip-permissions`. If the retry is also permission-blocked, preserve both attempts, return `permission`, and report that an exact command grant requires explicit user authorization.
- Do not retry `empty`, `timeout`, `refusal`, or `failed` automatically.

## Error Recovery

| Failure | Cause | Recovery |
| --- | --- | --- |
| Runner refuses to run before invoking the provider | The artifact path fails `git check-ignore` because `.second-opinion/` is not ignored | Ask the user once, add the working folders to the exclude file as shown below, then rerun. |
| `failed` with a missing dependency | The provider CLI (`claude`, `agy` or `codex`) is not on `PATH` | Report the missing CLI; use another installed provider only if the user agrees. |
| `timeout` | The bounded execution expired | Report the partial output preserved in the artifact; rerun with a narrower prompt only on the user's request. |
| `permission` after the single retry | Headless Antigravity denied a command twice | Preserve both attempts and report that an exact command grant requires explicit user authorization. |
| `refusal` | Claude's safety classifier declined, often because the prompt asked it to write out its internal reasoning | Remove that request and rerun only on the user's request. |

## Ignore Working Folders and Save the Artifact

Artifacts are saved under:

```text
<repo-root>/.second-opinion/second-opinion-<provider>-<slug>-<UTC timestamp>.md
```

Use a filesystem-safe lowercase slug and a UTC timestamp such as `20260825T143000Z`. Before invoking the provider, the runner requires the final artifact path to pass `git check-ignore`; it refuses to run otherwise. Keep `.second-opinion/` local and ignored by Git. If it is not ignored, ask the user once whether to ignore both working folders of these skills in this repository, so `delegate-code` will not ask again. On a yes, add the missing ones to the untracked exclude file (or to `.gitignore` if the user prefers):

```sh
for d in .second-opinion .delegate-code; do
  git check-ignore -q "$d/x" || echo "$d/" >> "$(git rev-parse --git-path info/exclude)"
done
```

Each artifact contains:

1. Original user task
2. Provider and final prompt
3. Exact CLI command shape, excluding secrets
4. Raw stdout and stderr
5. Classified status and process exit status
6. Attempt count and raw output from every attempt
7. Concise execution summary and action item

Preserve raw output verbatim. Clearly label any summary written by the current agent so it is not confused with the advisor's words.
