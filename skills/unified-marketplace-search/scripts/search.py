#!/usr/bin/env python3
"""Search four skill marketplaces for one query or a QUERY_PACK."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

SOURCES = ("lobehub", "skillhub", "skills-sh", "awesome-top-skills")
HOME = Path.home()
AWESOME_ROOTS = (
    Path(__file__).resolve().parents[4] / "skill-manager" / "awesome-top-skills",
    Path(__file__).resolve().parents[3] / "awesome-top-skills",
    HOME / ".skills-manager" / "skills" / "awesome-top-skills",
)
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+.#_:/-]*|[\u3400-\u9fff]")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def compact(value: Any, limit: int = 400) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_count(value: Any) -> int:
    text = str(value or "").lower().replace(",", "").strip()
    try:
        multiplier = 1_000_000 if text.endswith("m") else 1_000 if text.endswith("k") else 1
        return int(float(text.rstrip("km")) * multiplier)
    except ValueError:
        return 0


def run_command(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
        raise RuntimeError(f"timeout after {timeout:.1f}s") from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _json_output(stdout: str) -> Any:
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        match = re.search(r"(\{.*\}|\[.*\])\s*$", stdout, re.S)
        if match:
            return json.loads(match.group(1))
        raise


def _check(result: subprocess.CompletedProcess[str], source: str) -> Any:
    if result.returncode:
        raise RuntimeError(f"{source} exit {result.returncode}: {compact(result.stderr or result.stdout)}")
    try:
        return _json_output(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{source} returned invalid JSON: {compact(result.stdout)}") from exc


def _lobehub(query: str, limit: int, timeout: float) -> list[dict[str, Any]]:
    cli = HOME / ".skills-manager/cache/lobehub-cli/node_modules/.bin/lhm"
    command = [str(cli) if cli.exists() else "npx", "mcp", "search", "--q", query, "--output", "json", "--page-size", str(limit)]
    if command[0] == "npx":
        command[1:1] = ["-y", "@lobehub/market-cli"]
    payload = _check(run_command(command, timeout), "lobehub")
    items = payload.get("items", []) if isinstance(payload, dict) else payload
    hits = []
    for item in items or []:
        github = item.get("github") or {}
        installs = parse_count(item.get("installCount"))
        hits.append({"source": "lobehub", "title": item.get("name") or item.get("identifier") or item.get("slug"), "description": item.get("description", ""), "identifier": item.get("identifier") or item.get("slug"), "url": github.get("url") or item.get("homepage") or item.get("manifestUrl", ""), "installs": installs, "score": float(github.get("stars") or installs), "popularity": installs})
    return hits


def _skillhub(query: str, limit: int, timeout: float) -> list[dict[str, Any]]:
    bundled = Path(__file__).resolve().parent / "skillhub" / "skills_store_cli.py"
    if bundled.exists():
        command = [sys.executable, str(bundled)]
    else:
        local = Path("/Users/mac/.local/bin/skillhub")
        command = [str(local) if local.exists() else "skillhub"]
    command += ["--skip-self-upgrade", "search", query, "--json", "--search-limit", str(limit)]
    payload = _check(run_command(command, timeout), "skillhub")
    items = payload.get("results", []) if isinstance(payload, dict) else payload
    return [{"source": "skillhub", "title": item.get("name") or item.get("slug"), "description": item.get("description", ""), "identifier": item.get("slug") or item.get("id"), "url": item.get("url") or f"https://skillhub.cn/{item.get('slug', '')}", "installs": parse_count(item.get("installs")), "score": float(item.get("score") or 0)} for item in items or []]


def _skills_sh(query: str, limit: int, timeout: float) -> list[dict[str, Any]]:
    payload = run_command(["npx", "-y", "-p", "skills@1.5.15", "skills", "find", query], timeout)
    if payload.returncode:
        raise RuntimeError(f"skills-sh exit {payload.returncode}: {compact(payload.stderr or payload.stdout)}")
    hits = []
    current: dict[str, str] | None = None
    for raw in payload.stdout.splitlines():
        line = ANSI_RE.sub("", raw).rstrip()
        match = re.match(r"^([^/]+)/([^@\s]+)@([^\s]+)\s+([\d,.KkMm]+) installs$", line)
        if match:
            if current:
                hits.append(current)
            owner, repo, path, installs = match.groups()
            current = {"source": "skills-sh", "title": path.replace("-", " "), "description": "", "identifier": f"{owner}/{repo}@{path}", "url": f"https://skills.sh/{owner}/{repo}/{path}", "installs": parse_count(installs), "score": float(parse_count(installs))}
        elif current and line.startswith("└ "):
            current["url"] = line[2:].strip()
    if current:
        hits.append(current)
    return hits[:limit]


def _awesome(query: str, limit: int, timeout: float) -> list[dict[str, Any]]:
    roots = [root.resolve() for root in AWESOME_ROOTS if root.exists()]
    if not roots:
        raise RuntimeError("awesome-top-skills local index not found")
    terms = [term.casefold() for term in TOKEN_RE.findall(query)]
    hits = []
    for root in roots:
        for path in (root / "categories").rglob("*.md"):
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            for line in content.splitlines():
                if not line.startswith("| ["):
                    continue
                cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
                if len(cells) < 7:
                    continue
                name = re.search(r"\[([^]]+)\]\(([^)]+)\)", cells[0])
                repo = re.search(r"\[([^]]+)\]\(([^)]+)\)", cells[1])
                if not name or not repo:
                    continue
                haystack = " ".join((name.group(1), cells[2], name.group(2), repo.group(1))).casefold()
                if terms and not all(term in haystack for term in terms):
                    continue
                hits.append({"source": "awesome-top-skills", "title": name.group(1), "description": cells[2], "identifier": name.group(2), "url": name.group(2), "installs": parse_count(cells[4]), "score": float(parse_count(cells[3])), "updated": cells[5]})
    return hits[:limit]


def run_source(query: str, source: str, limit: int, timeout: float) -> list[dict[str, Any]]:
    handlers = {"lobehub": _lobehub, "skillhub": _skillhub, "skills-sh": _skills_sh, "awesome-top-skills": _awesome}
    return handlers[source](query, limit, timeout)


def result_key(item: dict[str, Any]) -> str:
    for value in (item.get("url"), item.get("identifier"), item.get("title")):
        if value:
            return re.sub(r"/+$", "", str(value).strip().casefold())
    return "unknown"


def _relevance(query: str, item: dict[str, Any]) -> float:
    haystack = " ".join(str(item.get(field) or "").casefold() for field in ("title", "description", "identifier", "path"))
    terms = [term.casefold() for term in TOKEN_RE.findall(query)]
    return sum(term in haystack for term in terms) / len(terms) if terms else 0.0


def search(query: str | None = None, source: str | None = None, limit: int = 20, timeout: float = 90.0, query_pack: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], list[str], list[str], list[str]]:
    if source and source not in SOURCES:
        raise ValueError(f"unknown source {source!r}")
    if query_pack:
        original = str(query_pack.get("original_query") or query or "").strip()
        queries = [str(item.get("q", "")).strip() for item in query_pack.get("queries", []) if item.get("q")]
        if original and original not in queries:
            queries.insert(0, original)
    else:
        original = str(query or "").strip()
        queries = [original] if original else []
    if not queries:
        raise ValueError("query or query_pack is required")
    selected = [source] if source else list(SOURCES)
    tasks = [(q, s) for q in queries for s in selected]
    warnings: list[str] = []
    completed_sources: set[str] = set()
    records: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(16, len(tasks))) as pool:
        futures = {pool.submit(run_source, q, s, max(1, limit), min(timeout, 45.0)): (q, s) for q, s in tasks}
        for future in as_completed(futures):
            q, s = futures[future]
            try:
                hits = future.result()
                completed_sources.add(s)
            except Exception as exc:
                warnings.append(f"{s} [{q}]: {exc}")
                continue
            for rank, hit in enumerate(hits, 1):
                key = result_key(hit)
                record = records.setdefault(key, {"item": dict(hit), "matched_queries": set(), "observed_sources": set(), "rrf": 0.0, "best_rank": rank})
                record["matched_queries"].add(q)
                record["observed_sources"].add(s)
                record["rrf"] += 1.0 / (60 + rank)
                record["best_rank"] = min(record["best_rank"], rank)
                if (parse_count(hit.get("installs")), float(hit.get("score") or 0)) > (parse_count(record["item"].get("installs")), float(record["item"].get("score") or 0)):
                    record["item"] = dict(hit)
    if not completed_sources:
        raise RuntimeError("all selected sources failed")
    ranked = []
    count = max(1, len(queries))
    for record in records.values():
        item = record["item"]
        coverage = len(record["matched_queries"]) / count
        rrf = min(1.0, record["rrf"] * 61 / count)
        relevance = max((_relevance(q, item) for q in record["matched_queries"]), default=0.0)
        popularity = min(1.0, math.log1p(parse_count(item.get("installs"))) / math.log1p(100_000))
        sources = sorted(record["observed_sources"])
        source_count = min(1.0, len(sources) / len(selected))
        item.update({"matched_queries": sorted(record["matched_queries"]), "observed_sources": sources, "rank_score": round(100 * (0.3 * coverage + 0.2 * rrf + 0.35 * relevance + 0.1 * source_count + 0.05 * popularity), 2)})
        ranked.append(item)
    ranked.sort(key=lambda item: (item["rank_score"], len(item["matched_queries"]), parse_count(item.get("installs")), str(item.get("title") or "").casefold()), reverse=True)
    return ranked[:limit], warnings, sorted(completed_sources), queries


def main() -> int:
    parser = argparse.ArgumentParser(description="Search four skill marketplaces")
    parser.add_argument("query", nargs="*", help="query terms (legacy single-query mode)")
    parser.add_argument("--query-pack", help="JSON string or path to a QUERY_PACK")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--source", choices=SOURCES)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    pack = None
    if args.query_pack:
        raw = Path(args.query_pack).read_text(encoding="utf-8") if Path(args.query_pack).is_file() else args.query_pack
        pack = json.loads(raw)
    try:
        results, warnings, completed, queries = search(" ".join(args.query), args.source, args.limit, args.timeout, pack)
    except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
        expanded = [item.get("q") for item in (pack or {}).get("queries", []) if isinstance(item, dict) and item.get("q")]
        payload = {"original_query": (pack or {}).get("original_query") or " ".join(args.query), "expanded_queries": expanded, "sources": list(SOURCES), "observed_sources": [], "results": [], "warnings": [str(exc)]}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"Search failed: {exc}", file=sys.stderr)
        return 1
    payload = {"original_query": (pack or {}).get("original_query") or (queries[0] if queries else ""), "expanded_queries": queries, "sources": list(SOURCES), "observed_sources": completed, "results": results, "warnings": warnings}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"=== Unified marketplace search: {payload['original_query']!r} ({len(results)} results) ===")
        print("Sources: " + (", ".join(completed) or "none"))
        for index, item in enumerate(results, 1):
            print(f"{index}. [{item.get('source')}] {item.get('title') or item.get('identifier')} | {item.get('url') or '-'}")
        for warning in warnings:
            print(f"Warning: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
