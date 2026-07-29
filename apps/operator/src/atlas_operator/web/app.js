"use strict";

const state = {
  token: "",
  tenant: "demo-hotel",
  proposals: [],
  filePreviewSha: "",
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function showNotice(message, error = false) {
  const notice = $("#notice");
  notice.textContent = message;
  notice.classList.toggle("is-error", error);
  notice.hidden = false;
  window.clearTimeout(showNotice.timer);
  showNotice.timer = window.setTimeout(() => { notice.hidden = true; }, 5000);
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      ...(options.body ? {
        "Content-Type": "application/json",
        "X-Atlas-Request-Token": state.token,
        "X-Atlas-Tenant": state.tenant,
      } : {}),
      ...(options.headers || {}),
    },
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error?.message || `Request failed (${response.status})`);
  }
  return payload;
}

function tenantQuery(extra = {}) {
  return new URLSearchParams({ tenant_id: state.tenant, ...extra }).toString();
}

function switchView(name) {
  $$(".view").forEach((view) => view.classList.toggle("is-active", view.id === name));
  $$(".nav-item").forEach((item) => item.classList.toggle("is-active", item.dataset.view === name));
  const titles = {
    pulse: "System pulse",
    hotel: "Hotel operations workspace",
    ask: "Ask Atlas",
    approvals: "Approval engine",
    cartographer: "Cartographer",
    imports: "Import ledger",
    backups: "Recovery points",
    automation: "Automation health",
    executions: "Execution outbox",
    providers: "Provider readiness",
    audit: "Evidence trail",
  };
  $("#viewTitle").textContent = titles[name];
  if (name === "hotel") loadHotelWorkspace();
  if (name === "approvals") loadProposals();
  if (name === "cartographer") loadCartography();
  if (name === "imports") loadImports();
  if (name === "backups") loadBackups();
  if (name === "automation") loadAutomation();
  if (name === "executions") loadExecutions();
  if (name === "providers") loadProviders();
  if (name === "audit") loadAudit();
}

function renderAnswer(answer) {
  const container = $("#answerResult");
  const header = el("article", "answer-header");
  header.append(
    el("p", "eyebrow", `Grounded confidence ${Math.round(answer.confidence * 100)}%`),
    el("h2", "", answer.headline),
    el("small", "", `Generated ${new Date(answer.generated_at).toLocaleString()}`),
  );
  const citationNumbers = new Map(
    answer.citations.map((citation, index) => [citation.citation_id, index + 1]),
  );
  const sections = answer.sections.map((section) => {
    const article = el("article", "answer-section");
    article.append(el("h3", "", section.title));
    const list = el("div", "claim-list");
    section.claims.forEach((claim) => {
      const row = el("div", "claim");
      row.append(el("p", "", claim.text));
      const refs = el("div", "claim-citations");
      claim.citation_ids.forEach((citationId) => {
        refs.append(el("span", "citation-chip", `[${citationNumbers.get(citationId)}]`));
      });
      row.append(refs);
      list.append(row);
    });
    article.append(list);
    return article;
  });
  const evidence = el("article", "evidence-panel");
  evidence.append(el("h3", "", "Evidence"));
  if (answer.citations.length) {
    answer.citations.forEach((citation, index) => {
      const row = el("div", "evidence-row");
      const heading = el("div", "evidence-heading");
      heading.append(
        el("span", "citation-number", `[${index + 1}]`),
        el("strong", "", `${citation.evidence_type} · ${citation.entity_type} ${citation.entity_id}`),
      );
      row.append(
        heading,
        el("code", "", citation.source_pointer),
        el("p", "", citation.excerpt),
      );
      evidence.append(row);
    });
  } else {
    evidence.append(empty("No evidence matched this question."));
  }
  const limitations = el("article", "limitations");
  limitations.append(el("h3", "", "Boundaries"));
  const boundaryList = el("ul");
  answer.limitations.forEach((item) => boundaryList.append(el("li", "", item)));
  limitations.append(boundaryList);
  container.replaceChildren(header, ...sections, evidence, limitations);
}

async function askAtlas() {
  const query = $("#askInput").value.trim();
  if (!query) {
    showNotice("Enter a question for Atlas.", true);
    return;
  }
  const button = $("#askButton");
  button.disabled = true;
  button.textContent = "Retrieving…";
  try {
    const answer = await request(`/api/answer?${tenantQuery({ query, limit: "5" })}`);
    renderAnswer(answer);
  } finally {
    button.disabled = false;
    button.textContent = "Ask Atlas";
  }
}

