# MATRACON ODOO ERP — COMPLETE SYSTEM FLOW

**Version:** Final  
**Prepared by:** CWARE Technologies

---

## Core Architecture Principles

**Native Odoo first.** Everything runs on standard Odoo models — `purchase.order`, `stock.picking`, `account.move`, `account.payment`, `account.analytic.account`, `hr.employee`, `hr.payslip`. Customization sits on top as field extensions, computed fields, and view overrides. Core Odoo logic is never replaced.

**Dynamic and scalable.** No hardcoded user IDs, role names, or project codes anywhere in the system. Access rules are record-rule based, driven by a project-user assignment table on the project itself. Adding a new project, new site store manager, or new site accountant requires only a configuration step — not a code change. The system must handle 2 projects today and 50 projects tomorrow without any structural change.

---

## Project Configuration Layer

Every `project.project` record has two additional many2many fields: **Site Store Managers** (`res.users`) and **Site Accountants** (`res.users`). Each project is linked to exactly one analytic account. This is the single source of truth for all access control and auto-fill logic across the entire system.

**Site Store** — When a Site Store user logs in and creates any entry (stock picking, material issuance, site-to-site transfer), the analytic account auto-populates read-only from their assigned project. Record rules filter all inventory, issuance, receipt, and transfer views to show only records belonging to their assigned project analytic account.

**Site Accountant** — When a Site Accountant logs in and creates any entry (petty cash, attendance, salary, employee record), the analytic account auto-populates read-only from their assigned project. Record rules filter all accounting, HR, and payroll views to show only their assigned project.

**Procurement HO, CEO, and Finance HO** have no record rule restrictions — they see all projects across all views.

This access pattern scales to any number of projects and users without code changes.

---

## 1. Site Store — Demand Raise

Site Store opens the Purchase application and creates a new Purchase Requisition. Product selection is restricted to predefined approved categories only. An **Other** option is available for miscellaneous or unidentified items. On-hand stock quantity is visible in real time on each line to prevent duplicate or excess demands. Vendor field accepts an **Unknown Vendor** placeholder — Procurement HO will finalize the actual vendor during the RFQ stage. Analytic account auto-fills read-only from the user's assigned project. Site Store cannot see pricing, vendor rates, or any financial fields on the PR.

Site Store confirms the PR, prints it, takes a physical signature from the Project Manager, scans the signed copy, and uploads it as a mandatory attachment. The Validate / Submit button is blocked until the PM-signed PR attachment is present. PR is then submitted and enters the approval workflow. Status moves to **Submitted**.

---

## 2. Procurement HO — Requisition Review

Procurement HO (Nasir + Abdullah) receives all submitted PRs across all projects in their queue. Reviews each PR for feasibility — checks stock position, market availability, and budget constraints. May adjust the recommended quantity (e.g., site requested 100 units, Procurement recommends 70). This recommended quantity and the reasoning are recorded as an internal log entry in plain text in the chatter. The original demanded quantity remains visible alongside the recommended quantity. This is an internal step — no system approval required, no notification to site at this stage.

---

## 3. Procurement HO — Comparative Statement

Procurement HO sends RFQs to multiple vendors from the standard Odoo Purchase application. Each RFQ is linked back to the originating PR. Vendor quotations are collected and stored against their respective RFQs.

The Comparative Statement is generated from all linked RFQs for the same PR. Format follows the HVAC Pumps Rate Comparative Statement reference exactly: rows are line items, columns are vendors, each cell shows Unit Rate and Total Amount, GST is shown separately, Grand Total per vendor is shown, and the lowest combined total is flagged with a star marker. Terms and conditions per vendor (delivery period, payment terms, warranty, GST treatment, VFD/spare inclusion) are shown in a summary table below the main comparison grid.

CS document is attached to the PO after vendor finalization. All negotiation emails and revised quotations are also attached. Final vendor is selected and marked on the CS.

---

## 4. Procurement HO — PO Preparation and Submission

Purchase Order is prepared for the finalized vendor. Compliance fields are mandatory: **Vendor NTN**, **Exemption Certificate Number**, **Validity Date**. Predefined Terms and Conditions auto-apply based on the product-vendor combination — only approved predefined terms are allowed, no free-text T&C. PO chatter and email discussion are disabled — no internal or vendor-facing communication on the PO record itself.

