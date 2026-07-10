from datetime import timedelta

from odoo import models, fields, api

# Must match ONLINE_THRESHOLD_SECONDS in hr_employee_ext.py.
# 600 s = 10 minutes — user stays online until 10 min of no heartbeat.
ONLINE_THRESHOLD_SECONDS = 600


class MailPresenceExt(models.Model):
    """Hook into mail.presence to maintain session records in x.employee.presence.log.

    Each session = one continuous browser open period (online_at → offline_at).

    Rules:
      • A session is only split when the user was absent for > ONLINE_THRESHOLD_SECONDS.
        Brief reconnects (server restarts, page reloads, tab switches) are ignored and
        the existing session continues uninterrupted.
      • Offline detection is handled by the cron (hr.employee._cron_update_presence_log),
        NOT here — Odoo never sends an 'offline' event when the browser closes.

    create() — fired when a presence record is (re)created:
        • If there is already an open session that was started within ONLINE_THRESHOLD_SECONDS,
          this is a rapid reconnect or server restart — let the session continue.
        • If there is no open session (or the open one is stale), start a new session.

    write() — fired on every heartbeat:
        • If last_poll is None, the presence record was just created — create() handled it.
        • If last_poll is recent (< ONLINE_THRESHOLD_SECONDS ago), the user is continuously
          online — session continues, no action.
        • If last_poll is stale (> ONLINE_THRESHOLD_SECONDS ago), the user is reconnecting
          after a real absence — close the old session and start a new one.
    """
    _inherit = 'mail.presence'

    # ── helpers ─────────────────────────────────────────────────────────────

    def _get_employee(self, user_id):
        return self.env['hr.employee'].sudo().search(
            [('user_id', '=', user_id)], limit=1,
        )

    def _open_session_for(self, employee, user_id, now, close_existing_at=None):
        """Close any open session (if meaningful) and start a new one.

        close_existing_at — timestamp to stamp on the open session's offline_at.
                            If None, uses now. Only closes if the resulting
                            duration would be ≥ ONLINE_THRESHOLD_SECONDS.
        """
        Session = self.env['x.employee.presence.log'].sudo()

        open_session = Session.search([
            ('employee_id', '=', employee.id),
            ('offline_at', '=', False),
        ], limit=1)

        if open_session:
            effective_close = close_existing_at or now
            duration = (effective_close - open_session.online_at).total_seconds()
            if duration >= ONLINE_THRESHOLD_SECONDS:
                open_session.offline_at = effective_close
            # else: session is too short to be meaningful — discard and replace

        Session.create({
            'employee_id': employee.id,
            'user_id': user_id,
            'online_at': now,
        })

    # ── ORM hooks ────────────────────────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        now = fields.Datetime.now()
        Session = self.env['x.employee.presence.log'].sudo()

        for rec in records:
            if not rec.user_id:
                continue
            emp = self._get_employee(rec.user_id.id)
            if not emp:
                continue

            open_session = Session.search([
                ('employee_id', '=', emp.id),
                ('offline_at', '=', False),
            ], limit=1)

            if open_session:
                gap = (now - open_session.online_at).total_seconds()
                if gap < ONLINE_THRESHOLD_SECONDS:
                    # Session was opened very recently — this is a server restart
                    # or rapid reconnect. Let the session continue uninterrupted.
                    continue
                # Session is stale (cron hasn't caught up yet). Close it at a
                # conservative estimate and start fresh.
                open_session.offline_at = now - timedelta(seconds=ONLINE_THRESHOLD_SECONDS)

            Session.create({
                'employee_id': emp.id,
                'user_id': rec.user_id.id,
                'online_at': now,
            })

        return records

    def write(self, vals):
        """On each heartbeat, detect genuine reconnects and start a new session."""
        new_status = vals.get('status')
        # Only intercept heartbeat writes ('online' / 'away')
        if new_status not in ('online', 'away'):
            return super().write(vals)

        now = fields.Datetime.now()
        threshold = now - timedelta(seconds=ONLINE_THRESHOLD_SECONDS)
        Session = self.env['x.employee.presence.log'].sudo()

        for presence in self:
            if not presence.user_id:
                continue

            # If last_poll is None the presence record was just created by create().
            # create() already handled session management — skip here to avoid duplicates.
            if not presence.last_poll:
                continue

            # If last_poll is recent, the user is continuously online — no action needed.
            if presence.last_poll >= threshold:
                continue

            # last_poll is stale → user is genuinely reconnecting after > 65 s absence.
            emp = self._get_employee(presence.user_id.id)
            if not emp:
                continue

            # Close existing session at last_poll (final heartbeat ≈ disconnect time),
            # then start a fresh session.
            open_session = Session.search([
                ('employee_id', '=', emp.id),
                ('offline_at', '=', False),
            ], limit=1)
            if open_session:
                open_session.offline_at = presence.last_poll

            Session.create({
                'employee_id': emp.id,
                'user_id': presence.user_id.id,
                'online_at': now,
            })

        return super().write(vals)
