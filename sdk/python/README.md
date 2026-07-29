# Atlas Core Python SDK

This is the official application and adapter SDK for Atlas Core.

## Ask Atlas with citations

```python
answer = client.answer(
    tenant_id="hotel-1",
    query="What changed, why did it change, and what should happen next?",
)
```

`answer.sections` contains the three cited answer sections and
`answer.citations` identifies stored changes, observations, Cartographer
lineage, and proposals. Retrieval is deterministic and tenant-scoped.

## Portable encrypted recovery

```python
backup = client.create_backup()
artifact = client.export_encrypted_backup(
    backup.backup_id,
    passphrase="a long operator-managed passphrase",
)
client.import_encrypted_backup(
    artifact.data,
    passphrase="a long operator-managed passphrase",
)
preview = client.prune_backups(keep_last=5)
```

Artifacts use AES-256-GCM with scrypt-derived keys and authenticated metadata.
Atlas never persists the passphrase. Pruning is dry-run by default and applying
it requires the exact phrase `PRUNE BACKUPS KEEP <count>`.

Version `0.1.x` supports Atlas Core `0.1.x` and Python 3.12. The SDK is local
first. Atlas Core uses the audited `cryptography` package for portable recovery.

## Local JSONL and CSV intake

```python
preview = client.preview_file_import(
    content,
    format="jsonl",
    source_name="hotel-export.jsonl",
)
receipt = client.import_file(
    content,
    format="jsonl",
    source_name="hotel-export.jsonl",
    tenant_id="hotel-1",
    connector_id="hotel-file-drop",
    domain="hotel",
    source_system="pms-export",
)
```

The default external batch ID is derived from the file SHA-256, making exact
replays safe. JSONL preserves nested JSON values. CSV requires `record_id` and
`version`; its remaining columns become payload fields and JSON scalar literals
such as `true`, `42`, and `null` are coerced predictably.

## Automation health

Local clients may associate the approved runner configuration with the SDK:

```python
client = AtlasClient.local(
    database="runtime/company-memory.db",
    adapters=(HotelAdapter(),),
    automation_config="config/atlas-automation.json",
)
status = client.automation_status()
```

`status.last_run` and `status.recent_events` contain typed, payload-free
receipts. Remote clients use the same method. Atlas Gateway requires global
backup permission for this database-wide view and redacts server filesystem
paths from its response. `status.last_run.portable_exported` reports whether
that pass authenticated a provider-neutral portable copy. A
`backup.portable` event carries only safe destination/artifact identifiers and
verification metadata—not its path or passphrase.

Portable destination configuration and import/prune operations deliberately
remain operator/automation contracts rather than remote SDK mutations. This
prevents a backup-authorized remote client from selecting server filesystem
destinations or deleting destination artifacts.

`py -3.12 scripts/build_sdk.py` creates both `atlas_core` and
`atlas_core_sdk` wheels plus the private `atlas_core_gateway` wheel in
`sdk/python/dist`, so the release bundle can be installed without reaching a
package index. Platform dependency wheels are placed in `dist/vendor`.

## Application quick start

```python
from atlas_core.adapters import HotelAdapter
from atlas_core_sdk import AtlasClient, LocalAtlasTransport

client = AtlasClient.local(
    database="runtime/company-memory.db",
    adapters=(HotelAdapter(),),
)

result = client.ingest(
    domain="hotel",
    tenant_id="hotel-1",
    source_system="my-pms",
    record={
        "reservation_id": "R-100",
        "updated_at": "2026-07-24T12:00:00Z",
        "arrival_date": "2026-08-10",
        "departure_date": "2026-08-13",
        "guest_count": 2,
        "room_type": "king",
        "status": "confirmed",
        "total": "729.00",
    },
)
```

## Approval contract

`ingest()` may return action proposals. A proposal is not an executed action.
Call `approve()` or `reject()` only after a human reviews it. The local
transport records that decision; it still does not execute the business
action.

## Controlled execution

