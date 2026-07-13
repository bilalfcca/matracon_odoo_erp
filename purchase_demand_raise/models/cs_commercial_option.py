from odoo import fields, models


class CsCommercialOption(models.Model):
    """Configurable dropdown options for CS vendor commercial terms.

    Each record is one selectable option for one of the six quick-pick
    dropdowns on the Comparative Statement vendor dialog.  Admins add/
    remove options via Purchase → Configuration → CS Commercial Options.
    """

    _name = 'x.cs.commercial.option'
    _description = 'CS Commercial Term Option'
    _order = 'category, sequence, name'

    CATEGORY_SELECTION = [
        ('delivery_basis', 'Delivery Basis'),
        ('tax_type', 'Taxes & Duties'),
        ('payment_mode', 'Payment Mode'),
        ('delivery_time', 'Delivery Time'),
        ('brand', 'Brand / Origin'),
        ('warranty', 'Warranty'),
    ]

    name = fields.Char(string='Option', required=True, translate=False)
    category = fields.Selection(
        CATEGORY_SELECTION,
        string='Category',
        required=True,
        index=True,
    )
    sequence = fields.Integer(string='Sequence', default=10)
    active = fields.Boolean(string='Active', default=True)
