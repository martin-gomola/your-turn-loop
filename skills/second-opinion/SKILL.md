---
name: second-opinion
metadata:
  version: "0.6.0"
description: Asks Claude, Anthropic, Codex, OpenAI, Gemini, Google, or Antigravity through installed local CLIs and saves a repository-local audit artifact. Use for an explicitly requested external-model second opinion, critique, brainstorm, review, or research pass.
---

# Second Opinion

Requires Python 3, Git and the explicitly selected authenticated CLI. Requests send the scoped prompt to that provider; audit artifacts stay in the target repository.

Use the Claude, Codex or Antigravity CLI as a focused external advisor. Keep the
execution local, read-only, independent of OMX, and auditable.

## Usage

```text
$second-opinion claude <question or task>
$second-opinion gemini <question or task>
$second-opinion codex <question or task>
```

Natural-language requests such as "ask Claude", "ask Codex", "ask Gemini", or "get an
external second opinion" use the same workflow.

## Select the Provider

- Use `claude` when the user names Claude or Anthropic.
- Use `codex` when the user names Codex, OpenAI, or GPT.
- Use `gemini` through the `agy` binary when the user names Gemini, Google, or
  Antigravity.
- If the user requests an external second opinion without naming a provider,
  use an installed provider that fits the task and state the choice.
- Never substitute another provider when the user names one explicitly.
- Never ask the agent you are running in; a second opinion comes from a
  different model.
- Never use the deprecated `gemini` binary, an OMX command, MCP, or web search
  as a silent fallback.

## Validate the Local CLI

Check only the selected provider:

```bash
command -v claude
command -v codex
command -v agy
```

If the required binary is missing, stop and report the missing dependency. Do
not install software or change providers unless the user asks.

## Build the Prompt

Include:

1. The user's exact objective and requested provider.
2. The exact review target: uncommitted changes, a named base branch, one
   commit, specific files, a plan or architecture document, or supplied text.
3. The minimum local context needed to answer well.
4. The expected output shape and a concise response budget appropriate to the question; preserve full raw output in the artifact.
5. A requirement to separate evidence, inference, and uncertainty.
6. The checks you already ran on the target, each with its exact command and result, labeled
   "reported by the author, not rerun". A read-only advisor cannot run them, so without this it
   calls a passing test suite unverifiable. Ask it to treat these as reported evidence and to say
   if the target suggests a check is missing or would not cover the change.
7. This completion-authority prefix:

```text
This read-only review is already authorized. Begin immediately; do not present a plan and do not ask for confirmation. Return the completed review in this response. Do not modify files or external systems.
```

When the user supplies a ready prompt, such as one drafted with `what-to-ask`,
keep its task, scope, and constraints as written and add only the missing items
above.

Do not assume `main`, the current branch, or the entire repository when the
review target is not established. Resolve the target from the user's request
or current task context; ask one short question only when different targets
would materially change the review.

Do not include secrets, credentials, auth files, unrelated repository content,
or private data that the user did not place in scope.

## Run the Advisor

Use `scripts/run_advisor.py` from this skill package. It is a standard-library
Python runner that checks the selected CLI, enforces read-only provider flags,
applies a bounded timeout, classifies the result, performs at most one safe
recovery retry, and writes the audit artifact.

Create UTF-8 task and prompt files in a temporary directory using a native
filesystem API. Do not construct them with shell interpolation. Then run:

```bash
python3 <skill-dir>/scripts/run_advisor.py \
  --provider <claude|codex|gemini> \
  --repo-root <repo-root> \
  --task-file <task-file> \
  --prompt-file <prompt-file> \
  --timeout-seconds 600 \
  [--resume <session_id>]
```

The runner invokes these provider command shapes:

```bash
claude -p --permission-mode plan --output-format json \
  [--resume <session_id>] "<final prompt>"
```

```bash
agy --mode plan --sandbox --print-timeout 600s \
  --output-format text -p="<final prompt>"
```

```bash
codex exec --sandbox read-only --ephemeral "<final prompt>"
```

The runner passes arguments through a native process API. Never replace it with
`eval`, interpolate the prompt into a shell program, enable dangerous permission
bypasses, or allow the advisor to write to the workspace. Remove only the
temporary task and prompt files after the runner exits.

## Continue a Claude Conversation

Every Claude result includes `session_id` in the runner's JSON and in the
artifact. To ask a follow-up in the same conversation, for example "now check the
rollback section too", rerun the runner with `--resume <session_id>` and a prompt
that contains only the follow-up. Claude keeps the earlier prompt, its answer, and
the files it read, and stays in plan mode. Each call still writes its own
artifact. Keep the latest `session_id` and reuse it for the next follow-up.

Use `--resume` only for a follow-up the user asked for, and only with a
`session_id` that an earlier `second-opinion` result returned in this repository. Antigravity
has no resume, and Codex runs with `--ephemeral`, so `gemini` and `codex` calls always start fresh.

