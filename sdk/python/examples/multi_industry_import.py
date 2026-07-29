from __future__ import annotations

from pathlib import Path

from atlas_core.adapters import (
    ApartmentAdapter,
    ConstructionAdapter,
    HealthcareAdapter,
    HotelAdapter,
    ManufacturingAdapter,
)
from atlas_core_sdk import AtlasClient


client = AtlasClient.local(
    database=Path("runtime") / "sdk-multi-industry.db",
    adapters=(
        HotelAdapter(),
        ApartmentAdapter(),
        ConstructionAdapter(),
        ManufacturingAdapter(),
        HealthcareAdapter(),
    ),
)

receipt = client.import_batch(
    tenant_id="factory-1",
    connector_id="mes-snapshot",
    domain="manufacturing",
    source_system="example-mes",
    external_batch_id="work-orders-2026-07-24T15:05Z",
    records=(
        {
            "record_id": "WO-22",
            "version": "1",
            "payload": {
                "work_order_id": "WO-22",
                "updated_at": "2026-07-24T15:05:00Z",
                "line_id": "LINE-2",
                "sku": "PART-7",
                "status": "running",
                "quantity_planned": 100,
                "quantity_completed": 45,
                "defect_count": 1,
                "downtime_minutes": 0,
                "quality_hold": False,
            },
        },
    ),
)

print(receipt)
print(client.search_cartography(tenant_id="factory-1"))
