const state = {
  data: null,
  allProposals: [],
  proposals: [],
  feedback: loadFeedback(),
  favoriteSourceIds: loadSet("datepipeFavoriteSources"),
  hiddenSourceIds: loadSet("datepipeHiddenSources"),
  expandedProposalIds: new Set(),
  pointer: null,
  lastSwipeAt: 0,
  cityResearch: null,
  cityResearchTimer: null,
  researchReview: null,
  weeklyEventResearch: null,
  qualityReport: null,
  releaseEvaluation: null,
  reviewFilters: {
    kind: "all",
    status: "new",
  },
};

const $ = (selector) => document.querySelector(selector);

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function csv(value) {
  return Array.isArray(value) ? value.join(", ") : String(value || "");
}

function fromCsv(value) {
  return String(value || "")
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
}

function loadFeedback() {
  try {
    return JSON.parse(localStorage.getItem("datepipeFeedback") || "{}");
  } catch {
    return {};
  }
}

function loadSet(key) {
  try {
    const values = JSON.parse(localStorage.getItem(key) || "[]");
    return new Set(Array.isArray(values) ? values : []);
  } catch {
    return new Set();
  }
}

function saveSet(key, value) {
  localStorage.setItem(key, JSON.stringify([...value]));
}

function saveFeedback() {
  localStorage.setItem("datepipeFeedback", JSON.stringify(state.feedback));
}

function rememberFeedback(proposal, action) {
  if (!proposal) return;
  for (const key of [`category:${proposal.category}`, `stretch:${proposal.stretch}`, `source:${proposal.sourceIdeaId}`]) {
    state.feedback[key] = state.feedback[key] || { keep: 0, remove: 0 };
    state.feedback[key][action] += 1;
  }
  saveFeedback();
}

function statusLabel(value) {
  if (value === "precise") return "more details";
  return String(value || "open").replaceAll("-", " ").replaceAll("_", " ");
}

function timingLabel(value) {
  if (value === "precise") return "more details";
  return statusLabel(value);
}

function durationLabel(value) {
  if (value === "normal evening") return "evening";
  return statusLabel(value);
}

function attachmentPreview(attachment) {
  const isImage = attachment.previewUrl || (attachment.kind === "image" && attachment.value);
  const safeValue = escapeHtml(attachment.value || "");
  return `
    <article class="attachment-preview">
      <strong>${escapeHtml(attachment.label || "Attachment")}</strong>
      <span>${escapeHtml(statusLabel(attachment.kind || "link"))}</span>
      ${isImage ? `<img src="${escapeHtml(attachment.previewUrl || attachment.value)}" alt="${escapeHtml(attachment.label || "Attachment preview")}" loading="lazy">` : ""}
      ${String(attachment.value || "").startsWith("http") ? `<a href="${safeValue}" target="_blank" rel="noreferrer">Open</a>` : `<small>${safeValue}</small>`}
    </article>
  `;
}

function proposalImage(proposal) {
  return proposal.imageUrl ? `<img class="detail-image" src="${escapeHtml(proposal.imageUrl)}" alt="${escapeHtml(proposal.title)}" loading="lazy">` : "";
}

function currentFormValues() {
  const values = Object.fromEntries(new FormData($("#findForm")).entries());
  return { ...values, mood: "" };
}

function currentFilters() {
  return Object.fromEntries(new FormData($("#filters")).entries());
}

async function loadState() {
  state.data = await api("/api/state");
  renderAll();
  await findDates();
  maybeShowOnboarding();
}

function renderAll() {
  renderSearchDate();
  renderDateFilter();
  renderFavorites();
  renderHiddenIdeas();
  renderBookedThings();
  renderExistingDates();
  renderSettings();
  renderResearchReview();
  renderWeeklyEventResearch();
  const ai = state.data.ai || {};
  $("#aiStatus").textContent = ai.geminiConfigured ? "Gemini key set" : (ai.openaiConfigured ? "OpenAI key set" : "local");
  const cityInput = $("#findCity");
  if (cityInput && !cityInput.value) cityInput.value = state.data.profile?.homeBase || "Vienna";
  renderCityResearch();
  loadResearchReview().catch(() => {});
  loadWeeklyEventResearch().catch(() => {});
}

function renderSearchDate() {
  const input = $("#searchDate");
  if (input.value) {
    renderWeekRail();
    return;
  }
  const preferred = state.data.slots.find((slot) => ["needs-plan", "open", "idea", "candidate"].includes(slot.status));
  input.value = maxIsoDate(preferred?.start, todayIso());
  renderWeekRail();
}

function renderWeekRail() {
  const rail = $("#weekendRail");
  if (!rail) return;
  const selected = $("#searchDate").value || todayIso();
  rail.innerHTML = upcomingWeeks(10).map((week, index) => {
    const active = selected >= week.start && selected <= week.end;
    const conflict = weekConflictSummary(week.start, week.end);
    return `
      <button class="weekend-card week-card ${active ? "active" : ""} ${conflict ? "has-conflict" : ""}" type="button" data-week="${escapeHtml(week.start)}">
        <strong>${index === 0 ? "This week" : `Week ${index + 1}`}</strong>
        <span class="weekend-days">
          ${week.days.map((day) => `
            <span>
              <em>${escapeHtml(day.weekday)}</em>
              <b>${escapeHtml(day.day)}</b>
            </span>
          `).join("")}
        </span>
        <small>${escapeHtml(conflict || week.label)}</small>
      </button>
    `;
  }).join("");
}

function renderDateFilter() {
  const dates = searchWindowDates();
  const select = $("#filters select[name='date']");
  select.innerHTML = `<option value="">All</option>` + dates.map((item) => (
    `<option value="${escapeHtml(item.iso)}">${escapeHtml(item.label)}</option>`
  )).join("");
}

