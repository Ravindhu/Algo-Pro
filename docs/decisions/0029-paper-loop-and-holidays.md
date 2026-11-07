# 0029 - Paper loop from the dashboard, and the holiday source

Date: 2026-11-07
Status: ACCEPTED (owner chose option B, 2026-11-07). Paper only. Live stays refused (decisions 0017, 0028).

## Context

START and STOP on the dashboard should run the quant algo. Holidays should come from NSE.

## Decision: paper loop

1. START launches `scripts/run/paper_loop.py` in the background. STOP asks it to stop (a stop flag file; it stops within a few seconds).
2. The loop is paper only. It runs `run_paper.run()` with the in-process paper broker. It never uses analyze or live.
3. The loop runs only in the NSE cash session: a trading day, 09:15 to 15:30 IST, per the holiday calendar. Outside it, each cycle records the reason and does nothing.
4. Signals come from `data/paper/signals.csv`. No live signal source exists yet. Until the file is there, each cycle records "no signals file" and nothing is traded.
5. START is refused while the kill switch is on (fail closed). It is refused if a loop is already running.
6. STOP does not close positions. The kill switch does that, through the paper runner.
7. Every start, stop and error is written to the audit log. The page shows the loop status, the last cycle, and the last error.
8. A loop that stops sending heartbeats for three cycles counts as stopped.

## Decision: holidays

1. The equity holiday list is `config/calendar/holidays.csv`. It holds the equity weekday holidays from NSE's Capital Market circular of 12 Dec 2025, plus the 15 Jan 2026 amendment (NSE circular 07/2026).
2. Currency-only and commodity-only dates are not in that file (for example 19 Feb, 19 Mar, 1 Apr, 26 Aug 2026). The calendar would wrongly close the market on those days if they were added.
3. The dashboard reads that file and shows the market status and the holiday count.
4. Sandbox check: NSE's site is not reachable from the sandbox. The circular text was checked through a public mirror of NSE circulars, and the currency circular CD71962 was checked on NSE's archive. The owner should confirm the list on nseindia.com before live use.
5. `scripts/research/fetch_nse_holidays.py` can refresh the file from NSE on the owner machine. Its response format is not verified yet, and it leaves the file alone if the fetch fails.
6. Muhurat special sessions (for example Sunday 8 Nov 2026) are not traded.

## Open items

- A signal source for `data/paper/signals.csv`.
- Owner confirmation of the holiday list on nseindia.com, and a run of the NSE fetch script on the owner machine.
- The dashboard runs the loop only while the dashboard process is up. A loop started from the dashboard keeps running after the page closes, and it stops only when STOP is pressed or the machine restarts.
