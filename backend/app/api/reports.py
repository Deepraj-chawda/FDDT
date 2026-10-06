"""
MIS/compliance reporting endpoints — STUB.

Out of scope for the initial foundation task (System Specification section 6). Will
expose pandas-aggregated reporting (fraud rate, avg resolution time,
case volumes) exported as PDF/Excel, per section 3.5.
"""
from fastapi import APIRouter

router = APIRouter(prefix="/reports", tags=["reports"])
