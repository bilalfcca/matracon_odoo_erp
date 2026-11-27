from odoo import api, SUPERUSER_ID

from odoo.addons.purchase_demand_raise.models.multi_attachment_mixin import (
    matracon_migrate_binary_to_attachments,
)

# (model, legacy binary field, legacy filename field, new multi-file field, default file name)
SPECS = [
    ('purchase.order', 'x_pm_signed_pr', 'x_pm_signed_pr_filename',
     'x_pm_signed_pr_attachment_ids', 'pm_signed_pr'),
    ('stock.picking', 'x_weight_document', 'x_weight_document_filename',
     'x_weight_document_attachment_ids', 'weight_document'),
]


def migrate(cr, version):
    """Move single-file uploads into the new multi-file fields."""
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    for spec in SPECS:
        matracon_migrate_binary_to_attachments(env, *spec)
