Breaking: environment flags read one accepted set (`1`, `true`, `yes`, `on`, `0`, `false`, `no`, `off`); an unknown `DECOY_PACK_REQUIRE_SIGNATURE` raises `EnvFlagError` instead of `ModelPackLoadError`, and the custom provider folder variable `CUSTOM_PROVIDERS_DIR` is now `DECOY_CUSTOM_PROVIDERS_DIR`.

### Changed (one engine flag rule, DUP-FREEZE-E3, 2026-11-14)

Boolean environment flags go through one parser, `decoy_engine.internal.env_flags.env_flag`. It accepts `1`, `true`, `yes`, `on`, `0`, `false`, `no` and `off` in any case, and gives the flag's default for an unset or empty variable. Any other value raises the new public `decoy_engine.errors.EnvFlagError`, code `config/env_flag_invalid`, whose text names the variable and never the value.

`DECOY_ML_DISABLED` used to disable ML only for `1`; it now accepts the full set. An unknown value leaves ML off, and `classifier_status()` reports it down with cause `disabled`. `DECOY_PACK_REQUIRE_SIGNATURE` with an unknown value raises `EnvFlagError` from `ModelPackLoader.load()`; `load_with_fallback()` still returns `None`, and its warning no longer holds the value. The worker marker `DECOY_INSIDE_ISOLATED_WORKER` reads through the same parser. The custom provider folder variable `CUSTOM_PROVIDERS_DIR` is renamed `DECOY_CUSTOM_PROVIDERS_DIR`, with no alias. The new reason is `config/env_flag_invalid`.
