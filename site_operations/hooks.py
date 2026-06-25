# ═══════════════════════════════════════════════════════════════════════════
# PRODUCTION USER CONFIGURATION — Matracon Pakistan
# ═══════════════════════════════════════════════════════════════════════════
# User IDs are stable in production. Groups are the source of truth for access.
#
# HEAD OFFICE (all projects) — each user gets group_head_office + role group:
#   ID  2 → Bilal Khan (Admin)     → group_matracon_admin (implies all HO roles)
#   ID  5 → CEO                    → group_head_office + group_ceo_approval
#   ID 10 → Procurement Officer    → group_head_office + group_procurement_ho
#   ID 11 → Finance Officer        → group_head_office + group_finance_ho
#
# SITE USERS (one project each via Site Project Configuration):
#   MCH:      accountant 12, store 13
#   RWASA:    accountant  6, store  7
#   STP:      accountant  9, store  8
# ═══════════════════════════════════════════════════════════════════════════

PRODUCTION_CONFIG = {
    'head_office_ids': [2, 5, 10, 11],
    'ceo_ids': [5],
    'procurement_ho_ids': [10],
    'finance_ho_ids': [11],
    'admin_ids': [2],
    'projects': {
        'MCH - BAHAWALNAGAR': {
            'analytic_xml_id': 'purchase_demand_raise.analytic_account_mch_bahawalnagar',
            'site_accountant_ids': [12],
            'site_store_ids': [13],
        },
        'RWASA': {
            'analytic_xml_id': 'purchase_demand_raise.analytic_account_rwasa',
            'site_accountant_ids': [6],
            'site_store_ids': [7],
        },
        'STP - MARDAN': {
            'analytic_xml_id': 'purchase_demand_raise.analytic_account_stp_mardan',
            'site_accountant_ids': [9],
            'site_store_ids': [8],
        },
    }
}


def configure_production_users(env):
    """
    Configure security groups and default project for all production users.
    Safe to call multiple times (idempotent). Skips users that don't exist
    in this environment (dev environments have different user IDs).
    """
    Users = env['res.users']

    g_head_office = env.ref('purchase_demand_raise.group_head_office')
    g_ceo = env.ref('purchase_demand_raise.group_ceo_approval')
    g_proc_ho = env.ref('purchase_demand_raise.group_procurement_ho')
    g_finance_ho = env.ref('site_operations.group_finance_ho')
    g_matracon_admin = env.ref('site_operations.group_matracon_admin', raise_if_not_found=False)
    g_site_store = env.ref('purchase_demand_raise.group_site_store')
    g_stock_user = env.ref('stock.group_stock_user', raise_if_not_found=False)
    g_site_accountant = env.ref('site_operations.group_site_accountant')

    # ── Head Office users by role ───────────────────────────────────────────
    for uid in PRODUCTION_CONFIG['admin_ids']:
        user = Users.sudo().browse(uid).exists()
        if user and g_matracon_admin:
            Users._matracon_add_group(user, g_matracon_admin)

    for uid in PRODUCTION_CONFIG['ceo_ids']:
        user = Users.sudo().browse(uid).exists()
        if not user:
            continue
        Users._matracon_add_group(user, g_head_office)
        Users._matracon_add_group(user, g_ceo)

    for uid in PRODUCTION_CONFIG['procurement_ho_ids']:
        user = Users.sudo().browse(uid).exists()
        if not user:
            continue
        Users._matracon_add_group(user, g_head_office)
        Users._matracon_add_group(user, g_proc_ho)

    for uid in PRODUCTION_CONFIG['finance_ho_ids']:
        user = Users.sudo().browse(uid).exists()
        if not user:
            continue
        Users._matracon_add_group(user, g_head_office)
        Users._matracon_add_group(user, g_finance_ho)

    # ── Site users — per project via Site Project Configuration ─────────────
    SiteConfig = env['x.project.site.config']
    for project_name, cfg in PRODUCTION_CONFIG['projects'].items():
        analytic = env.ref(cfg['analytic_xml_id'], raise_if_not_found=False)
        if not analytic:
            continue

        site_config = SiteConfig.search([('analytic_account_id', '=', analytic.id)], limit=1)
        if not site_config:
            site_config = SiteConfig.create({
                'name': project_name,
                'analytic_account_id': analytic.id,
            })

        store_users = Users.browse(cfg['site_store_ids']).exists()
        if store_users:
            site_config.write({'site_user_ids': [(4, u.id) for u in store_users]})
            for user in store_users:
                Users._matracon_add_group(user, g_site_store)
                if g_stock_user:
                    Users._matracon_add_group(user, g_stock_user)

        accountant_users = Users.browse(cfg['site_accountant_ids']).exists()
        for user in accountant_users:
            Users._matracon_add_group(user, g_site_accountant)
            user.sudo().write({
                'x_default_analytic_account_id': analytic.id,
                'x_site_config_id': site_config.id,
            })
            if site_config.warehouse_id:
                user.sudo().write({
                    'x_default_warehouse_id': site_config.warehouse_id.id,
                })

        if accountant_users:
            site_config.write({
                'x_site_accountant_ids': [(4, u.id) for u in accountant_users],
            })


def sync_alternative_prs(env):
    """Re-sync all alternative RFQs from their root PR (safe after module upgrade)."""
    PO = env['purchase.order']
    roots = PO.search([
        ('x_is_pr_document', '=', True),
        ('purchase_group_id', '!=', False),
    ])
    if roots:
        roots._matracon_sync_alternatives_from_root()
    # Mark cancelled Odoo alternatives
    cancelled = PO.search([
        ('state', '=', 'cancel'),
        ('x_pr_state', '!=', 'cancelled'),
    ])
    for order in cancelled:
        order.x_pr_state = 'cancelled'


def post_init_hook(env):
    try:
        configure_production_users(env)
        sync_alternative_prs(env)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            'site_operations post_init_hook: skipped user configuration '
            '(not a production DB or users not yet created): %s', e
        )
