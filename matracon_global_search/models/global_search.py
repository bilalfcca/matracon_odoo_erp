"""Global search across every record type the user can open.

Called from the 🔍 top-bar panel (static/src/global_search.js).

Security: everything runs as the calling user, never sudo. Model access
rights (has_access), record rules (applied by search) and field groups
(_has_field_access) decide what can be searched and shown, so a user only
finds records they could already open in Odoo.

Speed: each query has a statement timeout, the whole search has a time
budget, and only the first few records per type are fetched (with a capped
count). When the budget runs out the result is returned as "partial".
"""

import logging
import re
import time
from datetime import datetime, time as dtime, timedelta

from psycopg2 import errors as pg_errors

from odoo import _, api, fields, models, tools
from odoo.exceptions import AccessError, UserError
from odoo.tools import html2plaintext

_logger = logging.getLogger(__name__)

MIN_TERM = 3
RECORDS_PER_TYPE = 5
COUNT_CAP = 1000
STATEMENT_TIMEOUT_MS = 6000
TIME_BUDGET_S = 25.0

MODES = [
    ('all', 'Everything', 'fa-globe'),
    ('partner', 'Partner', 'fa-user'),
    ('description', 'Description / Memo', 'fa-align-left'),
    ('reference', 'Reference / Number', 'fa-hashtag'),
    ('amount', 'Amount', 'fa-money'),
    ('date', 'Date', 'fa-calendar'),
    ('project', 'Project', 'fa-building'),
]

# Technical / configuration models that are not worth searching.
EXCLUDED_PREFIXES = (
    'ir.', 'base.', 'base_', 'bus.', 'web.', 'web_', 'auth_', 'iap.', 'digest.',
    'report.', 'onboarding.', 'res.config', 'res.users', 'res.groups', 'res.lang',
    'res.country', 'res.currency', 'mail.', 'discuss.', 'spreadsheet', 'uom.',
    'decimal.precision', 'html_editor', 'privacy.', 'sms.', 'snailmail.',
    'fetchmail.', 'account.report', 'change.password', 'resource.',
)
EXCLUDED_MODELS = {
    'product.product',        # same records as product.template
    'hr.employee.public',     # same records as hr.employee
    'res.company',
    'res.partner.title',
    'res.partner.industry',
}
# Models searched first, so they are always in the result even if the time
# budget runs out on a very large database.
PRIORITY_MODELS = [
    'res.partner', 'hr.employee', 'project.project', 'purchase.order',
    'stock.picking', 'account.move', 'account.payment', 'sale.order',
]

SKIP_FIELD_RE = re.compile(
    r'(password|token|secret|api_key|signature|^arch|_icon$|^color$|website_url|'
    r'^access_url$|^activity_|^message_|avatar|image|^display_name$|^__)')
REFERENCE_RE = re.compile(
    r'(ref|number|(^|_)no($|_)|code|origin|sequence|serial|cheque|check|invoice|'
    r'voucher|bill|receipt|dmr|gate|ntn|cnic|vat|registration|plate|licen|iban|'
    r'acc_number|barcode|name$)', re.I)
DESCRIPTION_RE = re.compile(
    r'(desc|memo|note|narration|remark|comment|label|detail|purpose|reason|'
    r'subject|summary|particular|instruction|body|message|explanation)', re.I)
TECHNICAL_DATES = {'create_date', 'write_date', 'date_deadline_activity'}
DATE_FIELD_PRIORITY = [
    'date', 'invoice_date', 'date_order', 'date_approve', 'date_done', 'scheduled_date',
    'payment_date', 'expense_date', 'ipc_date', 'date_from', 'date_start',
]
PARTNER_FIELD_PRIORITY = ['partner_id', 'x_contact_id', 'x_site_vendor_id', 'employee_id']
AMOUNT_FIELD_PRIORITY = ['amount_total', 'amount', 'amount_total_signed', 'price_total',
                         'balance', 'net_payable']


