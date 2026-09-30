"use strict";

const STAGES = ["OBSERVE", "TRIGGER", "ANALYZE", "GUARD", "FIX", "VERIFY", "GIT", "DONE"];
const STATUS_LABEL = {
  accumulating: "Đang gom lỗi",
  queued: "Chờ chạy",
  running: "Agent đang chạy",
  waiting_for_review: "Chờ review PR",
  reported: "Đã báo cáo",
  merged: "Đã merge",
  dismissed: "PR bị đóng",
  closed: "Đã đóng",
  failed: "Thất bại",
};
// Statuses where a fix already exists; repeats are attached instead of re-running the agent.
const FIX_PENDING = ["waiting_for_review", "merged", "dismissed", "reported"];
const MODE_LABEL = { report_and_fix: "Báo cáo + fix", fix_only: "Chỉ fix", report_only: "Chỉ báo cáo" };
const FINISHED = ["waiting_for_review", "merged", "dismissed", "closed"];
const FILTERS = [
  ["all", "Tất cả", () => true],
  ["active", "Đang xử lý", (i) => ["accumulating", "queued", "running", "reported"].includes(i.status)],
  ["review", "Review", (i) => i.status === "waiting_for_review"],
  ["done", "Đã xử lý", (i) => ["merged", "dismissed", "closed"].includes(i.status)],
  ["failed", "Lỗi", (i) => i.status === "failed"],
];
// run.status -> [stage index reached, is error]
const RUN_PROGRESS = {
  analyzing: [2, false],
  fixing: [4, false],
  verifying: [5, false],
  publishing: [6, false],
  waiting_for_review: [8, false],
  analysis_failed: [2, true],
  fix_failed: [4, true],
  verification_failed: [5, true],
  publish_failed: [6, true],
};

const state = {
  overview: null,
  detail: null,
  selected: location.hash.slice(1) || null,
  filter: "all",
  lastOverview: "",
  lastDetail: "",
  openKeys: new Set(),
  failures: 0,
};

/* ---------- helpers ---------- */

