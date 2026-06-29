# Matracon Odoo ERP

Custom Odoo 19 modules for Matracon Pakistan — procurement, site inventory, liability sheets, and finance workflows across multiple project sites.

**Requires:** Odoo Enterprise (`account_reports` for Partner Ledger tax columns).

## Modules

| Module | Purpose |
|--------|---------|
| `purchase_demand_raise` | PR → HO review → CEO approval → locked PO, comparative statement, RFQ/PO reports |
| `site_operations` | Material issuance, returns, site-to-site transfers, liability sheets, finance payments |
| `my_custom_module` | Partner Ledger **Tax** and **Withheld Tax** columns |

Install order: `purchase_demand_raise` → `site_operations` → `my_custom_module`

## Projects

- **MCH - BAHAWALNAGAR**
- **RWASA**
- **STP - MARDAN**

Each site has a Site Store and Site Accountant, linked via **Site Project Configuration** (analytic account + warehouse).

## Users & roles

| Role | Access |
|------|--------|
| Site Store | Raise PRs (own site only, no prices) |
| Procurement HO | Review PRs, vendor selection, comparative statement |
| CEO | Final PR/PO approval, liability sheet approval |
| Site Accountant | Liability sheets, vendor payables |
| Finance HO | Payments against approved liability sheets |
| Head Office | Cross-project visibility |

### Demo users (`site_operations` demo data)

| Login | Password | Role |
|-------|----------|------|
| `bilal.khan@matracon.pk` | `admin` | Admin + Head Office |
| `ceo@matracon.pk` | `user` | CEO |
| `procurement@matracon.pk` | `user` | Procurement HO |
| `finance@matracon.pk` | `user` | Finance HO |
| `accountant.mch@matracon.pk` | `user` | MCH Site Accountant |
| `store.mch@matracon.pk` | `user` | MCH Site Store |
| `accountant.rwasa@matracon.pk` | `user` | RWASA Site Accountant |
| `store.rwasa@matracon.pk` | `user` | RWASA Site Store |
| `accountant.stp@matracon.pk` | `user` | STP Site Accountant |
| `store.stp@matracon.pk` | `user` | STP Site Store |

Production user IDs and group mapping are in `site_operations/hooks.py` (applied on module install/upgrade).

## Main menus

- **Purchase** — Purchase Requisitions / PO workflow
- **Inventory → Transfers** — Material Issuance, Site-to-Site Transfers
- **Accounting → Vendors** — Liability Sheets, Finance Payments
- **Reporting → Partner Ledger** — Tax columns (after `my_custom_module`)

## PR workflow (short)

`Draft` → attach PM-signed PR → `Submit` → HO approves & sets vendor → CEO final approve → PO locked → dispatch to vendor

## Reference docs

- **[MATRACON_SYSTEM_FLOW.md](MATRACON_SYSTEM_FLOW.md)** — complete target system flow (CWARE Technologies)
- FSD flow documents in repo root (`*_flow.docx`) are earlier planning references; implementation may differ.

## Branch

Active development: `Development` (Odoo.sh)
