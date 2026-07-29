# Atlas Core Build Status

Last verified: 2026-07-24

## Current stopping point

Atlas Core is intentionally paused at the completed declarative-adapter SDK
milestone. The core, official SDK, Gateway, Operator, Cartographer integration,
provider tooling, certification flow, and local Hotel/PMS sandbox are working
and the full 113-test suite passes from the installed bundle environment.

The next bounded product slice is Adapter Studio: explicitly register reviewed
declarative adapter specs at Gateway and Operator startup, expose a payload-free
adapter inventory and synthetic validation API, populate import domains from
that inventory, and add the corresponding local Operator workspace. No part of
that slice has been introduced yet, so there is no partial runtime contract or
migration to recover before continuing.

## Working now

- Python 3.12 canonical core and SQLite company memory
- universal observation contract and industry adapter interface
- deterministic field-level change intelligence
- approval policy, proposal storage, human approve/reject decisions
- controlled post-approval execution outbox with one execution per proposal,
  stable provider idempotency keys, transactional claims, bounded attempts,
  explicit uncertain outcomes, privileged reconciliation, and rollback receipts
- server-registered executor identifiers and action allowlists; callers cannot
  provide executor endpoints, credentials, module paths, or arbitrary actions
- persistent local-only Hotel/PMS sandbox with tenant-scoped reservation state,
  restart-safe provider idempotency, observable inventory release, and rollback
- provider capability manifests and deterministic certification with static
  runtime checks plus isolated local-sandbox execute, replay, rollback,
  state-restoration, rollback-replay, and cleanup checks
- payload-free provider readiness in the SDK, global-permission Gateway admin
  route, and local operator Providers workspace
- installed `atlas-provider` developer CLI with atomic non-overwriting project
  scaffolding, strict bounded manifest inspection, explicit trusted-local
  factory assessment, local-sandbox conformance, and exclusive JSON evidence
- strict declarative translation adapters for new non-sensitive domains with
  versioned mapping specs, unknown-field denial, scalar coercion,
  timezone-aware observations, change-triggered approval rules, and normal
  Company Memory plus Cartographer lineage
- `atlas-provider scaffold-adapter`, `inspect-adapter`, and payload-free
  `validate-record` developer commands
- Cartographer mappings, source pointers, stable lanes, decisions, validation,
  unresolved work, confidence, and freshness warning
- official Python SDK 0.1.0 with local transport
- deterministic tenant-scoped Ask Atlas retrieval with claim-level citations
- verified whole-database backup receipts with SHA-256, schema, size, and
  SQLite integrity evidence
- guarded restore by stable backup ID with an automatic verified safety backup
- AES-256-GCM/scrypt portable encrypted backup export/import with authenticated
  metadata, wrong-passphrase and tamper rejection, and no stored passphrases
- preview-first backup retention with exact confirmation before pruning
- official HTTPS remote transport with redirect rejection and bounded responses
- private Atlas Gateway with hashed bearer-token grants, server-enforced tenant
  and function permissions, bounded bodies, and per-identity rate limiting
- separate Gateway `execute` and `reconcile` permissions with execution actors
  bound to authenticated grant identities instead of client assertions
- non-loopback gateway refusal unless explicit remote enablement and TLS
  certificate/key configuration are both present
- offline-installable four-wheel bundle for core, SDK, gateway, and operator
- hotel reservation adapter and synthetic full-path fixture
- apartment lease adapter and delinquent-balance approval rule
- construction work-item adapter and safety/budget exception rule
- manufacturing work-order adapter and quality exception rule
- administrative healthcare adapter and authorization/documentation rule
- direct-PHI field rejection at the healthcare adapter boundary
- read-only connector contract and bounded snapshot connector
- bounded UTF-8 JSONL and CSV file connectors with preview-first CLI, SDK,
  remote SDK, and operator workflows
- deterministic file-hash batch IDs, CSV scalar coercion, record-only previews,
  and replay-safe application
- schema-versioned unattended local inbox runner with exclusive locking,
  processed/rejected archives, append-only receipts, and replay recovery
