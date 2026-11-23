# Work across agents

These skills cover one loop: plan the goal, write the brief, delegate it, and
bring the result back as the next step. Install each one only in the agents where
you need it. With `orchestrate`, the calling agent runs the plan, brief, delegate
and result steps itself; you step in for decisions, merges and the next goal.

| Step | Skill | What moves | Who moves it |
| --- | --- | --- | --- |
| Plan | [orchestrate](../skills/orchestrate/SKILL.md) | One goal becomes tasks with a worker and a reviewer, run through the skills below. | The agent plans and runs them. |
| Brief | [what-to-ask](../skills/what-to-ask/SKILL.md) | Rough notes or another agent's result become a self-contained prompt. | The agent drafts it. |
| Delegate a read-only opinion | [second-opinion](../skills/second-opinion/SKILL.md) | The prompt goes to the local `claude`, `codex` or `agy` CLI. | The agent runs the CLI. |
| Delegate an image | [codex-image](../skills/codex-image/SKILL.md) | An image brief goes to the local `codex exec`. | The agent runs the CLI. |
| Delegate implementation | [delegate-code](../skills/delegate-code/SKILL.md) | The prompt goes to `codex exec`, `claude -p` or `agy -p` in a new git worktree; the caller reviews the diff. | The agent runs the CLI. |
| Result | [your-turn-handoff](handoff.md) | The recipient ends with `Outcome` and `Next step`. | The recipient writes it. |
| Next step | what-to-ask | The pasted result becomes the next prompt. | You paste it back. |

## Example

1. In Cursor: "Orchestrate this: add the CSV export button in Atlas, with a
   short help page. Worker: codex, reviewer: claude." Cursor shows a plan of
   two tasks and starts, since both roles are named.
2. For each task Cursor writes the brief with what-to-ask and runs `codex exec`
   in a new worktree through delegate-code, then runs the check and reviews
   the diff itself.
3. Claude reviews each diff read-only through second-opinion. The calling agent
   receives the result and an audit file under `.second-opinion/`, with no copy
   and paste. The runner writes there only when Git ignores it.
4. Cursor stops with a table of both tasks, ready to merge. Merging stays with
   you; the help page, which depends on the export, starts only after you merge
   the export.
5. Without orchestrate, you run the same skills one at a time, and to continue
   in another chat you paste the handoff block there and ask what-to-ask for the
   follow-up prompt.

## What still needs you

- Only `delegate-code` sends a write task, to Codex, Claude or Antigravity, in a
  separate worktree that you merge yourself. Cursor can be the orchestrator
  but is never a worker. No skill types into another chat. `second-opinion` runs its
  advisor read-only, and `codex-image` only produces images.
- Each `second-opinion` call starts a new CLI process. For Claude, a follow-up can continue
  the same conversation with `--resume <session_id>` from the earlier result;
  Codex and Antigravity calls always start fresh.
- No skill reads another agent's chat history. Results travel only as text you
  paste, a file the runner writes, or the CLI output returned to the caller.
- The [Your Turn inbox](https://github.com/martin-gomola/your-turn) shows which
  chats wait on you. These skills do not use it to pass messages between agents.
- Approving consequential steps, choosing what to delegate, and judging the
  advisor's claims stay with you and the calling agent.
