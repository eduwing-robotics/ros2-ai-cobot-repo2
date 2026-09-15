"""Retired Incoming QA v0.1 HTTP callback namespace.

Incoming QA terminal evidence is no longer accepted over public HTTP. Actual
production authority is the v0.2 FMS UDP Request → ACK → Final Result path.
The module remains only as a stable import location for downstream source users;
it intentionally defines no state-changing route.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/incoming-qa", tags=["Incoming Material QA"])
