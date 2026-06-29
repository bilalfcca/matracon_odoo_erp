from odoo import models, fields, api


class PaymentTaxLine(models.Model):
    _name = 'x.payment.tax.line'
    _description = 'Vendor Payment Tax Line'
    _order = 'sequence, id'

    payment_id = fields.Many2one(
        'account.payment', string='Payment',
        required=True, ondelete='cascade', index=True)
    sequence = fields.Integer(default=10)
    name = fields.Char(
        string='Description',
        help='Optional label (used for salary deductions where no tax record exists).')
    tax_type = fields.Selection([
        ('wht', 'Withholding Tax (WHT)'),
        ('retention', 'Retention Money'),
        ('other', 'Other Tax'),
    ], string='Tax Type', default='wht', required=True)
    tax_id = fields.Many2one(
        'account.tax', string='Tax',
        domain="[('type_tax_use', '=', 'purchase'), ('active', '=', True)]")
    effect = fields.Selection([
        ('deduct', 'Deducted from gross'),
        ('add', 'Added to gross'),
    ], string='Effect on Payment', default='deduct', required=True)
    x_fixed_amount = fields.Monetary(
        string='Fixed Amount',
        currency_field='currency_id',
        help='When set, overrides the tax-rate computation (used for salary deductions '
             'where the amount comes from the payroll sheet, not a tax rate).')
    amount = fields.Monetary(
        string='Amount',
        compute='_compute_amount',
        store=True,
        currency_field='currency_id',
    )
    currency_id = fields.Many2one(
        related='payment_id.currency_id', depends=['payment_id'])

    @api.depends(
        'tax_id', 'x_fixed_amount',
        'payment_id.x_gross_approved_amount', 'payment_id.amount',
        'effect',
    )
    def _compute_amount(self):
        for line in self:
            if line.x_fixed_amount:
                line.amount = line.x_fixed_amount
            else:
                payment = line.payment_id
                base = payment.x_gross_approved_amount or payment.amount or 0.0
                line.amount = (
                    payment._matracon_tax_amount(line.tax_id, base)
                    if line.tax_id else 0.0
                )