- interval-based verified recovery points with preview-only retention by
  default and explicit configuration required for automatic pruning
- provider-neutral encrypted local-folder recovery destinations with atomic
  writes, authenticated read-back, replay safety, payload-free manifests,
  preview-first retention, and exact confirmation before deletion
- runtime-only portable recovery passphrases, strict unknown-field rejection,
  and a no-echo attended Windows launcher prompt
- typed local and remote SDK automation status, backup-admin Gateway
  authorization, `portable_exported` evidence, server-path redaction, and a
  read-only operator health view
- transactional, replayable import batches with per-record receipts
- exact batch replay, batch-ID content conflict protection, and cross-batch
  fingerprint deduplication
- atomic rollback with a retained failed-batch receipt
- tenant-scoped proposal, overview, Cartographer, and audit reads
- localhost operator HTTP API
- responsive operator dashboard for pulse, Hotel operations, Ask Atlas,
  approvals, Cartographer, imports, backups, automation health, execution
  receipts, and audit
- exact-confirmation operator controls hard-limited to queueing, running, and
  rolling back the built-in Hotel/PMS sandbox
- same-origin/token mutation protection and tenant-bound decisions
- Windows launcher

## Verified

- Python compile checks
- 113 core, retrieval, SDK, gateway, connector, file-intake, automation, industry, declarative-adapter, recovery, execution, provider-certification, provider-CLI, concurrency, security,
  migration, and HTTP
  tests
- JavaScript syntax check
- real localhost service and browser interaction
- apartment import produced two lease observations, two change sets, one
  delinquency proposal, two Cartographer cards, one completed import receipt,
  and nine audit events
- exact apartment-batch replay left observations, changes, proposals, maps, and
  import-batch count unchanged while recording the replay audit event
- JSONL and CSV tests proved UTF-8/NUL/size/header/shape bounds, deterministic
  replay, typed adapter compatibility, healthcare PHI rollback, preview-only
  CLI behavior, remote SDK transport, and operator payload redaction
- browser approval changed pending from one to zero, approved from zero to one,
  and added the decision audit event without executing an external action
- import ledger rendered the batch plus two per-record receipts
- Cartographer explorer rendered both apartment-operation map cards
- one local browser session imported construction, manufacturing, and healthcare
  into the same tenant and showed six observations, six changes, six
  Cartographer cards, three import receipts, and three pending proposals
- the browser rendered all three industry receipt cards and all six
  source-to-decision maps without horizontal overflow
- the administrative-healthcare proposal passed through the human approval gate
  and recorded a decision without external execution
- browser created and rendered a schema-4 recovery point with SHA-256 and
  SQLite integrity evidence and no horizontal overflow
- recovery tests proved checksum-tamper rejection, exact confirmation,
  automatic safety backup, restored state, path-like identifier rejection, and
  isolation between databases sharing one parent directory
- installed four-wheel smoke started Atlas Gateway and Atlas Operator from the installed packages,
  authenticated the installed SDK, returned cited Cartographer evidence,
  round-tripped an encrypted recovery artifact, and applied retention
- live browser created a recovery point, downloaded its encrypted artifact,
  verified its SHA-256 before saving, cleared the passphrase field, and showed
  no desktop horizontal overflow
- gateway tests proved unauthenticated and duplicate-credential rejection,
  cross-tenant denial, read-versus-write separation, backup-admin separation,
  body bounds, rate limiting, remote connector replay, and non-loopback TLS
  enforcement
- desktop and mobile responsive layout checks showed no horizontal overflow
- browser console contained no errors
- live browser file intake previewed a two-version JSONL hotel export without
  exposing payload values, imported both records, raised one pending approval,
  and rendered both source-linked Cartographer cards
- the file-intake browser path rendered at 1280 pixels with matching body and
  main content widths, proving no horizontal overflow
- automation tests proved valid-file processing, invalid-file isolation,
  deterministic replay, non-overlapping runners, interval enforcement, explicit
  automatic retention, configuration validation, and CLI status
- installed four-wheel smoke processed an unattended inbox, created a verified
  backup, wrote and authenticated a portable encrypted copy, and read the typed
  automation receipt remotely with `portable_exported=true` and server paths
  redacted
