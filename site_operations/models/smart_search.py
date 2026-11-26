"""Advanced search: amount expressions, project-wise partners, smart suggestions.

Amount search
-------------
Search fields named ``x_amt_*`` accept an amount expression typed in the
search bar (the browser only offers them when the text looks like one):

    >50000   >=50000   <1000   <=1000   =25000   25000      (equal)
    10000-50000   10000..50000   10000 to 50000   between 10000 and 50000
    Thousand separators and k / lac / lakh / m / mn / million / cr / crore
    are understood:  >5 lac   1m-2.5m   1,00,000

They are ordinary domain filters, so they combine with every other filter,
group-by and favourite.
"""
import re

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.fields import Domain

_MULT = {'k': 1e3, 'thousand': 1e3, 'lac': 1e5, 'lacs': 1e5, 'lakh': 1e5, 'lakhs': 1e5,
         'm': 1e6, 'mn': 1e6, 'million': 1e6, 'cr': 1e7, 'crore': 1e7, 'crores': 1e7}
_NUM = r'(-?\d[\d,]*(?:\.\d+)?|-?\.\d+)\s*(' + '|'.join(sorted(_MULT, key=len, reverse=True)) + r')?'
_RANGE = re.compile(r'^\s*(?:between\s+)?' + _NUM + r'\s*(?:-|\.\.|to|and)\s*' + _NUM + r'\s*$', re.I)
_SINGLE = re.compile(r'^\s*(>=|<=|=>|=<|>|<|==|=)?\s*' + _NUM + r'\s*$', re.I)
_EPS = 0.005


def _num(digits, unit):
    return float(digits.replace(',', '')) * _MULT.get((unit or '').lower(), 1.0)


def parse_amount(text):
    """'>5000' → [('>', 5000.0)] ; '10-20' → [('>=', 10.0), ('<=', 20.0)]."""
    text = (text or '').strip()
    m = _RANGE.match(text)
    if m:
        lo, hi = sorted((_num(m.group(1), m.group(2)), _num(m.group(3), m.group(4))))
        return [('>=', lo - _EPS), ('<=', hi + _EPS)]
    m = _SINGLE.match(text)
    if m:
        op = {'=>': '>=', '=<': '<=', '==': '=', None: '='}.get(m.group(1), m.group(1))
        val = _num(m.group(2), m.group(3))
        if op == '=':
            return [('>=', val - _EPS), ('<=', val + _EPS)]
        return [(op, val)]
    raise UserError(_(
        '"%s" is not an amount.\n\nExamples:  >50000   <1000   =25000   10000-50000   '
        'between 1 lac and 5 lac', text))


def amount_domain(field, operator, value):
    if operator not in ('ilike', '=', 'like', '=ilike'):
        raise UserError(_('This amount search only supports "contains" style searches, e.g. >5000 or 100-500.'))
    return Domain.AND([Domain(field, op, val) for op, val in parse_amount(value)])


class _AmountSearchMixin(models.AbstractModel):
    _name = 'x.amount.search.mixin'
    _description = 'Amount search fields helper'

    def _compute_x_amt_dummy(self):
        for rec in self:
            for name in self._fields:
                if name.startswith('x_amt_'):
                    rec[name] = False


class AccountMoveLineAmountSearch(models.Model):
    _name = 'account.move.line'
    _inherit = ['account.move.line', 'x.amount.search.mixin']

    x_amt_amount = fields.Char('Amount (Debit or Credit)', compute='_compute_x_amt_dummy',
                               search='_search_x_amt_amount')
    x_amt_debit = fields.Char('Debit Amount', compute='_compute_x_amt_dummy', search='_search_x_amt_debit')
    x_amt_credit = fields.Char('Credit Amount', compute='_compute_x_amt_dummy', search='_search_x_amt_credit')
    x_amt_balance = fields.Char('Balance Amount', compute='_compute_x_amt_dummy', search='_search_x_amt_balance')

    def _search_x_amt_amount(self, operator, value):
        return Domain.OR([amount_domain('debit', operator, value), amount_domain('credit', operator, value)])

    def _search_x_amt_debit(self, operator, value):
        return amount_domain('debit', operator, value)

    def _search_x_amt_credit(self, operator, value):
        return amount_domain('credit', operator, value)

    def _search_x_amt_balance(self, operator, value):
        return amount_domain('balance', operator, value)


