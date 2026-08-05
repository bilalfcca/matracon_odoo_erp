"""Product template: enforce 'Track Inventory' for all goods by default.

Odoo 19 ships with is_storable=False for new Goods products, which means
inventory moves are not tracked.  Matracon always wants physical goods
to be storable so stock levels, valuations, and site store dashboards
work correctly without manual configuration after every product creation
or import.

Three mechanisms ensure this:
1. default_get  — new-form UI: checkbox is pre-ticked when you open New Product
2. create       — direct-create / CSV/XLSX import: rows that don't specify
                  is_storable get True automatically
3. Retroactive SQL run once (see shell command in commit notes)
"""
from odoo import models, fields, api


class ProductTemplateSiteOps(models.Model):
    _inherit = 'product.template'

    x_add_to_dashboard = fields.Boolean(
        string='Show on Inventory Dashboard',
        default=False,
        help='When enabled, this product will appear on the HO Inventory Dashboard '
             'with on-hand quantity and PO-based valuation.',
    )
    x_show_zero_qty = fields.Boolean(
        string='Show even if quantity is 0',
        default=False,
        help='When enabled, this product will appear on the Inventory Dashboard '
             'even when its on-hand quantity is zero.',
    )

    @api.model
    def default_get(self, fields_list):
        """Pre-tick 'Track Inventory' on the New Product form."""
        defaults = super().default_get(fields_list)
        if 'is_storable' in fields_list and not defaults.get('is_storable'):
            defaults['is_storable'] = True
        return defaults

    @api.model_create_multi
    def create(self, vals_list):
        """Ensure imported / programmatically created Goods products are storable."""
        for vals in vals_list:
            # Only applicable to Goods (type='consu'); services/combos are
            # unaffected.  Respect an explicit False if the caller set it.
            if vals.get('type', 'consu') == 'consu' and 'is_storable' not in vals:
                vals['is_storable'] = True
        return super().create(vals_list)
