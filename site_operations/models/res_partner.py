from odoo import models, fields, api, _
from odoo.exceptions import ValidationError


class ResPartnerSiteOps(models.Model):
    _inherit = 'res.partner'

    x_cnic = fields.Char(string='CNIC', tracking=True)

    x_description = fields.Char(
        string='Description',
        help='Brief description of this contact (e.g. "Cement Supplier", "Labour Contractor"). '
             'Appears on Liability Sheet lines as "Tag (Description)".'
    )

    x_is_project_entity = fields.Boolean(
        string='Internal Project Entity',
        default=False,
        help='Set on auto-generated partners that represent a project '
             'for inter-project accounting purposes. '
             'Hides them from vendor/customer selection lists.'
    )

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

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        ctx = self.env.context
        # Auto-apply tag when partner is created from a contextual field.
        # The tag is also forced read-only in partner_views.xml so the user
        # cannot change it after the dialog opens.
        if 'category_id' in fields_list:
            tag_name = None
            if ctx.get('site_procurement_vendor'):
                tag_name = 'Local Supplier'
            elif ctx.get('subcontractor_create'):
                tag_name = 'Subcontractor'
            elif ctx.get('site_accountant_vendor'):
                tag_name = 'Vendor'
            if tag_name:
                tag = self.env['res.partner.category'].search(
                    [('name', '=', tag_name)], limit=1
                )
                if tag:
                    res['category_id'] = [(4, tag.id)]
        return res

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

    @api.constrains('category_id', 'x_description')
    def _check_description_required_for_vendor(self):
        """Vendor / Subcontractor / Local Supplier partners must have a description
        so that liability sheet lines are auto-labelled meaningfully."""
        REQUIRED_TAGS = {'Vendor', 'Subcontractor', 'Local Supplier'}
        for partner in self:
            tag_names = set(partner.category_id.mapped('name'))
            if tag_names & REQUIRED_TAGS and not (partner.x_description or '').strip():
                raise ValidationError(_(
                    'Contact "%s": Description is required for Vendor / Subcontractor '
                    'partners (e.g. "Cement Supplier", "Labour Contractor"). '
                    'Please fill in the Description field before saving.'
                ) % (partner.name or ''))
