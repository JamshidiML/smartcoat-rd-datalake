const API = "http://127.0.0.1:8000";
let token = sessionStorage.getItem("scRdToken");
let activeDraft = null;
let sourceObjectUrl = null;
let pipelinePollTimer = null;
let lastUploads = [];
const knownJobStatuses = new Map();

const $ = (selector) => document.querySelector(selector);
const message = (text, error = false) => {
  const element = $("#message");
  element.textContent = text;
  element.style.background = error ? "#8d2f27" : "#17201d";
  element.classList.add("visible");
  window.setTimeout(() => element.classList.remove("visible"), 4200);
};
const request = async (path, options = {}) => {
  const headers = new Headers(options.headers || {});
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(`${API}${path}`, { ...options, headers });
  if (!response.ok) {
    let detail = response.statusText;
    try { const payload = await response.json(); detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail); } catch (_) { /* use status */ }
    throw new Error(detail);
  }
  return response;
};

function showWorkspace() {
  $("#login-panel").classList.toggle("hidden", Boolean(token));
  $("#workspace").classList.toggle("hidden", !token);
  $("#logout").classList.toggle("hidden", !token);
  if (token) refreshActivity({ announce: false });
  else stopPipelinePolling();
}

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const response = await request("/api/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: $("#email").value, password: $("#password").value }),
    });
    token = (await response.json()).access_token;
    sessionStorage.setItem("scRdToken", token);
    showWorkspace(); message("Signed in to the local pilot.");
  } catch (error) { message(error.message, true); }
});

$("#logout").addEventListener("click", () => {
  token = null; lastUploads = []; knownJobStatuses.clear();
  sessionStorage.removeItem("scRdToken"); showWorkspace();
});

document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
  document.querySelectorAll(".view").forEach((view) => view.classList.add("hidden"));
  tab.classList.add("active"); $(`#${tab.dataset.view}`).classList.remove("hidden");
  if (tab.dataset.view === "review-view") refreshActivity({ announce: false });
  if (tab.dataset.view === "activity-view") refreshActivity({ announce: false });
}));

$("#upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData();
  form.set("file", $("#file").files[0]);
  form.set("document_category", $("#document-category").value);
  form.set("context_note", $("#context-note").value);
  if ($("#capture-date").value) form.set("capture_date", $("#capture-date").value);
  try {
    const response = await request("/api/uploads", { method: "POST", body: form });
    const result = await response.json();
    event.target.reset();
    message("Bronze saved. OCR is running in the background; Review queue updates automatically.");
    refreshActivity();
  } catch (error) { message(error.message, true); }
});

async function refreshDrafts() {
  try {
    const drafts = await (await request("/api/drafts")).json();
    const list = $("#draft-list");
    const activeCount = lastUploads.filter(isPipelineActive).length;
    const countLabel = drafts.length
      ? `${drafts.length} waiting${activeCount ? ` · ${activeCount} processing` : ""}`
      : activeCount ? `${activeCount} processing` : "0 waiting";
    const countClass = activeCount && !drafts.length ? "status processing" : "status warning";
    list.innerHTML = `<div class="section-heading"><div><p class="eyebrow">SILVER QUEUE</p><h2>Unverified drafts</h2></div><span class="${countClass}">${countLabel}</span></div>`;
    drafts.forEach((draft) => {
      const row = document.createElement("div"); row.className = "queue-item";
      row.innerHTML = `<div><strong>${escapeHtml(draft.original_filename)}</strong><p class="mono">${draft.ingestion_id}</p></div>`;
      const button = document.createElement("button"); button.textContent = "Compare source";
      button.addEventListener("click", () => openDraft(draft.silver_draft_id)); row.append(button); list.append(row);
    });
    if (!drafts.length && activeCount) {
      list.insertAdjacentHTML("beforeend", `<div class="pipeline-wait"><span class="pipeline-spinner" aria-hidden="true"></span><div><strong>OCR is processing in the background</strong><p>${activeCount === 1 ? "This file" : "These files"} will appear here automatically when ready. You can keep using the platform.</p></div></div>`);
    } else if (!drafts.length) {
      list.insertAdjacentHTML("beforeend", "<p>No drafts are waiting for human review.</p>");
    }
  } catch (error) { message(error.message, true); }
}

