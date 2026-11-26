# Multi-Delegation Plans and Status Checks

Load this reference when checking running or leftover delegations in `.delegate-code/`, or when splitting one request into two or more delegations.

## Check on Delegations

When the user asks how a delegation is going, or what is left in `.delegate-code/`, read each `.delegate-code/*.run` note and its worktree. Only read; change nothing.

- **Progress.** Report the time since `started` and the files changed so far (`git -C .delegate-code/<slug> status --short`), which works for every worker. For Codex, also give the last step from the tail of `$TASK/events.jsonl` in plain words, such as "ran the tests" or "edited src/list.py". Claude and Antigravity write nothing until they finish.
- **State.**
  - `running`: `pid` is set and `kill -0 <pid>` succeeds;
  - `finished`: the final message exists (`last.md` or `result.json` in `task`) and is not yet reviewed or merged;
  - `abandoned`: no live process, no final message, and the time limit has passed;
  - `unknown`: anything else, including a worktree with no note. Never call it abandoned without that evidence.
- **Cleanup.** List what each finished or abandoned worktree holds (changes, commits, or nothing) and offer to review, keep, or remove it. Remove a worktree, its branch, and its note only after the user approves, and never remove one that is `running` or `unknown`.

## Several Delegations

**Split only when it pays.** Keep a request as one delegation when one worker can finish it within the watchdog limit and its parts share most of their files. Split it when the parts touch separate files and can run in parallel, or when one run would be too large to review as one diff. Each extra task costs a brief, a worktree and a review, so do not split for neatness.

When one request becomes more than one delegation, create one shared plan directory (`mktemp -d`, use its absolute path as `$PLAN`) and write `$PLAN/plan.md` before any worktree exists. List each task with its slug, its files (the same list its brief carries) and the slugs it depends on. Give each task its own `$TASK` directory, such as `$PLAN/<slug>/` or a separate `mktemp -d`, so parallel tasks never overwrite each other's `brief.md`, `last.md`, `events.jsonl`, `result.json`, `worker.log` or `diff.patch`.

- If two tasks list the same file, make one depend on the other. Never run them at the same time.
- Every worktree branches from `HEAD`. A task with no dependencies starts from `HEAD` as it is. A dependent task starts only after each task it depends on is reviewed and committed on the target branch, so its worktree, created from `HEAD`, sees their code. An applied but uncommitted change is not in `HEAD`.
- Run independent tasks in parallel, each through steps 2 to 9 on its own. If one task is waiting on merge approval or user input, keep running any other independent tasks and their reviews before pausing, so the user can review and approve them together.
- If a task fails or is blocked, do not start the tasks that depend on it. Report them as not started.
- **Progress notes.** When a task is committed, fails or is blocked, add one line under `## Done` in `plan.md`: the slug, its state, and what a later task must know, such as a new function name, a changed interface or a trap the worker hit. Before you write a dependent task's brief, read those lines and put the ones it needs into the brief. Its worker cannot see the earlier runs.
