"""Odoo fields required to make lot/serial automation auditable and safe."""

from __future__ import annotations

from .config import LotSerialConfig


WORKFLOW_CREATED_FIELD = "x_lot_serial_workflow_generated"
ORIGINAL_NAME_FIELD = LotSerialConfig.AUDIT_FIELD
REQUIRED_FIELDS = (WORKFLOW_CREATED_FIELD, ORIGINAL_NAME_FIELD)


def get_workflow_field_status(odoo) -> dict:
    """Read field metadata without mutating Odoo."""
    fields = odoo.fields_get("stock.lot")
    missing = [name for name in REQUIRED_FIELDS if name not in fields]
    return {"ready": not missing, "missing": missing, "fields": fields}


def ensure_workflow_fields(odoo) -> dict:
    """Explicitly create the manual custom fields on the *test* database.

    This is intentionally not called by Preview or Apply.  A schema mutation
    requires an opt-in environment flag and an authenticated setup request.
    """
    before = get_workflow_field_status(odoo)
    if before["ready"]:
        return {"created": [], "missing": [], "ready": True}

    if not LotSerialConfig.ALLOW_SCHEMA_SETUP:
        raise RuntimeError(
            "Lot/serial custom fields are missing. Set "
            "LOT_SERIAL_ALLOW_SCHEMA_SETUP=true only on the test server, "
            "then call the authenticated setup endpoint."
        )

    model_ids = odoo.search("ir.model", [["model", "=", "stock.lot"]], limit=1)
    if not model_ids:
        raise RuntimeError("Cannot find ir.model for stock.lot")

    definitions = {
        WORKFLOW_CREATED_FIELD: {
            "field_description": "Created by Lot/Serial Workflow",
            "ttype": "boolean",
        },
        ORIGINAL_NAME_FIELD: {
            "field_description": "Original Lot/Serial Name",
            "ttype": "char",
        },
    }
    created = []
    for name in before["missing"]:
        definition = definitions[name]
        odoo.create(
            "ir.model.fields",
            {
                "name": name,
                "model_id": model_ids[0],
                "state": "manual",
                "store": True,
                **definition,
            },
        )
        created.append(name)

    after = get_workflow_field_status(odoo)
    if not after["ready"]:
        raise RuntimeError(f"Odoo did not expose the required fields: {after['missing']}")
    return {"created": created, "missing": [], "ready": True}


def assert_workflow_fields(odoo) -> None:
    status = get_workflow_field_status(odoo)
    if not status["ready"]:
        raise RuntimeError(
            "Lot/serial workflow is not initialized. Missing stock.lot fields: "
            + ", ".join(status["missing"])
            + ". Run the authenticated /lot-serial/setup endpoint on the test server."
        )
