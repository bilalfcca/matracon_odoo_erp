# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Update a module after code/XML changes
odoo-bin -u module_name --stop-after-init --no-http

# Install a new module (first time)
odoo-bin -i module_name --stop-after-init --no-http

# View live logs from the running background Odoo process
tail -f /home/odoo/logs/odoo.log

# Database access (credentials injected automatically)
psql

# Push to Odoo.sh remote (NEVER use git push)
odoosh-push
```

Always update both modules together when changing shared security:
```bash
odoo-bin -u purchase_demand_raise,site_operations --stop-after-init --no-http
```

## Module Architecture

### `purchase_demand_raise` (v1.4.4) — PR/PO Workflow
Entry point for all procurement. Defines the core role/privilege system that `site_operations` extends.

- **Security groups** (defined here, used everywhere):
  - `group_head_office` — cross-project visibility
  - `group_procurement_ho`, `group_ceo_approval`, `group_site_store`, `group_matracon_admin`
  - Privilege: `res_groups_privilege_demand_raise` ("Matracon Roles")
- **Flow**: Purchase Requisition → HO review/vendor selection → Comparative Statement → CEO approval → locked PO

### `site_operations` (v1.7.5) — Finance, Inventory, Compliance
The main module. Depends on `purchase_demand_raise`.

- **Security groups** defined here: `group_site_accountant`, `group_finance_ho`
- **Role permissions** are set in `security/role_app_groups.xml` using `(6, 0, [...])` REPLACE semantics — this is the authoritative place for `implied_ids`. `security_groups.xml` only defines group metadata (name, privilege, comment). Any `implied_ids` set in `security_groups.xml` will be overwritten by `role_app_groups.xml` which loads after it, **except for `group_head_office`** which has no entry in `role_app_groups.xml`.
- **Site scoping**: Each project site has a `project.site.config` record linking a `project.project`, `stock.warehouse`, and `account.analytic.account`. Record rules scope data by analytic account.

### `my_custom_module` — Partner Ledger Extensions
Adds Tax and Withheld Tax columns to the standard Odoo Partner Ledger report.

### `matracon_ss` — Screenshot utility (ignore)

## Key Custom Models (all in `site_operations/models/`)

| Model | Description |
|-------|-------------|
| `x.liability.sheet` | Vendor payables per site/month, synced from partner ledger |
| `x.attendance.sheet` | Monthly attendance registry per site |
| `x.salary.sheet` | Monthly salary sheet per site |
| `x.petty.cash` | Petty cash vouchers with PDF attachment requirement |
| `x.bank.guarantee` | Bank guarantees with expiry tracking |
| `x.tax.notice` | Tax/regulatory compliance notices |
| `x.subcontractor.ipc` | Interim Payment Certificates for subcontractors |
| `x.subcontractor.ho.advance` | HO advance payments to subcontractors |
| `x.subcontractor.backcharge` | Backcharge deductions |
| `x.employee.backcharge` | Employee salary deductions |
| `project.site.config` | Site configuration (warehouse, analytic account, users) |
| `x.wht.certificate` | Withholding tax certificates |
| `x.postdated.cheque` | Post-dated cheque register |

## Critical Design Patterns

### Liability Sheet ↔ Partner Ledger Sync
The liability sheet (`x.liability.sheet`) must mirror the Odoo Partner Ledger exactly:
- Only `account_type = 'liability_payable'` lines count (NOT `liability_current`)
- Analytic filtering checks **both** `move_id.x_project_analytic_account_id` (vendor bills) AND `aml.analytic_distribution` JSON field (MISC journal entries)
- Auto-creation triggers on `action_post` via `_ensure_liability_from_journal_entry` in `account_move.py`
- `action_refresh_from_ledger` uses a Python-level `_in_project()` helper + raw SQL with JSONB `?` operator for MISC entries

### Analytic Distribution on Move Lines
MISC journal entries use line-level `analytic_distribution` (JSON `{"analytic_id_str": percentage}`) — the move header `x_project_analytic_account_id` is only set on vendor bills (`in_invoice`). Always check both.

### Group/Export Permissions
`base.group_allow_export` controls the Export button on list views. All Matracon roles get it via `implied_ids` in `role_app_groups.xml`. Head Office gets it via `Command.link` in `security_groups.xml` (since `role_app_groups.xml` has no entry for it).

### Site-Scoped Record Rules
Record rules in `security/record_rules.xml` filter data by the site analytic account of the logged-in user's `project.site.config`. HO users (`group_head_office`) bypass these rules and see all sites.

### Stock Picking Extensions
`stock_picking.py` (101KB) is the largest file — handles material issuance, site-to-site transfers, and material returns with extensive custom logic for analytic distribution, cost tracking, and approval workflows.

### Payment Flow
`account_payment.py` (54KB) handles vendor payments against liability sheet lines, including WHT deduction, bank charges, and payment allocation across multiple bills.

## File Load Order Matters

In `site_operations/__manifest__.py`:
1. `security/app_visibility_groups.xml` — app gate groups (`group_mtr_app_*`)
2. `security/security_groups.xml` — group definitions (name, privilege, comment only)
3. `security/role_app_groups.xml` — role permissions via `(6, 0, [...])` REPLACE
4. `security/ir.model.access.csv` — model-level ACL
5. `security/record_rules.xml` — data scoping rules

## Hooks (`site_operations/hooks.py`)
`post_init_hook` and `post_migrate_hook` map production user IDs to groups and configure site project assignments. Changes here apply on install/upgrade, not at runtime.

## Reports
All reports use QWeb templates. Report XML IDs follow the pattern `site_operations.report_*`. Templates are in `report/*_template.xml`.

## Commit Identity
Use `awaisqureshidev01@gmail.com` / `Awais` from `.claude.json` oauthAccount.

---

## Session Notes — 2026-07-09

### Analytic Distribution on Move Lines — UPDATED pattern
The note above says "MISC journal entries use line-level `analytic_distribution`". This is now **also true for customer invoices (`out_invoice`) and site-accountant journal entries (`entry`)**. The analytic propagation in `account_move.py` now covers all three:

| `move_type` | Propagation | View editable? |
|---|---|---|
| `in_invoice` | auto-fill from header `x_project_analytic_account_id` | ✅ Yes |
| `out_invoice` | auto-fill from header `x_project_analytic_account_id` | 🔒 Read-only (SA view) |
| `entry` (site accountant only) | auto-fill from user's default analytic | 🔒 Read-only (SA view) |

Three hooks in `account_move.py` cover all paths:
- `_onchange_project_analytic_account_fill_lines` — handles `in_invoice` + `out_invoice`
- `_onchange_invoice_line_ids_fill_analytic` — handles `in_invoice` + `out_invoice`
- `_onchange_line_ids_fill_analytic_for_entry` — handles `entry` for site accountants (NEW)
- `AccountMoveLineSiteOps.create()` — DB-level stamp for all three types

The `analytic_distribution` column in the site-accountant Journal Entries tab (`sa_invoice_lines`) has:
```xml
readonly="parent.move_type in ('out_invoice', 'entry')"
```

### New Fields: `hr.employee.x_last_online` + `x_is_currently_online`
- File: `site_operations/models/hr_employee_ext.py`
- Both computed (store=False) together in `_compute_x_presence_info()`
- Batch SQL — single `SELECT user_id, last_poll FROM mail_presence WHERE user_id = ANY(%s)` per recordset
- `x_last_online` = `last_poll` timestamp (last heartbeat); shown as "Last Seen" in Settings tab and kanban
- `x_is_currently_online` = True when `last_poll >= now() - 65s`
- **Do NOT read `mail.presence.status`** — it stays 'online' in DB up to 12h after browser closes; `last_poll` is the only reliable signal

### Pending — odoosh-push blocked
All commits are **local only** and have NOT reached staging. Enable **AI Code Push** in Odoo.sh project settings, then run `odoosh-push`.

Commits waiting to be pushed (oldest first):
```
f394994  Feat: Trial Balance — group by account type
3d01a2e  Fix: dashboard payment KPIs + BG amendment attachments
91067e4  Feat: liability sheet — compact header + sticky scrollable lines table
dcd564f  Feat: customer invoice — analytic account shown per-line (read-only)
53679c8  Feat: journal entries — auto-fill analytic (read-only) for site accountants
891c0aa  Feat: employee form — Last Online timestamp for linked users
4ffdbac Fix: site balance + petty cash balance — HO MISC via analytic_distribution
fd2cf14 Docs: CLAUDE.md — session notes 2026-07-09
050924a Feat: subcontractor CoA + payment tracking overhaul (v1.7.6)
```

### Key design decisions made this session
- **`group_analytic_accounting` not in site accountant's `implied_ids`** — it's granted to all users via the system-wide "Analytic Accounting" setting (`res.config.settings.group_analytic_accounting`). In staging this setting is ON so all users see the `analytic_distribution` widget. The `groups="analytic.group_analytic_accounting"` attribute in views is already satisfied.
- **`mail.presence` over `res.users.login_date`** — `login_date` only updates on password re-entry. `mail.presence.last_presence` updates every 60 s while the browser tab is open. Much more accurate for "last online".
- **`x_project_analytic_account_id` on `entry` type** — the field has `invisible="move_type not in ('in_invoice', 'out_invoice')"` in the view but still exists on the model with `default=lambda self: self.env.user.x_default_analytic_account_id`. Site accountants always have this default set. The field is invisible/read-only on journal entries so they can't change it — only the line-level `analytic_distribution` is visible (read-only).

---

## Session Notes — 2026-07-09 (continued)

### Subcontractor CoA + Payment Flow — v1.7.6

#### New GL Accounts (account_configuration_data.xml)
| ID | Name | Code | Type |
|---|---|---|---|
| 61 | Payable to Suppliers | 211010 | liability_payable |
| 62 | Payable to Subcontractors | 211020 | liability_payable |

Both are `reconcile=True` so they appear in the partner ledger and can be reconciled.

#### Partner Auto-Assignment (res_partner.py)
`@api.onchange('category_id')` on `res.partner`:
- "Subcontractor" category → auto-sets `property_account_payable_id` to "Payable to Subcontractors"
- Other categories → auto-sets to "Payable to Suppliers"
- Only fires when `property_account_payable_id` is NOT already set (no override).

#### Petty Cash Subcontractor Advance (petty_cash.py)
New fields on `x.petty.cash.expense`:
- `is_subcontractor_advance` (Boolean)
- `advance_subcontractor_id` (Many2one res.partner, domain Subcontractor category)
- `advance_payable_account_id` (Many2one account.account, domain liability_payable)

Journal entry when posted:
- Dr: `advance_payable_account_id` (partner = subcontractor, analytic = project)
- Cr: Petty Cash account (Cash in Hand)

Auto-fill onchange: when `is_subcontractor_advance` is checked, `advance_payable_account_id` defaults to "Payable to Subcontractors" (if found).

The debit on the payable account with `partner_id` = subcontractor ensures the entry appears in the partner ledger AND is captured by the IPC "payments made" GL query.

#### IPC — Payments Made to Subcontractor (subcontractor_ipc.py)
**Replaced** `_compute_ho_advance` (reads `x.subcontractor.ho.advance` records) with `_compute_payments_made` (reads GL):

```sql
SELECT COALESCE(SUM(aml.debit), 0)
FROM account_move_line aml
JOIN account_move am ON am.id = aml.move_id
JOIN account_account aa ON aa.id = aml.account_id
WHERE aml.partner_id = [subcontractor_id]
AND aa.account_type = 'liability_payable'
AND am.state = 'posted'
AND am.date <= [cutoff_date]
AND (am.x_project_analytic_account_id = [analytic_id]
     OR aml.analytic_distribution ? [analytic_id_str])
```

**New fields** (stored=False computed):
- `payments_till_prev_ipc` → total payments up to previous IPC date
- `payments_till_this_ipc` → total payments up to this IPC date
- `payments_this_period` → difference (auto-fills "Payment Recovery")

**Kept field** `ho_advance_recovery` (DB column preserved for existing data) — relabelled "Payment Recovery" in view. Feeds into `total_deductions` unchanged.

**IPC view changes:**
- Section title: "HO Advance by Head Office" → "Payments Made to Subcontractor"
- Field labels updated
- HO Advances smart button removed from IPC form

#### HO Advance Legacy Menu
- Menu renamed "HO Advances (Legacy)"
- Restricted to `group_matracon_admin` and `base.group_system` only
- Model `x.subcontractor.ho.advance` and its data preserved for audit history

---

## Session Notes — 2026-07-09 (continued — second session)

### Employee Online Presence — Complete Overhaul

#### New Model: `x.employee.presence.log`
- File: `site_operations/models/x_employee_presence_log.py`
- Records every status transition (online → away → offline) from `mail.presence`
- Fields: `employee_id`, `user_id`, `timestamp`, `status`, `duration` (computed)
- `duration` = time in that state until next transition; most recent = "Ongoing"
- Hooked via `mail_presence_ext.py` overriding `mail.presence.create()` and `.write()`
- Only transitions are logged — repeated heartbeats keeping same status are skipped

#### New Field: `x_is_currently_online` on `hr.employee`
- Replaces `hr_icon_display == 'presence_online'` for online detection
- Uses a **2-minute threshold** on `mail.presence.last_presence` (heartbeat ~60s)
- Odoo's built-in `hr_icon_display` lags 5–10 min after logout; our field is accurate
- Computed together with `x_last_online` in `_compute_x_presence_info()` (single batch query)

#### Kanban Card
- Green dot + **"Online"** → only when `x_is_currently_online == True` (heartbeat ≤ 2 min ago)
- Otherwise shows **"Online From: [datetime]"** (was "Last online:")
- Uses `x_is_currently_online` field; `hr_icon_display` no longer referenced for text

#### Online History Tab (employee form)
- Visible when employee has a linked user; restricted to Admin / HO / Finance HO
- Columns: **Date & Time**, **Status** (Online/Offline with colour — no Away), **Duration**
- Duration computed via batch SQL per employee (not N+1)

### Vendor Payments (Accounting → Vendors → Payments) — Overhaul

#### Removed Fields (UI only, model retained for data integrity)
- `x_vendor_bank_account_id` — "Vendor Bank Account" hidden
- `x_is_ho_advance` — "HO Advance to Subcontractor" checkbox hidden (legacy)

#### New Field: `x_expense_account_id`
- Added to `account.payment`; shows ALL active chart of accounts (no HO-only restriction)
- Auto-fills from `partner_id.property_account_payable_id` on partner change
- `_prepare_move_line_default_vals` substitutes this account on the payable/receivable JE line
- **Works for ANY payment**, not just subcontractors
- For subcontractors: auto-fills to 211020 → JE debit has partner = subcontractor → IPC GL query picks it up automatically

#### IPC Auto-population via GL
Vendor payments to subcontractors now feed IPC "Payments Made" automatically:
- JE: Dr 211020 Payable to Subcontractors (partner = subcontractor, analytic = project), Cr Bank
- Same SQL query in `subcontractor_ipc.py` captures this without any changes to IPC code

### Petty Cash Subcontractor Advance — Consolidation

#### Removed Field (UI only, model retained)
- `advance_payable_account_id` hidden from view — replaced by `expense_account_id`

#### Flow After Change
- Checking `is_subcontractor_advance` → auto-fills **`expense_account_id`** to "Payable to Subcontractors" (211020)
- `_create_journal_entry()`: when `is_subcontractor_advance`, stamps `advance_subcontractor_id` as `partner_id` on the debit line
- JE: Dr `expense_account_id` (partner = subcontractor), Cr Petty Cash → IPC picks up automatically
- Domain on `expense_account_id` is conditional: `liability_payable` accounts when `is_subcontractor_advance`, normal site-scoped expense filter otherwise

### Journal Entries — Due Date & Payment Terms Removed

#### View (`vendor_bill_views.xml`)
- `invisible` condition on `div[@name='due_date']` extended from `move_type == 'out_invoice'` to `move_type in ('out_invoice', 'entry')`
- Same for the label div `div[hasclass('o_td_label')][label[@for='invoice_date_due']]`
- Both Due Date and Payment Terms now completely hidden on Journal Entry form for all users

#### Backend (`account_move_line.py` — `_check_payable_receivable`)
- Extended exempt move types from `('out_invoice', 'out_refund')` to `('out_invoice', 'out_refund', 'entry')`
- Journal entries may now freely use payable/receivable accounts without a due date
- Vendor bills retain the full constraint (due date still required there)
- Customer invoices retain the "no payable account on sale doc" guard

### Commits This Session (oldest → newest)
```
4f9d777  Feat: employee presence — kanban Online/Offline label + History tab audit trail
5966553  Feat: vendor payment expense account + petty cash subcontractor flow overhaul
82423b9  Fix: journal entries — remove Due Date & Payment Terms; suppress due-date constraint
ad99bdc  Feat: employee presence — 'Online From' label + Duration column in history
6f1cb83  Fix: employee kanban — accurate online detection via 2-min heartbeat threshold
```

---

## Session Notes — 2026-07-09 (third session)

### IPC Payment Tracking — Removed Custom GL Accounts

#### What changed
- Removed custom accounts "Payable to Suppliers" (211010) and "Payable to Subcontractors" (211020) from `account_configuration_data.xml` — user has their own imported chart of accounts
- Removed `_onchange_category_auto_payable` from `res_partner.py` (was auto-assigning those accounts by partner category)
- Removed `_onchange_partner_fill_expense_account` from `account_payment.py` (was auto-filling from partner's payable account)
- `x_expense_account_id` on `account.payment` is now fully optional — selecting any account (or none) does not affect IPC tracking

#### IPC query — now uses `account.payment` directly (not GL)
**File:** `site_operations/models/subcontractor_ipc.py` → `_compute_payments_made` and `_onchange_prefill_ho_advance_recovery`

```sql
-- Bank / vendor payments
SELECT COALESCE(SUM(ap.amount), 0)
FROM account_payment ap
WHERE ap.partner_id = %s
  AND ap.payment_type = 'outbound'
  AND ap.state IN ('in_process', 'paid')   -- Odoo 19: NOT 'posted'
  AND ap.date <= %s
  AND ap.x_destination_project_id = %s

-- Petty cash subcontractor advances
SELECT COALESCE(SUM(pce.amount), 0)
FROM x_petty_cash_expense pce
WHERE pce.advance_subcontractor_id = %s
  AND pce.is_subcontractor_advance = true
  AND pce.state = 'posted'
  AND pce.expense_date <= %s
  AND pce.project_analytic_account_id = %s
```

**Key Odoo 19 gotcha:** `account.payment.state` is `'in_process'` or `'paid'` — NOT `'posted'`. The underlying `account.move.state` is `'posted'` but that's a different table. Always use `IN ('in_process', 'paid')` when querying `account_payment` directly.

### Employee Presence — Binary Online/Offline Only

#### Removed: Away state
- `x.employee.presence.log` status selection now only has `('online', 'Online')` and `('offline', 'Offline')` — no 'away'
- `mail_presence_ext.py` no longer logs 'away' transitions; Odoo's 'away' (idle browser tab) is treated identically to 'online'
- History tab decorations updated: green = Online, grey = Offline — no yellow/warning

#### Current presence detection logic
| Signal | Source | Action |
|---|---|---|
| Browser tab open | `mail.presence.write(status='online'/'away')` | If was offline → write 'offline' entry at last_poll, then 'online' entry |
| Browser tab closes | Nothing (Odoo is never notified) | Cron detects stale last_poll every 2 min, writes 'offline' at last_poll time |
| View employee form/kanban | `_compute_x_presence_info()` | `last_poll >= now - 65s` → Online; else → Offline + "Last seen: [last_poll]" |

#### `ONLINE_THRESHOLD_SECONDS = 65`
Matches Odoo's own `DISCONNECTION_TIMER = UPDATE_PRESENCE_DELAY + 5 = 65`. Defined in both `hr_employee_ext.py` and `mail_presence_ext.py`.

### Menu Rename
- **Accounting → Customers → Payments** renamed to **Receipts**
- Override in `views/menus.xml`: `<record id="account.menu_action_account_payments_receivable" model="ir.ui.menu"><field name="name">Receipts</field></record>`

### Commits This Session (oldest → newest)
```
13c1b5f  Fix: remove custom GL accounts + decouple IPC payment tracking from account type
faf6559  Fix: employee online presence — reliable last_poll-based detection + cron offline log
5354846  Fix: IPC payments query — use state IN ('in_process','paid') for Odoo 19
214276e  Fix: employee presence — binary Online/Offline only, remove Away state
2ee5e7e  Rename: Accounting > Customers > Payments → Receipts
```

### Pending — odoosh-push blocked
All commits are **local only**. Enable **AI Code Push** in Odoo.sh project settings, then run `odoosh-push`.

---

## Session Notes — 2026-07-15

### Petty Cash Release — GL Account Not Hit (Production Bug)

#### Root Cause
`destination_account_id` on `account.payment` is a **computed stored field** in Odoo 19. `action_release()` set it to the site's Cash in Hand account immediately after `Payment.create()`, but every time Finance HO edited the draft payment (changed payment method Manual → Checks, added bank allocations, changed journal), Odoo **re-computed it back to the default AP/payable account**. The JE therefore debited AP instead of the petty cash GL account — so account 112634 "Cash at MCH Bahawalnagar" remained at 0.

#### Pattern: Never rely on destination_account_id being stable on a draft payment
Any FO edit after creation re-triggers `_compute_destination_account_id`. The only safe place to enforce the petty cash account is **at JE creation time** in `_prepare_move_line_default_vals()`.

#### Files changed

**`models/account_payment.py`** — `_prepare_move_line_default_vals()`:
- When `x_petty_cash_request_id` is set, look up the fund's `_get_petty_cash_account()`
- Identify the bank/outstanding line by `outstanding_account_id` or `journal.default_account_id`
- Override the counterpart line's `account_id` to the petty cash account
- This fires right before Odoo finalises JE lines — regardless of what `destination_account_id` was at that moment

**`models/petty_cash.py`** — `action_release()`:
- Added `UserError` if no petty cash account is configured: "Please go to Configuration → Site Configurations → [site] and set the Petty Cash Account (Cash in Hand) field"
- `destination_account_id` is still set as a convenience UI hint for Finance HO — no longer the authoritative mechanism

**`models/petty_cash.py`** — new `action_fix_petty_cash_entry()` on `x.petty.cash.request`:
- Creates a correcting journal entry for already-posted payments that hit the wrong account
- JE: Dr Cash in Hand (petty cash account) / Cr wrong account (whatever was incorrectly debited)
- Identifies counterpart line by excluding `outstanding_account_id` and `journal.default_account_id`
- Posts automatically and logs to chatter

**`views/petty_cash_views.xml`**:
- "Fix GL Entry" button on PCR form — visible when state in ('released', 'confirmed') and payment_id is set
- Restricted to Finance HO + Matracon Admin groups
- Has confirm dialog to prevent accidental clicks

#### Production fix for PCR/MCH/00001 (PAY00001)
1. Go to **Configuration → Site Configurations → MCH - BAHAWALNAGAR** → set "Petty Cash Account (Cash in Hand)" to account **112634 Cash at MCH Bahawalnagar**
2. Open **PCR/MCH/00001** → click **"Fix GL Entry"** → confirm
3. A correcting JE is posted: Dr 112634 / Cr wrong account → balance on 112634 becomes +1,550,000

#### Commit
```
abf8fa2  Fix: petty cash release — enforce Cash-in-Hand account on JE at posting time
```

---

## Session Notes — 2026-07-20

### PO / RFQ PDF Letterhead — Final Fix

#### Files changed
- `purchase_demand_raise/report/final_po_report.xml` — paper format `margin_top` 45 → 50mm
- `purchase_demand_raise/report/final_po_report_template.xml` — complete rewrite with standalone `matracon_po_layout` template
- `purchase_demand_raise/report/rfq_report_template.xml` — complete rewrite, reuses `matracon_po_layout`

#### `matracon_po_layout` template (standalone)
Defined in `final_po_report_template.xml`. Provides three divs consumed by Odoo's `_prepare_html()`:

| Div class | Content | Renders as |
|---|---|---|
| `header` | Logo + "MATRACON PAKISTAN (PVT) LIMITED" + 2.5px navy divider | wkhtmltopdf `--header-html` (repeats every page) |
| `article` | CSS block + `<t t-out="0"/>` | wkhtmltopdf main body, wrapped in `minimal_layout` |
| `footer` | Thin line + Head Office + Regional Office | wkhtmltopdf `--footer-html` (repeats every page) |

**Critical design notes:**
- `inherit_id` attribute must be **omitted** (not `inherit_id="False"`) — the string `"False"` is parsed as an external XML ID (`purchase_demand_raise.False`) → `ValueError` crash on module update
- `article` div `padding-top: 16mm` — wkhtmltopdf has an invisible zone of ~14mm at the start of the article body due to `html { height: 0 }` + `body { overflow: hidden }` in `minimal_layout`. Without 16mm padding the ref-bar (PO#/date) is invisible
- All vendor/meta layout uses `<div>` elements, NOT `<table>` — Bootstrap's `report_assets_common` CSS adds unwanted cell borders to any `<table>` that doesn't have `.table` class
- Article `<style>` block has `td, th { border: none !important }` global reset; data tables `.po-table` and `.rfq-table` restore borders via higher specificity `.po-table td { border: 1px solid #ccc !important }`

#### Root causes resolved

| Symptom | Root cause | Fix |
|---|---|---|
| Module update crashed (`ValueError: External ID not found: purchase_demand_raise.False`) | `inherit_id="False"` on `<template>` shorthand is parsed as literal XML ID "False" | Remove `inherit_id` attribute entirely for standalone templates |
| PO# / date ref-bar invisible | wkhtmltopdf article body invisible zone ~14mm from top | `padding-top: 16mm` on article div |
| Vendor company name invisible | Same invisible zone (content at 4-11mm clipped) | 16mm padding pushes all content past invisible zone |
| Unwanted borders on vendor/meta tables | Bootstrap `report_assets_common` CSS adds borders to non-data tables | Switch vendor block + RFQ meta block from `<table>` to `<div>/<span>` layout |
| Product table lost dark navy styling | `<style>` block was deleted in a prior iteration | Restored `.po-table` / `.rfq-table` CSS in article `<style>` block |

#### Commits (oldest → newest)
```
cf44c3e  Fix: PO/RFQ — date and vendor name invisible due to Bootstrap overriding div display:table
870d1f1  Fix: PO signature block — remove duplicate cursive name fallback
2dd3ee2  Fix: PO/RFQ header/footer — use Odoo header/footer/article div mechanism
612d77b  Fix: PO/RFQ — blank lines, print-date, watermark centering; unbind standard Odoo reports
3e6fa72  Fix: move print/direct-print to list view, remove from form headers
92e5fef  Feat: BG export PDF + direct-print buttons for vouchers
d67bf22  Fix: PO/RFQ letterhead — all content visible; clean meta/vendor blocks
```

#### Pending — odoosh-push blocked
All commits are **local only**. Enable **AI Code Push** in Odoo.sh project settings, then run `odoosh-push`.

---

## Session Notes — 2026-07-25

### Petty Cash Credit Account Bug Fix

#### Root Cause
`x_petty_cash_account_id` on `x.petty.cash.expense` is `readonly="1"` in the view for site accountants. Odoo never sends readonly field values back on form save, so the value set by `default_get` was discarded — the field was always `NULL` in the DB. `_create_journal_entry()` then silently returned (`return`) with no JE created → GL never touched → petty cash balance never decreased.

#### Files Changed

**`site_operations/views/petty_cash_views.xml`**
- Collapsed the two group-conditional `x_petty_cash_account_id` entries (SA + HO/Finance) into a single `readonly="1"` `force_save="1"` field for ALL users
- `force_save="1"` forces Odoo to include the readonly value in the save payload

**`site_operations/models/petty_cash.py`** — `create()`
- Auto-fills `x_petty_cash_account_id` from `fund._get_petty_cash_account()` when client doesn't send it
- Defensive layer independent of view attributes

**`site_operations/hooks.py`** — `fix_petty_cash_expense_accounts()`
- New function called from `post_migrate_hook`
- Step 1: fills `x_petty_cash_account_id` on all existing expenses where it's NULL
- Step 2: creates missing JEs for posted expenses with `x_account_move_id = NULL`
- JEs dated to original `expense_date` (not today) so historical records are correct
- Fund balance auto-corrects (computed from GL)
- **This runs automatically on `odoosh-push`** — no manual intervention needed for the ~100 production entries

**`site_operations/models/stock_picking.py`**
- Removed `_check_duplicate_asset_issuance()` call from `button_validate()` — re-issuing same asset to same contact no longer blocked

#### Commits This Session
```
8afd7cf  Fix: petty cash credit account lost on save + remove duplicate-issuance block
08bae06  Fix: petty cash credit account — fully readonly for all users
```

### Push Strategy
All commits pushed to Development branch on 2026-07-25 via `odoosh-push`.
Remote: `cecf525..c9e8c9e → Development`

To promote to Staging/Production: use the Odoo.sh dashboard to merge Development → Staging → Production.

---

## Session Notes — 2026-07-25 (continued)

### Petty Cash Admin Wizard — Manual Fix Trigger

#### What was added
The 3-step petty cash fix (`fix_petty_cash_expense_accounts`) previously only ran
automatically on module upgrade (via `post_migrate_hook`). A dedicated admin UI is now
available so the fix can be triggered on demand without upgrading.

#### New: `x.petty.cash.admin.wizard` (TransientModel)
- **File:** `site_operations/models/petty_cash.py` (appended at end)
- Single method `action_run_fix()` — calls the same `fix_petty_cash_expense_accounts(env)`
  as the hook. Group-restricted to `group_matracon_admin` or `base.group_system`.
- Returns a sticky success notification.

#### New form view + window action
- **File:** `site_operations/views/petty_cash_views.xml`
- `view_petty_cash_admin_wizard_form` — modal form with:
  - Blue info box describing all 3 steps
  - Yellow warning reminding admin to set the Petty Cash Account on Site Config first
  - **"Run Fix Now"** button with a confirmation dialog
  - "Close" cancel button
- `action_petty_cash_admin_wizard` — `target="new"` (opens as dialog)

#### New menu path
```
Accounting → Petty Cash → Configuration → Fix Petty Cash Accounts
```
Visible only to **Matracon Admin** and **System Administrator**.

#### ACL added
- `access_petty_cash_admin_wizard_admin` → `group_matracon_admin` (CRUD)
- `access_petty_cash_admin_wizard_system` → `base.group_system` (CRUD)

#### Commit
```
c9e8c9e  Feat: petty cash admin wizard — manual fix trigger under Petty Cash → Configuration
```

### All Commits Pushed to Development (2026-07-25)
51 commits total pushed via `odoosh-push`. Remote tip: `c9e8c9e` on branch `Development`.
All previous session commits (July 9–25) are now in the remote. Development branch is
fully up-to-date.

---

## Session Notes — 2026-07-30

### Development Build — DB Not Initialized (500 Error)

#### What happened
After the last `odoosh-push`, the Odoo.sh development container was rebuilt with a **fresh empty database**
(normal for dev branches). However, the automated DB initialization (`install.log`) was 0 bytes — it silently
failed to run. Odoo started with no `ir_module_module` table → every HTTP request threw `KeyError: 'ir.http'`
→ 500 Internal Server Error on the site.

#### Fix applied (manual, this session)
```bash
odoo-bin -i base --stop-after-init --no-http
odoo-bin -i purchase_demand_raise,site_operations --stop-after-init --no-http
```
Both completed with zero errors. The background server restarted via socket activation and returned
`303` (redirect to login) — site is healthy.

#### Non-critical warning observed
```
hr.employee.hr_icon_display: selection=... overrides existing selection; use selection_add instead
```
Source: `mail_presence_ext.py` overrides the `hr_icon_display` field selection entirely instead of
using `selection_add`. Non-fatal — field still works. Fix in a future commit if needed.

#### Key lesson
On a fresh Odoo.sh dev build, if the site shows 500 immediately after a push, check `install.log`
(size 0 = init didn't run). Run the two `odoo-bin -i` commands above manually to recover.

### Commits This Session
```
(docs only — no code changes)
```

---

## Session Notes — 2026-09-22

### New Module: `account_counterpart_column` (v1.0.0)
Adds an "Offsetting/Counterpart Account" column to the **General Ledger** report (Accounting >
Reporting > Ledgers > General Ledger) and, hidden-by-default via the optional-columns toggle, to
the **Journal Items** list view (covers the Trial Balance drill-down too, since it opens the same
base view). Shows which account received the opposite debit/credit side of the same journal entry
— comma-joined if the entry has more than 2 lines. Trial Balance's own report grid is untouched
(it's account-aggregated, no single line to attach a counterpart to).

- `models/account_move_line.py` — `_get_counterpart_account_display()`: one batched SQL query
  (never N+1) shared by both surfaces; resolves account code/name via the ORM (not raw SQL) so
  multi-company `code_store` resolves correctly.
- `models/account_general_ledger.py` — extends `AccountGeneralLedgerReportHandler
  ._report_custom_engine_general_ledger()`. **Gotcha**: return `None`, not `False`, for "no
  counterpart" (account-subtotal / grand-total rows) — the report's `_format_value()` renders
  `None` as blank but does `str()` on `False`, producing a literal "False" in the cell.
- `views/account_move_line_views.xml` — adds the field as `optional="hide"` on the shared
  `account.view_move_line_tree`, so it flows into every view that inherits it without touching any
  of them individually.

### New Module: `matracon_admin_tools` (v1.0.0) — Centralized "Fix Tools" Screen
**Menu: Settings → Fix Tools.** Consolidates every ad-hoc "fix"/"rebuild"/data-repair Server Action
that had accumulated as one-off buttons on individual model views into one screen. Reuses the
native `ir.actions.server` model (two new fields: `x_is_fix_tool` boolean, `x_fix_tool_description`
text) rather than a parallel model — every entry is a real Server Action, runnable via `.run()`.

#### Access control design
Each tool keeps the **exact same security groups** it had at its old button/menu location, via
`ir.actions.server`'s native `group_ids` field (e.g. the petty-cash GL fix stays Finance HO +
Matracon Admin; everything else stays Matracon Admin + System Administrator — nobody gained or
lost capability). Two problems this created and how they were solved:
- Finance HO / Matracon Admin had **zero** pre-existing ACL access to `ir.actions.server` (core
  ACL only grants `base.group_system`) — new read-only ACL rows added for both groups.
- A blanket grant would expose them to every OTHER Server Action in the system (automations,
  technical config). Fixed with one global `ir.rule` (`security/ir_rule.xml`) whose domain is
  `['|', ('x_is_fix_tool', '!=', True), '|', ('group_ids', '=', False), ('group_ids', 'in',
  user.all_group_ids.ids)]` — non-fix-tool rows are *always* allowed (so nothing else in the
  system is ever restricted by this rule, for anyone), fix-tool rows are scoped by `group_ids`.
- **Odoo 19 gotcha**: `res.users.groups_id` was renamed to `group_ids` (and `all_group_ids` added
  for implied groups) — `user.groups_id` in a domain raises `AttributeError`.
- **`ir.actions.server.run()` gotcha**: `_can_execute_action_on_records()` checks `group_ids`
  against the *calling user's actual group membership* — `self.sudo()` on the action record does
  NOT bypass this. Verified empirically: a user in neither group is cleanly denied; a user in
  either permitted group runs it; a user with zero ACL on the model gets denied earlier, at ACL
  level, with no rule involved.
- **Code-field gotcha**: `state='code'` actions only return whatever gets assigned to a variable
  literally named `action` — a bare `model.method()` statement discards the method's return value
  (its notification dict). Two migrated entries had this exact bug already in production (silently
  swallowing their own success notification); fixed by writing `action = model.method()` when
  editing those two records here — a **notification-visibility fix**, not a logic change.

#### Migrated (9 tools)
| Tool | Was at | Now |
|---|---|---|
| Rebuild Site Analytics | JE list → Action menu | Unbound (`binding_model_id` cleared), flagged in place |
| Fix Petty Cash Accounts | Petty Cash Admin wizard button **+** PCE list → Action menu (2 duplicate entry points, same routine) | One entry, unbound from the list menu |
| Fix GL Analytic Distribution on Payment Entries | Petty Cash Admin wizard's 2nd button | Method relocated (unchanged) from the retired wizard to `account.payment` |
| Fix Petty Cash GL Entry | Button on `x.petty.cash.request` form | New `x.petty.cash.gl.fix.wizard` (pick request → run) |
| Fix Old Inter-Project Entries / Fix Old Issuances / Fix Analytic Visibility | 3 buttons on Site Configuration form | One shared `x.site.config.fix.wizard` (pick site + fix type via context default → run), 3 list entries |
| Unlink GL (Fleet) | Button on Fleet Service Log form | New `x.fleet.gl.fix.wizard` (pick log entry → run) |
| Change Product UoM | Inventory → Configuration menu | Same wizard/action reused, just opened from here (`_get_action_dict()`) |

`x.petty.cash.admin.wizard` (TransientModel, its view, window action, menu, and ACL rows) was
**fully retired** — it took zero input fields, so both its buttons became direct Server Actions
with no functional loss. Confirmed via `-u` that Odoo's module-update cleanup fully purged its
stale `ir.model`/`ir.model.fields`/`ir.ui.menu`/`ir.actions.act_window`/`ir.model.access` rows —
no manual cleanup needed.

#### Left alone (deliberately, per user decision)
Three role-gated but *routine* business actions were reviewed and left at their original
locations, not treated as one-off fix tools: IPC "Regenerate Journal Entry" (Finance HO), Liability
Sheet "Force Reset to Draft", and the payment/batch "Reverse" actions. These are everyday
operational tools restricted by role, not ad-hoc data-repair patches.

### Push Status
Both new modules (`account_counterpart_column`, `matracon_admin_tools`) and all edits to
`site_operations` / `matracon_fleet` are **local only** — not yet pushed via `odoosh-push`.

---

## Session Notes — 2026-09-23

### Petty Cash Expenses — Missing Memo on Cash-in-Hand (Credit) Line

#### Root cause
`x.petty.cash.expense._create_journal_entry()` (`site_operations/models/petty_cash.py`) always
hardcoded the credit (Cash-in-Hand) line's label to a generic `"Cash in Hand — <site>"` string,
identical on every posted expense regardless of what it was for. The debit line was never the
problem — it already correctly used `line.name` (the per-line "Description" field). General
Ledger and Partner Ledger display the per-**line** `account_move_line.name`, not the move-level
`ref` — so browsing the Cash-in-Hand account showed no real memo on the credit side, unlike a
manually-typed Journal Entry where both sides normally carry a real description. Confirmed
empirically via `odoo-bin shell` before touching any code.

#### Fix (new entries)
`_create_journal_entry()` now computes the credit line's label the same way a manual JE would
read on both sides of a simple transaction:
- **Single-line voucher** → same description as that one debit line.
- **Multi-line voucher** → no single line represents the whole consolidated credit, so it falls
  back to the voucher's own `name` (Voucher Title / memo).

#### Backfill (existing entries) — new Fix Tool
`x.petty.cash.expense.action_backfill_credit_line_memo()` — corrects already-posted JEs whose
credit line still carries the old generic label. Same safe in-place SQL pattern already
established by `fix_petty_cash_expense_accounts()`'s Step 3 (hooks.py): direct `UPDATE
account_move_line SET name = ...` scoped to `credit > 0` lines still matching the old
`'Cash in Hand — %'` pattern — label-only, no amounts/accounts/reconciliation touched, safe on
posted lines, idempotent (re-running reports 0 fixed once done; already-corrected or
manually-edited labels are left alone).

Registered in the centralized **Settings → Fix Tools** screen (`matracon_admin_tools`, see prior
session) as **"Fix Missing Memo on Petty Cash Journal Entries"**, sequence 25 — *not* as a
standalone button on the Petty Cash Expenses view, per the screen's own stated purpose. Same
`group_ids` restriction as the other petty-cash fix tools (Matracon Admin + System Administrator).

#### Verified via `odoo-bin shell` (all test data cleaned up afterward)
- New single-line expense → debit and credit lines both show the real expense description.
- New multi-line expense → debit lines keep their own descriptions; credit line shows the
  voucher title.
- Backfill on an old-style entry → credit line corrected from the generic label to the real
  memo; re-running reports 0 fixed (idempotent); already-correct new entries left untouched.
- Confirmed a same-transaction ORM-cache staleness read is not a real bug (a fresh process /
  post-commit read shows the corrected value immediately — direct SQL writes just don't
  auto-invalidate an already-loaded recordset's cache within the same transaction).

#### Areas reviewed, not touched
Partner Ledger reads the same `account_move_line.name` field, so it benefits from this fix
automatically wherever a petty cash JE line has a `partner_id` (subcontractor-advance lines) —
no separate Partner Ledger-specific change needed. Regular (non-advance) expense lines have no
`partner_id` and correctly don't appear in Partner Ledger at all, unaffected by this change. The
`account_counterpart_column` report (prior session) and Trial Balance were reviewed and are
unaffected — neither reads `account_move_line.name` for its own logic.

---

## Session Notes — 2026-09-23 (continued)

### Site-Wise / HO-Wise Sequential Numbering — Customer Invoices, Vendor Bills, Vendor Payments, Journal Entries

**Format:** `[DOC_TYPE]/[SITE_CODE]/[YEAR]/[SEQUENCE]` — e.g. `INV/MCH-BHW/2026/000001`,
`BILL/HO/2026/000001`, `PAY/STP-MRD/2026/000001`, `JE/RWASA/2026/000001`. `DOC_TYPE` is
`INV`/`BILL`/`PAY`/`JE`. `SITE_CODE` is the project analytic account's own `code` field (the
same one used everywhere else for site scoping — e.g. `MCH-BHW`, `STP-MRD`) or literal `HO` when
the move has no project. `SEQUENCE` is 6 digits, resets each calendar year.

**File:** `site_operations/models/account_move.py` — extends the *existing* (previously
out_invoice-only) `_get_starting_sequence()` override, generalized to all 4 doc types, plus a
new `_get_last_sequence_domain()` override. Both are Odoo's own sanctioned `SequenceMixin`
extension points (`account.move` inherits `sequence.mixin` from core) — no new `ir.sequence`
records, no new journals, no hardcoded string concatenation at create time.

#### Why journal_id-scoping (the base default) had to be dropped for in-scope docs
Odoo's own `_get_last_sequence_domain()` scopes "find the previous number" by `journal_id`. Journal
Entries in this system are posted through *several* different journals (bank/cash journals, the
Miscellaneous journal, ...). Keeping that scoping would give each journal its own independent
000001, 000002... counter and produce **duplicate numbers across journals for the same site**.
Fix: override `_get_last_sequence_domain()` to scope by (doc type, site, calendar year) instead,
spanning every journal in the company. Verified empirically: a site's Journal Entries posted
through two different journals (bank, then misc) correctly continue as 000001 → 000002, not
000001 in each independently.

#### Two more bugs found and fixed via empirical testing (not visible from reading the code)
1. **Site interleaving must not reset the counter.** Two different sites' invoices created
   back-to-back in the *same* shared journal must each keep incrementing their own counter, not
   restart. Verified: MCH → STP → MCH correctly continued MCH's own count.
2. **New-format rows can "poison" both directions if they share a journal with pre-existing
   old-format or out-of-scope rows**, because the underlying regex-based lookup finds "the most
   recently inserted row in this journal" and tries to continue *its* prefix, regardless of
   whether that prefix means anything to the record currently being numbered:
   - An HO-scoped new invoice initially picked up a *pre-existing* old-format invoice's prefix
     (both have `x_project_analytic_account_id IS NULL`) and continued the *old* numbering
     thread instead of starting fresh.
   - A **customer payment** (out of scope — Vendor Payments only per this task) sharing a bank
     journal with an in-scope vendor payment picked up the vendor payment's *new*-format prefix
     and continued *that* counter instead of falling back to standard behaviour.
   - **Fix:** a symmetric regex guard on `name` — in-scope lookups require the candidate row to
     already match the new 4-segment shape (`^[^/]+/[^/]+/[0-9]{4}/[0-9]+$`); the fallback path
     for out-of-scope records requires the candidate to **not** match it. Each numbering universe
     is now blind to the other's rows, however they happen to share a journal.
3. **`account.payment`'s own fields are not reliably readable via a plain attribute access** at
   the exact point `_get_starting_sequence()`/`_get_last_sequence_domain()` run (mid
   create/post flow) — `payment.payment_type`/`partner_type` and
   `payment.x_destination_project_id`/`x_fund_project_id` can reflect stale/default values.
   Fixed by flushing the payment's pending writes then reading `payment_type`/`partner_type` (and
   the destination/fund project, as a site-code fallback when the move's own
   `x_project_analytic_account_id` isn't stamped yet) directly via raw SQL — the same technique
   `sequence.mixin` itself uses before its own raw-SQL lookups.

#### Out of scope (deliberately, per user decision) — left completely untouched
Customer Payments/Receipts (inbound), refunds/credit notes (`out_refund`/`in_refund`), and any
other `entry` that isn't a plain JE or an outbound-to-supplier payment. `_matracon_sequence_doc_type()`
returns `None` for these and both overrides fall straight back to standard Odoo behaviour — verified
a refund still gets the old `RINV/26-27/NNNN` format untouched.

#### Historical backfill — new Fix Tool, manual-only
`account.move.action_matracon_backfill_sequence()` renumbers existing posted documents still on
the old format into the new scheme. Registered in **Settings → Fix Tools** as "Renumber Historical
Documents (Site-Wise Sequence)" — **never called from any hook/cron/automatic trigger anywhere**
(verified via a full-codebase grep: the only references are the Fix Tools XML record, the method
definition, and its own internal log line). Matracon Admin / System Administrator only.

- Skips (and reports by name) any document with a reconciled line or an `inalterable_hash`
  (hash-secured journal) — renaming those could break a reconciliation reference or an audit
  hash chain.
- New numbers continue from whatever is already the highest number in each (doc type, site,
  year) group, rather than reordering documents already created live under the new scheme —
  nothing already-correct is ever touched or renamed twice. Documents this run *does* renumber
  are still assigned oldest-first among themselves.
- Uses `write()`, not raw SQL, so Odoo recomputes the stored `sequence_prefix`/`sequence_number`
  correctly for future auto-numbering.
- Verified idempotent (a second run reports 0 renumbered, same skip list) and correct (21
  renumbered, 2 correctly skipped as reconciled, in this dev database) — **then fully reverted**:
  per the explicit "never run this except on deliberate click" instruction for this specific
  tool, every renamed document was restored to its original historical name immediately after
  verifying the method's logic in an isolated shell session, and all forward-numbering test
  documents created earlier in the same verification pass were deleted. The Fix Tools "Run"
  button itself was never clicked through the actual delivered screen.

#### Reports/views reviewed for consistency
Grepped for any parsing/regex/fixed-width assumption on `move.name` or `payment.name` across all
custom modules — none found; every reference is a plain display (`ipc.x_account_move_id.name` in
a notification, an error message, ...), not a structural assumption. No QWeb templates in the
custom modules reference `move.name` directly (the standard Odoo invoice/bill PDF templates
handle variable-length references natively). No changes needed there.

---

## Session Notes — 2026-09-23 (continued — signature automation, phase 1)

### Discovery phase → implementation of the approved decisions

A discovery pass first found that a real digital-signature system already existed
(`res.users.x_sign_signature` + shared macros in `site_operations/report/signature_block.xml`)
but was inconsistently wired, plus **three separate legacy signature mechanisms** side by side.
Full inventory/flow-mapping was published as an Artifact for review. This session implements the
5 decisions approved from that review (decision 2 — Attendance CEO gate — is on hold; decision 5
— IPC/Bank Guarantee approval — stays out of scope).

### 1. Macro consolidation (dead code removed)
`report_sig_4col_labels` in `signature_block.xml` had **zero callers** — Bank Payment Voucher and
Journal Entry Voucher both hand-copied their own blank inline markup instead of calling either
4-column macro. Removed `report_sig_4col_labels` entirely; both reports now call the
already-proven `report_sig_4col` (used successfully by Petty Cash Expense, Cheque Print, MIF, Gate
Pass, MTN).

### 2. Bank Payment Voucher / Journal Entry Voucher — Prepared By / Approved By wired
- **Bank Payment Voucher** (`account.payment`): Prepared By = `create_uid`; Approved By =
  `x_ceo_approved_by_id` (already existed on the model, just never reached the PDF).
- **Journal Entry Voucher** (`account.move`, used for both payment-derived and plain entries):
  Prepared By = `create_uid`; Approved By = `origin_payment_id.x_ceo_approved_by_id` — resolves to
  blank on a plain, non-payment journal entry (no new CEO-approval gate added there, per decision
  1 — scope stays to the two report types that already have an approval workflow).
- Checked By / Received By: untouched — no variable passed, same as before.

### 3. Purchase Requisition → RFQ → PO signature identities
**New fields on `purchase.order`** (same `x_ceo_approved_by_id` naming already used on Liability
Sheet/Petty Cash/Salary Sheet/Payment, for consistency):
- `x_ceo_approved_by_id` — stamped in `action_ceo_final_approve()`. Blank on a Site-Procurement PR
  that auto-bypasses CEO review (`x_ceo_status='approved'` with no real click) — correct, nobody
  actually approved it as CEO.
- `x_rfq_prepared_by_id` — whoever in Procurement actually processed the PR into an RFQ.
  First-write-wins, stamped at whichever happens first: `action_ho_approve()` (main PR flow — HO
  review is literally "Procurement processes it"), `x.comparative.statement
  .action_send_rfq_to_vendors()` (CS "Send RFQ to Vendors" button), or
  `purchase.order.action_rfq_send()` (native Odoo "alternative RFQ" direct-send path, also
  gated to block Site Store). Covers all three ways an RFQ actually gets sent/prepared in this
  codebase.

**RFQ report** (`rfq_report_template.xml`) had **no signature block at all** — added one, single
column, sourced from `x_rfq_prepared_by_id`. Kept self-contained (not a call into
`site_operations.report_sig_col`) — **`purchase_demand_raise` does not depend on `site_operations`,
site_operations depends on it**, so reaching the other way would be a backwards, fragile
cross-module reference (and could not resolve if site_operations were ever absent).

**PO report** (`final_po_report_template.xml`) CEO block — new priority order: (1) the actual
`x_ceo_approved_by_id` approver's own `x_sign_signature`, (2) `x.po.signature.config` uploaded
fallback image, (3) legacy CEO-group user search, (4) hardcoded name. Previously only had (2)-(4)
— the PO never showed the real approver's own signature, only a company-wide config or a search
for *any* signed-up CEO-group user. Title stays company-config/hardcoded (not a per-user
attribute). Verified end-to-end via `odoo-bin shell`: PO rendered with the actual approver's real
name after `action_ceo_final_approve`.

### 4. Comparative Statement `action_confirm` — security gap fixed
Found during discovery: this button had **no `groups=` restriction at all** — any user with basic
model write access could route a PR to CEO. Fixed both ways (view attribute alone only hides the
button, doesn't stop the method being called another way):
- View: `groups="...group_procurement_ho,...group_ceo_approval,...group_matracon_admin,base.group_system"`.
- Server-side: explicit `has_group` check inside `action_confirm()` itself, raising `UserError`.
- Verified: an unprivileged test user is cleanly denied; Procurement HO succeeds.

### 5. Salary Slip — fixed to match its own parent Salary Sheet
`report_sig_3col` was called with **zero variables set** — even Prepared By was blank, despite the
parent Salary Sheet report already correctly showing both Prepared By and CEO Approved By. Now
sources `sig_prepared_user` from the slip's own `create_uid` and `sig_approved_user` from
`line.sheet_id.x_ceo_approved_by_id` — same data the parent already uses.

### 6. Dead legacy signature fields removed
- `res.company` — `x_po_officer_name`, `x_po_officer_title`, `x_ceo_name`, `x_ceo_title`. Entire
  file (`purchase_demand_raise/models/res_company.py`) deleted — confirmed zero references
  anywhere in the codebase, not exposed in any view, module-update cleanup purged the stale
  `ir.model.fields` rows automatically.
- `x.po.signature.config` — `x_po_officer_name/title/signature` (defined, editable in
  `po_signature_config_views.xml`, but never read by any report). CEO fields on that same model
  are kept — genuinely used as the PO's fallback image/name when the approving CEO hasn't
  configured a personal signature. View updated: single "CEO (fallback)" section, help text
  reworded to state it's a fallback, not the primary source.

### Verified via `odoo-bin shell` (all test data cleaned up afterward)
- Site-Store-raised PR → submit (CS auto-created) → HO approve (`x_rfq_prepared_by_id` stamped) →
  RFQ PDF renders with the HO officer's real name **and an embedded signature image** → CEO final
  approve (`x_ceo_approved_by_id` stamped) → PO PDF renders with the approver's real name.
- HO-raised PR (bypasses HO review, routes straight to `ceo_final`) → CEO approve → PO PDF correct;
  confirms `x_rfq_prepared_by_id` correctly stays blank when there was no RFQ-processing step.
- Comparative Statement `action_confirm`: unprivileged user denied with the new error message;
  Procurement HO user's own permission check passes (a separate, pre-existing business-state
  check then correctly blocked reusing an already-`po_locked` test record — expected, not a bug).
- Bank Payment Voucher and Journal Entry Voucher PDFs render correctly with Prepared By populated.
- All test POs, the auto-created Comparative Statement, a test payment, and temporary
  `x_sign_signature` values set on real CEO/Procurement HO user accounts were fully cleaned up —
  environment restored to its pre-test state.

### Still pending (not in this phase)
- Decision 2 (Attendance Sheet CEO-approval gate) — on hold, client confirmation needed before any
  workflow change there.
- Decision 5 (Subcontractor IPC / Bank Guarantee approval steps) — explicitly out of scope.

---

## Session Notes — 2026-09-23 (continued — Board Resolutions app)

### New Standalone App: `board_resolutions` (v1.0.0)

Legal/governance documents (SECP authorizations etc.) — deliberately its **own top-level app**, not
folded into Settings or an existing app, restricted to `purchase_demand_raise.group_matracon_admin`
+ `purchase_demand_raise.group_ceo_approval` + `base.group_system` **only**. Security is enforced at
the ACL level (`security/ir.model.access.csv` grants zero rows to any other group) — the menu's
`groups=` is just a visibility convenience on top of that, not the actual boundary. Verified: an
unrelated user gets `AccessError` on both `search()` and `create()`.

#### Models
- `x.board.resolution` — `name` (reference), `meeting_date`, `meeting_venue` (defaults from the
  company address, editable), `subject`, `attendee_ids`, `clause_ids`, `ratification_text` (Html,
  boilerplate default), `authorized_person_id` (`res.partner`, optional — only for a clause
  authorizing a specific individual) + `authorized_person_cnic` (related, read-only, pulled from
  the partner's existing `x_cnic` field already used elsewhere in this codebase — not duplicated),
  `state` (draft/confirmed, matching every other document model's pattern here).
- `x.board.resolution.attendee` — `name`, `designation` (free-text Char, not a rigid Selection —
  "Director/CEO/Company Secretary/etc." is open-ended), `user_id` (optional, only to source a
  signature image — same "only if configured" rule used everywhere else in this system).
- `x.board.resolution.clause` — `sequence` + `text` (Html), numbered 1/2/3... in the report.

#### Reference numbering — why NOT a plain `ir.sequence`
The requirement is explicit: auto-suggest the next number, always stay editable, and after a
manual override **the next suggestion must follow that override**, not an untouched internal
counter. A raw `ir.sequence`'s own counter has no way to "notice" that a record's field was
hand-edited — it just keeps incrementing from wherever it already was. Implemented instead as a
small `_default_name()` that regex-finds the trailing number on the most recently created record's
own actual `name` value and increments it (`(\d+)(\D*)$`, preserves zero-padding width). This is
the same *category* of mechanism as Odoo's own SequenceMixin (used for invoice/PO/JE numbering
elsewhere in this system, see the 2026-09-23 site-wise-numbering session above) — purpose-built
lighter version here since the format has no year/month reset requirement (numbering is
**continuous, never resets**, confirmed with the user given the sample format "BR # 26/16" was
ambiguous about whether "26" meant a resetting fiscal year).
- First-ever resolution: no prior record to continue from → default is blank, Admin/CEO types the
  starting reference by hand, exactly as required.
- Verified: `BR # 26/16` → confirmed → next default computes `BR # 26/17`; manually overriding the
  first record to `BR # 26/50` correctly makes the *next* default `BR # 26/51`, not `26/17`.

#### Report — reuses `purchase_demand_raise.matracon_po_layout`
The polished PO/RFQ letterhead (logo + centered company name + navy divider + header ref-bar +
Head Office/Regional Office footer) is already a **generic, reusable** template — confirmed by
reading it: no PO-specific text baked in, just generic `po_ref_number`/`po_ref_date` variables the
caller sets. Reused here instead of duplicating letterhead markup a third time; this is why
`board_resolutions` depends on `purchase_demand_raise` (which nearly every custom module already
depends on anyway, for its core security groups). Also depends on `site_operations`, solely to
reuse `res.partner.x_cnic` — same precedent as `matracon_fleet`, which already depends on both.

Signature block: one cell per attendee, company stamp/seal (new `res.company
.x_board_resolution_stamp` field — a small addition to the standard Company form's own "Board
Resolutions" tab, Settings-level access like the rest of that form, deliberately **not** part of
the app's own restricted ACL) layered behind each attendee's own signature image (if their linked
user has one configured) via `position:absolute` CSS layering, printed name, and designation below.

**Gotcha hit and fixed**: `meeting_date` is a plain `Date` field, not `Datetime` — the PO
template's `context_timestamp(...)` helper (used there because `date_approve`/`date_order` ARE
Datetime) raises `AssertionError: Datetime instance expected` on a Date. Fixed by formatting
`meeting_date` directly with `.strftime()` — no timezone conversion needed for a pure date anyway.

#### Verified via `odoo-bin shell` (all test data rolled back, nothing persisted)
- Numbering: first-record blank default, correct auto-increment, correct pickup after manual
  override (see above).
- `action_confirm` blocks with a clear error when there are no attendees, blocks again with no
  clauses, succeeds once both exist.
- Full PDF (HTML render **and** the actual wkhtmltopdf binary generation) renders correctly with
  the real subject, attendee names, embedded signature image, clause text, and ratification text.
- Access control: an unrelated user gets `AccessError` on `search()` and `create()`; a Matracon
  Admin user can create/confirm normally.
