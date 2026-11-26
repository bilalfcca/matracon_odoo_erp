{
    'name': 'Counterpart Account Column',
    'version': '1.0.2',
    'summary': 'Adds Counterpart Account and Cheque No. columns to the General Ledger report (and Counterpart to Journal Items)',
    'description': """
Counterpart Account Column
===========================
Adds a "Counterpart Account" column showing which account received the
opposite debit/credit side of the same journal entry, so it is no longer
necessary to open the journal entry manually to see the other side of a
transaction.

Shown on:

* **General Ledger** report (Accounting > Reporting > Ledgers > General
  Ledger) - as a genuine report column, computed via the report's existing
  custom engine.
* **Journal Items** list view - hidden by default (available via the
  optional-columns toggle), so it is available wherever Journal Items are
  shown, including the drill-down opened by clicking an account balance on
  the Trial Balance report.

The Trial Balance report itself is intentionally left untouched: it shows
one aggregated row per account (netting many unrelated journal entries), so
a single "counterpart account" has no meaning at that level of aggregation.

When a journal entry has more than two lines, every other account on the
entry is listed, comma-separated.

The General Ledger also shows a **Cheque No.** column, read from the
existing cheque fields (series leaf or typed number on the journal item,
journal entry or payment). Included in PDF and XLSX exports.
""",
    'depends': ['account', 'account_reports', 'site_operations'],
    'data': [
        'data/general_ledger_counterpart_column.xml',
        'views/account_move_line_views.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
