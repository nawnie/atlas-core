# Atlas Core

Atlas Core is the shared memory and reasoning layer for AI Embedded Systems products. It turns records from different business systems into one canonical event model, remembers their provenance, explains what changed, and gates recommended actions behind explicit human approval.

This is the Atlas **runtime and SDK**. It is separate from the AIWF Research
Atlas corpus (knowledge cards, lanes, and source material) and the Atlas Reader
LoRA research project. See [the repository map](docs/REPOSITORY_MAP.md) for the
product boundaries.

```text
Company Memory + Universal Translator
                  |
            Cartographer
       (maps, lineage, continuity)
                  |
          Change Intelligence
                  |
           Approval Engine
                  |
     Hotel / Apartment / Construction /
     Manufacturing / Healthcare adapters
```

This first vertical slice is deliberately local and deterministic. Its cited answer layer does not call an LLM or connect to a live company system. Approved Hotel actions can execute only against the bundled persistent local PMS sandbox. It proves the reusable contract:

1. A domain adapter translates an industry record into a canonical observation.
2. Company Memory stores the observation and its source provenance.
3. Change Intelligence compares it with the previous version and emits typed changes.
4. The adapter recommends a domain action from those changes.
5. The Approval Engine evaluates policy and records a pending, approved, or rejected decision.
6. Every step is queryable through an append-only audit trail.
7. Cartographer records source-to-canonical maps, lineage pointers, decisions,
   verification, and unresolved work as searchable cards and stable lanes.
8. A separately confirmed execution outbox can hand an approved proposal to a
   server-registered provider adapter with one stable idempotency key.
9. The Hotel workspace verifies the resulting provider state and can separately
   roll the sandbox action back.

## Run the hotel demonstration

Python 3.12 is required. The core demonstration uses the standard library; the recovery and signed-backup path also installs `cryptography`.

```powershell
py -3.12 -m pip install -e .
py -3.12 -m atlas_core.cli demo --database runtime\atlas-core.db
```

From a source checkout without installing:

```powershell
$env:PYTHONPATH = "src"
py -3.12 -m atlas_core.cli demo --database runtime\atlas-core.db
```

The demonstration ingests a synthetic reservation, ingests an updated copy with a cancellation and revenue change, explains the changes, and creates a pending human approval request.

## Run the operator console

Double-click `Start Atlas Core.cmd`, or run:

```powershell
py -3.12 scripts\run_operator.py
```

Then open `http://127.0.0.1:8791/`.

The local operator console provides:

- Ask Atlas with claim-level citations to Company Memory and Cartographer;
- tenant-scoped system metrics;
- a dedicated Hotel workspace that exercises a persistent local PMS sandbox
  through translation, Cartographer, approval, exact-confirmation execution,
  provider-side inventory release, receipts, and rollback;
- a synthetic apartment connector import with replayable receipts;
- synthetic construction, manufacturing, and administrative-healthcare imports;
- pending, approved, rejected, and blocked proposal views;
- an import ledger showing imported and deduplicated connector records;
- preview-first JSONL and CSV file intake for all five reference industries,
  with deterministic replay IDs and no raw payload display in the operator;
- guarded unattended local inbox processing with durable imported, replayed,
  rejected, skipped, backup, and retention receipts;
- verified whole-database recovery points with guarded restore and automatic
  safety backup;
- password-encrypted portable `.atlasbackup` export/import using AES-256-GCM
  and scrypt, plus preview-first retention controls;
- an authenticated Atlas Gateway and HTTPS-enforcing official SDK transport for
  approved private deployments;
- explicit approve/reject decisions with reviewer notes;
- an execution outbox showing queued, running, succeeded, failed, uncertain,
  reconciled, and rollback evidence without proposal payloads; local mutation
  controls are hard-limited to the built-in Hotel/PMS sandbox;
- Cartographer source maps, lanes, lineage, validation, and unresolved work;
- the immutable audit timeline.

The server binds only to localhost. It rejects public binding, cross-origin
mutations, missing request tokens, mismatched tenant headers, non-JSON mutation
bodies, and oversized JSON requests. It sends no CORS permission and no external
assets or telemetry.

## Verify

```powershell
$env:PYTHONPATH = "src"
py -3.12 -m unittest discover -s tests -v
```

## Import a local export

JSON Lines uses one envelope per line:

```json
{"record_id":"R-100","version":"1","payload":{"reservation_id":"R-100","status":"confirmed","arrival_date":"2026-08-01","departure_date":"2026-08-03","room_type":"king","updated_at":"2026-07-24T12:00:00Z"}}
```

CSV requires `record_id` and `version` columns; remaining columns become the
adapter payload. Preview is the default:

```powershell
atlas-core import-file hotel-export.jsonl --format jsonl --domain hotel `
  --tenant hotel-1 --connector-id hotel-file-drop `
  --source-system pms-export --database runtime\atlas-core.db
```

Add `--apply` only after reviewing the checksum, record count, field names, and
deterministic batch ID.

## Run guarded local automation

Copy `config\atlas-automation.example.json` to the ignored
`config\atlas-automation.json`, then review every tenant, industry, connector,
source, inbox, backup interval, and retention setting. Start the continuous
local runner with:

