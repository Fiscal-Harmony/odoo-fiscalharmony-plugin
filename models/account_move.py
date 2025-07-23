# zimra_fiscal/models/account_move.py
# -*- coding: utf-8 -*-
from odoo import models, fields, api
import json
import requests
import logging
from datetime import datetime

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

    def action_fiscalize_manual(self):
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
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Fiscalization Successful',
                    'message': f'Invoice {self.name} has been successfully fiscalized',
                    'type': 'success',
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

    def _send_to_zimra(self):
        """Send invoice to ZIMRA"""
        self.ensure_one()

        # Get configuration
        config = self.env['zimra.config'].search([
            ('company_id', '=', self.company_id.id),
            ('active', '=', True)
        ], limit=1)

        if not config:
            self.zimra_status = 'failed'
            self.zimra_error = 'No active ZIMRA configuration found'
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

            # Send to ZIMRA
            headers = {
                'Authorization': f'Bearer {config.api_key}',
                'Content-Type': 'application/json'
            }

            invoice_url = f"{config.api_url}/invoices" if config.api_url.endswith('/') else f"{config.api_url}/invoices"

            response = requests.post(
                invoice_url,
                json=invoice_data,
                headers=headers,
                timeout=config.timeout
            )

            # Update fields
            self.zimra_sent_date = fields.Datetime.now()
            self.zimra_response = response.text
            self.zimra_retry_count += 1

            # Update invoice log
            zimra_invoice.write({
                'status': 'sent',
                'sent_date': self.zimra_sent_date,
                'response_data': response.text,
            })

            if response.status_code == 200:
                response_data = response.json()

                self.zimra_status = 'fiscalized'
                self.zimra_fiscal_number = response_data.get('fiscal_number', response_data.get('receiptNumber'))
                self.zimra_fiscalized_date = fields.Datetime.now()
                self.zimra_qr_code = response_data.get('qr_code')
                self.zimra_verification_url = response_data.get('verification_url')

                # Update invoice log
                zimra_invoice.write({
                    'status': 'fiscalized',
                    'zimra_fiscal_number': self.zimra_fiscal_number,
                    'fiscalized_date': self.zimra_fiscalized_date,
                })

                _logger.info(
                    f"Successfully fiscalized invoice {self.name} - Fiscal Number: {self.zimra_fiscal_number}")
                return True

            else:
                self.zimra_status = 'failed'
                self.zimra_error = f"HTTP {response.status_code}: {response.text}"

                # Update invoice log
                zimra_invoice.write({
                    'status': 'failed',
                    'error_message': self.zimra_error,
                })

                _logger.error(f"Failed to fiscalize invoice {self.name}: {response.text}")
                return False

        except requests.exceptions.Timeout:
            error_msg = f"Timeout after {config.timeout} seconds"
            self.zimra_status = 'failed'
            self.zimra_error = error_msg
            _logger.error(f"Timeout fiscalizing invoice {self.name}: {error_msg}")
            return False

        except Exception as e:
            error_msg = str(e)
            self.zimra_status = 'failed'
            self.zimra_error = error_msg
            _logger.error(f"Error fiscalizing invoice {self.name}: {error_msg}")
            return False

    def _should_fiscalize(self):
        """Check if invoice should be fiscalized"""
        # Only fiscalize customer invoices
        if not self.is_invoice(include_receipts=True):
            return False

        # Skip if amount is zero or negative
        if self.amount_total <= 0:
            return False

        # Skip if already fiscalized
        if self.zimra_status in ['fiscalized', 'exempted']:
            return False

        # Skip if invoice is not posted
        if self.state != 'posted':
            return False

        # Skip draft invoices
        if self.payment_state == 'draft':
            return False

        # Skip credit notes (refunds) - handle separately if needed
        if self.move_type == 'out_refund':
            return False

        # Only customer invoices
        if self.move_type != 'out_invoice':
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
        invoice_datetime = self.invoice_date or fields.Date.today()
        timestamp = self.__create_timestamp(invoice_datetime)

        # Prepare main invoice data in ZIMRA format
        data = {
            "InvoiceId": self.name,
            "InvoiceNumber": self.name,
            "Reference": self.ref or "",
            "IsDiscounted": has_discount,
            "IsTaxInclusive": True,
            "BuyerContact": buyer_contact,
            "Date": timestamp,
            "LineItems": line_items,
            "SubTotal": round(self.amount_untaxed, 2),
            "TotalTax": round(self.amount_tax, 2),
            "Total": round(self.amount_total, 2),
            "CurrencyCode": currency_code,
            "IsRetry": bool(self.zimra_retry_count > 0),
        }

        return data

    def __get_buyer_contact(self):
        """Get buyer contact information"""
        if not self.partner_id:
            return {
                "Name": "Walk-in Customer",
                "TIN": "",
                "Address": "",
                "Phone": "",
                "Email": ""
            }

        return {
            "Name": self.partner_id.name,
            "TIN": self.partner_id.vat or "",
            "Address": self._get_customer_address(),
            "Phone": self.partner_id.phone or "",
            "Email": self.partner_id.email or ""
        }

    def __get_line_items(self, tax_mappings):
        """Get line items in ZIMRA format"""
        line_items = []

        for line in self.invoice_line_ids:
            # Skip lines that are not product lines
            if line.display_type in ['line_section', 'line_note']:
                continue

            # Calculate tax information
            tax_amount = 0
            tax_code = ""
            tax_rate = 0

            if line.tax_ids:
                for tax in line.tax_ids:
                    if tax.id in tax_mappings:
                        tax_mapping = tax_mappings[tax.id]
                        tax_amount += (line.price_subtotal * tax_mapping.zimra_tax_rate / 100)
                        tax_code = tax_mapping.zimra_tax_code
                        tax_rate = tax_mapping.zimra_tax_rate

            # Calculate discount amount
            discount_amount = 0
            if line.discount:
                discount_amount = line.price_unit * line.quantity * line.discount / 100

            line_item = {
                "ItemCode": line.product_id.default_code or str(line.product_id.id) if line.product_id else "SERVICE",
                "Description": line.name or (line.product_id.name if line.product_id else "Service"),
                "Quantity": line.quantity,
                "UnitPrice": round(line.price_unit, 2),
                "Discount": round(discount_amount, 2),
                "SubTotal": round(line.price_subtotal, 2),
                "TaxCode": tax_code,
                "TaxRate": tax_rate,
                "TaxAmount": round(tax_amount, 2),
                "Total": round(line.price_subtotal + tax_amount, 2)
            }

            line_items.append(line_item)

        return line_items

    def __create_timestamp(self, date_field):
        """Create timestamp from date field"""
        if isinstance(date_field, str):
            return date_field

        if hasattr(date_field, 'strftime'):
            return date_field.strftime('%Y-%m-%d %H:%M:%S')

        return str(date_field)

    def _get_customer_address(self):
        """Get customer address"""
        if not self.partner_id:
            return ''

        address_parts = []
        for field in ['street', 'street2', 'city', 'zip']:
            value = getattr(self.partner_id, field, '')
            if value:
                address_parts.append(value)

        if self.partner_id.state_id:
            address_parts.append(self.partner_id.state_id.name)
        if self.partner_id.country_id:
            address_parts.append(self.partner_id.country_id.name)

        return ', '.join(address_parts)

    def action_post(self):
        """Override action_post to auto-fiscalize when invoice is posted"""
        result = super(AccountMove, self).action_post()

        # Auto-fiscalize customer invoices
        for move in self:
            if move.is_invoice() and move.move_type == 'out_invoice':
                config = self.env['zimra.config'].search([
                    ('company_id', '=', move.company_id.id),
                    ('active', '=', True),
                    ('auto_fiscalize_invoices', '=', True)  # New config field
                ], limit=1)

                if config and move.zimra_status == 'pending':
                    # Use queue job if available, otherwise direct call
                    if hasattr(move, 'with_delay'):
                        move.with_delay()._send_to_zimra()
                    else:
                        move._send_to_zimra()

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
                })

        return result

    @api.model
    def create(self, vals):
        """Override create to set initial ZIMRA status"""
        move = super(AccountMove, self).create(vals)

        # Set initial status for customer invoices
        if move.is_invoice() and move.move_type == 'out_invoice':
            move.zimra_status = 'pending'
        else:
            move.zimra_status = 'exempted'

        return move

    def write(self, vals):
        """Override write to handle state changes"""
        result = super(AccountMove, self).write(vals)

        # Handle payment state changes
        if 'payment_state' in vals:
            for move in self:
                if (move.is_invoice() and move.move_type == 'out_invoice' and
                        move.payment_state == 'paid' and move.zimra_status == 'pending'):

                    config = self.env['zimra.config'].search([
                        ('company_id', '=', move.company_id.id),
                        ('active', '=', True),
                        ('auto_fiscalize_on_payment', '=', True)  # New config field
                    ], limit=1)

                    if config:
                        if hasattr(move, 'with_delay'):
                            move.with_delay()._send_to_zimra()
                        else:
                            move._send_to_zimra()

        return result