class MatraconGlobalSearch(models.AbstractModel):
    _name = 'matracon.global.search'
    _description = 'Global Search'

    # ─────────────────────────────────────────────────────────────────────
    # Public API (called from the web client)
    # ─────────────────────────────────────────────────────────────────────

    @api.model
    def get_search_options(self):
        """Search modes and the record types this user can search."""
        types = [
            {'model': name, 'label': self.env[name]._description or name}
            for name in self._gs_user_models()
        ]
        types.sort(key=lambda t: t['label'].lower())
        return {
            'modes': [{'key': k, 'label': _(label), 'icon': icon} for k, label, icon in MODES],
            'types': types,
            'min_term': MIN_TERM,
        }

    @api.model
    def global_search(self, term, mode='all', model_names=None, date_from=None,
                      date_to=None, states=None):
        term = (term or '').strip()
        if len(term) < MIN_TERM and mode not in ('amount', 'date'):
            raise UserError(_('Type at least %s characters to search.', MIN_TERM))
        if mode not in dict((m[0], m) for m in MODES):
            mode = 'all'

        started = time.monotonic()
        self.env.cr.execute('SET LOCAL statement_timeout = %s', [STATEMENT_TIMEOUT_MS])
        ctx = self._gs_prepare(term, mode)
        filters = {
            'date_from': fields.Date.to_date(date_from) if date_from else None,
            'date_to': fields.Date.to_date(date_to) if date_to else None,
            'states': states or [],
        }

        names = self._gs_user_models()
        if model_names:
            wanted = set(model_names)
            names = [n for n in names if n in wanted]

        groups, partial, skipped = [], False, 0
        for name in names:
            if time.monotonic() - started > TIME_BUDGET_S:
                partial, skipped = True, skipped + 1
                continue
            try:
                with self.env.cr.savepoint():
                    group = self._gs_search_model(name, mode, ctx, filters)
            except pg_errors.QueryCanceled:
                partial, skipped = True, skipped + 1
                continue
            except Exception as exc:  # noqa: BLE001 - one odd model must not break the search
                _logger.info('Global search skipped %s: %s', name, exc)
                continue
            if group:
                groups.append(group)

        if mode in ('all', 'description') and (not model_names or 'mail.message' in model_names) \
                and not filters['states']:
            try:
                with self.env.cr.savepoint():
                    msg_group = self._gs_search_messages(term, filters)
                if msg_group:
                    groups.append(msg_group)
            except pg_errors.QueryCanceled:
                partial = True
            except (AccessError, UserError, ValueError) as exc:
                _logger.debug('Global search skipped messages: %s', exc)

        groups.sort(key=lambda g: (-g['priority'], -min(g['count'], COUNT_CAP), g['label'].lower()))
        return {
            'groups': groups,
            'partial': partial,
            'skipped': skipped,
            'seconds': round(time.monotonic() - started, 1),
            'note': ctx.get('note'),
        }

    # ─────────────────────────────────────────────────────────────────────
    # Model discovery
    # ─────────────────────────────────────────────────────────────────────

    @api.model
    @tools.ormcache()
    def _gs_candidate_models(self):
        """User-facing models (they have a window action), minus technical ones."""
        self.env.cr.execute("""
            SELECT DISTINCT res_model FROM ir_act_window WHERE res_model IS NOT NULL
        """)
        action_models = {row[0] for row in self.env.cr.fetchall()}
        result = []
        for name in action_models:
            if name not in self.env or name in EXCLUDED_MODELS:
                continue
            if name.startswith(EXCLUDED_PREFIXES):
                continue
            model = self.env[name]
            if model._abstract or model._transient or not model._auto or not model._table:
                continue
            result.append(name)
        priority = {n: i for i, n in enumerate(PRIORITY_MODELS)}
        result.sort(key=lambda n: (priority.get(n, len(priority)), n))
        return tuple(result)

    def _gs_user_models(self):
        return [n for n in self._gs_candidate_models() if self.env[n].has_access('read')]

    # ─────────────────────────────────────────────────────────────────────
    # Per-mode preparation (things looked up once per search)
    # ─────────────────────────────────────────────────────────────────────

    def _gs_prepare(self, term, mode):
        ctx = {'term': term}
        if mode == 'partner':
            ctx['m2o'] = {}
            if 'res.partner' in self.env and self.env['res.partner'].has_access('read'):
                ctx['m2o']['res.partner'] = set(self.env['res.partner'].with_context(active_test=False)._search(
                    [('complete_name', 'ilike', term)], limit=500))
            if 'hr.employee' in self.env and self.env['hr.employee'].has_access('read'):
                ctx['m2o']['hr.employee'] = set(self.env['hr.employee'].with_context(active_test=False)._search(
                    [('name', 'ilike', term)], limit=500))
            ctx['self_match'] = {'res.partner', 'hr.employee'}
        elif mode == 'project':
            ctx['m2o'] = {}
            for comodel in ('project.project', 'account.analytic.account'):
                if comodel in self.env and self.env[comodel].has_access('read'):
                    ctx['m2o'][comodel] = set(self.env[comodel].with_context(active_test=False)._search(
                        [('name', 'ilike', term)], limit=500))
            ctx['self_match'] = {'project.project', 'account.analytic.account'}
        elif mode == 'amount':
            ctx['amount'] = self._gs_parse_amount(term)
            if ctx['amount'] is None:
                raise UserError(_('"%s" is not an amount. Type a number, e.g. 25000 or 25,000.50', term))
        elif mode == 'date':
            ctx['date'] = self._gs_parse_date(term)
            if not ctx['date']:
                raise UserError(_('"%s" is not a date. Use e.g. 15/10/2026 or 2026-10-15.', term))
        return ctx

    @staticmethod
    def _gs_parse_amount(term):
        cleaned = re.sub(r'[,\s]|rs\.?|pkr', '', term, flags=re.I)
        try:
            return float(cleaned)
        except ValueError:
            return None

    @staticmethod
    def _gs_parse_date(term):
        for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%d.%m.%Y', '%d/%m/%y', '%d %b %Y', '%d %B %Y'):
            try:
                return datetime.strptime(term.strip(), fmt).date()
            except ValueError:
                continue
        return None

    # ─────────────────────────────────────────────────────────────────────
    # Field classification
    # ─────────────────────────────────────────────────────────────────────

    def _gs_fields(self, model):
        """Stored fields of the model the user may read, split by kind."""
        out = {'text': [], 'reference': [], 'description': [], 'amount': [], 'date': [], 'm2o': []}
        for fname, field in model._fields.items():
            if not field.store or not field.column_type or SKIP_FIELD_RE.search(fname):
                continue
            if fname in ('create_uid', 'write_uid', 'company_id', 'currency_id'):
                continue
            if not model._has_field_access(field, 'read'):
                continue
            label = field.string or ''
            if field.type in ('char', 'text', 'html'):
                out['text'].append(fname)
                if field.type in ('text', 'html') or DESCRIPTION_RE.search(fname) or DESCRIPTION_RE.search(label):
                    out['description'].append(fname)
                if field.type == 'char' and (REFERENCE_RE.search(fname) or REFERENCE_RE.search(label)
                                             or fname == model._rec_name):
                    out['reference'].append(fname)
            elif field.type == 'monetary' or (field.type == 'float' and 'amount' in fname):
                out['amount'].append(fname)
            elif field.type in ('date', 'datetime') and fname not in TECHNICAL_DATES:
                out['date'].append(fname)
            elif field.type == 'many2one':
                out['m2o'].append((fname, field.comodel_name))
        return out

    @staticmethod
    def _gs_pick(candidates, available):
        for name in candidates:
            if name in available:
                return name
        return available[0] if available else None

    # ─────────────────────────────────────────────────────────────────────
    # Searching one model
    # ─────────────────────────────────────────────────────────────────────

    def _gs_or(self, terms):
        if not terms:
            return None
        return ['|'] * (len(terms) - 1) + [t for t in terms]

    def _gs_match_domain(self, model, flds, mode, ctx):
        term = ctx['term']
        if mode == 'all':
            return self._gs_or([(f, 'ilike', term) for f in flds['text']])
        if mode == 'description':
            return self._gs_or([(f, 'ilike', term) for f in flds['description']])
        if mode == 'reference':
            return self._gs_or([(f, 'ilike', term) for f in flds['reference']])
        if mode == 'amount':
            amt = ctx['amount']
            terms = [['&', (f, '>=', amt - 0.005), (f, '<=', amt + 0.005)] for f in flds['amount']]
            if not terms:
                return None
            dom = ['|'] * (len(terms) - 1)
            for t in terms:
                dom.extend(t)
            return dom
        if mode == 'date':
            day = ctx['date']
            terms = []
            for f in flds['date']:
                if model._fields[f].type == 'date':
                    terms.append([(f, '=', day)])
                else:
                    start = datetime.combine(day, dtime.min)
                    terms.append(['&', (f, '>=', start), (f, '<', start + timedelta(days=1))])
            if not terms:
                return None
            dom = ['|'] * (len(terms) - 1)
            for t in terms:
                dom.extend(t)
            return dom
        if mode in ('partner', 'project'):
            terms = []
            if model._name in ctx['self_match'] and ctx['m2o'].get(model._name):
                terms.append(('id', 'in', list(ctx['m2o'][model._name])))
            for fname, comodel in flds['m2o']:
                ids = ctx['m2o'].get(comodel)
                if ids:
                    terms.append((fname, 'in', list(ids)))
            return self._gs_or(terms)
        return None

    def _gs_filter_domain(self, model, flds, filters):
        dom = []
        date_field = self._gs_pick(DATE_FIELD_PRIORITY, flds['date']) or 'create_date'
        if filters['date_from']:
            dom.append((date_field, '>=', filters['date_from']))
        if filters['date_to']:
            if model._fields[date_field].type == 'datetime':
                dom.append((date_field, '<', filters['date_to'] + timedelta(days=1)))
            else:
                dom.append((date_field, '<=', filters['date_to']))
        if filters['states']:
            if 'state' not in model._fields or not model._fields['state'].store:
                return None   # this type has no status: leave it out
            dom.append(('state', 'in', filters['states']))
        return dom

    def _gs_search_model(self, name, mode, ctx, filters):
        model = self.env[name].with_context(active_test=False)
        flds = self._gs_fields(model)
        match = self._gs_match_domain(model, flds, mode, ctx)
        if not match:
            return None
        extra = self._gs_filter_domain(model, flds, filters)
        if extra is None:
            return None
        domain = (['&'] * len(extra)) + extra + match if extra else match

        date_f = self._gs_pick(DATE_FIELD_PRIORITY, flds['date'])
        partner_f = next((f for f in PARTNER_FIELD_PRIORITY if f in dict(flds['m2o'])), None)
        amount_f = self._gs_pick(AMOUNT_FIELD_PRIORITY, flds['amount'])
        state_f = 'state' if ('state' in model._fields and model._fields['state'].type == 'selection'
                              and model._has_field_access(model._fields['state'], 'read')) else None
        show = [f for f in (date_f, partner_f, amount_f, state_f) if f]
        if mode in ('all', 'description', 'reference'):
            show += flds[{'all': 'text'}.get(mode, mode)]
        elif mode in ('partner', 'project'):
            show += [f for f, c in flds['m2o'] if c in ctx['m2o']]
        elif mode == 'amount':
            show += flds['amount']
        elif mode == 'date':
            show += flds['date']
        show = list(dict.fromkeys(show))

        records = model.search_fetch(domain, show, limit=RECORDS_PER_TYPE)
        if not records:
            return None
        count = len(records)
        if count == RECORDS_PER_TYPE:
            count = model.search_count(domain, limit=COUNT_CAP + 1)

        rows = []
        for rec in records:
            rows.append({
                'id': rec.id,
                'model': name,
                'name': rec.display_name or _('(no name)'),
                'subtitle': self._gs_subtitle(rec, date_f, partner_f, amount_f, state_f),
                'snippet': self._gs_snippet(rec, mode, ctx, flds),
            })
        states = []
        if state_f:
            states = [[k, str(v)] for k, v in model._fields['state']._description_selection(self.env)]
        return {
            'model': name,
            'label': model._description or name,
            'count': count,
            'count_capped': count > COUNT_CAP,
            'records': rows,
            'domain': domain,
            'states': states,
            'priority': 1 if name in ctx.get('self_match', ()) else 0,
        }

    # ─────────────────────────────────────────────────────────────────────
    # Messages & emails (chatter)
    # ─────────────────────────────────────────────────────────────────────

    def _gs_search_messages(self, term, filters):
        Message = self.env['mail.message']
        domain = [
            ('model', '!=', False), ('res_id', '!=', False),
            ('message_type', 'in', ('comment', 'email', 'email_outgoing')),
            '|', ('body', 'ilike', term), ('subject', 'ilike', term),
        ]
        if filters['date_from']:
            domain.append(('date', '>=', filters['date_from']))
        if filters['date_to']:
            domain.append(('date', '<', filters['date_to'] + timedelta(days=1)))
        messages = Message.search(domain, limit=10)
        if not messages:
            return None
        rows = []
        for msg in messages:
            text = html2plaintext(msg.body or '') or msg.subject or ''
            rows.append({
                'id': msg.res_id,
                'model': msg.model,
                'name': msg.record_name or msg.subject or msg.model,
                'subtitle': ' · '.join(filter(None, [
                    self.env[msg.model]._description if msg.model in self.env else msg.model,
                    msg.author_id.name or msg.email_from,
                    fields.Datetime.to_string(msg.date)[:16] if msg.date else '',
                ])),
                'snippet': self._gs_cut(text, term),
            })
        return {
            'model': 'mail.message',
            'label': _('Messages & Emails'),
            'count': len(rows),
            'count_capped': len(rows) == 10,
            'records': rows,
            'domain': False,
            'states': [],
            'priority': -1,
        }

    # ─────────────────────────────────────────────────────────────────────
    # Display helpers
    # ─────────────────────────────────────────────────────────────────────

    def _gs_value(self, rec, fname):
        field = rec._fields[fname]
        value = rec[fname]
        if field.type not in ('monetary', 'float', 'integer') and not value:
            return ''
        if field.type == 'many2one':
            return value.display_name or ''
        if field.type == 'selection':
            return str(dict(field._description_selection(self.env)).get(value, value))
        if field.type == 'date':
            return value.strftime('%d/%m/%Y')
        if field.type == 'datetime':
            return fields.Datetime.context_timestamp(rec, value).strftime('%d/%m/%Y %H:%M')
        if field.type in ('monetary', 'float'):
            return f'{value:,.2f}'
        if field.type == 'html':
            return html2plaintext(value)
        return str(value)

    def _gs_subtitle(self, rec, *fnames):
        parts = []
        for fname in fnames:
            if not fname or (rec._fields[fname].type in ('monetary', 'float', 'integer') and not rec[fname]):
                continue
            value = self._gs_value(rec, fname)
            if value:
                parts.append(value)
        return ' · '.join(parts)

    @staticmethod
    def _gs_cut(text, term, width=70):
        text = ' '.join((text or '').split())
        pos = text.lower().find(term.lower()) if term else -1
        if pos < 0:
            return text[:width * 2] + ('…' if len(text) > width * 2 else '')
        start = max(pos - width, 0)
        end = min(pos + len(term) + width, len(text))
        return ('…' if start else '') + text[start:end] + ('…' if end < len(text) else '')

    def _gs_snippet(self, rec, mode, ctx, flds):
        """Show which field matched: 'Memo: … awais …', 'Vendor: Awais Traders'."""
        term = ctx['term']
        if mode in ('all', 'description', 'reference'):
            names = flds[{'all': 'text'}.get(mode, mode)]
            for fname in names:
                text = self._gs_value(rec, fname)
                if term.lower() in text.lower():
                    return f'{rec._fields[fname].string}: {self._gs_cut(text, term)}'
        elif mode in ('partner', 'project'):
            if rec.id in ctx['m2o'].get(rec._name, ()):
                return ''   # the contact / employee / project itself
            for fname, comodel in flds['m2o']:
                if rec[fname].id in ctx['m2o'].get(comodel, ()):
                    return f'{rec._fields[fname].string}: {rec[fname].display_name}'
        elif mode == 'amount':
            for fname in flds['amount']:
                if abs((rec[fname] or 0.0) - ctx['amount']) < 0.006:
                    return f'{rec._fields[fname].string}: {rec[fname]:,.2f}'
        elif mode == 'date':
            for fname in flds['date']:
                value = rec[fname]
                if isinstance(value, datetime):
                    value = fields.Datetime.context_timestamp(rec, value).date()
                if value == ctx['date']:
                    return f'{rec._fields[fname].string}: {self._gs_value(rec, fname)}'
        return ''