function renderMetrics(data) {
  const metrics = [
    ["observations", "Memory records"],
    ["changes", "Change sets"],
    ["pending", "Awaiting approval"],
    ["cartography_cards", "Map cards"],
    ["import_batches", "Import batches"],
    ["approved", "Approved"],
    ["rejected", "Rejected"],
    ["audit_events", "Audit events"],
    ["proposals", "Total proposals"],
    ["executions", "Executions"],
    ["execution_attention", "Execution attention"],
  ];
  const grid = $("#metricGrid");
  grid.replaceChildren(...metrics.map(([key, label]) => {
    const card = el("article", "metric");
    card.append(el("strong", "", data[key]), el("span", "", label));
    return card;
  }));
  $("#pendingBadge").textContent = String(data.pending);
}

function proposalCard(proposal, compact = false) {
  const card = el("article", "work-card");
  const copy = el("div");
  copy.append(
    el("p", "eyebrow", `${proposal.domain} · ${proposal.entity_type} ${proposal.entity_id}`),
    el("h3", "", proposal.action_type.replaceAll("_", " ")),
    el("p", "", proposal.reason),
  );
  const meta = el("div", "card-meta");
  meta.append(
    el("span", `pill risk-${proposal.risk}`, `${proposal.risk} risk`),
    el("span", `pill status-${proposal.approval_status}`, proposal.approval_status),
  );
  copy.append(meta);
  card.append(copy);
  if (proposal.approval_status === "pending") {
    const button = el("button", "primary-button", compact ? "Review" : "Open decision");
    button.addEventListener("click", () => openDecision(proposal));
    card.append(button);
  }
  return card;
}

function empty(message) {
  return el("div", "empty-state", message);
}

async function loadOverview() {
  const data = await request(`/api/overview?${tenantQuery()}`);
  renderMetrics(data);
  const pending = await request(`/api/proposals?${tenantQuery({ status: "pending", limit: "3" })}`);
  const target = $("#pulseApprovals");
  target.replaceChildren(...(
    pending.items.length ? pending.items.map((item) => proposalCard(item, true))
      : [empty("No decisions are waiting. Import an industry demonstration to exercise the full flow.")]
  ));
}

async function loadProposals() {
  const status = $("#proposalFilter").value;
  const payload = await request(`/api/proposals?${tenantQuery({ status, limit: "100" })}`);
  state.proposals = payload.items;
  $("#approvalList").replaceChildren(...(
    payload.items.length ? payload.items.map((item) => proposalCard(item))
      : [empty("No proposals match this view.")]
  ));
}

function mapCard(card) {
  const article = el("article", "map-card");
  article.append(
    el("p", "eyebrow", `${card.lane} · confidence ${Math.round(card.confidence * 100)}%`),
    el("h3", "", card.title),
    el("p", "", card.summary),
  );
  const grid = el("div", "map-grid");
  const sources = el("div");
  sources.append(el("strong", "", "Source pointers"));
  const sourceList = el("ul");
  card.source_pointers.forEach((value) => sourceList.append(el("li", "", value)));
  sources.append(sourceList);
  const unresolved = el("div");
  unresolved.append(el("strong", "", "Unresolved"));
  const unresolvedList = el("ul");
  (card.unresolved.length ? card.unresolved : ["Nothing unresolved"]).forEach(
    (value) => unresolvedList.append(el("li", "", value)),
  );
  unresolved.append(unresolvedList);
  grid.append(sources, unresolved);
  article.append(grid);
  return article;
}

async function loadCartography() {
  const query = $("#mapSearch").value.trim();
  const payload = await request(`/api/cartography?${tenantQuery({ query, limit: "100" })}`);
  $("#mapList").replaceChildren(...(
    payload.items.length ? payload.items.map(mapCard)
      : [empty("No Cartographer cards match this search.")]
  ));
}

async function loadAudit() {
  const payload = await request(`/api/audit?${tenantQuery({ limit: "200" })}`);
  $("#auditList").replaceChildren(...(
    payload.items.length ? payload.items.map((event) => {
      const item = el("article", "timeline-item");
      item.append(
        el("strong", "", event.event_type.replaceAll(".", " · ")),
        el("small", "", `${event.subject_id} · ${new Date(event.occurred_at).toLocaleString()}`),
      );
      return item;
    }) : [empty("No audit evidence exists for this company yet.")]
  ));
}

function receiptCard(receipt) {
  const card = el("article", "receipt-card");
  const copy = el("div");
  copy.append(
    el("p", "eyebrow", `${receipt.domain} · ${receipt.connector_id}`),
    el("h3", "", receipt.external_batch_id),
    el("p", "", `${receipt.source_system} · ${receipt.status}${receipt.replayed ? " · replayed" : ""}`),
  );
  const meta = el("div", "card-meta");
  meta.append(
    el("span", `pill status-${receipt.status}`, receipt.status),
    el("span", "pill", receipt.batch_id),
  );
  copy.append(meta);
  const counts = el("div", "receipt-counts");
  [["imported_count", "Imported"], ["duplicate_count", "Duplicates"], ["record_count", "Records"]]
    .forEach(([key, label]) => {
      const item = el("div");
      item.append(el("strong", "", receipt[key]), el("small", "", label));
      counts.append(item);
    });
  const records = el("div", "record-strip");
  receipt.records.forEach((record) => {
    records.append(
      el("span", `pill status-${record.status}`, `${record.record_id} v${record.version} · ${record.status}`),
    );
  });
  card.append(copy, counts, records);
  return card;
}

