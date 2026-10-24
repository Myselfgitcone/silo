// The Silo page: send a question to /api/ask, show the answer and how it got there.

const $ = (id) => document.getElementById(id);
let config = null;
let busy = false;

const money = (v) => `$${v < 0.01 ? v.toFixed(4) : v.toFixed(2)}`;

function render(markdown) {
  if (window.marked && window.DOMPurify) return DOMPurify.sanitize(marked.parse(markdown));
  const div = document.createElement("div");
  div.textContent = markdown;
  return div.innerHTML.replace(/\n/g, "<br>");
}

function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children.filter((c) => c !== null && c !== undefined));
  return node;
}

function setBusy(value) {
  busy = value;
  $("ask-button").disabled = value;
  document.querySelectorAll("#presets button").forEach((b) => (b.disabled = value));
}

function updateStatus(spent) {
  if (!config) return;
  if (!config.analyst_online) {
    $("status").textContent = "The analyst is offline right now. The MCP endpoint below still works.";
    return;
  }
  const built = config.lake_built_at ? new Date(config.lake_built_at).toLocaleDateString([], { dateStyle: "medium" }) : "unknown";
  $("status").textContent =
    `Data rebuilt ${built}. Examples are free; your own questions share a ` +
    `${money(config.daily_budget)} daily budget (${money(spent ?? config.spent_today)} used today), ` +
    `${config.asks_per_hour} per hour.`;
}

function showWaiting(question) {
  $("result-section").classList.remove("hidden");
  const started = performance.now();
  const timer = el("span", { textContent: "0s" });
  const box = $("result");
  box.replaceChildren(
    el("p", { className: "asked", textContent: question }),
    el("div", { className: "waiting" }, el("div", { className: "spinner" }),
      el("span", {}, "Querying the lake… ", timer, " (usually 5–10 seconds)")),
  );
  const tick = setInterval(() => (timer.textContent = `${Math.round((performance.now() - started) / 1000)}s`), 500);
  return () => clearInterval(tick);
}

function showError(question, message) {
  $("result").replaceChildren(
    el("p", { className: "asked", textContent: question }),
    el("p", { className: "error", textContent: message }),
  );
}

function showAnswer(data) {
  const answer = el("div", { className: "answer" });
  answer.innerHTML = render(data.text || "_No answer._");

  const steps = el("ol", { className: "steps" });
  for (const step of data.steps) {
    const head = el("div", { className: "step-head" },
      el("b", { textContent: step.tool }),
      el("span", { className: `pill ${step.ok ? "ok" : "no"}`, textContent: step.ok ? "ok" : "refused" }),
      el("span", { className: "step-note", textContent: `${step.ms} ms` }),
      step.ok && step.summary !== step.tool ? el("span", { className: "step-note", textContent: step.summary }) : null,
    );
    const li = el("li", {}, head);
    if (!step.ok) li.append(el("div", { className: "reason", textContent: step.summary }));
    if (step.sql) li.append(el("pre", { textContent: step.sql }));
    steps.append(li);
  }

  const meta = el("div", { className: "meta" },
    data.cached ? el("span", { className: "badge", textContent: "Cached for today's data" }) : null,
    el("span", { textContent: `${data.steps.length} tool call${data.steps.length === 1 ? "" : "s"}` }),
    el("span", { textContent: `${data.tokens_in.toLocaleString()} in / ${data.tokens_out.toLocaleString()} out tokens` }),
    el("span", { textContent: `${money(data.cost_usd)}${data.cached ? " when first answered" : ""}` }),
    el("span", { textContent: `${(data.latency_ms / 1000).toFixed(1)}s` }),
    el("span", { textContent: data.model }),
  );

  $("result").replaceChildren(
    el("p", { className: "asked", textContent: data.question }),
    answer,
    el("div", { className: "trail" }, el("h3", { textContent: "How it got the answer" }), steps, meta),
  );
  updateStatus(data.spent_today);
}

async function ask(body, label) {
  if (busy) return;
  setBusy(true);
  const stop = showWaiting(label);
  try {
    const response = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({ error: `The server returned ${response.status}.` }));
    stop();
    if (!response.ok) showError(label, data.error || `The server returned ${response.status}.`);
    else showAnswer(data);
  } catch {
    stop();
    showError(label, "Couldn't reach the server. Check your connection and try again.");
  } finally {
    setBusy(false);
    $("result-section").scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

function setUpCopy() {
  document.querySelectorAll("[data-copy]").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText($(button.dataset.copy).textContent);
        button.textContent = "Copied";
        setTimeout(() => (button.textContent = "Copy"), 1500);
      } catch {
        button.textContent = "Select and copy";
      }
    });
  });
}

(async () => {
  $("mcp-url").textContent = `${location.origin}/mcp`;
  setUpCopy();
  $("ask-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const question = $("question").value.trim();
    if (question) ask({ question }, question);
  });
  try {
    config = await (await fetch("/api/config")).json();
  } catch {
    $("status").textContent = "Couldn't load the configuration. Reload to try again.";
    return;
  }
  $("grainflow-link").href = config.grainflow_url;
  if (config.portfolio_url) {
    $("hub-link").href = config.portfolio_url;
    $("hub-link").classList.remove("hidden");
  }
  $("presets").replaceChildren(
    ...config.presets.map((p) => {
      const button = el("button", { type: "button", textContent: p.question });
      button.addEventListener("click", () => ask({ preset: p.id }, p.question));
      return button;
    }),
  );
  if (!config.analyst_online) {
    $("ask-button").disabled = true;
    $("presets").classList.add("hidden");
  }
  updateStatus();
})();
