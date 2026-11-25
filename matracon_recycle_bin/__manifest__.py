{
    'name': 'Matracon Recycle Bin',
    'version': '19.0.1.1.0',
    'summary': 'Keeps a full copy of every deleted business record (Admin only)',
    'description': """
Recycle Bin / Deleted Records
=============================

Whenever a business record is deleted in any app (by a user or by the nightly
draft cleanup), a full copy is saved before the delete: every field value, its
lines and attachment names, who deleted it and when. The delete itself runs
exactly as before, so no workflow, permission or accounting behaviour changes.

Deletion control: Critical records (master data, bank guarantees, cheques,
certificates…) can only be deleted by an Administrator; Approval records
(PRs/POs, draft bills, payments, petty cash, sheets, transfers…) need an
Administrator to approve a deletion request (reason required). Only deletions a
person starts are controlled; system deletions (nightly cleanup, Cancel
Receipt, Odoo line re-sync) run as before. Rules are editable.

Menus (Matracon Admin / System Administrator only):
    Recycle Bin → Deletion Requests
    Recycle Bin → Deleted Records
    Recycle Bin → Deletion Rules

Search by record name; group by App, Record Type, Deleted By, Deletion Date or
How Deleted; filter by deleted-by and deletion date. View only.
""",
    'category': 'Productivity',
    'author': 'Matracon',
    'license': 'LGPL-3',
    'depends': ['base', 'web', 'mail', 'purchase_demand_raise'],
    'data': [
        'security/ir.model.access.csv',
        'security/deletion_rules.xml',
        'views/recycle_bin_views.xml',
        'views/deletion_control_views.xml',
        'data/deletion_policy_data.xml',
    ],
    'installable': True,
    'application': True,
}