async function loadImports() {
  const payload = await request(`/api/imports?${tenantQuery({ limit: "100" })}`);
  $("#importList").replaceChildren(...(
    payload.items.length ? payload.items.map(receiptCard)
      : [empty("No connector batches have been imported for this company.")]
  ));
}

async function sha256Hex(bytes) {
  return [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))]
    .map((value) => value.toString(16).padStart(2, "0")).join("");
}

async function fileImportEnvelope() {
  const file = $("#fileImportInput").files[0];
  if (!file) throw new Error("Choose a JSONL or CSV export first.");
  const content = new Uint8Array(await file.arrayBuffer());
  const metadata = {
    format: $("#fileFormat").value,
    source_name: file.name,
    connector_id: $("#fileConnectorId").value.trim(),
    source_system: $("#fileSourceSystem").value.trim(),
    domain: $("#fileDomain").value,
  };
  if (!metadata.connector_id || !metadata.source_system) {
    throw new Error("Connector ID and source system are required.");
  }
  const header = new TextEncoder().encode(JSON.stringify(metadata));
  const envelope = new Uint8Array(4 + header.length + content.length);
  new DataView(envelope.buffer).setUint32(0, header.length);
  envelope.set(header, 4);
  envelope.set(content, 4 + header.length);
  return { envelope, content, metadata };
}

function invalidateFilePreview() {
  state.filePreviewSha = "";
  $("#applyFileImport").disabled = true;
  $("#fileImportPreview").replaceChildren();
}

function renderFilePreview(preview) {
  const panel = $("#fileImportPreview");
  const summary = el("article", "preview-summary");
  summary.append(
    el("strong", "", `${preview.record_count} validated record(s)`),
    el("small", "", `${preview.source_name} · ${preview.size_bytes.toLocaleString()} bytes · ${preview.sha256.slice(0, 16)}…`),
    el("p", "", `Fields: ${preview.fields.join(", ")}`),
  );
  const records = el("div", "record-strip");
  preview.records.forEach((record) => {
    records.append(el("span", "pill", `${record.record_id} · v${record.version}`));
  });
  if (preview.records_truncated) records.append(el("span", "pill", "More records validated"));
  summary.append(records);
  panel.replaceChildren(summary);
}

async function previewFileImport() {
  const button = $("#previewFileImport");
  button.disabled = true;
  button.textContent = "Validating…";
  try {
    const { envelope, content } = await fileImportEnvelope();
    const response = await fetch("/api/file-imports/preview", {
      method: "POST",
      headers: {
        "Content-Type": "application/vnd.atlas.import-file",
        "X-Atlas-Request-Token": state.token,
        "X-Atlas-Tenant": state.tenant,
      },
      body: envelope,
    });
    if (!response.ok) throw new Error(await responseError(response));
    const preview = await response.json();
    const localSha = await sha256Hex(content);
    if (localSha !== preview.sha256) {
      throw new Error("Preview checksum did not match the selected file.");
    }
    state.filePreviewSha = preview.sha256;
    $("#applyFileImport").disabled = false;
    renderFilePreview(preview);
    showNotice("File validated. Review the record IDs, then import the batch.");
  } finally {
    button.disabled = false;
    button.textContent = "Preview file";
  }
}

async function applyFileImport() {
  const button = $("#applyFileImport");
  button.disabled = true;
  button.textContent = "Importing…";
  try {
    const { envelope, content } = await fileImportEnvelope();
    const currentSha = await sha256Hex(content);
    if (!state.filePreviewSha || currentSha !== state.filePreviewSha) {
      throw new Error("The selected file changed after preview. Preview it again.");
    }
    const response = await fetch("/api/file-imports/apply", {
      method: "POST",
      headers: {
        "Content-Type": "application/vnd.atlas.import-file",
        "X-Atlas-Request-Token": state.token,
        "X-Atlas-Tenant": state.tenant,
      },
      body: envelope,
    });
    if (!response.ok) throw new Error(await responseError(response));
    const receipt = await response.json();
    showNotice(`Import ${receipt.batch_id} completed: ${receipt.imported_count} imported, ${receipt.duplicate_count} duplicate.`);
    invalidateFilePreview();
    $("#fileImportInput").value = "";
    await Promise.all([loadImports(), loadOverview()]);
  } finally {
    button.textContent = "Import validated batch";
    if (state.filePreviewSha) button.disabled = false;
  }
}

