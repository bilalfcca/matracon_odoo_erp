"""Shared workflow notifications — email + in-app via chatter followers.

Module-level helpers only. Do not use as an Odoo _inherit target or Python
base class on models.Model subclasses (Odoo 19 rejects both patterns).
"""

from markupsafe import Markup

from odoo import _


def notify_users(record, users, body, summary=None):
    """Notify users via chatter (email if follower + mail enabled)."""
    record.ensure_one()
    if not users:
        return
    partners = users.mapped('partner_id').filtered(lambda p: p.id)
    if not partners:
        return
    record.message_subscribe(partner_ids=partners.ids)
    record.message_post(
        body=Markup(body),
        subject=summary or _('Matracon Notification'),
        partner_ids=partners.ids,
        message_type='notification',
        subtype_xmlid='mail.mt_comment',
    )


def notify_group(record, group_xml_id, body, summary=None):
    """Notify all users in a security group."""
    record.ensure_one()
    group = record.env.ref(group_xml_id, raise_if_not_found=False)
    if not group:
        return
    users = record.env['res.users'].search([('group_ids', 'in', group.id)])
    notify_users(record, users, body, summary=summary)


def schedule_activity(record, users, summary, note=None):
    """Create todo activities for approvers."""
    record.ensure_one()
    activity_type = record.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)
    if not activity_type:
        return
    for user in users:
        if not user:
            continue
        record.activity_schedule(
            activity_type_id=activity_type.id,
            user_id=user.id,
            summary=summary,
            note=note or summary,
        )


def site_accountants_for_analytic(env, analytic_account):
    if not analytic_account:
        return env['res.users']
    config = env['x.project.site.config'].search([
        ('analytic_account_id', '=', analytic_account.id),
    ], limit=1)
    return config.x_site_accountant_ids if config else env['res.users']
