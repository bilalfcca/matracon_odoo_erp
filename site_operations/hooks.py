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


def deduplicate_partner_tags(env):
    """
    Remove duplicate res.partner.category records that share the same name.
    Keeps the record with the lowest ID; re-links all partner associations.
    Idempotent — safe to call on every upgrade.
    """
    import logging
    _logger = logging.getLogger(__name__)
    cr = env.cr
    cr.execute("""
        SELECT name, array_agg(id ORDER BY id) AS ids
        FROM res_partner_category
        GROUP BY name
        HAVING count(*) > 1
    """)
    rows = cr.fetchall()
    for name, ids in rows:
        keep_id = ids[0]
        dup_ids = ids[1:]
        _logger.info('deduplicate_partner_tags: keeping tag id=%s "%s", removing %s', keep_id, name, dup_ids)
        # Re-link partners that point to a duplicate but not yet to the keeper
        cr.execute("""
            UPDATE res_partner_res_partner_category_rel
            SET category_id = %s
            WHERE category_id = ANY(%s)
              AND partner_id NOT IN (
                SELECT partner_id FROM res_partner_res_partner_category_rel
                WHERE category_id = %s
              )
        """, (keep_id, dup_ids, keep_id))
        # Drop any remaining duplicate links (partner already has keeper)
        cr.execute("""
            DELETE FROM res_partner_res_partner_category_rel
            WHERE category_id = ANY(%s)
        """, (dup_ids,))
        # Delete the duplicate tag records
        cr.execute("DELETE FROM res_partner_category WHERE id = ANY(%s)", (dup_ids,))


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


def set_pkr_decimal_places(env):
    """
    Force PKR (Pakistani Rupee) to 0 decimal places so all monetary fields
    display as whole numbers. PKR paise are not used in practice.
    Uses raw SQL because the base.PKR record has noupdate=True in ir.model.data.
    Also called from the 1.7.9 migration script so it applies on upgrades too.
    """
    import logging
    _logger = logging.getLogger(__name__)
    env.cr.execute(
        "UPDATE res_currency SET decimal_places = 0 WHERE name = 'PKR' AND decimal_places != 0"
    )
    if env.cr.rowcount:
        _logger.info('set_pkr_decimal_places: set PKR to 0 decimal places')
        env['res.currency'].invalidate_model(['decimal_places'])


def set_analytic_percentage_precision(env):
    """
    Raise 'Percentage Analytic' decimal precision from 2 → 6.

    The analytic_distribution widget rounds the stored percentage ratio to
    (analytic_precision + 2) decimal places before saving to the JSON field.
    With the default of 2 the ratio has only 4 dp:
        4,001 / 40,000 = 0.10002500 → 0.1000 (4 dp) → back-calc 4,000  ← wrong
    With 6 the ratio has 8 dp:
        4,001 / 40,000 = 0.10002500 → 0.10002500 (8 dp) → back-calc 4,001 ✓

    Uses raw SQL because the analytic.decimal_percentage_analytic record has
    noupdate=True in ir.model.data and cannot be overridden from a data file.
    """
    import logging
    _logger = logging.getLogger(__name__)
    env.cr.execute(
        "UPDATE decimal_precision SET digits = 6 WHERE name = 'Percentage Analytic' AND digits < 6"
    )
    if env.cr.rowcount:
        _logger.info('set_analytic_percentage_precision: raised Percentage Analytic precision to 6')
        env['decimal.precision'].invalidate_model(['digits'])


def set_date_format(env):
    """
    Set DD/MM/YYYY date format on every installed language so the format
    applies globally: views, list/form fields, format_date() calls, and
    standard Odoo reports all read res.lang.date_format at render time.
    Uses raw SQL to bypass ORM write restrictions on res.lang.
    """
    import logging
    _logger = logging.getLogger(__name__)
    env.cr.execute("UPDATE res_lang SET date_format = '%d/%m/%Y'")
    _logger.info('set_date_format: applied %%d/%%m/%%Y to all res.lang rows (%d updated)',
                 env.cr.rowcount)
    # Invalidate cached language data so the change is picked up immediately
    env['res.lang'].invalidate_model(['date_format'])


def set_pakistan_fiscal_year(env):
    """
    Set the fiscal year to July 1 – June 30 (Pakistan standard) on every
    company in this database.

    Odoo's accounting reports (Trial Balance, P&L, Balance Sheet, etc.) and
    the date-range picker all derive their "Year" period from these two fields:
        fiscalyear_last_day   = 30  (June 30)
        fiscalyear_last_month = '6' (June)

    After this change the "Year" filter in reports will show the FY period
    07/01/YYYY–06/30/YYYY+1 instead of the default 01/01–12/31 calendar year.

    Uses raw SQL so it cannot be blocked by noupdate flags or ORM validation
    (which can reject the write if the company already has posted entries).
    Safe to call on every upgrade — only updates rows that are still on the
    default calendar-year end (month=12, day=31) to avoid clobbering a
    deliberate manual override.
    """
    import logging
    _logger = logging.getLogger(__name__)
    env.cr.execute(
        """
        UPDATE res_company
        SET    fiscalyear_last_day   = 30,
               fiscalyear_last_month = '6'
        WHERE  fiscalyear_last_month != '6'
           OR  fiscalyear_last_day   != 30
        """
    )
    updated = env.cr.rowcount
    if updated:
        _logger.info(
            'set_pakistan_fiscal_year: updated %d company record(s) to Jul–Jun FY',
            updated,
        )
        env['res.company'].invalidate_model(['fiscalyear_last_day', 'fiscalyear_last_month'])


