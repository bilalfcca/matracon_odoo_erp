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

    # ── Vendor payment WHT (Section 153) ───────────────────────────────────
    payment_id = fields.Many2one(
        'account.payment', string='Payment',
        ondelete='restrict')

    # ── Salary WHT (Section 149) ────────────────────────────────────────────
    x_salary_sheet_line_id = fields.Many2one(
        'x.salary.sheet.line', string='Salary Sheet Line',
        ondelete='set null', index=True)
    x_salary_sheet_id = fields.Many2one(
        'x.salary.sheet', string='Salary Sheet',
        related='x_salary_sheet_line_id.sheet_id', store=True, readonly=True)
    x_employee_id = fields.Many2one(
        'hr.employee', string='Employee',
        related='x_salary_sheet_line_id.employee_id', store=True, readonly=True)
    x_gross_salary = fields.Monetary(
        string='Gross Salary', currency_field='currency_id',
        help='Basic + Allowances for the salary period')

    # ── Computed fields that work for both types ────────────────────────────
    partner_id = fields.Many2one(
        'res.partner', string='Vendor / Employee Contact',
        compute='_compute_partner_and_amounts', store=True, readonly=True)
    amount_paid = fields.Monetary(
        string='Gross Amount Paid',
        compute='_compute_partner_and_amounts', store=True, readonly=True)
    payment_date = fields.Date(
        string='Payment / Period Date',
        compute='_compute_partner_and_amounts', store=True, readonly=True)

    wht_amount = fields.Monetary(
        string='WHT Deducted', required=True, tracking=True)
    tax_period = fields.Char(
        string='Tax Period', required=True, tracking=True,
        help='e.g. July 2025 – June 2026')
    issue_date = fields.Date(
        string='Issue Date', default=fields.Date.context_today, required=True)
    currency_id = fields.Many2one(
        'res.currency',
        default=lambda self: self.env.company.currency_id)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('issued', 'Issued'),
    ], default='draft', tracking=True)
    company_id = fields.Many2one(
        'res.company', default=lambda self: self.env.company)

    # Tax line reference (vendor WHT only)
    tax_line_id = fields.Many2one(
        'x.payment.tax.line', string='Tax Line', ondelete='set null')

    @api.depends(
        'payment_id', 'payment_id.partner_id', 'payment_id.x_gross_approved_amount',
        'payment_id.date',
        'x_salary_sheet_line_id', 'x_salary_sheet_line_id.employee_id',
        'x_salary_sheet_line_id.employee_id.work_contact_id',
        'x_gross_salary', 'x_salary_sheet_id.date_from',
    )
    def _compute_partner_and_amounts(self):
        for cert in self:
            if cert.payment_id:
                cert.partner_id = cert.payment_id.partner_id
                cert.amount_paid = cert.payment_id.x_gross_approved_amount
                cert.payment_date = cert.payment_id.date
            elif cert.x_salary_sheet_line_id:
                emp = cert.x_salary_sheet_line_id.employee_id
                cert.partner_id = emp.work_contact_id if emp else False
                cert.amount_paid = cert.x_gross_salary
                cert.payment_date = cert.x_salary_sheet_id.date_from
            else:
                cert.partner_id = False
                cert.amount_paid = 0.0
                cert.payment_date = False

    @api.constrains('payment_id', 'x_salary_sheet_line_id')
    def _check_source(self):
        for cert in self:
            if not cert.payment_id and not cert.x_salary_sheet_line_id:
                raise UserError(_(
                    'A WHT certificate must be linked to either a Payment '
                    '(vendor WHT) or a Salary Sheet Line (salary WHT).'))

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
