"""Configurable Terms & Conditions templates for Purchase Orders."""

from odoo import models, fields, api, _


class PoTermsTemplate(models.Model):
    _name = 'x.po.terms.template'
    _description = 'Purchase Order Terms & Conditions Template'
    _order = 'sequence, name'
    _rec_name = 'name'

    name = fields.Char(string='Template Name', required=True, translate=True)
    sequence = fields.Integer(
        string='Sequence', default=10,
        help='Lower number = appears first in the selection dropdown.')
    description = fields.Char(
        string='Description',
        help='Brief note on when to use this template (internal reference only).')
    content = fields.Html(
        string='Terms & Conditions',
        sanitize_attributes=False,
        help='Full text of the Terms & Conditions that will be copied into '
             'the Purchase Order when this template is selected. '
             'The copied text is fully editable per order.')
    active = fields.Boolean(
        string='Active', default=True,
        help='Inactive templates are hidden from the PO selection dropdown '
             'but their historical records remain intact.')

    # ── Smart button: how many POs use this template ─────────────────────────
    po_count = fields.Integer(
        string='Purchase Orders',
        compute='_compute_po_count',
        help='Number of Purchase Orders that have used this template.')

    @api.depends()
    def _compute_po_count(self):
        PO = self.env['purchase.order']
        for tmpl in self:
            tmpl.po_count = PO.search_count([
                ('x_terms_template_id', '=', tmpl.id)
            ])

    def action_view_purchase_orders(self):
        """Open the list of Purchase Orders that used this template."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Purchase Orders — %s') % self.name,
            'res_model': 'purchase.order',
            'view_mode': 'list,form',
            'domain': [('x_terms_template_id', '=', self.id)],
        }
