# Token Optimization & Context Efficiency Rules (**TOKEN_OPTIMIZATION_RULES.md**)

## 1. File Reading & Codebase Exploration Rules
- **Targeted Reads Only:** Never read entire large files when only a specific function, class, or section is needed. Use line ranges (`offset`/`limit`) or search for exact symbols first.
- **Search Before Opening:** Use `grep`, `ripgrep`, or AST/graph queries (`GRAPH_REPORT.md` if present) to locate exact file paths and line numbers before reading file contents.
- **Ignore Noise Directories:** Never scan, list, or read `node_modules/`, `.venv/`, `venv/`, `__pycache__/`, `.git/`, `dist/`, `build/`, `*.lock`, `*.min.js`, or large `.csv`/`.json` data dumps.
- **No Redundant Re-Reads:** Do not re-read files already inspected in the current session unless modified externally.

## 2. Code Generation & Editing Rules
- **Diffs Over Full Rewrites:** Never output entire files when modifying existing code. Always apply minimal, surgical edits (`search/replace` or unified diffs) touching only the changed lines.
- **Zero Placeholder Bloat:** Do not rewrite unchanged functions, imports, or boilerplate around an edit.
- **Modular File Size:** Keep new source files focused and under ~300 lines so future reads and edits require minimal context windows.

## 3. Output & Communication Conciseness
- **Code First, Zero Fluff:** Omit conversational preambles, apologies, step-by-step narration of obvious actions, and post-task summaries unless explicitly requested.
- **Concise Explanations:** Limit explanations or error diagnoses to 1–3 crisp bullet points; never paste raw multi-hundred-line terminal logs or stack traces back into responses.
- **Single-Pass Execution:** Plan all file edits and tool calls upfront to avoid iterative trial-and-error loops and repeated context reloads.

## 4. Sub-Agent & Context Hygiene Rules
- **Delegate Broad Searches:** Offload multi-file exploration, log parsing, or test-suite runs to sub-agents/background tasks so raw output does not pollute the main conversation context.
- **Compact State Checkpoints:** When summarizing progress or handing off context, use dense bullet lists of modified file paths and function names rather than prose paragraphs.
