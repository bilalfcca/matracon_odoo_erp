"""Issued Items tracking — read-only views over the existing Material Issuance
and Return transfers (no new records, no workflow change).

* x.issued.item.report   one row per person × project × item:
                         issued, returned, outstanding, status.
* x.issued.item.history  every issue (+) and return (−) transaction.

Returns are matched like the existing return check (_check_return_quantities):
per contact + project + product.
"""
from odoo import api, fields, models, tools, _

_ISSUE_DOMAIN_SQL = """
    sp.x_transfer_purpose = 'material_issuance'
    AND sp.state = 'done'
    AND sp.x_contact_id IS NOT NULL
    AND sm.state = 'done'
    AND sm.product_id IS NOT NULL
"""

INVENTORY_TYPES = [('asset', 'Asset'), ('consumable', 'Consumable')]
ISSUE_TYPES = [('normal', 'Normal'), ('subcontractor', 'Subcontractor'),
               ('3rd_party', '3rd Party'), ('vehicle', 'Vehicle')]


class IssuedItemHistory(models.Model):
    _name = 'x.issued.item.history'
    _description = 'Issued Items — Issue & Return History'
    _auto = False
    _order = 'date desc, id desc'
    _rec_name = 'reference'

    date = fields.Datetime(readonly=True)
    move_type = fields.Selection([('issue', 'Issued'), ('return', 'Returned')],
                                 string='Transaction', readonly=True)
    picking_id = fields.Many2one('stock.picking', string='Document', readonly=True)
    reference = fields.Char(readonly=True)
    partner_id = fields.Many2one('res.partner', string='Issued To', readonly=True)
    project_id = fields.Many2one('account.analytic.account', string='Project', readonly=True)
    product_id = fields.Many2one('product.product', string='Item', readonly=True)
    categ_id = fields.Many2one('product.category', string='Category', readonly=True)
    uom_id = fields.Many2one('uom.uom', string='UoM', readonly=True)
    qty = fields.Float(string='Quantity', readonly=True, digits='Product Unit of Measure',
                       help='Issued quantity, or returned quantity.')
    signed_qty = fields.Float(string='Net (+issued / −returned)', readonly=True,
                              digits='Product Unit of Measure')
    issue_type = fields.Selection(ISSUE_TYPES, readonly=True)
    inventory_type = fields.Selection(INVENTORY_TYPES, readonly=True)
    return_condition = fields.Selection([('new', 'New'), ('used', 'Used'), ('repairable', 'Repairable'),
                                         ('scrap', 'Scrap')], readonly=True)

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute(f"""
            CREATE OR REPLACE VIEW {self._table} AS (
                SELECT sm.id AS id,
                       COALESCE(sm.date, sp.date_done) AS date,
                       CASE WHEN sp.x_is_return_transfer THEN 'return' ELSE 'issue' END AS move_type,
                       sp.id AS picking_id, sp.name AS reference,
                       COALESCE(rp.commercial_partner_id, sp.x_contact_id) AS partner_id,
                       sp.x_issuance_project_id AS project_id,
                       sm.product_id, pt.categ_id, pt.uom_id,
                       q.qty AS qty,
                       CASE WHEN sp.x_is_return_transfer THEN -q.qty ELSE q.qty END AS signed_qty,
                       sp.x_issue_type AS issue_type, sp.x_inventory_type AS inventory_type,
                       sm.x_return_condition AS return_condition
                  FROM stock_move sm
                  JOIN stock_picking sp ON sp.id = sm.picking_id
                  JOIN res_partner rp ON rp.id = sp.x_contact_id
                  JOIN product_product pp ON pp.id = sm.product_id
                  JOIN product_template pt ON pt.id = pp.product_tmpl_id
                  JOIN LATERAL (
                      SELECT COALESCE(SUM(sml.quantity_product_uom), 0) AS qty
                        FROM stock_move_line sml WHERE sml.move_id = sm.id
                  ) q ON TRUE
                 WHERE {_ISSUE_DOMAIN_SQL}
            )
        """)


