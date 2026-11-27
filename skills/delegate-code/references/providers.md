# Worker CLIs, Flags and Troubleshooting

Load this reference when invoking or resuming a worker in `delegate-code` step 6 or step 8. Invoke the binary path resolved in step 3, or `command <worker>`, so a shell function or alias cannot add flags such as `--yolo`.

## Codex (`codex exec`)

Restricted, then full access:

```sh
codex exec --sandbox workspace-write --json -o "$TASK/last.md" - \
  < "$TASK/brief.md" > "$TASK/events.jsonl" 2> "$TASK/worker.log"
codex exec --dangerously-bypass-approvals-and-sandbox --json \
  -o "$TASK/last.md" - < "$TASK/brief.md" > "$TASK/events.jsonl" 2> "$TASK/worker.log"
```

The session id is `thread_id` in the `thread.started` event of `events.jsonl`. Read the final message from `$TASK/last.md`.

Resume for a correction (restricted uses `-c 'sandbox_mode="workspace-write"'` because `resume` has no `--sandbox` flag):

```sh
codex exec resume <access flag> -o "$TASK/last.md" <thread_id> - < "$TASK/fix.md"
```

## Claude (`claude -p`)

Restricted, then full access:

```sh
claude -p --permission-mode acceptEdits --allowedTools "Bash(<check command>)" \
  --output-format json < "$TASK/brief.md" > "$TASK/result.json" 2> "$TASK/worker.log"
claude -p --permission-mode bypassPermissions \
  --output-format json < "$TASK/brief.md" > "$TASK/result.json" 2> "$TASK/worker.log"
```

`result.json` holds the final message in `result` and the session id in `session_id`. Check `is_error` and `subtype`: `is_error: true` or an error subtype such as `error_max_budget_usd` is a failed run even when the exit code is 0. In restricted mode, add one `--allowedTools` entry for each command the worker must run.

Resume for a correction:

```sh
claude -p <access flags> --resume <session_id> --output-format json \
  < "$TASK/fix.md" > "$TASK/result.json"
```

## Antigravity (`agy`)

`agy` takes the prompt as the `-p` argument, not on stdin. Restricted, then full access:

```sh
agy --mode accept-edits --sandbox --print-timeout 1800s --output-format json \
  --settings '{"permissions":{"allow":["command(<program>)"]}}' \
  -p="$(cat "$TASK/brief.md")" > "$TASK/result.json" 2> "$TASK/worker.log"
agy --dangerously-skip-permissions --print-timeout 1800s --output-format json \
  -p="$(cat "$TASK/brief.md")" > "$TASK/result.json" 2> "$TASK/worker.log"
```

`result.json` holds the final message in `response` and the session id in `conversation_id`. In restricted mode, allow each program the worker must run, such as `command(npm)` for `npm test`; the rule matches the program name. A denied command shows in `denied_actions` and can leave `response` empty.

Resume for a correction (Antigravity returns a new `conversation_id` after each resume; use the latest):

```sh
agy --conversation <conversation_id> <access flags> --output-format json \
  -p="$(cat "$TASK/fix.md")" > "$TASK/result.json"
```

## Model and Effort Flags

Keep the user's default model and effort unless the user names a model or effort, or sets it in standing role preferences. Never edit the worker's config. Pass only flags confirmed in `<worker> --help`:

- **Claude:** `--model <model>`, `--effort <low|medium|high|xhigh|max>`
- **Codex:** `--model <model>`, `-c model_reasoning_effort="<low|medium|high|xhigh|max>"`
- **Antigravity:** `--model <model>`, `--effort <low|medium|high|xhigh|max>`

On the single allowed correction pass (step 8), if the first run used a lower explicit effort and the CLI's `--help` supports an effort flag, you may step effort up one level (for example, `medium` to `high`) on that correction pass.

## Spend Cap

By default runs have no spend cap: on a subscription the user pays a flat fee, not per token, and the watchdog's time limit still stops a runaway run. Add `--max-budget-usd <usd>` to the Claude command only when the user gives a limit, or when the worker bills an API key per token and the user has not said to run without one. Several delegations share one total: split it so the caps add up to no more than the user's limit. A run stopped by the cap is a failed run; report it as that and do not raise the cap without asking. Codex and Antigravity have no spend flag, so the time limit is their only cap; say so when the user asked for a spend limit.

## Watchdog and Run Note

A worker that writes only to files gives no sign of life: `claude -p` prints nothing until its final answer, so a hung run and a working one both look like an empty file. Give every run a hard limit by prefixing the command with `perl -e 'alarm shift; exec @ARGV' 1800` (seconds; `timeout 1800` works too where installed). Classify the run:

- `ok`: exit 0 and a non-empty final message;
- `empty`: exit 0 but no final message;
- `timeout`: exit 142 (`perl`) or 124 (`timeout`);
- `failed`: any other exit code; read the tail of `worker.log`.

Treat anything but `ok` as a failed run. Do not wait on an empty file past the limit.

When the run starts, write `.delegate-code/<slug>.run` next to the worktree (not inside it, so the diff stays clean) with four lines: `worker=<name>`, `task=<$TASK>`, `started=<UTC time>` and `pid=<PID>` (from `$!` when started with `&`; leave it empty if the host does not show it). Delete it with the worktree.

## Error Recovery

| Failure | Cause | Recovery |
| --- | --- | --- |
| `Operation not permitted` or connection errors | A sandboxed caller, such as Claude Code's sandbox or Codex in `workspace-write` without network, blocks the worker from writing its own state (`~/.codex`, `~/.claude` or `~/.gemini`) or reaching its service | Run the worker outside the caller's sandbox with the user's approval. |
| `model_not_found` or "not supported with a ChatGPT account" | An old binary earlier on `PATH` does not know newer models | Compare `which -a` results and their versions, run the newest one, and tell the user which old binary shadows it. |
| Transport errors about unreachable MCP servers in `worker.log` | The worker's own config, not the run | Ignore them; judge the run by the exit code and the final message. |
| Exit 142 (`perl`) or 124 (`timeout`) | The watchdog limit expired | Classify the run as `timeout`, read the tail of `worker.log`, and report a failed run. |
| Entries in Antigravity `denied_actions` or another confirmed restricted-mode command denial | Restricted mode blocked a command the worker needed | Add the missing `--allowedTools` entry or `command(<program>)` rule on the one correction pass only if that command is already within the user's authorization; otherwise stop and ask the user. |
| Exit 0 with an empty final message and no confirmed denial | The worker ended without answering | Read the tail of `worker.log`, classify the run as `empty` (a failed run) and report it; do not retry automatically or widen permissions. |
| Claude `is_error: true` or a subtype such as `error_max_budget_usd` | The run failed or hit its spend cap despite exit 0 | Report a failed run; do not raise the cap without asking. |
