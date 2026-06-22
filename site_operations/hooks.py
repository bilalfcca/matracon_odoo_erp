# DEMO USERS (site_operations/demo/demo_users.xml) — same roles, dev logins:
#   bilal.khan@matracon.pk      → Admin + Head Office        (password: admin)
#   ceo@matracon.pk             → CEO Approval + Head Office (password: user)
#   procurement@matracon.pk     → Procurement HO + HO        (password: user)
#   finance@matracon.pk         → Finance HO + HO            (password: user)
#   accountant.mch@matracon.pk  → MCH Site Accountant        (password: user)
#   store.mch@matracon.pk       → MCH Site Store             (password: user)
#   accountant.rwasa@matracon.pk→ RWASA Site Accountant      (password: user)
#   store.rwasa@matracon.pk     → RWASA Site Store           (password: user)
#   accountant.stp@matracon.pk  → STP Site Accountant        (password: user)
#   store.stp@matracon.pk       → STP Site Store             (password: user)
# ═══════════════════════════════════════════════════════════════════════════
# HEAD OFFICE (all projects):
#   ID  2 → Bilal Khan (Admin)     → group_head_office
#   ID  5 → CEO                    → group_ceo_approval, group_head_office
#   ID 10 → Procurement HO         → group_procurement_ho, group_head_office
#   ID 11 → Finance HO             → group_finance_ho, group_head_office
#
# MCH - BAHAWALNAGAR:
#   ID 12 → Site Accountant        → group_site_accountant
#   ID 13 → Site Store Keeper      → group_site_store
#
# RWASA:
#   ID  6 → Site Accountant        → group_site_accountant
#   ID  7 → Site Store Keeper      → group_site_store
#
# STP - MARDAN:
#   ID  9 → Site Accountant        → group_site_accountant
#   ID  8 → Site Store Keeper      → group_site_store
# ═══════════════════════════════════════════════════════════════════════════

PRODUCTION_CONFIG = {
    'head_office_ids': [2, 5, 10, 11],
    'ceo_ids': [5],
    'procurement_ho_ids': [10],
    'finance_ho_ids': [11],
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

    # Groups
    g_head_office = env.ref('purchase_demand_raise.group_head_office')
    g_ceo = env.ref('purchase_demand_raise.group_ceo_approval')
    g_proc_ho = env.ref('purchase_demand_raise.group_procurement_ho')
    g_finance_ho = env.ref('site_operations.group_finance_ho')
    g_site_store = env.ref('purchase_demand_raise.group_site_store')
    g_site_accountant = env.ref('site_operations.group_site_accountant')

    # Head Office users
    for uid, extra_groups in [
        (2, [g_head_office]),                       # Bilal Khan - Admin, just add HO visibility
        (5, [g_head_office, g_ceo]),                # CEO
        (10, [g_head_office, g_proc_ho]),           # Procurement HO
        (11, [g_head_office, g_finance_ho]),        # Finance HO
    ]:
        user = Users.sudo().browse(uid).exists()
        if not user:
            continue
        for grp in extra_groups:
            if grp.id not in user.sudo().groups_id.ids:
                user.sudo().write({'groups_id': [(4, grp.id)]})

    # Site users - per project
    SiteConfig = env['x.project.site.config']
    for project_name, cfg in PRODUCTION_CONFIG['projects'].items():
        analytic = env.ref(cfg['analytic_xml_id'], raise_if_not_found=False)
        if not analytic:
            continue

        # Get or create site config
        site_config = SiteConfig.search([('analytic_account_id', '=', analytic.id)], limit=1)
        if not site_config:
            site_config = SiteConfig.create({
                'name': project_name,
                'analytic_account_id': analytic.id,
            })

        # Site Store users
        store_users = Users.browse(cfg['site_store_ids']).exists()
        if store_users:
            site_config.write({'site_user_ids': [(4, u.id) for u in store_users]})

        # Site Accountant users
        accountant_users = Users.browse(cfg['site_accountant_ids']).exists()
        for user in accountant_users:
            user = user.sudo()
            if g_site_accountant.id not in user.groups_id.ids:
                user.write({'groups_id': [(4, g_site_accountant.id)]})
            user.write({
                'x_default_analytic_account_id': analytic.id,
                'x_site_config_id': site_config.id,
            })
            if site_config.warehouse_id:
                user.write({'x_default_warehouse_id': site_config.warehouse_id.id})

        # Also add accountants to the x_site_accountant_ids field
        if accountant_users:
            site_config.write({'x_site_accountant_ids': [(4, u.id) for u in accountant_users]})


def post_init_hook(env):
    try:
        configure_production_users(env)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            'site_operations post_init_hook: skipped user configuration '
            '(not a production DB or users not yet created): %s', e
        )
    try:
        env['project.project'].sync_from_site_configs()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            'site_operations post_init_hook: project sync skipped: %s', e
        )
