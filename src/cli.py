"""Apprentice command-line interface (`apprentice` after pip/pipx install).

Commands:
  apprentice init        set up the data home: create dirs, seed default config,
                         check Ollama, and print the MCP registration command
  apprentice chat        interactive coding agent in the current repo (see below)
  apprentice run         headless agent: work a task until a done_when command passes
  apprentice serve       run the MCP stdio server (what your orchestrator spawns)
  apprentice doctor      environment checks (config, Ollama, models, optional extras)
  apprentice report [N]  metering report over the last N events (default 50)
  apprentice reindex     rebuild the retrieval index from corrections.jsonl
  apprentice sessions    list recent agent sessions (resume with chat --resume <id>)

Agent options (chat/run):
  --repo PATH        target repository (default: current directory)
  --provider NAME    qwen (local, default) | gemini | openai | any configured provider
  --model TIER       provider model/tier override (e.g. flash, pro)
  --verify MODE      off | gate | tests   (default: config agent_chat.verify)
  --test-cmd CMD     the project's test command (else .qwen-pipeline.json / config)
  --yes              don't prompt before non-allowlisted shell commands
  --allow-dirty      allow an uncommitted/non-git working tree
  --resume ID        continue a saved chat session
  --done-when CMD    (run only) the acceptance command that must exit 0

Non-interactive by design: `init` is idempotent and prints what it did/found, so it
works the same in a terminal, a script, or CI.
"""

from __future__ import annotations

import json
import shutil
import sys
import urllib.request
from pathlib import Path

try:
    from . import paths
except ImportError:
    import paths


def _ollama_status(cfg: dict) -> tuple[bool, str]:
    host = cfg.get("runner", {}).get("host", "http://127.0.0.1:11434")
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=5) as resp:
            tags = json.load(resp)
        names = [m.get("name", "") for m in tags.get("models", [])]
        worker = cfg.get("worker_model", {}).get("tag", "qwen3-coder-next:latest")
        lines = [f"Ollama reachable at {host} ({len(names)} model(s) pulled)."]
        if not any(n == worker or n.split(":")[0] == worker.split(":")[0] for n in names):
            lines.append(f"  worker model '{worker}' NOT pulled yet -> ollama pull "
                         f"{worker.split(':')[0]}")
        if not any("embed" in n for n in names):
            lines.append("  no embedding model found -> ollama pull nomic-embed-text "
                         "(needed for retrieval; delegation works without it)")
        return True, "\n".join(lines)
    except Exception as exc:
        return False, (f"Ollama NOT reachable at {host} ({exc}). Install/start it "
                       f"(https://ollama.com), or configure a cloud provider instead.")


