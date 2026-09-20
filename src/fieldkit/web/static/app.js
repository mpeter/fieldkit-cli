/* fieldkit web — PWA shell. No build step, no dependencies. */

/* ---------- tiny markdown renderer (headings, lists, tables, emphasis) ---------- */
function esc(s) {
	return s
		.replace(/&/g, "&amp;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;")
		.replace(/"/g, "&quot;")
		.replace(/'/g, "&#39;");
}
function inline(s) {
	return s
		.replace(/`([^`]+)`/g, (_, c) => "<code>" + c + "</code>")
		.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
		.replace(/(^|\W)\*([^*\n]+)\*(?=\W|$)/g, "$1<em>$2</em>")
		.replace(
			/\[([^\]]+)\]\((https?:[^)]+)\)/g,
			'<a href="$2" target="_blank" rel="noopener">$1</a>',
		);
}
function renderMarkdown(md) {
	const lines = md.split("\n");
	const out = [];
	let list = null,
		table = null,
		code = false;
	const closeList = () => {
		if (list) {
			out.push("</" + list + ">");
			list = null;
		}
	};
	const closeTable = () => {
		if (table) {
			out.push("</tbody></table>");
			table = null;
		}
	};
	for (const raw of lines) {
		const line = raw.replace(/\r$/, "");
		if (line.startsWith("```")) {
			closeList();
			closeTable();
			out.push(code ? "</code></pre>" : "<pre><code>");
			code = !code;
			continue;
		}
		if (code) {
			out.push(esc(line));
			continue;
		}
		const l = esc(line);
		if (/^\|.*\|\s*$/.test(line)) {
			const cells = line
				.trim()
				.slice(1, -1)
				.split("|")
				.map((c) => c.trim());
			if (cells.every((c) => /^:?-{3,}:?$/.test(c))) continue; // separator row
			if (!table) {
				table = "thead";
				out.push(
					"<table><thead><tr>" +
						cells.map((c) => "<th>" + inline(esc(c)) + "</th>").join("") +
						"</tr></thead><tbody>",
				);
			} else {
				out.push(
					"<tr>" +
						cells.map((c) => "<td>" + inline(esc(c)) + "</td>").join("") +
						"</tr>",
				);
			}
			continue;
		}
		closeTable();
		const h = line.match(/^(#{1,4})\s+(.*)/);
		if (h) {
			closeList();
			out.push(`<h${h[1].length}>` + inline(esc(h[2])) + `</h${h[1].length}>`);
			continue;
		}
		const li = line.match(/^\s*[-*]\s+(.*)/);
		if (li) {
			if (list !== "ul") {
				closeList();
				out.push("<ul>");
				list = "ul";
			}
			out.push("<li>" + inline(esc(li[1])) + "</li>");
			continue;
		}
		const ol = line.match(/^\s*\d+\.\s+(.*)/);
		if (ol) {
			if (list !== "ol") {
				closeList();
				out.push("<ol>");
				list = "ol";
			}
			out.push("<li>" + inline(esc(ol[1])) + "</li>");
			continue;
		}
		closeList();
		if (line.trim() === "") {
			out.push("");
			continue;
		}
		if (/^-{3,}$/.test(line.trim())) {
			out.push("<hr>");
			continue;
		}
		out.push("<p>" + inline(l) + "</p>");
	}
	closeList();
	closeTable();
	if (code) out.push("</code></pre>");
	return out.join("\n");
}

/* ---------- auth (token mode) ---------- */
function getToken() {
	return localStorage.getItem("fk_token") || "";
}
function askToken() {
	const t = prompt(
		"This fieldkit server requires a token (see: fieldkit web token).",
	);
	if (t && t.trim()) {
		localStorage.setItem("fk_token", t.trim());
		return true;
	}
	return false;
}

/* ---------- api helper ---------- */
async function api(path, opts) {
	const doFetch = () => {
		const headers = Object.assign({}, (opts && opts.headers) || {});
		const tok = getToken();
		if (tok) headers["Authorization"] = "Bearer " + tok;
		return fetch(path, Object.assign({}, opts, { headers }));
	};
	let res = await doFetch();
	if (res.status === 401 && askToken()) res = await doFetch();
	const body = await res.json().catch(() => ({}));
	if (!res.ok) throw new Error(body.error || body.message || res.status + " " + res.statusText);
	return body;
}

/* ---------- tabs ---------- */
document.querySelectorAll("#tabs button").forEach((btn) => {
	btn.addEventListener("click", () => {
		document
			.querySelectorAll("#tabs button")
			.forEach((b) => b.classList.remove("active"));
		document
			.querySelectorAll(".tab")
			.forEach((t) => t.classList.remove("active"));
		btn.classList.add("active");
		document.getElementById("tab-" + btn.dataset.tab).classList.add("active");
	});
});

/* ---------- brief ---------- */
async function loadBrief() {
	const el = document.getElementById("brief-content");
	try {
		const doc = await api("/api/brief");
		el.innerHTML = renderMarkdown(doc.markdown);
	} catch (e) {
		el.innerHTML = '<div class="empty">' + esc(e.message) + "</div>";
	}
}

/* ---------- pipeline ---------- */
function stat(label, value) {
	return `<div class="stat"><div class="label">${esc(label)}</div><div class="value">${esc(String(value))}</div></div>`;
}
function fmtUsd(v) {
	const n = Number(v);
	if (!isFinite(n)) return String(v);
	return (
		"$" + (n >= 1e6 ? (n / 1e6).toFixed(2) + "M" : Math.round(n / 1e3) + "k")
	);
}
async function loadPipeline() {
	const strip = document.getElementById("quota-strip");
	const tableEl = document.getElementById("health-table");
	try {
		const q = (await api("/api/pipeline/quota")).data;
		const entries = [];
		for (const [k, v] of Object.entries(q || {})) {
			if (typeof v === "number")
				entries.push(stat(k.replace(/_/g, " "), fmtUsd(v)));
		}
		strip.innerHTML = entries.slice(0, 4).join("");
	} catch (e) {
		strip.innerHTML = '<div class="empty">quota: ' + esc(e.message) + "</div>";
	}
	try {
		const rows = (await api("/api/pipeline/health")).data || [];
		if (!rows.length) {
			tableEl.innerHTML =
				'<div class="empty">No active pursuits at risk.</div>';
			return;
		}
		tableEl.innerHTML = rows
			.map(
				(r) => `
      <div class="risk-row ${esc(r.risk_tier || "")}">
        <div class="deal">${esc(r.relative_path || "")}</div>
        <div class="meta">${esc(r.stage || "")} · qualification ${esc(r.qualification_status || "unavailable")} · close ${esc(r.close_date_str || "—")}${r.days_in_stage != null ? " · " + r.days_in_stage + "d in stage" : ""}</div>
        ${(r.risk_reasons || []).length ? '<div class="reasons">' + r.risk_reasons.map((x) => "• " + esc(x)).join("<br>") + "</div>" : ""}
      </div>`,
			)
			.join("");
	} catch (e) {
		tableEl.innerHTML = '<div class="empty">' + esc(e.message) + "</div>";
	}
}

/* ---------- alerts ---------- */
async function loadAlerts() {
	const el = document.getElementById("alerts-list");
	try {
		const { alerts } = await api("/api/alerts");
		if (!alerts.length) {
			el.innerHTML = '<div class="empty">No watcher alerts. Quiet day.</div>';
			return;
		}
		el.innerHTML = alerts
			.map(
				(a, i) => `
      <details class="card alert-card" ${i === 0 ? "open" : ""}>
        <summary>${esc(a.name.replace(/-alerts\.md$/, ""))} <span class="meta">${new Date(a.mtime * 1000).toLocaleString()}</span></summary>
        <div class="md">${renderMarkdown(a.markdown)}</div>
      </details>`,
			)
			.join("");
	} catch (e) {
		el.innerHTML = '<div class="empty">' + esc(e.message) + "</div>";
	}
}

/* ---------- Google Tasks ---------- */
let companion = { tier: "read", writes_enabled: false, minimum_write_tier: "propose" };
async function loadCompanion() {
	const el = document.getElementById("companion-tier");
	try {
		companion = await api("/api/companion");
		el.textContent = `Companion tier: ${companion.tier}. Writes require ${companion.minimum_write_tier}.`;
	} catch (e) {
		companion = { tier: "read", writes_enabled: false, minimum_write_tier: "propose" };
		el.textContent = "Companion controls unavailable: " + e.message;
	}
	document.getElementById("task-create").disabled = !companion.writes_enabled;
}
async function loadTasks() {
	const el = document.getElementById("tasks-list");
	try {
		const { tasks } = await api("/api/tasks");
		if (!tasks.length) {
			el.innerHTML = '<div class="empty">No Google Tasks found.</div>';
			return;
		}
		const labels = { today: "Today", active: "Active", other: "Other" };
		el.innerHTML = ["today", "active", "other"]
			.map((section) => {
				const rows = tasks.filter((task) => task.section === section);
				if (!rows.length) return "";
				return `
      <section class="task-group">
        <h2>${esc(labels[section])}</h2>
        ${rows
					.map((task) => {
						const meta = [];
						if (task.due) meta.push("due " + esc(task.due.slice(0, 10)));
						if (task.account) meta.push(esc(task.account));
						if (task.completed) meta.push("completed " + esc(task.completed.slice(0, 10)));
						return `<article class="task-row">
          <div class="task-title">${esc(task.title)}</div>
          <div class="task-meta">${meta.join(" · ")}</div>
		  ${!task.completed ? `<button class="task-complete" data-task-id="${esc(task.id)}" ${companion.writes_enabled ? "" : "disabled"}>Complete</button>` : ""}
        </article>`;
					})
					.join("")}
      </section>`;
			})
			.join("");
		el.querySelectorAll(".task-complete").forEach((button) => button.addEventListener("click", async () => {
			button.disabled = true;
			try {
				const outcome = await api("/api/tasks/" + encodeURIComponent(button.dataset.taskId) + "/complete", { method: "POST" });
				alert(outcome.message);
				await refreshCompanionViews();
			} catch (e) { alert(e.message); button.disabled = !companion.writes_enabled; }
		}));
	} catch (e) {
		el.innerHTML = '<div class="empty">' + esc(e.message) + "</div>";
	}
}

document.getElementById("task-create-form").addEventListener("submit", async (event) => {
	event.preventDefault();
	const button = document.getElementById("task-create");
	button.disabled = true;
	const body = {
		title: document.getElementById("task-title").value,
		section: document.getElementById("task-section").value,
		account: document.getElementById("task-account").value || null,
		due: document.getElementById("task-due").value || null,
	};
	try {
		const outcome = await api("/api/tasks/create", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
		alert(outcome.message);
		document.getElementById("task-title").value = "";
		await refreshCompanionViews();
	} catch (e) { alert(e.message); }
	button.disabled = !companion.writes_enabled;
});

/* ---------- companion feed and outbox ---------- */
async function loadFeed() {
	const el = document.getElementById("feed-list");
	try {
		const { items } = await api("/api/feed");
		const groups = ["critical", "warning", "info"];
		el.innerHTML = groups.map((severity) => {
			const rows = items.filter((item) => item.severity === severity);
			if (!rows.length) return "";
			return `<section class="feed-group"><h2>${esc(severity)}</h2>${rows.map((item) => `<article class="operation ${esc(severity)}"><strong>${esc(String(item.summary || ""))}</strong><div class="meta">${esc(String(item.account || "No account"))} · ${esc(String(item.source || ""))} · ${esc(String(item.observed_at || ""))}</div></article>`).join("")}</section>`;
		}).join("") || '<div class="empty">No companion feed items.</div>';
	} catch (e) { el.innerHTML = '<div class="empty">' + esc(e.message) + "</div>"; }
}

async function loadOutbox() {
	const proposals = document.getElementById("proposals-list");
	try {
		const result = await api("/api/operations");
		if (!result.proposals.length) { proposals.innerHTML = '<div class="empty">No proposal is awaiting review.</div>'; return; }
		proposals.innerHTML = result.proposals.map((proposal) => `<details class="card proposal-card" data-proposal="${esc(proposal.name)}"><summary>${esc(proposal.name)}<span class="meta">${esc(displayTime(proposal.mtime * 1000))}</span></summary><div class="md proposal-content">Tap to load proposal details.</div><div class="proposal-actions">${proposal.approvable ? `<button class="proposal-approve" ${companion.writes_enabled ? "" : "disabled"}>Approve</button>` : `<span>Review only${proposal.validation_error ? ": " + esc(proposal.validation_error) : ""}</span>`}</div></details>`).join("");
		proposals.querySelectorAll(".proposal-card").forEach((card) => {
			card.addEventListener("toggle", async () => {
				if (!card.open || card.dataset.loaded) return;
				const content = card.querySelector(".proposal-content");
				try { const proposal = await api("/api/proposals/" + encodeURIComponent(card.dataset.proposal)); content.innerHTML = renderMarkdown(proposal.markdown); card.dataset.loaded = "true"; }
				catch (e) { content.textContent = "Unable to load proposal: " + e.message; }
			});
			const approve = card.querySelector(".proposal-approve");
			if (approve) approve.addEventListener("click", async (event) => {
				event.preventDefault(); approve.disabled = true;
				try { const outcome = await api("/api/proposals/" + encodeURIComponent(card.dataset.proposal) + "/approve", { method: "POST" }); alert(outcome.message); await refreshCompanionViews(); }
				catch (e) { alert(e.message); approve.disabled = !companion.writes_enabled; }
			});
		});
	} catch (e) { proposals.innerHTML = '<div class="empty">' + esc(e.message) + "</div>"; }
}

async function refreshCompanionViews() {
	await loadCompanion();
	await Promise.all([loadTasks(), loadFeed(), loadOutbox(), loadOperations()]);
}

/* ---------- mother-hen operations ---------- */
function displayTime(value) {
	if (!value) return "No recorded run";
	const date = new Date(value);
	return isNaN(date) ? value : date.toLocaleString();
}
function stageTitle(name) {
	return name
		.replace(/(^|[-_])([a-z])/g, (_, __, c) => " " + c.toUpperCase())
		.trim();
}
async function loadOperations() {
	const action = document.getElementById("next-action");
	const stages = document.getElementById("operations-list");
	try {
		const result = await api("/api/operations");
		action.innerHTML =
			'<div class="eyebrow">Next safe action</div><div>' +
			esc(result.next_action) +
			"</div>";
		stages.innerHTML = result.stages
			.map(
				(stage) => `
      <article class="operation ${esc(stage.status)}">
        <div class="operation-heading">
          <h2>${esc(stageTitle(stage.name))}</h2>
          <span class="status ${esc(stage.status)}">${esc(stage.status)}</span>
        </div>
        <p>${esc(stage.detail)}</p>
        <div class="meta">${esc(displayTime(stage.updated_at))}</div>
        ${stage.action ? '<div class="safe-action">' + esc(stage.action) + "</div>" : ""}
      </article>`,
			)
			.join("");
	} catch (e) {
		action.innerHTML =
			'<div class="eyebrow">Operations unavailable</div><div>' +
			esc(e.message) +
			"</div>";
		stages.innerHTML = "";
	}
}

/* ---------- PR queue ---------- */
const CI_ICON = { passing: "✅", failing: "❌", pending: "🟡", none: "⚪" };
async function loadPRs() {
	const el = document.getElementById("prs-list");
	try {
		const { prs, writes_enabled } = await api("/api/prs");
		if (!prs.length) {
			el.innerHTML = '<div class="empty">No open PRs. Queue is clear.</div>';
			return;
		}
		el.innerHTML = prs
			.map(
				(p) => `
      <div class="card pr-row" data-pr="${p.number}">
        <div class="deal">
          <a href="${esc(p.url)}" target="_blank" rel="noopener">#${p.number}</a>
          ${esc(p.title)} ${p.is_draft ? '<span class="meta">(draft)</span>' : ""}
        </div>
        <div class="meta">${CI_ICON[p.ci.state] || "⚪"} CI ${esc(p.ci.state)} (${p.ci.passed}✓ ${p.ci.failed}✗ ${p.ci.pending}…) · ${esc(p.author)} · ${esc(p.branch)}</div>
        <div class="pr-actions">
          ${
						writes_enabled
							? `
            <button class="pr-merge" ${p.ci.state !== "passing" || p.is_draft ? "disabled" : ""}>Merge</button>
            <button class="pr-bounce">Bounce…</button>`
							: '<span class="meta">writes disabled (serve with a token)</span>'
					}
        </div>
      </div>`,
			)
			.join("");
		el.querySelectorAll(".pr-merge").forEach((btn) =>
			btn.addEventListener("click", async () => {
				const n = btn.closest(".pr-row").dataset.pr;
				if (!confirm("Squash-merge PR #" + n + "?")) return;
				btn.disabled = true;
				btn.textContent = "Merging…";
				try {
					await api("/api/prs/" + n + "/merge", { method: "POST" });
					loadPRs();
				} catch (e) {
					alert(e.message);
					btn.disabled = false;
					btn.textContent = "Merge";
				}
			}),
		);
		el.querySelectorAll(".pr-bounce").forEach((btn) =>
			btn.addEventListener("click", async () => {
				const n = btn.closest(".pr-row").dataset.pr;
				const body = prompt("Comment to post on PR #" + n + ":");
				if (!body) return;
				try {
					await api("/api/prs/" + n + "/comment", {
						method: "POST",
						headers: { "Content-Type": "application/json" },
						body: JSON.stringify({ body }),
					});
					alert("Comment posted.");
				} catch (e) {
					alert(e.message);
				}
			}),
		);
	} catch (e) {
		el.innerHTML = '<div class="empty">' + esc(e.message) + "</div>";
	}
}

/* ---------- chat ---------- */
const chatHistory = [];
const MAX_CHAT_HISTORY_TURN_CHARS = 2000;

function chatHistoryPayload() {
	return chatHistory.slice(-6).map((turn) => ({
		...turn,
		content: turn.content.slice(0, MAX_CHAT_HISTORY_TURN_CHARS),
	}));
}

function pushMsg(cls, text) {
	const log = document.getElementById("chat-log");
	const div = document.createElement("div");
	div.className = "msg " + cls;
	div.innerHTML = cls === "bot" ? renderMarkdown(text) : esc(text);
	log.appendChild(div);
	div.scrollIntoView({ behavior: "smooth", block: "end" });
}
document.getElementById("chat-form").addEventListener("submit", async (e) => {
	e.preventDefault();
	const input = document.getElementById("chat-input");
	const msg = input.value.trim();
	if (!msg) return;
	input.value = "";
	pushMsg("user", msg);
	pushMsg("bot", "…");
	const placeholder = document.querySelector("#chat-log .msg:last-child");
	try {
		const { reply } = await api("/api/chat", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ message: msg, history: chatHistoryPayload() }),
		});
		placeholder.innerHTML = renderMarkdown(reply);
		chatHistory.push(
			{ role: "user", content: msg },
			{ role: "assistant", content: reply },
		);
	} catch (err) {
		placeholder.className = "msg err";
		placeholder.textContent = err.message;
	}
});

