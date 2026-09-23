"""Tiny "pick a record, then run" wizards for fix tools whose underlying
method is inherently record-specific (it corrects one particular
petty cash request / site configuration / fleet log entry), unlike the
global sweep-style fix tools which just run directly from a Server Action
with no picker needed.

Each wizard only ever calls a pre-existing model method - none of the
underlying fix logic lives here or is changed here. See
data/fix_tools_data.xml for the Server Actions that open these wizards.
"""
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class XPettyCashGlFixWizard(models.TransientModel):
    """Fix Petty Cash GL Entry - was a button on the x.petty.cash.request
    form (site_operations/models/petty_cash.py: action_fix_petty_cash_entry,
    unchanged). Picking the request here replaces opening it and clicking
    the button there.
    """
    _name = 'x.petty.cash.gl.fix.wizard'
    _description = 'Fix Petty Cash GL Entry'

    petty_cash_request_id = fields.Many2one(
        'x.petty.cash.request', string='Petty Cash Request', required=True,
        domain=[('state', 'in', ('released', 'confirmed')), ('payment_id', '!=', False)],
        help='Only requests that have been released (a payment exists) can be fixed.',
    )

    def action_apply(self):
        self.ensure_one()
        if not (self.env.user.has_group('site_operations.group_finance_ho')
                or self.env.user.has_group('purchase_demand_raise.group_matracon_admin')):
            raise UserError(_('Only Finance HO or Matracon Admin can run this action.'))
        return self.petty_cash_request_id.action_fix_petty_cash_entry()


class XSiteConfigFixWizard(models.TransientModel):
    """Shared picker for the three Site Configuration fix tools - was three
    separate buttons on the x.project.site.config form
    (site_operations/models/project_site_config.py:
    action_fix_old_interproject_entries, action_fix_material_issue_accounts,
    action_fix_analytic_visibility - all unchanged). Each Server Action in
    Fix Tools opens this same wizard with a different default fix_type via
    context, so the list still shows three distinct, clearly-named entries.
    """
    _name = 'x.site.config.fix.wizard'
    _description = 'Fix Site Configuration Data'

    fix_type = fields.Selection([
        ('interproject', 'Merge Old Inter-Project Accounts'),
        ('material_issue', 'Apply Material Issue Account to Old Issuances'),
        ('analytic_visibility', 'Rebuild Analytic Visibility Links'),
    ], required=True, default='interproject')
    site_config_id = fields.Many2one(
        'x.project.site.config', string='Site Configuration', required=True,
        help='The fix runs for this site (Inter-Project merge additionally sweeps '
             'all sites at once, since the account is shared).',
    )
    fix_type_help = fields.Text(compute='_compute_fix_type_help')

    @api.depends('fix_type')
    def _compute_fix_type_help(self):
        help_by_type = {
            'interproject': _(
                'Updates ALL previously posted inter-project journal lines (across all '
                'sites) from the old hardcoded accounts (13100/21100) to this site\'s '
                'configured Inter-Project Account. Run this once after setting the account.'
            ),
            'material_issue': _(
                'Updates all previously posted material issuance journal entries for this '
                'site to use its configured Material Issue Account.'
            ),
            'analytic_visibility': _(
                'Scans all posted accounting entries for this site\'s analytic account and '
                'ensures they are visible to the site accountant and appear correctly in '
                'all accounting reports.'
            ),
        }
        for wizard in self:
            wizard.fix_type_help = help_by_type.get(wizard.fix_type, '')

    def action_apply(self):
        self.ensure_one()
        if not (self.env.user.has_group('purchase_demand_raise.group_matracon_admin')
                or self.env.user.has_group('base.group_system')):
            raise UserError(_('Only Matracon Admin or System Administrator can run this action.'))
        config = self.site_config_id
        if self.fix_type == 'interproject':
            return config.action_fix_old_interproject_entries()
        if self.fix_type == 'material_issue':
            return config.action_fix_material_issue_accounts()
        return config.action_fix_analytic_visibility()


class XFleetGlFixWizard(models.TransientModel):
    """Unlink GL - was a button on the fleet.vehicle.log.services form
    (matracon_fleet/models/fleet_log_entry_ext.py: action_unlink_gl,
    unchanged).
    """
    _name = 'x.fleet.gl.fix.wizard'
    _description = 'Fix Fleet Service Log GL Link'

    log_entry_id = fields.Many2one(
        'fleet.vehicle.log.services', string='Fleet Service Log', required=True,
        domain=[('x_account_move_id', '!=', False)],
        help='Only log entries with a linked journal entry can be fixed.',
    )

    def action_apply(self):
        self.ensure_one()
        if not (self.env.user.has_group('purchase_demand_raise.group_matracon_admin')
                or self.env.user.has_group('base.group_system')):
            raise UserError(_('Only Matracon Admin or System Administrator can run this action.'))
        log_entry = self.log_entry_id
        # action_unlink_gl() itself returns nothing (it just clears the link)
        # - show a confirmation notification instead of leaving the dialog open.
        log_entry.action_unlink_gl()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('GL Link Removed'),
                'message': _('The journal entry link on %(name)s has been detached. '
                             'The journal entry itself was not reversed.', name=log_entry.display_name),
                'type': 'success',
                'sticky': False,
            },
        }