CS document and all supporting negotiation documents are attached in the attachment section below the subtotal line of the PO. **Nasir** (Procurement Head) reviews and approves the PO first — this is the first system approval stage. Only after Nasir's approval does the PO route to CEO/MD.

---

## 5. CEO/MD — Procurement Approval

CEO receives the PO with the attached Comparative Statement and all supporting documents on their **Procurement Approvals** dashboard. Reviews line by line. For each line, CEO selects a decision type: **Full** (approve full quantity), **Manual** (enter a specific approved quantity), **25%**, **50%**, or **75%** (system auto-computes approved quantity from the demanded quantity). Approved quantity and approved amount auto-compute in real time — no save or refresh required.

CEO can approve all lines, partially approve selected lines, or reject individual lines. Lines can have mixed decisions on the same PO. Once CEO submits the approval, the PO is locked — no edits allowed by anyone. Auto-notification is sent to Site Store, Procurement HO, and the finalized vendor. PO is dispatched to vendor electronically.

---

## 6. Site Store — Material Receiving

Vendor delivers material to site. Site Store opens the Receipt record linked to the approved PO in the Inventory application. Two mandatory fields must be filled before the Validate button becomes active: **Gate Pass Inward Number** (manual text entry) and **Weight Document** (file upload). Project field auto-fills read-only from the linked PO's analytic account.

Site Store verifies quantity received against PO quantity. Partial deliveries are handled natively — Odoo creates a backorder automatically for the remaining quantity, linked to the original PO. The backorder remains visible in the dashboard until fully fulfilled. Inventory updates in real time upon validation. **No liability or payable entry is created at the receiving stage** — liability creation is handled by the Site Accountant after the vendor claim arrives.

---

## 7. Site Store — Normal Issuance

Site Store receives a material demand from the site team. Checks available stock in real time on the issuance form. Each line is classified as **Consumable** or **Asset** — system defaults to Consumable, user can change to Asset. For Asset lines, if the same asset item is already issued to the same person, the system shows a warning and blocks duplicate issuance.

Material is issued. Stock is deducted from inventory. A **Gate Pass Outward** is generated for all material leaving the store. The **MIF** (Material Issue Form) print document is also available. Both documents are triggered by the **Generate Gate Pass Outward** checkbox on the issuance record — when checked, both MIF Print and Gate Pass Outward become available for printing. Analytic account auto-fills from the user's assigned project.

No backcharge applies on normal issuance to site team members.

---

## 8. Site Store — 3rd Party (Subcontractor) Issuance with Auto Liability Update

Site Store issues material to a subcontractor. The issuance form has a **Backcharge** field per line — enabled or disabled based on subcontract terms. When backcharge is enabled on any line, the following happens automatically upon confirmation of the issuance, with no action required from the Site Accountant:

- The system posts a real journal entry: **Debit Subcontractor Partner Account (payable), Credit Material Cost Account.** This entry carries the project's analytic account tag.
- The subcontractor's partner ledger is updated immediately and in real time.
- The liability sheet for that subcontractor reads directly from the partner ledger — the backcharge amount appears as an automatic deduction in the liability sheet without any manual entry by the Site Accountant.

When the subcontractor returns material and backcharge was applicable, the return posting reverses the original journal entry automatically: **Debit Material Cost Account, Credit Subcontractor Partner Account.** The partner ledger and liability sheet update in real time to reflect the reversal — net liability increases back by the returned amount. The liability sheet always shows the live net position.

Gate Pass Outward and MIF Print are generated same as normal issuance.

---

## 9. Site Store — Normal Return

Material returned by site team member. Return form shows original issuance reference. Condition is assessed per line: **New / Used / Repairable / Scrap**. Partial returns are handled — system records the returned quantity and notes the difference from the originally issued quantity (e.g., 10m pipe issued, 9m returned, 1m consumed recorded). Accepted material in New, Used, or Repairable condition re-enters inventory immediately. Scrap material is routed to the scrap location. Full return history is maintained per item, per person, per project for audit and traceability.

---

## 10. Site Store — 3rd Party (Subcontractor) Return

Material returned by subcontractor. System checks inventory type (Asset or Consumable) and whether backcharge was applicable on the original issuance. If backcharge was applicable, the return automatically reverses the journal entry that was posted at issuance — no action required from the Site Accountant. Partner ledger and liability sheet update in real time as described in Step 8. If an asset is returned in damaged or incomplete condition, a backcharge for the damage amount is posted as a separate journal entry. Return history is maintained for reconciliation against subcontractor liability.