function renderBookedThings() {
  const bookedSlots = state.data.slots.filter((slot) => ["booked", "done"].includes(slot.status));
  const bookedPlans = (state.data.plans || []).filter((plan) => ["booked", "done"].includes(plan.status));
  $("#bookedCount").textContent = `${bookedSlots.length + bookedPlans.length}`;
  const rows = [
    ...bookedSlots.map((slot) => ({
      id: slot.id,
      title: slot.title || "Booked date",
      meta: `${slot.start} to ${slot.end} · ${statusLabel(slot.status)}`,
      note: slot.notes || "",
      type: "slot",
    })),
    ...bookedPlans.map((plan) => ({
      id: plan.id,
      title: plan.title || "Plan",
      meta: `${plan.dateChoice || "unscheduled"} · ${plan.mode || "plan"} · reveal ${plan.reveal || "later"} · ${plan.status}`,
      note: plan.visibleHint || plan.hiddenNotes || "",
      type: "plan",
    })),
  ];
  $("#bookedList").innerHTML = rows.length ? rows.map((row) => `
    <button class="status-row ${row.type === "plan" ? "plan-strip" : ""}" data-${row.type}="${escapeHtml(row.id)}">
      <strong>${escapeHtml(row.title)}</strong>
      <span>${escapeHtml(row.meta)}</span>
      ${row.note ? `<small>${escapeHtml(row.note)}</small>` : ""}
    </button>
  `).join("") : `<div class="empty">Nothing booked yet.</div>`;
}

function renderExistingDates() {
  const plansBySlot = new Map();
  for (const plan of state.data.plans || []) {
    if (!plansBySlot.has(plan.slotId)) plansBySlot.set(plan.slotId, []);
    plansBySlot.get(plan.slotId).push(plan);
  }
  const visibleSlots = state.data.slots.filter((slot) => !["booked", "done"].includes(slot.status));
  $("#dateCount").textContent = `${visibleSlots.length}`;
  $("#dateList").innerHTML = visibleSlots.map((slot) => {
    const plans = plansBySlot.get(slot.id) || [];
    return `
      <article class="status-row date-row">
        <div class="card-head">
          <div>
            <p class="eyebrow">${escapeHtml(slot.week || "date")}</p>
            <h3>${escapeHtml(slot.title || "Open date window")}</h3>
            <p class="meta">${escapeHtml(slot.start)} to ${escapeHtml(slot.end)}</p>
          </div>
          <span class="pill status-${escapeHtml(slot.status || "open")}">${escapeHtml(statusLabel(slot.status))}</span>
        </div>
        <p class="meta">${escapeHtml(slot.notes || "")}</p>
        ${weekConflictSummary(slot.start, slot.end) ? `<p class="warning-line">${escapeHtml(weekConflictSummary(slot.start, slot.end))}</p>` : ""}
        ${plans.map((plan) => `
          <button class="plan-strip" data-plan="${escapeHtml(plan.id)}">
            ${escapeHtml(plan.title)} · reveal ${escapeHtml(plan.reveal || "later")}
          </button>
        `).join("")}
        <div class="tag-row">${(slot.tags || []).map((tag) => `<span class="tag">${escapeHtml(statusLabel(tag))}</span>`).join("")}</div>
        <div class="action-row">
          <button class="tiny-button use-slot" data-slot="${escapeHtml(slot.id)}">Find ideas here</button>
          <button class="tiny-button status-slot" data-slot="${escapeHtml(slot.id)}">Change plan state</button>
        </div>
      </article>
    `;
  }).join("");
}

function renderSettings() {
  const profile = state.data.profile || {};
  const form = $("#settingsForm");
  form.elements.homeBase.value = profile.homeBase || "Vienna";
  form.elements.budgetComfort.value = profile.budgetComfort || "casual";
  form.elements.freeTime.value = profile.freeTime || "evening";
  form.elements.frequency.value = profile.frequency || "weekly";
  form.elements.radiusKm.value = String(profile.radiusKm || 6);
  form.elements.searchKeywords.value = (profile.searchKeywords || ["comedy"])[0] || "comedy";
  form.elements.impossibleDays.value = (profile.impossibleDays || [])[0] || "";
}

async function findDates(event) {
  if (event) event.preventDefault();
  const values = currentFormValues();
  const slot = selectedSlot();
  const payload = await api("/api/generate", {
    method: "POST",
    body: JSON.stringify({
      slotId: slot?.id,
      searchDate: values.searchDate,
      mood: values.mood,
      profile: { homeBase: values.homeBase || state.data.profile?.homeBase || "Vienna" },
    }),
  });
  $("#aiStatus").textContent = payload.source || "local";
  state.cityResearch = payload.cityResearch || null;
  state.qualityReport = payload.qualityReport || null;
  renderCityResearch();
  await loadResearchReview().catch(() => {});
  state.allProposals = (payload.options || []).sort(sortByFeedbackAndVariety);
  applyFilters();
}

async function loadResearchReview() {
  const city = $("#findCity")?.value || state.data?.profile?.homeBase || "Vienna";
  const date = $("#searchDate")?.value || todayIso();
  const params = new URLSearchParams({ city, date });
  state.researchReview = await api(`/api/research-review?${params.toString()}`, { headers: {} });
  if (!qualityReportMatches(state.qualityReport, city, date)) {
    state.qualityReport = state.researchReview.qualityReport || null;
  }
  renderResearchReview();
}

async function loadReleaseEvaluation() {
  const city = $("#findCity")?.value || state.data?.profile?.homeBase || "Vienna";
  const params = new URLSearchParams({ cities: city, weeks: "4" });
  state.releaseEvaluation = await api(`/api/developer/release-evaluation?${params.toString()}`, { headers: {} });
  renderReleaseEvaluation();
}

async function loadWeeklyEventResearch() {
  state.weeklyEventResearch = await api("/api/developer/weekly-event-research/latest", { headers: {} });
  renderWeeklyEventResearch();
}

