# Follow NCC xuất hóa đơn — rollout

## Business rule

When a **purchase receipt** is completed, `auto-workflow` creates one To-Do
activity named `Follow NCC xuất hóa đơn` on that receipt.

Assignee priority and provenance:

1. Direct `stock.picking.sale_id`.
2. Studio `stock.picking.x_studio_source_so`.
3. Receipt move → Purchase Order line → Sale Order/Sale Order line.
4. Exact BG/O-BG reference in `purchase.order.origin`.
5. If no Sales Order can be resolved, use the Purchase Order **Buyer**
   (`purchase.order.user_id`).
6. If multiple Sales Orders resolve to different OPs, or a named OP cannot be
   mapped safely, create nothing and retry instead of assigning the wrong user.

For every Sales Order path, the `OP in charge` person is matched to the active
`BON SC` user. The code tolerates the known naming variation `NGUYỄN MAI NGÂN`
vs `NGUYỄN THỊ MAI NGÂN`, but rejects genuinely mismatched linked-user data.

The production example `stock.picking(15689)` resolves to:

- Receipt: `MID/NHAN/06929`
- Purchase Order: `MH08545`
- Sales Order: `BG-202512-3272`
- OP: `NGUYỄN THỊ TRANG`
- Assignee: `res.users(122)` — `BON SC, NGUYỄN THỊ TRANG`

## Safety contract

- Runs outside Odoo; no Python Server Action or Automation Rule code.
- Scope includes both `Bonario Vietnam` (ID 1) and `Ordinaire Việt Nam`
  (ID 11), restricted to `state=done`, incoming, non-return, purchase-linked
  receipts.
- First start writes a UTC cutoff and **does not backfill** historical receipts.
- Only receipts with `date_done > cutoff` are eligible.
- Existing `(receipt, summary)` activity causes an idempotent skip.
- Activity is attached only to `stock.picking`; no Sale Order, Purchase Order,
  or receipt business fields are modified.
- State is atomically persisted in `/app/data/vendor_invoice_follow_snapshot.json`.
- The watcher defaults to disabled and requires explicit enablement.

## Test server UAT

Run the unit/full suites:

```bash
python -m pytest -q tests/test_vendor_invoice_follow.py
python -m pytest -q
```

Run the live canary against `testing2608`:

```bash
ODOO_TEST_URL=https://testing2608.odoo.com \
ODOO_TEST_DB=testing2608 \
ODOO_TEST_UID='<uid>' \
ODOO_TEST_API_KEY='<api-key>' \
python scripts/test_vendor_invoice_follow_testing.py --picking-id 16609
```

The canary creates a uniquely named test activity, verifies receipt linkage and
assignee, then deletes only that canary ID. It asserts all pre-existing
activities on the receipt are unchanged.

Verified on 05/09/2026:

- Direct SO: `MID/NHAN/07130` → Trang → `res.users(122)`.
- Studio Source SO: `MID/NHAN/06328` → `BG-202512-3371` → Mai Hồng →
  `res.users(249)`.
- Move/Purchase Line, including Ordinaire: `O-MID/IN/00346` →
  `O-MH09101` → `O-BG-2608-0495` → Mai Hồng → `res.users(249)`.
- PO Origin: `MID/NHAN/07428` → `MH09903` → `BG-2608-0925` → Trang →
  `res.users(122)`.
- Buyer fallback with no BG: `MID/NHAN/07431` → `MH09909` → Mai Thu →
  `res.users(276)`.
- Stale linked-user defense: `MID/NHAN/07164` maps OP Mai Thu to
  `res.users(276)`, not the incorrectly linked Mai Ngân user.
- Every canary activity was created, read back, then removed. Pre-existing
  activity sets remained unchanged.

Read-only 2026 census on production covered 1,120 completed incoming purchase
receipts across both companies. Among the 1,108 records exposing a Purchase
Order relation, 765 had BG/SO provenance, including nine apparent Origin
ambiguities that the receipt-line path disambiguates; 343 had no BG and are
handled by the PO Buyer fallback. Twelve records expose an Odoo computed-field
anomaly and remain fail-safe rather than being assigned speculatively.

## Production activation (only after approval)

Set these values in the production container environment:

```dotenv
VENDOR_INVOICE_FOLLOW_ENABLED=true
VENDOR_INVOICE_FOLLOW_POLL_INTERVAL=30
VENDOR_INVOICE_FOLLOW_ACTIVITY_TYPE_ID=4
VENDOR_INVOICE_FOLLOW_COMPANY_IDS=1,11
VENDOR_INVOICE_FOLLOW_SUMMARY=Follow NCC xuất hóa đơn
VENDOR_INVOICE_FOLLOW_DEADLINE_DAYS=0
VENDOR_INVOICE_FOLLOW_STATE_PATH=/app/data/vendor_invoice_follow_snapshot.json
```

On the first production start, inspect `/health` and logs for the seed cutoff.
The first poll must create zero historical activities. Only then complete one
new controlled receipt and verify exactly one activity with the expected OP.
