<div align="center">

# Your Turn Loop

<p align="center"><strong>A handoff protocol for working across AI coding agents.</strong></p>

![Portable skills · SKILL.md](https://img.shields.io/badge/Portable%20skills-SKILL.md-56645f)
![No MCP required](https://img.shields.io/badge/No%20MCP-required-2f7a6b)

[Get started](#get-started) · [Cross-agent loop](#cross-agent-loop) · [Help](#help)

</div>

The companion skills for [Your Turn](https://github.com/martin-gomola/your-turn),
for developers who work with more than one AI coding agent. Write a brief
another agent can act on, delegate it, and get back a result that ends with the
same two lines in every agent:

```text
**Outcome:** The CSV export includes only the filtered rows; relevant tests passed.

**Next step:** none
```

That [your-turn-handoff](docs/handoff.md) format is the protocol. Paste it into
any agent and the next brief starts from a settled result, not a retold chat.
The [protocol spec](https://martin-gomola.github.io/your-turn-loop/spec.html)
defines each field for agents and for tools that read the block.

## Why

- **The agents you already pay for.** The skills drive the agent CLIs you have
  signed in (`claude`, `codex`, `agy`), so work runs on those logins, such as a
  Claude or ChatGPT plan. No API keys to set up or per-call billing to plan.
- **One orchestrator, many agents.** The agent you talk to is the brain: it
  picks the skills a task needs and hands work to the others, spreading it
  across the quotas you have.
- **Your own tools stay in.** Your other skills and MCP servers remain available
  to every agent. These skills only move work and results between them.

For how this compares with agent frameworks and terminal wrappers, see
[Why Your Turn](https://github.com/martin-gomola/your-turn#why-your-turn-instead-of-another-agent-framework).

## Cross-agent loop

| Step | Who acts | Skill | Use it when |
| --- | --- | --- | --- |
| Plan | You → orchestrator | [orchestrate](skills/orchestrate/SKILL.md) | Give one goal and name a worker and reviewer, or let it propose them; it runs the steps below without you pasting between agents and stops for merges and decisions. |
| Brief | You → orchestrator | [what-to-ask](skills/what-to-ask/SKILL.md) | Turn notes, a screenshot, or another agent's result into a self-contained prompt. |
| Delegate | Orchestrator → reviewer | [second-opinion](skills/second-opinion/SKILL.md) | Get a read-only second opinion through an installed Claude, Codex or Antigravity CLI. |
| Delegate | Orchestrator → builder | [delegate-code](skills/delegate-code/SKILL.md) | Have Codex, Claude or Antigravity implement a scoped task in its own worktree while your agent reviews the diff. |
| Delegate | Orchestrator → builder | [codex-image](skills/codex-image/SKILL.md) | Let any agent without an image tool create icons, illustrations or previews through a local Codex CLI. |
| Result | Every agent | [your-turn-handoff](docs/handoff.md) | End every answer with a clear Outcome and Next step, plus Recap and Decision in longer chats. |
| Your turn | You | [Your Turn inbox](https://github.com/martin-gomola/your-turn) (optional) | See which chats need you, then carry a result back as the next brief. |

[Work across agents](docs/across-agents.md) walks through the loop and lists the
handoffs that still need you to paste text.

## Get started

[Open the skill installer](https://martin-gomola.github.io/your-turn-loop/) to
choose skills, coding agents, and whether to install for this project or all your
projects. Copy one command and run it in your terminal. Installation uses the
[Skills CLI](https://github.com/vercel-labs/skills) and requires Node.js 22.20+
and Git. Codex, Claude Code, Cursor, Antigravity, and Gemini CLI are supported.
Generated commands disable Skills CLI telemetry for that run.

Install only the behavior you need. The standalone
[your-turn-handoff](docs/handoff.md) skill works with or without the separate
[Your Turn inbox](https://github.com/martin-gomola/your-turn).

## Help

- [Update or remove your installation](docs/install.md)
- [Skill missing, duplicate, or unable to install?](docs/install.md#troubleshooting)
- [Development and validation](docs/development.md)
- [Installer page (local preview)](docs/development.md#installer-page)
- [MIT license](LICENSE)
