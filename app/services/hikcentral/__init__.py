"""HikCentral plate validation and missing-plate recovery.

The ANPR camera already reports the plate — this package does not replace it and
runs no OCR. HikCentral has exactly two jobs:

  1. **Validate** a plate the camera reported.
  2. **Recover** a plate when the camera reported none (~22% of cars, which are
     otherwise lost as `silent_entry` alerts with no session).

Everything HikCentral-shaped stays behind this boundary. Callers import only the
names below, receive one canonical plate, and never learn where it came from.

Gated by `HIK_VALIDATION_MODE`:
  off           — never contacted; behaviour identical to before this layer.
  shadow        — looked up and logged; the ANPR plate always wins and no
                  recovery session is created. Changes no behaviour.
  authoritative — a disagreeing HikCentral plate replaces the ANPR plate, and a
                  unique HikCentral record recovers a missing plate.
"""

from app.services.hikcentral.client import (
    close_hikcentral_http_client,
    start_hikcentral_http_client,
)
from app.services.hikcentral.models import (
    PLATE_SOURCE_EDGE_ANPR,
    PLATE_SOURCE_HIK_CONFIRMED,
    PLATE_SOURCE_HIK_CORRECTED,
    PLATE_SOURCE_HIK_RECOVERED,
    HikImages,
    HikOutcome,
)
from app.services.hikcentral.validation import (
    DIRECTION_ENTRY,
    download_hik_images,
    record_hik_validation,
    recover_entry_plate,
    validate_entry_plate,
)

__all__ = [
    "DIRECTION_ENTRY",
    "HikImages",
    "HikOutcome",
    "PLATE_SOURCE_EDGE_ANPR",
    "PLATE_SOURCE_HIK_CONFIRMED",
    "PLATE_SOURCE_HIK_CORRECTED",
    "PLATE_SOURCE_HIK_RECOVERED",
    "close_hikcentral_http_client",
    "download_hik_images",
    "record_hik_validation",
    "recover_entry_plate",
    "start_hikcentral_http_client",
    "validate_entry_plate",
]
