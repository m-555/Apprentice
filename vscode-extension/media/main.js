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

  let working = null;

  function renderHeader(m) {
    if (m.provider !== undefined) {
      hdrProvider.textContent = m.model ? `${m.provider} / ${m.model}` : m.provider;
    }
    if (m.verify !== undefined) {
      hdrVerify.textContent = m.verify ? `verify: ${m.verify}` : "";
      // Make the risky mode visually obvious: with verification off nothing is checked.
      hdrVerify.className = "pill " + (m.verify === "off" ? "loose" : "strict");
      hdrVerify.title =
        m.verify === "tests" ? "Changes must pass this project's tests, or they're reverted"
        : m.verify === "gate" ? "Changes must compile/lint, or they're reverted"
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
    if (on && !working) {
      working = el("div", "working");
      working.appendChild(el("span", "dot"));
      working.appendChild(el("span", null, "working…"));
      add(working);
    } else if (!on && working) {
      working.remove();
      working = null;
    }
    sendBtn.disabled = !!on;
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
      vscode.postMessage({ type: "confirm", allow: ok });
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
      chip.title = "Open diff against HEAD";
      chip.onclick = () => vscode.postMessage({ type: "openDiff", path: f });
      changedTray.appendChild(chip);
    }
  }

  // The reply streams in as many text_delta events, then arrives once more complete as
  // `text`. We append deltas into one live bubble and drop the duplicate `text`.
  let streamBubble = null;

  function renderEvent(ev) {
    switch (ev.type) {
      case "user":
        add(el("div", "msg user", ev.text));
        break;
      case "text_delta": {
        setWorking(false);
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
        add(el("div", "badge ok", "✓ verified (" + (ev.check || "") + ")"));
        break;
      case "verify_failed": {
        add(el("div", "badge fail",
               "✗ verification failed (" + (ev.check || "") + ") — change reverted"));
        if (ev.text) add(el("div", "tool-output", ev.text));
        break;
      }
      case "escalated":
        add(el("div", "badge info", "⇧ " + (ev.text || "escalated")));
        break;
      case "confirm_request":
        setWorking(false);
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
        renderChanged([]);
        setWorking(false);
        break;
      case "notice":
        add(el("div", "notice", msg.text));
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
    streamBubble = null;
    vscode.postMessage({ type: "user", text });
    input.value = "";
    setWorking(true);
  }

  sendBtn.onclick = send;
  input.addEventListener("keydown", (e) => {
    // Enter sends; Shift+Enter is a newline (the convention people expect in chat UIs).
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });

  vscode.postMessage({ type: "ready" });
})();
