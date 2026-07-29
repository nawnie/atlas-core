from __future__ import annotations

import json
import tempfile
from pathlib import Path

from atlas_core.adapters import HotelAdapter
from atlas_core_sdk import AtlasClient


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        client = AtlasClient.local(
            database=Path(directory) / "atlas.db",
            adapters=(HotelAdapter(),),
        )
        confirmed = {
            "reservation_id": "SDK-100",
            "version": "1",
            "updated_at": "2026-07-24T12:00:00Z",
            "arrival_date": "2026-08-10",
            "departure_date": "2026-08-13",
            "guest_count": 2,
            "room_type": "king",
            "status": "confirmed",
            "total": "729.00",
            "currency": "USD",
        }
        client.ingest(
            domain="hotel",
            tenant_id="sdk-demo-hotel",
            source_system="synthetic-pms",
            record=confirmed,
        )
        result = client.ingest(
            domain="hotel",
            tenant_id="sdk-demo-hotel",
            source_system="synthetic-pms",
            record={
                **confirmed,
                "version": "2",
                "updated_at": "2026-07-24T12:05:00Z",
                "status": "cancelled",
                "total": "0.00",
            },
        )
        print(json.dumps(result.to_dict(), indent=2))


if __name__ == "__main__":
    main()
