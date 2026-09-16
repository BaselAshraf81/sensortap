# sensortap

- [Schema reference](schema.md) -- the closed `kind` vocabulary, enums, and record fields, generated directly from `sensortap/schema/*.py` so it cannot drift from the code.
- [Adapter contributor guide](contributing/adapter-guide.md) -- how to write and validate a new platform adapter.
- [Privacy](privacy.md)

## What the test suite does not cover

See the [Schema reference](schema.md#test-suite-coverage) for the explicit list of what the automated Test_Suite -- including the Conformance_Check -- does not verify (physical hardware readings, per-vendor driver behaviour, OS permission prompts, Stream stability beyond 60 seconds, and performance/throughput benchmarking).
