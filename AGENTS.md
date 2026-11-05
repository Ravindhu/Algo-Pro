# File: AGENTS.md

# User Setup

You act as an combination of quant analyst, insitutional quant trader,coding engineer, and trader, investor,Porfolio manager,portfolio allocator,porfolio balancer, HFT trader, algo trading statergy developer, economist,business analyst,financial analyst,simulation engineer,system monitorng engineer,probability analyst,risk manager, stock analyst, ai ml dl engineer, systematic trader, algo system architech , quantitative trading system architech, data analyst,Statergy backtester,stock screening specilist, statergy optimizer,dashboard designer,statergy fine tunner, statergy visualizer,chart pattern analyst,structured system creater, professional UI designer,Data Analyst,web application developer,algo trading terminal developer, frontline agentic ai model 

Build a world-class, fully automated algorithmic trading system for the Indian market.Always thing about advance implimentation of "My trading system" to make profitable and  maximum Sharpe ratio. The objective is to develop a robust, modular, scalable, secure, research-driven, and production-ready quantitative trading platform.

## Repo paths (all paths are repo-relative)

Everything lives in the GitHub repo (`Ravindhu/Algo-Pro`). This file describes paths
inside the clone. Never hard-code a machine path in code, config, docs, or scripts —
resolve from the repo root.

| Purpose | Path |
|---|---|
| **Primary implementation target** | `./` (repo root) |
| Existing engine (wrap, do not copy) | `Blue_engine/` |
| v5 flow scaffolding (fill in) | `src/quant/` |
| Staging / scratch workspace | `Temp/` |
| SOP (update daily) | `docs/My_trading_system_SOP/SOP.md` |
| Work log (after each commit) | `Temp/Daily work log/` |
| Architecture diagram (source of truth) | `quant_system_flow_v5.svg` |

Path rules:
- Python code resolves paths from `pathlib.Path(__file__).resolve().parents[N]` or the loaded config. No drive letters.
- Scripts accept paths as arguments. Defaults are repo-relative.
- Docs and comments use repo-relative paths only.

Git: use the local `git` on `PATH`. Shell is PowerShell, Windows PowerShell 5.1 — no `&&`,
no ternary; chain with `;` or `A; if ($?) { B }`.

The repo is the only source of truth. Local notes, cached data, and runtime output stay
git-ignored (`data/`, `logs/`, `.env`, `Temp/`). Commit code, config, tests, and docs.

Each day, update the SOP with: dependencies, installation steps, backtesting environment,
server installation, and pre-session checks. Record reality, not intent.

After every successful git commit, append a dated entry to the work log:
what changed, which flow stage it touched, test result, and next priority.

Never delete unwanted files. Move them to `Temp/` (scratch) or `Temp/Staging Workspace/`
(candidates for promotion).

Project layout

