# Lot/Serial workflow — test server

This module is deliberately disabled by default and only accepts the
`testing0808.odoo.com` test server. It never validates a picking.

## One-time test setup

1. Set `ODOO_TEST_*`, `LOT_SERIAL_ENABLED=true`, and a long
   `LOT_SERIAL_API_TOKEN` in `.env`.
2. Temporarily set `LOT_SERIAL_ALLOW_SCHEMA_SETUP=true`.
3. Call `POST /lot-serial/setup` with header `X-Lot-Serial-Token` and body
   `{ "confirm": true }`.
4. Set `LOT_SERIAL_ALLOW_SCHEMA_SETUP=false` again and restart the service.

Setup creates two manual fields on `stock.lot`:

- `x_lot_serial_workflow_generated`: provenance; only these lots can be renamed.
- `x_original_lot_name`: immutable audit value saved before a rename.

## API contract

All requests require `X-Lot-Serial-Token`.

1. Call a Preview endpoint first. It returns `plan_hash`.
2. Send that exact `plan_hash` to the matching Apply endpoint.
3. Apply rejects an outdated Preview and rolls back workflow-created lots if
   creating or assigning a later line fails.

Receipt preview:

```json
POST /lot-serial/receipt/preview
{ "picking_id": 17878 }
```

Receipt apply:

```json
POST /lot-serial/receipt/apply
{ "picking_id": 17878, "plan_hash": "<from-preview>" }
```

Rename preview/apply requires a scanner or user to supply the real serial.
The system cannot infer a physical value such as `1234` by itself.

```json
POST /lot-serial/rename/preview
{
  "picking_id": 123,
  "rename_map": {
    "O-MH08966-BED-NERISSA-001": "1234"
  }
}
```

Only a completed internal transfer from `MID` to a destination warehouse in
`LOT_SERIAL_WHITELIST_WAREHOUSES` (default `ORDST,ORDAP`) can rename a serial.
Transfers in the other directions, and deliveries to a partner, are rejected.

## Automatic trigger boundary

The service exposes a safe endpoint; it does not poll arbitrary done pickings.
To automate renaming, configure the barcode/scanner integration to call the
Preview endpoint with its scanned `rename_map`, show the result to the operator,
then call Apply with the returned hash. This prevents changing a serial without
an actual scanned value or an operator confirmation.