class AccountMoveAmountSearch(models.Model):
    _name = 'account.move'
    _inherit = ['account.move', 'x.amount.search.mixin']

    x_amt_total = fields.Char('Total Amount', compute='_compute_x_amt_dummy', search='_search_x_amt_total')
    x_amt_due = fields.Char('Due Amount', compute='_compute_x_amt_dummy', search='_search_x_amt_due')

    def _search_x_amt_total(self, operator, value):
        return amount_domain('amount_total', operator, value)

    def _search_x_amt_due(self, operator, value):
        return amount_domain('amount_residual', operator, value)


class AccountPaymentAmountSearch(models.Model):
    _name = 'account.payment'
    _inherit = ['account.payment', 'x.amount.search.mixin']

    x_amt_amount = fields.Char('Payment Amount', compute='_compute_x_amt_dummy', search='_search_x_amt_amount')

    def _search_x_amt_amount(self, operator, value):
        return amount_domain('amount', operator, value)


class AccountAccountAmountSearch(models.Model):
    _name = 'account.account'
    _inherit = ['account.account', 'x.amount.search.mixin']

    x_amt_balance = fields.Char('Balance Amount', compute='_compute_x_amt_dummy', search='_search_x_amt_balance')

    def _search_x_amt_balance(self, operator, value):
        """Same balance as the account's Balance button (posted, current company)."""
        amount_domain('balance', operator, value)   # validates operator and text
        conds = parse_amount(value)
        balances = dict(self.env['account.move.line']._read_group(
            [('parent_state', '=', 'posted'), ('company_id', 'child_of', self.env.company.id)],
            groupby=['account_id'], aggregates=['balance:sum']))
        ops = {'>': float.__gt__, '>=': float.__ge__, '<': float.__lt__, '<=': float.__le__}
        accounts = self.with_context(active_test=False).search([])
        ids = [a.id for a in accounts
               if all(ops[op](float(balances.get(a, 0.0)), float(val)) for op, val in conds)]
        return [('id', 'in', ids)]


class ResPartnerProjectSearch(models.Model):
    _inherit = 'res.partner'

    x_project_filter_id = fields.Many2one(
        'account.analytic.account', string='Project', compute='_compute_x_project_filter_id',
        search='_search_x_project_filter_id',
        help='Partners with any transaction on the project: bills, invoices, payments, '
             'journal entries or purchase orders.')

    def _compute_x_project_filter_id(self):
        self.x_project_filter_id = False

    def _search_x_project_filter_id(self, operator, value):
        Analytic = self.env['account.analytic.account']
        if operator in ('any', 'any!'):
            return self._search_x_project_filter_id('in', Analytic.sudo(operator == 'any!').search(Domain(value)).ids or [0])
        values = [value] if isinstance(value, (int, str)) else list(value or [])
        if operator in ('in', '=') and values and all(isinstance(v, int) for v in values):
            project_ids = values
        elif operator == 'in' and values and all(isinstance(v, str) for v in values):
            project_ids = Analytic.search([('display_name', 'ilike', values[0])]).ids
        elif operator in ('ilike', '=ilike', 'like', '=') and isinstance(value, str):
            project_ids = Analytic.search([('display_name', operator if operator != '=' else 'ilike', value)]).ids
        else:
            raise UserError(_('Choose a project to filter partners by.'))
        if not project_ids:
            return [('id', 'in', [])]
        self.env.flush_all()
        self.env.cr.execute("""
            WITH p AS (
                SELECT partner_id FROM account_move_line
                 WHERE x_project_id = ANY(%(p)s) AND partner_id IS NOT NULL AND parent_state != 'cancel'
                UNION
                SELECT partner_id FROM account_move
                 WHERE x_project_analytic_account_id = ANY(%(p)s) AND partner_id IS NOT NULL AND state != 'cancel'
                UNION
                SELECT partner_id FROM purchase_order
                 WHERE x_project_analytic_account_id = ANY(%(p)s) AND partner_id IS NOT NULL AND state != 'cancel'
            )
            SELECT rp.id FROM res_partner rp JOIN p ON p.partner_id = rp.id
            UNION
            SELECT rp.commercial_partner_id FROM res_partner rp JOIN p ON p.partner_id = rp.id
             WHERE rp.commercial_partner_id IS NOT NULL
        """, {'p': project_ids})
        return [('id', 'in', [r[0] for r in self.env.cr.fetchall()])]


