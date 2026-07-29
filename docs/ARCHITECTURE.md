# Atlas Core Architecture

## Product boundary

Atlas Core is one shared business intelligence engine with domain adapters.
Industry products provide translation and recommendation rules; they do not
rebuild memory, Cartographer, change detection, approval policy, or auditing.

```text
Industry source
      |
ReadOnlyConnector + ImportBatchReceipt
      |
IndustryAdapter.mapping_manifest + translate
      |
CanonicalObservation
      |
CompanyMemory -------- Cartographer
      |                    |
ChangeSet             maps / lineage / lanes
      |
ActionProposal
      |
ApprovalPolicy
      |
Human decision
      |
Controlled execution outbox
      |
Server-registered ActionExecutor
```

## Layers

### Core

`src/atlas_core` owns canonical observations, storage, deterministic comparison,
approval policy, Cartographer, and adapter contracts.

### Cited intelligence retrieval

`IntelligenceRetriever` deterministically ranks tenant-scoped changes together
with their source observations, Cartographer lineage cards, and recorded action
proposals. It returns three structured sections and stable citations. A query
with no matching evidence returns an explicit empty result; Atlas does not
invoke a generative model or infer an unstored root cause.

### Official SDK

`sdk/python/src/atlas_core_sdk` is the supported application boundary.
Applications use `AtlasClient`; transports hide storage and deployment details.

### Operator application

`apps/operator` is a localhost-only HTTP service and browser dashboard. It uses
the SDK and never queries SQLite directly.

### Private gateway

`apps/gateway` is the machine-to-machine deployment surface. It has no browser
bootstrap token and does not share the operator's trust model. Service tokens
arrive through the environment and are retained by the gateway as SHA-256
digests. Each grant declares server-enforced tenant IDs and `read`, `ingest`,
`approve`, or global `backup` permissions.

The official `HttpAtlasTransport` requires HTTPS and rejects redirects. Plain
HTTP can be enabled only for an explicit loopback development connection.
Atlas Gateway refuses non-loopback binding unless remote operation is
explicitly enabled and a TLS certificate plus private key are supplied. Its TLS
terminator requires TLS 1.2 or newer. Request and response sizes are bounded,
credentials are not placed in URLs or logs, and valid identities have an
in-memory request rate limit.

### Portable recovery boundary

Verified SQLite recovery points can be exported as authenticated
`.atlasbackup` containers. A unique 96-bit nonce and 128-bit salt are generated
per export; scrypt derives a 256-bit key and AES-GCM authenticates the database
and recovery metadata. Import decrypts into a staging file, verifies SHA-256,
SQLite integrity, and schema compatibility, then registers the backup without
restoring it. Passphrases are operation inputs only.

`LocalFolderRecoveryDestination` is the first provider-neutral destination
adapter. It accepts an explicitly configured local or mounted folder, rejects
filesystem roots and link/reparse-point destinations, writes an encrypted
artifact plus a filename-only manifest using exclusive temporary files and
same-directory replacement, and authenticates a read-back before reporting
success. Replays are idempotent. Tampered or incomplete entries remain visible
as invalid rather than being silently removed.

Portable retention is preview-first, preserves invalid entries for inspection,
requires an exact destination-specific confirmation, and keeps at least two
valid copies. The automation configuration stores only an environment-variable
name; unknown fields—including an inline `passphrase`—are rejected. Receipts
contain destination and artifact identifiers, hashes, sizes, cipher/KDF names,
and outcomes, but no passphrase or absolute destination path.

### Provider-neutral file intake

`file_connectors.py` parses bounded UTF-8 JSONL and CSV bytes without acquiring
write access to the source. The SDK computes a deterministic file-hash batch ID
and submits normalized connector records through the existing authenticated,
transactional import contract. The operator separates preview from apply and
returns only field names plus record IDs and versions during preview; raw
payload values are not rendered.

### Guarded local automation

`automation.py` converts the manual file and recovery contracts into a bounded
local runner. Its schema-versioned JSON configuration names every tenant,
domain, connector, source system, and inbox explicitly. Only regular JSONL,
NDJSON, and CSV files directly inside those inboxes are considered. Each file
still passes the normal parser, adapter, transaction, replay, approval, audit,
and Cartographer path.

