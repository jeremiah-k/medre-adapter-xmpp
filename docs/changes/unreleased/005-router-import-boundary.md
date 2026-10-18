# Router import boundary in configuration preflight

- Type the route preflight parameter through the duck-typed seam contract (`Iterable[Any]`) shared with core's `prepare_adapter_config`, removing the only import of core routing models from adapter source.
- Add an operational boundary test that source-scans every adapter module for the banned core routing import paths, mirroring the other first-party adapters and core's cross-transport containment guard.
