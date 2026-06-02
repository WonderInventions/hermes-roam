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

To cut a release:

1. Bump the version in three places to the same string:
   - `pyproject.toml` → `project.version`
   - `plugin.yaml` → `version`
   - `INSTALL.md` → the `v…` references in the tarball one-liner
2. Commit the bump on `master` and push.
3. Tag and push:
   ```sh
   git tag v0.0.8
   git push origin v0.0.8
   ```
4. The `Release` GitHub Actions workflow fires on `v*` tag push and:
   - checks that the tag matches `pyproject.toml`'s `version`,
   - runs `pytest`,
   - builds `hermes-roam-plugin-v<version>.tar.gz` (the plugin
     tarball that the INSTALL.md one-liner extracts into
     `~/.hermes/plugins/`),
   - creates a GitHub Release with the tarball attached and
     auto-generated notes.

If the tag/version check fails the workflow aborts before publishing,
so a mismatched bump can't produce a misnamed release.

## CI

`.github/workflows/ci.yml` runs `pytest` on every push and pull
request across Python 3.10 / 3.11 / 3.12.
