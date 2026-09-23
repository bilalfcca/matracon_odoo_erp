{
    'name': 'Matracon Admin Tools — Fix Tools',
    'version': '1.0.0',
    'summary': 'Centralized Settings screen listing every custom admin/data-repair Server Action',
    'description': """
Fix Tools — Centralized Server Action Registry
================================================
Consolidates every ad-hoc "fix" / "rebuild" / "recompute" data-repair Server
Action that used to live as a scattered button on an individual model's form
or list view into one screen: **Settings > Fix Tools**.

Each entry is a real ``ir.actions.server`` record (two extra fields added
here: "Fix Tool" flag + a plain-language description of what it does), so
running one is exactly what happened before - only the button moved. Access
per tool is unchanged: each action keeps the same security groups it had at
its old location (via the native ``group_ids`` field on Server Actions), so
nobody gains or loses capability because of the move.

Tools migrated in this pass:
  * Rebuild Site Analytics (was: Journal Entries list > Action menu)
  * Fix Petty Cash Accounts (was: Petty Cash > Configuration wizard +
    Petty Cash Expenses list > Action menu - both called the same routine)
  * Fix GL Analytic Distribution on Payment Entries (was: same wizard)
  * Fix Petty Cash GL Entry (was: a button on the Petty Cash Request form -
    now a small "pick the request, then run" wizard here)
  * Fix Old Inter-Project Entries / Fix Old Issuances / Fix Analytic
    Visibility (was: three buttons on the Site Configuration form - now one
    shared "pick the site, then run" wizard, one entry per fix)
  * Unlink GL (was: a button on the Fleet Service Log form - now a
    "pick the log entry, then run" wizard here)
  * Change Product UoM (was: Inventory > Configuration menu - same wizard,
    just opened from here now)

Going forward, any new custom admin/data-repair Server Action should be
added here (a new ``ir.actions.server`` record with the "Fix Tool" box
checked) instead of as a one-off button on a model view.
""",
    'depends': ['site_operations', 'matracon_fleet'],
    'data': [
        'security/ir.model.access.csv',
        'security/ir_rule.xml',
        'views/ir_actions_server_views.xml',
        'views/fix_tool_wizards_views.xml',
        'data/fix_tools_data.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