/* ---------- live events ---------- */
async function connectEvents() {
	const dot = document.getElementById("conn");
	try {
		const headers = {};
		const tok = getToken();
		if (tok) headers.Authorization = "Bearer " + tok;
		const response = await fetch("/events", { headers });
		if (!response.ok || !response.body) throw new Error("event stream unavailable");
		const reader = response.body.getReader();
		const decoder = new TextDecoder();
		let pending = "";
		for (;;) {
			const { done, value } = await reader.read();
			if (done) throw new Error("event stream closed");
			pending += decoder.decode(value, { stream: true });
			const frames = pending.split("\n\n");
			pending = frames.pop();
			frames.forEach((frame) => {
				if (frame.includes("event: hello")) dot.classList.add("live");
				if (!frame.includes("event: alert")) return;
				loadAlerts();
				loadOperations();
				const data = frame.split("data: ", 2)[1];
				const { name } = JSON.parse(data);
				if (Notification.permission === "granted") {
					new Notification("fieldkit alert", { body: name.replace(/-alerts\.md$/, "") + " updated" });
				}
			});
		}
	} catch (_error) {
		dot.classList.remove("live");
		setTimeout(connectEvents, 5000);
	}
}

/* ---------- boot ---------- */
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");
if ("Notification" in window && Notification.permission === "default") {
	// Ask once, on first interaction, not on load.
	document.body.addEventListener(
		"click",
		() => Notification.requestPermission(),
		{ once: true },
	);
}
loadCompanion().then(() => Promise.all([loadOperations(), loadFeed(), loadOutbox(), loadTasks()]));
loadBrief();
loadPipeline();
loadAlerts();
loadPRs();
connectEvents();