def post_init_hook(env):
    set_pkr_decimal_places(env)
    set_analytic_percentage_precision(env)
    set_date_format(env)
    set_pakistan_fiscal_year(env)
    deduplicate_partner_tags(env)
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
    ensure_deduction_accounts(env)
    cleanup_stale_views(env)
    reprocess_existing_payments(env)
    # Restrict Odoo's built-in 'see all' account.move rules to group_account_manager
    # so site accountants are properly scoped to their own project.
    fix_account_move_rules(env)
    # Fix any posted petty cash expenses that have no JE or wrong JE credit account.
    # On a fresh install into a DB with existing data (e.g. a restored production dump),
    # post_migrate_hook does NOT run — only post_init_hook runs.
    try:
        migrate_petty_cash_expense_lines(env)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            'post_init_hook: migrate_petty_cash_expense_lines failed: %s', e)
    try:
        fix_petty_cash_expense_accounts(env)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            'post_init_hook: fix_petty_cash_expense_accounts failed: %s', e)
    # Mark Main accounts, create bank children, and wire parent-child hierarchy.
    try:
        setup_main_accounts_and_bank_children(env)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            'post_init_hook: setup_main_accounts_and_bank_children failed: %s', e)
    # Build account.group records from the x_parent_account_id tree so the
    # Trial Balance shows collapsible group headers with subtotals.
    try:
        setup_account_groups(env)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            'post_init_hook: setup_account_groups failed: %s', e)


def migrate_petty_cash_expense_lines(env):
    """One-time migration: create x.petty.cash.expense.line for every existing
    flat expense that has no lines yet.

    Reads the legacy flat columns directly via SQL (bypassing the ORM) so that
    the values are available even when the ``amount`` field is later driven by
    line totals.  Safe to run multiple times — skips expenses that already have lines.
    """
    import logging
    _log = logging.getLogger(__name__)

    env.cr.execute("""
        SELECT
            e.id,
            e.name,
            e.amount,
            e.expense_account_id,
            e.is_employee_advance,
            e.employee_id,
            e.is_subcontractor_advance,
            e.advance_subcontractor_id
        FROM x_petty_cash_expense e
        WHERE NOT EXISTS (
            SELECT 1 FROM x_petty_cash_expense_line l WHERE l.expense_id = e.id
        )
        AND e.amount IS NOT NULL AND e.amount > 0
    """)
    rows = env.cr.fetchall()
    if not rows:
        _log.info('migrate_petty_cash_expense_lines: all expenses already have lines, nothing to do.')
        return

    _log.info('migrate_petty_cash_expense_lines: migrating %d flat expense(s) → lines', len(rows))
    Line = env['x.petty.cash.expense.line'].sudo()
    for (exp_id, name, amount, acct_id, is_emp, emp_id, is_sc, sc_id) in rows:
        try:
            Line.create({
                'expense_id': exp_id,
                'name': name or 'Expense',
                'amount': amount,
                'expense_account_id': acct_id or False,
                'is_employee_advance': is_emp or False,
                'employee_id': emp_id or False,
                'is_subcontractor_advance': is_sc or False,
                'advance_subcontractor_id': sc_id or False,
            })
        except Exception as e:
            _log.warning(
                'migrate_petty_cash_expense_lines: expense id=%s skipped (%s)', exp_id, e)
    _log.info('migrate_petty_cash_expense_lines: done.')


