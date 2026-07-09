{
    'name': 'Partner Ledger Tax Column',
    'version': '1.2.0',
    'summary': 'Adds a combined Taxes column to the Partner Ledger report',
    'depends': ['account', 'account_reports'],
    'data': [
        'views/partner_ledger_tax_templates.xml',
        'views/trial_balance_type_groupby.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