Applications register provider adapters on the trusted Atlas runtime. The
official SDK never accepts a caller-supplied endpoint or credential:

```python
client = AtlasClient.local(
    database="runtime/company-memory.db",
    adapters=(HotelAdapter(),),
    executors=(my_registered_executor,),
)

queued = client.queue_execution(
    proposal.id,
    tenant_id=proposal.tenant_id,
    executor_id=my_registered_executor.executor_id,
    requested_by="local-operator",
    confirmation=(
        f"QUEUE EXECUTION {proposal.id} WITH "
        f"{my_registered_executor.executor_id}"
    ),
)
completed = client.run_execution(
    queued.execution_id,
    tenant_id=proposal.tenant_id,
    confirmation=f"EXECUTE {queued.execution_id}",
)
```

An `ActionExecutor` declares an action allowlist and receives a stable
`idempotency_key` on every attempt. It returns a bounded operator-safe summary
and optional opaque external reference—not the provider's raw response.
Exceptions become `uncertain`; Atlas will not retry until a separately
authorized reconciliation records a verified outcome. Rollback, when supported,
requires its own exact confirmation. `client.executions(...)` returns
payload-free receipts.

### Bundled Hotel/PMS sandbox

The SDK exports `HotelPmsSandbox` for complete local product validation:

```python
from atlas_core.adapters import HotelAdapter
from atlas_core_sdk import AtlasClient, HotelPmsSandbox

sandbox = HotelPmsSandbox("runtime/hotel-sandbox.db")
client = AtlasClient.local(
    database="runtime/company-memory.db",
    adapters=(HotelAdapter(),),
    executors=(sandbox,),
)
```

The sandbox persists tenant reservation state and provider idempotency across
process restarts. It can complete and roll back
`review_cancelled_reservation`, but it has no URL, credential, network call, or
live-provider mode. Use it to validate application flows; never put real guest
data in it.

### Adapter certification

Execution adapters declare a `ProviderManifest`. Local callers can inspect
readiness or run deterministic conformance for eligible local sandboxes:

```python
receipts = client.provider_readiness(certify_local_sandboxes=True)
assert receipts[0].status == "sandbox_certified"
```

The run verifies execution, observable provider state, idempotent replay,
rollback, restored state, rollback replay, and fixture cleanup. Dynamic
conformance is unavailable through remote transport. Networked vendor
sandboxes receive static readiness only, while production providers remain
`production_review_required`.

### Provider developer CLI

Installing the official SDK adds `atlas-provider`, a local developer workflow
for creating and certifying adapter projects:

```powershell
atlas-provider scaffold `
  --output .\my-provider `
  --provider-id my-provider-sandbox `
  --display-name "My Provider Sandbox" `
  --domain hotel `
  --action review_cancelled_reservation

Set-Location .\my-provider
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
atlas-provider inspect-manifest .\atlas-provider.json
atlas-provider certify `
  --factory my_provider_sandbox:create_provider `
  --dynamic `
  --evidence .\certification.json
```

The scaffold is a complete offline, credential-free, rollback-capable synthetic
adapter with packaging metadata and a conformance test. It refuses an existing
output path. Evidence files use exclusive creation and are never overwritten.

`certify --factory` imports and executes the developer-selected local Python
module. Use it only with code you have reviewed and trust. Dynamic mode remains
restricted by the core runner to synthetic local sandboxes; a networked or
production adapter cannot be promoted through this command.

### Declarative translation adapters

For a new non-sensitive industry, create a strict source mapping without
writing an `IndustryAdapter` subclass:

```powershell
atlas-provider scaffold-adapter `
  --output .\asset-adapter.json `
  --adapter-id asset-operations-v1 `
  --domain asset `
  --entity-type equipment `
  --id-field asset_id `
  --observed-at-field updated_at `
  --version-field version `
  --field asset_name:asset_name:string:strip:required `
  --field status:status:string:lower:required `
  --field utilization:utilization:decimal:none:required `
  --rule status-review:status:changed_to:review_required:review_asset_status:medium