- installed four-wheel smoke also ingested a changed record, created and
  approved its proposal, queued it through the authenticated SDK/Gateway,
  invoked the persistent Hotel/PMS sandbox with a stable idempotency key,
  verified provider inventory changed to released, and returned a succeeded
  payload-free receipt
- installed remote SDK returned static Hotel/PMS readiness as
  `ready_for_conformance`; remote dynamic conformance was rejected
- local conformance returned `sandbox_certified`, verified idempotent execute
  and rollback replays, restored state, and removed its isolated fixture
- live Providers workspace rendered one registered and certified sandbox, all
  thirteen checks, its safe manifest hash, no credential-like text, no browser
  warnings or errors, and no horizontal overflow at 1265 pixels
- installed `atlas-provider.exe` scaffolded a complete package, its generated
  test passed, strict manifest inspection reported offline/credential-free
  posture, dynamic certification passed all thirteen checks, and the saved
  evidence matched stdout
- overwrite regression tests proved existing project files and certification
  evidence remain unchanged; unknown manifest fields and string booleans are
  rejected
- declarative asset ingestion mapped two source versions into two observations,
  two change sets, two Cartographer cards, and three pending proposals while
  preserving source and version lineage
- declarative negative tests rejected unmapped and common direct-sensitive
  fields, healthcare/clinical/medical domains, duplicate JSON keys, oversized
  specs, non-string identifiers, string booleans, non-finite decimals, and
  timestamps without timezone offsets
- installed SDK smoke scaffolded and inspected an asset adapter, validated a
  record without returning its values, ingested two versions, and returned two
  Cartographer cards plus one pending status-review proposal
- rebuilt wheel SHA-256 values: core
  `beecbae32093084558afe688331babe8962ed51d56df1ca6b12c545f49d6d848`,
  SDK `ff15fe3a398d1fcb77c5f33a0b0cd53898b49ba975275ab39dd547da78b28437`,
  Gateway `559db49e0f024a9ddec6f795fe780bfb7047095af1afd2817afeee22d13ae74`,
  Operator `7d8fbb7a08732de9f2475ce7f19f6ee73cf11d63759504c85e0298ea631230c0`
- the live Automation view showed a ready configuration, idle released lock,
  completed run, and a verified portable-copy receipt with no destination path,
  secret name, passphrase, browser errors, or horizontal overflow
- portable destination tests proved atomic store/read/replay, checksum-tamper
  visibility, manifest minimization, missing-secret retry, guarded retention,
  preview-first confirmed import, and inline-secret configuration rejection
- execution tests proved human-approval and exact-confirmation gates, tenant and
  action allowlists, replay without a second provider call, stable idempotency,
  unavailable-executor failure, payload-free receipts, separately confirmed
  rollback, and no retry before uncertain-outcome reconciliation
- the Execution outbox keeps proposal payloads and database paths out of
  receipts; mutation buttons appear only for receipts owned by the built-in
  Hotel/PMS sandbox
- the live Hotel workspace completed seed, human approval, queue, execution,
  provider-state verification, and rollback through exact typed confirmations;
  inventory changed from held to released and back to held with no console
  errors or desktop horizontal overflow
- the hotel CLI demonstration regression is covered and again returns its
  change explanation and pending approval

## Not yet production-ready

- no live PMS, property-management, construction, manufacturing, or healthcare
  connector is authorized or configured
- no live provider-specific business-action executor or credential; only the
  provider-neutral contract, certification layer, and persistent local
  Hotel/PMS sandbox exist
- no live provider-specific connector authentication or scheduling
- no operating-system service or startup schedule has been installed, and no
  approved physical, mounted, synchronized, or off-device storage destination
  is configured
- no production gateway certificate, identity provider, token rotation service,
  firewall/reverse-proxy configuration, monitoring, or deployment
- no signed release or external package publication
- no generative LLM synthesis, embedding model, or vector index; current cited
  retrieval uses deterministic lexical ranking over stored evidence

These are real remaining product tracks, not hidden behavior in the current
build.
