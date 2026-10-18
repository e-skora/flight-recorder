"""HubSpot-style company adapter: local synthetic operational capture (FR-V2-01).

Scope of this package today: supplied synthetic notifications and supplied
property-read results become one immutable operational capture per processing
unit, in a separate SQLite store. No collector envelope is built, no decision is
recorded, and no HTTP, CRM, n8n or network operation exists here. The capture is
an operational record, not domain provenance and not exact replay.
"""
