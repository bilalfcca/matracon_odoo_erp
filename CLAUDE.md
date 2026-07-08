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