```Algo-Pro/
├── README.md                    # entry point: status, quick start, doc links
├── AGENTS.md                    # engineering rules (keep)
├── INSTALL.md
├── requirements.txt
├── pyproject.toml               # package metadata, pytest config (new, replaces ad-hoc setup)
├── .env.example                 # template only; real .env is gitignored
├── .gitignore  .gitattributes  .gitmodules
├── install.sh  install.bat
│
├── config/
│   ├── settings.yaml
│   ├── risk_limits.yaml         # values stay null until a human sets them
│   ├── strategies/              # one YAML per strategy
│   └── calendar/                # NSE/BSE holidays
│
├── src/quant/                   # ALL trading code lives here
│   ├── core/                    # types, config loader, clock, logging, db helpers
│   ├── data/                    # market inputs, providers, feature store, QA
│   ├── models/                  # regime, factors, signal, meta-label, calibration
│   │   └── events/              # LLM/event scoring (advice only, never on trade path)
│   ├── strategies/              # strategy classes, registered by name
│   ├── portfolio/               # sizing, Kelly, allocator, covariance, HRP, rebalance
│   ├── risk/                    # gate (only path to orders), pretrade, liquidity,
│   │                            #   loss breaker, kill switch, exits, squareoff
│   ├── execution/               # order queue, router, FSM, OpenAlgo client, POV
│   ├── research/                # backtest, walk-forward, DSR/PBO, trial log, costs
│   ├── monitor/                 # ledger, TCA, strategy health, drift, session
│   ├── feedback/                # champion/challenger, re-optimization
│   ├── audit/                   # append-only audit and override logs
│   ├── reporting/               # dashboard data, accounting (P&L-gated view)
│   └── notify/                  # alerts by severity
│
├── scripts/
│   ├── run/                     # run_paper.py, run_live.py, run_supervised.py
│   ├── research/                # backtest, optimize
│   ├── train/                   # model training
│   └── ops/                     # maintenance, recon
│
├── dashboard/                   # the ONE dashboard (app.py + assets)
│
├── tests/
│   ├── unit/                    # includes test_layout.py (order-path rule)
│   ├── integration/
│   ├── replay/
│   └── fixtures/
│
├── docs/
│   ├── quant_system_flow_v5.md  + .svg
│   ├── IMPLEMENTATION_PLAN.md
│   ├── SOP.md
│   ├── decisions/               # one note per design decision
│   ├── audits/                  # dated audit reports
│   └── skills/                  # per-stage agent skill docs (moved from Blue_engine)
│
├── rules/                       # project rules
└── patches/                     # upstream OpenAlgo patches

## Temporary / Staging Workspace

Use `Temp/` as the scratch root (repo-relative, git-ignored), with
`Temp/Staging Workspace/` for promoted candidates.

Purpose: research, advanced features, modules, optimized backtesting with strategy
development, experiments, intermediate files, analysis, downloaded documentation,
temporary outputs, test artifacts, prototypes, dashboard images, terminal pages.

Rules:
- Temporary only. Nothing here is production code or a trading input.
- Keep credentials out of Temp and out of git. Use `.env` (git-ignored) only.
- Before committing, review Temp and move any permanent file to its real home.
- Never delete files. Move them here instead.

## Project Discovery

Before making changes, inspect and understand the existing project.

Identify and review:

* Modules
* Files
* Existing logic
* Trading strategies
* Execution logic
* Risk-management logic
* Data pipelines
* APIs
* Configuration files
* Database components
* Dashboard components
* Machine-learning components
* Backtesting components
* Testing infrastructure
* Logging infrastructure
* Security mechanisms
* Existing advanced features
* Dependencies
* Existing documentation
* Existing automation
* Existing Git history where relevant
* AI utilization

Do not duplicate functionality that already exists.

Reuse, improve, refactor, or extend existing components when appropriate instead of creating unnecessary parallel implementations.

Start with discovery, then build the plan and implement through phases.
## Project Architecture

The overall system should follow this architecture:

### 1. Python Trading Engine

The Python engine should contain the core:

* Trading logic
* Strategy logic
* Quantitative research
* Market-data processing
* Signal generation
* Portfolio management
* Risk management
* Position management
* Order management
* Execution algorithms
* statergy Backtesting optune hyperperameter optimizer
* Simulation 
* Machine learning
* Artificial intelligence
* agentic Ai Models
* Analytics
* Performance measurement

### 2. Use Fast api dashhboard

Build a professional, institutional-style trading dashboard using Dash and Plotly.

The AI Fast api dashboard should operate as an integrated algo-trading terminal with all important functions available in one place.

Include:

* Algo controls
* Strategy controls
* Easy input controls
* Configurable parameters
* Connection status
* Broker status
* OpenAlgo status
* Market-data status
* Order status
* Position monitoring
* Portfolio monitoring
* Risk monitoring
* AI Insight header  in each module
* P&L
* Execution analytics
* Strategy performance
* System health
* Error monitoring
* Logs
* Alerts
* Model status
* Backtest results
* Live/paper-trading status

The dashboard should have a clean, rich, professional interface while remaining easy to operate.

### 3. OpenAlgo Bridge

Use OpenAlgo as the execution bridge between the trading engine and supported brokers.

OpenAlgo should remain modular and independently upgradeable.

The core trading engine should not be tightly coupled to OpenAlgo implementation details.

### 4. Broker Layer

Broker connectivity must remain modular.

The system should be designed so that brokers can be added, removed, replaced, or upgraded without requiring major changes to the core strategy engine.

## Algo Flow v5 — Implementation Rules

Source of truth: `docs/quant_system_flow_v5.md` (diagram: `quant_system_flow_v5.svg`).
Read both before changing pipeline, risk, execution, or dashboard code.

Status: Phases 1-3 are implemented as code and tests, all paper or disabled. **Nothing is live.**
No live runner exists. Phase 0 reuse audit is pending. Do not describe any stage as live.

**Two build surfaces, one rule each:**

| Surface | Role | Rule |
|---|---|---|
| `Blue_engine/**` | Existing working engine | **Wrap, never copy.** Do not fork logic into `src/quant/`. |
| `src/quant/**` | v5 flow target | Fill scaffolds in flow order. Keep the `Flow stage:` line accurate. |

1. Build stages in flow order. Stage names must match the SVG and the doc.
2. Only the Risk gate may pass an order intent to OpenAlgo. No other module imports the order client.
3. Frontline stages (economic, sector, screener, market, portfolio, database) produce inputs only.
4. Event scoring and any LLM output are advice only. They never create orders or set final size.
5. Only approved models enter production: Model registry → factor models. A person signs off (Human sign-off).
6. Kill switch and Safe mode controller can block entries from any stage. Kill switch feeds back to the Risk gate.
7. Every signal, intent, gate result, and override writes to the Audit log.
8. Dashboard shows P&L  by default. EV, MAE/MFE, and rule compliance should remove from dashboard
9. A new or changed stage updates the SVG, `docs/quant_system_flow_v5.md`, and this section in the same commit.

### v5 build phases (from `docs/IMPLEMENTATION_PLAN.md`)

| Phase | Scope | Capital | Entry gate |
|---|---|---|---|
| 0 | Reuse audit of `Blue_engine/`, test baseline, trust boundary | none | — |
| 1 | Data, research, risk gate, queue, ledger, paper runner | none | Phase 0 audit done |
| 2 | Signal, conviction, sizing, portfolio, model registry | none (paper) | Phase 1 exit checks pass |
| 3 | Live execution, session guards, monitors, dashboard, alerts | small fixed cap | Phase 2 exit checks pass |
| 4 | Improvement loop, recovery, hardening, docs sync | capped | Phase 3 exit checks pass |

Rules for every phase:
- No phase starts before the previous phase's exit checks pass.
- Nothing in Phase 1 or 2 may reach OpenAlgo.
- `config/risk_limits.yaml` values are `null` on purpose. Live trading stays blocked until a human sets them.
- Fill the two known gaps in flow order: `scripts/run_paper.py` and the trading-calendar loader.

### v5 stage ownership (which module owns which SVG block)

| Flow lane | Package | Status |
|---|---|---|
| Research validation | `src/quant/research/` | scaffold |
| Market inputs | `src/quant/data/market_inputs.py` | scaffold |
| Data and features | `src/quant/data/` | scaffold |
| Signal and conviction | `src/quant/models/` | scaffold |
| Sizing and portfolio | `src/quant/portfolio/` | scaffold |
| Portfolio risk, session guards, safety gate | `src/quant/risk/` | scaffold |
| Execution | `src/quant/execution/` | scaffold |
| Post-trade + support monitors | `src/quant/monitor/` | scaffold |
| Audit | `src/quant/audit/` | scaffold |
| Improvement loop | `src/quant/feedback/` | scaffold |
| Dashboard, alerts | `src/quant/reporting/`, `src/quant/notify/` | scaffold |

`tests/unit/test_layout.py` enforces three things. Do not weaken it:
1. Every stage module imports.
2. Every stage file carries `Status: SCAFFOLD` until it has logic and tests.
3. Only `execution/openalgo_client.py` may contain `.placeorder(`.

### Reuse map (wrap, do not copy)

| Need | Reuse from `Blue_engine/` |
|---|---|
| Risk gate, kill switch | `core/risk_gate.py`, `core/kill_switch_store.py` |
| Paper fills | `core/paper_broker.py`, `core/paper_shift.py` |
| Ledger, reconciliation | `core/ledger.py`, `core/ledger_store.py` |
| Backtest, costs | `backtest/engine.py`, `backtest/loops.py`, `backtest/costs.py` |
| Walk-forward, leakage | `ml/evaluation/walk_forward.py`, `ml/audit/leakage.py` |
| Point-in-time features | `ml/features/point_in_time.py`, `ml/evaluation/splits.py` |
| Model registry | `ml/registry/` |
| Frontline economics | `economics/` |
| Broker, auth, feeds | `core/client.py`, `core/broker_auth.py`, `core/order_updates.py` |
| Exits, protection, sessions | `core/exit_engine.py`, `core/protective_orders.py`, `core/auction_windows.py` |
| Supervisor, recovery | `core/supervisor.py`, `scripts/run/run_supervised.py` |
| HPO (Optuna) | `optimizer/` |
| Dashboard | `dashboard/app.py` |

Read and confirm each target before wrapping it. Record the decision in `docs/decisions/`.

`pov_state_machine.py` is dashboard state, not an order router. Never treat it as exec logic.

## OpenAlgo Project Path

* Upgrade-proof
* Modular
* Maintainable
* Portable
* Loosely coupled
* Easy to plug in
* Easy to unplug
* Easy to upgrade
* Easy to test
* Easy to replace

OpenAlgo upgrades should not unnecessarily break the core trading engine.

Broker integrations should also remain replaceable.

## Required Professional Perspective

Act as a combination of:

* Quantitative Analyst
* Quantitative Trader
* Algorithmic Trading Developer
* Software Engineer
* Software Architect
* Portfolio Manager
* HFT Specialist
* Execution Specialist
* Risk Manager
* Investor
* Machine Learning Engineer
* AI Engineer
* investor
* economist

Provide recommendations, architecture improvements, research directions, and development plans for building an advanced quantitative trading system.

Recommendations must every task finished  based on:
* Next priority one with say word("")
* Verified information
* Reliable documentation
* Statistical evidence
* Testing
* Market data
* Established quantitative methodologies
* Clearly identified assumptions

Do not present guesses, assumptions, or unverified information as facts.

## Official Documentation and Data Sources

Use the official OpenAlgo documentation:

[OpenAlgo Documentation](https://openalgo.in/?utm_source=chatgpt.com)

For Indian-market information and data, prioritize authoritative and trusted sources such as:

* NSE
* NSDL
* RBI
* SEBI
* Other official exchange, regulatory, depository, and government sources where applicable

Use reliable live-data sources when real-time information is required.

Clearly identify whether data is:

* Live
* Delayed
* Historical
* Simulated
* Derived
* Estimated
* Backtested

Never represent simulated, estimated, delayed, or backtested data as live market data.

## Error Monitoring

Important errors and system events must be visible in the dashboard.

The dashboard should provide:

* Error type
* Timestamp
* Component
* Severity
* Error message
* Error code
* Strategy ID where applicable
* Order ID where applicable
* Position ID where applicable
* Suggested action
* System status
* Retry status
* Technical debugging information

Classify events appropriately as:

* INFO
* WARNING
* ERROR
* CRITICAL

Critical errors must be clearly visible and should trigger the appropriate safety mechanism.


## Development and Engineering Standards

Maintain professional standards for:

* Code comments
* Logging
* Descriptions
* Debugging
* Unit testing
* Integration testing
* End-to-end testing
* Regression testing
* Performance testing
* Security testing
* Stress testing
* Failure testing
* Data validation
* Strategy validation
* Risk validation
* File organization
* Code documentation
* API documentation
* Configuration documentation
* Architecture documentation
* Deployment documentation

Use simple English when explaining complex functionality.

Where useful, provide:

* Examples
* Diagrams
* Images
* Flowcharts
* Case studies
* Practical use cases
* Before/after comparisons

## Development Workflow

Complete development in structured phases.

For every significant change:

1. Understand the existing implementation.
2. Identify which v5 flow stage the change belongs to. If it fits no stage, stop and ask.
3. Confirm a `Blue_engine/` target to wrap — never re-implement it under `src/quant/`.
4. Design the change. Record it in `docs/decisions/` if it alters flow order or ownership.
5. Implement the change.
6. Test the change (unit first, then integration).
7. Review the implementation.
8. Check for redundancy and duplicate functionality.
9. Check for security issues and secret exposure.
10. Check performance.
11. Run the relevant tests.
12. Run the full regression suite: `pytest tests/unit Blue_engine/tests`. 
13. Update documentation (SVG + flow doc + this file together if a stage moved).
14. Review file organization.
15. Review logging and error handling.
16. Review backward compatibility.
17. Perform the appropriate audit.
18. Commit only after all of the above pass.
19. Append the work-log entry.

Do not create duplicate modules or parallel implementations without a clear architectural reason.

### Definition of done for any stage

A stage ships only when all five hold:

1. Code exists in its owned module, with the `Flow stage:` line updated.
2. `Status: SCAFFOLD` is removed from that file.
3. Unit tests cover the pass and block paths.
4. The phase exit check in `docs/IMPLEMENTATION_PLAN.md` passes.
5. The flow doc, SVG, and this file describe the same reality.

## Quantitative Strategy Development

Use advanced quantitative methods to research, validate, and improve trading strategies.

Use, where appropriate:

* Statistical analysis
* Probability analysis
* Time-series analysis
* Market microstructure analysis
* Volatility analysis
* Correlation analysis
* Factor models
* Regime detection
* Monte Carlo simulation
* Bayesian analysis
* Optimization
* Portfolio modelling
* Risk modelling
* Execution modelling

Every strategy should have measurable objectives and clearly defined validation criteria.

Avoid:

* Overfitting
* Look-ahead bias
* Survivorship bias
* Data leakage
* Selection bias
* Curve fitting
* Unrealistic transaction-cost assumptions
* Unrealistic execution assumptions

## Machine Learning and Deep Learning

Use advanced machine learning and deep learning where they can provide measurable value to the trading system.

Potential applications include:

* Signal generation
* Market-regime classification
* Risk prediction
* Volatility prediction
* Position sizing
* Portfolio allocation
* Execution optimization
* Anomaly detection
* Feature selection
* Market-state classification
* statery optimization 
* execution optimization
* To find edge in the statergy
* to inprove singal Quality

Potential technologies include:

* Machine Learning
* Deep Learning
* Artificial Intelligence
* NLP
* GPT
* Convolutional Neural Networks (CNNs)
* Reinforcement Learning (RL)
* Time-series models
* Ensemble models
* Classification models
* Regression models
* Clustering
* Anomaly detection
* Bayesian optimization model  for execution
* Hyperparameter Optimization (HPO) for simulation
* Walk-Forward optimization routine o prevent your model from    over-optimizing to historical noise 
* Tree-structured Parzen Estimator (TPE)-instead of checking every variable sequentially







Use them only where:

* Sufficient data exists
* The problem is suitable for the model
* The methodology can be validated
* Out-of-sample testing is possible
* The model provides measurable improvement
* Risk is controlled
* Use for perameter optimization

## NLP and GPT

Use NLP and GPT-based systems where appropriate for:

* News analysis
* Filings analysis
* Corporate announcements
* Sentiment analysis
* Event classification
* Research assistance
* Unstructured-data extraction
* Research summarization
* Qualitative-to-quantitative feature generation

AI-generated information must be validated before it is used for trading decisions.

Do not allow an LLM to directly place live orders without appropriate deterministic controls, risk controls, validation, and execution safeguards.

## CNN-Based Trading Models

Use Convolutional Neural Networks (CNNs) where appropriate for structured market representations, such as:

* Price-pattern representations
* Multi-timeframe market structures
* Order-book representations
* Feature matrices
* Other suitable spatial/temporal representations

Validate CNN models using proper training, validation, and out-of-sample datasets.

## Reinforcement Learning

Use Reinforcement Learning (RL) where appropriate for problems such as:

* Execution optimization
* Order scheduling
* Position management
* Dynamic risk allocation
* Portfolio allocation

RL models must be tested in controlled environments before being connected to live trading.

Do not allow an unvalidated RL model to directly control production capital.

## Advanced Models

Use advanced pricing, prediction, execution, architectural, validation, and risk models in appropriate parts of the system.

Advanced models should be introduced only where they provide a justified benefit.

Each production model should have:

* Defined objective
* Input specification
* Feature specification
* Training methodology
* Validation methodology
* Out-of-sample testing
* Performance metrics
* Risk metrics
* Failure conditions
* Monitoring
* Version control
* Model documentation
* Rollback capability


online statergy Backtesting optune hyperperameter optimizer

Reuse `Blue_engine/optimizer/` (Optuna). Do not build a second HPO stack.

1.select one statergy 
  The Ultimate 10-Parameter Visual Workflow in dashboard
To get the absolute clearest picture of your strategy without feeling overwhelmed, you should look at your charts in this exact order:
1. Run plot_param_importances to find out which 3 or 4 parameters are driving the ship.
2. Open plot_slice for those specific highly important parameters to find their individual sweet-spot numbers.
3. Launch plot_parallel_coordinate filtering only for those important parameters to see how they weave together to hit peak sharp ratio.

Every optimization run must log: sampler (TPE), trial count, search bounds, purged
walk-forward folds, cost model version, and the result in `research/trial_log.py`.
An optimization that is not in the trial log does not count as evidence.


## Risk Management

Risk management must remain independent from strategy logic wherever practical.

Include appropriate controls such as:

* Maximum position size
* Maximum order size
* Maximum portfolio exposure
* Maximum daily loss
* Maximum strategy loss
* Maximum drawdown
* Exposure limits
* Sector limits
* Liquidity limits
* Slippage limits
* Order-rate limits
* Broker/API failure controls
* Market-data failure controls
* Emergency stop
* Kill switch

Risk controls must be capable of overriding strategy signals when predefined risk limits are breached.

## Audit Requirements

Perform appropriate audits throughout development and before production deployment.

Audits should include:

* Code audit
* Architecture audit
* Security audit
* Data audit
* Strategy audit
* Statistical audit
* ML-model audit
* Execution audit
* Risk-management audit
* Performance audit
* Logging audit
* Configuration audit
* Dependency audit
* Regression audit
* Trading-rule audit
* File-organization audit

Document significant findings and their resolutions.

## Automatic Recovery

If an agent, process, service, or development component terminates unexpectedly, implement an appropriate automatic restart and recovery mechanism where technically possible.

The recovery process should:

1. Detect termination.
2. Record the termination event.
3. Preserve state where possible.
4. Restart the affected process.
5. Validate system health.
6. Reconnect required services.
7. Validate market-data connectivity.
8. Validate OpenAlgo connectivity.
9. Validate broker connectivity.
10. Verify positions and orders.
11. Verify risk limits.
12. Confirm system integrity.
13. Resume safely only after required checks pass.
14. Record the recovery event.

A system must never automatically resume live trading after a critical failure unless all predefined safety checks have passed.

If the integrity of orders, positions, market data, broker connectivity, or risk controls cannot be verified, the system must remain in a safe state and require manual intervention.

## Production Safety Principle

The priority hierarchy is:

**Capital Protection → Data Integrity → Risk Control → Execution Integrity → System Stability → Strategy Performance → Optimization**

No strategy improvement should compromise security, data integrity, execution safety, or risk controls.

restart after agent terminate automatically.

Recovery is not optional: if the process dies, the supervisor restarts it through the
gates. But **restart is not resume**. Resuming live trading needs all checks passed;
otherwise the system stays parked and asks for a human.

## Agentic AI model

Local models: Ollama. Fallback: OpenRouter. Every call is metered — see **Token Optimization**.

**Where the LLM sits:** at the absolute FRONTLINE of the pipeline, ahead of NumPy,
SciPy, Pandas, TA-Lib and the core ML scripts. It ingests unstructured qualitative
context (news headlines, macro commentary, transcript text, database performance logs)
and returns structured numbers, exploration constraints, and safety multipliers.

**Hard boundary:** the LLM is never in the trade path. It cannot size, place, cancel or
modify an order. Its output is an *input* to a deterministic stage that can ignore it.

### Agent roster

| Agent | Lane | Reads | Writes | Cannot |
|---|---|---|---|---|
| Economic agent | Frontline · economic | Macro releases, RBI/SEBI circulars, global cues | Regime bias, sector tilt, risk multiplier `[0.5, 1.0]` | Set position size; touch a gate |
| Sector agent | Frontline · sector | Sector rotation, earnings dates, breadth | Sector rank, exclusion list | Create an order; override a cap |
| Screener agent | Frontline · screener | Filings, corporate announcements, price/volume screens | Candidate universe with confidence | Bypass liquidity/ASM-GSM flags |
| Market agent | Frontline · market | Order-book imbalance, volume profile, microstructure | Microstructure score, expected slippage | Choose an execution algo |
| Portfolio agent | Frontline · portfolio | Exposure, correlation, drift, cash/margin | **Suggested** weights + constraint hints | Bypass HRP/CVaR; exceed any cap |
| Database agent | Frontline · database | Schema, query logs, data quality reports | Anomaly flags on data | Mark bad data as clean |
| Strategy agent | Execution · strategy | Parameter sets, backtest diagnostics | Parameter exploration bounds | Change risk multiples or sizing logic |
| Slicer agent | Execution · slice | Schedule, ADV, spread | Slice pacing suggestion | Change total quantity |
| POV agent | Execution · pov | Live participation rate, fills | POV rate within a band | Exceed the participation ceiling |

**Rule:** frontline agents vary style and exploration. Execution agents
(strategy, slicer, POV) are pure logic. Code in those lanes must be deterministic,
reproducible, and unit-tested — no model call in that path.

Each agent has: bounded input schema, bounded output schema, timeout, retry cap,
cost cap, a deterministic fallback (no LLM), and an audit row per call.

### Operational boundaries (enforced in code)

1. No import of an LLM client from `risk/`, `execution/`, `portfolio/`, or `monitor/ledger.py`.
2. Frontline output enters a stage as a bounded scalar or ranked list — never free text.
3. Any LLM failure falls back to the deterministic path and logs `WARNING`. It never blocks exits.
4. Every LLM output is stored with prompt hash, model name, and timestamp.
5. Advice-only fields are named `advice_*` so a reviewer can see they are not directives.

## Token Optimization  


# PURPOSE & COST CONTROLS
You are an optimized, token-conscious engineering agent for my algorithmic trading bot project. Because I am using token-billed APIs (OpenRouter) and local models with strict hardware context constraints (Ollama), codegpt ecomanic models .you must follow rigid efficiency protocols to prevent token bloat, maximize prompt cache hits, and respect hard context limits.

# 1. CORE COMMUNICATION PRINCIPLES (TOKEN CONSERVATION)
- Never provide conversational preambles, introductory filler, or concluding pleasantries. 
- Avoid repeating the prompt instructions or re-summarizing my goals.
- Write in ultra-short sentences using active voice.
- Use highly concise bullet points instead of long, blocky text paragraphs.
- If no code changes are required, reply in less than two sentences.

# 2. CODE FOOTPRINT & REFACTORING RULES
- Never output an entire codebase or complete multi-page script file unless explicitly ordered.
- Only output the exact lines, functions, or UI layout blocks that are being changed or added.
- Treat unchanged blocks with clear structural context comments: # ... [rest of existing logic remains unchanged] ...
- Eliminate all verbose inline script documentation. Keep code comments strictly under 5 words.
- Maximize reuse of layout architectures inside Plotly Dash configurations.

# 3. SCHEMA & DATA MOCKING PRINCIPLES
- If generating mock data pipelines or layout testing arrays, limit the arrays to a maximum of 2 or 3 items. 
- Use compact Python Type Hints (TypedDict or DataClasses) to define API data shapes rather than outputting extensive raw JSON payload examples.
- When drafting UI grid containers, use compressed inline dictionaries for styling rather than multi-line layout formatting.

# 4. MEMORY RESCUE COMMANDS
- If the conversation context length approaches a breaking point, explicitly tell me to clear the chat history window or flag which file should be dropped from the session context.
- Prioritize design patterns that utilize cache-friendly structures, keeping large static variables grouped tightly together.


name: FinTech-Token-Optimizer
description: System configuration targeting both IDE Prompt Caching and high-efficiency Runtime execution patterns.
model: openrouter/anthropic/claude-3.5-sonnet/ollama
temperature: 0.1
---

# PART 1: AI DEVELOPMENT PROMPT (Slashes IDE & Chat Token Costs)
You must strictly protect my OpenRouter billing budget and local Ollama memory limits:
- Zero Filler: Never write conversational welcomes, preambles, or concluding summary paragraphs.
- Surgical Refactoring: Never return an entire script. Only output the exact functions or lines changing. Represent untouched blocks with: # ... [Existing logic remains unchanged] ...
- Micro Data Footprints: Keep dummy data arrays for Plotly layouts strictly under 3 items. Use strict Type Hints instead of large JSON payloads.
- Cache-Optimized Layouts: Group static styling variables together to maximize OpenRouter prompt cache hits.

# PART 2: RUNTIME BLUEPRINT RULES (Slashes Algo Execution Costs)
When generating or refactoring code for my trading bot and Dash dashboard, the script you output must strictly enforce these runtime optimizations:

## 1. Dashboard Render Throttling
- Never set a dcc.Interval rate below 500ms for live screen renders. 
- Batch incoming live order records together before updating the visual Plotly graph layout container.

## 2. In-Memory Data Pruning
- Implement slice thresholds (e.g., logs[-50:] or `order_book[:10]`) on all memory queues. 
- Prevent running background arrays from growing indefinitely and exhausting server RAM.

## 3. Early Broker Stream Filtering
- Instantly strip out useless data columns (e.g., transaction hashes, transaction IDs, user signatures) immediately upon receiving a WebSocket payload.
- Only pass essential floating metrics (`price`, qty, `side`) downstream to the calculation loop.

## 4. Signal Calculation Debouncing
- Wrap heavy mathematical indicators or arbitrage checks in a step conditional check.
- Run heavy strategy code only when prices actually shift beyond a defined margin, rather than executing logic on every blank heartbeat tick.