function renderWeeklyEventResearch() {
  const panel = $("#weeklyResearchPanel");
  if (!panel) return;
  const report = state.weeklyEventResearch;
  if (!report) {
    panel.innerHTML = `<div class="empty">No weekly research report loaded yet.</div>`;
    return;
  }
  if (!report.available) {
    panel.innerHTML = `
      <article class="quality-card weekly-card">
        <div class="quality-head">
          <div>
            <p class="eyebrow">weekly source freshness</p>
            <h3>${escapeHtml(statusLabel(report.status || "missing report"))}</h3>
          </div>
          <span class="pill">not run</span>
        </div>
        <p class="meta">${escapeHtml(report.message || "Run the weekly event research tool to create the first report.")}</p>
      </article>
    `;
    return;
  }
  const rows = (report.rows || []).slice(0, 6).map((row) => `
    <article class="weekly-row ${row.needsCodexReview ? "needs-review" : ""}">
      <div>
        <strong>${escapeHtml(row.city || "City")} · ${escapeHtml(row.weekStart || "")}</strong>
        <span>${Number(row.acceptedEventCount || 0)} leads · ${Number(row.fetchedSourceCount || 0)} fetched · ${escapeHtml(statusLabel(row.qualityGateStatus || "unknown"))}</span>
        ${(row.flags || []).slice(0, 3).map((flag) => `<small>${escapeHtml(statusLabel(flag))}</small>`).join("")}
      </div>
      <span class="pill">${row.needsCodexReview ? "review" : "fresh"}</span>
    </article>
  `).join("");
  const weakCities = (report.weakCities || []).slice(0, 8).map((city) => `<span class="tag">${escapeHtml(city)}</span>`).join("");
  const families = (report.sourceSummary?.sourceFamilyCounts || []).slice(0, 5).map((item) => `
    <div class="quality-row">
      <div>
        <strong>${escapeHtml(statusLabel(item[0]))}</strong>
        <span>source family</span>
      </div>
      <span class="quality-score">${Number(item[1] || 0)}</span>
    </div>
  `).join("");
  panel.innerHTML = `
    <article class="quality-card weekly-card">
      <div class="quality-head">
        <div>
          <p class="eyebrow">weekly source freshness</p>
          <h3>${Number(report.cityCount || 0)} cities · ${Number(report.weekCount || 0)} weeks</h3>
        </div>
        <span class="pill">${escapeHtml((report.weakCities || []).length ? "needs review" : "fresh")}</span>
      </div>
      <p class="meta">Generated ${escapeHtml(report.generatedAt || "")}</p>
      ${weakCities ? `<div class="tag-row">${weakCities}</div>` : `<p class="success-line">No weak cities in the latest report.</p>`}
      ${families ? `<div class="quality-grid">${families}</div>` : ""}
      <div class="weekly-list">${rows || `<div class="empty">No city-week rows in this report.</div>`}</div>
    </article>
  `;
}

function qualityReportMatches(report, city, date) {
  if (!report) return false;
  return String(report.city || "").toLowerCase() === String(city || "").toLowerCase()
    && String(report.date || "").slice(0, 10) === String(date || "").slice(0, 10);
}

function renderReleaseEvaluation() {
  const panel = $("#releaseScorePanel");
  if (!panel) return;
  const report = state.releaseEvaluation;
  if (!report) {
    panel.innerHTML = `<div class="empty">Run a no-user release check for the selected city and upcoming weeks.</div>`;
    return;
  }
  const rows = (report.rows || []).slice(0, 4).map((row) => `
    <article class="release-row">
      <div>
        <strong>${escapeHtml(row.label || `${row.weekStart} to ${row.weekEnd}`)}</strong>
        <span>
          proposals ${Number(row.suggestionScore || 0)} · catalog ${Number(row.catalogScore || 0)} ·
          retrieval ${Number(row.retrievalScore || 0)} · research ${Number(row.researchScore || 0)}
        </span>
        ${(row.flags || []).slice(0, 3).map((flag) => `<small>${escapeHtml(statusLabel(flag))}</small>`).join("")}
      </div>
      <span class="quality-score">${Number(row.score || 0)}</span>
    </article>
  `).join("");
  const planning = report.components?.planning;
  const blockers = (report.blockingIssues || []).slice(0, 3).map((issue) => `
    <article class="release-issue ${escapeHtml(issue.severity || "watch")}">
      <strong>${escapeHtml(statusLabel(issue.severity || "watch"))}</strong>
      <span>${escapeHtml(issue.message || statusLabel(issue.code || "release issue"))}</span>
    </article>
  `).join("");
  panel.innerHTML = `
    <article class="quality-card release-card">
      <div class="quality-head">
        <div>
          <p class="eyebrow">release readiness</p>
          <h3>${Number(report.score || 0)} · ${escapeHtml(statusLabel(report.band || "unknown"))}</h3>
        </div>
        <span class="pill">${escapeHtml(statusLabel(report.releaseGate || "unknown"))}</span>
      </div>
      <p class="meta">${escapeHtml(report.summary || "Release check")}</p>
      <p class="${report.readyForRelease ? "success-line" : "warning-line"}">
        ${report.readyForRelease ? "Ready for release smoke." : `${Number((report.blockingIssues || []).length)} release issues need attention.`}
      </p>
      ${planning ? `<p class="warning-line">Planning readiness ${Number(planning.score || 0)} · ${escapeHtml(statusLabel(planning.band || ""))}</p>` : ""}
      ${blockers ? `<div class="release-issues">${blockers}</div>` : ""}
      <div class="release-list">${rows}</div>
    </article>
  `;
}

