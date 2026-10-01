# Private documents (gitignored)

Put your own files in a collection folder, for example `data/private/demo/`, together with an `_access.yaml` (copy
`_access.example.yaml`). Nothing in this folder except this README and the example file is committed. Files without a
valid `_access.yaml` are quarantined, so they are denied by default. Then run `make ingest` and sign in as a synthetic user who belongs
to one of the `allowed_groups`.
