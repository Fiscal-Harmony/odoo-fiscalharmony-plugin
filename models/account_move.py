# zimra_fiscal/models/account_move.py
# -*- coding: utf-8 -*-
from odoo import models, fields, api
import json
import requests
import logging
from datetime import datetime
import base64

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = 'account.move'

    # ZIMRA Status Fields
    zimra_status = fields.Selection([
        ('pending', 'Pending'),
        ('sent', 'Sent'),
        ('fiscalized', 'Fiscalized'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
        ('exempted', 'Exempted')
    ], string='ZIMRA Status', default='pending', tracking=True)

    zimra_fiscal_number = fields.Char('ZIMRA Fiscal Number', readonly=True, copy=False)
    zimra_response = fields.Text('ZIMRA Response', readonly=True, copy=False)
    zimra_error = fields.Text('ZIMRA Error', readonly=True, copy=False)
    zimra_sent_date = fields.Datetime('ZIMRA Sent Date', readonly=True, copy=False)
    zimra_fiscalized_date = fields.Datetime('ZIMRA Fiscalized Date', readonly=True, copy=False)
    zimra_retry_count = fields.Integer('Retry Count', default=0, copy=False)

    # Additional ZIMRA fields
    zimra_qr_code = fields.Char('ZIMRA QR Code', readonly=True, copy=False)
    zimra_verification_url = fields.Char('ZIMRA Verification URL', readonly=True, copy=False)
    fiscal_pdf_attachment_id = fields.Many2one('ir.attachment', 'Fiscal PDF', readonly=True, copy=False)

    # Add field to store PDF data like POS
    fiscalized_pdf = fields.Char('Fiscalized Pdf', readonly=True, copy=False)

    def action_fiscalize_invoice(self):
        """Manual fiscalization action for invoices"""
        self.ensure_one()

        if not self.is_invoice():
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Invalid Document',
                    'message': 'Only customer invoices can be fiscalized',
                    'type': 'warning',
                }
            }

        if self.zimra_status in ['fiscalized', 'sent']:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Already Fiscalized',
                    'message': 'This invoice has already been fiscalized',
                    'type': 'warning',
                }
            }

        result = self._send_to_zimra()

        if result:
            message = f'Invoice {self.name} has been successfully fiscalized'
            if self.fiscal_pdf_attachment_id:
                pdf_url = f'/web/content/{self.fiscal_pdf_attachment_id.id}?filename=FiscalInvoice_{self.name}.pdf'
                message += f'. <a href="{pdf_url}" target="_blank" class="btn btn-primary btn-sm">View PDF</a>'

            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Fiscalization Successful',
                    'message': message,
                    'type': 'success',
                    'sticky': bool(self.fiscal_pdf_attachment_id),
                }
            }
        else:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Fiscalization Failed',
                    'message': f'Failed to fiscalize invoice {self.name}. Check error details.',
                    'type': 'danger',
                }
            }

    def action_download_fiscal_pdf(self):
        """Download the fiscal PDF using zimra_config download method"""
        self.ensure_one()

        # Check if we have fiscal PDF data
        if not self.fiscalized_pdf:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'No PDF Available',
                    'message': 'No fiscal PDF is available for this invoice',
                    'type': 'warning',
                }
            }

        try:
            # Get configuration
            config = self.env['zimra.config'].search([
                ('company_id', '=', self.company_id.id),
                ('active', '=', True)
            ], limit=1)

            if not config:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': 'Configuration Error',
                        'message': 'No active ZIMRA configuration found',
                        'type': 'danger',
                    }
                }

            # Use the config's download_pdf method
            pdf_data = config.download_pdf(self.fiscalized_pdf)

            if isinstance(pdf_data, str):  # Success - PDF data returned
                # Create or update the PDF attachment
                attachment_vals = {
                    'name': f'FiscalInvoice_{self.name}.pdf',
                    'type': 'binary',
                    'datas': pdf_data,
                    'res_model': 'account.move',
                    'res_id': self.id,
                    'mimetype': 'application/pdf',
                }

                if self.fiscal_pdf_attachment_id:
                    # Update existing attachment
                    self.fiscal_pdf_attachment_id.write(attachment_vals)
                else:
                    # Create new attachment
                    attachment = self.env['ir.attachment'].create(attachment_vals)
                    self.fiscal_pdf_attachment_id = attachment.id

                # Return action to download the PDF
                return {
                    'type': 'ir.actions.act_window',
                    'res_model': 'account.move',
                    'res_id': self.id,
                    'view_mode': 'form',
                    'target': 'current',
                }

            else:  # Error - status code returned
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': 'Download Failed',
                        'message': f'Failed to download PDF. Server returned status code: {pdf_data}',
                        'type': 'danger',
                    }
                }

        except Exception as e:
            _logger.error(f"Error downloading fiscal PDF for invoice {self.name}: {str(e)}")
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Download Error',
                    'message': f'Error downloading PDF: {str(e)}',
                    'type': 'danger',
                }
            }
    def _send_to_zimra(self):
        """Send invoice to ZIMRA using the same approach as POS orders"""
        self.ensure_one()

        # Get configuration
        config = self.env['zimra.config'].search([
            ('company_id', '=', self.company_id.id),
            ('active', '=', True)
        ], limit=1)

        if not config:
            self.zimra_status = 'failed'
            self.zimra_error = 'No active FiscalHarmony configuration found'
            _logger.error(f"No ZIMRA configuration found for company {self.company_id.name}")
            return False

        # Check if invoice should be fiscalized
        if not self._should_fiscalize():
            self.zimra_status = 'exempted'
            return True

        try:
            # Prepare ZIMRA invoice data
            invoice_data = self._prepare_zimra_invoice_data(config)

            # Log the invoice
            zimra_invoice = self.env['zimra.invoice'].create({
                'name': self.name,
                'account_move_id': self.id,
                'status': 'pending',
                'request_data': json.dumps(invoice_data, indent=2),
                'company_id': self.company_id.id,
            })

            # Update fields before sending
            self.zimra_sent_date = fields.Datetime.now()
            self.zimra_retry_count += 1

            # Update invoice log
            zimra_invoice.write({
                'status': 'sent',
                'sent_date': self.zimra_sent_date,
            })

            fiscal_invoice = json.dumps(invoice_data, separators=(',', ':'), ensure_ascii=False)

            # Determine endpoint (same logic as POS)
            invoice_id = invoice_data.get("InvoiceId", "").strip().lower()

            # Check for CreditNoteId first
            if "CreditNoteId" in invoice_data and invoice_data["CreditNoteId"]:
                endpoint = "/creditnote"
            # Fallback: check if 'refund' is in the invoice ID
            elif "refund" in invoice_id:
                endpoint = "/creditnote"
            else:
                endpoint = "/invoice"

            response_data = config.send_fiscal_data(fiscal_invoice, endpoint)
            _logger.info("ZIMRA says:%s", response_data)

            # Store the response
            self.zimra_response = json.dumps(response_data) if response_data else ''

            # Update invoice log
            zimra_invoice.write({
                'response_data': self.zimra_response,
            })

            # Check if fiscalization was successful (same logic as POS)
            if self._is_fiscalization_successful(response_data):
                # response_data is a list, so get the first element
                response = response_data[0] if response_data else {}
                fiscalday = response.get("FiscalDay")
                invoice_number = response.get("InvoiceNumber")

                self.zimra_status = 'fiscalized'
                self.zimra_fiscal_number = f"{invoice_number}/{fiscalday}"
                self.zimra_fiscalized_date = fields.Datetime.now()
                self.zimra_qr_code = response.get('QrData')
                self.zimra_verification_url = response.get('verification_url')

                # Store PDF data like POS
                self.fiscalized_pdf = response.get('FiscalInvoicePdf')
                _logger.info("Fiscal pdf is %s", self.fiscalized_pdf)



                # Clear any previous errors
                self.zimra_error = False

                # Update invoice log
                zimra_invoice.write({
                    'status': 'fiscalized',
                    'zimra_fiscal_number': f"{invoice_number}/{fiscalday}",
                    'fiscalized_date': self.zimra_fiscalized_date,
                })

                _logger.info(
                    f"Successfully fiscalized invoice {self.name} - Fiscal Number: {self.zimra_fiscal_number}")

                return True

            else:
                # response_data is a list, so get the first element
                response = response_data[0] if response_data else {}

                self.zimra_status = 'failed'
                self.zimra_fiscal_number = response.get('fiscal_number', response.get('RequestId'))
                self.zimra_error = response.get('Error')

                # Update invoice log
                zimra_invoice.write({
                    'status': 'failed',
                    'error_message': self.zimra_error,
                    'zimra_fiscal_number': self.zimra_fiscal_number,
                })

                _logger.error(
                    f"Failed to fiscalize invoice {self.name} - Error: {self.zimra_error}")
                return False

        except Exception as e:
            error_msg = str(e)
            self.zimra_status = 'failed'
            self.zimra_error = error_msg

            # Update invoice log if it exists
            if 'zimra_invoice' in locals():
                zimra_invoice.write({
                    'status': 'failed',
                    'error_message': error_msg,
                })

            _logger.error(f"Error fiscalizing invoice {self.name}: {error_msg}")
            return False


    def _is_fiscalization_successful(self, response_data):
        """Check if fiscalization response indicates success based on 'Error' field."""
        if not response_data or not isinstance(response_data, list):
            return False

        response = response_data[0]
        return not response.get("Error")  # True if Error is None or ''

    def _should_fiscalize(self):
        """Check if invoice should be fiscalized"""
        # Only fiscalize customer invoices and credit notes
        if not self.is_invoice(include_receipts=True):
            return False

        # Skip if already fiscalized
        if self.zimra_status in ['fiscalized', 'exempted']:
            return False

        # Skip if invoice is not posted
        if self.state != 'posted':
            return False

        # Only customer invoices and credit notes
        if self.move_type not in ['out_invoice', 'out_refund']:
            return False

        return True

    def _prepare_zimra_invoice_data(self, config):
        """Prepare invoice data for ZIMRA format"""
        # Get tax and currency mappings
        tax_mappings = {tm.odoo_tax_id.id: tm for tm in config.tax_mapping_ids}
        currency_mappings = {cm.odoo_currency_id.id: cm for cm in config.currency_mapping_ids}

        # Get currency code
        currency_code = 'USD'  # Default
        if self.currency_id.id in currency_mappings:
            currency_code = currency_mappings[self.currency_id.id].zimra_currency_code

        # Prepare buyer contact
        buyer_contact = self.__get_buyer_contact()

        # Prepare line items
        line_items = self.__get_line_items(tax_mappings)

        # Check if invoice has any discounts
        has_discount = any(line.discount > 0 for line in self.invoice_line_ids)

        # Create timestamp from invoice date
        timestamp = self.__create_timestamp(self.invoice_date or fields.Date.today())

        # Determine if this is a credit note
        is_credit_note = self.move_type == 'out_refund'

        if is_credit_note:
            # Credit Note format
            data = {
                "CreditNoteId": self.name,
                "CreditNoteNumber": self.name,
                "OriginalInvoiceId": self.reversed_entry_id.name if self.reversed_entry_id else "",
                "Reference": self.ref or '',
                "IsTaxInclusive": True,
                "BuyerContact": buyer_contact,
                "Date": timestamp,
                "LineItems": line_items,
                "SubTotal": f"{abs(self.amount_untaxed):.2f}",
                "TotalTax": f"{abs(self.amount_tax):.2f}",
                "Total": f"{abs(self.amount_total):.2f}",
                "CurrencyCode": currency_code,
                "IsRetry": bool(self.zimra_retry_count > 0),
            }
        else:
            # Regular Invoice format
            data = {
                "InvoiceId": self.name,
                "InvoiceNumber": self.name,
                "Reference": self.ref or "",
                "IsDiscounted": has_discount,
                "IsTaxInclusive": True,
                "BuyerContact": buyer_contact,
                "Date": timestamp,
                "LineItems": line_items,
                "SubTotal": f"{self.amount_untaxed:.2f}",
                "TotalTax": f"{self.amount_tax:.2f}",
                "Total": f"{self.amount_total:.2f}",
                "CurrencyCode": currency_code,
                "IsRetry": bool(self.zimra_retry_count > 0),
            }

        _logger.info(f"Account Move {'Credit Note' if is_credit_note else 'Invoice'} data: %s", data)
        return data

    def __get_buyer_contact(self):
        """Get buyer contact information"""
        if not self.partner_id:
            return {}

        # Handle TIN/VAT parsing similar to POS
        if self.partner_id.company_registry:
            vat = self.partner_id.vat
            tin = self.partner_id.company_registry
        else:
            tin, vat = self._parse_vat_field(self.partner_id.vat)

        return {
            "Name": self.partner_id.name,
            "Tin": tin,
            "VatNumber": vat,
            "Address": self._get_customer_address(),
            "Phone": self.partner_id.phone or "",
            "Email": self.partner_id.email or ""
        }

    def _parse_vat_field(self, vat_string):
        """Parse VAT string to extract TIN and VAT numbers"""
        import re

        match_tin = re.search(r'TIN[:=]\s*(\d+)', vat_string or "")
        tin = match_tin.group(1) if match_tin else ''

        match_vat = re.search(r'VAT[:=]\s*(\d+)', vat_string or "")
        vat = match_vat.group(1) if match_vat else ''

        return tin, vat

    def __get_line_items(self, tax_mappings):
        """Get line items in ZIMRA format"""
        line_items = []

        for line in self.invoice_line_ids:
            # Skip lines that are not product lines
            if line.display_type in ['line_section', 'line_note']:
                continue

            # Calculate tax information using Odoo's tax computation
            tax_amount = 0
            tax_code = ""

            if line.tax_ids:
                # Use Odoo's tax computation
                tax_results = line.tax_ids.compute_all(
                    price_unit=line.price_unit,
                    quantity=line.quantity,
                    product=line.product_id,
                    partner=self.partner_id
                )

                tax_amount = tax_results['total_included'] - tax_results['total_excluded']

                # Get tax code from mapping
                for tax in line.tax_ids:
                    if tax.id in tax_mappings:
                        tax_mapping = tax_mappings[tax.id]
                        tax_code = tax_mapping.zimra_tax_code
                        break

            # Safely split product name into name and hscode
            if line.product_id:
                try:
                    name, hscode = line.product_id.name.rsplit(' ', 1)
                except ValueError:
                    name = line.product_id.name
                    hscode = ''
            else:
                name = line.name or "Service"
                hscode = ''

            # Calculate discount if applicable
            discount_amount = 0
            if line.discount:
                discount_amount = line.price_unit * line.quantity * line.discount / 100

            # For credit notes, use absolute values
            if self.move_type == 'out_refund':
                unit_amount = abs(
                    line.price_subtotal_incl if hasattr(line, 'price_subtotal_incl') else line.price_total)
                line_amount = abs(
                    line.price_subtotal_incl if hasattr(line, 'price_subtotal_incl') else line.price_total)
                quantity = abs(line.quantity)
                discount_amount = abs(discount_amount)
            else:
                unit_amount = line.price_subtotal_incl if hasattr(line, 'price_subtotal_incl') else line.price_total
                line_amount = line.price_subtotal_incl if hasattr(line, 'price_subtotal_incl') else line.price_total
                quantity = line.quantity

            # Build the line item
            line_item = {
                "Description": name,
                "UnitAmount": f"{abs(unit_amount / quantity):.3f}" if quantity != 0 else "0.000",
                "TaxCode": tax_code,
                "ProductCode": hscode,
                "LineAmount": f"{abs(line_amount):.2f}",
                "DiscountAmount": f"{abs(discount_amount):.2f}",
                "Quantity": f"{abs(quantity):.3f}",
            }

            line_items.append(line_item)

        return line_items

    def __create_timestamp(self, date_field):
        """Create timestamp in ISO format"""
        if not date_field:
            date_field = fields.Datetime.now()

        if isinstance(date_field, str):
            return date_field

        # Convert date to datetime if needed
        if hasattr(date_field, 'replace'):
            if hasattr(date_field, 'hour'):  # It's already a datetime
                return date_field.replace(microsecond=0).isoformat()
            else:  # It's a date, convert to datetime
                dt = datetime.combine(date_field, datetime.now().time())
                return dt.replace(microsecond=0).isoformat()

        return str(date_field)

    def _get_customer_address(self):
        """Get customer address as a structured dictionary"""
        if not self.partner_id:
            return {}

        return {
            "Province": self.partner_id.state_id.name if self.partner_id.state_id else '',
            "Street": self.partner_id.street2 or '',
            "HouseNo": self.partner_id.street or '',
            "City": self.partner_id.city or ''
        }

    def action_post(self):
        """Override action_post to auto-fiscalize when invoice is posted"""
        result = super(AccountMove, self).action_post()

        # Auto-fiscalize customer invoices and credit notes
        for move in self:
            if move._should_fiscalize():
                config = self.env['zimra.config'].search([
                    ('company_id', '=', move.company_id.id),
                    ('active', '=', True),
                    ('auto_fiscalize', '=', False)  # Use same field as POS
                ], limit=1)

                if config and move.zimra_status == 'pending':
                    fiscalize_result = move._send_to_zimra()
                    if not fiscalize_result:
                        _logger.error(f"Auto-fiscalization failed for invoice {move.name}")

        return result

    def button_cancel(self):
        """Override cancel to handle ZIMRA cancellation"""
        result = super(AccountMove, self).button_cancel()

        for move in self:
            if move.zimra_status == 'fiscalized':
                # You might want to send cancellation to ZIMRA here
                move.zimra_status = 'cancelled'
                _logger.info(f"Cancelled fiscalized invoice {move.name}")

        return result

    def button_draft(self):
        """Override draft to reset ZIMRA status"""
        result = super(AccountMove, self).button_draft()

        for move in self:
            if move.zimra_status in ['fiscalized', 'sent']:
                # Reset ZIMRA status when moving to draft
                move.write({
                    'zimra_status': 'pending',
                    'zimra_fiscal_number': False,
                    'zimra_response': False,
                    'zimra_error': False,
                    'zimra_sent_date': False,
                    'zimra_fiscalized_date': False,
                    'zimra_qr_code': False,
                    'zimra_verification_url': False,
                    'fiscal_pdf_attachment_id': False,
                    'fiscalized_pdf': False,
                })

        return result

    @api.model
    def create(self, vals):
        """Override create to set initial ZIMRA status and auto-fiscalize if posted"""
        move = super(AccountMove, self).create(vals)

        # Set initial status
        if move.is_invoice() and move.move_type in ['out_invoice', 'out_refund']:
            move.zimra_status = 'pending'

            # Auto-fiscalize if already posted and configuration allows
            if move.state == 'posted':
                config = self.env['zimra.config'].search([
                    ('company_id', '=', move.company_id.id),
                    ('active', '=', True),
                    ('auto_fiscalize', '=', False)
                ], limit=1)

                if config:
                    fiscalize_result = move._send_to_zimra()
                    if not fiscalize_result:
                        _logger.error(f"Auto-fiscalization failed for invoice {move.name}")
        else:
            move.zimra_status = 'exempted'

        return move

    def write(self, vals):
        """Override write to handle state changes"""
        result = super(AccountMove, self).write(vals)

        # Handle state changes to 'posted'
        if 'state' in vals and vals['state'] == 'posted':
            for move in self:
                if move._should_fiscalize() and move.zimra_status == 'pending':
                    config = self.env['zimra.config'].search([
                        ('company_id', '=', move.company_id.id),
                        ('active', '=', True),
                        ('auto_fiscalize', '=', False)
                    ], limit=1)

                    if config:
                        fiscalize_result = move._send_to_zimra()
                        if not fiscalize_result:
                            _logger.error(f"Auto-fiscalization failed for invoice {move.name}")

        # Handle payment state changes
        if 'payment_state' in vals and vals['payment_state'] == 'paid':
            for move in self:
                if move._should_fiscalize() and move.zimra_status == 'pending':
                    config = self.env['zimra.config'].search([
                        ('company_id', '=', move.company_id.id),
                        ('active', '=', True),
                        ('auto_fiscalize', '=', False)
                    ], limit=1)

                    if config:
                        fiscalize_result = move._send_to_zimra()
                        if not fiscalize_result:
                            _logger.error(f"Auto-fiscalization failed for invoice {move.name}")

        return result

    def action_retry_fiscalization(self):
        """Retry fiscalization for failed invoices"""
        self.ensure_one()
        if self.zimra_status != 'failed':
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Cannot Retry',
                    'message': 'Only failed invoices can be retried',
                    'type': 'warning',
                }
            }

        # Reset status to pending and retry
        self.zimra_status = 'pending'
        self.zimra_error = False

        return self.action_fiscalize_invoice()

    def action_view_zimra_logs(self):
        """View ZIMRA logs for this invoice"""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'ZIMRA Logs',
            'res_model': 'zimra.invoice',
            'view_mode': 'tree,form',
            'domain': [('account_move_id', '=', self.id)],
            'context': {'default_account_move_id': self.id}
        }

    @api.model
    def cron_retry_failed_fiscalization(self):
        """Cron job to retry failed fiscalization for invoices"""
        failed_invoices = self.search([
            ('zimra_status', '=', 'failed'),
            ('zimra_retry_count', '<', 3)  # Only retry up to 3 times
        ])

        for invoice in failed_invoices:
            try:
                invoice._send_to_zimra()
                _logger.info(f"Successfully retried fiscalization for invoice: {invoice.name}")
            except Exception as e:
                _logger.error(f"Failed to retry fiscalization for invoice {invoice.name}: {str(e)}")