def fix_petty_cash_expense_accounts(env):
    """Back-fill x_petty_cash_account_id on petty cash expenses where it was never
    persisted (readonly field not sent by browser on save), create missing journal
    entries, and correct JEs that were created with the wrong credit account.

    Runs on every module upgrade via post_migrate_hook. Safe to call multiple times
    — each step is idempotent and skips records that are already correct.

    Three-step process
    ──────────────────
    Step 1 — Fill x_petty_cash_account_id where NULL
        Resolution order:
          1. fund._get_petty_cash_account()  →  fund.x_petty_cash_account_id
                                             OR  site_config.x_petty_cash_account_id
          2. Cash journal linked to the site analytic  →  journal.default_account_id
          3. Any company cash journal                  →  journal.default_account_id

    Step 2 — Create missing JEs for posted expenses
        Covers expenses where _create_journal_entry() silently returned early
        (no credit account resolved, no cash journal found at post time).

    Step 3 — Correct JEs where credit account ≠ x_petty_cash_account_id
        Covers expenses posted before x_petty_cash_account_id was set on the record.
        _create_journal_entry fell back to cash_journal.default_account_id, which
        may be a different account than the configured site petty cash account.
        Fix: reverse the wrong JE, clear x_account_move_id, re-run _create_journal_entry.
    """
    import logging
    _logger = logging.getLogger(__name__)

    PCE = env['x.petty.cash.expense'].sudo()
    Journal = env['account.journal'].sudo()

    # ── Step 1: fill missing x_petty_cash_account_id ─────────────────────────
    missing_account = PCE.search([('x_petty_cash_account_id', '=', False)])
    _logger.info(
        'fix_petty_cash_expense_accounts: %d expense(s) have no petty cash account set',
        len(missing_account),
    )
    filled = 0
    skipped_no_config = []
    for expense in missing_account:
        pc_account = expense.fund_id._get_petty_cash_account()

        # Fallback: look up the site cash journal (same logic as _create_journal_entry)
        if not pc_account:
            analytic = expense.project_analytic_account_id
            cash_journal = False
            if analytic:
                cash_journal = Journal.search([
                    ('type', '=', 'cash'),
                    ('x_site_ids', 'in', [analytic.id]),
                    ('company_id', '=', env.company.id),
                ], limit=1)
            if not cash_journal:
                cash_journal = Journal.search([
                    ('type', '=', 'cash'),
                    ('company_id', '=', env.company.id),
                ], limit=1)
            if cash_journal and cash_journal.default_account_id:
                pc_account = cash_journal.default_account_id

        if pc_account:
            expense.x_petty_cash_account_id = pc_account
            filled += 1
        else:
            skipped_no_config.append(expense.x_ref or str(expense.id))

    if filled:
        _logger.info(
            'fix_petty_cash_expense_accounts: filled x_petty_cash_account_id on %d expense(s)',
            filled,
        )
    if skipped_no_config:
        _logger.warning(
            'fix_petty_cash_expense_accounts: %d expense(s) skipped — no petty cash account '
            'found (configure "Petty Cash Account" on Site Configuration for each site): %s',
            len(skipped_no_config),
            ', '.join(skipped_no_config[:20]),
        )

    # ── Step 2: create missing JEs for posted expenses ────────────────────────
    # These are expenses that were "posted" but _create_journal_entry silently
    # returned early (no credit account / no journal) — so the GL was never
    # touched and the fund balance never decreased.
    posted_no_je = PCE.search([
        ('state', '=', 'posted'),
        ('x_account_move_id', '=', False),
    ])
    _logger.info(
        'fix_petty_cash_expense_accounts: %d posted expense(s) have no journal entry',
        len(posted_no_je),
    )
    created = 0
    failed = []
    for expense in posted_no_je:
        try:
            expense._create_journal_entry()
            if expense.x_account_move_id:
                created += 1
            else:
                failed.append(
                    '%s (no credit/journal account configured)'
                    % (expense.x_ref or expense.id)
                )
        except Exception as e:
            failed.append('%s: %s' % (expense.x_ref or expense.id, e))
            _logger.warning(
                'fix_petty_cash_expense_accounts: could not create JE for %s: %s',
                expense.x_ref or expense.id, e,
            )
    if created:
        _logger.info(
            'fix_petty_cash_expense_accounts: created %d missing journal entry/entries',
            created,
        )
    if failed:
        _logger.warning(
            'fix_petty_cash_expense_accounts: %d expense(s) still have no JE after fix attempt: %s',
            len(failed),
            ', '.join(failed[:20]),
        )

    # ── Step 3: correct JEs where credit account ≠ expected petty cash account ─
    # Covers all ~257 MCH petty cash JEs that were posted via the "Cash at HO"
    # journal (wrong credit account 112631) instead of the site-specific account
    # (e.g. 112634 Cash at MCH Bahawalnagar).
    #
    # Strategy: direct SQL update on account_move_line.account_id.
    # This is correct for a migration fix because:
    #   1. account_type is NOT a stored column in Odoo 19 — only account_id
    #      needs updating.
    #   2. Preserves original JE numbers (no reversal entries created).
    #      257 JEs → 257 corrected in-place, zero extra accounting entries.
    #   3. Petty cash (asset_cash) accounts are not reconcilable, so no
    #      reconciliation entries are broken.
    #   4. We run in the post_migrate context as sudo so no lock-period issue.
    posted_with_je = PCE.search([
        ('state', '=', 'posted'),
        ('x_account_move_id', '!=', False),
        ('x_petty_cash_account_id', '!=', False),
    ])
    wrong_count = 0
    fixed_count = 0
    unfixed_step3 = []
    cr = env.cr

    for expense in posted_with_je:
        move = expense.x_account_move_id
        if not move or move.state != 'posted':
            continue
        expected_acct = expense.x_petty_cash_account_id

        # Fast check via SQL: does the move have a credit line with the wrong account?
        cr.execute("""
            SELECT id, account_id
            FROM account_move_line
            WHERE move_id = %s
              AND credit > 0
              AND account_id != %s
            LIMIT 1
        """, (move.id, expected_acct.id))
        wrong_row = cr.fetchone()
        if not wrong_row:
            continue  # Credit already correct — skip

        wrong_acct_id = wrong_row[1]
        # Safety: skip if reconciled (shouldn't happen for cash accounts)
        cr.execute("""
            SELECT 1 FROM account_move_line
            WHERE move_id = %s AND credit > 0 AND reconciled = true LIMIT 1
        """, (move.id,))
        if cr.fetchone():
            unfixed_step3.append('%s (reconciled credit lines — manual fix required)'
                                 % (expense.x_ref or expense.id))
            _logger.warning(
                'fix_petty_cash_expense_accounts: expense %s — JE %s credit lines are '
                'reconciled; skipping auto-fix. Correct this JE manually.',
                expense.x_ref or expense.id, move.name,
            )
            continue

        wrong_count += 1
        _logger.info(
            'fix_petty_cash_expense_accounts: expense %s — JE %s: '
            'credit account_id %s → %s (%s)',
            expense.x_ref or expense.id,
            move.name,
            wrong_acct_id,
            expected_acct.id,
            expected_acct.code or expected_acct.name,
        )

        try:
            # Direct in-place correction — no reversal entries created
            cr.execute("""
                UPDATE account_move_line
                SET account_id = %s
                WHERE move_id = %s
                  AND credit > 0
                  AND account_id != %s
            """, (expected_acct.id, move.id, expected_acct.id))
            fixed_count += 1
        except Exception as e:
            unfixed_step3.append('%s: %s' % (expense.x_ref or expense.id, e))
            _logger.warning(
                'fix_petty_cash_expense_accounts: SQL update failed for expense %s: %s',
                expense.x_ref or expense.id, e,
            )

    # Invalidate ORM cache so recomputed fields (balance, etc.) reflect the SQL changes
    if fixed_count:
        env['account.move'].invalidate_model()
        env['account.move.line'].invalidate_model()
        _logger.info(
            'fix_petty_cash_expense_accounts: corrected credit account on %d JE(s) '
            'in-place (no reversal entries created)',
            fixed_count,
        )
    if wrong_count and wrong_count != fixed_count:
        _logger.info(
            'fix_petty_cash_expense_accounts: found %d JE(s) with wrong credit account — '
            'fixed %d, could not fix %d',
            wrong_count, fixed_count, len(unfixed_step3),
        )
    if unfixed_step3:
        _logger.warning(
            'fix_petty_cash_expense_accounts: %d JE(s) could not be auto-corrected '
            '(manual fix required): %s',
            len(unfixed_step3),
            ', '.join(unfixed_step3[:20]),
        )


