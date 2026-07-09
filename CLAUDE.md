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
