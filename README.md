# hermes-roam

[Hermes Agent](https://github.com/NousResearch/hermes-agent) platform
adapter plugin for [Roam](https://ro.am). Receives Roam `chat.message`
webhooks (Standard-Webhooks signature-verified), normalizes them into
Hermes `MessageEvent`s, and delivers outbound replies via the Roam V1
API (`chat.post`, `chat.update`, `chat.typing`). Roam threads are
mapped to Hermes session threads via `threadTimestamp`.

## Install

See [INSTALL.md](INSTALL.md).

## Development

```sh
python -m venv .venv
.venv/bin/pip install aiohttp pytest pytest-asyncio
.venv/bin/python -m pytest
```

Tests stub the `gateway.*` modules in `tests/conftest.py`, so the
plugin can be exercised without a running Hermes installation.

## Release process

Releases are tagged commits on `master`. There is no PyPI / NPM
registry — users install straight from the Git repo via
`hermes plugins install` (see [INSTALL.md](INSTALL.md)); the GitHub
Release page also carries a plugin tarball for scripted installs.

`master` is branch-protected: direct pushes are rejected, every change
must land through a pull request, and the three `pytest` checks (Python
3.10 / 3.11 / 3.12) are required. The version bump therefore goes
through a PR like any other change.

To cut a release:

1. On a branch, bump the version in both machine-read files to the same
   string:
   - `pyproject.toml` → `project.version`
   - `plugin.yaml` → `version`

   (The `v…` tags in INSTALL.md's install one-liners are illustrative
   examples — "replace with whichever release tag you want" — so they
   are *not* bumped each release.)
2. Open a PR, let the required checks pass, and merge it. The repo is
   set to **squash** merges (rebase and merge-commit are disabled),
   which keeps the linear `… (#N)` history.
3. Tag the resulting `master` commit and push the tag:
   ```sh
   git switch master && git pull
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```
4. The `Release` GitHub Actions workflow fires on `v*` tag push and:
   - checks that the tag matches `pyproject.toml`'s `version`,
   - runs `pytest`,
   - builds `hermes-roam-plugin-v<version>.tar.gz` (the plugin
     tarball that the INSTALL.md one-liner extracts into
     `~/.hermes/plugins/`),
   - attaches signed SLSA build provenance to the tarball,
   - creates a GitHub Release with the tarball attached and
     auto-generated notes.

If the tag/version check fails the workflow aborts before publishing,
so a mismatched bump can't produce a misnamed release.

## CI

`.github/workflows/ci.yml` runs `pytest` on every push and pull
request across Python 3.10 / 3.11 / 3.12.
