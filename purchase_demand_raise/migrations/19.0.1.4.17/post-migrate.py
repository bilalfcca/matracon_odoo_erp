"""Vendor-wise products: fill product vendors from existing confirmed POs and
Vendor Pricelists, then set the vendor on stock moves that have no PO
(issues / transfers / returns) to the product's last vendor."""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    n = env['product.template']._mtr_backfill_vendors()
    env.flush_all()
    # variants mirror the template's last vendor (stored related, column pre-created)
    cr.execute("""
        UPDATE product_product pp
           SET x_last_vendor_id = pt.x_last_vendor_id
          FROM product_template pt
         WHERE pt.id = pp.product_tmpl_id
           AND pp.x_last_vendor_id IS DISTINCT FROM pt.x_last_vendor_id
    """)
    cr.execute("""
        UPDATE stock_move sm
           SET x_vendor_id = pt.x_last_vendor_id
          FROM product_product pp
          JOIN product_template pt ON pt.id = pp.product_tmpl_id
         WHERE sm.product_id = pp.id
           AND sm.x_vendor_id IS NULL
           AND pt.x_last_vendor_id IS NOT NULL
    """)
    moves = cr.rowcount
    cr.execute("""
        UPDATE stock_move_line sml
           SET x_vendor_id = sm.x_vendor_id
          FROM stock_move sm
         WHERE sml.move_id = sm.id
           AND sml.x_vendor_id IS DISTINCT FROM sm.x_vendor_id
    """)
    _logger.info("Vendor-wise products: %s products filled, %s moves given the product's last vendor",
                 n, moves)