function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "style") node.style.cssText = value; // CSSOM is allowed by the strict CSP
    else if (key === "open") node.open = Boolean(value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

// replaceChildren() would render null as the text "null"; drop empty slots first.
function fill(node, ...children) {
  node.replaceChildren(...children.flat().filter((child) => child !== null && child !== undefined && child !== false));
}

const short = (id) => (id || "").slice(0, 8);

function ago(ts) {
  if (!ts) return "—";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return `${Math.floor(s)}s trước`;
  if (s < 3600) return `${Math.floor(s / 60)} phút trước`;
  if (s < 86400) return `${Math.floor(s / 3600)} giờ trước`;
  return `${Math.floor(s / 86400)} ngày trước`;
}

function clock(ts) {
  return ts ? new Date(ts * 1000).toLocaleTimeString("vi-VN", { hour12: false }) : "—";
}

function stamp(ts) {
  return ts ? new Date(ts * 1000).toLocaleString("vi-VN", { hour12: false }) : "—";
}

function duration(start, end) {
  if (!start) return "—";
  const s = Math.round((end || Date.now() / 1000) - start);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

function badge(status) {
  return h("span", { class: `badge ${status}` }, STATUS_LABEL[status] || status);
}

function toast(message) {
  const node = document.getElementById("toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.remove("show"), 3500);
}

async function api(path, options) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw Object.assign(new Error(body.error || response.statusText), { status: response.status });
  return body;
}

/* ---------- top bar & stats ---------- */

function renderPills(o) {
  const w = o.watcher;
  const watcher = w.online
    ? h("span", { class: "pill" }, h("span", { class: "dot ok" }), "Watcher ", h("strong", {}, "online"))
    : w.last_seen
      ? h("span", { class: "pill", title: "Watcher không poll log gần đây: đã dừng hoặc đang chạy agent" }, h("span", { class: "dot warn" }), "Watcher ", h("strong", {}, `im lặng ${ago(w.last_seen)}`))
      : h("span", { class: "pill" }, h("span", { class: "dot bad" }), "Watcher ", h("strong", {}, "chưa chạy"));
  const s = o.settings;
  const r = o.runtime;
  fill(document.getElementById("pills"), 
    watcher,
    h("span", { class: "pill" }, "DB ", h("strong", {}, o.database === "mysql" ? "MySQL" : "SQLite")),
    h("span", { class: "pill" }, "Chế độ ", h("strong", {}, MODE_LABEL[r.mode] || r.mode)),
    h("span", { class: "pill" }, "Ngưỡng ", h("strong", {}, `${r.threshold} lỗi / ${r.window_seconds}s`)),
    h("span", { class: "pill", title: s.log_source }, "Log ", h("strong", {}, s.log_source.split(":")[0] === "file" ? "file local" : s.log_source.split(":")[0])),
    h("span", { class: "pill" }, "Base ", h("strong", {}, s.base_branch)),
    h("span", { class: "pill" }, "PR ", h("strong", {}, s.publish_pull_request ? s.github_repository : "tắt")),
    h("span", { class: "pill" }, "Telegram ", h("strong", {}, s.telegram_enabled ? "bật" : "tắt")),
  );
}

function renderStats(o) {
  const c = o.counts;
  const total = Object.values(c).reduce((a, b) => a + b, 0);
  const tiles = [
    ["Tổng incident", total],
    ["Đang gom lỗi", c.accumulating || 0],
    ["Đang chạy", (c.running || 0) + (c.queued || 0)],
    ["Chờ người xử lý", (c.waiting_for_review || 0) + (c.reported || 0)],
    ["Thất bại", c.failed || 0],
  ];
  fill(document.getElementById("stats"), 
    ...tiles.map(([label, value]) => h("div", { class: "stat" }, h("span", {}, label), h("strong", {}, value))),
  );
}

/* ---------- incident list ---------- */

function renderFilters() {
  fill(document.getElementById("filters"), 
    ...FILTERS.map(([key, label]) =>
      h("button", {
        type: "button", role: "tab", "aria-selected": state.filter === key ? "true" : "false",
        onclick: () => { state.filter = key; renderFilters(); renderList(); },
      }, label),
    ),
  );
}

function renderList() {
  const list = document.getElementById("incident-list");
  if (!state.overview) return;
  const test = FILTERS.find(([key]) => key === state.filter)[2];
  const items = state.overview.incidents.filter(test);
  if (!items.length) {
    fill(list, h("li", { class: "list-empty" }, state.overview.incidents.length
      ? "Không có incident nào trong bộ lọc này."
      : "Chưa có incident. Gây lỗi profile trong Laravel để watcher ghi nhận."));
    return;
  }
  fill(list, ...items.map((i) => {
    const pct = Math.min(100, Math.round((i.occurrence_count / i.threshold) * 100));
    return h("li", {},
      h("button", {
        type: "button", "aria-current": i.id === state.selected ? "true" : "false",
        onclick: () => select(i.id),
      },
        h("div", { class: "row" }, h("code", {}, short(i.id)), badge(i.status)),
        h("p", { class: "incident-msg" }, i.message),
        h("div", { class: "row meta-line" },
          h("span", {}, `${i.occurrence_count}/${i.threshold} lần`,
            i.suppressed_count ? h("span", {
              class: "repeat-tag",
              title: i.status === "reported" ? "Lỗi lặp lại sau khi đã báo cáo" : "Lỗi lặp lại sau khi đã có fix; không chạy agent lại",
            }, ` · +${i.suppressed_count} ${i.status === "reported" ? "sau báo cáo" : "sau fix"}`) : null),
          h("span", {}, ago(i.last_seen_at))),
        h("div", { class: "progress" }, h("span", { style: `width:${pct}%` })),
      ));
  }));
}

/* ---------- live feed ---------- */

function renderFeed(o) {
  const items = [...o.events].reverse();
  const feed = document.getElementById("feed");
  if (!items.length) {
    fill(feed, h("li", { class: "meta-line" }, "Chưa có event. Chạy `make watch` để bắt đầu."));
    return;
  }
  fill(feed, ...items.map((e) =>
    h("li", { "data-stage": e.stage },
      h("div", { class: "row" },
        h("span", { class: "stage-tag" }, e.stage),
        h("span", { class: "meta-line mono" }, clock(e.created_at))),
      h("span", { class: "msg" }, e.message),
      e.incident_id ? h("button", { type: "button", class: "link", onclick: () => select(e.incident_id) }, `incident ${short(e.incident_id)} →`) : null,
    )));
}

/* ---------- detail ---------- */

function progressFor(incident, run) {
  if (FINISHED.includes(incident.status)) return { reached: 8, current: 8, error: false };
  if (incident.status === "reported") return { reached: 2, current: -1, error: false };
  if (incident.status === "accumulating") return { reached: 0, current: 0, error: false };
  if (incident.status === "queued") return { reached: 1, current: 1, error: false };
  if (!run) return { reached: 1, current: 1, error: false };
  const [index, error] = RUN_PROGRESS[run.status] || [2, false];
  if (incident.status === "failed" && !error) return { reached: index, current: index, error: true };
  return { reached: index, current: index, error };
}

function renderStepper(incident, run) {
  const p = progressFor(incident, run);
  return h("div", { class: "stepper", "aria-label": "Pipeline progress" },
    ...STAGES.map((name, index) => {
      let cls = "step";
      if (index < p.reached) cls += " done";
      else if (index === p.current && p.error) cls += " error";
      else if (index === p.current && !FINISHED.includes(incident.status)) cls += " current";
      return h("div", { class: cls }, h("i"), name);
    }));
}

function fact(label, value, mono) {
  return h("div", { class: "fact" }, h("span", {}, label), mono ? h("code", { title: value || "" }, value || "—") : h("strong", { title: value || "" }, value || "—"));
}

function listBlock(label, values) {
  if (!values || !values.length) return null;
  return h("div", {}, h("div", { class: "label" }, label), h("ul", {}, ...values.map((v) => h("li", {}, v))));
}

function renderAnalysis(run) {
  const a = run && run.analysis;
  if (!a) return null;
  return h("section", { class: "block analysis" },
    h("h3", {}, `Phân tích của Claude · confidence ${Math.round((a.confidence || 0) * 100)}%`),
    h("p", {}, h("strong", {}, a.summary)),
    h("p", {}, h("span", { class: "label" }, "Root cause: "), a.root_cause),
    listBlock("Đề xuất sửa", a.proposed_fix),
    listBlock("File dự kiến", a.proposed_files),
    listBlock("Test bắt buộc", a.required_tests),
    listBlock("Rủi ro", a.risks),
  );
}

function renderVerification(run) {
  const v = run && run.verification;
  if (!v || !v.length) return null;
  return h("section", { class: "block" },
    h("h3", {}, "Trusted verification"),
    h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Command"), h("th", {}, "Kết quả"), h("th", {}, "Thời gian"))),
      h("tbody", {}, ...v.map((item) => h("tr", {},
        h("td", {}, h("code", {}, item.command.join(" "))),
        h("td", {}, item.returncode === 0 ? h("span", { class: "ok-text" }, "pass") : h("span", { class: "bad-text" }, `exit ${item.returncode}`)),
        h("td", { class: "num" }, `${item.duration_seconds}s`))))));
}

function keyedDetails(key, summary, body) {
  return h("details", {
    "data-key": key, open: state.openKeys.has(key),
    ontoggle: (event) => event.target.open ? state.openKeys.add(key) : state.openKeys.delete(key),
  }, h("summary", {}, summary), body);
}

function renderTimeline(events) {
  if (!events.length) return null;
  return h("section", { class: "block" },
    h("h3", {}, "Timeline"),
    h("ol", { class: "timeline" }, ...events.map((e) => {
      const ctx = Object.entries(e.context || {}).map(([k, v]) => `${k}=${v}`).join("  ");
      return h("li", { "data-stage": e.stage },
        h("div", {}, h("span", { class: "stage-tag" }, e.stage), " ", h("span", { class: "when" }, clock(e.created_at))),
        h("div", { class: "msg" }, e.message),
        ctx ? h("div", { class: "ctx" }, ctx) : null);
    })));
}

function renderRuns(runs) {
  if (!runs.length) return null;
  return h("section", { class: "block" },
    h("h3", {}, "Các lần chạy agent"),
    h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Run"), h("th", {}, "Trạng thái"), h("th", {}, "Bắt đầu"), h("th", {}, "Thời lượng"), h("th", {}, ""))),
      h("tbody", {}, ...runs.map((r) => h("tr", {},
        h("td", {}, h("code", {}, short(r.id))),
        h("td", {}, r.status, r.failure_reason ? h("div", { class: "ctx" }, r.failure_reason) : null),
        h("td", { class: "num" }, stamp(r.started_at)),
        h("td", { class: "num" }, duration(r.started_at, r.finished_at)),
        h("td", {}, r.has_report ? h("button", { type: "button", class: "btn ghost small", onclick: () => openReport(r.id) }, "Report") : null))))));
}