---

## 11. Site Store — Site-to-Site Transfer

Site Store raises a **Material Transfer Note (MTN)** specifying source site (own project), destination site (another project), material lines, and quantity. Analytic account of the source project auto-fills. MTN is submitted for approval to CEO / Procurement HO.

Once approved, Gate Pass Outward is generated at the source site. Material is dispatched. Destination site store opens the incoming transfer, verifies the received quantity, and acknowledges receipt. Both site inventories update in real time — source site stock decreases, destination site stock increases.

Inter-project accounting entries post automatically on dispatch confirmation: **Debit Destination Project Receivable / Payable** account tagged with destination analytic, **Credit Source Project payable** account tagged with source analytic. Both project financial reports reflect the transfer.

---

## 12. Site Accountant — Vendor Ledger and Liability Sheet

Site Accountant receives the vendor's claim after the GRN has been validated. Opens the vendor liability entry form for their assigned project (analytic account auto-fills read-only). No invoice number is required — this is a liability-based system. Enters the total claimed amount, tax type, exemption certificate number, and exemption validity date (all mandatory). The payable posts to the vendor's partner ledger as a real accounting entry. The liability sheet reads from the partner ledger in real time — shows total liability and remaining amount (total minus any payments already made by Finance HO). **WHT is not applied at this stage** — gross amount only.

---

## 13. Site Accountant — Subcontractor Ledger and Liability Sheet

Site Accountant receives the **IPC** (Interim Payment Certificate / work certification) for a subcontractor. Opens the subcontractor liability entry. The backcharge amounts from material issuances and returns are already present in the partner ledger from the automatic journal entries posted by the store (Steps 8 and 10) — the Site Accountant does not enter these manually.

Site Accountant records: IPC gross value, withheld security amount, retention percentage (auto-computes retention amount), advance recovery amount, and any additional manual adjustments. System computes net payable:

**IPC Value − Backcharges (already in ledger) − Retention − Advance Recovery − Withheld Amount**

Net payable posts to the subcontractor partner ledger. Liability sheet shows all components and the final net payable. WHT is not applied at this stage — gross net payable only.

---

## 14. Site Accountant — Petty Cash Management

Site Accountant creates a petty cash request with: current petty cash balance, required amount, and reason. Request is submitted to Finance HO. Finance HO verifies and releases the amount via bank transfer or cheque. Site Accountant confirms receipt and the opening balance updates. Daily expenses are recorded against the petty cash account as they occur. Running balance auto-calculates after each entry. Finance HO can see the real-time balance for all sites simultaneously on their dashboard.

---

## 15. Site Accountant — Attendance

Site Accountant maintains a single combined employee list per project — no distinction between labor and staff categories. Marks daily attendance per employee per day: **Present**, **Absent**, **Leave**, or **Public Holiday**. Monthly attendance summary auto-generates at the end of each period showing total present days, absent days, and leave days per employee. Average manpower count (total present / working days in period) is calculated for the month and for any custom date range. Attendance data feeds directly into salary calculation — no re-entry required.

---

## 16. Site Accountant — Salary Slips

Gross salary is computed per employee from the attendance data for the period. System deducts recorded advances automatically. Approved adjustments (additions or deductions, approved separately) are applied. Individual salary slip is generated per employee showing gross, deductions, adjustments, and net payable. Payroll summary is consolidated at project level and visible to Finance HO and CEO. Salary payment is processed by Finance HO against the consolidated payroll.

---

## 17. Site Accountant — Employee Creation

Employee profile is created with: full name, CNIC number, job position, department, and project assignment. Mandatory documents are uploaded against the profile (CNIC copy and any additional supporting documents). System generates a printable employee card from the profile. Employee does not require an Odoo user account — the Site Accountant manages all records on their behalf. Analytic account of the assigned project auto-tags all employee records.

---

## 18. CEO/MD — Dashboards

CEO has a single consolidated dashboard with two sections: **Actionable** and **Informational**.

**Actionable**

- Procurement Approvals (all pending POs waiting for CEO decision)
- Payment Approvals (all pending liability recommendations from Site Accountants waiting for CEO approval across all projects)

**Informational**

