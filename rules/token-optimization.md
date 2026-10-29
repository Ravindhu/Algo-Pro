# Token Optimization Rules — Blue Engine

Two budgets are protected. They are separate problems and get separate fixes.

| Budget | Where spent | Owner of this file |
|---|---|---|
| **A. IDE / copilot tokens** | OpenRouter billed by CodeGPT, Ollama prompt cache | Sections 1–2 |
| **B. Runtime tokens** | Frontline agent calls to Ollama / OpenRouter, per pipeline run | Section 3 |

---

## 1. IDE behaviour (agent-authored output)

- No preamble, no restating the request, no trailing summary unless asked.
- Never reprint a whole file. Output only the changed lines/functions.
  Untouched blocks → `# ... [existing logic unchanged] ...`
- Comments ≤ 5 words, only where the *why* is non-obvious.
- Mock/sample data: max 3 items. Use TypedDict/dataclass, not raw JSON blobs.
- Group constant/style dicts at the top of a file (prompt-cache friendly).
- Read with line ranges (±10), not whole files, when the target line is known.
- Search with `grep`, not terminal `findstr`/`rg` pipelines — one call, less output.
- If context approaches the limit: say which file to drop, then continue.
  Do not silently re-read files already in context.

## 2. Editor scoping (VS Code / CodeGPT)

- `.vscode/settings.json` excludes heavy, generated or stale trees from search,
  indexing and chat context: `.venv`, `data/`, `db/`, `log/`, `logs/`, `_restored/`,
  `patches/`, `Temp/`, `__pycache__`, `openalgo/frontend/node_modules`.
- Reward: less index noise per request, fewer accidental file dumps.
- Keep `openalgo/` readable only when a task actually targets the bridge.

## 3. Runtime token governance (in-repo, enforced by code)

Implemented in `Blue_engine/core/llm/tokens.py`, wired into `LLMClient`.

- `TokenGovernor` wraps every `LLMClient.complete` call when built through
  `LLMClient.from_config` (`engines.llm.tokens` block).
- **Compaction** — prompts are capped to `max_prompt_tokens` (default 3072,
  output 600). System messages are never dropped; user text is cut at a line
  boundary with a visible `[... input elided N chars ...]` marker.
- **Cache** — SHA-256 over system+user content, model, backend chain, session.
  TTL 900 s, LRU 256. Only `ok=True` results are stored. Calls are already
  `temperature=0`, so a hit is the same deterministic answer.
- **Metering** — `client.status()["tokens"]` exposes calls, hits, prompt tokens
  saved, compaction events, per-backend split. Feed it to the dashboard.
- Config:

```yaml
engines:
  llm:
    ollama: {enabled: true, model: "qwen3:8b"}
    openrouter: {enabled: true, model: "qwen/qwen-2.5-7b-instruct:free"}
    tokens:
      enabled: true
      max_prompt_tokens: 3072
      max_tokens_out: 600
      cache_ttl_seconds: 900
      cache_entries: 256
```

- Disable with `tokens.enabled: false`. Default is ON — cost control is the safe side.
- Reference test: `Blue_engine/tests/test_llm_token_governor.py`.

### Invariants (do not break)

1. Governance may only *shrink* prompts and *skip duplicate* calls.
   It may never widen a schema, drop a system message, or cache a failure.
2. Estimation is for budgeting only. Never size a position from a token count.
3. Frontline only. Risk, execution, slice and POV paths must not import
   `core/llm/*`.

## 4. Dashboard runtime rules (unchanged, restated for clarity)

- `dcc.Interval` ≥ 500 ms.
- Slice every live queue (`logs[-50:]`, `order_book[:10]`).
- Strip non-essential WebSocket columns at ingest.
- Debounce indicator math on a price-delta check.
