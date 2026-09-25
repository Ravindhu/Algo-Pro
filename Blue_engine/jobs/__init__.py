"""
jobs - scheduled operational jobs (I/O lives here, not in the engines).

The engines are **pure by contract** (``core/base.py``): ``_run()`` performs no
network, SDK or DB work. Anything that must run *on a schedule* therefore belongs
in a job, not in an engine.

    daily_portfolio   post-close: fetch -> engine -> persist (grows the dataset)

Each job:

* is runnable as ``python -m blue_engine.jobs.<name>``,
* returns a result dict **and** writes ``data/job_status/<name>.json`` so the
  dashboard can show job health without parsing logs,
* never raises - a failure is ``ok=False`` + ``error``, and the process exits
  non-zero so Task Scheduler / cron can alert,
* honours market hours by default (pass ``--force`` to override).

NOTE: jobs are deliberately NOT imported here. Importing them from ``__init__``
means ``python -m blue_engine.jobs.daily_portfolio`` imports the module twice
(once via the package, once via runpy), which emits a RuntimeWarning. Import the
module you need directly: ``from blue_engine.jobs import daily_portfolio``.
"""

__all__: list = []