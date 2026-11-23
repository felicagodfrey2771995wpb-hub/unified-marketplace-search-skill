---
name: unified-marketplace-search
description: Search skills across LobeHub, SkillHub, skills.sh, and the local awesome-top-skills index. Use for marketplace discovery, bilingual skill lookup, source comparison, and install-identifier triage. This skill deliberately excludes the legacy SkillDB and SkillsMP channels.
---

# Unified Marketplace Search

Use the bundled script to query exactly four sources:

- `lobehub` — LobeHub MCP marketplace (`@lobehub/market-cli` through the existing search entry point).
- `skillhub` — SkillHub CLI (`skillhub --skip-self-upgrade`). In this distribution the CLI ships at `scripts/skillhub/skills_store_cli.py` and is used automatically.
- `skills-sh` — skills.sh CLI (`skills find`).
- `awesome-top-skills` — local Markdown index, searched without network access. In this distribution the index ships at the plugin root under `awesome-top-skills/`.

The legacy `skilldb` and `skillsmp` channels are intentionally absent. Keep the existing `skilldb-search` skill unchanged for callers that still need it.

## QueryPack input

Accept `query_pack: QUERY_PACK` from `skill-router`. Execute every `queries[].q`
in parallel, always retaining `original_query` as the first query. For legacy
callers without a pack, treat the supplied query as a one-item pack.

## Run

From this skill directory:

```bash
python3 scripts/search.py "pdf editor"
python3 scripts/search.py --query-pack /tmp/query-pack.json --limit 20 --json
python3 scripts/search.py "travel planning" --source skillhub
```

Use `--source` to isolate one of the four providers. Default mode queries all four concurrently, keeps successful providers when another fails, and reports provider failures in `warnings`.

## Result handling

For each result, preserve `source`, `title`, `description`, `identifier`, `url`,
and popularity fields. Add `matched_queries` and `observed_sources` provenance.
The response includes `original_query`, `expanded_queries`, `results`,
`observed_sources`, and `warnings`. A single source failure becomes a warning;
all selected sources failing returns a non-zero status. Deduplicate by URL,
identifier, then title. Rank by query coverage, RRF, relevance, source count,
and a light popularity signal. Treat ranking as candidate triage.

Install only when the user asks or approves. Discovery is search-only; this skill does not install, modify, or delete skills.

## Options

```text
--limit N       Maximum merged results (default 20)
--timeout SEC   Total provider budget (default 90)
--source NAME   One of lobehub, skillhub, skills-sh, awesome-top-skills
--json          Emit normalized JSON for downstream tools
```
