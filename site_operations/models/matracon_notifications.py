"""Shared workflow notifications — email + in-app via chatter followers.

Plain Python mixin (not an Odoo _inherit target) so it can be mixed into
account.move without duplicating standard relational fields.
"""

from markupsafe import Markup

from odoo import _


class MatraconNotificationsMixin:
    """Mixin providing notification helpers for models with mail.thread."""

    def _matracon_notify_users(self, users, body, summary=None):
        """Notify users via chatter (email if follower + mail enabled)."""
        self.ensure_one()
        if not users:
            return
        partners = users.mapped('partner_id').filtered(lambda p: p.id)
        if not partners:
            return
        self.message_subscribe(partner_ids=partners.ids)
        self.message_post(
            body=Markup(body),
            subject=summary or _('Matracon Notification'),
            partner_ids=partners.ids,
            message_type='notification',
            subtype_xmlid='mail.mt_comment',
        )

    def _matracon_notify_group(self, group_xml_id, body, summary=None):
        """Notify all users in a security group."""
        self.ensure_one()
        group = self.env.ref(group_xml_id, raise_if_not_found=False)
        if not group:
            return
        users = self.env['res.users'].search([('group_ids', 'in', group.id)])
        self._matracon_notify_users(users, body, summary=summary)

    def _matracon_schedule_activity(self, users, summary, note=None):
        """Create todo activities for approvers."""
        self.ensure_one()
        activity_type = self.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)
        if not activity_type:
            return
        for user in users:
            if not user:
                continue
            self.activity_schedule(
                activity_type_id=activity_type.id,
                user_id=user.id,
                summary=summary,
                note=note or summary,
            )

    def _matracon_site_accountants_for_analytic(self, analytic_account):
        if not analytic_account:
            return self.env['res.users']
        config = self.env['x.project.site.config'].search([
            ('analytic_account_id', '=', analytic_account.id),
        ], limit=1)
        return config.x_site_accountant_ids if config else self.env['res.users']
