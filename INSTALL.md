# Install

`hermes-roam` is a Hermes Agent platform plugin. Hermes discovers
plugins from `~/.hermes/plugins/<name>/`, so installation is two
steps: drop the plugin into that directory, then opt in via
`hermes plugins enable`.

## One-liner

```sh
mkdir -p ~/.hermes/plugins && \
  curl -sL https://github.com/WonderInventions/hermes-roam/releases/download/v0.0.4/hermes-roam-plugin-v0.0.4.tar.gz \
  | tar -xz -C ~/.hermes/plugins && \
  hermes plugins enable roam
```

This downloads the plugin tarball attached to release `v0.0.4`,
extracts it to `~/.hermes/plugins/roam/`, and enables it. Replace
`v0.0.4` with whichever release tag you want from
<https://github.com/WonderInventions/hermes-roam/releases>.

## Configure

Set the required env vars (typically in `~/.hermes/.env` or your
shell):

```sh
export ROAM_API_KEY="..."          # from Roam Administration > Developer
export ROAM_WEBHOOK_SECRET="whsec_..."   # the signing secret Roam shows you
```

Optional vars (see `roam/plugin.yaml` for the full list) include
`ROAM_WEBHOOK_PUBLIC_URL` (the public HTTPS URL Roam should call —
required if you want the plugin to auto-subscribe webhooks),
`ROAM_ALLOWED_USERS`, `ROAM_ALLOWED_GROUPS`, `ROAM_HOME_CHANNEL`, and
`ROAM_REQUIRE_MENTION`.

## Verify

```sh
hermes plugins list | grep roam
```

You should see `roam | enabled | 0.0.4 | Roam ... | user`. If it says
`not enabled`, re-run `hermes plugins enable roam`.

Then start the gateway: `hermes gateway`. The Roam adapter will bind
its webhook server (default `0.0.0.0:8647/roam/webhook`) and log
`roam: webhook listening on …`.

## Upgrading

Re-run the one-liner with the new tag — the tarball overwrites
`~/.hermes/plugins/roam/`. Then restart `hermes gateway`.

## Alternative: pip install

A wheel and sdist are also attached to each release. They install the
plugin via the `hermes_agent.plugins` entry point. **However**,
Hermes's `plugins enable` CLI only looks at `~/.hermes/plugins/`, so
after pip-installing you'd have to enable the plugin by editing
`~/.hermes/config.yaml` manually:

```yaml
plugins:
  enabled:
    - roam
```

The tarball install above is the recommended path because it
integrates with `hermes plugins enable`.