function backupCard(receipt) {
  const card = el("article", "receipt-card");
  const copy = el("div");
  copy.append(
    el("p", "eyebrow", `Schema ${receipt.schema_version} · ${receipt.integrity_check}`),
    el("h3", "", receipt.backup_id),
    el("p", "", receipt.created_at ? new Date(receipt.created_at).toLocaleString() : receipt.error),
  );
  const meta = el("div", "card-meta");
  meta.append(
    el("span", `pill status-${receipt.status}`, receipt.status),
    el("span", "pill", `${receipt.size_bytes.toLocaleString()} bytes`),
  );
  copy.append(meta);
  const checksum = el("div", "checksum");
  checksum.append(el("strong", "", "SHA-256"), el("code", "", receipt.sha256 || "unavailable"));
  card.append(copy, checksum);
  if (receipt.status === "verified") {
    const actions = el("div", "backup-actions");
    const exportButton = el("button", "quiet-button", "Export encrypted");
    exportButton.addEventListener("click", () => exportBackup(receipt.backup_id)
      .catch((error) => showNotice(error.message, true)));
    const restore = el("button", "danger-button", "Restore");
    restore.addEventListener("click", () => restoreBackup(receipt.backup_id));
    actions.append(exportButton, restore);
    card.append(actions);
  }
  return card;
}

async function loadBackups() {
  const payload = await request("/api/backups");
  $("#backupList").replaceChildren(...(
    payload.items.length ? payload.items.map(backupCard)
      : [empty("No recovery points exist yet.")]
  ));
}

function automationMetric(label, value) {
  const card = el("article", "metric");
  card.append(el("strong", "", value), el("span", "", label));
  return card;
}

async function loadAutomation() {
  const status = await request("/api/automation");
  const last = status.last_run;
  const portable = status.recent_events.find(
    (event) => event.event_type === "backup.portable",
  );
  $("#automationSummary").replaceChildren(
    automationMetric("Configuration", status.configured ? "Ready" : "Not linked"),
    automationMetric("Runner lock", status.locked ? "Running" : "Idle"),
    automationMetric("Last run", last ? last.status.replaceAll("_", " ") : "Never"),
    automationMetric("Files handled", last
      ? last.imported_files + last.replayed_files + last.rejected_files
      : 0),
    automationMetric("Portable copy", portable
      ? portable.status.replaceAll("_", " ")
      : "Not recorded"),
  );
  const events = status.recent_events.map((event) => {
    const item = el("article", "timeline-item");
    const label = event.job_id
      ? `${event.event_type} · ${event.job_id}`
      : event.event_type;
    item.append(
      el("strong", "", `${label} · ${event.status.replaceAll("_", " ")}`),
      el("small", "", event.occurred_at
        ? new Date(event.occurred_at).toLocaleString()
        : "Time unavailable"),
    );
    if (event.source_name) item.append(el("code", "", event.source_name));
    if (event.event_type === "backup.portable" && event.detail?.destination_id) {
      item.append(el(
        "code",
        "",
        `${event.detail.destination_id} · ${event.detail.filename || "no artifact"}`,
      ));
    }
    if (event.error) item.append(el("p", "automation-error", event.error));
    return item;
  });
  $("#automationEvents").replaceChildren(...(
    events.length ? events : [empty("No unattended-operation receipts exist yet.")]
  ));
}

async function loadExecutions() {
  const status = $("#executionFilter").value;
  const payload = await request(`/api/executions?${tenantQuery({ status, limit: "100" })}`);
  const attention = payload.items.filter(
    (item) => ["failed", "uncertain", "rollback_failed"].includes(item.status),
  ).length;
  const completed = payload.items.filter(
    (item) => ["succeeded", "rolled_back"].includes(item.status),
  ).length;
  $("#executionSummary").replaceChildren(
    automationMetric("Receipts", payload.items.length),
    automationMetric("Completed", completed),
    automationMetric("Needs attention", attention),
  );
  const cards = payload.items.map((receipt) => {
    const item = el("article", "timeline-item");
    item.append(
      el("strong", "", `${receipt.action_type.replaceAll("_", " ")} · ${receipt.status.replaceAll("_", " ")}`),
      el("small", "", `${receipt.executor_id} · attempt ${receipt.attempt_count}`),
      el("code", "", receipt.execution_id),
      el("code", "", `idempotency ${receipt.idempotency_key}`),
    );
    if (receipt.result_summary) item.append(el("p", "", receipt.result_summary));
    if (receipt.external_reference) {
      item.append(el("small", "", `Reference ${receipt.external_reference}`));
    }
    if (receipt.rollback_status) {
      item.append(el("span", `pill status-${receipt.rollback_status}`, `rollback ${receipt.rollback_status}`));
    }
    if (receipt.error) item.append(el("p", "automation-error", receipt.error));
    const action = sandboxExecutionButton(receipt);
    if (action) item.append(action);
    return item;
  });
  $("#executionList").replaceChildren(...(
    cards.length ? cards : [empty("No post-approval execution receipts exist for this company.")]
  ));
}

