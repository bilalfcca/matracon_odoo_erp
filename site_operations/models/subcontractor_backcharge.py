"""Subcontractor Back Charge — standalone record, processed via IPC submission."""

from markupsafe import Markup

from odoo import models, fields, api, _
from odoo.exceptions import UserError


class SubcontractorBackcharge(models.Model):
    _name = 'x.subcontractor.backcharge'
    _description = 'Subcontractor Back Charge'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date desc, id desc'

    name = fields.Char(
        string='Back Charge Reference', readonly=True,
        default=lambda self: _('New'), copy=False, tracking=True)
    subcontractor_id = fields.Many2one(
        'res.partner', string='Subcontractor',
        required=True,
        domain=[('category_id.name', '=', 'Subcontractor')],
        tracking=True)
    project_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Project',
        required=True, tracking=True)
    description = fields.Char(
        string='Description / Reason', required=True,
        help='Brief description of what this back charge covers.')
    amount = fields.Monetary(
        string='Amount', required=True,
        currency_field='currency_id', tracking=True)
    date = fields.Date(
        string='Date', default=fields.Date.context_today, required=True)
    state = fields.Selection([
        ('pending', 'Pending'),
        ('processed', 'Processed'),
    ], default='pending', string='Status', tracking=True)
    ipc_id = fields.Many2one(
        'x.subcontractor.ipc', string='Processed in IPC',
        readonly=True, copy=False)
    currency_id = fields.Many2one(
        'res.currency',
        default=lambda self: self.env.company.currency_id)
    notes = fields.Text(string='Additional Notes')

    @api.model_create_multi
    def create(self, vals_list):
        Analytic = self.env['account.analytic.account']
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                site_code = Analytic._matracon_site_code_for_id(
                    vals.get('project_analytic_account_id'))
                vals['name'] = Analytic._matracon_ref_with_site(
                    'x.subcontractor.backcharge', site_code)
        return super().create(vals_list)

    def write(self, vals):
        for bc in self:
            if bc.state == 'processed' and set(vals) - {'notes'}:
                raise UserError(_(
                    'Back charge %s has already been processed in IPC %s '
                    'and cannot be modified.'
                ) % (bc.name, bc.ipc_id.name))
        return super().write(vals)
