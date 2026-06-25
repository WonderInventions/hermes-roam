# Install

`hermes-roam` is a Hermes Agent platform plugin. The repo root **is** the
plugin (it ships `plugin.yaml` + `__init__.py` at the top level), so Hermes can
install it straight from its Git URL — the recommended path. Hermes discovers
plugins from `~/.hermes/plugins/<name>/`; every method below lands the plugin in
`~/.hermes/plugins/roam/` and then enables it.

## Recommended: install from Git (console or CLI)

### Web dashboard

In the Hermes dashboard's **Plugins** page, find **Install from GitHub / Git
URL**, enter:

```
WonderInventions/hermes-roam
```

(or the full `https://github.com/WonderInventions/hermes-roam.git`), install,
then **Rescan** so the sidebar picks up the new manifest, and enable it. The
required configuration (API key, webhook secret) can be set from the dashboard.

### CLI

```sh
hermes plugins install WonderInventions/hermes-roam
```

This clones the repo into `~/.hermes/plugins/roam/`, **prompts you for the
required env vars** (`ROAM_API_KEY`, `ROAM_WEBHOOK_SECRET`) and saves them to
`~/.hermes/.env`, then offers to enable the plugin. Add `--enable` to skip the
prompt in scripts. To update later: `hermes plugins update roam`.

## Alternatives

These produce the same `~/.hermes/plugins/roam/` layout.

**Manual git clone:**

```sh
git clone https://github.com/WonderInventions/hermes-roam ~/.hermes/plugins/roam \
  && hermes plugins enable roam
```

**Release tarball one-liner** (no interactive prompt; good for scripted
installs):

```sh
mkdir -p ~/.hermes/plugins && \
  curl -sL https://github.com/WonderInventions/hermes-roam/releases/download/v0.0.8/hermes-roam-plugin-v0.0.8.tar.gz \
  | tar -xz -C ~/.hermes/plugins && \
  hermes plugins enable roam
```

Replace `v0.0.8` with whichever release tag you want from
<https://github.com/WonderInventions/hermes-roam/releases>.

**Verify the tarball's provenance (optional but recommended).** Every release
tarball is published from CI with signed [build provenance][prov]. Confirm it
was built from this repo by the release workflow — not tampered with — before
installing:

```sh
gh attestation verify hermes-roam-plugin-v0.0.8.tar.gz \
  --repo WonderInventions/hermes-roam
```

A `✓ Verification succeeded!` line means the artifact's SLSA provenance checks
out against GitHub's transparency log.

[prov]: https://docs.github.com/actions/security-guides/using-artifact-attestations-to-establish-provenance-for-builds

## Configure

The two required vars are prompted by `hermes plugins install`. You can also set
them (and the optional vars) from the dashboard, or directly in `~/.hermes/.env`
/ your shell:

```sh
export ROAM_API_KEY="..."                 # from Roam Administration > Developer
export ROAM_WEBHOOK_SECRET="whsec_..."    # the signing secret Roam shows you
```

Optional vars (see `plugin.yaml` for the full list with descriptions) include
`ROAM_WEBHOOK_PUBLIC_URL` (the public HTTPS URL Roam should call — required if
you want the plugin to auto-subscribe webhooks), `ROAM_ALLOWED_USERS`,
`ROAM_ALLOWED_GROUPS`, `ROAM_HOME_CHANNEL`, and `ROAM_REQUIRE_MENTION`.

## Verify

```sh
hermes plugins list | grep roam
```

You should see `roam | enabled | 0.0.8 | Roam ... | user`. If it says
`not enabled`, run `hermes plugins enable roam`.

Then start the gateway: `hermes gateway` (or `hermes gateway restart`). The Roam
adapter binds its webhook server (default `0.0.0.0:8647/roam/webhook`) and logs
`roam: webhook listening on …`.

## Upgrading

- Git installs: `hermes plugins update roam` (pulls the latest from the remote).
- Tarball: re-run the one-liner with the new tag — it overwrites
  `~/.hermes/plugins/roam/`.

Then restart the gateway: `hermes gateway restart`.
</content>
