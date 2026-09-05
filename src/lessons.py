"""Repository-scoped, inspectable task memory; no embedding-model side effects."""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path
try:
    from . import paths
except ImportError:
    import paths


def records(repo: str = "") -> list[dict]:
    source = paths.CORRECTIONS_PATH
    if not source.exists():
        return []
    disabled = set()
    policy = source.with_name("disabled.jsonl")
    if policy.exists():
        for line in policy.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            if item["disabled"]:
                disabled.add(item["id"])
            else:
                disabled.discard(item["id"])
    result = []
    for line in source.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not item.get("repo") or (repo and Path(item["repo"]).resolve() != Path(repo).resolve()):
            continue
        key = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()[:16]
        result.append({**item, "id": key, "disabled": key in disabled})
    return result


def select(task: str, repo: str, cfg: dict) -> list[dict]:
    if not cfg.get("retrieval", {}).get("enabled", True):
        return []
    terms = set(re.findall(r"[a-zA-Z_][a-zA-Z_0-9]{3,}", task.lower())) - {
        "this", "that", "with", "from", "please", "code", "file", "make", "have", "what", "tell", "about"}
    if not terms:
        return []
    candidates = []
    for item in records(repo):
        if item["disabled"]:
            continue
        words = set(re.findall(r"[a-zA-Z_][a-zA-Z_0-9]{3,}", item.get("task", "").lower()))
        score = len(terms & words) / len(terms | words) if terms | words else 0
        if len(terms & words) >= min(2, len(terms)) and score >= .2:
            candidates.append((score, item))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in candidates[:int(cfg.get("retrieval", {}).get("top_k", 5))]]


def set_disabled(key: str, disabled: bool):
    if not any(item["id"] == key for item in records()):
        raise ValueError("Unknown repository-scoped lesson ID.")
    policy = paths.CORRECTIONS_PATH.with_name("disabled.jsonl")
    with policy.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"id": key, "disabled": disabled}) + "\n")


def cli(argv: list[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="apprentice lessons")
    parser.add_argument("action", choices=("list", "show", "disable", "enable"), nargs="?", default="list")
    parser.add_argument("id", nargs="?", default="")
    parser.add_argument("--repo", default="")
    args = parser.parse_args(argv)
    if args.action in ("disable", "enable"):
        set_disabled(args.id, args.action == "disable")
        print(f"Lesson {args.id}: {args.action}d (original evidence retained).")
    else:
        for item in records(args.repo):
            if args.action == "show":
                if item["id"] == args.id:
                    print(json.dumps(item, indent=2, ensure_ascii=False))
            else:
                print(f"{item['id']}  {'disabled' if item['disabled'] else 'active'}  {item.get('repo')}  {item.get('task', '')[:100]}")
    return 0
