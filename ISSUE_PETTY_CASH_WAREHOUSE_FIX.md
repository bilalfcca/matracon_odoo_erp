# Issue Context File — Petty Cash + Warehouse ACL Fix
**Created:** 2026-07-25  
**For:** Next AI session if staging still not working  
**Tell AI:** "Read the file ISSUE_PETTY_CASH_WAREHOUSE_FIX.md in the project root for full context"

---

## Two Problems Being Fixed

### Problem 1 — Stock Warehouse Access Error (Site Store Keeper)
**Error on staging:**
> "Site Store Keeper - RWASA (id=7) doesn't have 'read' access to: Warehouse (stock.warehouse)"

**Root cause:** `group_site_store` had no ACL entry for `stock.warehouse` model.

**Fix applied:** `site_operations/security/ir.model.access.csv` — added line:
```
access_stock_warehouse_site_store,stock.warehouse site store,stock.model_stock_warehouse,purchase_demand_raise.group_site_store,1,0,0,0
```
**Commit:** `83c717e`

---

### Problem 2 — Petty Cash Expense Credit Account Empty + Wrong JE Account

**Symptoms:**
- Posted petty cash expenses have empty `x_petty_cash_account_id` (Petty Cash Account Credit field)
- Posted journal entries show "112631 Cash at HO" on the credit line instead of the site-specific account (e.g., "112634 Cash at MCH Bahawalnagar")
- Fund balance shows wrong because GL was never correctly credited

**Root cause (historical):** The field `x_petty_cash_account_id` is `readonly="1"` in the view. Odoo never sends readonly field values back on save, so the default value was silently dropped — field stayed NULL. `_create_journal_entry()` returned early when this was NULL → GL never touched.

**Fix already applied (earlier session):**
- `force_save="1"` added to view so Odoo sends the value even when readonly
- `PettyCashExpense.create()` now auto-fills from fund if client doesn't send it
- `post_migrate_hook` runs `fix_petty_cash_expense_accounts(env)` on every `-u site_operations`
- Commits: `36283f0`, `b1ffceb`

---

## The 3-Step Auto-Fix Logic (`hooks.py` → `fix_petty_cash_expense_accounts`)

Runs automatically on every `odoo-bin -u site_operations` AND on-demand via Action menu.

```
Step 1: Fill x_petty_cash_account_id
  - For every posted x.petty.cash.expense where x_petty_cash_account_id is NULL
  - Look up: expense.fund_id → fund.x_petty_cash_account_id
              → OR fund.site_config_id.x_petty_cash_account_id
  - UPDATE the field if found
  - Commits: 073a032, 0ef9fc9

Step 2: Create missing JEs
  - For posted expenses with x_account_move_id = NULL
  - Call expense._create_journal_entry() to create and post the JE
  - Fixes fund balance (it's computed from GL)

Step 3: Correct wrong credit account on existing JEs (SQL UPDATE, no reversal)
  - For posted expenses that DO have a JE
  - Compare JE credit line account vs site's configured petty cash account
  - If different → UPDATE account_move_line SET account_id = [correct_id]
  - Then invalidate ORM cache
  - Commit: 0ef9fc9
```

---

## On-Demand Admin Action (NEW — added this session)

**Commit:** `b83d405`  
**Where:** Petty Cash Expenses list view → Action menu → "Fix Petty Cash Accounts (Fill + Correct JEs)"  
**Who can run:** Matracon Admin or System Administrator only  
**What it does:** Calls `fix_petty_cash_expense_accounts(env)` — same 3 steps above  

**Files added/changed:**
- `site_operations/data/petty_cash_fix_action.xml` — NEW: `ir.actions.server` record
- `site_operations/models/petty_cash.py` — NEW method `action_admin_fix_petty_cash_accounts()`
- `site_operations/__manifest__.py` — added `petty_cash_fix_action.xml` to data list

---

## Key Files

| File | What it does |
|------|-------------|
| `site_operations/hooks.py` | `fix_petty_cash_expense_accounts(env)` — the 3-step fix |
| `site_operations/models/petty_cash.py` | `PettyCashFund._get_petty_cash_account()`, `PettyCashExpense.create()`, `action_admin_fix_petty_cash_accounts()` |
| `site_operations/models/account_account.py` | `x_site_ids` on journal + account; `x_petty_cash_account_id` resolution |
| `site_operations/data/petty_cash_fix_action.xml` | Server action XML |
| `site_operations/security/ir.model.access.csv` | Line 228: warehouse ACL for site store |
| `site_operations/views/petty_cash_views.xml` | `force_save="1"` on `x_petty_cash_account_id` |

---

## Site Configuration Prerequisite

For Step 3 to correct JEs, each site MUST have "Petty Cash Account (Cash in Hand)" set:

**Path:** Configuration → Site Configurations → [Site Name] → Petty Cash Account (Cash in Hand)

| Site | Account | Status |
|------|---------|--------|
| MCH - Bahawalnagar | 112634 Cash at MCH Bahawalnagar | ✅ Set (July 15 session) |
| RWASA | TBD | ❓ Must check |
| Others | TBD | ❓ Must check |

If this field is empty for a site → Step 3 skips it silently → JEs on that site remain wrong.

---

## Current Commit State (as of 2026-07-25)

```
b83d405  Feat: petty cash admin action — fix credit accounts + correct JEs on demand  ← LATEST
83c717e  Fix: grant stock.warehouse read to group_site_store
0ef9fc9  Fix: petty cash hook Step 3 — in-place SQL correction (no reversal entries)
073a032  Fix: petty cash hook — 3-step auto-correction on every module upgrade
02db87e  Fix: stock.warehouse ACL + petty cash hook diagnostics
cf84f4e  Docs: CLAUDE.md — session notes 2026-07-25
```

All 6 commits are on the `Development` branch, pushed to both Odoo.sh remote and GitHub.

---

## Deployment Steps (What to Do on Odoo.sh)

### Step 1 — Verify Site Configurations
On Staging after build: Configuration → Site Configurations → fill Petty Cash Account for each site

### Step 2 — Merge Development → Staging on Odoo.sh
Odoo.sh auto-runs: `odoo-bin -u site_operations` → `post_migrate_hook` → 3-step fix

### Step 3 — Verify
- Open a previously-blank posted Petty Cash Expense
- `x_petty_cash_account_id` (Credit field) should now be filled
- Open its Journal Entry → credit line should show site-specific cash account (NOT "Cash at HO")

### Step 4 — If Still Not Fixed (manual trigger)
Go to Petty Cash Expenses list → Action → "Fix Petty Cash Accounts (Fill + Correct JEs)" → confirm

---

## Why Previous Hotfix Branches Failed (History)

User was creating hotfix branches from `origin/main` (GitHub commit `501e806`) which is **far behind** the Odoo.sh Production branch (`631e36a`). The `x_petty_cash_account_id` field doesn't even exist at `origin/main`. Switching staging to those branches lost the entire petty cash workflow.

**Correct approach:** Always branch from Odoo.sh `Production` branch, not `origin/main`.

---

## Odoo 19 Gotchas Discovered This Project

- `account.payment.state` values are `'in_process'` / `'paid'` (NOT `'posted'`)
- `ir.actions.server` has NO `groups_id` field in Odoo 19 → use Python group check inside method
- `inherit_id="False"` on `<template>` crashes — omit the attribute entirely for standalone templates
- `destination_account_id` on `account.payment` is re-computed on every draft edit — never rely on it post-creation
- `force_save="1"` needed on any readonly field that must be persisted in Odoo 17+