function renderOccurrences(items) {
  if (!items.length) return null;
  return h("section", { class: "block" },
    h("h3", {}, `Log occurrences (${items.length})`),
    ...items.map((o) => keyedDetails(`occ-${o.id}`, `${o.log_timestamp} · ghi nhận ${clock(o.observed_at)}`, h("pre", {}, o.excerpt))));
}

function renderDetail() {
  const root = document.getElementById("detail");
  const d = state.detail;
  if (!d) return;
  const i = d.incident;
  const run = d.runs[0];
  const actions = h("div", { class: "actions" },
    i.status === "failed" ? h("button", { type: "button", class: "btn danger", id: "retry", onclick: retry }, "Retry incident") : null,
    i.status === "reported" ? h("button", { type: "button", class: "btn", id: "retry", onclick: retry }, "Chạy AI fix") : null,
    FIX_PENDING.includes(i.status) ? h("button", {
      type: "button", class: "btn ghost", id: "release", onclick: release,
      title: i.status === "reported"
        ? "Đóng incident mà không sửa; lỗi xuất hiện lại sẽ tạo incident mới"
        : "Ngừng gom lỗi vào incident này; lần lỗi kế tiếp sẽ được phép chạy auto-fix mới",
    }, i.status === "reported" ? "Đóng incident" : "Cho phép fix lại") : null,
    i.pull_request_url ? h("a", { class: "btn", href: i.pull_request_url, target: "_blank", rel: "noopener noreferrer" }, "Mở Pull Request ↗") : null,
    run && run.has_report ? h("button", { type: "button", class: "btn ghost", onclick: () => openReport(run.id) }, "Xem report") : null,
  );
  fill(root, 
    h("div", { class: "detail-head" },
      h("div", {},
        h("div", { class: "row", style: "justify-content:flex-start" }, badge(i.status), h("code", {}, i.id)),
        h("h2", {}, i.message),
        h("div", { class: "meta-line" }, `Lần cuối ${ago(i.last_seen_at)} · cửa sổ bắt đầu ${stamp(i.window_started_at)}`)),
      actions),
    renderStepper(i, run),
    i.failure_reason ? h("section", { class: "block" }, h("div", { class: "callout bad" }, h("strong", {}, "Lý do dừng: "), i.failure_reason)) : null,
    fixCallout(i),
    h("section", { class: "block" },
      h("h3", {}, "Chi tiết"),
      h("div", { class: "facts" },
        fact("Số lần lỗi", `${i.occurrence_count} / ${i.threshold}`),
        fact("Top frame", i.top_application_frame, true),
        fact("Source commit", (i.source_commit || "").slice(0, 12), true),
        fact("Branch", i.branch_name, true),
        fact("Verified commit", (i.commit_sha || "").slice(0, 12), true),
        fact("Fingerprint", (i.fingerprint || "").slice(0, 16), true))),
    renderAnalysis(run),
    renderVerification(run),
    renderTimeline(d.events),
    renderRuns(d.runs),
    renderOccurrences(d.occurrences),
  );
}

