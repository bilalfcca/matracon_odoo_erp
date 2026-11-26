"""Create and fill the new stored vendor / not-delivered columns in SQL
*before* the fields are loaded.

Without this, Odoo creates the columns itself and computes them through the
ORM for every existing stock move, stock move line, product and PO line in a
single pass - on a production-size database that runs the server out of
memory (Odoo.sh: "odoo-bin process got killed"). When the column already
exists Odoo leaves it alone; the post-migrate then fills the product vendors
and copies them to the remaining moves / move lines, also in SQL.
"""


def _add(cr, table, column, sqltype):
    cr.execute(f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "{column}" {sqltype}')


def migrate(cr, version):
    _add(cr, 'product_template', 'x_last_vendor_id', 'integer')
    _add(cr, 'product_template', 'x_last_purchase_date', 'date')
    _add(cr, 'product_product', 'x_last_vendor_id', 'integer')
    _add(cr, 'stock_move', 'x_vendor_id', 'integer')
    _add(cr, 'stock_move_line', 'x_vendor_id', 'integer')
    _add(cr, 'purchase_order_line', 'x_qty_not_delivered', 'numeric')
    _add(cr, 'purchase_order', 'x_force_completed', 'boolean')

    # Receipts from a PO: the PO vendor (commercial entity) - same as the compute.
    cr.execute("""
        UPDATE stock_move sm
           SET x_vendor_id = COALESCE(rp.commercial_partner_id, po.partner_id)
          FROM purchase_order_line pol
          JOIN purchase_order po ON po.id = pol.order_id
     LEFT JOIN res_partner rp ON rp.id = po.partner_id
         WHERE sm.purchase_line_id = pol.id
           AND po.partner_id IS NOT NULL
           AND sm.x_vendor_id IS NULL
    """)
    # Nothing is force-completed yet, so nothing is "not delivered".
    cr.execute("UPDATE purchase_order_line SET x_qty_not_delivered = 0 WHERE x_qty_not_delivered IS NULL")
