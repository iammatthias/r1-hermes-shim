# patches/ — LEGACY (pre-v0.16.0 Hermes only)

These source patches are **no longer needed on Hermes `v0.16.0` (tag `v2026.6.5`) or newer**,
where the `r1_shim` adapter — the `Platform.R1_SHIM` enum member, the adapter-factory dispatch,
the env-driven config, and the auth bypass — is bundled upstream.

On a current Hermes, do not apply these. Just set `R1_SHIM_ENABLED` / `R1_SHIM_TOKEN` /
`R1_SHIM_PORT` in `~/.hermes/.env` and restart the gateway. See the repo
[README](../README.md#install-on-hermes-v0160-bundled-adapter) and [llms.txt](../llms.txt).

They remain here only for older Hermes builds that predate the upstream merge.
