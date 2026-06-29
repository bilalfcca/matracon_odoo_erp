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

from odoo import fields, _

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
    g_matracon_admin = env.ref('purchase_demand_raise.group_matracon_admin', raise_if_not_found=False)
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


def seed_demo_bank_balances(env):
    """Opening balances for HBL / BOK demo bank journals (idempotent)."""
    import logging
    _logger = logging.getLogger(__name__)
    Move = env['account.move'].sudo()
    journal_refs = {
        'site_operations.bank_journal_hbl': 50_000_000.0,
        'site_operations.bank_journal_bok': 25_000_000.0,
    }
    company = env.company
    equity = env.ref('account.1_equity', raise_if_not_found=False)
    if not equity:
        equity = env['account.account'].search([
            ('account_type', '=', 'equity'),
            ('company_ids', 'in', company.id),
        ], limit=1)
    if not equity:
        _logger.warning('seed_demo_bank_balances: no equity account — skipped')
        return
    for xml_id, amount in journal_refs.items():
        journal = env.ref(xml_id, raise_if_not_found=False)
        if not journal or not journal.default_account_id:
            continue
        ref = 'matracon_opening_%s' % journal.code
        if Move.search([('ref', '=', ref)], limit=1):
            continue
        bank_acc = journal.default_account_id
        Move.create({
            'move_type': 'entry',
            'date': fields.Date.today(),
            'ref': ref,
            'journal_id': env['account.journal'].search([
                ('type', '=', 'general'),
                ('company_id', '=', company.id),
            ], limit=1).id,
            'line_ids': [
                (0, 0, {
                    'name': _('Opening balance %s') % journal.name,
                    'account_id': bank_acc.id,
                    'debit': amount,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': _('Opening balance %s') % journal.name,
                    'account_id': equity.id,
                    'debit': 0.0,
                    'credit': amount,
                }),
            ],
        }).action_post()


def migrate_matracon_admin_group(env):
    """Move users from legacy site_operations admin group to purchase_demand_raise."""
    old = env.ref('site_operations.group_matracon_admin', raise_if_not_found=False)
    new = env.ref('purchase_demand_raise.group_matracon_admin', raise_if_not_found=False)
    if not new or not old or old.id == new.id:
        return
    users = env['res.users'].sudo().search([('group_ids', 'in', old.id)])
    for user in users:
        user.write({'group_ids': [(3, old.id), (4, new.id)]})


def reprocess_existing_payments(env):
    """
    Backfill side-effects for payments that were posted before the Odoo 19
    state-fix (state='posted' → 'in_process'/'paid').

    Re-runs on every module upgrade so it is always idempotent:
      - Tags payment move lines with analytic distribution
      - Updates liability sheet line paid_amount / marks x_payment_status='paid'
      - Invalidates project fund caches so financial overview is accurate
    """
    import logging
    _logger = logging.getLogger(__name__)

    POSTED = ('in_process', 'paid', 'partial', 'posted')

    Payment = env['account.payment'].sudo()
    posted = Payment.search([('state', 'in', list(POSTED))])
    if not posted:
        return

    _logger.info('reprocess_existing_payments: processing %d payments', len(posted))

    # 1. Analytic tagging on existing move lines
    for payment in posted:
        try:
            payment._matracon_tag_payment_move_analytic()
        except Exception:
            pass

    # 2. Liability paid_amount update
    liability_payments = posted.filtered(lambda p: p.x_liability_sheet_line_id)
    for payment in liability_payments:
        try:
            line = payment.x_liability_sheet_line_id
            sibling_payments = payment.x_liability_sheet_id.payment_ids.filtered(
                lambda p: p.state in POSTED and p.x_liability_sheet_line_id == line
            )
            line.paid_amount = sum(
                p.x_gross_approved_amount or p.amount for p in sibling_payments
            )
            if payment.x_payment_status != 'paid':
                payment.x_payment_status = 'paid'
            if payment.x_liability_sheet_id:
                payment.x_liability_sheet_id.action_finalize_if_fully_paid()
        except Exception:
            pass

    # 3. Invalidate project fund caches so overview recomputes correctly
    try:
        posted._matracon_invalidate_project_funds()
    except Exception:
        pass

    _logger.info('reprocess_existing_payments: done')


def post_init_hook(env):
    try:
        migrate_matracon_admin_group(env)
        configure_production_users(env)
        env['x.project.site.config']._matracon_ensure_site_warehouses()
        sync_alternative_prs(env)
        seed_demo_bank_balances(env)
        env['x.matracon.app.visibility'].apply_menu_visibility()
        # Finance HO needs payroll functional access if hr_payroll is installed
        payroll_user = env.ref('hr_payroll.group_hr_payroll_user', raise_if_not_found=False)
        if payroll_user:
            env.ref('site_operations.group_finance_ho').sudo().write({
                'implied_ids': [(4, payroll_user.id)],
            })
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            'site_operations post_init_hook: skipped user configuration '
            '(not a production DB or users not yet created): %s', e
        )
    reprocess_existing_payments(env)


def post_migrate_hook(env):
    reprocess_existing_payments(env)
    # Re-apply production user config (groups + default analytic/warehouse) on every update
    # so that a module upgrade or re-install never silently resets site user settings.
    try:
        configure_production_users(env)
        env['x.project.site.config']._matracon_ensure_site_warehouses()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            'post_migrate_hook: configure_production_users skipped: %s', e)
    # Re-apply payroll functional group to Finance HO if hr_payroll is installed
    try:
        payroll_user = env.ref('hr_payroll.group_hr_payroll_user', raise_if_not_found=False)
        if payroll_user:
            env.ref('site_operations.group_finance_ho').sudo().write({
                'implied_ids': [(4, payroll_user.id)],
            })
    except Exception:
        pass
    # Always re-apply menu visibility on update so group changes take effect.
    try:
        env['x.matracon.app.visibility'].apply_menu_visibility()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning('post_migrate_hook: apply_menu_visibility failed: %s', e)
