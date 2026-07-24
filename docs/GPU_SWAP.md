# GPU and production swap

The PRD promises that the CPU-first local stack becomes a GPU/production stack "with zero API
changes" (§9.3). This page is the mapping, and the honest status of each seam.

Nothing here is aspirational architecture: every swap listed is a `SIO_*` environment variable
selecting a different adapter behind an existing port, and
`tests/unit/test_architecture.py` fails the build if any service reaches around a port to import
an adapter directly.

## The matrix

| Concern | Local (CPU, default) | Production / GPU | Selector | Status |
|---|---|---|---|---|
| Detection, segmentation | `yolo26n*.onnx` via onnxruntime | DeepStream 9.1 (RT-DETR), TensorRT engines | `SIO_DETECTOR=deepstream` | adapter stub, Phase 7 |
| Execution provider | `CPUExecutionProvider` | `CUDAExecutionProvider`, `TensorrtExecutionProvider` | `SIO_ORT_PROVIDERS` | Phase 2 |
| Tracking | in-repo ByteTrack + ONNX ReID | DeepStream MV3DT (multi-camera) | `SIO_TRACKER=deepstream` | adapter stub, Phase 7 |
| Copilot LLM | Ollama, ~1.7 B, pinned tag | Nemotron 3 via vLLM / SGLang / NIM | `SIO_LLM_PROVIDER=openai_compat` + `SIO_OPENAI_BASE_URL` | seam exists, tested against a mock in Phase 7 |
| Forecasting | StatsForecast (AutoETS/ARIMA) | TimesFM, Moirai-2 | `SIO_FORECASTER=timesfm` | adapter stub, Phase 7 |
| Simulation | SimPy / Mesa | Cosmos 3 generative worlds | `SIO_SIMULATOR=cosmos` | Phase 7 |
| Event bus | Redis Streams | Kafka / Redpanda | `SIO_BUS_BACKEND=kafka` | adapter stub, Phase 7 |
| Graph | Neo4j | Memgraph (Bolt-compatible) | `SIO_GRAPH_BACKEND=neo4j` + `NEO4J_URI` | works today: Memgraph speaks Bolt |
| Vectors | pgvector | Qdrant / Milvus | `SIO_VECTOR_BACKEND=qdrant` | adapter stub, Phase 7 |
| Stream CEP | native async consumer | Bytewax → Flink | `SIO_CEP_RUNTIME` | Phase 3 |
| Auth | dev JWT | Keycloak OIDC | `SIO_AUTH_MODE=keycloak` | Phase 5 |
| Authz | embedded evaluator | OPA + OpenFGA | `SIO_POLICY_ENGINE=opa` | Phase 5 |
| Workflows | Temporal dev server | Temporal Cloud / self-hosted cluster | `SIO_TEMPORAL_HOST` | works today |

"Adapter stub" means the class exists behind the port and raises a clear, actionable error
naming the phase that implements it — rather than the port pretending the capability is there.

## Why the ONNX choice makes this cheap

Because the local stack is onnxruntime rather than PyTorch, moving to GPU inference does not
change the dependency tree at all: the same `.onnx` file, the same pre/post-processing, a
different execution provider. That is the difference between a swap and a rewrite, and it is the
single most consequential implementation decision in the project.

For genuinely different serving stacks (DeepStream, Triton) the adapter changes but the
`Detector` port does not, so `perception` is untouched.

## Verification approach (Phase 7)

Each swap gets a parity test: the same fixture input through both adapters, asserting the
contract holds (shape, ranges, ordering, tenant scoping) rather than bit-identical output. Where
the production component cannot run in CI, the adapter is exercised against a mock server
speaking the real protocol — an OpenAI-compatible endpoint for the LLM, a Bolt endpoint for the
graph.
