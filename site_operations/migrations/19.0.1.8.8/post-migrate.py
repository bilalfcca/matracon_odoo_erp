"""
Migration 19.0.1.8.7 — Restrict Odoo's built-in account.move 'see all' rules

Odoo ships several group-based record rules on account.move that grant
[(1,'=',1)] to account.group_account_invoice and account.group_account_readonly.
Since group_site_accountant implies group_account_user → group_account_basic →
group_account_invoice (and also group_account_readonly), site accountants were
bypassing the project-scoped rule and seeing all journal entries.

Fix: restrict those built-in rules to account.group_account_manager only.
Finance HO and Matracon Admin both imply group_account_manager, so they keep
full visibility.  Site accountants only have group_account_user and therefore
fall through to rule_account_move_site_accountant (own project only).

These rules are noupdate=True so XML cannot override them — Python is needed.
"""


def migrate(cr, version):
    from odoo import api, SUPERUSER_ID
    env = api.Environment(cr, SUPERUSER_ID, {})
    from odoo.addons.site_operations.hooks import fix_account_move_rules
    fix_account_move_rules(env)
