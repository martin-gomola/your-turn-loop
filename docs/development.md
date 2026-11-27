# Development

Edit skills in `skills/<name>/`. Each folder is the complete installable skill,
including its license, resources and evaluation fixtures. There are no generated
skill copies or separate packages to maintain.

Keep each skill's `metadata.version` and its entry in `site/catalog.json` in sync.
After editing a skill or the installer, run `make test`. The handoff is opt-in:
installing another skill must not install or activate it.

## Installer page

Start the static chooser locally:

```bash
make dev
```

Open `http://localhost:8983`. Stop the server with `Ctrl+C`; use
`make dev PORT=8984` if the default port is occupied. The page reads
`site/catalog.json` and generates one Skills CLI command for the selected skills,
agents, and project or global scope. The page has no backend, analytics, cookies
or automatic installation. Generated commands set `DISABLE_TELEMETRY=1` for the
Skills CLI process; no global shell setting is changed.

GitHub Pages uses GitHub Actions as its source. The workflow deploys changes to
`site/`, `pets/`, `Makefile`, or the workflow itself after pushes to `main`, and
supports manual runs. `make site` copies canonical pet assets from `pets/` into
the ignored `site/pets/` directory for preview and deployment. The DeeTee section
uses the Codex pet install deep link with sprite format v2. Only `site/` is uploaded.

## Validate

The installer-page checks use Node.js 18 or newer. There is no frontend build step.

```sh
make test
```

`make test` uses `uv` and the authoring dependency in `requirements-dev.txt`.
Alternatively install that dependency in a virtual environment, then run:

```sh
python3 scripts/validate.py
python3 -m unittest discover -s tests -v
python3 -m pytest skills/second-opinion/tests/test_run_advisor.py -v
node --test tests/installer.test.mjs
```

Validation checks portable metadata, catalog completeness and version consistency,
source checksums, grounded instructions, and common private paths/token patterns.
Installer tests check skill and agent selections, scope, command safety and usage
examples. Trigger and behavior fixtures in each skill's `tests/` directory are
evaluation rubrics; passing repository validation does not establish model quality.
An eval case can add `rubric.trajectory` with `in_order` steps the agent must take
in sequence and `forbidden` steps it must not take, so a grader can catch a correct
result reached the wrong way. Validation checks only the shape of these lists.

### Papercuts to regressions

Capture real skill friction as a short note instead of adding an always-on skill:

```text
Tried: what you asked the agent to do
Expected: what the skill should have made it do
Observed: what actually happened
Fix: the skill change that resolves it
```

Each fixed skill bug adds a regression case to that skill's `tests/eval.yaml`.
Add `rubric.trajectory` when step order matters, not only the final answer.

### Local eval and ablation runs

`scripts/run_evals.py` is an opt-in local tool and is not run in CI. It sends eval
cases to a local `claude`, `codex` or `agy` CLI in read-only mode:

```sh
python3 scripts/run_evals.py --list
python3 scripts/run_evals.py --skill delegate-code --ablation --dry-run
python3 scripts/run_evals.py --skill delegate-code --case scoped_codex_delegation --provider codex --ablation
```

`--ablation` runs each case with the skill text as context and again without it,
so the two outputs can be compared blind. Ablation runs also write
`blind/<case>.A.md` and `blind/<case>.B.md` with only the responses, and
`blind/key.json` records which label is which variant, shuffled per case. Grade the
blind files before opening the key. Results go to a new, gitignored
`tmp/eval-runs/<UTC timestamp>/` directory by default; a non-empty `--output-dir`
is refused rather than overwritten. The runner checks `must_mention` and
`must_not_mention`; expectations and trajectory steps are listed in `summary.md`
for a human or a `second-opinion` judge to grade.

Each case and variant runs in a fresh temporary directory outside the repository,
removed afterwards, so the `without_skill` arm cannot find `skills/` or repository
config. For `with_skill`, the skill folder is copied into
`<workspace>/.claude/skills/<name>` without `tests/` or `__pycache__/`, so
`references/` and `scripts/` resolve but eval rubrics never reach the model; the
prompt still embeds `SKILL.md` in a `<skill>` block for every provider. A symlink
in the skill tree, or a `TMPDIR` inside the repository, stops the run with an
error. The output directory is claimed with an exclusive `summary.json`
placeholder, so two runs cannot share it. Claude runs
with `--output-format stream-json --verbose --strict-mcp-config
--no-session-persistence` and Codex with `--json`. Each result's `metrics` records
`wall_seconds` for every provider and, when the CLI reports them, `turns`,
`cost_usd`, input, cache write, cache read and output tokens, plus the observed
`tools` sequence for trajectory review. `summary.json` adds per-variant `totals`,
and `summary.md` opens with a metrics table comparing the variants.

Client installation and activation are separate from command-generation checks.
Use a disposable project and isolated client configuration for installation tests,
never your real skill directories. No live client installation coverage is claimed
by `make test`.

## Consume a pinned version

Consumers can pin this repository as a Git submodule and read `skills/` alongside
their internal source. Pin a reviewed commit and update deliberately. Detect
duplicate skill names and fail if a required submodule is uninitialized before
replacing installed files. Installed copies are not authoring sources.

## License and provenance

Original instructions and distribution code are [MIT licensed](../LICENSE).
Authoring references and their limitations are recorded in the skill's local
source notes and manifests. Referenced documents retain their own licenses.
