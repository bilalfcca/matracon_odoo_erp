from odoo import models, fields, api, _
from odoo.exceptions import ValidationError


class ResPartnerSiteOps(models.Model):
    _inherit = 'res.partner'

    x_material_issuance_count = fields.Integer(
        string='Material Issuances',
        compute='_compute_x_material_issuance_count',
    )

    def _compute_x_material_issuance_count(self):
        StockPicking = self.env['stock.picking'].sudo()
        for partner in self:
            partner.x_material_issuance_count = StockPicking.search_count([
                ('x_contact_id', '=', partner.id),
                ('x_transfer_purpose', 'in', ['material_issuance', 'site_to_site']),
            ])

    def action_view_material_issuances(self):
        self.ensure_one()
        return {
            'name': _('Issuances — %s') % self.name,
            'type': 'ir.actions.act_window',
            'res_model': 'stock.picking',
            'view_mode': 'list,form',
            'domain': [
                ('x_contact_id', '=', self.id),
                ('x_transfer_purpose', 'in', ['material_issuance', 'site_to_site']),
            ],
            'context': {},
        }

    @api.constrains('category_id')
    def _check_category_required_for_site_store(self):
        """Site store users must always set at least one tag on partners."""
        if self.env.user.has_group('purchase_demand_raise.group_site_store'):
            for partner in self:
                if not partner.category_id:
                    raise ValidationError(_(
                        'Contact "%s": Category (Tag) is required. '
                        'Please add "Subcontractor" or another tag.'
                    ) % (partner.name or ''))