def setup_main_accounts_and_bank_children(env):
    """Mark Main accounts (x_is_main=True), create 3 child accounts per bank,
    and wire the full x_parent_account_id hierarchy for the Matracon 9-digit CoA.

    Main accounts are group/hierarchy nodes — posting to them is blocked by the
    _check_no_posting_to_main_account constraint on account.move.line.
    The only exception is when x_allow_posting_to_main=True.

    Bank accounts get 3 fixed children:
      {code}1  → {name} - Useable Balance
      {code}2  → {name} - Cash Margin
      {code}3  → {name} - PO

    Step 4 switches every bank journal whose default_account is a Main account
    to the Useable Balance child so postings land on the right leaf account.

    Idempotent — safe to run on every module upgrade.
    """
    import logging
    _logger = logging.getLogger(__name__)

    Account = env['account.account'].sudo()
    company = env.company

    # ── All codes marked as Main in the chart of accounts ──────────────────
    MAIN_CODES = [
        '200000000',  # ASSETS
        '220000000',  # Fixed Assets
        '222000000',  # ACCUMULATED DEPRECIATION
        '230000000',  # CURRENT ASSETS
        '231100000',  # Banks (group)
        # Individual bank accounts — become group accounts after children are created
        '231100100', '231100200', '231100300', '231100400', '231100500',
        '231100600', '231100700', '231100800', '231100900', '231101000',
        '231101100', '231101200', '231101300', '231101400', '231101500',
        '231101600', '231101700', '231101800', '231101900', '231102000',
        '231102100', '231102200', '231102300', '231102400', '231102500',
        '231102600', '231102700', '231102800', '231102900', '231103000',
        '231103100', '231103200', '231103300', '231103400', '231103500',
        '231103600', '231103700', '231103800',
        # Cash & current assets
        '231200000',  # Cash & Cash Equivalents
        '230200000',  # Account Receivables
        '230210000',  # Receivable From Employer
        '230500000',  # Suspense Account
        '230600000',  # Advances To Employee's
        '230700000',  # Prepaid Expenses
        '230100000',  # Advance Tax
        # Liabilities
        '400000000',  # Liabilities
        '430000000',  # Current Liabilities
        '430200000',  # Payable To Sub-Contractor's
        '430500000',  # Taxes Payable (group)
        # 430510000 WHT Payable — NOT main; transactions post directly here
        # 430520000 Sales Tax Payable — NOT main; transactions post directly here
        '430600000',  # Accrued Expenses
        '430610000',  # Accrued Salaries
        '420000000',  # Non-Current Liabilities
        '420100000',  # Payable To Client/Employer
        '420200000',  # Inter-Project Transaction
        # Equity
        '100000000',  # Equity
        '100200000',  # Directors Loans
        # Revenue
        '600000000',  # Revenue
        '610000000',  # OTHER INCOME
        # Direct Project Cost
        '700000000',  # Direct Project Cost
        '700500000',  # Rental Expenses (Site)
        '701100000',  # Utilities Expenses
        '701600000',  # Rent Of HTV / LTV/ Equipment
        '701800000',  # Repairs And Maintenance
        '702100000',  # Salaries And Allowances
        # General & Admin
        '800000000',  # General Office & Admin Exp
        '801000000',  # HO Staff Cost
        '802000000',  # Office Expenses
        '803000000',  # Utilities Expenses
        '804000000',  # Legal & Professional Fee
        '805000000',  # Travelling Expenses (HO)
        '806000000',  # Rental Expenses (HO)
        '801400000',  # Financial Charges
        '801500000',  # MAQ sb Personal Expenses
    ]

    # ── Bank codes that each need 3 fixed children ──────────────────────────
    BANK_CODES = [
        '231100100', '231100200', '231100300', '231100400', '231100500',
        '231100600', '231100700', '231100800', '231100900', '231101000',
        '231101100', '231101200', '231101300', '231101400', '231101500',
        '231101600', '231101700', '231101800', '231101900', '231102000',
        '231102100', '231102200', '231102300', '231102400', '231102500',
        '231102600', '231102700', '231102800', '231102900', '231103000',
        '231103100', '231103200', '231103300', '231103400', '231103500',
        '231103600', '231103700', '231103800',
    ]

    CHILD_SUFFIXES = [
        ('1', 'Useable Balance'),
        ('2', 'Cash Margin'),
        ('3', 'PO'),
    ]

    # ── Step 1: Mark all main accounts ─────────────────────────────────────
    accounts = Account.search([
        ('code', 'in', MAIN_CODES),
        ('company_ids', 'in', [company.id]),
    ])
    newly_marked = 0
    for acc in accounts:
        if not acc.x_is_main:
            acc.x_is_main = True
            newly_marked += 1
    _logger.info(
        'setup_main_accounts: marked %d account(s) as Main (x_is_main=True)',
        newly_marked,
    )

    # ── Step 1b: Un-mark posting accounts that must NOT be Main ─────────────
    # WHT Payable (430510000) and Sales Tax Payable (430520000) are leaf
    # posting accounts — transactions post directly here (e.g. WHT deductions).
    # They must NOT have x_is_main=True or the posting constraint will block
    # every payment/bill that includes WHT/sales-tax deductions.
    POSTING_ONLY = ['430510000', '430520000']
    posting_accs = Account.search([
        ('code', 'in', POSTING_ONLY),
        ('company_ids', 'in', [company.id]),
        ('x_is_main', '=', True),
    ])
    if posting_accs:
        posting_accs.sudo().write({'x_is_main': False})
        _logger.info(
            'setup_main_accounts: cleared x_is_main on %d posting-only account(s): %s',
            len(posting_accs),
            ', '.join(posting_accs.mapped('code')),
        )

    # ── Step 2: Create 3 child accounts per bank ────────────────────────────
    bank_accounts = Account.search([
        ('code', 'in', BANK_CODES),
        ('company_ids', 'in', [company.id]),
    ])
    created = 0
    for bank in bank_accounts:
        for suffix, label in CHILD_SUFFIXES:
            child_code = bank.code + suffix
            if Account.search([
                ('code', '=', child_code),
                ('company_ids', 'in', [company.id]),
            ], limit=1):
                continue  # Already exists — idempotent
            Account.create({
                'name': '%s - %s' % (bank.name, label),
                'code': child_code,
                'account_type': bank.account_type,
                'reconcile': bank.reconcile,
                'company_ids': [(4, company.id)],
                'x_parent_account_id': bank.id,
            })
            created += 1
    if created:
        _logger.info(
            'setup_main_accounts: created %d bank child account(s) '
            '(Useable Balance / Cash Margin / PO)',
            created,
        )

    # ── Step 3: Set x_parent_account_id for the full CoA hierarchy ──────────
    PARENT_MAP = {
        # ── ASSETS (200000000) ──────────────────────────────────────────────
        '220000000': '200000000',   # Fixed Assets → ASSETS
        '222000000': '200000000',   # Accumulated Depreciation → ASSETS
        '230000000': '200000000',   # Current Assets → ASSETS
        # Fixed Assets children
        '221100000': '220000000',   # Vehicles
        '221200000': '220000000',   # Plant & Machinery
        '221300000': '220000000',   # Computers
        '221400000': '220000000',   # Tools & Equipments
        '221500000': '220000000',   # Scaffolding Pipes
        '221600000': '220000000',   # Furniture & Fixture
        '221700000': '220000000',   # Office Equipments
        '222800000': '220000000',   # Electrical Equipments
        # Accumulated Depreciation children
        '222100000': '222000000',   # Plants & Machinery (dep.)
        '222200000': '222000000',   # Computers (dep.)
        '222300000': '222000000',   # Tools & Equipment (dep.)
        '222400000': '222000000',   # Scaffolding Pipes (dep.)
        '222500000': '222000000',   # Furniture & Fixture (dep.)
        '222600000': '222000000',   # Office Equipments (dep.)
        '222700000': '222000000',   # Electrical Equipments (dep.)
        '223000000': '222000000',   # Intangible Assets
        # Current Assets children
        '231100000': '230000000',   # Banks → Current Assets
        '231200000': '230000000',   # Cash & Cash Equivalents
        '230200000': '230000000',   # Account Receivables
        '230210000': '230200000',   # Receivable From Employer → Account Receivables
        '230500000': '230000000',   # Suspense Account
        '230600000': '230000000',   # Advances To Employee's
        '230700000': '230000000',   # Prepaid Expenses
        '230100000': '230000000',   # Advance Tax
        # Individual bank accounts → Banks (231100000)
        '231100100': '231100000', '231100200': '231100000', '231100300': '231100000',
        '231100400': '231100000', '231100500': '231100000', '231100600': '231100000',
        '231100700': '231100000', '231100800': '231100000', '231100900': '231100000',
        '231101000': '231100000', '231101100': '231100000', '231101200': '231100000',
        '231101300': '231100000', '231101400': '231100000', '231101500': '231100000',
        '231101600': '231100000', '231101700': '231100000', '231101800': '231100000',
        '231101900': '231100000', '231102000': '231100000', '231102100': '231100000',
        '231102200': '231100000', '231102300': '231100000', '231102400': '231100000',
        '231102500': '231100000', '231102600': '231100000', '231102700': '231100000',
        '231102800': '231100000', '231102900': '231100000', '231103000': '231100000',
        '231103100': '231100000', '231103200': '231100000', '231103300': '231100000',
        '231103400': '231100000', '231103500': '231100000', '231103600': '231100000',
        '231103700': '231100000', '231103800': '231100000',
        # ── LIABILITIES (400000000) ─────────────────────────────────────────
        '430000000': '400000000',   # Current Liabilities → Liabilities
        '420000000': '400000000',   # Non-Current Liabilities → Liabilities
        # Current Liabilities children
        '430200000': '430000000',   # Payable To Sub-Contractor's
        '430500000': '430000000',   # Taxes Payable
        '430510000': '430500000',   # WHT Payable → Taxes Payable
        '430520000': '430500000',   # Sales Tax Payable → Taxes Payable
        '430600000': '430000000',   # Accrued Expenses
        '430610000': '430600000',   # Accrued Salaries → Accrued Expenses
        # Non-Current Liabilities children
        '420100000': '420000000',   # Payable To Client/Employer
        '420200000': '420000000',   # Inter-Project Transaction
        # ── EQUITY (100000000) ──────────────────────────────────────────────
        '100200000': '100000000',   # Directors Loans → Equity
        # ── DIRECT PROJECT COST (700000000) ─────────────────────────────────
        '700500000': '700000000',   # Rental Expenses (Site)
        '701100000': '700000000',   # Utilities Expenses
        '701600000': '700000000',   # Rent Of HTV / LTV / Equipment
        '701800000': '700000000',   # Repairs And Maintenance
        '702100000': '700000000',   # Salaries And Allowances
        # ── GENERAL OFFICE & ADMIN (800000000) ──────────────────────────────
        '801000000': '800000000',   # HO Staff Cost
        '802000000': '800000000',   # Office Expenses
        '803000000': '800000000',   # Utilities Expenses (HO)
        '804000000': '800000000',   # Legal & Professional Fee
        '805000000': '800000000',   # Travelling Expenses (HO)
        '806000000': '800000000',   # Rental Expenses (HO)
        '801400000': '800000000',   # Financial Charges
        '801500000': '800000000',   # MAQ sb Personal Expenses
    }

    all_accounts = Account.search([('company_ids', 'in', [company.id])])
    code_to_acc = {a.code: a for a in all_accounts}

    parent_linked = 0
    for child_code, parent_code in PARENT_MAP.items():
        child_acc = code_to_acc.get(child_code)
        parent_acc = code_to_acc.get(parent_code)
        if not child_acc or not parent_acc:
            continue
        if child_acc.x_parent_account_id.id == parent_acc.id:
            continue
        child_acc.x_parent_account_id = parent_acc.id
        parent_linked += 1
    _logger.info(
        'setup_main_accounts: linked x_parent_account_id on %d account(s)',
        parent_linked,
    )

    # ── Step 4: Switch bank journals to Useable Balance child account ────────
    Journal = env['account.journal'].sudo()
    bank_journals = Journal.search([
        ('type', '=', 'bank'),
        ('company_id', '=', company.id),
    ])
    journals_updated = 0
    for journal in bank_journals:
        current_acc = journal.default_account_id
        if not current_acc or not current_acc.x_is_main:
            continue
        useable_code = current_acc.code + '1'
        child_acc = code_to_acc.get(useable_code)
        if not child_acc:
            _logger.warning(
                'setup_main_accounts: journal "%s" — Useable Balance child (%s) '
                'not found, skipping', journal.name, useable_code,
            )
            continue
        journal.default_account_id = child_acc.id
        journals_updated += 1
        _logger.info(
            'setup_main_accounts: journal "%s" → account %s → %s',
            journal.name, current_acc.code, child_acc.code,
        )
    if journals_updated:
        _logger.info(
            'setup_main_accounts: updated %d bank journal(s) to Useable Balance account',
            journals_updated,
        )


