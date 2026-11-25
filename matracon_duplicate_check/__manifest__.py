{
    'name': 'Matracon Duplicate Check',
    'version': '19.0.1.0.0',
    'summary': 'Warns before saving a new record that looks like an existing one',
    'description': """
Duplicate data detection
========================
When a user saves a NEW record (contacts, employees, products, PRs/POs,
vendor bills, customer invoices, journal entries, payments, petty cash,
receipts / material issuances, projects, analytic accounts, bank guarantees,
leave applications), the system first looks for likely duplicates using
rules specific to each record type. A popup shows the existing record(s) with
key details and offers: Open Existing, Create Anyway, Cancel. Nothing is
blocked. "Create Anyway" is noted in the new record's chatter.

Edits, imports and records created by the system are not checked.
Administrators can switch rules on/off in Settings → Duplicate Check Rules.
""",
    'category': 'Productivity',
    'author': 'Matracon',
    'license': 'LGPL-3',
    'depends': ['web', 'mail', 'account', 'purchase', 'stock', 'project', 'hr', 'site_operations',
                'purchase_demand_raise'],
    'data': [
        'security/ir.model.access.csv',
        'data/duplicate_rule_data.xml',
        'views/duplicate_rule_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'matracon_duplicate_check/static/src/xml/duplicate_dialog.xml',
            'matracon_duplicate_check/static/src/js/duplicate_check.js',
        ],
    },
    'installable': True,
}