class SmartSearch(models.AbstractModel):
    """Record suggestions shown in the search bar while typing."""
    _name = 'x.smart.search'
    _description = 'Smart Search Suggestions'

    _CONFIG = {
        'res.partner': {'name': 'complete_name', 'codes': ['ref', 'vat', 'x_cnic'],
                        'others': ['email', 'phone', 'city']},
        'product.template': {'name': 'name', 'codes': ['default_code', 'barcode'], 'others': []},
        'product.product': {'name': 'name', 'codes': ['default_code', 'barcode'], 'others': []},
    }
    _LIMIT = 8

    @api.model
    def get_models(self):
        return list(self._CONFIG)

    @api.model
    def suggest(self, model, term, domain=None):
        cfg = self._CONFIG.get(model)
        term = (term or '').strip()
        if not cfg or len(term) < 2 or not self.env[model].has_access('read'):
            return []
        Model = self.env[model]
        fnames = [f for f in [cfg['name']] + cfg['codes'] + cfg['others'] if f in Model._fields]
        words = [w for w in re.split(r'\s+', term) if w]
        word_domains = [Domain.OR([Domain(f, 'ilike', w) for f in fnames]) for w in words]
        # digits-only phone match (0300-1234567 ≈ +92 300 1234567)
        digits = re.sub(r'\D', '', term)
        dom = Domain.AND(word_domains)
        if 'phone' in fnames and len(digits) >= 6:
            dom = Domain.OR([dom, Domain('phone', 'ilike', digits[-7:])])
        if domain:
            dom = Domain.AND([dom, Domain(domain)])
        recs = Model.search(dom, limit=40)
        low = term.lower()

        def score(r):
            name = (r[cfg['name']] or '').lower()
            codes = [str(r[f] or '').lower() for f in cfg['codes'] if f in Model._fields]
            if low in codes or name == low:
                return 0
            if name.startswith(low) or any(c.startswith(low) for c in codes if c):
                return 1
            if low in name:
                return 2
            if all(w.lower() in name for w in words):
                return 3
            return 4
        recs = sorted(recs, key=lambda r: (score(r), (r.display_name or '').lower()))[:self._LIMIT]
        return [{'id': r.id, 'label': r.display_name, 'details': self._details(model, r)} for r in recs]

    def _details(self, model, r):
        parts = []
        if model == 'res.partner':
            parts = [r.ref and _('Ref %s', r.ref), r.vat and _('NTN %s', r.vat),
                     'x_cnic' in r._fields and r.x_cnic and _('CNIC %s', r.x_cnic),
                     r.phone, r.email, r.city]
        else:
            parts = [r.default_code and '[%s]' % r.default_code, r.categ_id.display_name,
                     r.uom_id.name, r.barcode and _('Barcode %s', r.barcode)]
            if r.is_storable:
                parts.append(_('On hand %s', ('%g' % r.qty_available)))
        return ' · '.join(p for p in parts if p)
