# macOS startup checklist

1. Use Python 3.12–3.13 and Node 22+, with `uv` and `just` installed.
2. Run `just setup`. Default infrastructure is PostgreSQL plus Redis; optional model and storage engines are not required for the simulated yard.
3. Verify `just doctor`. Current Homebrew PostGIS/pgvector packages target PostgreSQL 17/18; the bootstrap selects PostgreSQL 17. Verify extension availability against the server actually listening on the configured port.
4. If an older server is already running on port 5432, use an isolated compatible server and set `SIO_PG_PORT`, `SIO_PG_DATABASE`, and credentials. Never reuse its data directory with another server major version.
5. Run `just dev-lite` and open http://localhost:5173. All consumers retain individual health endpoints. A failed component prevents the supervisor reporting the stack ready.
6. Sign in with an explicitly selected development role. Inspect Administration → System health and Sources.
7. Confirm simulated/scripted/dry-run labels before demonstrating the yard. Test a real source separately before claiming real hardware support.
8. Run `just check`, `npm test --prefix web`, and the live-database acceptance scenario from the root README.

The lite profile reduces process overhead but does not guarantee a particular RAM footprint. ONNX models, browser 3D, and local LLMs add workload-dependent memory. The Cesium twin loads only when opened. Default hash embeddings and the scripted copilot avoid large model downloads and are explicitly labelled.

Stop supervised application processes with `just stop`. This does not remove saved source configuration or database data. Homebrew-managed PostgreSQL data is outside `.sio`; `just clean` is not a complete database reset.
