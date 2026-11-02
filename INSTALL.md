# Install My Trading System

One-click installers for Windows and Ubuntu. Both are safe to re-run.
Neither places orders. The trading engine stays stopped until you start it.

## Windows (one click)

1. Install **Python 3.11+** (tick "Add python.exe to PATH") and **Git for Windows**.
2. Clone or download this repo.
3. Double-click **`install.bat`**.

It creates `.venv`, installs the dependencies and `Blue_engine`, clones
`openalgo\`, creates `.env` from `.env.example`, then starts the stack.

- Dashboard: http://127.0.0.1:8050
- OpenAlgo: http://127.0.0.1:5000
- Stop: `.venv\Scripts\python.exe scripts\ops\start_all.py --stop`

Fill in `.env` with your keys before you connect a broker.

## Ubuntu server (one command)

Run as root on a fresh Ubuntu or Debian server with systemd:

```bash
sudo bash install.sh --domain trade.example.com --email you@example.com
```

No domain yet on a private LAN? Use `--allow-plain-http`. Do not expose that
host to the internet. To keep options in a file, copy
`Servers/deploy.env.example` to `Servers/deploy.env`, edit it, and run
`sudo bash install.sh` with no arguments.

`install.sh` clones the repo to `/opt/my-trading-system` (set `MTS_ROOT` to change
it), then runs `Servers/install_ubuntu.sh`, which does the real work.

After install, put your broker state in `/opt/my-trading-system/_restored/`
(`.env` and `db/` together). Then open the UI and log in to your broker.
