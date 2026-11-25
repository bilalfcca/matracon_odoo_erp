"""Recycle Bin: a full, read-only copy of every deleted business record."""
import json
import logging

from markupsafe import Markup, escape

from odoo import SUPERUSER_ID, api, fields, models, tools, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Odoo technical / internal models: deleted constantly by the framework itself.
SKIP_PREFIXES = (
    'ir.', 'mail.', 'bus.', 'base.', 'base_', 'web_', 'web.', 'auth_', 'res.users',
    'res.device', 'res.config', 'digest.', 'iap.', 'sms.', 'snailmail.', 'spreadsheet',
    'onboarding.', 'resource.', 'report.', 'x.recycle.bin', 'discuss.', 'privacy.',
    'portal.', 'html_editor', 'google.', 'microsoft.', 'fetchmail.',
)
# Lines Odoo rebuilds automatically (invoice re-sync, stock moves, reconciliation…).
# They are captured inside their parent document's copy instead.
SKIP_MODELS = {
    'account.move.line', 'account.analytic.line', 'account.partial.reconcile',
    'account.full.reconcile', 'account.payment.method.line', 'stock.move',
    'stock.move.line', 'stock.quant', 'stock.valuation.layer', 'product.value',
    'stock.route.rule', 'uom.uom', 'account.tax.repartition.line',
}
# One2many targets that are never worth copying into the snapshot.
SKIP_CHILD_MODELS = {
    'mail.message', 'mail.followers', 'mail.activity', 'ir.attachment',
    'mail.tracking.value', 'bus.presence', 'rating.rating', 'website.track',
}
MAX_CHILD_ROWS = 500
MAX_TEXT = 5000


