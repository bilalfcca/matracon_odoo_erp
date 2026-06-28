from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class WHTCertificate(models.Model):
    _name = 'x.wht.certificate'
    _description = 'WHT Deduction Certificate'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'issue_date desc, id desc'

    name = fields.Char(
        string='Certificate No', readonly=True,
        default=lambda self: _('New'), copy=False)
    payment_id = fields.Many2one(
        'account.payment', string='Payment',
        required=True, ondelete='restrict')
    partner_id = fields.Many2one(
        related='payment_id.partner_id', store=True,
        readonly=True, string='Vendor')
    amount_paid = fields.Monetary(
        related='payment_id.x_gross_approved_amount', store=True,
        readonly=True, string='Gross Amount Paid')
    wht_amount = fields.Monetary(
        string='WHT Deducted', required=True, tracking=True)
    tax_period = fields.Char(
        string='Tax Period', required=True, tracking=True,
        help='e.g. July 2025 – June 2026')
    issue_date = fields.Date(
        string='Issue Date', default=fields.Date.context_today, required=True)
    payment_date = fields.Date(
        related='payment_id.date', store=True, readonly=True)
    currency_id = fields.Many2one(
        'res.currency',
        default=lambda self: self.env.company.currency_id)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('issued', 'Issued'),
    ], default='draft', tracking=True)
    company_id = fields.Many2one(
        'res.company', default=lambda self: self.env.company)

    # Tax line reference
    tax_line_id = fields.Many2one(
        'x.payment.tax.line', string='Tax Line', ondelete='set null')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = (
                    self.env['ir.sequence'].next_by_code('x.wht.certificate')
                    or _('New')
                )
        return super().create(vals_list)

    def action_issue(self):
        for cert in self:
            if cert.state != 'draft':
                raise UserError(_('Only draft certificates can be issued.'))
            cert.state = 'issued'
            cert.message_post(
                body=Markup(_('WHT Certificate <b>%s</b> issued.')) % cert.name)

    @api.model
    def _generate_from_payment(self, payment):
        """Generate a WHT certificate from a posted payment's WHT tax line."""
        wht_lines = payment.x_tax_line_ids.filtered(
            lambda l: l.tax_type == 'wht' and l.amount > 0)
        if not wht_lines:
            raise UserError(_('No WHT deduction found on this payment.'))
        total_wht = sum(wht_lines.mapped('amount'))
        cert = self.create({
            'payment_id': payment.id,
            'wht_amount': total_wht,
            'tax_period': payment.date.strftime('%B %Y') if payment.date else '',
            'issue_date': fields.Date.today(),
            'tax_line_id': wht_lines[0].id,
        })
        return cert

    def action_print_certificate(self):
        return self.env.ref(
            'site_operations.action_report_wht_certificate'
        ).report_action(self)
