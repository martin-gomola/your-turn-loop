---
name: orchestrate
metadata:
  version: "0.4.2"
description: Takes one goal with several parts, splits it into tasks, assigns a worker and reviewer among the local Codex, Claude Code and Antigravity CLIs and routes images, then runs the Your Turn Loop skills to the end without the user pasting between agents. Use when the user has a multi-task goal to coordinate across agents, names roles such as "worker codex, reviewer claude", or asks the agent to orchestrate. Avoid for a single brief, one read-only review, one delegation of one scoped task, an image alone, or work the current agent should do itself.
---

# Orchestrate

Use this workflow as the entry point of the Your Turn Loop. The calling agent
keeps the plan, the reviews and the decisions; other local agents do the work.
It composes the companion skills and does not restate their rules: when a step
names a skill, follow that skill. It needs the companion skills it uses to be
installed and the agents' CLIs to be installed and signed in. It installs
nothing and never reads or prints credentials.

## Roles

| Role | Does | Can be | Through |
| --- | --- | --- | --- |
| Worker | Writes code in its own worktree | `codex`, `claude` or `agy` | `delegate-code` |
| Reviewer | Gives a read-only review of a plan or diff | `claude`, `codex` or `gemini` (`agy`) | `second-opinion` |

Worker and reviewer are delegated roles. Their own skills' rules apply: never
the agent you are running in; Cursor orchestrates but is never a worker or
reviewer; the reviewer is neither the worker nor you.

Images are not a delegated role. Make them with your own image tool when you
have one, Codex included. Without one, hand them to a local Codex through
`codex-image`.

Resolve the worker and the reviewer once per goal, each on its own through
rules 1, 2 and 3 in this order:

1. **The request.** Use roles the user names, such as "worker: codex, reviewer:
   claude", including a different worker per task or optional model and effort
   hints such as "worker: codex (medium), reviewer: claude (sonnet, high)".
2. **Standing preference.** Otherwise use a line the user keeps in the agent's
   instructions (`AGENTS.md`, `CLAUDE.md` or the agent's global instructions),
   such as `Your Turn roles: worker codex, reviewer claude` (with optional
   model and effort hints in parentheses). Say where the roles came from.
3. **Proposal.** Otherwise propose `codex` as worker for routine code and
   `claude` for hard design, and as reviewer an installed CLI among `claude`,
   `codex` and `gemini` that is neither that worker nor you. Ask once and wait
   for the answer before the first delegation.

Keep each CLI's default model and effort when no hint is given; pass optional
model or effort flags only when confirmed in that CLI's `--help`.

If the user names only one role, such as the worker, and the other is not in
standing instructions, propose the missing role under rule 3 and wait once
before the first delegation.

If a named role breaks a rule above or its CLI is missing, say which and ask.
Do not swap in another agent without the user's approval.

## Workflow

1. **Pin the goal.** State the goal, the done criteria and what is out of scope
   in a few lines. Settle open points as `what-to-ask` does.
2. **Plan.** Split the goal as `delegate-code` describes under "Several
   delegations", and keep its `plan.md` for two or more code tasks.
   Write the goal, the done criteria and what is out of scope at the top of that `plan.md`. Each
   delegated task costs a brief, a worktree, a worker run and a review, and
   uses the user's quota. Delegate a part only when it is independent of the
   others or too large to review as one diff; keep tightly coupled or small
   parts in one task, or do them yourself as local. Give each
   task its kind (code, image, review, or local for a task you do yourself)
   and, for code, its worker. Show the
   plan and roles in a short table, then start, unless a role is still waiting
   on the user.
3. **Brief each task** with `what-to-ask`, carrying in the progress notes that
   `delegate-code` keeps for later tasks.
4. **Run each task** in the order the plan allows:
   - code: `delegate-code` with the task's worker;
   - image: your own image tool, or `codex-image`;
   - review of a plan or design before building: `second-opinion` with the
     reviewer;
   - local: do it yourself.
5. **Review and stuck-worker consult.** For every code task, follow
   `delegate-code`'s verification, then its independent review through
   `second-opinion` with the reviewer resolved above. Without a reviewer, which
   applies only when no eligible third CLI is installed or the user declines
   one, run that review only when the change is risky. When a worker repeats
   the same failure after a correction and a reviewer is resolved, run one
   bounded read-only diagnostic pass through `second-opinion` on the brief,
   diff and error output before stopping and reporting to the user. Report the
   reviewer's answer as `second-opinion` requires.
6. **Stop only when the user must act:**
   - to confirm proposed roles, or to name a role whose CLI is missing;
   - for a decision that changes the result;
   - for a worker's `NEEDS_CONTEXT` question that the repository and this
     chat cannot answer;
   - to approve a merge. Merge nothing until the user approves it, and start a
     dependent task only after the tasks it depends on are merged;
   - before a step with an outside effect: push, release, deploy, delete, or a
     message or post;
   - after a failed, `BLOCKED` or out-of-quota run, handled as
     `delegate-code` says.

   Steps that need no approval once roles are settled: reading the
   repository, writing plans and briefs, starting delegations in restricted
   mode, running checks, reading diffs, running read-only reviews and
   diagnostic consults, and sending each task's one correction.

   Diagnose ordinary obstacles (missing local context answerable from the repo,
   transient tool glitches safe to retry) and work through them instead of
   stopping early; stop immediately on deliberate hard blockers above. When one
   task is waiting on merge approval or user input, keep running any other
   independent tasks and their reviews before pausing, so the user can review
   and approve ready tasks together. Otherwise keep going until every task is
   ready to merge, merged, failed, blocked or waiting on the user.
7. **Report** one table for the goal.
   When `plan.md` exists, judge its done criteria against each code task's brief and worktree, including a task that is ready to merge and not yet on `HEAD`. A criterion that no brief states is one more table row with status `not started`. Do not start that row. Add no such row when every criterion is already stated in some brief. Skip this judgment while any planned code task is failed, blocked or still running, and name that task in the table.

   | Task | Agent and role | Status | Check | Metrics | Left for you |
   | --- | --- | --- | --- | --- | --- |

   Status is merged, ready to merge, failed, blocked or not started. Metrics
   is the runner's `metrics` in one short cell, such as `312s, 14 turns, 1.2M
   tok, $0.84`, keeping only what was reported, or `-` when nothing was. End
   with the `your-turn-handoff` block when it is installed, with the action
   that unblocks the most work as `Next step`. That block is the thread's only
   human handoff: finished workers return `Next step: none`.

## Boundaries

- Work moves only through the agents' CLIs, their worktrees and the files the
  companion skills write. No skill types into another agent's chat or reads its
  history, and none uses the Your Turn inbox to pass messages.
- Tasks the user wants you to do yourself stay with you.
- A companion skill that is not installed cannot be replaced from memory. Name
  it, skip that kind of task, and say how to install it.
- Data and quota follow each companion skill's own boundaries.