- Total Vendor Payables (all projects)
- Total Subcontractor Payables IPC-wise summary (all projects)
- Project-wise Liability Overview (vendor + subcontractor)
- Site-wise Petty Cash Expenses (all sites)
- Material Cost Consumption per project
- Active Projects Financial Health Overview
- Project Work Done and Remaining Status
- Bank Guarantees status summary
- Post-Dated Cheques summary

All informational panels are read-only live views from real accounting data — no manual data entry feeds these dashboards.

---

## 19. CEO/MD — Payment Approvals

CEO reviews liability recommendations from Site Accountants across all projects on the **Payment Approvals** dashboard. For each liability entry, CEO selects: **Full** (pay full remaining amount), **Manual** (enter specific approved amount), **25%**, **50%**, or **75%** (system auto-computes approved amount). Approved amount locks immediately. Finance HO can only process payment up to the CEO-approved locked amount — no override is possible.

---

## 20. Finance HO — Vendor Payment

Finance HO opens the CEO-approved vendor liability. Payment screen shows: vendor name, project, gross approved amount, WHT section. **WHT is calculated and applied here for the first time in the entire flow** — auto-calculated based on the vendor's configured tax section and applicable rate. Net payable = Gross Approved Amount − WHT. Bank journal is selected from a list of available bank and cash journals pulled from Odoo's native journal configuration — each journal shows its real current available balance from the chart of accounts.

Payment is confirmed. Journal entry posts: **Debit Vendor Payable, Credit Bank/Cash Journal, Credit WHT Payable account.** Partner ledger updates. WHT tax certificate details are recorded against the payment. Cheque number is assigned from the cheque inventory for the selected bank — only unused cheque numbers appear in the dropdown. Cheque is printable directly from the system using the configured cheque template for that bank.

---

## 21. Finance HO — Subcontractor Payment

Identical flow to vendor payment. CEO-approved locked amount. WHT applied at payment stage only. Bank journal selected from native Odoo journals showing real available balances. Net payable after WHT is processed. Ledger posts with project analytic tag. Tax certificate recorded. Cheque assigned and printable.

---

## 22. Finance HO — Petty Cash Release to Sites

Finance HO receives petty cash request from Site Accountant. Verifies current balance and requested amount. Releases via cheque or bank transfer. Fund transfer is recorded as a journal entry. Site Accountant's petty cash opening balance updates upon confirmation of receipt.

---

## 23. Finance HO — HO Direct Payments

CEO initiates a direct payment outside the normal liability cycle — specifies receiver (vendor or subcontractor), project, and amount. Finance HO verifies operational details. WHT is applied at this payment stage. Payment is processed through the selected bank journal. Ledger posts with the selected project's analytic account. Full audit trail is maintained.

---

## 24. Finance HO — Adjustments and Miscellaneous Entries

Journal entries for loans given, loans received, MD account corrections, and miscellaneous adjustments. All entries pass through standard `account.move` with proper debit/credit accounts. Adjustment reason and reference are mandatory fields. Full audit trail with change history maintained. Accessible to Finance HO and CEO.

---

## 25. Finance HO — Tax and WHT Management

WHT rules are configured once per tax section in Odoo's native tax configuration. On every vendor or subcontractor payment, the correct tax section is selected (or auto-selected from the partner's configured section) and WHT auto-calculates. All WHT entries are linked to their originating payment. Tax certificates are generated per payment. WHT payable account is updated per transaction. Tax ledger is always current and reconcilable.

---

## 26. Finance HO — Salary Sheets and Slips

Finance HO sees consolidated payroll across all projects from the Site Accountants' submitted payroll summaries. Salary payments are processed through the standard Odoo payroll journal. Each salary payment carries the project's analytic account tag for project-level cost reporting.

---

## 27. Finance HO — Cheque Number Management

Cheque books are configured per bank account: bank journal, starting cheque number, ending cheque number. System maintains a cheque inventory per bank. When a payment is processed against a bank journal, only unused cheque numbers for that bank appear in the selection dropdown. Once a cheque number is used and payment is posted, that cheque number is locked and cannot appear again. Full cheque issuance history is maintained for bank reconciliation and audit.

---

## 28. Finance HO — Cheque Printing