async function loadProviders() {
  const payload = await request("/api/providers");
  const certified = payload.items.filter(
    (item) => item.status === "sandbox_certified",
  ).length;
  const review = payload.items.filter(
    (item) => item.status === "production_review_required",
  ).length;
  const failed = payload.items.filter(
    (item) => item.status === "failed",
  ).length;
  $("#providerSummary").replaceChildren(
    automationMetric("Registered", payload.items.length),
    automationMetric("Sandbox certified", certified),
    automationMetric("Review required", review),
    automationMetric("Failed", failed),
  );
  const cards = payload.items.map((provider) => {
    const card = el("article", "map-card provider-card");
    card.append(
      el("p", "eyebrow", `${provider.domain} · ${provider.environment.replaceAll("_", " ")}`),
      el("h3", "", provider.display_name),
      el("p", "", `${provider.provider_id} is ${provider.status.replaceAll("_", " ")}.`),
    );
    const meta = el("div", "card-meta");
    meta.append(
      el("span", `pill status-${provider.status}`, provider.status.replaceAll("_", " ")),
      el("span", "pill", provider.network_access ? "network declared" : "offline"),
      el("span", "pill", provider.credential_strategy.replaceAll("_", " ")),
      el("span", "pill", provider.supports_rollback ? "rollback" : "no rollback"),
    );
    card.append(meta, el("code", "provider-hash", `manifest ${provider.manifest_sha256}`));
    const checks = el("div", "provider-checks");
    provider.checks.forEach((check) => {
      const row = el("div", `provider-check status-${check.status}`);
      row.append(
        el("strong", "", `${check.status === "passed" ? "✓" : "!"} ${check.name.replaceAll("_", " ")}`),
        el("small", "", check.detail),
      );
      checks.append(row);
    });
    card.append(checks);
    return card;
  });
  $("#providerList").replaceChildren(...(
    cards.length
      ? cards
      : [empty("No execution providers are registered on this Atlas runtime.")]
  ));
}

function openExecutionGate({ route, expected, title, copy }) {
  $("#executionRoute").value = route;
  $("#executionExpected").textContent = expected;
  $("#executionConfirmation").value = "";
  $("#executionTitle").textContent = title;
  $("#executionCopy").textContent = copy;
  $("#executionDialog").showModal();
}

function sandboxExecutionButton(receipt) {
  if (receipt.executor_id !== "hotel-pms-sandbox") return null;
  if (["queued", "failed"].includes(receipt.status)) {
    const button = el("button", "primary-button inline-action", "Run in sandbox");
    button.addEventListener("click", () => openExecutionGate({
      route: `/api/executions/${encodeURIComponent(receipt.execution_id)}/run`,
      expected: `EXECUTE ${receipt.execution_id}`,
      title: "Run approved sandbox action",
      copy: "Atlas will apply the approved cancellation workflow to the local Hotel/PMS sandbox.",
    }));
    return button;
  }
  if (receipt.status === "succeeded") {
    const button = el("button", "quiet-button inline-action", "Rollback sandbox action");
    button.addEventListener("click", () => openExecutionGate({
      route: `/api/executions/${encodeURIComponent(receipt.execution_id)}/rollback`,
      expected: `ROLLBACK EXECUTION ${receipt.execution_id}`,
      title: "Rollback sandbox action",
      copy: "Atlas will restore the prior cancellation-review and inventory state in the local sandbox.",
    }));
    return button;
  }
  return null;
}

function queueSandboxButton(proposal) {
  const button = el("button", "primary-button", "Queue in sandbox");
  const expected = `QUEUE EXECUTION ${proposal.id} WITH hotel-pms-sandbox`;
  button.addEventListener("click", () => openExecutionGate({
    route: `/api/proposals/${encodeURIComponent(proposal.id)}/sandbox-execution`,
    expected,
    title: "Queue approved sandbox action",
    copy: "Queue this human-approved proposal for the built-in Hotel/PMS sandbox.",
  }));
  return button;
}