function fixCallout(i) {
  const repeats = i.suppressed_count || 0;
  const repeatNote = repeats
    ? ` Lỗi đã lặp lại ${repeats} lần sau đó và được gom vào incident này; hệ thống không chạy agent lại.`
    : "";
  const messages = {
    waiting_for_review: ["ok", `Fix đã pass verification và được commit. Agent không tự merge; hãy review PR.${repeatNote}`],
    merged: [repeats ? "warn" : "ok", repeats
      ? `PR đã merge nhưng lỗi vẫn xuất hiện ${repeats} lần — nhiều khả năng bản fix chưa được deploy. Nếu đã deploy mà vẫn lỗi, bấm "Cho phép fix lại".`
      : "PR đã merge. Nếu lỗi quay lại trước khi deploy, hệ thống chỉ ghi nhận, không tạo fix mới."],
    dismissed: ["warn", `PR đã bị đóng mà không merge; auto-fix cho lỗi này tạm dừng.${repeatNote} Bấm "Cho phép fix lại" nếu muốn agent thử lần nữa.`],
    closed: ["ok", "Incident đã đóng. Lỗi cùng loại xuất hiện sau thời điểm này sẽ tạo incident mới."],
    reported: ["warn", `Chế độ chỉ báo cáo: lỗi đã đạt ngưỡng nhưng agent chưa chạy.${repeats ? ` Lỗi đã lặp lại thêm ${repeats} lần.` : ""} Bấm "Chạy AI fix" để sửa, hoặc "Đóng incident" để bỏ qua.`],
  };
  const entry = messages[i.status];
  if (!entry) return null;
  return h("section", { class: "block" }, h("div", { class: `callout ${entry[0]}` }, entry[1]));
}

