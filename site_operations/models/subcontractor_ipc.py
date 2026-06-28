from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class SubcontractorIPC(models.Model):
    _name = 'x.subcontractor.ipc'
    _description = 'Subcontractor Interim Payment Certificate'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'ipc_date desc, id desc'

    name = fields.Char(
        string='IPC No', readonly=True,
        default=lambda self: _('New'), copy=False)
    subcontractor_id = fields.Many2one(
        'res.partner', string='Subcontractor',
        required=True, domain=[('category_id.name', '=', 'Subcontractor')], tracking=True)
    project_analytic_account_id = fields.Many2one(
        'account.analytic.account',
        string='Project', required=True, tracking=True)
    ipc_date = fields.Date(
        string='IPC Date',
        default=fields.Date.context_today, required=True)
    period = fields.Char(
        string='Period / Billing Month', required=True, tracking=True)

    # Work amounts
    gross_work_done = fields.Monetary(
        string='Gross Work Done', required=True,
        currency_field='currency_id', tracking=True)
    advance_recovery = fields.Monetary(
        string='Advance Recovery',
        currency_field='currency_id', tracking=True)
    retention_pct = fields.Float(
        string='Retention %', digits=(5, 2), tracking=True)
    retention_amount = fields.Monetary(
        string='Retention Amount',
        compute='_compute_deductions', store=True,
        currency_field='currency_id')
    security_withheld = fields.Monetary(
        string='Security Withheld',
        currency_field='currency_id', tracking=True)
    backcharge_amount = fields.Monetary(
        string='Backcharge Deductions',
        currency_field='currency_id', tracking=True)
    other_deductions = fields.Monetary(
        string='Other Deductions',
        currency_field='currency_id', tracking=True)

    total_deductions = fields.Monetary(
        string='Total Deductions',
        compute='_compute_deductions', store=True,
        currency_field='currency_id')
    net_payable = fields.Monetary(
        string='Net Payable',
        compute='_compute_deductions', store=True,
        currency_field='currency_id')

    currency_id = fields.Many2one(
        'res.currency',
        default=lambda self: self.env.company.currency_id)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('submitted', 'Submitted'),
        ('approved', 'Approved'),
        ('paid', 'Paid'),
    ], default='draft', tracking=True)

    liability_sheet_id = fields.Many2one(
        'x.liability.sheet', string='Liability Sheet',
        domain=[('state', 'in', ('approved', 'paid'))], tracking=True)
    payment_ids = fields.One2many(
        'account.payment', 'x_ipc_id', string='Payments')
    payment_count = fields.Integer(compute='_compute_payment_count')

    notes = fields.Text(string='Notes / Scope of Work')

    @api.depends(
        'gross_work_done', 'retention_pct', 'advance_recovery',
        'security_withheld', 'backcharge_amount', 'other_deductions',
    )
    def _compute_deductions(self):
        for ipc in self:
            ipc.retention_amount = (
                ipc.gross_work_done * ipc.retention_pct / 100.0)
            ipc.total_deductions = (
                ipc.retention_amount + ipc.advance_recovery
                + ipc.security_withheld + ipc.backcharge_amount
                + ipc.other_deductions
            )
            ipc.net_payable = max(
                ipc.gross_work_done - ipc.total_deductions, 0.0)

    def _compute_payment_count(self):
        for ipc in self:
            ipc.payment_count = len(ipc.payment_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = (
                    self.env['ir.sequence'].next_by_code('x.subcontractor.ipc')
                    or _('New')
                )
        return super().create(vals_list)

    def action_submit(self):
        for ipc in self:
            if ipc.state != 'draft':
                raise UserError(_('Only draft IPCs can be submitted.'))
            ipc.state = 'submitted'
            ipc.message_post(
                body=Markup(_('IPC <b>%s</b> submitted for approval.'))
                % ipc.name)

    def action_approve(self):
        for ipc in self:
            if ipc.state != 'submitted':
                raise UserError(_('Only submitted IPCs can be approved.'))
            ipc.state = 'approved'
            ipc.message_post(
                body=Markup(
                    _('IPC <b>%s</b> approved. Net payable: '
                      '<b>%s %.2f</b>')
                ) % (ipc.name, ipc.currency_id.symbol, ipc.net_payable))

    def action_view_payments(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('IPC Payments'),
            'res_model': 'account.payment',
            'view_mode': 'list,form',
            'domain': [('x_ipc_id', '=', self.id)],
        }

    def action_print_ipc(self):
        return self.env.ref('site_operations.action_report_subcontractor_ipc').report_action(self)