async function openDraft(draftId) {
  try {
    const context = await (await request(`/api/drafts/${draftId}`)).json(); activeDraft = context;
    $("#review-editor").classList.remove("hidden"); $("#review-title").textContent = context.upload.original_filename;
    $("#ocr-text").value = context.draft.extracted_text; $("#verified-text").value = context.draft.extracted_text;
    $("#source-meta").innerHTML = `<dt>SHA-256</dt><dd class="mono">${context.upload.source_sha256}</dd><dt>Uploader</dt><dd>${escapeHtml(context.upload.uploader_display_name)}</dd><dt>Category</dt><dd>${context.upload.document_category}</dd><dt>Captured</dt><dd>${context.upload.capture_date || "Unknown"}</dd>`;
    const response = await request(`/api/uploads/${context.upload.ingestion_id}/source`);
    if (sourceObjectUrl) URL.revokeObjectURL(sourceObjectUrl); sourceObjectUrl = URL.createObjectURL(await response.blob());
    const preview = $("#source-preview"); preview.replaceChildren();
    if (context.upload.declared_file_type === "PHOTO") { const image = new Image(); image.src = sourceObjectUrl; image.alt = "Immutable Bronze source"; preview.append(image); }
    else if (context.upload.declared_file_type === "PDF") { const frame = document.createElement("iframe"); frame.src = sourceObjectUrl; frame.title = "Immutable Bronze PDF"; preview.append(frame); }
    else { const link = document.createElement("a"); link.href = sourceObjectUrl; link.download = context.upload.original_filename; link.textContent = "Download immutable Excel source for comparison"; preview.append(link); }
    $("#review-editor").scrollIntoView({ behavior: "smooth" });
  } catch (error) { message(error.message, true); }
}

$("#submit-review").addEventListener("click", async () => {
  if (!activeDraft) return;
  try {
    await request(`/api/drafts/${activeDraft.draft.silver_draft_id}/review`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ verified_text: $("#verified-text").value, decision: $("#decision").value, correction_summary: $("#correction-summary").value, explicit_confirmation: $("#confirmation").checked }),
    });
    $("#review-editor").classList.add("hidden"); activeDraft = null; message("Human review decision recorded in the audit trail."); refreshActivity();
  } catch (error) { message(error.message, true); }
});

async function refreshActivity({ announce = true } = {}) {
  if (!token) return;
  stopPipelinePolling();
  try {
    const uploads = await (await request("/api/uploads")).json();
    const list = $("#activity-list"); list.replaceChildren();
    uploads.forEach((upload) => {
      const previous = knownJobStatuses.get(upload.ingestion_id);
      if (announce && ["QUEUED", "RUNNING"].includes(previous) && upload.ocr_job_status === "COMPLETED") {
        message(`${upload.original_filename} is ready for human review.`);
      }
      knownJobStatuses.set(upload.ingestion_id, upload.ocr_job_status);
      const display = pipelineDisplay(upload);
      const row = document.createElement("div"); row.className = "activity-item";
      row.innerHTML = `<div><strong>${escapeHtml(upload.original_filename)}</strong><p class="activity-detail">${escapeHtml(display.detail)}</p><p class="mono">${upload.source_sha256}</p></div><span class="status ${display.className}">${display.label}</span>`;
      list.append(row);
    });
    lastUploads = uploads;
    if (!uploads.length) list.innerHTML = "<p>No uploads yet.</p>";
    if (!$("#review-view").classList.contains("hidden")) await refreshDrafts();
    schedulePipelinePolling(uploads);
  } catch (error) {
    if (error.message.includes("Authentication")) { token = null; sessionStorage.removeItem("scRdToken"); showWorkspace(); }
    else { message(error.message, true); pipelinePollTimer = window.setTimeout(() => refreshActivity(), 5000); }
  }
}

function isPipelineActive(upload) { return ["QUEUED", "RUNNING"].includes(upload.ocr_job_status); }
function stopPipelinePolling() { if (pipelinePollTimer) window.clearTimeout(pipelinePollTimer); pipelinePollTimer = null; }
function schedulePipelinePolling(uploads) {
  if (token && uploads.some(isPipelineActive)) pipelinePollTimer = window.setTimeout(() => refreshActivity(), 2000);
}
function elapsedLabel(timestamp) {
  if (!timestamp) return "";
  const seconds = Math.max(0, Math.round((Date.now() - new Date(timestamp).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}
function pipelineDisplay(upload) {
  if (upload.ocr_job_status === "RUNNING") return { label: "OCR PROCESSING", className: "processing", detail: `Extracting text · ${elapsedLabel(upload.ocr_started_at_utc)} elapsed · updates automatically` };
  if (upload.ocr_job_status === "QUEUED") return { label: "OCR WAITING", className: "warning", detail: "Waiting for the local OCR worker · updates automatically" };
  if (upload.ocr_job_status === "FAILED" || upload.state === "OCR_FAILED") return { label: "OCR FAILED", className: "error", detail: "The source remains safe in Bronze; the OCR attempt needs attention" };
  if (upload.state === "SILVER_DRAFT_READY") return { label: "READY FOR REVIEW", className: "ready", detail: "OCR complete · open Review queue to compare and confirm" };
  if (upload.state === "VERIFIED") return { label: "VERIFIED", className: "ready", detail: "Human-reviewed Silver record created" };
  return { label: upload.state.replaceAll("_", " "), className: "", detail: "Pipeline record is up to date" };
}
function escapeHtml(value) { const node = document.createElement("span"); node.textContent = value; return node.innerHTML; }
showWorkspace();
