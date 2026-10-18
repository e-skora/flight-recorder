"""Owned source adapters (FR-V2-01 onward).

Adapters keep their own operational records. They never write domain records
directly: every domain append still crosses the collector (INV-11).
"""
