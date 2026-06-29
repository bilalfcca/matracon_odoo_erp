"""Upgrade hook: link project.project records to site configurations."""

def migrate(cr, version):
    from odoo import api, SUPERUSER_ID
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['project.project'].sync_from_site_configs()
