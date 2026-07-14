"""
Approval Document Number — Webhook Service.
Replaces Odoo base.automation ID 36 ("10.2 Documents Creator") on approval.request.

Original Odoo automation (7 lines) triggered on_create_or_write when
date_confirmed=False, then:
  1. Lookup hr.employee by request_owner_id
  2. Map department → dept_code (BD/MKT/SC/ACC/HR)
  3. Map company → company_code (BON/FUR/ORD/IDY/NAM)
  4. Call ir.sequence.next_by_code('approval.banhanh')
  5. Write x_studio_documents_number = "{seq}/{doc_type}-{dept}/{company}"

This service receives a webhook from Odoo (simplified server action = 2 lines)
and performs the same logic via XML-RPC. Guard: only assigns if the field is
empty (fixes the original bug where every save consumed a sequence number).

Odoo server action 1922 should be replaced with:
    if not record.x_studio_documents_number:
        requests.post(WEBHOOK_URL, json={'id': record.id})
"""

import json
import logging
from datetime import datetime

from config import Config
from services.odoo_client import odoo

logger = logging.getLogger(__name__)

# ─── Mappings (from original automation code) ───
DEPT_MAP = {
    "Business Development": "BD",
    "Marketing": "MKT",
    "Supply Chain": "SC",
    "Accounting": "ACC",
    "Human Resources": "HR",
}

COMPANY_MAP = {
    "Bonario Vietnam": "BON",
    "FURNY": "FUR",
    "Ordinaire Việt Nam": "ORD",
    "IDYLLE HOME": "IDY",
    "NAMCHUL": "NAM",
}

# ─── Runtime stats ───
_total_requests = 0
_total_assigned = 0
_total_skipped_has_number = 0
_total_errors = 0
_last_result = None


def generate_doc_number(approval_id: int) -> dict:
    """
    Generate and assign document number for an approval.request record.

    Args:
        approval_id: The approval.request record ID.

    Returns:
        dict with result details.
    """
    global _last_result

    _total_requests += 1
    logger.info(f"[DOC-NUM] Processing approval.request id={approval_id}")

    # 1. Fetch the approval.request record
    recs = odoo.read("approval.request", [approval_id], fields=[
        "id", "name", "request_owner_id", "x_studio_documents_number",
        "x_studio_documents_types", "company_id",
    ])
    if not recs:
        raise ValueError(f"approval.request {approval_id} not found")

    rec = recs[0]
    existing = rec.get("x_studio_documents_number")

    # 2. Guard: skip if already has a document number (fixes waste bug)
    if existing:
        _total_skipped_has_number += 1
        logger.info(f"[DOC-NUM]   {rec.get('name','?')}: already has '{existing}' — skip")
        result = {
            "approval_id": approval_id,
            "name": rec.get("name"),
            "action": "skipped",
            "reason": "already_has_number",
            "existing_number": existing,
        }
        _last_result = result
        return result

    # 3. Lookup hr.employee by request_owner_id
    owner = rec.get("request_owner_id")
    owner_id = owner[0] if isinstance(owner, (list, tuple)) else owner
    owner_name = owner[1] if isinstance(owner, (list, tuple)) and len(owner) > 1 else "?"

    dept_code = "NoDept"
    if owner_id:
        emps = odoo.search_read(
            "hr.employee",
            [["user_id", "=", owner_id]],
            fields=["id", "department_id"],
            limit=1,
        )
        if emps:
            dept = emps[0].get("department_id")
            dept_name = dept[1] if isinstance(dept, (list, tuple)) else None
            dept_code = DEPT_MAP.get(dept_name, "OTH") if dept_name else "NoDept"
            logger.info(f"[DOC-NUM]   Owner={owner_name}, dept={dept_name} → {dept_code}")
        else:
            logger.warning(f"[DOC-NUM]   No hr.employee found for user_id={owner_id}")

    # 4. Map company → company_code
    company = rec.get("company_id")
    company_name = company[1] if isinstance(company, (list, tuple)) and len(company) > 1 else None
    company_code = COMPANY_MAP.get(company_name, "CMP") if company_name else "CMP"

    # 5. Document type (from selection field, fallback 'XX')
    doc_type = rec.get("x_studio_documents_types") or "XX"

    # 6. Get next sequence number (atomic, Odoo-side)
    seq_num = odoo.execute(
        "ir.sequence", "next_by_code",
        [Config.APPROVAL_DOC_SEQUENCE_CODE],
    )
    if not seq_num:
        seq_num = Config.APPROVAL_DOC_FALLBACK_NUM
        logger.warning(f"[DOC-NUM]   next_by_code returned None — using fallback '{seq_num}'")

    # 7. Build document number
    doc_number = f"{seq_num}/{doc_type}-{dept_code}/{company_code}"
    logger.info(f"[DOC-NUM]   Generated: {doc_number}")

    # 8. Write back to approval.request
    odoo.write("approval.request", [approval_id], {
        "x_studio_documents_number": doc_number,
    })
    _total_assigned += 1
    logger.info(f"[DOC-NUM]   ✅ Written to approval.request {approval_id}")

    result = {
        "approval_id": approval_id,
        "name": rec.get("name"),
        "action": "assigned",
        "doc_number": doc_number,
        "seq_num": seq_num,
        "doc_type": doc_type,
        "dept_code": dept_code,
        "company_code": company_code,
        "owner": owner_name,
    }
    _last_result = result
    return result


def get_approval_doc_number_status() -> dict:
    """Health check status."""
    return {
        "enabled": Config.APPROVAL_DOC_NUMBER_ENABLED,
        "sequence_code": Config.APPROVAL_DOC_SEQUENCE_CODE,
        "total_requests": _total_requests,
        "total_assigned": _total_assigned,
        "total_skipped_has_number": _total_skipped_has_number,
        "total_errors": _total_errors,
        "last_result": _last_result,
    }