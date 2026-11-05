import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Customer receipts never need CEO approval. Receipts created before the
    fix were saved as 'Pending CEO' (payment_type is readonly on the form, so
    create() never saw 'inbound'); clear that state so the Approve (CEO)
    button no longer shows on them."""
    cr.execute("""
        UPDATE account_payment
           SET x_ceo_approval_state = 'not_required'
         WHERE payment_type = 'inbound'
           AND x_ceo_approval_state IN ('pending', 'submitted')
     RETURNING id
    """)
    ids = [r[0] for r in cr.fetchall()]
    _logger.info("site_operations 19.0.2.2.1: cleared CEO approval on %s customer receipts: %s", len(ids), ids)