def setup_account_groups(env):
    """Build account.group records that mirror the x_parent_account_id hierarchy.

    Every account that acts as a parent (has at least one child pointing to it
    via x_parent_account_id) gets one account.group record:

        prefix = account.code.rstrip('0')

    Examples from the Matracon 9-digit CoA:
        230000000 Current Assets  → prefix '23'      → captures all 23xxxxxxx
        231100000 Banks           → prefix '2311'    → captures all 2311xxxxx
        231100100 AlBaraka-4012   → prefix '2311001' → captures all 2311001xx

    The parent_id chain on account.group mirrors x_parent_account_id exactly so
    the Trial Balance report tree reflects the live CoA structure.

    Root prefixes (single character '1','2','4','6','7','8') are SKIPPED to
    prevent short-code legacy accounts from being mis-assigned.

    Idempotent: creates missing groups, updates names/parents on existing ones.
    Safe on fresh dev DBs with no x_parent_account_id data (no-op).
    """
    import logging
    _logger = logging.getLogger(__name__)

    Group = env['account.group'].sudo()
    Account = env['account.account'].sudo()
    company = env.company

    # ── 1. Find all parent accounts ─────────────────────────────────────────
    env.cr.execute("""
        SELECT DISTINCT x_parent_account_id
        FROM account_account
        WHERE x_parent_account_id IS NOT NULL
    """)
    parent_ids = [r[0] for r in env.cr.fetchall()]

    if not parent_ids:
        _logger.info(
            'setup_account_groups: no x_parent_account_id relationships found — '
            'skipping (no 9-digit CoA imported yet)'
        )
        return

    parent_accounts = Account.browse(parent_ids)

    # ── 2. Compute prefix (strip trailing zeros) ─────────────────────────────
    def get_prefix(code):
        if not code:
            return ''
        stripped = code.rstrip('0')
        return stripped if stripped else code[:1]

    valid_parents = [
        a for a in parent_accounts
        if a.code and len(get_prefix(a.code)) >= 2
    ]

    if not valid_parents:
        _logger.info('setup_account_groups: no qualifying parent accounts found')
        return

    # Shortest prefix first — ensures parent groups exist before children
    valid_parents.sort(key=lambda a: len(get_prefix(a.code)))

    # ── 3. Get-or-create helper ──────────────────────────────────────────────
    def get_or_create_group(name, prefix):
        # 1. Exact prefix match
        grp = Group.search([
            ('code_prefix_start', '=', prefix),
            ('code_prefix_end', '=', prefix),
            ('company_id', '=', company.id),
        ], limit=1)
        # 2. Name match
        if not grp:
            grp = Group.search([
                ('name', '=', name),
                ('company_id', '=', company.id),
            ], limit=1)
        # 3. Overlap check (bypass ORM constraint via SQL)
        if not grp:
            env.cr.execute("""
                SELECT id FROM account_group
                WHERE company_id = %s
                  AND char_length(code_prefix_start) = %s
                  AND code_prefix_start <= %s
                  AND code_prefix_end   >= %s
                LIMIT 1
            """, (company.id, len(prefix), prefix, prefix))
            row = env.cr.fetchone()
            if row:
                grp = Group.browse(row[0])
                _logger.warning(
                    'setup_account_groups: "%s" (%s) overlaps existing group id=%d '
                    '"%s" (%s–%s); reusing it.',
                    name, prefix, grp.id, grp.name,
                    grp.code_prefix_start, grp.code_prefix_end,
                )
        if grp:
            if grp.name != name:
                grp.write({'name': name})
                _logger.info('setup_account_groups: renamed → "%s" (%s)', name, prefix)
            return grp
        # 4. Create fresh
        grp = Group.create({
            'name': name,
            'code_prefix_start': prefix,
            'code_prefix_end': prefix,
            'company_id': company.id,
        })
        _logger.info('setup_account_groups: created "%s" (prefix=%s)', name, prefix)
        return grp

    # ── 4. First pass: create / update groups ───────────────────────────────
    account_to_group = {}
    for acct in valid_parents:
        prefix = get_prefix(acct.code)
        grp = get_or_create_group(acct.name, prefix)
        account_to_group[acct.id] = grp

    # ── 5. Second pass: wire parent_id to mirror x_parent_account_id ────────
    for acct in valid_parents:
        if acct.id not in account_to_group:
            continue
        grp = account_to_group[acct.id]
        desired_parent = False
        p = acct.x_parent_account_id
        while p:
            if p.id in account_to_group:
                desired_parent = account_to_group[p.id]
                break
            p = p.x_parent_account_id
        current_parent_id = grp.parent_id.id or False
        desired_parent_id = desired_parent.id if desired_parent else False
        if current_parent_id != desired_parent_id:
            grp.write({'parent_id': desired_parent_id})

    _logger.info(
        'setup_account_groups: synced %d account groups for company "%s"',
        len(valid_parents), company.name,
    )


