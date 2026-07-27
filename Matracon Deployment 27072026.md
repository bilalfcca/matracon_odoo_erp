# Matracon Deployment 27072026

**Release Date:** 27 July 2026
**Environment:** Odoo 19 (Odoo.sh — Development → Staging → Production)
**Modules Affected:** `site_operations` (v1.7.6+), `purchase_demand_raise` (v1.4.4+)
**Prepared by:** Awais

---

## 1. Scope Summary

This deployment consolidates all changes developed between **09 July 2026 and 27 July 2026** across two sessions. It covers five major functional areas:

| Area | Type |
|---|---|
| Batch Vendor Payment System | New Feature |
| Purchase Requisition (PR) Cancel / Delete | Enhancement |
| PO / RFQ PDF Letterhead Redesign | Enhancement |
| Bank Payment Voucher (BPV) PDF Overhaul | Enhancement |
| Petty Cash Credit Account Bug Fix | Bug Fix |
| Material Issuance — 3rd Party & SA Review | Enhancement |
| Employee Online Presence | New Feature |
| Subcontractor IPC Payment Tracking | Overhaul |
| Contact Tag & Description Enforcement | Enhancement |
| Cheque Series Management | New Feature |
| Site User Contact Permissions — Read-Only | Security |

---

## 2. Database Updates

The following schema and data changes are applied automatically on module upgrade (`odoo-bin -u site_operations,purchase_demand_raise`). No manual SQL is required.

### New Fields
| Model | Field | Type | Purpose |
|---|---|---|---|
| `account.payment` | `x_expense_account_id` | Many2one → `account.account` | Payable account override on vendor payments |
| `account.payment` | `x_destination_project_id` | Many2one → `project.project` | Project tagging on payments |
| `account.payment` | `x_bpv_ref` | Char | BPV reference number (BPV-YY-MM-XXXXX) |
| `account.payment` | `x_account_title` | Char | Account title for cheque |
| `account.payment` | `x_cheque_number` | Char | Cheque number |
| `account.payment` | `x_bank_allocation_ids` | One2many | Multi-bank split allocations |
| `x.petty.cash.expense` | `is_subcontractor_advance` | Boolean | Flags petty cash line as subcontractor advance |
| `x.petty.cash.expense` | `advance_subcontractor_id` | Many2one → `res.partner` | Subcontractor receiving the advance |
| `x.subcontractor.ipc` | `payments_till_prev_ipc` | Float (computed) | Cumulative payments up to prior IPC date |
| `x.subcontractor.ipc` | `payments_till_this_ipc` | Float (computed) | Cumulative payments up to this IPC date |
| `x.subcontractor.ipc` | `payments_this_period` | Float (computed) | Period payment (auto-fills Payment Recovery) |
| `hr.employee` | `x_last_online` | Datetime (computed) | Last heartbeat timestamp from `mail.presence` |
| `hr.employee` | `x_is_currently_online` | Boolean (computed) | True when last heartbeat ≤ 65 seconds ago |
| `x.employee.presence.log` | *(new model)* | — | Audit log of Online/Offline transitions per employee |

### New Models
| Model | Description |
|---|---|
| `x.batch.payment` | Batch vendor payment header (multi-vendor, multi-bank) |
| `x.batch.payment.line` | Per-vendor line within a batch payment |
| `x.bank.allocation` | Per-bank split within a single payment |
| `x.employee.presence.log` | Employee online/offline history log |
| `x.petty.cash.admin.wizard` | Transient wizard for admin petty cash fix |

### Post-Migrate Hook
`post_migrate_hook` in `hooks.py` automatically runs `fix_petty_cash_expense_accounts()` on upgrade:
- **Step 1:** Fills `x_petty_cash_account_id` on all expense lines where it is NULL
- **Step 2:** Creates missing journal entries for posted expenses with no linked JE
- **Step 3:** Dates historical JEs to the original `expense_date` (not today)

No manual intervention is needed for existing petty cash data.

---

## 3. Module Adjustments

### `purchase_demand_raise`
- `security/ir.model.access.csv` line 57: `res.partner` for `group_site_store` — perm_write and perm_create changed from **1 → 0**
- Paper format `paperformat_matracon_po`: margin_top=4mm, margin_bottom=4mm, DPI=96
- PO report template rewritten: standalone `matracon_po_layout` with proper header/article/footer separation
- RFQ report template rewritten: shares `matracon_po_layout`
- PR cancel/delete logic: `button_cancel()` and `_unlink_if_cancelled()` overridden
- T&C template field: now full-width (colspan=2) in PO form view

### `site_operations`
- `security/ir.model.access.csv` line 235: `res.partner` for `group_site_accountant` — perm_write and perm_create changed from **1 → 0**
- New models registered in `__manifest__.py`: `x.batch.payment`, `x.batch.payment.line`, `x.bank.allocation`, `x.employee.presence.log`, `x.petty.cash.admin.wizard`
- New views added: batch payment form/list, BPV PDF, employee presence history tab, petty cash admin wizard
- New menus added: Accounting → Vendors → Batch Payments; Petty Cash → Configuration → Fix Petty Cash Accounts
- `mail.presence` extended: `create()` and `write()` hooked to log Online/Offline transitions
- `account_payment._prepare_move_line_default_vals()` extended: enforces petty cash GL account and expense account override
- `subcontractor_ipc._compute_payments_made()`: now reads `account.payment` directly (state IN `in_process`, `paid`) instead of GL ledger
- Accounting → Customers → Payments menu renamed to **Receipts**