class IssuedItemReport(models.Model):
    _name = 'x.issued.item.report'
    _description = 'Issued Items'
    _auto = False
    _order = 'partner_id, project_id, product_id'
    _rec_name = 'product_id'

    partner_id = fields.Many2one('res.partner', string='Issued To', readonly=True)
    project_id = fields.Many2one('account.analytic.account', string='Project', readonly=True)
    product_id = fields.Many2one('product.product', string='Item', readonly=True)
    categ_id = fields.Many2one('product.category', string='Category', readonly=True)
    uom_id = fields.Many2one('uom.uom', string='UoM', readonly=True)
    inventory_type = fields.Selection(INVENTORY_TYPES, readonly=True)
    issue_type = fields.Selection(ISSUE_TYPES, readonly=True)
    issued_qty = fields.Float(string='Issued', readonly=True, digits='Product Unit of Measure')
    returned_qty = fields.Float(string='Returned', readonly=True, digits='Product Unit of Measure')
    outstanding_qty = fields.Float(string='Outstanding (Held)', readonly=True,
                                   digits='Product Unit of Measure')
    first_issue_date = fields.Datetime(string='Issue Date', readonly=True, aggregator='min')
    last_issue_date = fields.Datetime(string='Last Issued', readonly=True, aggregator='max')
    last_return_date = fields.Datetime(string='Last Returned', readonly=True, aggregator='max')
    status = fields.Selection([
        ('outstanding', 'Outstanding'),
        ('partial', 'Partially Returned'),
        ('returned', 'Fully Returned'),
    ], readonly=True)

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute(f"""
            CREATE OR REPLACE VIEW {self._table} AS (
                WITH h AS (SELECT * FROM x_issued_item_history),
                g AS (
                    SELECT partner_id, project_id, product_id,
                           MIN(categ_id) AS categ_id, MIN(uom_id) AS uom_id,
                           MAX(inventory_type) FILTER (WHERE move_type = 'issue') AS inventory_type,
                           MAX(issue_type) FILTER (WHERE move_type = 'issue') AS issue_type,
                           COALESCE(SUM(qty) FILTER (WHERE move_type = 'issue'), 0) AS issued_qty,
                           COALESCE(SUM(qty) FILTER (WHERE move_type = 'return'), 0) AS returned_qty,
                           MIN(date) FILTER (WHERE move_type = 'issue') AS first_issue_date,
                           MAX(date) FILTER (WHERE move_type = 'issue') AS last_issue_date,
                           MAX(date) FILTER (WHERE move_type = 'return') AS last_return_date
                      FROM h
                  GROUP BY partner_id, project_id, product_id
                )
                SELECT ROW_NUMBER() OVER (ORDER BY partner_id, project_id, product_id) AS id,
                       g.*,
                       GREATEST(issued_qty - returned_qty, 0) AS outstanding_qty,
                       CASE
                           WHEN issued_qty - returned_qty <= 0.00001 THEN 'returned'
                           WHEN returned_qty > 0 THEN 'partial'
                           ELSE 'outstanding'
                       END AS status
                  FROM g
                 WHERE issued_qty > 0
            )
        """)

    def action_view_history(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('History — %(item)s / %(who)s') % {
                'item': self.product_id.display_name, 'who': self.partner_id.display_name},
            'res_model': 'x.issued.item.history',
            'view_mode': 'list',
            'domain': [('partner_id', '=', self.partner_id.id), ('product_id', '=', self.product_id.id),
                       ('project_id', '=', self.project_id.id)],
            'context': {'search_default_order_date': 1},
        }


class ResPartnerIssuedItems(models.Model):
    _inherit = 'res.partner'

    x_issued_items_count = fields.Integer(
        string='Issued Items', compute='_compute_issued_items_count',
        help='Number of items this person currently holds (outstanding).')

    def _compute_issued_items_count(self):
        Report = self.env['x.issued.item.report']
        if not Report.has_access('read'):
            self.x_issued_items_count = 0
            return
        data = Report._read_group(
            [('partner_id', 'in', self.commercial_partner_id.ids), ('outstanding_qty', '>', 0)],
            ['partner_id'], ['__count'])
        counts = {p.id: n for p, n in data}
        for partner in self:
            partner.x_issued_items_count = counts.get(partner.commercial_partner_id.id, 0)

    def action_view_issued_items(self):
        self.ensure_one()
        return self.env['x.issued.item.report']._action_for_partner(self.commercial_partner_id)


class IssuedItemReportActions(models.Model):
    _inherit = 'x.issued.item.report'

    @api.model
    def _action_for_partner(self, partner):
        action = self.env['ir.actions.act_window']._for_xml_id('site_operations.action_issued_item_report')
        action.update({
            'name': _('Issued Items — %s') % partner.display_name,
            'domain': [('partner_id', '=', partner.id)],
            'context': {'default_partner_id': partner.id, 'search_default_group_project': 1},
        })
        return action

    @api.model
    def action_open_history_all(self):
        partner_id = self.env.context.get('default_partner_id')
        action = self.env['ir.actions.act_window']._for_xml_id('site_operations.action_issued_item_history')
        if partner_id:
            partner = self.env['res.partner'].browse(partner_id)
            action.update({'name': _('Issue & Return History — %s') % partner.display_name,
                           'domain': [('partner_id', '=', partner_id)]})
        return action


class HrEmployeeIssuedItems(models.Model):
    _inherit = 'hr.employee'

    x_issued_items_count = fields.Integer(string='Issued Items', compute='_compute_x_issued_items_count')

    def _compute_x_issued_items_count(self):
        for emp in self:
            partner = emp.work_contact_id
            emp.x_issued_items_count = partner.x_issued_items_count if partner else 0

    def action_view_issued_items(self):
        self.ensure_one()
        partner = self.sudo()._get_or_create_partner()
        return self.env['x.issued.item.report']._action_for_partner(partner.commercial_partner_id)