function formatRemaining(seconds) {
  const total = Math.max(0, Number(seconds || 0));
  const minutes = Math.floor(total / 60);
  const secs = total % 60;
  return `${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

function renderCityResearch() {
  const panel = $("#cityResearchPanel");
  if (!panel) return;
  if (state.cityResearchTimer) {
    clearInterval(state.cityResearchTimer);
    state.cityResearchTimer = null;
  }
  const job = state.cityResearch;
  if (!job) {
    panel.classList.add("hidden");
    panel.innerHTML = "";
    return;
  }
  const remaining = Math.max(0, Number(job.remainingSeconds || 0));
  const total = 3600;
  const elapsed = Math.min(total, total - remaining);
  const progress = Math.max(4, Math.min(100, Math.round((elapsed / total) * 100)));
  const terminal = ["completed", "ready", "ready_needs_review", "source_validation_failed", "failed", "cancelled", "incomplete"].includes(job.status);
  const localQueue = job.provider === "local" && job.status === "queued";
  const integration = job.integration || null;
  const costPolicy = job.costPolicy || null;
  const integratedLine = integration ? `
    <p class="meta">
      ${Number(integration.promotedSourceCount || 0)} sources promoted ·
      ${escapeHtml(integration.sourceReadinessStatus || "source gate pending")} ·
      ${Number(integration.eventExtraction?.eventCount || 0)} leads ·
      ${Number(integration.eventExtraction?.imageResolvedCount || 0)} images
    </p>
  ` : "";
  const costLine = costPolicy && !costPolicy.deepResearchEnabled ? `
    <p class="meta">API deep research is off. No paid research call is running.</p>
  ` : "";
  const primaryStatus = terminal || localQueue ? statusLabel(job.status || "queued") : formatRemaining(remaining);
  const secondaryStatus = localQueue ? "manual scout" : (job.qualityGateStatus || job.provider || "local");
  panel.classList.remove("hidden");
  panel.innerHTML = `
    <div>
      <p class="eyebrow">city scout</p>
      <h3>${escapeHtml(job.city)} is being added</h3>
      <p class="meta">${escapeHtml(job.message || "We are building this city source pack.")}</p>
      ${integratedLine}
      ${costLine}
    </div>
    <div class="research-countdown" data-remaining="${remaining}">
      <strong>${escapeHtml(primaryStatus)}</strong>
      <span>${escapeHtml(secondaryStatus)}</span>
    </div>
    <div class="research-progress"><span style="width: ${progress}%"></span></div>
  `;
  if (remaining > 0 && !terminal && !localQueue) {
    state.cityResearchTimer = setInterval(() => {
      const node = panel.querySelector(".research-countdown");
      if (!node) return;
      const next = Math.max(0, Number(node.dataset.remaining || 0) - 1);
      node.dataset.remaining = String(next);
      const strong = node.querySelector("strong");
      if (strong) strong.textContent = formatRemaining(next);
      if (next <= 0) clearInterval(state.cityResearchTimer);
    }, 1000);
  }
}

function renderResearchReview() {
  const panel = $("#researchReviewPanel");
  if (!panel) return;
  const review = state.researchReview;
  if (!review) {
    panel.innerHTML = `<div class="empty">No review data loaded yet.</div>`;
    return;
  }
  const job = review.job || {};
  const sourceStatus = review.sourceStatus || {};
  const validation = review.validation || {};
  const quality = state.qualityReport || review.qualityReport || null;
  const sources = filteredReviewSources(review);
  const events = filteredReviewEvents(review);
  const sourceRows = sources.slice(0, 8).map((source) => `
    <article class="review-row">
      <div>
        <strong>${escapeHtml(source.name || source.id)}</strong>
        <span>${escapeHtml(source.sourceKind || "source")} · ${escapeHtml(statusLabel(source.reviewStatus || "new"))} · ${Number(source.lastEventCount || 0)} leads</span>
        <small>${escapeHtml(source.url || "")}</small>
      </div>
      <div class="review-actions">
        <button class="tiny-button review-source" data-source="${escapeHtml(source.id)}" data-status="approved">Approve</button>
        <button class="tiny-button review-source" data-source="${escapeHtml(source.id)}" data-status="needs_review">Later</button>
        <button class="tiny-button review-source" data-source="${escapeHtml(source.id)}" data-status="rejected">Reject</button>
      </div>
    </article>
  `).join("");
  const eventRows = events.slice(0, 8).map((item) => `
    <article class="review-row">
      <div>
        <strong>${escapeHtml(item.title || "Event lead")}</strong>
        <span>${escapeHtml(item.sourceName || "source")} · ${escapeHtml(statusLabel(item.researchState?.reviewStatus || item.reviewStatus || "new"))}</span>
        <small>${escapeHtml(item.eventUrl || "")}</small>
      </div>
      <div class="review-actions">
        <button class="tiny-button review-event" data-event="${escapeHtml(item.researchState?.id || item.id || eventIdFromUrl(item.eventUrl))}" data-status="approved">Keep</button>
        <button class="tiny-button review-event" data-event="${escapeHtml(item.researchState?.id || item.id || eventIdFromUrl(item.eventUrl))}" data-status="needs_review">Later</button>
        <button class="tiny-button review-event" data-event="${escapeHtml(item.researchState?.id || item.id || eventIdFromUrl(item.eventUrl))}" data-status="rejected">Drop</button>
      </div>
    </article>
  `).join("");
  const rejectedRows = (validation.rejectedSources || []).slice(0, 4).map((source) => `
    <div class="status-row rejected-source">
      <strong>${escapeHtml(source.name || source.url)}</strong>
      <span>${escapeHtml(source.rejectionReason || "rejected")}</span>
    </div>
  `).join("");
  panel.innerHTML = `
    ${quality ? qualityReportMarkup(quality) : ""}
    <div class="review-summary">
      <span class="pill">${escapeHtml(review.city || "city")}</span>
      <span class="pill">${escapeHtml(sourceStatus.sourceReadinessStatus || job.qualityGateStatus || "not scouted")}</span>
      <span class="pill">${Number(validation.validatedCount || 0)} valid / ${Number(validation.rejectedCount || 0)} rejected</span>
    </div>
    <div class="review-filterbar" aria-label="Review filters">
      ${reviewFilterButton("kind", "all", `All ${Number((review.sources || []).length) + Number((review.events || []).length)}`)}
      ${reviewFilterButton("kind", "sources", `Sources ${Number((review.sources || []).length)}`)}
      ${reviewFilterButton("kind", "events", `Leads ${Number((review.events || []).length)}`)}
      ${reviewFilterButton("status", "new", "New")}
      ${reviewFilterButton("status", "needs_review", "Later")}
      ${reviewFilterButton("status", "approved", "Approved")}
      ${reviewFilterButton("status", "rejected", "Rejected")}
    </div>
    ${job.message ? `<p class="meta">${escapeHtml(job.message)}</p>` : ""}
    <div class="review-columns">
      <div class="${state.reviewFilters.kind === "events" ? "hidden" : ""}">
        <h3>Sources</h3>
        ${sourceRows || `<div class="empty">No sources match these filters.</div>`}
      </div>
      <div class="${state.reviewFilters.kind === "sources" ? "hidden" : ""}">
        <h3>Event leads</h3>
        ${eventRows || `<div class="empty">No event leads match these filters.</div>`}
      </div>
    </div>
    ${rejectedRows ? `<div><h3>Rejected sources</h3><div class="status-list">${rejectedRows}</div></div>` : ""}
  `;
}

function qualityReportMarkup(report) {
  const dimensions = (report.dimensions || []).slice(0, 6).map((item) => `
    <div class="quality-row">
      <div>
        <strong>${escapeHtml(statusLabel(item.name))}</strong>
        <span>${escapeHtml(item.note || "")}</span>
      </div>
      <span class="quality-score">${Number(item.score || 0)}</span>
    </div>
  `).join("");
  const flags = (report.flags || []).slice(0, 4).map((flag) => `<span class="tag">${escapeHtml(statusLabel(flag))}</span>`).join("");
  return `
    <article class="quality-card">
      <div class="quality-head">
        <div>
          <p class="eyebrow">no-user quality meter</p>
          <h3>${Number(report.score || 0)} · ${escapeHtml(statusLabel(report.band || "unknown"))}</h3>
        </div>
        <span class="pill">${escapeHtml(statusLabel(report.scope || "quality"))}</span>
      </div>
      <p class="meta">${escapeHtml(report.summary || "Quality report")}</p>
      <div class="quality-grid">${dimensions}</div>
      ${flags ? `<div class="tag-row">${flags}</div>` : ""}
    </article>
  `;
}

function reviewFilterButton(group, value, label) {
  const active = state.reviewFilters[group] === value;
  return `<button class="filter ${active ? "active" : ""}" type="button" data-review-filter="${group}" data-value="${escapeHtml(value)}">${escapeHtml(label)}</button>`;
}

function filteredReviewSources(review) {
  if (state.reviewFilters.kind === "events") return [];
  return (review.sources || []).filter((source) => reviewStatusMatches(source.reviewStatus || "new"));
}

function filteredReviewEvents(review) {
  if (state.reviewFilters.kind === "sources") return [];
  return (review.events || []).filter((item) => reviewStatusMatches(item.researchState?.reviewStatus || item.reviewStatus || "new"));
}

function reviewStatusMatches(status) {
  const wanted = state.reviewFilters.status;
  if (!wanted || wanted === "all") return true;
  return String(status || "new") === wanted;
}

function eventIdFromUrl(url) {
  return `event_${String(url || "").split("").reduce((hash, char) => ((hash << 5) - hash + char.charCodeAt(0)) | 0, 0)}`;
}

function applyFilters() {
  state.proposals = filterProposals(state.allProposals, currentFilters())
    .filter((proposal) => !state.hiddenSourceIds.has(proposal.sourceIdeaId));
  renderProposals();
  renderFavorites();
  renderHiddenIdeas();
}

function filterProposals(proposals, values) {
  return proposals.filter((proposal) => {
    if (values.date && proposal.dateChoice !== values.date) return false;
    if (values.category && proposal.category !== values.category) return false;
    if (values.duration && proposal.duration !== values.duration) return false;
    if (values.stretch && proposal.stretch !== values.stretch) return false;
    return true;
  });
}

function selectedSearchDate() {
  const value = maxIsoDate($("#searchDate").value, todayIso());
  $("#searchDate").value = value;
  return new Date(`${value}T12:00:00`);
}

function selectedSlot() {
  const target = selectedSearchDate().getTime();
  return [...state.data.slots].sort((left, right) => {
    const leftDiff = Math.abs(new Date(`${left.start}T12:00:00`).getTime() - target);
    const rightDiff = Math.abs(new Date(`${right.start}T12:00:00`).getTime() - target);
    return leftDiff - rightDiff;
  })[0];
}

function searchWindowDates() {
  const center = selectedSearchDate();
  const today = new Date(`${todayIso()}T12:00:00`);
  const monday = weekStart(center);
  return Array.from({ length: 7 }, (_, offset) => {
    const date = new Date(monday);
    date.setDate(monday.getDate() + offset);
    return {
      label: `${date.toLocaleDateString("en-US", { weekday: "short" })} ${date.toISOString().slice(5, 10)}`,
      iso: date.toISOString().slice(0, 10),
    };
  }).filter((item) => new Date(`${item.iso}T12:00:00`) >= today);
}

function todayIso() {
  return new Date().toISOString().slice(0, 10);
}

function maxIsoDate(value, minimum) {
  if (!value || value < minimum) return minimum;
  return value;
}

function weekStart(day) {
  const out = new Date(day);
  const offset = (out.getDay() + 6) % 7;
  out.setDate(out.getDate() - offset);
  return out;
}

function upcomingWeeks(count) {
  const today = new Date(`${todayIso()}T12:00:00`);
  const first = weekStart(today);
  return Array.from({ length: count }, (_, index) => {
    const start = new Date(first);
    start.setDate(first.getDate() + index * 7);
    const days = Array.from({ length: 7 }, (_, offset) => {
      const day = new Date(start);
      day.setDate(start.getDate() + offset);
      return {
        iso: day.toISOString().slice(0, 10),
        weekday: day.toLocaleDateString("en-US", { weekday: "short" }),
        day: day.getDate().toString(),
      };
    });
    return {
      start: days[0].iso,
      end: days[2].iso,
      label: `${days[0].iso.slice(5)} to ${days[2].iso.slice(5)}`,
      days,
    };
  });
}

function weekConflictSummary(start, end) {
  const activePlans = (state.data?.plans || []).filter((plan) => ["candidate", "needs-booking", "booked"].includes(plan.status));
  const planCount = activePlans.filter((plan) => {
    const value = plan.dateChoice || (plan.itinerary || []).find((step) => step.date)?.date || "";
    return value && value >= start && value <= end;
  }).length;
  const bookedSlotCount = (state.data?.slots || []).filter((slot) => ["booked", "done"].includes(slot.status) && rangesOverlap(start, end, slot.start, slot.end)).length;
  const total = planCount + bookedSlotCount;
  if (!total) return "";
  return `${total} active date${total === 1 ? "" : "s"} this week`;
}

function rangesOverlap(leftStart, leftEnd, rightStart, rightEnd) {
  return String(leftStart || "") <= String(rightEnd || rightStart || "") && String(rightStart || "") <= String(leftEnd || leftStart || "");
}

function assignDates(proposals) {
  const dates = searchWindowDates();
  if (!dates.length) return proposals;
  return proposals.map((proposal, index) => {
    const date = dates[index % dates.length];
    return { ...proposal, dateChoice: date.iso, dateLabel: date.label };
  }).sort(sortByFeedbackAndVariety);
}

function sortByFeedbackAndVariety(left, right) {
  return proposalWeight(right) - proposalWeight(left);
}

function proposalWeight(proposal) {
  let weight = 0;
  for (const key of [`category:${proposal.category}`, `stretch:${proposal.stretch}`, `source:${proposal.sourceIdeaId}`]) {
    const item = state.feedback[key];
    if (!item) continue;
    weight += (item.keep || 0) * 8;
    weight -= (item.remove || 0) * 12;
  }
  if (proposal.stretch === "outside-zone") weight += 2;
  if (proposal.category === "social") weight += 2;
  return weight;
}

function renderProposals() {
  $("#suggestionCount").textContent = `${state.proposals.length}`;
  if (!state.proposals.length) {
    $("#proposalList").innerHTML = `<div class="empty">No match yet. Choose a broader date style, time, or surprise level.</div>`;
    return;
  }
  $("#proposalList").innerHTML = state.proposals.map((proposal) => `
    <article class="card suggestion-card ${state.expandedProposalIds.has(proposal.id) ? "expanded" : ""} ${state.favoriteSourceIds.has(proposal.sourceIdeaId) ? "kept" : ""}" data-proposal-card="${escapeHtml(proposal.id)}">
      <div class="proposal-thumb ${escapeHtml(visualClass(proposal))}">
        <span>${escapeHtml(visualLabel(proposal))}</span>
      </div>
      <div class="card-head">
        <div>
          <h3>${escapeHtml(proposal.title)}</h3>
          <p class="meta">${escapeHtml(proposal.dateLabel || "weekend")} · ${escapeHtml(durationLabel(proposal.duration))} · ${escapeHtml(statusLabel(proposal.stretch))}</p>
        </div>
        <span class="pill">${state.favoriteSourceIds.has(proposal.sourceIdeaId) ? "favorite" : escapeHtml(timingLabel(proposal.timing))}</span>
      </div>
      <div class="proposal-detail">
        <p>${escapeHtml(proposal.why)}</p>
        <p class="meta">Hint for partner: ${escapeHtml(proposal.visibleHint)}</p>
        <p class="swipe-help">Swipe right to keep. Swipe left to remove.</p>
        <div class="action-row">
          <button class="primary-button make-plan" data-proposal="${escapeHtml(proposal.id)}">Make this plan</button>
          <button class="secondary-button proposal-details" data-proposal="${escapeHtml(proposal.id)}">More details</button>
        </div>
      </div>
    </article>
  `).join("");
}

function renderFavorites() {
  const favorites = state.allProposals.filter((proposal) => state.favoriteSourceIds.has(proposal.sourceIdeaId));
  $("#favoriteCount").textContent = `${favorites.length}`;
  $("#favoriteList").innerHTML = favorites.length ? favorites.map((proposal) => `
    <button class="status-row plan-strip" data-favorite="${escapeHtml(proposal.id)}">
      <strong>${escapeHtml(proposal.title)}</strong>
      <span>${escapeHtml(durationLabel(proposal.duration))} · ${escapeHtml(statusLabel(proposal.category))}</span>
      <small>${escapeHtml(proposal.sourceName || "real link attached")}</small>
    </button>
  `).join("") : `<div class="empty">Swipe right on an idea to save it here.</div>`;
}

function renderHiddenIdeas() {
  const hidden = state.allProposals.filter((proposal) => state.hiddenSourceIds.has(proposal.sourceIdeaId));
  $("#hiddenList").innerHTML = hidden.length ? hidden.map((proposal) => `
    <div class="status-row">
      <strong>${escapeHtml(proposal.title)}</strong>
      <span>Hidden from future suggestions</span>
      <button class="tiny-button restore-hidden" data-source="${escapeHtml(proposal.sourceIdeaId)}">Restore</button>
    </div>
  `).join("") : `<div class="empty">Swipe left on an idea to stop considering it.</div>`;
}

function visualClass(proposal) {
  return `visual-${String(proposal.category || "standard").replace(/[^a-z0-9-]/gi, "").toLowerCase()}`;
}

function visualLabel(proposal) {
  const labels = {
    standard: "DATE",
    sportive: "MOVE",
    different: "NEW",
    cultural: "ART",
    culinary: "FOOD",
    romantic: "MOOD",
    social: "2+2",
    premium: "MENU",
  };
  return labels[proposal.category] || "PLAN";
}

function proposalById(id) {
  return state.allProposals.find((proposal) => proposal.id === id);
}

function planById(id) {
  return (state.data.plans || []).find((plan) => plan.id === id);
}

async function makePlan(id) {
  const proposal = proposalById(id);
  if (!proposal) return;
  await api("/api/plans", {
    method: "POST",
    body: JSON.stringify({ proposal }),
  });
  closeModal();
  await loadState();
  window.scrollTo({ top: $("#dateList").offsetTop - 12, behavior: "smooth" });
}

function showProposal(id) {
  const proposal = proposalById(id);
  if (!proposal) return;
  showModal("Date idea", `
    <article class="card">
      ${proposalImage(proposal)}
      <h3>${escapeHtml(proposal.title)}</h3>
      <p>${escapeHtml(proposal.why)}</p>
      <p class="meta">Hint: ${escapeHtml(proposal.visibleHint)}</p>
      <p class="meta">Backup: ${escapeHtml(proposal.backup)}</p>
      ${proposal.eventUrl ? `<p class="meta">Source link: <a href="${escapeHtml(proposal.eventUrl)}" target="_blank" rel="noreferrer">${escapeHtml(proposal.sourceName || proposal.eventUrl)}</a></p>` : ""}
      <div class="tag-row">${(proposal.steps || []).map((step) => `<span class="tag">${escapeHtml(step)}</span>`).join("")}</div>
      <div class="action-row">
        <button class="primary-button make-plan" data-proposal="${escapeHtml(proposal.id)}">Make this plan</button>
      </div>
    </article>
  `);
}

function showPlan(id) {
  const plan = planById(id);
  if (!plan) return;
  const attachments = plan.attachments || [];
  const warnings = plan.scheduleWarnings || [];
  showModal("Existing plan", `
    <div class="stack">
      <article class="card">
        ${proposalImage(plan)}
        <h3>${escapeHtml(plan.title)}</h3>
        <p class="meta">${escapeHtml(plan.dateChoice || "unscheduled")} · ${escapeHtml(plan.mode)} · ${escapeHtml(plan.status)} · reveal ${escapeHtml(plan.reveal)}</p>
        ${warnings.map((warning) => `<p class="warning-line">${escapeHtml(warning.message || "Schedule conflict")}</p>`).join("")}
        <p><strong>Visible hint:</strong> ${escapeHtml(plan.visibleHint || "")}</p>
        <p><strong>Hidden notes:</strong> ${escapeHtml(plan.hiddenNotes || "")}</p>
        <p><strong>Backup:</strong> ${escapeHtml(plan.backup || "")}</p>
        ${plan.eventUrl ? `<p class="meta">Place: <a href="${escapeHtml(plan.eventUrl)}" target="_blank" rel="noreferrer">${escapeHtml(plan.sourceName || plan.eventUrl)}</a></p>` : ""}
      </article>
      <article class="card">
        <div class="section-heading first">
          <h3>Attachments</h3>
          <button class="tiny-button add-attachment" data-plan="${escapeHtml(plan.id)}">Add</button>
        </div>
        <div class="attachment-grid">
          ${attachments.map(attachmentPreview).join("") || `<p class="meta">No attachments yet. Add the website, ticket, or reservation note.</p>`}
        </div>
      </article>
      <article class="card">
        <h3>Todos</h3>
        ${(plan.todos || []).map((todo) => `
          <label class="todo-row">
            <input type="checkbox" ${todo.done ? "checked" : ""} data-plan="${escapeHtml(plan.id)}" data-todo="${escapeHtml(todo.id)}">
            <span>${escapeHtml(todo.text)}</span>
          </label>
        `).join("") || `<p class="meta">No todos.</p>`}
      </article>
    </div>
  `);
}

function showAttachmentForm(planId) {
  const plan = planById(planId);
  if (!plan) return;
  showModal("Add attachment", `
    <form id="attachmentForm" class="form-grid">
      <input type="hidden" name="planId" value="${escapeHtml(plan.id)}">
      <label>Attachment
        <select name="preset">
          <option value="website">Official website</option>
          <option value="ticket">Ticket preview</option>
          <option value="reservation">Reservation note</option>
        </select>
      </label>
      <button class="primary-button wide" type="submit">Add attachment</button>
    </form>
  `);
}

function showStatusForm(slotId) {
  const slot = state.data.slots.find((item) => item.id === slotId);
  if (!slot) return;
  showModal("Change date status", `
    <form id="slotForm" class="form-grid">
      <label class="wide">Title <input name="title" value="${escapeHtml(slot.title)}"></label>
      <label>Status
        <select name="status">
          ${["open", "needs-plan", "idea", "candidate", "needs-booking", "booked", "blocked", "done"].map((status) => `
            <option value="${status}" ${slot.status === status ? "selected" : ""}>${statusLabel(status)}</option>
          `).join("")}
        </select>
      </label>
      <label class="wide">Notes <input name="notes" value="${escapeHtml(slot.notes || "")}"></label>
      <label class="wide">Date style
        <select name="tags">
          <option value="standard" ${(slot.tags || []).includes("standard") ? "selected" : ""}>standard</option>
          <option value="culinary" ${(slot.tags || []).includes("culinary") ? "selected" : ""}>culinary</option>
          <option value="cultural" ${(slot.tags || []).includes("cultural") ? "selected" : ""}>cultural</option>
          <option value="sportive" ${(slot.tags || []).includes("sportive") ? "selected" : ""}>sportive</option>
          <option value="different" ${(slot.tags || []).includes("different") ? "selected" : ""}>different</option>
        </select>
      </label>
      <button class="primary-button wide" type="submit" data-slot="${escapeHtml(slot.id)}">Save</button>
    </form>
  `);
}

async function saveSettings() {
  const form = $("#settingsForm");
  const payload = Object.fromEntries(new FormData(form).entries());
  payload.radiusKm = Number(payload.radiusKm || 0);
  payload.searchKeywords = payload.searchKeywords ? [payload.searchKeywords] : [];
  payload.impossibleDays = payload.impossibleDays ? [payload.impossibleDays] : [];
  await api("/api/settings", { method: "POST", body: JSON.stringify(payload) });
  await loadState();
}

async function useCurrentLocationForHomeBase() {
  const form = $("#settingsForm");
  if (!navigator.geolocation) {
    $("#aiStatus").textContent = "gps unavailable";
    return;
  }
  $("#aiStatus").textContent = "detecting location";
  navigator.geolocation.getCurrentPosition(async (position) => {
    const params = new URLSearchParams({
      lat: String(position.coords.latitude),
      lon: String(position.coords.longitude),
    });
    const payload = await api(`/api/reverse-geocode?${params.toString()}`, { headers: {} });
    if (payload.city) {
      form.elements.homeBase.value = payload.city;
      await saveSettings();
      $("#aiStatus").textContent = `using ${payload.city}`;
    } else {
      $("#aiStatus").textContent = "city not found";
    }
  }, () => {
    $("#aiStatus").textContent = "gps denied";
  }, { enableHighAccuracy: false, timeout: 10000, maximumAge: 900000 });
}

function showModal(title, body) {
  $("#modalTitle").textContent = title;
  $("#modalBody").innerHTML = body;
  $("#modal").classList.remove("hidden");
}

function closeModal() {
  $("#modal").classList.add("hidden");
}

function maybeShowOnboarding() {
  if (localStorage.getItem("datepipeOnboarded") === "1") return;
  const profile = state.data.profile || {};
  showModal("Set up DatePipe", `
    <form id="onboardingForm" class="form-grid">
      <label>Home base
        <input name="homeBase" value="${escapeHtml(profile.homeBase || "Vienna")}" autocomplete="address-level2">
      </label>
      <label>Budget comfort
        <select name="budgetComfort">
          <option value="casual">casual</option>
          <option value="nice">nice</option>
          <option value="premium">premium sometimes</option>
          <option value="free">mostly free</option>
        </select>
      </label>
      <label>Free time
        <select name="freeTime">
          <option value="2 hours">2 hours</option>
          <option value="evening">an evening</option>
          <option value="half day">half day</option>
          <option value="weekend">weekend</option>
        </select>
      </label>
      <label>Dating rhythm
        <select name="frequency">
          <option value="weekly">once a week</option>
          <option value="biweekly">every second week</option>
          <option value="monthly">once a month</option>
        </select>
      </label>
      <label class="wide">What already works?
        <select name="comfort">
          <option value="cultural">culture</option>
          <option value="culinary">food</option>
          <option value="comedy">comedy</option>
          <option value="seasonal">seasonal</option>
        </select>
      </label>
      <label class="wide">What should stretch you?
        <select name="stretch">
          <option value="sportive">active</option>
          <option value="dance class">dance class</option>
          <option value="new district">new district</option>
          <option value="unusual food">unusual food</option>
        </select>
      </label>
      <button class="primary-button wide" type="submit">Start planning</button>
    </form>
  `);
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (button) {
    if (button.id === "refreshBtn") await loadState();
    if (button.id === "refreshReviewBtn") await loadResearchReview();
    if (button.id === "refreshWeeklyResearchBtn") await loadWeeklyEventResearch();
    if (button.id === "runReleaseBtn") await loadReleaseEvaluation();
    if (button.id === "modalClose") closeModal();
    if (button.id === "saveSettingsBtn") await saveSettings();
    if (button.id === "useLocationBtn") await useCurrentLocationForHomeBase();
    if (button.matches(".weekend-card")) {
      $("#searchDate").value = button.dataset.week;
      renderWeekRail();
      renderDateFilter();
      await findDates();
    }
    if (button.id === "resetHiddenBtn") {
      state.hiddenSourceIds.clear();
      saveSet("datepipeHiddenSources", state.hiddenSourceIds);
      applyFilters();
    }
    if (button.matches(".make-plan")) await makePlan(button.dataset.proposal);
    if (button.matches(".proposal-details")) showProposal(button.dataset.proposal);
    if (button.matches(".restore-hidden")) {
      state.hiddenSourceIds.delete(button.dataset.source);
      saveSet("datepipeHiddenSources", state.hiddenSourceIds);
      applyFilters();
    }
    if (button.matches("[data-favorite]")) showProposal(button.dataset.favorite);
    if (button.matches(".use-slot")) {
      const slot = state.data.slots.find((item) => item.id === button.dataset.slot);
      if (slot) $("#searchDate").value = slot.start;
      renderDateFilter();
      window.scrollTo({ top: 0, behavior: "smooth" });
      await findDates();
    }
    if (button.matches(".status-slot")) showStatusForm(button.dataset.slot);
    if (button.matches(".plan-strip")) showPlan(button.dataset.plan);
    if (button.matches(".add-attachment")) showAttachmentForm(button.dataset.plan);
    if (button.matches("[data-review-filter]")) {
      state.reviewFilters[button.dataset.reviewFilter] = button.dataset.value;
      renderResearchReview();
    }
    if (button.matches(".review-source")) {
      await api(`/api/research/sources/${encodeURIComponent(button.dataset.source)}`, {
        method: "PATCH",
        body: JSON.stringify({ reviewStatus: button.dataset.status }),
      });
      await loadResearchReview();
    }
    if (button.matches(".review-event")) {
      await api(`/api/research/events/${encodeURIComponent(button.dataset.event)}`, {
        method: "PATCH",
        body: JSON.stringify({ reviewStatus: button.dataset.status }),
      });
      await loadResearchReview();
    }
    return;
  }
  const card = event.target.closest("[data-proposal-card]");
  if (card && Date.now() - state.lastSwipeAt > 250) {
    const id = card.dataset.proposalCard;
    if (state.expandedProposalIds.has(id)) state.expandedProposalIds.delete(id);
    else state.expandedProposalIds.add(id);
    renderProposals();
  }
});

document.addEventListener("pointerdown", (event) => {
  const card = event.target.closest("[data-proposal-card]");
  if (!card || event.target.closest("button")) return;
  state.pointer = {
    id: card.dataset.proposalCard,
    x: event.clientX,
    y: event.clientY,
  };
});

document.addEventListener("pointerup", (event) => {
  if (!state.pointer) return;
  const dx = event.clientX - state.pointer.x;
  const dy = event.clientY - state.pointer.y;
  const id = state.pointer.id;
  state.pointer = null;
  if (Math.abs(dx) < 60 || Math.abs(dx) < Math.abs(dy) * 1.4) return;
  state.lastSwipeAt = Date.now();
  if (dx > 0) {
    rememberFeedback(proposalById(id), "keep");
    const proposal = proposalById(id);
    if (proposal) {
      state.favoriteSourceIds.add(proposal.sourceIdeaId);
      state.hiddenSourceIds.delete(proposal.sourceIdeaId);
      saveSet("datepipeFavoriteSources", state.favoriteSourceIds);
      saveSet("datepipeHiddenSources", state.hiddenSourceIds);
    }
    state.expandedProposalIds.delete(id);
    renderProposals();
    renderFavorites();
    renderHiddenIdeas();
  } else {
    const proposal = proposalById(id);
    if (proposal) {
      if (state.favoriteSourceIds.has(proposal.sourceIdeaId)) {
        state.favoriteSourceIds.delete(proposal.sourceIdeaId);
      } else {
        rememberFeedback(proposal, "remove");
        state.hiddenSourceIds.add(proposal.sourceIdeaId);
      }
      saveSet("datepipeHiddenSources", state.hiddenSourceIds);
      saveSet("datepipeFavoriteSources", state.favoriteSourceIds);
    }
    state.expandedProposalIds.delete(id);
    applyFilters();
  }
});

document.addEventListener("submit", async (event) => {
  if (event.target.id === "findForm") {
    await findDates(event);
    return;
  }
  if (event.target.id === "slotForm") {
    event.preventDefault();
    const payload = Object.fromEntries(new FormData(event.target).entries());
    payload.tags = fromCsv(payload.tags);
    await api(`/api/slots/${encodeURIComponent(event.submitter.dataset.slot)}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
    closeModal();
    await loadState();
  }
  if (event.target.id === "attachmentForm") {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.target).entries());
    const plan = planById(values.planId);
    if (!plan) return;
    const presets = {
      website: {
        label: "Official website",
        kind: "website",
        value: plan.eventUrl || "",
        previewUrl: plan.imageUrl || "",
      },
      ticket: {
        label: "Ticket preview",
        kind: "ticket",
        value: "Ticket or QR screenshot preview",
        previewUrl: plan.imageUrl || "",
      },
      reservation: {
        label: "Reservation note",
        kind: "note",
        value: plan.hiddenNotes || plan.backup || "Reservation note",
        previewUrl: "",
      },
    };
    await api("/api/attachments", {
      method: "POST",
      body: JSON.stringify({ planId: plan.id, ...(presets[values.preset] || presets.website) }),
    });
    closeModal();
    await loadState();
    showPlan(plan.id);
  }
  if (event.target.id === "onboardingForm") {
    event.preventDefault();
    const payload = Object.fromEntries(new FormData(event.target).entries());
    payload.comfort = payload.comfort ? [payload.comfort] : [];
    payload.stretch = payload.stretch ? [payload.stretch] : [];
    localStorage.setItem("datepipeOnboarded", "1");
    await api("/api/settings", { method: "POST", body: JSON.stringify(payload) });
    closeModal();
    await loadState();
  }
});

document.addEventListener("change", async (event) => {
  if (event.target.matches("#searchDate")) {
    renderDateFilter();
    await findDates();
  }
  if (event.target.matches("#filters select")) applyFilters();
  if (event.target.matches("input[type='checkbox'][data-plan][data-todo]")) {
    const plan = planById(event.target.dataset.plan);
    if (!plan) return;
    const todos = (plan.todos || []).map((todo) => (
      todo.id === event.target.dataset.todo ? { ...todo, done: event.target.checked } : todo
    ));
    await api(`/api/plans/${encodeURIComponent(plan.id)}`, {
      method: "PATCH",
      body: JSON.stringify({ todos }),
    });
    await loadState();
    showPlan(plan.id);
  }
});

loadState().catch((error) => {
  document.body.innerHTML = `<main class="app-shell"><article class="card"><h1>DatePipe</h1><p>${escapeHtml(error.message)}</p></article></main>`;
});