def fix_account_move_rules(env):
    """
    Restrict Odoo's built-in 'see all' record rules on account.move /
    account.move.line so that site accountants cannot bypass project-scoped
    restrictions.

    The problem
    ───────────
    group_site_accountant implies account.group_account_user, which in turn
    implies group_account_basic → group_account_invoice AND group_account_readonly.

    Odoo ships three group-based rules on account.move that grant [(1,'=',1)]:
      • account.account_move_see_all           → group_account_invoice
      • account.account_move_rule_group_invoice → group_account_invoice  (Odoo 19)
      • account.account_move_rule_group_readonly → group_account_readonly

    Because Odoo ORs group-based rules, site accountants inherit these permissive
    rules and see every journal entry regardless of the project-scoped rule.

    The fix
    ───────
    Replace the groups on those rules with account.group_account_manager only.
    Finance HO and Matracon Admin both imply group_account_manager, so they
    keep full visibility.  Site accountants only have group_account_user (not
    group_account_manager) so they fall through to rule_account_move_site_accountant
    (own project only).

    These rules are shipped with noupdate=True so they cannot be overridden via
    XML.  Python is the only reliable way to modify them.
    """
    import logging
    _logger = logging.getLogger(__name__)

    # Rules to restrict (account.move and account.move.line variants)
    RULE_XML_IDS = [
        'account.account_move_see_all',
        'account.account_move_line_see_all',
        'account.account_move_rule_group_invoice',
        'account.account_move_line_rule_group_invoice',
        'account.account_move_rule_group_readonly',
        'account.account_move_line_rule_group_readonly',
    ]
    manager_group = env.ref('account.group_account_manager', raise_if_not_found=False)
    if not manager_group:
        _logger.warning('fix_account_move_rules: account.group_account_manager not found — skipping')
        return

    for xml_id in RULE_XML_IDS:
        rule = env.ref(xml_id, raise_if_not_found=False)
        if not rule:
            continue  # Some rules may not exist in all Odoo versions
        current_groups = rule.groups
        if current_groups == manager_group:
            continue  # Already correct — idempotent
        rule.sudo().write({'groups': [(6, 0, [manager_group.id])]})
        old_names = ', '.join(current_groups.mapped('name')) or '(none)'
        _logger.info(
            'fix_account_move_rules: %s → groups changed from [%s] to [Administrator]',
            xml_id, old_names,
        )


