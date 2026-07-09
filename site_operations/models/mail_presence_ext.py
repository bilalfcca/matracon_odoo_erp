from datetime import timedelta

from odoo import models, fields, api

# Must match ONLINE_THRESHOLD_SECONDS in hr_employee_ext.py
ONLINE_THRESHOLD_SECONDS = 65


class MailPresenceExt(models.Model):
    """Hook into mail.presence to capture online/offline transitions.

    Only two states are logged: 'online' and 'offline'.
    Odoo's 'away' status (idle browser tab) is intentionally ignored —
    a user is either online (heartbeat alive) or offline (heartbeat gone).

    Offline events are detected by the cron ``hr.employee._cron_update_presence_log``
    because Odoo never sets status='offline' server-side when a browser tab closes.
    """
    _inherit = 'mail.presence'

    # ── helpers ─────────────────────────────────────────────────────────────

    def _get_employee_for_user(self, user_id):
        return self.env['hr.employee'].sudo().search(
            [('user_id', '=', user_id)], limit=1
        )

    def _log_online(self, user_id):
        """Write an 'online' log entry for the employee linked to user_id."""
        employee = self._get_employee_for_user(user_id)
        if not employee:
            return
        self.env['x.employee.presence.log'].sudo().create({
            'employee_id': employee.id,
            'user_id': user_id,
            'timestamp': fields.Datetime.now(),
            'status': 'online',
        })

    # ── ORM hooks ────────────────────────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for record in records:
            # New presence record = fresh login (after GC deleted old one,
            # or first ever login). Log as 'online' regardless of status value.
            if record.user_id:
                self._log_online(record.user_id.id)
        return records

    def write(self, vals):
        """Log 'online' when a user reconnects after being effectively offline.

        'away' is treated identically to 'online' — both mean the browser
        is open. No separate 'away' entry is written to the log.

        The only transition we log here is: was-offline → now-online.
        The cron handles the online → offline direction.
        """
        new_status = vals.get('status')
        # Only care about heartbeat writes; ignore other field updates
        if new_status not in ('online', 'away'):
            return super().write(vals)

        now = fields.Datetime.now()
        threshold = now - timedelta(seconds=ONLINE_THRESHOLD_SECONDS)

        for presence in self:
            if not presence.user_id:
                continue

            # Was this user effectively offline before this write?
            was_offline = (
                not presence.last_poll or presence.last_poll < threshold
            )

            if was_offline:
                # User is reconnecting. First close the gap with an 'offline'
                # entry (if the last log entry isn't already 'offline').
                employee = self._get_employee_for_user(presence.user_id.id)
                if employee:
                    last_log = self.env['x.employee.presence.log'].sudo().search(
                        [('employee_id', '=', employee.id)],
                        order='timestamp desc', limit=1
                    )
                    if not last_log or last_log.status != 'offline':
                        # Stamp offline at last_poll (closest to actual disconnect)
                        offline_ts = presence.last_poll or now
                        self.env['x.employee.presence.log'].sudo().create({
                            'employee_id': employee.id,
                            'user_id': presence.user_id.id,
                            'timestamp': offline_ts,
                            'status': 'offline',
                        })
                    # Now log the reconnect as 'online'
                    self._log_online(presence.user_id.id)

        return super().write(vals)
