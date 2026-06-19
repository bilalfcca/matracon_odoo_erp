{
    'name': 'Partner Ledger Tax Column',
    'version': '1.1',
    'summary': 'Adds Tax and Withheld Tax columns to the Partner Ledger report',
    'depends': ['account', 'account_reports'],
    'data': [
        'views/partner_ledger_tax_templates.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