async function loadHotelWorkspace() {
  const [sandbox, proposalPayload, executionPayload] = await Promise.all([
    request(`/api/hotel-sandbox/reservations?${tenantQuery()}`),
    request(`/api/proposals?${tenantQuery({ limit: "100" })}`),
    request(`/api/executions?${tenantQuery({ limit: "100" })}`),
  ]);
  const hotelProposals = proposalPayload.items.filter(
    (item) => item.domain === "hotel"
      && item.action_type === "review_cancelled_reservation",
  );
  const hotelExecutions = executionPayload.items.filter(
    (item) => item.executor_id === sandbox.executor_id,
  );
  const pending = hotelProposals.filter(
    (item) => item.approval_status === "pending",
  ).length;
  const attention = hotelExecutions.filter(
    (item) => ["failed", "uncertain", "rollback_failed"].includes(item.status),
  ).length;
  $("#hotelSummary").replaceChildren(
    automationMetric("Reservations", sandbox.items.length),
    automationMetric("Pending approval", pending),
    automationMetric("Execution receipts", hotelExecutions.length),
    automationMetric("Needs attention", attention),
  );
  const reservations = sandbox.items.map((reservation) => {
    const card = el("article", "work-card");
    const body = el("div");
    body.append(
      el("p", "eyebrow", "Synthetic PMS reservation"),
      el("h3", "", reservation.reservation_id),
      el("p", "", `Reservation ${reservation.status}; cancellation review ${reservation.cancellation_review_status}; inventory ${reservation.inventory_status}.`),
    );
    const meta = el("div", "card-meta");
    meta.append(
      el("span", "pill", `version ${reservation.version}`),
      el("span", `pill status-${reservation.cancellation_review_status}`, reservation.cancellation_review_status),
      el("span", "pill", reservation.inventory_status),
    );
    body.append(meta);
    card.append(body);
    return card;
  });
  $("#hotelReservations").replaceChildren(...(
    reservations.length
      ? reservations
      : [empty("No sandbox reservation exists. Load the cancellation fixture to start the complete workflow.")]
  ));

  const executionsByProposal = new Map(
    hotelExecutions.map((receipt) => [receipt.proposal_id, receipt]),
  );
  const actions = hotelProposals.map((proposal) => {
    const card = el("article", "work-card");
    const body = el("div");
    const receipt = executionsByProposal.get(proposal.id);
    body.append(
      el("p", "eyebrow", proposal.approval_status),
      el("h3", "", proposal.action_type.replaceAll("_", " ")),
      el("p", "", proposal.reason),
    );
    const meta = el("div", "card-meta");
    meta.append(el("span", `pill status-${proposal.approval_status}`, proposal.approval_status));
    if (receipt) meta.append(el("span", `pill status-${receipt.status}`, receipt.status));
    body.append(meta);
    card.append(body);
    if (proposal.approval_status === "pending") {
      const button = el("button", "quiet-button", "Review approval");
      button.addEventListener("click", () => switchView("approvals"));
      card.append(button);
    } else if (proposal.approval_status === "approved" && !receipt) {
      card.append(queueSandboxButton(proposal));
    } else if (receipt) {
      const button = sandboxExecutionButton(receipt);
      if (button) card.append(button);
    }
    return card;
  });
  $("#hotelActions").replaceChildren(...(
    actions.length
      ? actions
      : [empty("No hotel action is waiting. Load the sandbox cancellation first.")]
  ));
}

async function confirmSandboxExecution() {
  const confirmation = $("#executionConfirmation").value;
  const expected = $("#executionExpected").textContent;
  if (confirmation !== expected) {
    showNotice("The confirmation must match the displayed phrase exactly.", true);
    return;
  }
  const receipt = await request($("#executionRoute").value, {
    method: "POST",
    body: JSON.stringify({
      tenant_id: state.tenant,
      confirmation,
    }),
  });
  $("#executionDialog").close();
  showNotice(`Sandbox execution is ${receipt.status.replaceAll("_", " ")}.`);
  await Promise.all([loadOverview(), loadHotelWorkspace(), loadExecutions()]);
}

async function createBackup() {
  const receipt = await request("/api/backups", {
    method: "POST",
    body: JSON.stringify({ tenant_id: state.tenant }),
  });
  showNotice(`Verified backup ${receipt.backup_id} created.`);
  await loadBackups();
}

function recoveryPassphrase() {
  const input = $("#recoveryPassphrase");
  const passphrase = input.value;
  if (new TextEncoder().encode(passphrase).length < 16) {
    throw new Error("Enter a recovery passphrase of at least 16 UTF-8 bytes.");
  }
  return passphrase;
}

async function responseError(response) {
  try {
    const payload = await response.json();
    return payload.error?.message || `Request failed (${response.status})`;
  } catch {
    return `Request failed (${response.status})`;
  }
}

async function exportBackup(backupId) {
  const passphrase = recoveryPassphrase();
  try {
    const response = await fetch(`/api/backups/${encodeURIComponent(backupId)}/export`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Atlas-Request-Token": state.token,
        "X-Atlas-Tenant": state.tenant,
      },
      body: JSON.stringify({ tenant_id: state.tenant, passphrase }),
    });
    if (!response.ok) throw new Error(await responseError(response));
    const blob = await response.blob();
    const digest = [...new Uint8Array(await crypto.subtle.digest(
      "SHA-256",
      await blob.arrayBuffer(),
    ))].map((value) => value.toString(16).padStart(2, "0")).join("");
    const expected = response.headers.get("X-Atlas-SHA256");
    if (!expected || digest !== expected) {
      throw new Error("Downloaded recovery artifact checksum did not match.");
    }
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename="([^"]+)"/);
    const link = document.createElement("a");
    const url = URL.createObjectURL(blob);
    link.href = url;
    link.download = match?.[1] || `${backupId}.atlasbackup`;
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
    showNotice(`Encrypted backup downloaded and SHA-256 verified: ${digest.slice(0, 12)}…`);
  } finally {
    $("#recoveryPassphrase").value = "";
  }
}

