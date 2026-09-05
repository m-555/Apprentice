// Webview UI. Renders protocol events; owns no logic beyond presentation.
// All text goes through textContent — never innerHTML — so file contents and model
// output can never inject markup into the panel.
(function () {
  const vscode = acquireVsCodeApi();
  const log = document.getElementById("log");
  const input = document.getElementById("input");
  const sendBtn = document.getElementById("send");
  const changedTray = document.getElementById("changed");
  const hdrProvider = document.getElementById("hdr-provider");
  const hdrVerify = document.getElementById("hdr-verify");
  const hdrSession = document.getElementById("hdr-session");
  const modelSelect = document.getElementById("model-select");
  const modeSelect = document.getElementById("mode-select");
  const roleSelect = document.getElementById("role-select");
  const parts = new Map();
  let active = false;
  let selection = {};
  const draft = vscode.getState();
  input.value = draft?.draft || "";

  let working = null;

  function renderHeader(m) {
    selection = { ...selection, ...Object.fromEntries(Object.entries(m).filter(([, value]) => value !== undefined)) };
    modelSelect.value = JSON.stringify([selection.provider, selection.model]);
    if (selection.mode) modeSelect.value = selection.mode;
    if (selection.role) roleSelect.value = selection.role;
    if (m.provider !== undefined) {
      hdrProvider.textContent = m.model ? `${m.provider} / ${m.model}` : m.provider;
    }
    if (m.verify !== undefined) {
      hdrVerify.textContent = m.verify ? `verify: ${m.verify}` : "";
      // Make the risky mode visually obvious: with verification off nothing is checked.
      hdrVerify.className = "pill " + (m.verify === "off" ? "loose" : "strict");
      hdrVerify.title =
        m.verify === "tests" ? "Candidate changes must pass the configured project check before delivery"
        : m.verify === "gate" ? "Supported edited files must pass the configured compile/lint gate before delivery"
        : "Verification is OFF — edits land unchecked";
    }
    if (m.session !== undefined) {
      hdrSession.textContent = m.session ? `session ${m.session}` : "";
    }
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function renderText(node, text) {
    // Conservative formatting: code blocks and inline code; raw HTML stays text.
    node.textContent = "";
    const chunks = String(text || "").split(/(```[^\n]*\n[\s\S]*?(?:```|$))/g);
    for (const chunk of chunks) {
      if (chunk.startsWith("```")) {
        const newline = chunk.indexOf("\n");
        const code = chunk.slice(newline + 1).replace(/```$/, "");
        const block = el("div", "code-block");
        const copy = el("button", "secondary copy-code", "Copy code");
        copy.onclick = () => vscode.postMessage({ type: "copy", text: code });
        block.appendChild(copy);
        block.appendChild(el("pre", null, code));
        node.appendChild(block);
      } else {
        for (const segment of chunk.split(/(`[^`\n]+`)/g)) {
          node.appendChild(segment.startsWith("`") && segment.endsWith("`")
            ? el("code", null, segment.slice(1, -1)) : document.createTextNode(segment));
        }
      }
    }
  }

  function atBottom() {
    return log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  }

  function add(node) {
    const stick = atBottom();
    log.appendChild(node);
    if (stick) log.scrollTop = log.scrollHeight;
    return node;
  }

  function setWorking(on) {
    active = on;
    modelSelect.disabled = modeSelect.disabled = roleSelect.disabled = on;
    if (on && !working) {
      working = el("div", "working");
      working.appendChild(el("span", "dot"));
      working.appendChild(el("span", null, "working…"));
      add(working);
    } else if (!on && working) {
      working.remove();
      working = null;
    }
    // Sending mid-turn queues another request. Stop clears that queue.
    sendBtn.textContent = on ? "Queue message" : "Send";
  }

  function addTool(ev) {
    const row = el("div", "tool");
    row.appendChild(el("span", "chev", "▸"));
    row.appendChild(el("span", "name", ev.tool || "tool"));
    const detail = toolDetail(ev);
    if (detail) row.appendChild(el("span", "detail", detail));
    add(row);
    return row;
  }

  function toolDetail(ev) {
    const args = ev.args || {};
    for (const key of ["path", "cmd", "pattern", "dir", "summary"]) {
      if (typeof args[key] === "string" && args[key]) return args[key];
    }
    return "";
  }

  // Tool results are collapsed by default: the interesting thing is usually WHICH tool
  // ran, not its full output. Clicking the row expands it.
  let lastToolRow = null;
  function addToolResult(ev) {
    const body = el("div", "tool-output hidden", ev.text || "(no output)");
    add(body);
    const row = lastToolRow;
    if (row) {
      row.onclick = () => {
        body.classList.toggle("hidden");
        row.firstChild.textContent = body.classList.contains("hidden") ? "▸" : "▾";
      };
    }
  }

  function addConfirm(ev, opts) {
    const o = opts || {};
    const box = el("div", "confirm");
    box.appendChild(el("div", null, o.title || "The agent wants to run a command:"));
    box.appendChild(el("div", "cmd", o.body !== undefined ? o.body : (ev.detail || "")));
    const row = el("div", "row");
    const allow = el("button", null, o.allowLabel || "Allow");
    const deny = el("button", "secondary", o.denyLabel || "Deny");
    const answer = (ok) => {
      vscode.postMessage({ type: "confirm", request_id: ev.request_id, allow: ok });
      row.remove();
      box.appendChild(el("div", "resolved",
                          ok ? (o.yes || "Allowed.") : (o.no || "Denied.")));
    };
    allow.onclick = () => answer(true);
    deny.onclick = () => answer(false);
    row.appendChild(allow);
    row.appendChild(deny);
    box.appendChild(row);
    add(box);
    if (ev.request_id) box.dataset.requestId = ev.request_id;
  }

  function renderChanged(files) {
    changedTray.textContent = "";
    if (!files || files.length === 0) {
      changedTray.classList.add("hidden");
      return;
    }
    changedTray.classList.remove("hidden");
    changedTray.appendChild(el("span", "label", "Changed:"));
    for (const f of files) {
      const chip = el("span", "chip", f);
      chip.title = "Open this task's before/after diff";
      chip.onclick = () => vscode.postMessage({ type: "openDiff", path: f });
      changedTray.appendChild(chip);
    }
  }

  // The reply streams in as many text_delta events, then arrives once more complete as
  // `text`. We append deltas into one live bubble and drop the duplicate `text`.
  let streamBubble = null;

  // A resumed session replays what was said before. Without this the panel came up
  // blank on resume — the agent had the whole conversation, the UI had nothing to draw.
  function renderHistory(items) {
    if (!items || !items.length) return;
    add(el("div", "badge info", "— earlier in this session —"));
    for (const m of items) {
      if (m.role === "user") {
        add(el("div", "msg user", m.text || ""));
      } else if (m.role === "assistant") {
        if (m.text) add(el("div", "msg assistant", m.text));
        for (const t of m.tools || []) add(el("div", "tool", t));
      } else if (m.role === "tool") {
        add(el("div", "tool-output", m.text || "(no output)"));
      }
    }
    add(el("div", "badge info", "— resumed here —"));
    log.scrollTop = log.scrollHeight;
  }

  function renderEvent(ev) {
    switch (ev.type) {
      case "session_start":
        renderHeader({ provider: ev.provider, model: ev.model, verify: ev.verify, session: ev.session_id });
        if (["ask", "plan", "build"].includes(ev.mode)) modeSelect.value = ev.mode;
        if (ev.role) roleSelect.value = ev.role;
        break;
      case "catalog":
        modelSelect.textContent = "";
        for (const model of ev.models) {
          const option = el("option", null, model.name);
          option.value = JSON.stringify([model.provider, model.model]);
          modelSelect.appendChild(option);
        }
        renderHeader({});
        break;
      case "message_part": {
        let node = parts.get(ev.id);
        if (!node) {
          node = add(el("div", "msg assistant", ""));
          node.dataset.messageId = ev.message_id;
          parts.set(ev.id, node);
        }
        const stick = atBottom();
        renderText(node, ev.text);
        if (stick) log.scrollTop = log.scrollHeight;
        break;
      }
      case "tool_part": {
        let node = parts.get(ev.id);
        if (!node) {
          node = el("details", "tool-card");
          node.appendChild(el("summary"));
          node.appendChild(el("pre", "tool-output"));
          node.dataset.messageId = ev.message_id;
          parts.set(ev.id, node);
          add(node);
        }
        node.firstChild.textContent = `${ev.tool} · ${ev.status}`;
        node.lastChild.textContent = ev.text || JSON.stringify(ev.args || {}, null, 2);
        break;
      }
      case "message_remove":
        for (const [id, node] of parts) if (node.dataset.messageId === ev.message_id) { node.remove(); parts.delete(id); }
        break;
      case "part_remove":
        parts.get(ev.id)?.remove(); parts.delete(ev.id);
        break;
      case "history_v2": {
        const wasActive = active;
        log.textContent = ""; parts.clear(); working = null; streamBubble = null;
        for (const item of ev.events || []) renderEvent(item);
        setWorking(wasActive);
        break;
      }
      case "task_status":
        setWorking(true);
        if (working) {
          const label = { preparing: "Preparing isolated workspace", "baseline-check": "Checking the starting code", working: "Working", verifying: "Running your checks", "awaiting-model": "Waiting for the model", stopping: "Stopping and cleaning up" }[ev.status] || ev.status;
          working.lastChild.textContent = `${label}${ev.elapsed_s ? ` · ${ev.elapsed_s}s elapsed` : ""}${ev.attempt ? ` · attempt ${ev.attempt}` : ""}…`;
        }
        break;
      case "approval_resolved":
        for (const box of log.querySelectorAll(".confirm")) if (box.dataset.requestId === ev.request_id) {
          for (const button of box.querySelectorAll("button")) button.disabled = true;
        }
        break;
      case "notice":
        add(el("div", "notice", ev.text));
        break;
      case "turn_end":
        setWorking(false);
        break;
      case "user":
        add(el("div", "msg user", ev.text));
        break;
      case "history":
        renderHistory(ev.messages);
        break;
      case "text_delta": {
        if (!streamBubble) streamBubble = add(el("div", "msg assistant", ""));
        const stick = atBottom();
        streamBubble.textContent += ev.text;
        if (stick) log.scrollTop = log.scrollHeight;
        break;
      }
      case "text":
        if (streamBubble) {
          // Already shown live — replace with the canonical text and stop appending.
          streamBubble.textContent = ev.text || streamBubble.textContent;
          streamBubble = null;
        } else if (ev.text) {
          add(el("div", "msg assistant", ev.text));
        }
        break;
      case "tool_call":
        lastToolRow = addTool(ev);
        break;
      case "tool_result":
        addToolResult(ev);
        break;
      case "verify_passed":
        add(el("div", ["not checked", "no applicable checks", "none"].includes(ev.check) ? "badge info" : "badge ok", "Checks: " + (ev.check || "not reported")));
        break;
      case "verify_failed": {
        add(el("div", "badge fail",
               "✗ verification failed (" + (ev.check || "") + ") — " + (ev.delivery === "not applied" ? "not applied to your files" : "change reverted")));
        if (ev.text) add(el("div", "tool-output", ev.text));
        break;
      }
      case "escalated":
        add(el("div", "badge info", "⇧ " + (ev.text || "escalated")));
        break;
      case "confirm_request":
        if (working) working.lastChild.textContent = "Waiting for your approval…";
        addConfirm(ev);
        break;
      case "escalation_offer":
        // The agent is stuck and wants a stronger (paid) model — the user decides.
        setWorking(false);
        addConfirm(ev, { title: "Switch to a stronger model?", body: ev.text || "",
                         allowLabel: "Switch", denyLabel: "Stay",
                         yes: "Switching.", no: "Staying on the current model." });
        break;
      case "ask":
        setWorking(false);
        addConfirm(ev, { title: ev.question || "Proceed?", body: ev.detail || "",
                         allowLabel: "Yes", denyLabel: "No",
                         yes: "Approved.", no: "Declined." });
        break;
      case "steered":
        add(el("div", "badge info", "-> redirected: " + (ev.text || "")));
        break;
      case "nudge":
        add(el("div", "badge info", "! " + (ev.text || "repeating") +
                                    " - told the model to change approach"));
        break;
      case "confirm_auto":
        add(el("div", "tool", "auto-approved: " + (ev.detail || "")));
        break;
      case "stopped":
      case "error":
        setWorking(false);
        add(el("div", "notice", ev.text || ev.type));
        break;
      case "ack":
        // Slash commands are answered with `ack` and produce NO turn_end, so the
        // busy indicator must be cleared here or it would spin forever.
        setWorking(false);
        if (ev.mode) modeSelect.value = ev.mode;
        if (ev.role) roleSelect.value = ev.role;
        if (ev.provider !== undefined) renderHeader(ev);
        if (ev.reverted && ev.reverted.length) {
          add(el("div", "notice", "Reverted: " + ev.reverted.join(", ")));
        } else if (ev.usage) {
          add(el("div", "notice",
                 `turns=${ev.usage.turns} tokens ${ev.usage.tokens_in}/${ev.usage.tokens_out} ` +
                 `≈$${(ev.usage.est_cost_usd || 0).toFixed(4)}`));
        }
        break;
      case "session_end":
        setWorking(false);
        if (ev.done_passed !== undefined) {
          add(el("div", ev.done_passed ? "badge ok" : "badge fail",
                 ev.done_passed ? "task complete" : "task did not pass"));
        }
        break;
    }
  }

  window.addEventListener("message", (e) => {
    const msg = e.data;
    switch (msg.type) {
      case "event":
        renderEvent(msg.event);
        break;
      case "busy":
        setWorking(msg.busy);
        break;
      case "changed":
        renderChanged(msg.files);
        break;
      case "clear":
        log.textContent = "";
        streamBubble = null;
        parts.clear(); working = null;
        renderChanged([]);
        setWorking(false);
        break;
      case "notice":
        add(el("div", "notice", msg.text));
        break;
      case "steering":
        // Sent mid-turn: shown as the user's message, but it redirects the running
        // task rather than starting a new one.
        add(el("div", "msg user", msg.text));
        add(el("div", "notice", "sent to the running task"));
        break;
      case "header":
        renderHeader(msg);
        break;
      case "exit":
        setWorking(false);
        streamBubble = null;
        add(el("div", msg.crashed ? "badge fail" : "notice",
               msg.text || "Agent session ended."));
        renderHeader({ session: "" });
        break;
    }
  });

  function send() {
    const text = input.value.trim();
    if (!text) return;
    const steering = !!working;      // mid-turn -> this redirects, not a new turn
    if (!steering) {
      streamBubble = null;
      setWorking(true);
    }
    vscode.postMessage({ type: "user", text });
    input.value = "";
    vscode.setState({ draft: "" });
  }

  sendBtn.onclick = send;
  document.getElementById("stop").onclick = () => vscode.postMessage({ type: "stop" });
  input.addEventListener("input", () => vscode.setState({ draft: input.value }));
  modelSelect.onchange = () => {
    const [provider, model] = JSON.parse(modelSelect.value);
    vscode.postMessage({ type: "select", provider, model });
  };
  modeSelect.onchange = () => vscode.postMessage({ type: "select", mode: modeSelect.value });
  roleSelect.onchange = () => vscode.postMessage({ type: "select", role: roleSelect.value });
  input.addEventListener("keydown", (e) => {
    // Enter sends; Shift+Enter is a newline (the convention people expect in chat UIs).
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      send();
    }
  });

  vscode.postMessage({ type: "ready" });
})();
