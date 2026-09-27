# SEPA Scanner

Local-first NSE VCP/SEPA research and screening tool. This repository is the initial project scaffold from [PROJECT-DESIGN.md](PROJECT-DESIGN.md).

## Development

Requirements: Python 3.12+, Node.js 20+, and npm.

Start the API:

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
uvicorn sepa_scanner.api.main:app --reload
```

Start the frontend in another terminal:

```sh
cd frontend
npm install
npm run dev
```

The API currently exposes `/health`, `/universe`, and `/scores/today` as scaffold routes. Data providers and analytics need configuration and implementation before the screener can produce market results.

## Local data

DuckDB analytics data defaults to `data/market.duckdb`. The SQLite app-state store and data directories are intentionally local and ignored by Git.

## Market data configuration

Copy `.env.example` to `.env` and supply your Kite Connect credentials using the KITE__ settings shown in the template. The selected provider is controlled by `DATA_PROVIDER`; the shared provider contract keeps the ingestion, storage, and analytics layers independent of Kite.

Kite's instrument master is synchronized into `provider_instruments`. The next data-layer increment will add the canonical NSE/Nifty 500 universe filter before scheduling a full historical backfill.
