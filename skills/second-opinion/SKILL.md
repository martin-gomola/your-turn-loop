---
name: second-opinion
metadata:
  version: "0.9.0"
description: Asks Claude, Anthropic, Codex, OpenAI, Gemini, Google, or Antigravity through installed local CLIs and saves a repository-local audit artifact. Use for an explicitly requested external-model second opinion, critique, brainstorm, review, or research pass.
---

# Second Opinion

Requires Python 3, Git and the explicitly selected authenticated CLI. Requests send the scoped prompt to that provider; audit artifacts stay in the target repository.

Use the Claude, Codex or Antigravity CLI as a focused external advisor. Keep execution local, read-only, independent of OMX, and auditable. Read `references/runner-and-artifact.md` when you need exact provider CLI shapes, status recovery rules, folder-ignore setup, or the audit artifact layout.

## Usage

```text
$second-opinion claude <question or task>
$second-opinion gemini <question or task>
$second-opinion codex <question or task>
```

Natural-language requests such as "ask Claude", "ask Codex", "ask Gemini", or "get an external second opinion" use the same workflow.

## Select the Provider

- Use `claude` when the user names Claude or Anthropic.
- Use `codex` when the user names Codex, OpenAI, or GPT.
- Use `gemini` through the `agy` binary when the user names Gemini, Google, or Antigravity.
- If the user asks for a second opinion without naming a provider, pick an installed provider that fits the task and state the choice.
- Never substitute another provider when the user names one explicitly, and never ask the agent you are running in; a second opinion comes from a different model.
- Never use the deprecated `gemini` binary, an OMX command, MCP, or web search as a silent fallback.

Check only the selected binary (`command -v claude`, `command -v codex`, or `command -v agy`). If it is missing, stop and report it; do not install software or switch providers unless the user asks.

## Build the Prompt

Include:

1. The user's exact objective and requested provider.
2. The exact review target: uncommitted changes, a named base branch, one commit, specific files, a plan or architecture document, or supplied text.
3. The minimum local context needed to answer well.
4. The expected output shape. Ask for concise, evidence-backed findings without adding an arbitrary word or token budget unless the user asks for a bound; preserve full raw output in the artifact.
5. A requirement to separate evidence, inference, and uncertainty. Ask for conclusions with their evidence, not a written-out chain of reasoning: some providers decline prompts that ask them to reproduce internal reasoning.
6. The checks you already ran on the target, each with its exact command and result, labeled "reported by the author, not rerun". Ask the advisor to treat these as reported evidence and say if the target suggests a check is missing or would not cover the change.
7. This completion-authority prefix:

```text
This read-only review is already authorized. Begin immediately; do not present a plan and do not ask for confirmation. Return the completed review in this response. Do not modify files or external systems.
```

When the user supplies a ready prompt (such as one drafted with `what-to-ask`), keep its task, scope, and constraints as written and add only the missing items above.

When reviewing files prepared by another skill (such as a `delegate-code` brief, worker output and diff), place them in a Git-ignored repo-local folder such as `.delegate-code/<slug>.review/` so sandboxed providers read them with native file tools and large diffs stay out of CLI arguments.

Do not assume `main`, the current branch, or the entire repository when the review target is not established; ask one short question when different targets would materially change the review. Exclude secrets, credentials, auth files, unrelated content, and out-of-scope private data.

## Run the Advisor

Use `scripts/run_advisor.py`. Write UTF-8 task and prompt files in a temporary directory using a native filesystem API (no shell interpolation), then run:

```bash
python3 <skill-dir>/scripts/run_advisor.py \
  --provider <claude|codex|gemini> \
  --repo-root <repo-root> \
  --task-file <task-file> \
  --prompt-file <prompt-file> \
  --timeout-seconds 600 \
  [--model <model>] \
  [--effort <low|medium|high|xhigh|max>] \
  [--resume <session_id>]
```

Keep each provider's default model and effort unless the user names them or sets them in standing role preferences, and pass only flags supported by the installed CLI's `--help`. Remove only the temporary task and prompt files after the runner exits.

Before invoking the provider, the runner requires `<repo-root>/.second-opinion/...` to pass `git check-ignore`; if it is not ignored, ask the user once to ignore both `.second-opinion/` and `.delegate-code/` (see `references/runner-and-artifact.md`).

### Continue a Claude Conversation

Every Claude result includes `session_id` in the runner's JSON and artifact. For a user-requested follow-up in the same conversation, rerun the runner with `--resume <session_id>` and a prompt containing only the follow-up. Keep the latest `session_id`. Use `--resume` only with a `session_id` returned by an earlier `second-opinion` call in this repository. `gemini` (text output) and `codex` (`--ephemeral`) always start fresh.

## Interpret the Status and Synthesize

For a review or critique, request a compact Markdown contract: findings with file/source evidence, severity (`Critical`, `Important` or `Minor`), impact and suggested action; uncertainties; and an overall assessment. When the target includes a task brief (such as a `delegate-code` diff), also ask separately whether each done criterion is met, not met, or cannot be verified from the diff. For brainstorming or research, adapt the fields to the deliverable.

Define severities in the prompt: `Critical` only for data loss, a security hole, or an action taken without user approval, backed by a concrete failure scenario (input or state, and what breaks); a real defect without one is `Important`; wording, duplication and polish are `Minor`.

Keep the raw response verbatim in the artifact and return a short synthesis to the parent chat:

- **Verify before recommending:** if the advisor contradicts local evidence, distinguish both positions and verify the disputed claim before recommending action. Escalate unresolved high-impact disagreements or an exhausted recovery attempt with the evidence and smallest next check.
- **Preserve every verdict:** report the verdict for every finding or proposal, not a subset; an "agree with changes" or "modify" verdict is approval with a change, not a rejection. When you rate a finding's severity differently after checking the files, report both ratings side by side (the advisor's and yours) with a one-line reason; never replace the advisor's rating silently.
- **Keep order separate from scope:** a priority, order or "do first" answer sequences the endorsed items; it never drops the rest. When asking the advisor to prioritize, ask for an order over endorsed items, not a choice of which to keep.

The runner returns `ok`, `timeout`, `empty`, `protocol`, `permission`, `refusal`, or `failed` (see `references/runner-and-artifact.md` for recovery details). It retries `protocol` and `permission` at most once safely, and never auto-retries `empty`, `timeout`, `refusal`, or `failed`.

## Verify and Return

- Require `status: ok`; a zero exit code with `empty`, `protocol`, `permission`, or `refusal` is not success.
- Confirm the artifact exists under `<repo-root>/.second-opinion/`.
- Treat the external answer as advisory; verify material claims against local files, official docs, or runtime evidence before acting.
- Return the provider, concise result (listing every finding's verdict, with any suggested order separately), artifact path, and any verification gap.

Stop after delivering the opinion and artifact unless the user also asked to implement its recommendations or an approved quality-gate workflow applies. Do not start an automatic fix-and-review loop.
