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
