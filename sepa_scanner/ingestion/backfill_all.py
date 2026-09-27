"""Run all remaining historical backfill batches with safe failure stopping."""

from __future__ import annotations

import json

from config.settings import get_settings
from sepa_scanner.ingestion.backfill import run_backfill_batch
from sepa_scanner.ingestion.providers import create_data_provider


BATCH_SIZE = 25


def main() -> None:
    """Backfill every remaining active symbol in bounded, resumable batches."""
    provider = create_data_provider(get_settings())
    batch_number = 0
    while True:
        result = run_backfill_batch(provider, batch_size=BATCH_SIZE)
        if result["symbols_selected"] == 0:
            print(json.dumps({"status": "complete", **result}), flush=True)
            return
        batch_number += 1
        print(json.dumps({"batch": batch_number, **result}), flush=True)
        if result.get("symbols_failed", 0):
            raise RuntimeError("Backfill stopped because a batch recorded failed symbols")


if __name__ == "__main__":
    main()
