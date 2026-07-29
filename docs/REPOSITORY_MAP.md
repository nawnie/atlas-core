# Atlas Core repository map

Atlas Core is the product runtime and SDK. It is deliberately separate from
the AIWF Research Atlas corpus and the Atlas Reader LoRA research project.

| Area | Path | Purpose |
| --- | --- | --- |
| Core runtime | `src/atlas_core/` | Canonical observations, memory, change intelligence, approvals, audit, Cartographer, recovery, and execution contracts. |
| Official Python SDK | `sdk/python/` | Stable `AtlasClient`, typed contracts, transports, examples, and provider developer CLI. |
| Local operator | `apps/operator/` | Localhost-only operator console using the official SDK. |
| Private Gateway | `apps/gateway/` | Explicitly configured private SDK transport boundary; no public deployment configuration is included. |
| Reference adapters | `src/atlas_core/adapters/` | Synthetic hotel, apartment, construction, manufacturing, and administrative-healthcare translation examples. |
| Synthetic provider | `src/atlas_core/providers/hotel_sandbox.py` | Local-only PMS simulator used for controlled execution and rollback validation. |
| Contracts and security evidence | `docs/` | Architecture, build status, and scoped safety assessments. |
| Tests | `tests/` | Deterministic unit and integration coverage for the local product boundary. |

## Related repositories

- **AIWF Research Atlas / `ai-without-fear`** is a structured research and
  retrieval corpus: cards, lanes, provenance, and evaluation materials. Atlas
  Core may consume it as a read-only knowledge source, but it is not the
  runtime, database, or SDK.
- **Atlas Reader LoRA / `atlas-lora-adapter`** is separate adapter-training and
  evaluation research. It is neither the Atlas Core SDK nor a live provider
  integration.

## Intentionally absent

This repository does not include production adapters, credentials, real
company records, private datasets, model weights, runtime databases, encrypted
backup artifacts, generated wheels, or deployment infrastructure.