One exclusive lock per Company Memory database prevents overlapping runners.
Successful and rejected source files move into separate inbox subdirectories;
records are never copied into the automation ledger. The runner appends compact
events to `<database>.automation/receipts.jsonl` and atomically replaces its
latest run status. Scheduled recovery uses the existing verified backup API.
Retention defaults to preview-only and, when explicitly enabled in the config,
cannot retain fewer than two recovery points.

When a portable destination is enabled, every automation pass retries exporting
the newest verified local recovery point until an authenticated copy exists.
This makes a missing runtime secret recoverable on the next poll without
creating redundant database backups. The operator exposes portable-copy health
and the official SDK carries the typed `portable_exported` receipt; the
operator cannot export, prune, import, or restore a portable copy.

The official SDK exposes typed automation status locally and through the
Gateway. Gateway access requires global backup permission and redacts server
configuration and state paths. The operator Automation view is read-only and
cannot start jobs.

### Controlled execution outbox

Approval and execution remain separate state transitions. A human-approved
proposal enters the outbox only after the caller provides
`QUEUE EXECUTION <proposal_id> WITH <executor_id>`. Atlas permits one execution
record per proposal and derives a stable unique idempotency key from the
proposal, tenant, executor, and action.

`ActionExecutor` implementations are registered on the trusted local or Gateway
runtime; callers cannot provide an endpoint, credential, module path, or action
type. Each executor declares an identifier, explicit action allowlist, and
rollback support. Execution requires a second exact confirmation and a
transactional claim. Definite failures may be retried at most three times with
the same idempotency key.

An executor exception is not treated as a definite failure because a provider
may have committed the action before the response was lost. Atlas records
`uncertain` and refuses automatic retry. A separately authorized operator must
verify the provider out of band and use an exact reconciliation confirmation.
Rollback is also a separate, confirmation-guarded operation and has its own
success or failure receipt.

Execution receipts omit the proposal payload and retain only tenant-safe
identifiers, state, attempt count, bounded operator-safe summary, opaque
external reference, and error. Gateway uses distinct `execute` and `reconcile`
permissions and binds actor fields to the authenticated grant rather than a
client assertion. The localhost operator can mutate only the built-in
`hotel-pms-sandbox` executor. It fixes that executor ID server-side, requires
the same exact queue, run, and rollback confirmations, and rejects receipts
assigned to any other executor. Reconciliation remains unavailable there.

### Hotel/PMS sandbox application

`HotelPmsSandbox` is the first complete industry application boundary. It uses
a separate local SQLite database to model synthetic reservation state and never
performs network I/O or accepts credentials. The Hotel workspace:

1. seeds a confirmed and cancelled reservation version;
2. records both through Hotel translation, Company Memory, and Cartographer;
3. requires a normal human approval;
4. queues and runs the fixed sandbox executor through the generic outbox;
5. exposes tenant-scoped provider-side review and inventory state;
6. persists the idempotency key and external reference across restarts; and
7. restores the recorded prior state only after separate rollback confirmation.

This is execution-contract and product-flow evidence. It is not a live PMS
connector, provider certification, or authorization to handle real guest data.

### Provider adapter certification

Every execution adapter can declare a versioned `ProviderManifest`: stable
provider and domain identifiers, environment, action allowlist, rollback
support, network posture, credential strategy, synthetic-only posture, and
reviewed live-action authorization. Manifests never contain secret values,
URLs, paths, or provider payloads.

`ProviderCertificationRunner` first compares the manifest with the registered
runtime contract. Dynamic conformance is deliberately limited to offline,
credential-free, synthetic local sandboxes. It verifies execute, observable
state change, idempotent replay, rollback, state restoration, rollback replay,
and cleanup in an isolated certification tenant. Networked vendor sandboxes
receive static readiness only; production manifests always require a separate
review and cannot be promoted by this runner.

The local operator performs Hotel/PMS sandbox conformance at startup and
exposes payload-free receipts. Gateway exposes static readiness through a
separately authorized global `providers` permission and never starts dynamic
certification remotely.

The official SDK also supplies the local-only `atlas-provider` developer CLI.
It creates a new provider project atomically, refuses existing paths, validates
bounded strict manifests, and can import an explicitly selected trusted local
factory for static assessment or explicitly requested local-sandbox
conformance. Evidence is payload-free and written only to a new JSON file.
Factory loading is intentional local Python code execution; it is not exposed
through Gateway, Operator, discovery, or automatic startup.

