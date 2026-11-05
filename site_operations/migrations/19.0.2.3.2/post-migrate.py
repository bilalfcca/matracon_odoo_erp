import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Batches reversed before this fix still link their (now draft) old
    payments on each line, which keeps the lines read-only. Release them on
    every draft / cancelled batch so the lines can be edited and re-posted."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    lines = env['x.batch.payment.line'].search([
        ('batch_id.state', 'in', ('draft', 'cancelled')),
        '|', ('payment_id', '!=', False), ('x_fbr_payment_id', '!=', False),
    ])
    lines._matracon_release_unposted_payments()
    _logger.info("site_operations 19.0.2.3.2: checked %s batch lines for reversed payments", len(lines))