atlas-provider inspect-adapter .\asset-adapter.json
atlas-provider validate-record `
  --adapter .\asset-adapter.json `
  --record .\synthetic-record.json
```

Then register it through the normal SDK extension point:

```python
from atlas_core_sdk import AtlasClient, DeclarativeIndustryAdapter

adapter = DeclarativeIndustryAdapter.from_file("asset-adapter.json")
client = AtlasClient.local(database="company-memory.db", adapters=(adapter,))
```

Validation never prints record values. Unknown record fields are denied rather
than silently dropped. Declarative rules emit ordinary approval-gated
proposals, while Cartographer records the exact mapping and source lineage.
Healthcare, clinical, medical, and common direct-sensitive fields require a
reviewed coded adapter instead.

## Cartographer

Cartographer is part of every ingestion. It records the adapter mapping,
source/version pointer, canonical observation ID, change summary, validation,
decisions, and unresolved approvals in searchable lanes without copying the raw
source record.

```python
cards = client.search_cartography(
    tenant_id="hotel-1",
    lane="hotel_operations",
    query="cancelled",
)
```

## Replayable imports

Connector batches are read-only and transactional. Reusing the same
`external_batch_id` with identical content returns the original receipt with
`replayed=True`; using it with different content is rejected.

```python
receipt = client.import_batch(
    tenant_id="property-company",
    connector_id="property-feed",
    domain="apartment",
    source_system="property-manager",
    external_batch_id="daily-2026-07-24",
    records=(
        {
            "record_id": "LEASE-310",
            "version": "1",
            "payload": {
                "lease_id": "LEASE-310",
                "updated_at": "2026-07-24T13:00:00Z",
                "unit_id": "B-204",
                "resident_count": 2,
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
                "rent": "1850.00",
                "balance_due": "0.00",
                "status": "active",
            },
        },
    ),
)
```

## Adapter authoring

Implement `IndustryAdapter`, set a unique `domain`, translate source records
into `CanonicalObservation`, and return zero or more `ActionProposal` objects
from a `ChangeSet`.

Applications should import supported symbols from `atlas_core_sdk` and
`atlas_core`, not internal modules. Direct SQLite access is not part of the SDK
contract.

The bundled reference adapters cover hotel reservations, apartment leases,
construction work items, manufacturing work orders, and administrative
healthcare encounters. See `examples/multi_industry_import.py` for registering
all five against one Atlas client.

## Backup and restore

Backups cover the complete Company Memory database and return checksum, size,
schema, and SQLite-integrity evidence.

```python
backup = client.create_backup()
available = client.backups()

restored = client.restore_backup(
    backup.backup_id,
    confirmation=f"RESTORE {backup.backup_id}",
)
print(restored.safety_backup_id)
```

Restore accepts only a locally issued backup ID and automatically creates a
verified safety backup first. The SDK does not accept arbitrary backup paths.

## Private remote transport

The official client can connect to an approved Atlas Gateway:

```python
import os

from atlas_core_sdk import AtlasClient

client = AtlasClient.remote(
    base_url="https://atlas.internal.example",
    api_token=os.environ["ATLAS_GATEWAY_TOKEN"],
    ca_file="private-ca.pem",
)

overview = client.overview(tenant_id="hotel-a")
```

HTTPS is mandatory. A plain-HTTP connection requires both a loopback hostname
and the explicit `allow_insecure_loopback=True` development flag. The transport
rejects redirects and never places the token in a URL. See
`apps/gateway/README.md` for the server-side grant and TLS contract.

## Compatibility policy

- Patch releases fix behavior without intentionally breaking public exports.
- Minor releases may add optional fields and methods.
- Breaking public-contract changes require a new major version.
- Unknown response fields should be ignored by future remote transports.
- The provider-neutral executor contract, safe manifest, certification runner,
  and local Hotel/PMS sandbox are included. Live provider adapters,
  credentials, and vendor actions require separate authorization.
