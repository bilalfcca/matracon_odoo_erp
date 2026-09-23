{
    'name': 'Board Resolutions',
    'version': '1.0.0',
    'summary': 'Standalone app for formal Board Resolutions — Admin/CEO only',
    'category': 'Human Resources',
    'description': """
Board Resolutions
==================
A standalone, tightly-restricted app for issuing formal Board Resolutions
(e.g. SECP representation authorizations) — the kind of document that lists
attending directors/officers, one or more numbered resolution clauses, a
standard ratification clause, and a signature block (signature + company
stamp + name + designation) per attendee.

**Access is restricted to Matracon Admin, CEO Approval, and System
Administrator only** — no other role can see the app, its menu, or its
records. This is enforced at the ACL level (no other group has any access
row at all), not just by hiding the menu.

Reference numbering: the "Resolution Reference" field is always manually
editable. The very first resolution is typed in by hand (e.g. "BR # 26/16").
Every resolution after that defaults to the previous resolution's own
saved reference with its trailing number incremented by one — so overriding
a number by hand is picked up automatically as the new baseline for the
*next* suggestion, exactly like Odoo's own invoice/PO numbering. See
models/board_resolution.py for why this is a small purpose-built mechanism
rather than a plain `ir.sequence` counter (a raw sequence's own internal
counter does not "notice" a manual override to a record's field — this one
does, by design).
""",
    'depends': ['purchase_demand_raise', 'site_operations'],
    'data': [
        'security/ir.model.access.csv',
        'report/board_resolution_report.xml',
        'report/board_resolution_template.xml',
        'views/board_resolution_views.xml',
        'views/res_company_views.xml',
        'views/menus.xml',
    ],
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