def generate_cheque_leaves_for_existing_series(env):
    """Generate x.cheque.leaf records for all existing series that have none.

    Safe to re-run — skips numbers that already have a leaf record.
    Called from post_migrate_hook so existing compliance series are populated
    automatically on the first upgrade after this feature is deployed.
    """
    import logging
    _log = logging.getLogger(__name__)
    series_all = env['x.cheque.series'].search([])
    for series in series_all:
        if series.leaf_ids:
            # Already has leaves — call the idempotent sync helper
            series.generate_leaves_for_existing()
        else:
            series._generate_leaves()
        _log.info('cheque_leaves: generated leaves for series "%s" (bank: %s)',
                  series.name, series.bank_journal_id.name)


def post_migrate_hook(env):
    set_pkr_decimal_places(env)
    set_analytic_percentage_precision(env)
    set_date_format(env)
    set_pakistan_fiscal_year(env)
    deduplicate_partner_tags(env)
    try:
        generate_cheque_leaves_for_existing_series(env)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            'post_migrate_hook: generate_cheque_leaves failed: %s', e)
    reprocess_existing_payments(env)
    # Restrict Odoo's built-in 'see all' account.move rules to group_account_manager
    # so site accountants are properly scoped to their own project.
    fix_account_move_rules(env)
    try:
        migrate_petty_cash_expense_lines(env)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            'post_migrate_hook: migrate_petty_cash_expense_lines failed: %s', e)
    try:
        fix_petty_cash_expense_accounts(env)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            'post_migrate_hook: fix_petty_cash_expense_accounts failed: %s', e)
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
    # Remove legacy menu_petty_cash_requests (replaced by menu_petty_cash_requests_ceo).
    # The old record may still exist in staging DBs that were installed before the
    # 778a46b collapse commit. Unlink it here so the CEO never sees a duplicate.
    try:
        old_menu = env.ref('site_operations.menu_petty_cash_requests', raise_if_not_found=False)
        if old_menu:
            old_menu.sudo().unlink()
    except Exception:
        pass
    # Hide the enterprise Vendors > Batch Payments menu (account_batch_payment module).
    # Matracon uses its own x.batch.payment model; the enterprise menu is redundant
    # and appears above Liability Sheets which is confusing.
    try:
        bp_menu = env.ref('account_batch_payment.menu_batch_payment_purchases',
                          raise_if_not_found=False)
        if bp_menu and bp_menu.active:
            bp_menu.sudo().write({'active': False})
    except Exception:
        pass
    # Backfill cheque numbers from payments → journal entries (and → journal lines).
    try:
        backfill_payment_cheque_numbers(env)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            'post_migrate_hook: backfill_payment_cheque_numbers failed: %s', e)
    # Mark Main accounts, create bank children, and wire parent-child hierarchy.
    try:
        setup_main_accounts_and_bank_children(env)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            'post_migrate_hook: setup_main_accounts_and_bank_children failed: %s', e)
    # Build account.group records from the x_parent_account_id tree.
    try:
        setup_account_groups(env)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            'post_migrate_hook: setup_account_groups failed: %s', e)


