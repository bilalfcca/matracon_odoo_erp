"""
Direct Print Controller
=======================
Serves any supported QWeb report as an HTML page and injects a
``window.print()`` call so the browser's print dialog opens automatically
the moment the page loads.

Supported models (mapped to their ``ir.actions.report`` XML IDs):

    x.bank.guarantee        → Bank Guarantee
    x.petty.cash.request    → Petty Cash Request
    x.petty.cash.expense    → Petty Cash Expense Voucher
    account.payment         → Bank Payment Voucher
    purchase.order          → PR Internal

The user still clicks "Print" in the browser dialog — fully silent
background printing is impossible in a web browser for security reasons.
"""
import logging

from odoo import http
from odoo.http import request, Response

_logger = logging.getLogger(__name__)

# model_name → ir.actions.report XML ID
_REPORT_MAP = {
    'x.bank.guarantee':      'site_operations.action_report_bank_guarantee',
    'x.petty.cash.request':  'site_operations.action_report_petty_cash_request',
    'x.petty.cash.expense':  'site_operations.action_report_petty_cash_expense',
    'account.payment':       'site_operations.action_report_bank_payment_voucher',
    'purchase.order':        'purchase_demand_raise.action_report_pr_internal',
}

# Auto-print script injected before </body>
_PRINT_SCRIPT = (
    b'<script>'
    b'window.addEventListener("load",function(){'
    b'  setTimeout(function(){window.print();},350);'
    b'});'
    b'</script>'
)


class DirectPrintController(http.Controller):

    @http.route(
        '/site_operations/print/<string:model_name>/<string:record_ids>',
        type='http', auth='user', website=False, csrf=False,
    )
    def direct_print(self, model_name, record_ids, **kwargs):
        """
        Render a QWeb report as HTML and auto-trigger the browser print dialog.

        URL: /site_operations/print/<model>/<comma-separated-ids>
        Example: /site_operations/print/account.payment/42
        """
        if model_name not in _REPORT_MAP:
            return Response(
                f'Direct print is not configured for model "{model_name}".',
                status=404, content_type='text/plain',
            )

        try:
            ids = [int(i) for i in record_ids.split(',') if i.strip().isdigit()]
        except (ValueError, AttributeError):
            return Response('Invalid record IDs.', status=400, content_type='text/plain')

        if not ids:
            return Response('No valid record IDs provided.', status=400,
                            content_type='text/plain')

        report_xml_id = _REPORT_MAP[model_name]

        try:
            html_content, _ = request.env['ir.actions.report']._render_qweb_html(
                report_xml_id, ids,
            )
        except Exception as e:
            _logger.error(
                'Direct print render failed — model=%s ids=%s report=%s: %s',
                model_name, ids, report_xml_id, e,
            )
            return Response(
                f'Failed to render report: {e}', status=500,
                content_type='text/plain',
            )

        # Inject auto-print script just before </body>
        if b'</body>' in html_content:
            html_content = html_content.replace(b'</body>', _PRINT_SCRIPT + b'</body>', 1)
        else:
            html_content = html_content + _PRINT_SCRIPT

        return Response(html_content, content_type='text/html;charset=utf-8')