---

## 4. Processes Changed / Added

### 4.1 Batch Vendor Payment (NEW)
**What it does:** Finance HO can now pay multiple vendors in a single operation, generating one consolidated journal entry and one Bank Payment Voucher PDF covering all vendors.

**Flow:**
1. Finance HO goes to **Accounting → Vendors → Payments → New** → opens Batch Payment form
2. Selects **Project** (propagates to all lines), **Payment Date**, **Bank Journal**
3. Adds vendor lines — each line carries: Vendor, Amount, Memo, WHT amount, Cheque number, Account Title, and optional Fund Allocation (source project + available balance)
4. Clicks **Post** → system creates a multi-debit/multi-credit journal entry and auto-generates BPV reference (BPV-YY-MM-XXXXX)
5. BPV PDF can be printed directly from the batch payment form

### 4.2 PR Cancel & Delete (ENHANCED)
**What changed:** Previously, PRs in `submitted` or `rejected` state could not be cancelled, and rejected PRs could not be deleted.

**New behaviour:**
- A PR in **Submitted** or **Rejected** state can now be cancelled using the **Cancel** button
- A PR in **Cancelled** or **Rejected** state can now be **deleted** directly from the list or form view
- Cancellation logs a chatter note: "❌ PR cancelled by [User]"

### 4.3 Petty Cash Release — GL Account Fix (BUG FIX)
**Root cause:** `destination_account_id` (computed stored field) was being recalculated back to the AP account every time Finance HO edited a draft payment, so the GL never debited the Cash-in-Hand account.

**Fix:** `_prepare_move_line_default_vals()` now enforces the site's Cash-in-Hand account at JE creation time, regardless of what `destination_account_id` holds at that moment.

**One-time fix for existing records:** A **"Fix GL Entry"** button is available on released/confirmed PCRs for Finance HO and Matracon Admin. It posts a correcting JE (Dr Cash-in-Hand / Cr wrong account) and logs it in the chatter.

### 4.4 Subcontractor IPC — Payment Tracking (OVERHAUL)
**What changed:** IPC "Payments Made to Subcontractor" previously read from the legacy `x.subcontractor.ho.advance` model. It now reads **actual posted payments** from `account.payment` directly.

**New logic:**
- Counts outbound payments (`payment_type = outbound`, state `in_process` or `paid`) to the subcontractor partner on the project
- Also counts petty cash subcontractor advances (`is_subcontractor_advance = True`, state `posted`)
- `payments_this_period` auto-fills "Payment Recovery" in the IPC deductions section
- HO Advances menu renamed to **HO Advances (Legacy)** and restricted to Matracon Admin only

### 4.5 Material Issuance — 3rd Party & SA Review (ENHANCED)
- New issue type: **3rd Party** (issues materials to an external party rather than an employee)
- All issue types now route to the **Production** stock location
- Site Accountant review workflow added before final posting
- Employee search enhanced: searchable by department, phone, and email with rich dropdown display

### 4.6 Cheque Series Management (NEW)
- Cheque series defined per journal; system auto-increments the next cheque number after each payment
- Default payment method on outbound payments set to **Checks**
- Leading zeros preserved in cheque numbers (e.g., 000142 stays 000142)
- Manual cheque number entry allowed without requiring an active series

### 4.7 Site User Contact Permissions — Read-Only (SECURITY)
**What changed:** Site Store Keepers and Site Accountants previously had full create and edit access to the Contacts (`res.partner`) model. This allowed them to create new vendors, clients, and employees directly from procurement forms, material issuance, subcontractor advance fields, billing forms, and other places.

**New behaviour:**
- Both `group_site_store` and `group_site_accountant` now have **read-only** access to `res.partner`
- The "Create and edit…" and "Create" quick-create options are suppressed in all partner dropdowns for these users
- Existing contacts can still be searched and selected
- Contact creation and editing is restricted to HO roles: Procurement HO, Finance HO, Head Office, and Matracon Admin

### 4.8 Contact Tag & Description Enforcement (ENHANCED)
All contacts (vendors, customers, employees) now require:
- At least one **Tag** (Category)
- A **Description** / notes field

Enforced at save time for all users.

---

## 5. UX Changes / Additions

### 5.1 PO & RFQ PDF — Letterhead Redesign
| Before | After |
|---|---|
| Odoo default layout, small logo | Custom letterhead: large logo (left) + bold company name (right) |
| No repeating header | Logo + company name + divider + PO ref + date repeats on every page |
| CC block (Director/File) at bottom | CC block removed |
| Standard paper format | Custom A4 format: margin 4mm top/bottom, DPI=96 |
| T&C picker narrow | T&C picker full-width (colspan=2) |
| All text left-aligned | All body text justified |

