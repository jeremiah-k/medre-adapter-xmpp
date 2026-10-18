# Declared capabilities exposed for planning resolution

- Assign the declared `AdapterCapabilities` to the adapter instance's `_capabilities` attribute so core's planning resolver and the fake capability contract read the same declaration `health_check` reports, instead of falling back to defaults that would suppress fallback threads.
- Cover both the real adapter and the pipeline fake with a regression test mirroring the core planning resolution contract.