def _seed_config(home: Path) -> list[str]:
    """Create the data dirs and seed config files (never overwrites). Returns notes."""
    notes = []
    for sub in ("config", "corrections", "outputs", "metrics"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    # Default config ships inside the wheel (default_config/); a checkout already has
    # config/qwen.json so seeding is skipped there.
    pairs = [("qwen.json", "qwen.json"), ("qwen.local.example.json", "qwen.local.example.json")]
    for src_name, dst_name in pairs:
        dst = home / "config" / dst_name
        src = paths.DEFAULT_CONFIG_DIR / src_name
        if dst.exists():
            notes.append(f"kept existing {dst}")
        elif src.exists():
            shutil.copy(src, dst)
            notes.append(f"seeded {dst}")
        else:
            notes.append(f"no default for {dst_name} (running from a checkout — fine)")
    local = home / "config" / "qwen.local.json"
    if not local.exists():
        local.write_text(json.dumps({
            "_comment": ("Machine-local overrides + secrets (deep-merged over qwen.json). "
                         "Never commit this file. See qwen.local.example.json for "
                         "provider/credential examples."),
        }, indent=2) + "\n", encoding="utf-8")
        notes.append(f"created {local}")
    return notes


def cmd_init(home: Path | None = None, check_ollama: bool = True) -> int:
    home = home or paths.ROOT
    print(f"Apprentice data home: {home}")
    for note in _seed_config(home):
        print(f"  {note}")
    cfg = paths.load_config()
    if check_ollama:
        _ok, msg = _ollama_status(cfg)
        print(msg)
    exe = "apprentice" if shutil.which("apprentice") else f"{sys.executable} -m apprentice.cli"
    print("\nRegister the MCP server with your orchestrator (Claude Code example):")
    print(f"  claude mcp add --scope local qwen -- {exe} serve")
    print("\nOptional extras:")
    print("  pip install 'apprentice-pipeline[gemini]'   # Gemini/Vertex provider")
    print("  (assign/Aider goes in its OWN venv — see README 'assign' section)")
    return 0


def cmd_doctor() -> int:
    cfg = paths.load_config()
    ok = True
    print(f"data home : {paths.ROOT}")
    print(f"config    : {'OK' if paths.CONFIG_PATH.exists() else 'MISSING (run: apprentice init)'}")
    o_ok, msg = _ollama_status(cfg)
    ok = ok and o_ok
    print(f"ollama    : {msg}")
    try:
        import google.genai  # type: ignore  # noqa: F401
        print("gemini    : google-genai installed")
    except ImportError:
        print("gemini    : google-genai not installed (optional — [gemini] extra)")
    aider = cfg.get("agent", {}).get("aider_exe", "aider")
    print(f"aider     : {'found' if shutil.which(aider) else 'not found (optional — only for assign)'}"
          f" ({aider})")
    enabled = [n for n, p in cfg.get("providers", {}).items()
               if isinstance(p, dict) and p.get("enabled")]
    print(f"providers : enabled = {', '.join(enabled) or '(none — run apprentice init and check config)'}")
    return 0 if ok else 1


def cmd_serve() -> int:
    try:
        from . import server
    except ImportError:
        import server
    server.mcp.run()
    return 0


def cmd_report(n: int = 50) -> int:
    try:
        from . import metering
    except ImportError:
        import metering
    print(metering.report(n))
    return 0


def cmd_reindex() -> int:
    try:
        from . import retrieval
    except ImportError:
        import retrieval
    n = retrieval.reindex(paths.load_config())
    print(f"reindexed {n} record(s) -> {retrieval._INDEX_PATH}")
    return 0


def _agent_parser(prog: str, headless: bool) -> "argparse.ArgumentParser":
    import argparse
    p = argparse.ArgumentParser(prog=f"apprentice {prog}", add_help=True)
    if headless:
        p.add_argument("task", help="what the agent should do")
        p.add_argument("--done-when", required=True,
                       help="command that must exit 0 for the task to count as done")
    p.add_argument("--repo", default=".")
    p.add_argument("--provider", default="")
    p.add_argument("--model", default="")
    p.add_argument("--verify", default="", choices=["", "off", "gate", "tests"])
    p.add_argument("--test-cmd", dest="test_cmd", default="")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--allow-dirty", dest="allow_dirty", action="store_true")
    if not headless:
        p.add_argument("--resume", default="")
    return p


def cmd_chat(argv: list[str]) -> int:
    try:
        from . import chat_ui
    except ImportError:
        import chat_ui
    args = _agent_parser("chat", headless=False).parse_args(argv)
    cfg = paths.load_config()
    provider = args.provider or cfg.get("providers", {}).get("default", "qwen")
    return chat_ui.chat(args.repo, cfg, provider, args.model, args.verify,
                        args.test_cmd, args.yes, args.allow_dirty, args.resume)


def cmd_run(argv: list[str]) -> int:
    try:
        from . import chat_ui
    except ImportError:
        import chat_ui
    args = _agent_parser("run", headless=True).parse_args(argv)
    cfg = paths.load_config()
    provider = args.provider or cfg.get("providers", {}).get("default", "qwen")
    return chat_ui.run_headless(args.repo, cfg, args.task, args.done_when, provider,
                                args.model, args.verify, args.test_cmd)


def cmd_sessions() -> int:
    try:
        from . import session as session_mod
    except ImportError:
        import session as session_mod
    rows = session_mod.Session.list_recent(15)
    if not rows:
        print("No agent sessions yet. Start one: apprentice chat")
        return 0
    for r in rows:
        print(f"  {r['id']}  {r['provider']:<8} {r['repo']}")
        if r["first_task"]:
            print(f"            {r['first_task']}")
    print("\nResume: apprentice chat --resume <id>")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "help"
    if cmd == "init":
        return cmd_init()
    if cmd == "serve":
        return cmd_serve()
    if cmd == "doctor":
        return cmd_doctor()
    if cmd == "chat":
        return cmd_chat(argv[1:])
    if cmd == "run":
        return cmd_run(argv[1:])
    if cmd == "sessions":
        return cmd_sessions()
    if cmd == "report":
        return cmd_report(int(argv[1]) if len(argv) > 1 else 50)
    if cmd == "reindex":
        return cmd_reindex()
    print(__doc__.strip())
    return 0 if cmd in ("help", "-h", "--help") else 2


if __name__ == "__main__":
    sys.exit(main())
