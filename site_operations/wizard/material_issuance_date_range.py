from datetime import datetime, time, timedelta

import pytz

from odoo import models, fields, api, _
from odoo.exceptions import UserError
from odoo.tools.safe_eval import safe_eval


DEFAULT_SOURCE_ACTION = 'site_operations.action_material_issuance_only'


class MaterialIssuanceDateRangeWizard(models.TransientModel):
    """Pick a From / To date and reopen the Material Issuance list
    filtered to that range (keeps the originating menu's base domain)."""
    _name = 'x.material.issuance.date.range.wizard'
    _description = 'Material Issuance Date Range Filter'

    date_from = fields.Date(
        string='From Date', required=True,
        default=lambda self: fields.Date.context_today(self).replace(day=1))
    date_to = fields.Date(
        string='To Date', required=True,
        default=lambda self: fields.Date.context_today(self))
    date_field = fields.Selection([
        ('scheduled_date', 'Issuance Date (Scheduled)'),
        ('date_done', 'Effective Date (Validated)'),
    ], string='Filter On', required=True, default='scheduled_date')

    @api.constrains('date_from', 'date_to')
    def _check_dates(self):
        for wiz in self:
            if wiz.date_from and wiz.date_to and wiz.date_from > wiz.date_to:
                raise UserError(_('"From Date" cannot be after "To Date".'))

    def _local_day_bounds_utc(self):
        """Convert the selected local dates into naive UTC datetimes:
        [date_from 00:00 local, date_to + 1 day 00:00 local)."""
        self.ensure_one()
        tz = pytz.timezone(self.env.user.tz or self.env.context.get('tz') or 'UTC')
        start_local = tz.localize(datetime.combine(self.date_from, time.min))
        end_local = tz.localize(datetime.combine(self.date_to + timedelta(days=1), time.min))
        start_utc = start_local.astimezone(pytz.utc).replace(tzinfo=None)
        end_utc = end_local.astimezone(pytz.utc).replace(tzinfo=None)
        return fields.Datetime.to_string(start_utc), fields.Datetime.to_string(end_utc)

    def action_apply(self):
        self.ensure_one()
        source_xmlid = self.env.context.get('x_mi_source_action') or DEFAULT_SOURCE_ACTION
        try:
            action = self.env['ir.actions.act_window']._for_xml_id(source_xmlid)
        except ValueError:
            action = self.env['ir.actions.act_window']._for_xml_id(DEFAULT_SOURCE_ACTION)

        eval_ctx = {'uid': self.env.uid, 'context': dict(self.env.context)}
        base_domain = action.get('domain') or []
        if isinstance(base_domain, str):
            base_domain = safe_eval(base_domain, eval_ctx)

        start_utc, end_utc = self._local_day_bounds_utc()
        date_domain = [
            (self.date_field, '>=', start_utc),
            (self.date_field, '<', end_utc),
        ]

        action_ctx = action.get('context') or {}
        if isinstance(action_ctx, str):
            action_ctx = safe_eval(action_ctx, eval_ctx)
        action_ctx = dict(action_ctx, x_mi_source_action=source_xmlid)

        lang = self.env['res.lang']._lang_get(self.env.user.lang or 'en_US')
        date_fmt = lang.date_format if lang else '%d/%m/%Y'
        action.update({
            'name': _('%(name)s (%(from)s → %(to)s)',
                      name=action.get('name'),
                      **{'from': self.date_from.strftime(date_fmt),
                         'to': self.date_to.strftime(date_fmt)}),
            'domain': list(base_domain) + date_domain,
            'context': action_ctx,
            'target': 'main',
        })
        return action