async function importBackup() {
  const file = $("#recoveryFile").files[0];
  if (!file) throw new Error("Choose an .atlasbackup file first.");
  const passphrase = recoveryPassphrase();
  const passphraseBytes = new TextEncoder().encode(passphrase);
  const artifact = new Uint8Array(await file.arrayBuffer());
  const envelope = new Uint8Array(4 + passphraseBytes.length + artifact.length);
  new DataView(envelope.buffer).setUint32(0, passphraseBytes.length);
  envelope.set(passphraseBytes, 4);
  envelope.set(artifact, 4 + passphraseBytes.length);
  try {
    const response = await fetch("/api/backups/import", {
      method: "POST",
      headers: {
        "Content-Type": "application/vnd.atlas.encrypted-backup",
        "X-Atlas-Request-Token": state.token,
        "X-Atlas-Tenant": state.tenant,
      },
      body: envelope,
    });
    if (!response.ok) throw new Error(await responseError(response));
    const receipt = await response.json();
    showNotice(`Imported and verified recovery point ${receipt.backup_id}.`);
    await loadBackups();
  } finally {
    $("#recoveryPassphrase").value = "";
    $("#recoveryFile").value = "";
  }
}

async function previewRetention() {
  const keepLast = Number.parseInt($("#retentionKeep").value, 10);
  const preview = await request("/api/backups/retention", {
    method: "POST",
    body: JSON.stringify({
      tenant_id: state.tenant,
      keep_last: keepLast,
      dry_run: true,
    }),
  });
  if (!preview.candidate_backup_ids.length) {
    showNotice(`Retention preview: nothing to remove; keeping newest ${keepLast}.`);
    return;
  }
  const required = `PRUNE BACKUPS KEEP ${keepLast}`;
  const confirmation = window.prompt(
    `${preview.candidate_backup_ids.length} verified recovery point(s) would be removed, reclaiming ${preview.reclaimed_bytes.toLocaleString()} bytes.\nType exactly:\n${required}`,
    "",
  );
  if (confirmation === null) return;
  const applied = await request("/api/backups/retention", {
    method: "POST",
    body: JSON.stringify({
      tenant_id: state.tenant,
      keep_last: keepLast,
      dry_run: false,
      confirmation,
    }),
  });
  showNotice(`Removed ${applied.deleted_backup_ids.length} old recovery point(s).`);
  await loadBackups();
}

async function restoreBackup(backupId) {
  const required = `RESTORE ${backupId}`;
  const confirmation = window.prompt(
    `Restore replaces current Company Memory after making a safety backup. Type exactly:\n${required}`,
    "",
  );
  if (confirmation === null) return;
  const receipt = await request(`/api/backups/${encodeURIComponent(backupId)}/restore`, {
    method: "POST",
    body: JSON.stringify({
      tenant_id: state.tenant,
      confirmation,
    }),
  });
  showNotice(`Restored ${receipt.restored_backup_id}. Safety backup: ${receipt.safety_backup_id}.`);
  await Promise.all([loadOverview(), loadBackups()]);
}

function openDecision(proposal) {
  $("#proposalId").value = proposal.id;
  $("#decisionTitle").textContent = proposal.action_type.replaceAll("_", " ");
  $("#decisionReason").textContent = proposal.reason;
  $("#decisionNote").value = "";
  $("#decisionDialog").showModal();
}

async function decide(approved) {
  const note = $("#decisionNote").value.trim();
  const reviewer = $("#reviewerInput").value.trim();
  if (!note || !reviewer) {
    showNotice("Reviewer and decision note are required.", true);
    return;
  }
  await request(`/api/proposals/${encodeURIComponent($("#proposalId").value)}/decision`, {
    method: "POST",
    body: JSON.stringify({
      tenant_id: state.tenant,
      approved,
      reason: note,
      decided_by: reviewer,
    }),
  });
  $("#decisionDialog").close();
  showNotice(approved ? "Proposal approved and recorded." : "Proposal rejected and recorded.");
  await Promise.all([loadOverview(), loadProposals()]);
}

async function seedDemo() {
  const payload = await request("/api/demo/seed", {
    method: "POST",
    body: JSON.stringify({ tenant_id: state.tenant }),
  });
  showNotice(payload.message);
  await loadOverview();
  if ($(".view.is-active").id === "hotel") await loadHotelWorkspace();
}

