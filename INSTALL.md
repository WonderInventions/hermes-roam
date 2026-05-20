# Install

`hermes-roam` is distributed as a tagged GitHub Release. `pip` can install
directly from the release tag — no package registry account needed.

## Latest version

```sh
pip install "hermes-roam @ git+https://github.com/WonderInventions/hermes-roam.git@v0.0.1"
```

Replace `v0.0.1` with the tag you want from
<https://github.com/WonderInventions/hermes-roam/releases>.

Each release also attaches a built wheel and sdist for hash-pinned or
offline installs. To install from the attached wheel:

```sh
pip install "https://github.com/WonderInventions/hermes-roam/releases/download/v0.0.1/hermes_roam-0.0.1-py3-none-any.whl"
```

## Configure

After installing, set the required env vars and (re)start Hermes:

```sh
export ROAM_API_KEY="..."
export ROAM_WEBHOOK_SECRET="whsec_..."
```

The plugin auto-registers via the `hermes_agent.plugins` entry point —
no manual edits to `config.yaml` needed. See `roam/plugin.yaml` for the
full list of optional env vars (`ROAM_WEBHOOK_PUBLIC_URL`,
`ROAM_ALLOWED_USERS`, etc.).