### Domain adapters

`HotelAdapter`, `ApartmentAdapter`, `ConstructionAdapter`,
`ManufacturingAdapter`, and `HealthcareAdapter` are the first reference
adapters. They share the `IndustryAdapter` contract and declare their field maps
through `mapping_manifest()`. The healthcare adapter is administrative-only,
requires opaque patient references, and rejects common direct-PHI fields before
storage.

`DeclarativeIndustryAdapter` is the bounded onboarding path for new
non-sensitive domains. A schema-versioned JSON specification defines identity,
timestamp, optional version, top-level source-to-attribute mappings, scalar
types, normalization, and single-field change rules. Unknown record fields,
duplicate JSON keys, ambiguous types, non-finite numbers, naive timestamps,
common direct-sensitive fields, and healthcare/clinical/medical domains are
rejected before storage.

It implements the normal `IndustryAdapter` contract, so records continue
through Company Memory, change intelligence, Cartographer, approval policy, and
the execution separation boundary. Declarative rules create proposals only;
they cannot approve, queue, or execute actions.

### Connector intake

`ReadOnlyConnector` returns versioned records and cannot mutate the upstream
system. `AtlasCore.import_batch()`:

1. bounds record count and encoded payload size;
2. hashes the entire external batch;
3. opens one immediate SQLite transaction;
4. recognizes an exact replay by tenant, connector, and external batch ID;
5. rejects reuse of a batch ID with different content;
6. deduplicates matching record/version/fingerprint combinations across batches;
7. translates new records through the normal adapter and approval path;
8. commits observations, changes, proposals, Cartographer cards, audit events,
   fingerprints, and receipts together;
9. rolls all business writes back when any record fails, then stores a failed
   batch receipt separately.

### Recovery

Company Memory creates whole-database recovery points with SQLite's online
backup API. Each database owns an isolated `<database>.backups` directory, so
two Atlas databases in the same parent folder cannot mix recovery points. Each
backup has a sidecar manifest containing its stable backup ID, size, SHA-256
checksum, schema version, and SQLite integrity result. Manifests never contain
an absolute source-database path.

Restore accepts a backup ID rather than a filesystem path. It verifies the
manifest, size, checksum, schema version, and SQLite integrity before changing
Company Memory. It then creates and verifies a safety backup of the current
state, stages the selected database beside the live database, verifies the
staged copy again, atomically replaces the live file, and appends a restore
audit event. The local SDK and operator require the exact phrase
`RESTORE <backup_id>`.

## Trust boundaries

- Raw source records enter through a registered adapter.
- Canonical observations retain source system, record ID, version, and time.
- Cartographer stores pointers and compact mapping evidence, not duplicate raw
  records, credentials, or hidden reasoning.
- Provider readiness receipts expose safe manifest facts, check outcomes, and a
  manifest hash; endpoint hosts and credential material remain omitted.
- Recommendations are proposals. They are never execution.
- Human decisions are tenant-bound and append to the audit history.
- Approval never invokes an executor. Queueing, running, reconciliation, and
  rollback are separately authorized and auditable operations.
- The local operator may queue, run, and roll back only
  `hotel-pms-sandbox`; every other executor remains Gateway/runtime-only.
- The current operator is for one trusted local Windows user and binds only to
  `127.0.0.1`.
- Local recovery points remain beside Company Memory. Portable copies are
  encrypted, but the repository does not choose, mount, synchronize, or approve
  an actual off-device destination.
- The automation runner is local-only, has no provider credentials, watches
  only configured directories, and never executes a proposed business action.
- Remote or multi-user deployment requires real identity, authorization,
  certificate lifecycle, secrets management, monitoring, and data-retention
  policy before it is safe to enable. The Gateway supplies protocol and
  authorization primitives; it does not configure infrastructure.

## Schema evolution

SQLite schema version 4 adds the action execution outbox and its idempotency,
outcome, reconciliation, and rollback fields. Version 3 added connector batch
receipts, per-record receipts, and the fingerprint ledger. Version 2 added
Cartographer cards to the original memory, proposal, decision, change, and
audit tables. Version 1, 2, and 3 databases migrate additively on open.
Destructive migrations are not implemented.
