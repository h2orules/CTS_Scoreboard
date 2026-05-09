"""Cold-storage clients (Azure Storage Tables + Blob).

Tables: long-lived meet metadata (so 'no live meet' / 'expired ID' pages keep
working after Redis evicts hot state).

Blob: optional snapshot of the final meet state when a meet is closed.

Phase 1: scaffold only.
"""
from __future__ import annotations

# TODO(phase-6): TableServiceClient + BlobServiceClient initialisation,
# upsert_meet_metadata(), archive_closed_meet(), lookup_meet_status().