```powershell
.\Start Atlas Automation.cmd
```

For a single pass:

```powershell
py -3.12 scripts\run_automation.py --once
```

The runner reads only JSONL, NDJSON, and CSV files directly inside explicitly
configured inboxes. Successful files move to `processed`; invalid files move to
`rejected`. Import and backup receipts are append-only under
`<database>.automation`, and exact file replays remain safe. Automatic backup
retention is disabled in the example; enabling `apply_retention` is an explicit
instruction to retain at least the newest two verified recovery points.

The same config can enable a provider-neutral `local_folder` portable recovery
destination. Atlas exports the latest verified recovery point as an encrypted,
authenticated `.atlasbackup`, writes the artifact and filename-only manifest
with same-directory atomic replacement, reads it back with the runtime
passphrase, and records a payload-free receipt. The passphrase itself is never
accepted in JSON: configure only `passphrase_env`. The Windows launcher prompts
without echo when that runtime variable is absent; unattended supervisors must
inject it through their approved secret mechanism.

Inspect portable copies:

```powershell
atlas-core portable-list --config config\atlas-automation.json `
  --database runtime\operator.db
```

Import is preview-only unless the exact confirmation printed by the preview is
provided. Import authenticates and registers a recovery point; it does not
restore Company Memory:

```powershell
atlas-core portable-import exp_... --config config\atlas-automation.json `
  --database runtime\recovered.db
```

## Core contracts

- `CanonicalObservation`: the universal language shared by every adapter.
- `ChangeSet`: a deterministic, field-level explanation of what changed.
- `ActionProposal`: a domain recommendation, never an execution.
- `ApprovalDecision`: policy result and human decision history.
- `AuditEvent`: immutable evidence of ingestion, change detection, and approval.
- `CartographyCard`: searchable lineage and continuity without copying raw
  source records or secrets.
- `ReadOnlyConnector`: bounded source reader that cannot execute upstream
  mutations.
- `ImportBatchReceipt`: replay, deduplication, rollback, and provenance evidence
  for every connector batch.
- `AutomationRunReceipt`: unattended file, backup, retention, and failure
  evidence without copying record payloads into the automation ledger.
- `LocalFolderRecoveryDestination`: provider-neutral encrypted portable-copy
  contract with authenticated read-back and confirmation-guarded retention.
- `ActionExecutor`: server-registered provider boundary with an action
  allowlist, stable idempotency key, bounded outcome, and optional rollback.
- `ActionExecutionReceipt`: payload-free durable queue, attempt, outcome,
  uncertainty, reconciliation, and rollback evidence.
- `HotelPmsSandbox`: restart-safe, tenant-scoped provider simulator with
  persistent idempotency, observable inventory state, and reversible actions;
  it has no endpoint or credential mode.
- `ProviderManifest` and `ProviderCertificationRunner`: safe capability
  declarations plus local-sandbox conformance checks for execution, idempotent
  replay, rollback, state restoration, and cleanup.
- `atlas-provider`: official SDK developer CLI for atomic adapter scaffolding,
  strict manifest inspection, trusted-local factory assessment, and exclusive
  certification evidence export.
- `DeclarativeIndustryAdapter`: versioned, strict source-to-canonical mappings
  and change-triggered approval rules for new non-sensitive industries without
  custom framework code; every ingestion still produces Cartographer lineage.

## Next build slices

1. Certify the first explicitly authorized vendor sandbox against the provider
   manifest and conformance contract; production adapters remain review-only.
2. Install the local runner under an approved service supervisor and configure
   an operator-approved physical, mounted, or synchronized destination.
3. Add identity-provider integration and production gateway operations.
4. Replace the proven local Hotel/PMS simulator with the first explicitly
   authorized vendor sandbox adapter, including provider-specific
   authentication, certification, idempotency, rate limits, and rollback
   verification.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and
[`docs/BUILD_STATUS.md`](docs/BUILD_STATUS.md) for the current product boundary
and completion evidence.

The product question Atlas Core is designed to answer is:

> What changed, why did it change, and what should I do next?

`AtlasClient.answer(...)` now retrieves that answer from tenant-scoped changes,
source observations, Cartographer cards, and proposals. Each claim carries
stable citations. An unmatched query returns no unsupported answer, and source
provenance is never presented as an unstored root cause.

## Publication boundary

Atlas Core is published under a proprietary, All Rights Reserved license; see
[LICENSE.md](LICENSE.md). This source release is a deterministic local product
foundation, not a live company-system integration, production deployment, or
authorization to handle real records. Generated runtime state, encrypted
recovery artifacts, local configuration, credentials, provider endpoints, and
production adapters are intentionally excluded.

## Official SDK

The official Python SDK is under [`sdk/python`](sdk/python). It provides the
stable application-facing `AtlasClient`, typed requests and responses, an
in-process local transport, approval methods, audit reads, and the supported
industry-adapter extension point.

```powershell
$env:PYTHONPATH = "src;sdk\python\src"
py -3.12 sdk\python\examples\hotel_quickstart.py
```

Build the reproducible Atlas Core, official SDK, private Gateway, and local
Operator wheels without downloading build tools:

```powershell
py -3.12 scripts\build_sdk.py
```