/* ---------- actions ---------- */

function select(id) {
  if (state.selected !== id) {
    state.selected = id;
    state.lastDetail = "";
    state.openKeys.clear();
    history.replaceState(null, "", `#${id}`);
  }
  renderList();
  refreshDetail();
}

async function retry() {
  const button = document.getElementById("retry");
  if (button) button.disabled = true;
  try {
    const result = await api(`/api/incidents/${state.selected}/retry`, {
      method: "POST", headers: { "Content-Type": "application/json", "X-AI-Fix-Request": "1" }, body: "{}",
    });
    toast(result.watcher.online
      ? "Đã đưa incident vào hàng đợi. Watcher sẽ chạy lại với branch/worktree mới."
      : "Đã đưa vào hàng đợi, nhưng watcher chưa chạy — hãy start `make watch`.");
    await refresh();
  } catch (error) {
    toast(`Retry thất bại: ${error.message}`);
    if (button) button.disabled = false;
  }
}

async function release() {
  const button = document.getElementById("release");
  if (button) button.disabled = true;
  try {
    await api(`/api/incidents/${state.selected}/release`, {
      method: "POST", headers: { "Content-Type": "application/json", "X-AI-Fix-Request": "1" }, body: "{}",
    });
    toast("Đã mở khóa. Nếu lỗi xuất hiện lại đủ ngưỡng, watcher sẽ tạo incident và fix mới.");
    await refresh();
  } catch (error) {
    toast(`Không mở khóa được: ${error.message}`);
    if (button) button.disabled = false;
  }
}

async function openReport(runId) {
  const dialog = document.getElementById("report-dialog");
  document.getElementById("report-title").textContent = `Report · run ${short(runId)}`;
  document.getElementById("report-body").textContent = "Đang tải…";
  dialog.showModal();
  try {
    const result = await api(`/api/runs/${runId}/report`);
    document.getElementById("report-body").textContent = result.markdown;
  } catch (error) {
    document.getElementById("report-body").textContent = `Không đọc được report: ${error.message}`;
  }
}

/* ---------- polling ---------- */

async function refreshDetail() {
  if (!state.selected) return;
  try {
    const detail = await api(`/api/incidents/${state.selected}`);
    const serialized = JSON.stringify(detail);
    if (serialized !== state.lastDetail) {
      state.lastDetail = serialized;
      state.detail = detail;
      renderDetail();
    }
  } catch (error) {
    if (error.status === 404) {
      state.selected = null;
      state.detail = null;
      history.replaceState(null, "", location.pathname);
    }
  }
}

async function refresh() {
  try {
    const overview = await api("/api/overview");
    state.failures = 0;
    document.getElementById("live-dot").classList.remove("stale");
    const serialized = JSON.stringify({ ...overview, now: 0 });
    state.overview = overview;
    renderPills(overview);
    if (serialized !== state.lastOverview) {
      state.lastOverview = serialized;
      renderStats(overview);
      renderFeed(overview);
    }
    renderList(); // relative times change every tick
    if (!state.selected && overview.incidents.length) select(overview.incidents[0].id);
    else await refreshDetail();
  } catch (error) {
    state.failures += 1;
    document.getElementById("live-dot").classList.add("stale");
    if (state.failures === 2) toast("Mất kết nối tới dashboard server.");
  }
}

