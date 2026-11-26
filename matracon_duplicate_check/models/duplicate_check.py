"""Duplicate detection rules.

The browser calls ``x.duplicate.check.check_values(model, values)`` just
before saving a NEW record (``values`` = the form's save values). Each active
rule for that model returns existing records that look the same. Only records
the user can read are returned. Nothing is ever blocked here.
"""
import re
from datetime import timedelta

from markupsafe import Markup, escape

from odoo import api, fields, models, _

MAX_MATCHES = 5


def _norm(text):
    """lower-case, letters/digits only — 'M/s. Khan Builders' == 'ms khan builders'."""
    return re.sub(r'[^a-z0-9]', '', (text or '').lower())


def _digits(text, last=None):
    d = re.sub(r'\D', '', text or '')
    return d[-last:] if last and len(d) >= last else d


def _m2o(value):
    if isinstance(value, (list, tuple)):
        value = value[0] if value else False
    if isinstance(value, dict):
        value = value.get('id')
    return value or False


def _x2m_lines(commands):
    """Line values from one2many save commands (new / updated lines)."""
    out = []
    for cmd in commands or []:
        if isinstance(cmd, (list, tuple)) and len(cmd) >= 3 and cmd[0] in (0, 1) and isinstance(cmd[2], dict):
            out.append(cmd[2])
    return out


