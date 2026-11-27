# Direction

The person already pays for several coding agents (Claude, Codex, Cursor) and is the queue. The handoff contract is the product. The Your Turn inbox is the queue. These skills add no runtime, no API keys, no per-token billing, and no MCP layer between the person and the CLIs they already run.

- Stay vendor-neutral. A skill that only makes one agent's app nicer is out of scope.
- A skill routes. Correctness the model gets wrong (worktrees, fingerprints, restricted tools, eval isolation) lives in a small script under that skill.
- The runner is a one-shot script that shells out to an installed CLI and exits. It is not a daemon, a scheduler, or an API client.
- Keep the skill short and the script small. A procedure the model mishandles stays a script. A paragraph that leaves that procedure to the model is a bug.
- Ship only data the transcripts and the runner already record. A dollar cost is one of those missing records.
- A claim that this is faster or cheaper ships with a public comparison and an answer key: handoff against a raw chat, or delegate against pasting a diff. The comparison table in your-turn's README is not that comparison.

# Skill sources and distribution

- Edit skills in `skills/<name>/` only.
- Distribute skills directly from `skills/<name>/`; do not add plugin packages or generated skill copies.
- Keep each skill's `metadata.version` and its entry in `site/catalog.json` in sync.
- Run `make test` before finishing.
- Install only selected skills; the handoff must remain opt-in.
