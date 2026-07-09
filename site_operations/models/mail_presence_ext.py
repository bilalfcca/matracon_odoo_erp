from datetime import timedelta

from odoo import models, fields, api

# Must match ONLINE_THRESHOLD_SECONDS in hr_employee_ext.py
ONLINE_THRESHOLD_SECONDS = 65


class MailPresenceExt(models.Model):
    """Hook into mail.presence to capture online/away transitions into the
    employee presence history log.

    Only *transitions* are logged — heartbeat writes that keep the same
    effective status are ignored to prevent log bloat.

    Offline events are NOT captured here (Odoo never writes status='offline'
    server-side). They are detected by the scheduled cron
    ``hr.employee._cron_update_presence_log`` which checks last_poll staleness.
    """
    _inherit = 'mail.presence'

    # ── helpers ─────────────────────────────────────────────────────────────

    def _get_employee_for_user(self, user_id):
        return self.env['hr.employee'].sudo().search(
            [('user_id', '=', user_id)], limit=1
        )

    def _log_presence_change(self, user_id, new_status, timestamp=None):
        """Write a presence log entry for the employee linked to user_id."""
        employee = self._get_employee_for_user(user_id)
        if not employee:
            return
        self.env['x.employee.presence.log'].sudo().create({
            'employee_id': employee.id,
            'user_id': user_id,
            'timestamp': timestamp or fields.Datetime.now(),
            'status': new_status,
        })

    # ── ORM hooks ────────────────────────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for record in records:
            # A new presence record is created when a user logs in fresh (after GC
            # deleted their old one, or their first ever login).
            # Always log as 'online' — the create default is 'online'/'away'.
            if record.user_id and record.status in ('online', 'away'):
                self._log_presence_change(record.user_id.id, record.status)
        return records

    def write(self, vals):
        """Log online/away transitions based on *effective* status.

        Effective status = last_poll-based:
          online/away  → last_poll is recent (within ONLINE_THRESHOLD_SECONDS)
          offline      → last_poll is stale (handled by cron, not here)

        We need effective status because mail.presence.status in the DB can be
        'online' even for users who have been disconnected for hours — the server
        never sets it back to 'offline' when the browser tab closes.
        """
        new_status = vals.get('status')
        if new_status not in ('online', 'away'):
            return super().write(vals)

        now = fields.Datetime.now()
        threshold = now - timedelta(seconds=ONLINE_THRESHOLD_SECONDS)

        for presence in self:
            if not presence.user_id:
                continue

            # Was this presence effectively offline before this write?
            # (last_poll older than threshold means the browser was closed)
            was_effectively_offline = (
                not presence.last_poll or presence.last_poll < threshold
            )

            # What was the last logged status for this employee?
            employee = self._get_employee_for_user(presence.user_id.id)
            if not employee:
                continue

            last_log = self.env['x.employee.presence.log'].sudo().search(
                [('employee_id', '=', employee.id)],
                order='timestamp desc', limit=1
            )
            last_logged_status = last_log.status if last_log else 'offline'

            if was_effectively_offline and last_logged_status != 'offline':
                # User reconnected after being offline but the cron hasn't caught up yet.
                # Write the offline event first (at last_poll time), then the online event.
                offline_ts = presence.last_poll or now
                self.env['x.employee.presence.log'].sudo().create({
                    'employee_id': employee.id,
                    'user_id': presence.user_id.id,
                    'timestamp': offline_ts,
                    'status': 'offline',
                })
                last_logged_status = 'offline'

            # Now log the new status if it differs from last logged
            if last_logged_status != new_status:
                self._log_presence_change(presence.user_id.id, new_status)

        return super().write(vals)