async function seedApartment() {
  const payload = await request("/api/demo/apartment", {
    method: "POST",
    body: JSON.stringify({ tenant_id: state.tenant }),
  });
  showNotice(payload.message);
  await loadOverview();
  const active = $(".view.is-active").id;
  if (active === "imports") await loadImports();
  if (active === "backups") await loadBackups();
}

async function seedIndustry(domain) {
  const payload = await request(`/api/demo/${encodeURIComponent(domain)}`, {
    method: "POST",
    body: JSON.stringify({ tenant_id: state.tenant }),
  });
  showNotice(payload.message);
  await loadOverview();
  const active = $(".view.is-active").id;
  if (active === "imports") await loadImports();
}

async function changeTenant() {
  const value = $("#tenantInput").value.trim();
  if (!value) {
    showNotice("Enter a company identifier.", true);
    return;
  }
  state.tenant = value;
  await loadOverview();
  const active = $(".view.is-active").id;
  if (active === "approvals") await loadProposals();
  if (active === "ask") await askAtlas();
  if (active === "cartographer") await loadCartography();
  if (active === "imports") await loadImports();
  if (active === "audit") await loadAudit();
  if (active === "automation") await loadAutomation();
  if (active === "executions") await loadExecutions();
  if (active === "hotel") await loadHotelWorkspace();
  if (active === "providers") await loadProviders();
  showNotice(`Switched to ${value}.`);
}

async function init() {
  try {
    const bootstrap = await request("/api/bootstrap");
    state.token = bootstrap.request_token;
    state.tenant = bootstrap.default_tenant;
    $("#tenantInput").value = state.tenant;
    $$(".nav-item").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
    $$("[data-go]").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.go)));
    $("#seedHotel").addEventListener("click", () => seedDemo().catch((error) => showNotice(error.message, true)));
    $("#seedHotelWorkspace").addEventListener("click", () => seedDemo().catch((error) => showNotice(error.message, true)));
    $("#refreshHotel").addEventListener("click", () => loadHotelWorkspace().catch((error) => showNotice(error.message, true)));
    $("#seedApartment").addEventListener("click", () => seedApartment().catch((error) => showNotice(error.message, true)));
    $$(".industry-demo").forEach((button) => button.addEventListener(
      "click",
      () => seedIndustry(button.dataset.domain).catch((error) => showNotice(error.message, true)),
    ));
    $("#switchTenant").addEventListener("click", () => changeTenant().catch((error) => showNotice(error.message, true)));
    $("#proposalFilter").addEventListener("change", () => loadProposals().catch((error) => showNotice(error.message, true)));
    $("#askForm").addEventListener("submit", (event) => {
      event.preventDefault();
      askAtlas().catch((error) => showNotice(error.message, true));
    });
    $("#searchMaps").addEventListener("click", () => loadCartography().catch((error) => showNotice(error.message, true)));
    $("#refreshAudit").addEventListener("click", () => loadAudit().catch((error) => showNotice(error.message, true)));
    $("#refreshImports").addEventListener("click", () => loadImports().catch((error) => showNotice(error.message, true)));
    $("#refreshAutomation").addEventListener("click", () => loadAutomation().catch((error) => showNotice(error.message, true)));
    $("#refreshExecutions").addEventListener("click", () => loadExecutions().catch((error) => showNotice(error.message, true)));
    $("#refreshProviders").addEventListener("click", () => loadProviders().catch((error) => showNotice(error.message, true)));
    $("#executionFilter").addEventListener("change", () => loadExecutions().catch((error) => showNotice(error.message, true)));
    ["#fileImportInput", "#fileDomain", "#fileFormat", "#fileConnectorId", "#fileSourceSystem"]
      .forEach((selector) => $(selector).addEventListener("input", invalidateFilePreview));
    $("#previewFileImport").addEventListener("click", () => previewFileImport().catch((error) => showNotice(error.message, true)));
    $("#applyFileImport").addEventListener("click", () => applyFileImport().catch((error) => showNotice(error.message, true)));
    $("#createBackup").addEventListener("click", () => createBackup().catch((error) => showNotice(error.message, true)));
    $("#importBackup").addEventListener("click", () => importBackup().catch((error) => showNotice(error.message, true)));
    $("#previewRetention").addEventListener("click", () => previewRetention().catch((error) => showNotice(error.message, true)));
    $("#approveProposal").addEventListener("click", () => decide(true).catch((error) => showNotice(error.message, true)));
    $("#rejectProposal").addEventListener("click", () => decide(false).catch((error) => showNotice(error.message, true)));
    $("#confirmExecution").addEventListener("click", () => confirmSandboxExecution().catch((error) => showNotice(error.message, true)));
    await loadOverview();
  } catch (error) {
    showNotice(error.message, true);
  }
}

init();
