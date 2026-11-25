{
    'name': 'Matracon Recycle Bin',
    'version': '19.0.1.0.0',
    'summary': 'Keeps a full copy of every deleted business record (Admin only)',
    'description': """
Recycle Bin / Deleted Records
=============================

Whenever a business record is deleted in any app (by a user or by the nightly
draft cleanup), a full copy is saved before the delete: every field value, its
lines and attachment names, who deleted it and when. The delete itself runs
exactly as before, so no workflow, permission or accounting behaviour changes.

Menu (Matracon Admin / System Administrator only):
    Recycle Bin → Deleted Records

Search by record name; group by App, Record Type, Deleted By, Deletion Date or
How Deleted; filter by deleted-by and deletion date. View only.
""",
    'category': 'Productivity',
    'author': 'Matracon',
    'license': 'LGPL-3',
    'depends': ['base', 'web', 'mail', 'purchase_demand_raise'],
    'data': [
        'security/ir.model.access.csv',
        'views/recycle_bin_views.xml',
    ],
    'installable': True,
    'application': True,
}