class DuplicateRule(models.Model):
    _name = 'x.duplicate.rule'
    _description = 'Duplicate Check Rule'
    _order = 'model_label, sequence, id'

    code = fields.Char(required=True, readonly=True)
    model = fields.Char(string='Technical Model', required=True, readonly=True)
    model_label = fields.Char(string='Record Type', compute='_compute_model_label', store=True)
    name = fields.Char(string='Rule', required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _unique_code = models.Constraint('UNIQUE(code)', 'Rule code must be unique.')

    @api.depends('model')
    def _compute_model_label(self):
        for rule in self:
            m = self.env['ir.model'].sudo()._get(rule.model) if rule.model else False
            rule.model_label = m.name if m else rule.model


class DuplicateCheck(models.AbstractModel):
    _name = 'x.duplicate.check'
    _description = 'Duplicate Check'

    # ── Public API (called by the form) ──────────────────────────────────────
    @api.model
    def get_checked_models(self):
        return sorted(set(self.env['x.duplicate.rule'].sudo().search([]).mapped('model')))

    @api.model
    def check_values(self, model, values, context=None):
        Model = self.env.get(model)
        if Model is None or not isinstance(values, dict):
            return []
        ctx = dict(context or {})
        result = []
        seen = set()
        for rule in self.env['x.duplicate.rule'].sudo().search([('model', '=', model)]):
            method = getattr(self, '_rule_' + rule.code, None)
            if not method:
                continue
            try:
                ids = method(Model, values, ctx) or []
            except Exception:  # noqa: BLE001 - a rule must never break saving
                continue
            recs = Model.search([('id', 'in', list(ids))], limit=MAX_MATCHES) if ids else Model.browse()
            recs = recs.filtered(lambda r: r.id not in seen)
            if recs:
                seen.update(recs.ids)
                result.append({'rule': rule.name, 'records': [self._details(r) for r in recs]})
        return result

    @api.model
    def log_forced(self, model, res_id, matched_ids):
        rec = self.env[model].browse(res_id).exists()
        if not rec or not hasattr(rec, 'message_post'):
            return False
        others = self.env[model].browse(matched_ids).exists()
        links = Markup(', ').join(
            Markup('<a href="/odoo/%s/%s">%s</a>') % (model, o.id, o.display_name) for o in others)
        rec.message_post(body=Markup(_(
            'Created despite a possible duplicate (confirmed by %(user)s): %(links)s')) % {
                'user': escape(self.env.user.name), 'links': links})
        return True

    # ── Display ──────────────────────────────────────────────────────────────
    @api.model
    def _details(self, rec):
        parts = []
        for fname, label in (('partner_id', _('Partner')), ('x_contact_id', _('Issued To')),
                             ('employee_id', _('Employee')), ('ref', _('Ref')),
                             ('default_code', _('Code')), ('vat', _('NTN')), ('x_cnic', _('CNIC')),
                             ('email', _('Email')), ('phone', _('Phone')),
                             ('invoice_date', _('Date')), ('date', _('Date')), ('date_order', _('Date')),
                             ('scheduled_date', _('Date')), ('expense_date', _('Date')),
                             ('amount_total', _('Amount')), ('amount', _('Amount')),
                             ('requested_amount', _('Amount')),
                             ('x_project_analytic_account_id', _('Project')),
                             ('project_analytic_account_id', _('Project')),
                             ('state', _('Status'))):
            if fname not in rec._fields or any(p.startswith(label + ':') for p in parts):
                continue
            try:
                field = rec._fields[fname]
                value = rec[fname]
                if not value:
                    continue
                if field.type == 'many2one':
                    value = value.display_name
                elif field.type == 'selection':
                    value = dict(field._description_selection(self.env)).get(value, value)
                elif field.type in ('float', 'monetary'):
                    value = '{:,.2f}'.format(value)
                elif field.type in ('date', 'datetime'):
                    value = fields.Date.to_string(value if field.type == 'date' else value.date())
                parts.append('%s: %s' % (label, value))
            except Exception:  # noqa: BLE001
                continue
        return {
            'id': rec.id,
            'name': rec.display_name,
            'details': ' · '.join(parts[:6]),
            'created': '%s, %s' % (rec.create_uid.name or '', fields.Date.to_string(rec.create_date.date()) if rec.create_date else ''),
        }

    # ── SQL helpers ──────────────────────────────────────────────────────────
    def _ids_where_norm(self, table, column, value, extra_sql='', extra_params=()):
        if not value:
            return []
        self.env.cr.execute(
            f"SELECT id FROM {table} WHERE regexp_replace(lower({column}::text), '[^a-z0-9]', '', 'g') = %s"
            f" {extra_sql} LIMIT 20", (value, *extra_params))
        return [r[0] for r in self.env.cr.fetchall()]

    def _ids_where_digits(self, table, columns, digits):
        if not digits or len(digits) < 7:
            return []
        conds = ' OR '.join(f"right(regexp_replace(coalesce({c}, ''), '[^0-9]', '', 'g'), {len(digits)}) = %s"
                            for c in columns)
        self.env.cr.execute(f"SELECT id FROM {table} WHERE {conds} LIMIT 20", tuple([digits] * len(columns)))
        return [r[0] for r in self.env.cr.fetchall()]

    # ── Contacts ─────────────────────────────────────────────────────────────
    def _rule_partner_vat(self, Model, v, ctx):
        return self._ids_where_norm('res_partner', 'vat', _norm(v.get('vat')), 'AND active')

    def _rule_partner_cnic(self, Model, v, ctx):
        if 'x_cnic' not in Model._fields:
            return []
        return self._ids_where_norm('res_partner', 'x_cnic', _norm(v.get('x_cnic')), 'AND active')

    def _rule_partner_email(self, Model, v, ctx):
        email = (v.get('email') or '').strip().lower()
        return Model.search([('email', '=ilike', email)], limit=20).ids if email else []

    def _rule_partner_phone(self, Model, v, ctx):
        cols = [c for c in ('phone', 'mobile') if c in Model._fields]
        ids = []
        for c in cols:
            ids += self._ids_where_digits('res_partner', cols, _digits(v.get(c), 10))
        return ids

    def _rule_partner_name(self, Model, v, ctx):
        return self._ids_where_norm('res_partner', 'name', _norm(v.get('name')), 'AND active')

    # ── Employees ────────────────────────────────────────────────────────────
    def _rule_employee_cnic(self, Model, v, ctx):
        if 'x_cnic' not in Model._fields:
            return []
        return self._ids_where_norm('hr_employee', 'x_cnic', _norm(v.get('x_cnic')), 'AND active')

    def _rule_employee_name_father(self, Model, v, ctx):
        if 'x_father_name' not in Model._fields or not v.get('x_father_name'):
            return []
        name_ids = self._ids_where_norm('hr_employee', 'name', _norm(v.get('name')), 'AND active')
        father = _norm(v.get('x_father_name'))
        return [e.id for e in Model.browse(name_ids) if _norm(e.x_father_name) == father]

    # ── Products (template and variant forms) ────────────────────────────────
    def _rule_product_code(self, Model, v, ctx):
        code = (v.get('default_code') or '').strip()
        return Model.search([('default_code', '=ilike', code)], limit=20).ids if code else []

    def _rule_product_barcode(self, Model, v, ctx):
        code = (v.get('barcode') or '').strip()
        return Model.search([('barcode', '=', code)], limit=20).ids if code else []

    def _rule_product_name_uom(self, Model, v, ctx):
        name = _norm(v.get('name'))
        if not name:
            return []
        table = 'product_template'
        tmpl_ids = self._ids_where_norm(table, "name->>'en_US'", name) or self._ids_where_norm(table, 'name', name)
        uom = _m2o(v.get('uom_id'))
        tmpls = self.env['product.template'].browse(tmpl_ids)
        if uom:
            tmpls = tmpls.filtered(lambda t: t.uom_id.id == uom)
        if Model._name == 'product.product':
            return tmpls.product_variant_ids.ids
        return tmpls.ids

    _rule_template_code = _rule_product_code
    _rule_template_barcode = _rule_product_barcode
    _rule_template_name_uom = _rule_product_name_uom

    # ── Purchase ─────────────────────────────────────────────────────────────
    def _rule_po_same_lines(self, Model, v, ctx):
        lines = {(_m2o(l.get('product_id')), round(float(l.get('product_qty') or l.get('x_requested_qty') or 0), 3))
                 for l in _x2m_lines(v.get('order_line')) if _m2o(l.get('product_id'))}
        if not lines:
            return []
        domain = [('state', '!=', 'cancel'), ('create_date', '>=', fields.Datetime.now() - timedelta(days=7))]
        if _m2o(v.get('partner_id')):
            domain.append(('partner_id', '=', _m2o(v.get('partner_id'))))
        if 'x_project_analytic_account_id' in Model._fields and _m2o(v.get('x_project_analytic_account_id')):
            domain.append(('x_project_analytic_account_id', '=', _m2o(v.get('x_project_analytic_account_id'))))
        out = []
        for po in Model.search(domain, limit=200):
            existing = {(l.product_id.id, round(l.product_qty or l.x_requested_qty or 0, 3))
                        for l in po.order_line if l.product_id}
            if existing == lines:
                out.append(po.id)
        return out

    # ── Accounting ───────────────────────────────────────────────────────────
    def _move_type(self, v, ctx):
        return v.get('move_type') or ctx.get('default_move_type') or 'entry'

    def _lines_untaxed(self, v):
        total = 0.0
        for l in _x2m_lines(v.get('invoice_line_ids')):
            qty = float(l.get('quantity') or 1.0)
            price = float(l.get('price_unit') or 0.0)
            disc = float(l.get('discount') or 0.0)
            total += qty * price * (1 - disc / 100.0)
        return round(total, 2)

    def _rule_bill_ref(self, Model, v, ctx):
        mt = self._move_type(v, ctx)
        partner, ref = _m2o(v.get('partner_id')), _norm(v.get('ref'))
        if mt not in ('in_invoice', 'in_refund') or not partner or not ref:
            return []
        cands = Model.search([('move_type', '=', mt), ('partner_id', '=', partner), ('state', '!=', 'cancel'),
                              ('ref', '!=', False)], limit=500)
        return [m.id for m in cands if _norm(m.ref) == ref]

    def _rule_invoice_ref(self, Model, v, ctx):
        mt = self._move_type(v, ctx)
        partner, ref = _m2o(v.get('partner_id')), _norm(v.get('ref'))
        if mt not in ('out_invoice', 'out_refund') or not partner or not ref:
            return []
        cands = Model.search([('move_type', '=', mt), ('partner_id', '=', partner), ('state', '!=', 'cancel'),
                              ('ref', '!=', False)], limit=500)
        return [m.id for m in cands if _norm(m.ref) == ref]

    def _rule_move_partner_amount_date(self, Model, v, ctx):
        mt = self._move_type(v, ctx)
        partner, date = _m2o(v.get('partner_id')), v.get('invoice_date')
        amount = self._lines_untaxed(v)
        if mt not in ('in_invoice', 'in_refund', 'out_invoice', 'out_refund') or not partner or not date or not amount:
            return []
        cands = Model.search([('move_type', '=', mt), ('partner_id', '=', partner), ('invoice_date', '=', date),
                              ('state', '!=', 'cancel')], limit=50)
        return [m.id for m in cands if abs(m.amount_untaxed - amount) < 0.01]

    def _rule_entry_ref_amount_date(self, Model, v, ctx):
        if self._move_type(v, ctx) != 'entry':
            return []
        ref, date = _norm(v.get('ref')), v.get('date')
        debit = round(sum(float(l.get('debit') or 0) for l in _x2m_lines(v.get('line_ids'))), 2)
        if not ref or not date or not debit:
            return []
        cands = Model.search([('move_type', '=', 'entry'), ('date', '=', date), ('state', '!=', 'cancel'),
                              ('ref', '!=', False)], limit=200)
        return [m.id for m in cands if _norm(m.ref) == ref
                and abs(sum(m.line_ids.mapped('debit')) - debit) < 0.01]

    def _rule_payment_same(self, Model, v, ctx):
        partner, amount, date = _m2o(v.get('partner_id')), v.get('amount'), v.get('date')
        if not partner or not amount or not date:
            return []
        domain = [('partner_id', '=', partner), ('amount', '=', amount), ('date', '=', date),
                  ('state', 'not in', ('canceled', 'cancel', 'rejected'))]
        if _m2o(v.get('journal_id')):
            domain.append(('journal_id', '=', _m2o(v.get('journal_id'))))
        return Model.search(domain, limit=20).ids

    # ── Petty cash ───────────────────────────────────────────────────────────
    def _rule_pc_expense_same(self, Model, v, ctx):
        date = v.get('expense_date')
        amount = round(sum(float(l.get('amount') or 0) for l in _x2m_lines(v.get('line_ids'))), 2)
        name = _norm(v.get('name'))
        if not date or not amount:
            return []
        domain = [('expense_date', '=', date)]
        if _m2o(v.get('project_analytic_account_id')):
            domain.append(('project_analytic_account_id', '=', _m2o(v.get('project_analytic_account_id'))))
        return [e.id for e in Model.search(domain, limit=200)
                if abs(e.amount - amount) < 0.01 and (not name or _norm(e.name) == name)]

    def _rule_pc_request_same(self, Model, v, ctx):
        amount = v.get('requested_amount')
        project = _m2o(v.get('project_analytic_account_id'))
        if not amount or not project:
            return []
        today = fields.Date.context_today(self)
        return Model.search([('project_analytic_account_id', '=', project), ('requested_amount', '=', amount),
                             ('state', '!=', 'cancelled'),
                             ('create_date', '>=', fields.Datetime.to_datetime(today))], limit=20).ids

    # ── Inventory ────────────────────────────────────────────────────────────
    def _rule_picking_same(self, Model, v, ctx):
        person = _m2o(v.get('x_contact_id')) or _m2o(v.get('partner_id'))
        lines = {(_m2o(l.get('product_id')), round(float(l.get('product_uom_qty') or 0), 3))
                 for l in _x2m_lines(v.get('move_ids') or v.get('move_ids_without_package')) if _m2o(l.get('product_id'))}
        if not person or not lines:
            return []
        day = fields.Datetime.to_datetime(v.get('scheduled_date')) if v.get('scheduled_date') else fields.Datetime.now()
        start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        field = 'x_contact_id' if _m2o(v.get('x_contact_id')) else 'partner_id'
        domain = [(field, '=', person), ('state', '!=', 'cancel'),
                  ('scheduled_date', '>=', start), ('scheduled_date', '<', start + timedelta(days=1))]
        if v.get('x_transfer_purpose'):
            domain.append(('x_transfer_purpose', '=', v['x_transfer_purpose']))
        out = []
        for p in Model.search(domain, limit=100):
            existing = {(m.product_id.id, round(m.product_uom_qty, 3)) for m in p.move_ids if m.product_id}
            if existing == lines:
                out.append(p.id)
        return out

    # ── Projects / analytic / BG / leave ─────────────────────────────────────
    def _rule_project_name(self, Model, v, ctx):
        name = _norm(v.get('name'))
        return [p.id for p in Model.search([], limit=5000) if _norm(p.name) == name] if name else []

    def _rule_analytic_name_code(self, Model, v, ctx):
        name, code = _norm(v.get('name')), _norm(v.get('code'))
        if not name and not code:
            return []
        return [a.id for a in Model.search([], limit=5000)
                if (name and _norm(a.name) == name) or (code and _norm(a.code) == code)]

    def _rule_bg_number(self, Model, v, ctx):
        num = _norm(v.get('reference_no'))
        if not num:
            return []
        return [b.id for b in Model.search([('reference_no', '!=', False)], limit=5000) if _norm(b.reference_no) == num]

    def _rule_leave_overlap(self, Model, v, ctx):
        emp, d1, d2 = _m2o(v.get('employee_id')), v.get('date_from'), v.get('date_to')
        if not emp or not d1 or not d2:
            return []
        return Model.search([('employee_id', '=', emp), ('state', 'in', ('draft', 'approved')),
                             ('date_from', '<=', d2), ('date_to', '>=', d1)], limit=20).ids