## Interpret the Status

For a review or critique, request a compact Markdown contract: findings with file/source evidence, severity (Critical, Important or Minor), impact and suggested action; uncertainties; and the overall assessment. When the review target comes with a task brief, such as a `delegate-code` diff, also ask whether each done criterion is met, not met, or cannot be verified from the diff, as a separate answer from the findings. For brainstorming or research, adapt those fields to the requested deliverable. Define the severities in the prompt: Critical only for data loss, a security hole, or an action taken without the user's approval, and only with a concrete failure scenario (the input or state, and what breaks); a real defect without one is Important; wording, duplication and polish are Minor. Use a default response budget of about 1,000 words unless the task warrants another explicit bound. This is a prompt budget, not an enforced CLI token limit. Keep the raw response intact in the artifact and return a short synthesis to the parent chat.

Define the review's acceptance criteria in the prompt. If the advisor contradicts local evidence, distinguish both positions and verify the disputed claim before recommending action. Escalate unresolved high-impact disagreements or an exhausted recovery attempt with the evidence and smallest next check; do not silently accept the advisor's verdict or start another review loop.

The synthesis must not change the advisor's verdicts. When you verify a finding and rate its severity differently, report both side by side, the advisor's and yours, with the one-line reason; never replace the advisor's rating silently. Report the verdict for every finding or proposal, not a subset; an "agree with changes" or "modify" verdict is approval with a change, not a rejection. Keep a priority, order or "do first" answer apart from the verdicts: it is sequencing, not scope, so it never narrows what the advisor endorsed or becomes a question that drops the rest. When the prompt asks the advisor to prioritize, ask for it as an order over the endorsed items, not as a choice of which to keep.

The runner prints a JSON result and returns one of these statuses:

- `ok`: completed non-empty response.
- `timeout`: the bounded execution expired; partial output is preserved.
- `empty`: the CLI exited successfully without a response.
- `protocol`: the response only requested confirmation or permission.
- `permission`: Antigravity auto-denied a command because headless mode could not prompt.
- `failed`: missing dependency, unsafe artifact path, non-zero CLI exit, or
  another execution failure.

On `protocol`, the runner retries exactly once with a corrective non-interactive
prefix. Do not answer the advisor's confirmation request.

For `gemini`, the runner already asks for native read-only file tools and no terminal commands in
the first prompt, since headless Antigravity denies commands; a `permission` status is then rarer.

On `permission`, the runner retries exactly once with a command-free correction:
use native read-only file tools and supplied context, state uncertainty where evidence is
unavailable, and return the completed opinion without requesting permissions. The recovery must
not edit Antigravity settings, add allow rules, switch providers, drop sandbox mode, or use
`--dangerously-skip-permissions`. If the retry is also permission-blocked, preserve both attempts,
return `permission`, and report that an exact command grant requires explicit user authorization.

Do not retry `empty`, `timeout`, or `failed` automatically.

## Save the Artifact

After the command completes, create:

```text
<repo-root>/.second-opinion/second-opinion-<provider>-<slug>-<UTC timestamp>.md
```

Use a filesystem-safe lowercase slug and a timestamp such as
`20260825T143000Z`. Before invoking the provider, the runner requires the final
artifact path to pass `git check-ignore`; it refuses to run otherwise. Keep
`.second-opinion/` local and ignored by Git. If it is not ignored, ask the user once
whether to ignore both working folders of these skills in this repository, so
`delegate-code` will not ask again. On a yes, add the missing ones to the
untracked exclude file (or to `.gitignore` if the user prefers):

```sh
for d in .second-opinion .delegate-code; do
  git check-ignore -q "$d/x" || echo "$d/" >> "$(git rev-parse --git-path info/exclude)"
done
```

The artifact must contain:

1. Original user task
2. Provider and final prompt
3. Exact CLI command shape, excluding secrets
4. Raw stdout and stderr
5. Classified status and process exit status
6. Attempt count and raw output from every attempt
7. Concise execution summary and action item

Preserve raw output verbatim. Clearly label any summary written by the current
agent so it is not confused with the advisor's words.

## Verify and Return

- Require `status: ok`; a zero process exit with `empty`, `protocol`, or
  `permission` is not success.
- Confirm the artifact exists under the current repository root.
- Treat the external answer as advisory. Verify material claims against local
  files, official documentation, or runtime evidence before acting on them.
- Return the provider, concise result, artifact path, and any verification gap.
  The concise result lists every finding's verdict; any suggested order follows
  separately as order.

Stop after delivering the requested external opinion and artifact unless the
user also asked to implement its recommendations. Do not start an automatic
fix-and-review loop. A later review pass requires an explicit user request or an
already-approved quality-gate workflow.
