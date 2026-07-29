# Atlas Core Gateway

Atlas Gateway is the authenticated private-deployment surface for the official
SDK. It is separate from the browser operator and has no bootstrap-token,
cookie, CORS, or public-by-default mode.

## Security model

- Service tokens are supplied through the process environment and retained by
  the gateway only as SHA-256 digests.
- Every grant has explicit tenant IDs and permissions: `read`, `ingest`,
  `approve`, `execute`, `reconcile`, or global `backup` and `providers`.
- Backup permission requires the wildcard tenant grant because recovery points
  cover the entire Company Memory database.
- Provider readiness requires the wildcard tenant grant. `GET
  /v1/admin/providers` returns payload-free static readiness only; remote
  callers cannot trigger dynamic conformance.
- Non-loopback binding requires both `--allow-remote` and a TLS certificate plus
  private key.
- The server requires TLS 1.2 or newer when it terminates TLS.
- The official SDK requires HTTPS. Plain HTTP is possible only for an explicitly
  enabled loopback development connection.
- Redirects are rejected so bearer credentials cannot be forwarded or
  downgraded.
- Request bodies and response bodies are bounded, and each service identity is
  rate limited.

Backup-authorized clients can create, restore, export, import, and prune
recovery points and read payload-free automation health. Automation status
redacts server configuration and state paths. Encrypted artifacts use
`application/vnd.atlas.encrypted-backup`; passphrases travel only inside
TLS-protected request bodies and are never persisted or logged by Atlas.
Portable-destination status includes verification receipts but not destination
paths or passphrases. Selecting, importing from, or pruning a server-side
portable destination is intentionally unavailable through Gateway.

Execution adapters are registered when the Gateway process is created; clients
cannot supply their endpoint or credentials. `execute` permits queue, run, and
rollback for granted tenants. The separate `reconcile` permission records a
definite outcome after an ambiguous provider failure. Gateway ignores
client-asserted execution actor names and records the authenticated grant name.
Read permission can inspect payload-free execution receipts.

## Local private smoke

Generate a token without writing it to disk:

```powershell
$env:ATLAS_GATEWAY_TOKEN = py -3.12 scripts\generate_gateway_token.py
$env:ATLAS_GATEWAY_TENANTS = "hotel-a"
$env:ATLAS_GATEWAY_PERMISSIONS = "read,ingest,approve"
py -3.12 scripts\run_gateway.py
```

This binds only to `127.0.0.1:8792`. Local HTTP exists for development and must
be explicitly enabled by SDK callers with `allow_insecure_loopback=True`.

## Private-network TLS

Supply a certificate and key issued for the actual private hostname:

```powershell
py -3.12 scripts\run_gateway.py `
  --host 10.0.0.25 `
  --port 8792 `
  --allow-remote `
  --tls-certificate C:\secure\atlas.crt `
  --tls-private-key C:\secure\atlas.key
```

Do not expose Atlas Gateway directly to the public internet. Certificate
issuance, firewall rules, reverse proxying, DNS, service supervision, secret
storage, and token rotation remain deployment-owner gates and are not changed by
this repository.
Read-authorized clients access cited answers through
`AtlasClient.answer(...)` and
`GET /v1/tenants/{tenant_id}/answer?query=...&limit=5`.