class RecycleBin(models.Model):
    _name = 'x.recycle.bin'
    _description = 'Deleted Record'
    _order = 'deleted_on desc, id desc'
    _rec_name = 'name'

    name = fields.Char(string='Record', readonly=True, index=True)
    res_model = fields.Char(string='Model', readonly=True, index=True)
    model_label = fields.Char(string='Record Type', readonly=True, index=True)
    app_name = fields.Char(string='App / Module', readonly=True, index=True)
    res_id = fields.Integer(string='Original ID', readonly=True)
    deleted_by_id = fields.Many2one('res.users', string='Deleted By', readonly=True, index=True)
    deleted_on = fields.Datetime(string='Deleted On', readonly=True, index=True,
                                 default=fields.Datetime.now)
    source = fields.Selection([
        ('user', 'By a user'),
        ('system', 'Automatic (system / scheduled job)'),
    ], string='How Deleted', readonly=True, index=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    original_create_uid = fields.Many2one('res.users', string='Originally Created By', readonly=True)
    original_create_date = fields.Datetime(string='Originally Created On', readonly=True)
    line_count = fields.Integer(string='Lines Kept', readonly=True)
    attachment_names = fields.Text(string='Attachments (names)', readonly=True)
    data_json = fields.Text(string='Saved Data (JSON)', readonly=True)
    # Deletion history (approval flow)
    request_id = fields.Many2one('x.deletion.request', string='Deletion Request', readonly=True, index=True)
    requested_by_id = fields.Many2one('res.users', string='Requested By', readonly=True)
    requested_on = fields.Datetime(string='Requested On', readonly=True)
    approved_by_id = fields.Many2one('res.users', string='Approved By', readonly=True, index=True)
    approved_on = fields.Datetime(string='Approved On', readonly=True)
    deletion_reason = fields.Text(string='Reason', readonly=True)
    details_html = fields.Html(string='Saved Details', compute='_compute_details_html',
                               sanitize=False)

    # ── Never really deleted / changed ───────────────────────────────────────
    def write(self, vals):
        if not self.env.context.get('_recycle_bin_internal'):
            raise UserError(_('Deleted records in the Recycle Bin cannot be changed.'))
        return super().write(vals)

    def unlink(self):
        raise UserError(_('Records in the Recycle Bin are kept permanently and cannot be deleted.'))

    # ── Which models are tracked ─────────────────────────────────────────────
    @api.model
    @tools.ormcache('model_name')
    def _is_tracked_model(self, model_name):
        """Business records users open from a menu (have a window action),
        excluding Odoo's technical models and auto-rebuilt lines."""
        if model_name in SKIP_MODELS or model_name.startswith(SKIP_PREFIXES):
            return False
        Model = self.env.get(model_name)
        if Model is None or Model._transient or Model._abstract or not Model._auto:
            return False
        self.env.cr.execute(
            "SELECT 1 FROM ir_act_window WHERE res_model = %s LIMIT 1", (model_name,))
        return bool(self.env.cr.fetchone())

    @api.model
    @tools.ormcache('model_name')
    def _app_name_for_model(self, model_name):
        """The app (top-level menu) where users find this record type."""
        cr = self.env.cr
        cr.execute("""
            SELECT root.name
              FROM ir_ui_menu m
              JOIN ir_act_window a ON m.action = 'ir.actions.act_window,' || a.id
              JOIN ir_ui_menu root
                ON root.id = split_part(m.parent_path, '/', 1)::int
             WHERE a.res_model = %s AND m.active
          ORDER BY m.id
             LIMIT 1
        """, (model_name,))
        row = cr.fetchone()
        if row and row[0]:
            name = row[0]
            if isinstance(name, dict):
                name = name.get(self.env.lang) or name.get('en_US') or next(iter(name.values()), '')
            return name
        model = self.env['ir.model']._get(model_name)
        modules = (model.modules or '').split(',')
        mod = self.env['ir.module.module'].sudo().search(
            [('name', '=', modules[0].strip())], limit=1) if modules and modules[0] else False
        return (mod and mod.shortdesc) or model.name or model_name

    # ── Snapshot ─────────────────────────────────────────────────────────────
    @api.model
    def _value(self, record, field):
        value = record[field.name]
        ftype = field.type
        if ftype == 'many2one':
            return value.display_name if value else False
        if ftype in ('many2many',):
            names = value[:50].mapped('display_name')
            return names + (['… +%s more' % (len(value) - 50)] if len(value) > 50 else [])
        if ftype == 'selection':
            if value is False:
                return False
            return dict(field._description_selection(self.env)).get(value, value)
        if ftype in ('binary', 'image'):
            return '[file]' if value else False
        if ftype in ('date', 'datetime'):
            return fields.Datetime.to_string(value) if ftype == 'datetime' and value else (
                fields.Date.to_string(value) if value else False)
        if ftype in ('html', 'text', 'char'):
            if not value:
                return value
            text = tools.html2plaintext(value) if ftype == 'html' else str(value)
            return text[:MAX_TEXT]
        if ftype in ('json', 'properties'):
            return value and json.loads(json.dumps(value, default=str))
        return value

    @api.model
    def _record_values(self, record, with_children=True):
        data = {}
        for fname, field in record._fields.items():
            if not field.store or fname in ('id', '__last_update', 'message_main_attachment_id'):
                continue
            if field.type == 'one2many':
                if not with_children or field.comodel_name in SKIP_CHILD_MODELS:
                    continue
                children = record[fname]
                if not children:
                    continue
                data[fname] = [self._record_values(c, with_children=False)
                               for c in children[:MAX_CHILD_ROWS]]
                continue
            try:
                data[fname] = self._value(record, field)
            except Exception:  # noqa: BLE001 - a bad field must not block the copy
                data[fname] = '[unreadable]'
        return data

    @api.model
    def _snapshot(self, records):
        """Save a copy of ``records`` (all belong to one model) before deletion."""
        if not records or not self._is_tracked_model(records._name):
            return
        env = self.env
        user = env.user
        is_system = env.uid == SUPERUSER_ID or user == env.ref('base.user_root', raise_if_not_found=False)
        recs = records.sudo().with_context(active_test=False).exists()
        if not recs:
            return
        model_label = env['ir.model']._get(records._name).name or records._name
        app = self._app_name_for_model(records._name)
        Attachment = env['ir.attachment'].sudo()
        history = self._history_vals(records)
        vals_list = []
        for rec in recs:
            data = self._record_values(rec)
            lines = sum(len(v) for v in data.values() if isinstance(v, list) and v and isinstance(v[0], dict))
            att = Attachment.search([('res_model', '=', rec._name), ('res_id', '=', rec.id)])
            name = self._display_name_for(rec, model_label)
            vals_list.append({
                'name': name or '%s #%s' % (model_label, rec.id),
                'res_model': rec._name,
                'model_label': model_label,
                'app_name': app,
                'res_id': rec.id,
                'deleted_by_id': user.id,
                'source': 'system' if is_system else 'user',
                'company_id': rec['company_id'].id if 'company_id' in rec._fields
                and rec._fields['company_id'].type == 'many2one' and rec['company_id'] else env.company.id,
                'original_create_uid': rec.create_uid.id if 'create_uid' in rec._fields else False,
                'original_create_date': rec.create_date if 'create_date' in rec._fields else False,
                'line_count': lines,
                'attachment_names': '\n'.join(att.mapped('name')) or False,
                'data_json': json.dumps(data, default=str, ensure_ascii=False),
                **history,
            })
        self.sudo().with_context(_recycle_bin_internal=True).create(vals_list)

    @api.model
    def _history_vals(self, records):
        """Who requested / approved the deletion, from the approval request or
        the direct action of the person deleting."""
        from .deletion_control import is_deletion_admin, user_started_delete
        env = self.env
        req_id = env.context.get('deletion_request_id')
        if req_id:
            req = env['x.deletion.request'].sudo().browse(req_id)
            return {
                'request_id': req.id, 'requested_by_id': req.requested_by_id.id,
                'requested_on': req.requested_on, 'approved_by_id': env.user.id,
                'approved_on': fields.Datetime.now(), 'deletion_reason': req.reason,
            }
        if env.uid == SUPERUSER_ID or not user_started_delete(records):
            return {}
        now = fields.Datetime.now()
        vals = {'requested_by_id': env.user.id, 'requested_on': now}
        level, controlled = env['x.deletion.policy']._level_for(records)
        if controlled and is_deletion_admin(env.user):
            vals.update({'approved_by_id': env.user.id, 'approved_on': now,
                         'deletion_reason': _('Deleted directly by Administrator.')})
        return vals

    @api.model
    def _display_name_for(self, rec, model_label):
        """Record name plus partner and amount when available, so entries
        like 'Draft Invoice' stay recognisable in the list."""
        try:
            name = rec.display_name or ''
        except Exception:  # noqa: BLE001
            name = ''
        name = name or '%s #%s' % (model_label, rec.id)
        extras = []
        try:
            partner = rec['partner_id'] if 'partner_id' in rec._fields else False
            if partner and getattr(partner, '_name', '') == 'res.partner' \
                    and partner.display_name and partner.display_name not in name:
                extras.append(partner.display_name)
            for fname in ('amount_total', 'amount'):
                field = rec._fields.get(fname)
                if field and field.type in ('monetary', 'float') and rec[fname]:
                    extras.append('{:,.2f}'.format(rec[fname]))
                    break
        except Exception:  # noqa: BLE001
            pass
        return ' · '.join([name] + extras)[:250]

    # ── Display ──────────────────────────────────────────────────────────────
    @api.depends('data_json', 'res_model')
    def _compute_details_html(self):
        for rec in self:
            try:
                data = json.loads(rec.data_json or '{}')
            except ValueError:
                data = {}
            Model = self.env.get(rec.res_model) if rec.res_model else None
            rec.details_html = self._render_table(data, Model)

    @api.model
    def _label(self, Model, fname):
        field = Model._fields.get(fname) if Model is not None else None
        return field.string if field else fname

    @api.model
    def _fmt(self, value):
        if value in (False, None, ''):
            return Markup('<span class="text-muted">—</span>')
        if isinstance(value, list):
            return escape(', '.join(str(v) for v in value))
        if isinstance(value, bool):
            return _('Yes')
        return escape(str(value))

    @api.model
    def _render_table(self, data, Model):
        skip = {'create_uid', 'create_date', 'write_uid', 'write_date'}
        rows, sub_tables = [], []
        for fname, value in data.items():
            if fname in skip:
                continue
            if isinstance(value, list) and value and isinstance(value[0], dict):
                field = Model._fields.get(fname) if Model is not None else None
                Child = self.env.get(field.comodel_name) if field else None
                sub_tables.append((self._label(Model, fname), value, Child))
                continue
            if value in (False, None, '', []):
                continue
            rows.append(Markup('<tr><th class="text-nowrap pe-3" style="width:30%%">%s</th><td>%s</td></tr>')
                        % (self._label(Model, fname), self._fmt(value)))
        html = Markup('<table class="table table-sm table-bordered mb-3"><tbody>%s</tbody></table>') % Markup('').join(rows)
        for label, lines, Child in sub_tables:
            cols = [c for c in lines[0].keys() if c not in skip and any(
                l.get(c) not in (False, None, '', []) for l in lines)][:12]
            head = Markup('').join(Markup('<th>%s</th>') % self._label(Child, c) for c in cols)
            body = Markup('').join(
                Markup('<tr>%s</tr>') % Markup('').join(Markup('<td>%s</td>') % self._fmt(l.get(c)) for c in cols)
                for l in lines)
            html += Markup('<h5 class="mt-3">%s (%s)</h5><div class="table-responsive">'
                           '<table class="table table-sm table-striped"><thead><tr>%s</tr></thead>'
                           '<tbody>%s</tbody></table></div>') % (label, len(lines), head, body)
        return html
