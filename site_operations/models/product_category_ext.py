"""Product category: Add to Inventory Dashboard flag."""

from odoo import models, fields


class ProductCategoryExt(models.Model):
    _inherit = 'product.category'

    x_add_to_dashboard = fields.Boolean(
        string='Show on Inventory Dashboard',
        default=False,
        help='When enabled, ALL products in this category will appear on the '
             'HO Inventory Dashboard, even if the product itself is not individually flagged.',
    )