def backfill_payment_cheque_numbers(env):
    """Propagate x_cheque_number from posted account.payment → account.move → account.move.line.

    For payments posted before _matracon_propagate_cheque_to_move() was added, the
    cheque number lived only on account.payment and was never written to the linked
    account.move.  This one-time SQL pass fills the gap so the Chart of Accounts
    drill-down shows the correct cheque / reference numbers.

    The UPDATE on account_move_line is needed because x_cheque_number there is a
    store=True related field — Odoo's ORM recompute is not triggered by a raw SQL
    UPDATE on the parent, so we patch both tables directly.
    """
    import logging
    _log = logging.getLogger(__name__)

    env.cr.execute("""
        UPDATE account_move am
           SET x_cheque_number = ap.x_cheque_number
          FROM account_payment ap
         WHERE ap.move_id = am.id
           AND ap.x_cheque_number IS NOT NULL
           AND ap.x_cheque_number != ''
           AND (am.x_cheque_number IS NULL OR am.x_cheque_number = '')
    """)
    move_rows = env.cr.rowcount
    _log.info('backfill_payment_cheque_numbers: updated %d account_move rows', move_rows)

    env.cr.execute("""
        UPDATE account_move_line aml
           SET x_cheque_number = am.x_cheque_number
          FROM account_move am
         WHERE aml.move_id = am.id
           AND am.x_cheque_number IS NOT NULL
           AND am.x_cheque_number != ''
           AND (aml.x_cheque_number IS NULL OR aml.x_cheque_number = '')
    """)
    line_rows = env.cr.rowcount
    _log.info('backfill_payment_cheque_numbers: updated %d account_move_line rows', line_rows)


def ensure_deduction_accounts(env):
    """Ensure WHT Payable (252100) and Retention Payable (211200) accounts
    exist and are linked to their XML IDs. Safe on any DB state."""
    import logging
    _logger = logging.getLogger(__name__)
    Account = env['account.account'].sudo()
    IMD = env['ir.model.data'].sudo()

    accounts_to_ensure = [
        ('account_wht_payable', '252100', 'WHT Payable', 'liability_current'),
        ('account_retention_payable', '211200', 'Retention Payable', 'liability_payable'),
    ]

    for xml_id, code, name, account_type in accounts_to_ensure:
        existing_ref = env.ref(f'site_operations.{xml_id}', raise_if_not_found=False)
        if existing_ref:
            continue
        account = Account.search([('code', '=', code)], limit=1)
        if not account:
            account = Account.create({
                'code': code,
                'name': name,
                'account_type': account_type,
            })
            _logger.info('ensure_deduction_accounts: created %s (%s)', name, code)
        IMD.search([
            ('module', '=', 'site_operations'),
            ('name', '=', xml_id),
        ]).unlink()
        IMD.create({
            'name': xml_id,
            'module': 'site_operations',
            'model': 'account.account',
            'res_id': account.id,
            'noupdate': True,
        })
        _logger.info('ensure_deduction_accounts: linked %s -> account #%s', xml_id, account.id)


def cleanup_stale_views(env):
    """Deactivate known orphaned/stale views left in the database from earlier
    Studio customizations or removed features. Safe to run multiple times."""
    import logging
    _logger = logging.getLogger(__name__)
    View = env['ir.ui.view'].sudo()

    stale_view_names = [
        'account.move.vendor.bill.backcharge.section',
    ]

    # Note: arch_db reset for x_parent_account_id views is no longer needed —
    # those fields are restored and the views are up to date.

    for name in stale_view_names:
        views = View.search([('name', '=', name), ('active', '=', True)])
        if views:
            views.write({'active': False})
            _logger.info('cleanup_stale_views: deactivated %s (ids: %s)', name, views.ids)



def pre_init_hook(env):
    """Runs BEFORE any module data/views are loaded — cleans up known stale
    Studio-created database records that would otherwise crash view
    validation during module install/update on any fresh database snapshot
    (since post_init_hook runs too late for view-loading-time crashes).
    Uses raw SQL only — registry may not be fully ready at this stage."""
    cr = env.cr
    # Deactivate stale Studio view referencing a removed action
    cr.execute("""
        UPDATE ir_ui_view SET active = false
        WHERE name = 'account.move.vendor.bill.backcharge.section'
          AND active = true
    """)
    cr.execute("""
        DELETE FROM ir_model_data
        WHERE model = 'ir.ui.view'
          AND res_id IN (
              SELECT id FROM ir_ui_view
              WHERE name = 'account.move.vendor.bill.backcharge.section'
          )
    """)
    # Note: arch_db reset for x_parent_account_id views removed — fields are restored.

