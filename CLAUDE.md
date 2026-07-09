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

### New Field: `hr.employee.x_last_online`
- File: `site_operations/models/hr_employee_ext.py`
- Computed (store=False), reads `mail.presence.last_presence` via sudo
- Batched compute — single SQL query per recordset, safe for list views
- Shown on Settings tab > User group, `invisible="not user_id"`
- `mail.presence.last_presence` updates every ~60 s while browser tab is open

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
