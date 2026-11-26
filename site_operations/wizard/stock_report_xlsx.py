"""Stock Report (Excel) — Inventory → Reports.

Sheet "Stock Summary": one row per product × warehouse for a period —
Opening, Purchased, Returns In, Transfers In, Other In, Material Issued,
Transfers Out, Other Out, Closing (= Opening + ins − outs), last movement
date; Vendor = product's last vendor. HO/Admin also get values at current
average cost (never Site Store).
Sheet "Material Issued" (optional): one row per issued product line.

Built from done stock move lines of the warehouses the user may report on
(Site Store: own warehouse only).
"""
import base64
import io
from collections import defaultdict
from datetime import datetime, time

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class StockReportXlsxWizard(models.TransientModel):
    _name = 'x.stock.report.xlsx.wizard'
    _description = 'Stock Report (Excel)'

    date_from = fields.Date(string='From', required=True,
                            default=lambda s: fields.Date.context_today(s).replace(day=1))
    date_to = fields.Date(string='To', required=True, default=fields.Date.context_today)
    warehouse_ids = fields.Many2many('stock.warehouse', string='Warehouse / Site',
                                     help='Leave empty for all warehouses you can see.')
    categ_ids = fields.Many2many('product.category', string='Product Categories')
    product_ids = fields.Many2many('product.product', string='Products')
    project_ids = fields.Many2many('account.analytic.account', string='Projects',
                                   help='Site projects (their warehouses).')
    vendor_ids = fields.Many2many('res.partner', string='Vendors',
                                  help='Only products whose vendors include one of these.')
    include_issued = fields.Boolean(string='Include Material Issued sheet', default=True)
    include_zero = fields.Boolean(string='Include products with no stock and no movement',
                                  default=False)
    show_values = fields.Boolean(compute='_compute_show_values')

    @api.depends_context('uid')
    def _compute_show_values(self):
        user = self.env.user
        show = not user.has_group('purchase_demand_raise.group_site_store') or user._matracon_is_admin()
        for wiz in self:
            wiz.show_values = show

    allowed_warehouse_ids = fields.Many2many('stock.warehouse', compute='_compute_allowed_sites')
    allowed_project_ids = fields.Many2many('account.analytic.account', compute='_compute_allowed_sites')

    @api.model
    def _mtr_user_site_warehouses(self):
        """Warehouses a site user may report on, or None when unrestricted.

        Head Office / management (Admin, Head Office, Procurement HO, CEO,
        Finance HO) keep full access. Site Store users (store keepers / site
        accountants) are limited to the site(s) they are assigned to in Site
        Project Configuration - the same anchor the stock record rules use.
        """
        user = self.env.user
        if (not user.has_group('purchase_demand_raise.group_site_store')
                or user._matracon_is_head_office() or user._matracon_is_procurement_officer()
                or user._matracon_is_ceo() or user._matracon_is_finance_officer()):
            return None
        cfgs = self.env['x.project.site.config'].sudo().search([('site_user_ids', 'in', user.id)])
        cfgs |= user.sudo().x_site_config_id
        return cfgs.filtered('active').mapped('warehouse_id')

    @api.model
    def _mtr_allowed_sites(self):
        """(warehouses, projects) the current user can pick in the report."""
        own = self._mtr_user_site_warehouses()
        if own is None:
            return (self.env['stock.warehouse'].search([]),
                    self._site_configs().mapped('analytic_account_id'))
        by_wh = self._project_by_warehouse()
        return own, self.env['account.analytic.account'].browse(
            {by_wh[w.id].id for w in own if by_wh.get(w.id)})

    @api.depends_context('uid')
    def _compute_allowed_sites(self):
        whs, projects = self._mtr_allowed_sites()
        for wiz in self:
            wiz.allowed_warehouse_ids = whs
            wiz.allowed_project_ids = projects

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        own = self._mtr_user_site_warehouses()
        if own and 'warehouse_ids' in fields_list:
            res['warehouse_ids'] = [(6, 0, own.ids)]
        return res

    # ── Data ─────────────────────────────────────────────────────────────────
    def _site_configs(self):
        return self.env['x.project.site.config'].sudo().search([])

    def _issue_location_ids(self):
        """Legacy 'Employees' / 'Subcontractor' locations (internal, inside each
        site warehouse): stock there was ISSUED, it is not available."""
        cfg = self._site_configs()
        roots = (cfg.mapped('x_employee_location_id') | cfg.mapped('x_subcontractor_location_id'))
        if not roots:
            return set()
        return set(self.env['stock.location'].sudo().search([('id', 'child_of', roots.ids)]).ids)

    def _project_by_warehouse(self):
        return {c.warehouse_id.id: c.analytic_account_id for c in self._site_configs() if c.warehouse_id}

    def _warehouses(self):
        whs = self.warehouse_ids or self.env['stock.warehouse'].search([])
        if self.project_ids:
            whs &= self._site_configs().filtered(
                lambda c: c.analytic_account_id in self.project_ids).mapped('warehouse_id')
        own = self._mtr_user_site_warehouses()
        if own is not None:
            if not own:
                raise UserError(_('No site warehouse is assigned to your user. '
                                  'Ask the Admin to add you to the Site Project Configuration.'))
            whs = (whs & own) if (self.warehouse_ids or self.project_ids) else own
            if not whs:
                raise UserError(_('You can only view stock of your own site: %s.',
                                  ', '.join(own.mapped('name'))))
        return whs

    @staticmethod
    def _classify(ml, inbound, issue_locs=()):
        """Movement bucket for a done move line entering/leaving a warehouse."""
        pick = ml.picking_id
        other = ml.location_id if inbound else ml.location_dest_id
        if other.id in issue_locs:          # legacy Employees / Subcontractor location
            return 'returns_in' if inbound else 'issued'
        if pick and pick.x_transfer_purpose == 'material_issuance' and not pick.x_is_return_transfer:
            return 'issued' if not inbound else 'returns_in'
        if pick and pick.x_is_return_transfer and inbound:
            return 'returns_in'
        if other.usage == 'supplier':
            return 'purchased' if inbound else 'other_out'
        if (pick and pick.x_transfer_purpose == 'site_to_site') or other.usage == 'internal':
            return 'transfer_in' if inbound else 'transfer_out'
        return 'other_in' if inbound else 'other_out'

    def _compute_rows(self):
        self.ensure_one()
        if self.date_to < self.date_from:
            raise UserError(_('"To" date is before "From" date.'))
        start = datetime.combine(self.date_from, time.min)
        end = datetime.combine(self.date_to, time.max)
        whs = self._warehouses()
        issue_locs = self._issue_location_ids()
        loc_wh = {}
        for wh in whs:
            for loc in self.env['stock.location'].sudo().search([
                    ('id', 'child_of', wh.view_location_id.id), ('usage', '=', 'internal')]):
                if loc.id not in issue_locs:
                    loc_wh[loc.id] = wh
        if not loc_wh:
            return [], []
        domain = [('state', '=', 'done'), ('date', '<=', end),
                  '|', ('location_id', 'in', list(loc_wh)), ('location_dest_id', 'in', list(loc_wh))]
        if self.categ_ids:
            domain.append(('product_id.categ_id', 'child_of', self.categ_ids.ids))
        if self.vendor_ids:
            domain.append(('product_id.x_vendor_ids', 'in', self.vendor_ids.ids))
        if self.product_ids:
            domain.append(('product_id', 'in', self.product_ids.ids))
        # Warehouses are already limited to what the user may report on (Site
        # Store: own warehouse only), so read that warehouse's moves in full —
        # otherwise moves without a transfer document (e.g. adjustments) would be
        # missing and Closing would not match the real on-hand.
        mls = self.env['stock.move.line'].sudo().search(domain, order='date, id')
        buckets = ('purchased', 'returns_in', 'transfer_in', 'other_in',
                   'issued', 'transfer_out', 'other_out')
        data = defaultdict(lambda: dict({b: 0.0 for b in buckets}, opening=0.0, last=False))
        issued = []
        for ml in mls:
            qty = ml.quantity_product_uom
            src_wh, dst_wh = loc_wh.get(ml.location_id.id), loc_wh.get(ml.location_dest_id.id)
            if src_wh == dst_wh:
                continue   # move inside the same warehouse: no stock change
            in_period = ml.date >= start
            for wh, inbound in ((dst_wh, True), (src_wh, False)):
                if not wh:
                    continue
                row = data[(ml.product_id.id, wh.id)]
                if not in_period:
                    row['opening'] += qty if inbound else -qty
                    continue
                kind = self._classify(ml, inbound, issue_locs)
                row[kind] += qty
                if not row['last'] or ml.date > row['last']:
                    row['last'] = ml.date
                if not inbound and self.include_issued and kind == 'issued':
                    pick = ml.picking_id
                    issued.append({
                        'date': ml.date, 'ref': pick.name, 'product': ml.product_id,
                        'qty': qty, 'warehouse': wh, 'picking': pick,
                    })
        if self.include_zero:
            Product = self.env['product.product']
            pdomain = [('is_storable', '=', True)]
            if self.categ_ids:
                pdomain.append(('categ_id', 'child_of', self.categ_ids.ids))
            if self.vendor_ids:
                pdomain.append(('x_vendor_ids', 'in', self.vendor_ids.ids))
            if self.product_ids:
                pdomain.append(('id', 'in', self.product_ids.ids))
            for product in Product.search(pdomain):
                for wh in whs:
                    data[(product.id, wh.id)]
        rows = []
        Product, WH = self.env['product.product'], self.env['stock.warehouse']
        project_by_wh = self._project_by_warehouse()
        for (pid, whid), r in data.items():
            ins = r['purchased'] + r['returns_in'] + r['transfer_in'] + r['other_in']
            outs = r['issued'] + r['transfer_out'] + r['other_out']
            closing = r['opening'] + ins - outs
            if not self.include_zero and not any(
                    abs(v) > 1e-9 for v in (r['opening'], ins, outs, closing)):
                continue
            rows.append(dict(r, product=Product.browse(pid), warehouse=WH.browse(whid),
                             project=project_by_wh.get(whid, self.env['account.analytic.account']),
                             ins=ins, outs=outs, closing=closing))
        rows.sort(key=lambda r: (r['warehouse'].name or '', r['product'].categ_id.complete_name or '',
                                 r['product'].display_name or ''))
        return rows, issued

    # ── Excel ────────────────────────────────────────────────────────────────
    def action_export(self):
        self.ensure_one()
        import xlsxwriter
        rows, issued = self._compute_rows()
        show_values = self.show_values
        buf = io.BytesIO()
        wb = xlsxwriter.Workbook(buf, {'in_memory': True})
        title = wb.add_format({'bold': True, 'font_size': 14})
        meta = wb.add_format({'italic': True, 'font_color': '#555555'})
        head = wb.add_format({'bold': True, 'bg_color': '#1a3a5c', 'font_color': 'white',
                              'border': 1, 'text_wrap': True, 'valign': 'vcenter', 'align': 'center'})
        txt = wb.add_format({'border': 1})
        num = wb.add_format({'border': 1, 'num_format': '#,##0.00'})
        dt = wb.add_format({'border': 1, 'num_format': 'dd/mm/yyyy'})
        tot = wb.add_format({'bold': True, 'border': 1, 'bg_color': '#eef1f7', 'num_format': '#,##0.00'})
        totl = wb.add_format({'bold': True, 'border': 1, 'bg_color': '#eef1f7'})
        company = self.env.company.name
        whs_label = ', '.join(self._warehouses().mapped('name')) or _('All')
        period = '%s to %s' % (self.date_from.strftime('%d/%m/%Y'), self.date_to.strftime('%d/%m/%Y'))
        printed = fields.Datetime.context_timestamp(self, fields.Datetime.now()).strftime('%d/%m/%Y %H:%M')

        # Sheet 1: Stock Summary
        ws = wb.add_worksheet(_('Stock Summary'))
        ws.write(0, 0, '%s — %s' % (company, _('Stock Report')), title)
        ws.write(1, 0, _('Period: %(p)s   |   Warehouse / Site: %(w)s   |   Printed: %(d)s') % {
            'p': period, 'w': whs_label, 'd': printed}, meta)
        cols = [
            (_('Product'), 34), (_('Internal Ref'), 14), (_('Category'), 22), (_('Vendor'), 24),
            (_('UoM'), 8), (_('Warehouse / Site'), 18), (_('Opening Stock'), 12),
            (_('Purchased Qty'), 12), (_('Returns In'), 11), (_('Transfers In'), 11),
            (_('Other In (Adjust.)'), 12), (_('Material Issued Qty'), 12), (_('Transfers Out'), 11),
            (_('Other Out (Adjust./Vendor Returns)'), 14), (_('Closing / Available Stock'), 13),
            (_('Last Movement Date'), 13),
        ]
        if show_values:
            cols += [(_('Avg. Cost'), 12), (_('Opening Value'), 14), (_('Closing Value'), 14)]
        hr = 3
        for c, (label, width) in enumerate(cols):
            ws.write(hr, c, label, head)
            ws.set_column(c, c, width)
        ws.set_row(hr, 36)
        ws.freeze_panes(hr + 1, 1)
        keys = ('opening', 'purchased', 'returns_in', 'transfer_in', 'other_in',
                'issued', 'transfer_out', 'other_out', 'closing')
        totals = defaultdict(float)
        r_i = hr + 1
        for r in rows:
            p = r['product']
            ws.write(r_i, 0, p.name or '', txt)
            ws.write(r_i, 1, p.default_code or '', txt)
            ws.write(r_i, 2, p.categ_id.complete_name or '', txt)
            ws.write(r_i, 3, p.x_last_vendor_id.display_name or '', txt)
            ws.write(r_i, 4, p.uom_id.name or '', txt)
            ws.write(r_i, 5, r['warehouse'].name or '', txt)
            for k_i, k in enumerate(keys):
                ws.write_number(r_i, 6 + k_i, r[k], num)
                totals[k] += r[k]
            if r['last']:
                ws.write_datetime(r_i, 15, fields.Datetime.context_timestamp(self, r['last']).replace(tzinfo=None), dt)
            else:
                ws.write(r_i, 15, '', txt)
            if show_values:
                cost = p.standard_price or 0.0
                ws.write_number(r_i, 16, cost, num)
                ws.write_number(r_i, 17, r['opening'] * cost, num)
                ws.write_number(r_i, 18, r['closing'] * cost, num)
                totals['opening_value'] += r['opening'] * cost
                totals['closing_value'] += r['closing'] * cost
            r_i += 1
        ws.write(r_i, 0, _('Total (%s rows)') % len(rows), totl)
        for c in range(1, 6):
            ws.write(r_i, c, '', totl)
        for k_i, k in enumerate(keys):
            ws.write_number(r_i, 6 + k_i, totals[k], tot)
        ws.write(r_i, 15, '', totl)
        if show_values:
            ws.write(r_i, 16, '', totl)
            ws.write_number(r_i, 17, totals['opening_value'], tot)
            ws.write_number(r_i, 18, totals['closing_value'], tot)
        ws.write(r_i + 2, 0, _('Closing = Opening + Purchased + Returns In + Transfers In + Other In '
                               '− Material Issued − Transfers Out − Other Out. Quantities in the '
                               'product\'s unit of measure.') + (
            _(' Values at current average cost.') if show_values else ''), meta)

        # Sheet 2: Material Issued
        if self.include_issued:
            ws2 = wb.add_worksheet(_('Material Issued'))
            ws2.write(0, 0, '%s — %s' % (company, _('Material Issued')), title)
            ws2.write(1, 0, _('Period: %(p)s   |   Warehouse / Site: %(w)s') % {'p': period, 'w': whs_label}, meta)
            cols2 = [(_('Date'), 12), (_('Issuance Ref'), 18), (_('Product'), 34), (_('Internal Ref'), 14),
                     (_('Category'), 22), (_('Issued Qty'), 12), (_('UoM'), 8), (_('Warehouse / Site'), 18),
                     (_('Issued To'), 26), (_('Issue Type'), 14), (_('Project'), 24)]
            for c, (label, width) in enumerate(cols2):
                ws2.write(3, c, label, head)
                ws2.set_column(c, c, width)
            ws2.freeze_panes(4, 0)
            issue_types = dict(self.env['stock.picking']._fields['x_issue_type'].selection)
            r2 = 4
            total_issued = 0.0
            for it in sorted(issued, key=lambda x: (x['date'], x['ref'] or '')):
                pick, p = it['picking'], it['product']
                issued_to = (pick.x_fleet_vehicle_id.display_name if pick.x_issue_type == 'vehicle'
                             else pick.x_contact_id.display_name) or ''
                ws2.write_datetime(r2, 0, fields.Datetime.context_timestamp(self, it['date']).replace(tzinfo=None), dt)
                ws2.write(r2, 1, it['ref'] or '', txt)
                ws2.write(r2, 2, p.name or '', txt)
                ws2.write(r2, 3, p.default_code or '', txt)
                ws2.write(r2, 4, p.categ_id.complete_name or '', txt)
                ws2.write_number(r2, 5, it['qty'], num)
                ws2.write(r2, 6, p.uom_id.name or '', txt)
                ws2.write(r2, 7, it['warehouse'].name or '', txt)
                ws2.write(r2, 8, issued_to, txt)
                ws2.write(r2, 9, issue_types.get(pick.x_issue_type, '') or '', txt)
                ws2.write(r2, 10, pick.x_issuance_project_id.display_name or '', txt)
                total_issued += it['qty']
                r2 += 1
            ws2.write(r2, 0, _('Total'), totl)
            for c in range(1, 11):
                if c == 5:
                    ws2.write_number(r2, 5, total_issued, tot)
                else:
                    ws2.write(r2, c, '', totl)
        wb.close()
        name = 'Stock Report %s to %s.xlsx' % (self.date_from, self.date_to)
        att = self.env['ir.attachment'].create({
            'name': name, 'datas': base64.b64encode(buf.getvalue()),
            'res_model': self._name, 'res_id': self.id,
            'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        })
        return {'type': 'ir.actions.act_url', 'url': '/web/content/%s?download=true' % att.id,
                'target': 'self'}
