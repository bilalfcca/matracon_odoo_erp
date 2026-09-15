"""IPC Global Account Configuration — one record per company.

Stores the GL accounts used for each IPC deduction component
(retention, mob advance, security, back charges, other deductions).
Set once in Accounting → Configuration → IPC Account Settings.
"""

from odoo import models, fields, api


class IPCAccountConfig(models.Model):
    _name = 'x.ipc.account.config'
    _description = 'IPC Global Account Configuration'
    _rec_name = 'company_id'

    company_id = fields.Many2one(
        'res.company', string='Company', required=True,
        default=lambda self: self.env.company,
        ondelete='cascade')

    # ── Default work entry accounts (auto-fill on new IPCs) ───────────────────
    expense_account_id = fields.Many2one(
        'account.account',
        string='Default Work Expense Account',
        domain="[('account_type', 'not in', ['liability_payable', 'asset_receivable']), ('active', '=', True)]",
        help='GL account debited for certified work done cost (Dr side of IPC JE). '
             'Auto-fills on new IPCs; can be overridden per IPC.')
    payable_account_id = fields.Many2one(
        'account.account',
        string='Default Subcontractor Payable Account',
        domain="[('account_type', '=', 'liability_payable'), ('active', '=', True)]",
        help='Default subcontractor payable account (Cr side — hits partner ledger). '
             'Overridden by the subcontractor partner\'s own payable account if set.')
    journal_id = fields.Many2one(
        'account.journal',
        string='Default IPC Journal',
        domain="[('type', 'in', ['general', 'purchase'])]",
        help='Journal used for all IPC accounting entries. '
             'Falls back to first General journal if not set.')

    # ── Deduction component accounts ──────────────────────────────────────────
    retention_payable_account_id = fields.Many2one(
        'account.account',
        string='Retention Payable Account',
        domain="[('active', '=', True)]",
        help='Liability account where retention money (5%) is held.\n'
             'Credited in IPC JE — appears in partner ledger under this account.\n'
             'Debited when retention is released back to the subcontractor.')
    mob_advance_account_id = fields.Many2one(
        'account.account',
        string='Mob Advance Account',
        domain="[('active', '=', True)]",
        help='Asset account for mobilization advances given to subcontractors.\n'
             'Debited when advance is given (Dr Mob Advance / Cr Bank).\n'
             'Credited in IPC JE to clear the advance as it is recovered.')
    security_withheld_account_id = fields.Many2one(
        'account.account',
        string='Security Withheld Account',
        domain="[('active', '=', True)]",
        help='Liability account for security deposits withheld from subcontractors.\n'
             'Credited in IPC JE; debited when security is returned.')
    backcharge_recovery_account_id = fields.Many2one(
        'account.account',
        string='Back Charge Recovery Account',
        domain="[('active', '=', True)]",
        help='Account credited when back charges are recovered from subcontractors in an IPC.')
    other_deductions_account_id = fields.Many2one(
        'account.account',
        string='Other Deductions Account',
        domain="[('active', '=', True)]",
        help='Account credited for the "Other Deductions" field in IPC entries.')

    # ── Helper ────────────────────────────────────────────────────────────────

    @api.model
    def _get_config(self):
        """Return the config for the current company, creating a blank one if absent."""
        config = self.search(
            [('company_id', '=', self.env.company.id)], limit=1)
        if not config:
            config = self.sudo().create({'company_id': self.env.company.id})
        return config

    @api.model
    def action_open_config(self):
        """Open (or auto-create) the singleton config form for the current company."""
        config = self._get_config()
        return {
            'type': 'ir.actions.act_window',
            'name': 'IPC Account Settings',
            'res_model': 'x.ipc.account.config',
            'view_mode': 'form',
            'res_id': config.id,
            'target': 'current',
            'context': {'create': False, 'delete': False},
        }

    _sql_constraints = [
        ('unique_company',
         'UNIQUE(company_id)',
         'Only one IPC account configuration per company is allowed.'),
    ]
