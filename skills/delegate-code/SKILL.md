---
name: delegate-code
metadata:
  version: "0.14.0"
description: Delegates a scoped implementation task to a local Codex, Claude Code or Antigravity CLI in its own git worktree, then reviews the diff and the returned Outcome and Next step. Use when the user asks to hand coding work to Codex, Claude or Antigravity from another agent. Avoid for read-only opinions, image generation, or work the current agent should do itself.
---

# Delegate Code

Use this workflow to give a write task to another local agent while the calling agent keeps planning and review. The worker edits files in a separate git worktree. The caller checks the diff before anything reaches the user's branch. It needs a Git repository and an installed, already-authenticated worker CLI. It installs nothing and never reads or prints credentials.

| Worker | Good for | Command |
| --- | --- | --- |
| `codex` | Routine implementation, tests, refactors; usually the largest quota | `codex exec` |
| `claude` | Hard design, tricky code, careful review fixes | `claude -p` |
| `agy` | Antigravity with Gemini models; a second opinion in code, or when the others are out of quota | `agy -p` |

Use the worker the user names. If they name none, ask, or pick `codex` for routine work and `claude` for hard design. Never delegate to the agent you are running in. Any of these agents, or Cursor, can be the caller; Cursor is never a worker. Use `second-opinion` for a read-only second opinion and `codex-image` for images.