### 5.2 Bank Payment Voucher PDF
- New layout: title (left) + logo (right) in header
- Meta box: Financial Year, Voucher No (BPV ref), Date
- GL table: Account Code | Title / Narration | Debit | Credit
- Cheque details embedded per bank row (Account Title + Cheque No)
- Amount in Words section
- 4-column signature block (Prepared / Checked / Approved / Received By)

### 5.3 Employee Kanban & Form
- Green dot + **"Online"** label when employee's browser is active (heartbeat ≤ 65 seconds)
- **"Online From: [datetime]"** when offline (replaces inaccurate Odoo built-in presence)
- New **Online History** tab on employee form (Admin / HO / Finance HO only): columns Date & Time, Status (Online/Offline with colour), Duration

### 5.4 Accounting Menu Rename
- **Accounting → Customers → Payments** renamed to **Receipts**

### 5.5 Payments List — New Button
- The **New** button in the Vendors → Payments list now opens the **Batch Payment** form instead of a single payment form

### 5.6 Journal Entries — Due Date & Payment Terms Hidden
- Due Date and Payment Terms fields are now hidden on Journal Entry forms (still visible on Vendor Bills)
- Journal entries may freely use payable/receivable accounts without requiring a due date

### 5.7 Contact Dropdowns — No Create Option for Site Users
All partner/vendor/contact Many2one dropdowns (vendor on PO, partner on material issuance, subcontractor on petty cash advance, partner on vendor bill, etc.) no longer show **"Create"** or **"Create and edit…"** options when the logged-in user is a Site Store Keeper or Site Accountant. The dropdown still allows searching and selecting from existing contacts.

### 5.8 Petty Cash — Admin Fix Wizard
- New menu: **Accounting → Petty Cash → Configuration → Fix Petty Cash Accounts**
- Opens a dialog with a "Run Fix Now" button (Matracon Admin / System Administrator only)
- Triggers the same 3-step fix as the post-migrate hook, on demand without requiring a module upgrade

---

## 6. Known Issues / Pending

| # | Area | Description | Priority |
|---|---|---|---|
| 1 | BPV PDF | Footer (Regional Office line) may not render on the BPV PDF because the footer div is inside the article; this is a known wkhtmltopdf constraint for voucher-style reports with no separate footer mechanism | Low |
| 2 | IPC Payments | Payments made via petty cash before this deployment are not automatically back-filled into IPC "Payment Recovery" — historical IPCs must be manually reviewed | Medium |
| 3 | Employee Presence | `x_is_currently_online` requires the `mail.presence` record to exist; employees who have never logged in have no presence record and show as Offline permanently | Low |
| 4 | Cheque Series | If a journal has no cheque series configured, the Cheque No field is left blank without warning — no validation prevents posting a payment with no cheque number | Medium |
| 5 | Batch Payment | Batch payments are not yet linked to the Liability Sheet approval workflow — they bypass the normal payment approval chain | Medium |
| 6 | HO Advances (Legacy) | Existing `x.subcontractor.ho.advance` records are preserved for audit but are no longer reflected in IPC totals; IPCs referencing old HO advances show a discrepancy until manually reconciled | Medium |

---

## 7. Rollback Plan

### Conditions for rollback
Rollback should be triggered if any of the following occur within 24 hours of production deployment:
- Vendor payments fail to post or generate incorrect journal entries
- Petty cash release flow stops creating journal entries
- PO/RFQ PDF renders blank or crashes

### Rollback steps

**Step 1 — Identify the last stable git SHA on Production**
```
git log --oneline origin/main | head -10
```
Note the SHA of the last known-good production commit (prior to this deployment).

**Step 2 — Revert the Development branch**
```
git revert <sha-range> --no-commit
git commit -m "Rollback: revert Matracon Deployment 27072026"
odoosh-push
```
Or use the Odoo.sh dashboard to roll back the branch to the previous build.

**Step 3 — Downgrade the modules**
On the production database after the branch reverts:
```
odoo-bin -u site_operations,purchase_demand_raise --stop-after-init --no-http
```

**Step 4 — Restore database if schema changes cause errors**
If new columns / models introduced in this release cause constraint errors on rollback:
- Use the Odoo.sh **Backup** created automatically before this deployment
- Restore from the pre-deployment backup via the Odoo.sh dashboard → Backups tab

**Step 5 — Verify**
- Open a Vendor Bill and post it — confirm GL entries are correct
- Open a Petty Cash Request and release it — confirm Cash-in-Hand is debited
- Print a PO PDF — confirm it renders
- Check employee kanban for presence indicators

### Data risk assessment
| Change | Reversibility |
|---|---|
| New fields (nullable) | Safe — old code ignores unknown columns |
| New models | Safe — old code doesn't reference them |
| `post_migrate_hook` JE corrections | **Irreversible** — correcting JEs are posted; must be manually reversed if rollback needed |
| BPV ref renaming | Safe — old code uses `name` field as fallback |
| Menu renames | Safe — revert restores original menu names |
