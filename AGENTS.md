# User Setup
act as an multi hyperperameter statergy optimizer anayist. and impliment and find my slected statergy's multiple perameter optimized and plot visual image in dash ploty using optuna to see my proper statrgy perameters to get max sharp ratio of my statergy.

always focus  on impliment the  C:\Users\Admin\CodeGPT\my-trading-system\Blue_engine\optimizer

Build a world-class, fully automated algorithmic trading system for the Indian market.in english only

git enviroment is 
c:\Program Files/Git/cmd
 Always Openalgo,My trading system should be run
 always thinking about to make profitable  algarithom 


The objective is to develop a robust, modular, scalable, secure, research-driven, and production-ready quantitative trading platform.


 

## Temporary / Staging Workspace

Use the following directory C:\Users\Admin\CodeGPT\my-trading-system\Temp\Staging Workspace as the temporary/staging workspace for research, advance features,Moudles,optimized backtesting with statergy development,experiments, intermediate files, analysis, downloaded documentation, temporary outputs, test artifacts, prototypes, and other non-production work,Dashboard image,terminal page:


Use this directory as a temporary workspace only.
C:\Users\Admin\CodeGPT\my-trading-system\Temp


keep credential open 
Before committing code, review the Temp directory and move any required permanent files to the appropriate project location.

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
* Analytics
* Performance measurement

### 2. Dash / Plotly Trading Dashboard

Build a professional, institutional-style trading dashboard using Dash and Plotly.

The dashboard (Dash martine component DCM)should operate as an integrated algo-trading terminal with all important functions available in one place.

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

## OpenAlgo Project Path

Current OpenAlgo project path:
C:\Users\Admin\CodeGPT\my-trading-system\openalgo


This architecture must be:

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
2. Identify dependencies.
3. Identify potential conflicts.
4. Design the change.
5. Implement the change.
6. Test the change.
7. Review the implementation.
8. Check for redundancy.
9. Check for duplicate functionality.
10. Check for security issues.
11. Check performance.
12. Run relevant tests.
13. Run regression tests.
14. Update documentation.
15. Review file organization.
16. Review logging and error handling.
17. Review backward compatibility.
18. Perform the appropriate audit.
19. Commit only after the above checks pass.

Do not create duplicate modules or parallel implementations without a clear architectural reason.

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

1.select one statergy 
  The Ultimate 10-Parameter Visual Workflow in dashboard
To get the absolute clearest picture of your strategy without feeling overwhelmed, you should look at your charts in this exact order:
1. Run plot_param_importances to find out which 3 or 4 parameters are driving the ship.
2. Open plot_slice for those specific highly important parameters to find their individual sweet-spot numbers.
3. Launch plot_parallel_coordinate filtering only for those important parameters to see how they weave together to hit peak sharp ratio.


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



