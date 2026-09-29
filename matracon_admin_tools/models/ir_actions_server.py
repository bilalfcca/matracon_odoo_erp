from odoo import fields, models


class IrActionsServer(models.Model):
    """Two additive fields that turn the standard Server Actions model into
    the backing store for the "Settings > Fix Tools" screen (see
    views/ir_actions_server_views.xml). Nothing about how ir.actions.server
    itself works is changed - existing Server Actions elsewhere in the
    system (automations, technical actions, etc.) are entirely unaffected;
    they simply don't have x_is_fix_tool set, so the Fix Tools screen's
    domain filter never shows them.
    """
    _inherit = 'ir.actions.server'

    x_is_fix_tool = fields.Boolean(
        string='Fix Tool',
        help='Show this Server Action in Settings > Fix Tools. Check this for any '
             'admin/data-repair action so it is discoverable in one place instead of '
             'a one-off button on a model view.',
    )
    x_fix_tool_description = fields.Text(
        string='Description',
        help='Plain-language explanation of what this tool does and when to run it, '
             'shown in Settings > Fix Tools so it is usable without reading the code.',
    )
