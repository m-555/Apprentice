"""Translate Apprentice's provider catalog to OpenCode without a second model list."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from urllib.parse import urlparse

PROMPT = """You are Apprentice, the user's coding colleague.
Respect the selected mode and the latest request. Ask/explain/review requests do not
authorize edits. Answer greetings directly. Inspect relevant code to explain it.
Give a short, relevant progress sentence before slow work, then a direct final answer.
Keep private reasoning and internal checkpoints out of user-facing text.
Search first; read targeted 80-150 line ranges. Never repeat a truncated whole-file
read. Preserve confirmed findings through compaction. If no new evidence is gained,
change approach or give the answer you can support and name the remaining gap.
For implementation, work only on the requested change. Do not commit, push, weaken
acceptance tests, or modify dependencies without explicit permission.
Inspect only enough code to locate the contract, then make the first useful edit before
extended analysis. Use tools or tests for arithmetic and runtime details instead of
repeatedly calculating them in private reasoning. If the response budget is getting
tight, stop investigating and give a concise, substantive status with the exact next
step; never consume the whole output budget without acting or answering.
Apprentice independently verifies and delivers your changes; never claim that delivery
or checks succeeded before their results exist. Finish with a substantive answer.
The selected role focuses your responsibilities; it does not authorize extra work.
Do not delegate to other agents or switch providers during this task.
"""


def executable(cfg: dict) -> str:
    configured = cfg.get("opencode", {}).get("executable", "opencode")
    if Path(configured).is_absolute():
        if Path(configured).is_file():
            return configured
        raise ValueError(f"OpenCode executable not found: {configured}")
    # npm .cmd shims cannot be spawned safely with shell=False on Windows.
    if os.name == "nt" and configured == "opencode":
        npm = Path(os.environ.get("APPDATA", "")) / "npm/node_modules/opencode-ai/node_modules"
        for variant in ("opencode-windows-x64", "opencode-windows-x64-baseline"):
            candidate = npm / variant / "bin/opencode.exe"
            if candidate.is_file():
                return str(candidate)
    found = shutil.which(configured)
    if found and Path(found).suffix.lower() not in (".cmd", ".bat"):
        return found
    raise ValueError("OpenCode was not found. Install the supported OpenCode CLI or set opencode.executable to its native executable.")


def catalog(cfg: dict) -> list[dict]:
    result = []
    for provider, item in cfg.get("providers", {}).items():
        if not isinstance(item, dict) or not item.get("enabled", False):
            continue
        if item.get("kind") not in ("openai-compatible", "vertex-ai"):
            continue
        models = item.get("models") or {"": item.get("model", "")}
        for alias, model in models.items():
            if not isinstance(model, str) or not model:
                continue
            local = urlparse(item.get("base_url", "")).hostname in ("127.0.0.1", "localhost", "::1")
            context = int(item.get("context_length", 16384 if "deepseek" in model.lower() else 32768))
            output = int(item.get("max_output_tokens", 8192 if local else min(4096, context // 8)))
            if context < 4096 or not 256 <= output < context - 2000:
                raise ValueError(f"Invalid context/output limits for {provider}/{model}.")
            result.append({"provider": provider, "model": alias or model, "model_id": model,
                           "name": item.get("display_name", provider) + " / " + (alias or model),
                           "context": context, "output": output, "local": local})
    return result


def resolve(cfg: dict, provider: str, model: str = "") -> dict:
    entries = [row for row in catalog(cfg) if row["provider"] == provider]
    default = cfg.get("providers", {}).get(provider, {}).get("default_model", "")
    wanted = model or default
    found = next((row for row in entries if wanted in (row["model"], row["model_id"])), None)
    if found:
        return found
    if entries and not wanted:
        return entries[0]
    raise ValueError(f"Unknown or disabled model {provider}/{wanted}. Select a configured model from the catalog.")


def configuration(cfg: dict, selected: dict, mode: str) -> dict:
    item = cfg["providers"][selected["provider"]]
    provider_id = "apprentice-worker"
    opts = {"timeout": int(item.get("timeout_s", 1800)) * 1000}
    if item["kind"] == "openai-compatible":
        npm = "@ai-sdk/openai-compatible"
        opts["baseURL"] = item["base_url"]
        key = item.get("api_key_env", "")
        opts["apiKey"] = os.environ.get(key, "") if key else "local-loopback-only"
        if key and not opts["apiKey"]:
            raise ValueError(f"Missing credential environment variable: {key}")
    else:
        npm = "@ai-sdk/google-vertex"
        opts.update(project=item.get("project", ""), location=item.get("location", "us-central1"))
        if item.get("credentials_file"):
            opts["googleAuthOptions"] = {"keyFilename": item["credentials_file"]}
    permission = {"*": "deny", "read": {"*": "allow", "*.env": "deny", "*.env.*": "deny"}, "glob": "allow", "grep": "allow",
                  "list": "allow", "question": "ask", "doom_loop": "deny",
                  "external_directory": "deny", "edit": "ask" if mode == "build" else "deny",
                  "bash": "ask" if mode == "build" else "deny", "task": "deny"}
    main = {"mode": "primary", "prompt": PROMPT, "steps": int(cfg.get("opencode", {}).get("max_steps", 24)),
            "permission": permission}
    options = {k: v for k, v in item.get("options", {}).items() if not k.startswith("_")}
    for source, target in (("temperature", "temperature"), ("top_p", "top_p")):
        if source in options:
            main[target] = options.pop(source)
    return {"$schema": "https://opencode.ai/config.json", "enabled_providers": [provider_id],
            "model": f"{provider_id}/{selected['model_id']}", "default_agent": "apprentice",
            "share": "disabled", "autoupdate": False,
            "tool_output": {"max_lines": 200, "max_bytes": 10000},
            "compaction": {"auto": True, "prune": True, "preserve_recent_tokens": 2000},
            "permission": permission, "agent": {"apprentice": main},
            "provider": {provider_id: {"npm": npm, "options": opts, "models": {
                selected["model_id"]: {"name": selected["name"], "limit": {
                    "context": selected["context"], "output": selected["output"]},
                    "options": options}}}}}