**Code Mode runner.** When Python 3.12+ is available, use `scripts/run_worker.py` (standard library only, in this skill's folder) for steps 6, 7, 8 and 9 instead of hand-built shell pipelines. Run it from the repository root as `python3 <skill-dir>/scripts/run_worker.py <subcommand>`. It resolves the worker binary on `PATH` (never a shell alias), validates every slug, path, model and session id, and prints one JSON object per call:

| Subcommand | Workflow step | What it does |
| --- | --- | --- |
| `start` | 6, 7 | Checks `.delegate-code/` is ignored, uses the step 4 worktree (`--existing-worktree`) or creates it, writes the run note, runs the worker in restricted mode (unless `--full-access`) under a 1800 s watchdog, and returns `status`, `session_id`, `worker_status`, `outcome` and `next_step` |
| `resume` | 8 | Sends `--fix-file` to the same session with the same access mode and records the latest session id; a capped Claude run needs the remaining `--max-budget-usd` |
| `status` | checks | Read-only `running`, `finished`, `abandoned` or `unknown` per delegation |
| `bundle` | 8 | Writes `.delegate-code/<slug>.review/` and returns the `fingerprint` |
| `apply` | 9 | With `--confirm`, refuses a `--expected-fingerprint` mismatch, then `git apply`s the staged diff into the main checkout; keeps the worktree |
| `cleanup` | 9 | With `--confirm`, removes a `finished` or `abandoned` worktree, run note, review folder and branch |

The runner does not replace any rule below: the caller still writes the brief, runs the check command and reviews the diff, and passes `--full-access`, `--confirm` or a spend cap only after the user approves it. A `null` `worker_status`, `outcome` or `next_step` means the run is unverified.

Read `references/providers.md` before step 6 for exact CLI flags, resume syntax, model and effort flags, spend caps, watchdog wrapping and troubleshooting. Read `references/multi-and-status.md` when checking existing delegations, cleaning up `.delegate-code/`, or splitting one request into several delegations.

Steps that need no approval once the user asked for the delegation: reading repository files, writing the brief and plan in a temporary directory, creating the worktree and branch under an ignored `.delegate-code/`, running the check command and the worker in restricted mode, reading diffs, running `second-opinion` reviews, and sending the one correction. Ask before full access, a spend above the user's limit, applying or merging, and anything outside the repository.

## Workflow

1. **Confirm the handoff.** Delegate only when the user asked another agent to do the work. Ask before delegating if the task is unclear, touches secrets or production, or needs decisions nobody has made yet.
2. **Write the brief** in `brief.md` in a new temporary directory outside the repository (`mktemp -d`, use its absolute path as `$TASK`). Use `what-to-ask` if it is installed. The brief must stand on its own, because the worker sees none of this chat. It states:
   - the goal and the done criteria;
   - the files the worker may change, as a list of paths or globs, and what must not change; step 8 checks the diff against this list;
   - decisions already settled (if code evidence contradicts one, stop and flag it as `BLOCKED` or `NEEDS_CONTEXT` rather than silently working around it);
   - how far changes may reach: by default, the smallest change that meets the done criteria. When consolidation is wanted, name the duplicated code and the shared helper or file to move it into. Do not tell the worker it owns the codebase: that invites unrequested refactors, tests and behavior changes that a scope rule does not cancel;
   - the exact check command that proves the change;
   - "Do not commit, push, or edit outside this directory.";
   - the required ending, quoted in full:

     ```text
     Diagnose and fix ordinary obstacles yourself, such as a failing test,
     an import or type error, or a flaky command worth one retry, using only
     the files you may change. Stop only for a real blocker: a fix that needs
     a file outside your list, a missing permission or dependency, or a
     settled decision the code contradicts.
     End your final message with these lines, each separated by a blank line:
     **Status:** DONE, DONE_WITH_CONCERNS, BLOCKED or NEEDS_CONTEXT.
     **Outcome:** one sentence: what changed and whether the check passed.
     **Next step:** none, or the one action left for a human.
     If the brief leaves a choice that would change the result, stop before
     editing and answer NEEDS_CONTEXT with your questions; do not guess.
     Never weaken, skip or delete a test to make the check pass.
     ```

   Leave out credentials, private data, and chat history the task does not need. Reread the brief as the worker, fill anything it would have to guess from the repo or chat, and ask the user only if a guess would change the result. Remove any step the worker must not take (migration, deletion, production or network side effect) unless the user approves it first.
3. **Preflight.**
   - Run `which -a <worker>`, `<worker> --version` and the command's `--help` (`codex exec --help`, `claude --help` or `agy --help`). Use only flags the help lists.
   - If `type -a <worker>` shows a shell function or alias wrapping the CLI, call the resolved binary path from `which -a` or `command <worker>` in step 6 so the wrapper cannot widen access or break flag order.
   - Run `git check-ignore -q .delegate-code/x`. If `.delegate-code/` is not ignored, ask the user once whether to ignore both `.second-opinion/` and `.delegate-code/` in this repository; on a yes, add the missing ones to `.git/info/exclude` (or `.gitignore` if the user prefers):

     ```sh
     for d in .second-opinion .delegate-code; do
       git check-ignore -q "$d/x" || echo "$d/" >> "$(git rev-parse --git-path info/exclude)"
     done
     ```
   - Check `git status --short`. The worktree starts from `HEAD`, so uncommitted changes are not in it. Tell the user before continuing without them.
4. **Create the worktree** inside the ignored folder:

   ```sh
   git worktree add -b delegate-code/<slug> .delegate-code/<slug> HEAD
   ```

   Run the brief's check command once in the new worktree before the worker starts:
   - If it cannot run (missing dependency, wrong path, missing script), fix the command in the brief or tell the user. Do not send a worker a check it cannot run.
   - If it already passes, it cannot prove the change. Add a failing check or say in the brief what the check must newly show.
   - Record the baseline result and list the files the check depends on (test files, fixtures, check script or config) for step 8.
5. **Choose the access mode.**
   - **Restricted** (default): the worker edits files in the worktree and runs only the commands it needs.
   - **Full access**: no approvals and no worker sandbox. Use only when the user asks for it for this run or states it in their standing instructions. The worktree is then the only boundary, so the no-edit-outside rule and step 8's scope check matter more.
6. **Run the worker** from inside the worktree (`cd .delegate-code/<slug>`), in the background when the host supports it. With the runner, create the worktree and run the baseline as in step 4, then launch with `start --existing-worktree`, which checks that the path is a registered worktree on `delegate-code/<slug>` with no run note yet. Otherwise follow `references/providers.md` for the exact command, optional model and effort flags, spend cap, hard watchdog (`perl -e 'alarm shift; exec @ARGV' 1800` or `timeout 1800`), run note (`.delegate-code/<slug>.run`) and status classification (`ok`, `empty`, `timeout`, `failed`).
   - Treat anything other than `ok` as a failed run. Do not switch to another agent or API without asking the user.
   - Distinguish ordinary obstacles (missing local context answerable from the repo, transient environment glitches safe to retry) from hard blockers (withheld permissions, quota limits, outside-directory edits, unapproved side effects): resolve ordinary obstacles before stopping, and stop immediately on hard blockers.
7. **Read the result** from `last.md` (Codex), `result` in `result.json` (Claude) or `response` in `result.json` (Antigravity). Take `Status`, `Outcome` and `Next step`; if missing, treat the run as unverified rather than failed:
   - `DONE`: go to step 8.
   - `DONE_WITH_CONCERNS`: go to step 8 and verify each concern.
   - `NEEDS_CONTEXT`: answer from the repository or chat, or ask the user, then send the answers as a resumed correction; do not resend the unchanged brief.
   - `BLOCKED`: report the blocker and stop; do not retry the same worker with the same brief.
8. **Verify it yourself.** Do not rely on the worker's report:
   - check scope with `git -C .delegate-code/<slug> status --short` and `git -C .delegate-code/<slug> diff`, and confirm `git status --short` in the main checkout shows no new changes;
   - list and review every changed file outside the brief's allowed file list, even when the edit looks harmless;
   - run the brief's check command in the worktree and compare it with the step 4 baseline;
   - if the diff touches any file the check depends on, review those edits first and report them, confirming no test was weakened, skipped or deleted;
   - review the diff against the brief.

   **Independent review.** The caller planned the task and is not neutral. When the user asks for an independent review, or the change is risky (security, data, public API), run `second-opinion` with a provider that is neither the worker nor you. Put the bundle in `.delegate-code/<slug>.review/` (next to the worktree, not inside it):

   ```sh
   R=.delegate-code/<slug>.review && mkdir -p "$R"
   cp "$TASK/brief.md" "$R/" && cp <worker final message> "$R/final.md"
   git -C .delegate-code/<slug> add -A && git -C .delegate-code/<slug> diff --cached > "$R/diff.patch"
   ```

   Ask the reviewer for two separate answers: **Brief** (met, not met, or cannot verify from the diff for each done criterion) and **Findings** (Critical, Important or Minor with file and line). Confirm every finding in the code or with a failing test before acting on it; drop unconfirmed ones or label them unverified. Correct only confirmed Critical and Important findings and unmet criteria; list Minor ones in the report.

   **Correction and repeated-failure consult.** Compact what you send. Write `fix.md` (and any diagnostic prompt) as the exact failing assertion or error line, the confirmed findings with file and line, and the unmet done criterion. Leave out full logs, passing output, unconfirmed review prose and earlier diffs; point to files by path instead of pasting them. If something misses the brief, send one short correction to the same session from inside the worktree using the resume command in `references/providers.md` (same access mode; you may step up effort one level if the first run used a lower explicit effort and the CLI supports it). If the worker still fails or repeats the same error, and an eligible third CLI (`second-opinion`, neither the worker nor you) is installed and not declined, you may run one bounded read-only diagnostic consult on the brief, diff and error output before stopping and reporting what is left. Never start an automatic fix-and-review loop.

   **Fingerprint the reviewed state.** Once the worker exits and review is done, hash the whole worktree (untracked files included; staging commits nothing). The runner's `bundle` writes the review bundle above and returns this hash as `fingerprint`:

   ```sh
   git -C .delegate-code/<slug> add -A && git -C .delegate-code/<slug> write-tree
   ```
9. **Report and apply on approval.** Report the worker, access mode, branch, worktree path, worker Status and Outcome, your check result, review findings, and fingerprint. Do not merge, commit, push or remove the worktree until the user approves.
   - On approval, rerun the fingerprint command first. If the hash differs, stop, show what changed, and review again before applying. The runner's `apply --expected-fingerprint <hash> --confirm` does this check and the apply below in one call, and `cleanup --confirm` does the removal step.
   - The worker does not commit, so `git merge delegate-code/<slug>` would bring in nothing. Apply the staged diff into the main checkout:

     ```sh
     git -C .delegate-code/<slug> diff --cached | git apply
     ```
   - Rerun the brief's check command in the main checkout (`HEAD` may have moved). If it fails, stop, keep the worktree and branch, and report the failure.
   - If it passes, run `git worktree remove --force .delegate-code/<slug>`, remove `.delegate-code/<slug>.run` and any `.delegate-code/<slug>.review/` folder, and run `git branch -D delegate-code/<slug>`. Keep `$TASK` until the user is done with it.
   - Commit on the target branch (or commit in the worktree and merge) before creating any dependent worktree from `HEAD`, or when the user asks for a commit.

## Boundaries

- One task per worktree, each on its own branch.
- The worker edits only inside its worktree; the caller does all merging.
- Exclude `.delegate-code/` from test discovery or linters if whole-repo scanners pick it up.
- Usage, quota and data handling follow the user's account for each worker service.