function setupClearDialog() {
  const dialog = document.getElementById("clear-dialog");
  const input = document.getElementById("clear-confirm");
  const submit = document.getElementById("clear-submit");
  const reset = () => {
    input.value = "";
    document.getElementById("clear-reports").checked = false;
    submit.disabled = true;
  };
  document.getElementById("clear-open").addEventListener("click", () => { reset(); dialog.showModal(); input.focus(); });
  document.getElementById("clear-cancel").addEventListener("click", () => dialog.close());
  input.addEventListener("input", () => { submit.disabled = input.value.trim() !== "CLEAR"; });
  document.getElementById("clear-form").addEventListener("submit", (event) => event.preventDefault());
  submit.addEventListener("click", async () => {
    submit.disabled = true;
    try {
      const result = await api("/api/clear", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-AI-Fix-Request": "1" },
        body: JSON.stringify({ confirm: input.value.trim(), delete_reports: document.getElementById("clear-reports").checked }),
      });
      dialog.close();
      state.selected = null;
      state.detail = null;
      state.lastDetail = "";
      state.lastOverview = "";
      history.replaceState(null, "", location.pathname);
      fill(document.getElementById("detail"), h("div", { class: "empty" },
        h("h2", {}, "Đã xóa dữ liệu"),
        h("p", {}, `Đã xóa ${result.deleted.incidents} incident${result.reports_deleted ? `, ${result.reports_deleted} report` : ""}. Bản sao lưu: ${result.backup}`)));
      toast("Đã xóa dữ liệu và lưu bản sao lưu.");
      await refresh();
    } catch (error) {
      toast(error.status === 404
        ? "Dashboard server đang chạy code cũ: dừng `make ui` (Ctrl-C) rồi chạy lại."
        : `Không xóa được: ${error.message}`);
      submit.disabled = input.value.trim() !== "CLEAR";
    }
  });
}

function setupSettingsDialog() {
  const dialog = document.getElementById("settings-dialog");
  const threshold = document.getElementById("setting-threshold");
  const windowInput = document.getElementById("setting-window");
  const hint = document.getElementById("settings-hint");
  const save = document.getElementById("settings-save");
  const describe = () => {
    const seconds = Number(windowInput.value) || 0;
    hint.textContent = seconds
      ? `Xử lý khi cùng một lỗi xảy ra ${threshold.value || "?"} lần trong ${seconds >= 120 ? `${Math.round(seconds / 60)} phút` : `${seconds} giây`}. Watcher áp dụng ngay ở lần poll kế tiếp, không cần restart.`
      : "";
  };
  threshold.addEventListener("input", describe);
  windowInput.addEventListener("input", describe);
  document.getElementById("settings-form").addEventListener("submit", (event) => event.preventDefault());
  document.getElementById("settings-cancel").addEventListener("click", () => dialog.close());
  document.getElementById("settings-open").addEventListener("click", async () => {
    try {
      const current = await api("/api/settings");
      const v = current.values;
      document.querySelectorAll('input[name="mode"]').forEach((radio) => { radio.checked = radio.value === v.mode; });
      threshold.value = v.threshold;
      windowInput.value = v.window_seconds;
      describe();
      save.disabled = false;
      dialog.showModal();
    } catch (error) {
      toast(error.status === 404
        ? "Dashboard server đang chạy code cũ: dừng `make ui` (Ctrl-C) rồi chạy lại."
        : `Không tải được cài đặt: ${error.message}`);
    }
  });
  save.addEventListener("click", async () => {
    const mode = document.querySelector('input[name="mode"]:checked');
    save.disabled = true;
    try {
      await api("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-AI-Fix-Request": "1" },
        body: JSON.stringify({ mode: mode && mode.value, threshold: Number(threshold.value), window_seconds: Number(windowInput.value) }),
      });
      dialog.close();
      toast("Đã lưu cài đặt. Watcher sẽ áp dụng ở lần poll kế tiếp.");
      await refresh();
    } catch (error) {
      hint.textContent = `Không lưu được: ${error.message}`;
      save.disabled = false;
    }
  });
}

setupSettingsDialog();
setupClearDialog();
document.getElementById("report-close").addEventListener("click", () => document.getElementById("report-dialog").close());
renderFilters();
refresh();
setInterval(refresh, 2000);