Cheque template is configured per bank account with the exact physical layout matching the bank's cheque book format. From any confirmed payment, Finance HO can print the cheque directly from the system. Printed cheque carries: payee name, amount in figures, amount in words, date, bank account details, and cheque number. Cheque printing is linked to the payment record and logged in the cheque audit trail.

---

## 29. Finance HO — Bank Guarantee Management

Bank Guarantee records are created with: serial number, bank name, BG nature, guarantee number, total limit, BG amount, cash margin, pricing/fee, JV name, project link, beneficiary name, issue date, and expiry date. Available limit auto-calculates in real time: **Total Limit − Utilized Amount**. Status field tracks: **Active**, **Expired**, or **Released**. System generates expiry alerts before the BG expiry date. CEO sees BG summary on dashboard. Finance HO manages all BG records.

---

## 30. Finance HO — Tax Notices

Tax notice records are created with: serial number, JV or taxpayer name, reference number, notice section (Notice U/S), description, tax year, document date, due date, total tax liability amount, official payments made, unofficial payments made, consultant fee paid, remaining balance (auto-calculated), and notice status (**Open**, **In Process**, **Closed**). Due date alerts are generated before the deadline. CEO and Finance HO both see this dashboard. Bilal (Tax Officer) manages all entries.

---

## 31. Project Financial Reporting

Every project has a live financial report built entirely from real journal entries and analytic account lines — no manual data entry. The report shows:

- Opening balance
- Total funds received from Finance HO
- Total vendor payments made
- Total subcontractor payments made
- Total petty cash released and consumed
- Total material cost consumed (from issuances with backcharge)
- Total backcharge recoveries (from subcontractor returns)
- Outstanding vendor liabilities
- Outstanding subcontractor liabilities
- Available remaining balance

CEO sees all projects consolidated with drill-down. Finance HO sees all projects. Each Site Store Manager sees only their assigned project's inventory and material cost data. Each Site Accountant sees only their assigned project's financial data. All filtering is driven dynamically by the project-user assignment.

---

## Role and Access Summary

| Role | Sees | Analytic auto-fill | Record rule scope |
|------|------|--------------------|-------------------|
| **Site Store** | Own project inventory, issuances, receipts, transfers | Yes — from project assignment | Own project only |
| **Site Accountant** | Own project ledger, liability, attendance, payroll, petty cash | Yes — from project assignment | Own project only |
| **Procurement HO** | All PRs and POs across all projects | No | All projects |
| **CEO / MD** | All dashboards, all approvals, all projects | N/A | Full access |
| **Finance HO** | All payments, all liability sheets, all projects | N/A | Full access |

### Role capabilities

**Site Store Manager** — Creates PRs, receives material, issues material (normal and subcontractor), processes returns, initiates site-to-site transfers. Cannot see pricing on PR. Cannot access accounting, HR, or payroll.

**Site Accountant** — Creates vendor and subcontractor liability entries, manages petty cash, records attendance, generates salary slips, creates employee profiles. Cannot access inventory or procurement.

**Procurement HO** — Manages RFQs, comparative statements, PO preparation, vendor communication. Cannot approve POs — approval is CEO only after Procurement Head sign-off.

**CEO / MD** — Approves procurement POs line by line. Approves payment recommendations. Initiates HO direct payments. Sees all dashboards.

**Finance HO** — Processes all payments (vendor, subcontractor, petty cash, direct, salary). Applies WHT at payment stage. Manages cheque books, cheque printing, bank guarantees, tax notices, adjustments.

---

## Backcharge Auto-Flow Summary

| Event | System action | Site Accountant action |
|-------|---------------|------------------------|
| Subcontractor issuance with backcharge | Auto-posts **Dr Subcontractor Payable / Cr Material Cost** → partner ledger + liability sheet update | None |
| Subcontractor return (backcharge reversal) | Auto-posts reverse entry **Dr Material Cost / Cr Subcontractor Payable** → net liability increases | None |
| Subcontractor return (damage) | Auto-posts damage entry **Dr Subcontractor Payable / Cr Damage Recovery** → liability reflects deduction | None |

---

## WHT Timing — Single Point of Deduction

WHT is calculated and applied **exactly once** in the entire flow: at the **Finance HO payment stage**.

It is **never** calculated or deducted at:

- Bill upload
- Liability creation
- IPC recording
- Liability sheet stage

All liability sheets and partner ledger entries carry **gross amounts**. WHT appears only in the payment journal entry and the tax certificate.
