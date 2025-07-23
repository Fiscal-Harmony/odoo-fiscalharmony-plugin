# zimra_fiscal/models/zimra_tax_mapping.py
# -*- coding: utf-8 -*-
from odoo import models, fields, api
from odoo.exceptions import ValidationError


class ZimraTaxMapping(models.Model):
    _name = 'zimra.tax.mapping'
    _description = 'ZIMRA Tax Mapping'
    _rec_name = 'display_name'

    config_id = fields.Many2one('zimra.config', 'Configuration', required=True, ondelete='cascade')
    odoo_tax_id = fields.Many2one('account.tax', 'Odoo Tax', required=True)
    zimra_tax_code = fields.Char('ZIMRA Tax Code', required=True)
    zimra_tax_name = fields.Char('ZIMRA Tax Name', required=True)
    zimra_tax_rate = fields.Float('ZIMRA Tax Rate (%)', required=True)
    zimra_tax_type = fields.Selection([
        ('Exempt', 'Exempt'),
        ('Standard rated 15%', 'Standard rated 15%'),
        ('Zero rated 0%', 'Zero rated 0%'),
        ('Non-VAT Withholding Tax', 'Non-VAT Withholding Tax')
    ], string='Tax Type', required=True, default='')
    odoo_tax_id = fields.Many2one('account.tax', 'Odoo Tax')

    # Additional fields from device response
    tax_description = fields.Text('Tax Description')
    is_active = fields.Boolean('Active', default=True)

    display_name = fields.Char('Display Name', compute='_compute_display_name', store=True)

    @api.depends('odoo_tax_id', 'zimra_tax_code')
    def _compute_display_name(self):
        for record in self:
            record.display_name = f"{record.odoo_tax_id.name} → {record.zimra_tax_code}"

    def save_line_taxmapping(self):
        for rec in self:
            if rec.config_id:
                rec.config_id.save_taxmapping(rec)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Tax Mapping Saved',
                'message': f'Tax {rec.zimra_tax_type} saved successfully.',
                'type': 'success',
                'sticky': False,
            }
        }

    @api.constrains('zimra_tax_rate')
    def _check_tax_rate(self):
        for record in self:
            if record.zimra_tax_rate < 0 or record.zimra_tax_rate > 100:
                raise ValidationError('Tax rate must be between 0 and 100%')

    @api.constrains('config_id', 'odoo_tax_id')
    def _check_unique_tax_mapping(self):
        for record in self:
            existing = self.search([
                ('config_id', '=', record.config_id.id),
                ('odoo_tax_id', '=', record.odoo_tax_id.id),
                ('id', '!=', record.id)
            ])
            if existing:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': 'Taxes Already Synced',
                        'message': f'Taxes Already Synced for this device',
                        'type': 'success',
                        'sticky': False,
                    }
                }

    def name_get(self):
        result = []
        for record in self:
            name = f"{record.odoo_tax_id.name} → {record.zimra_tax_code} ({record.zimra_tax_rate}%)"
            result.append((record.id, name))
        return result

    def write(self, vals):
        result = super().write(vals)
        if 'odoo_tax_id' in vals:
            for rec in self:
                if rec.odoo_tax_id and rec.config_id:
                    rec.config_id.save_taxmapping(rec)
        return result

    @api.model
    def create(self, vals):
        record = super().create(vals)
        if vals.get('odoo_tax_id') and record.config_id:
            record.config_id.save_taxmapping(record)
        return record

    @api.onchange('zimra_tax_type')
    def _onchange_zimra_tax_type(self):
        tax_lookup = {
            'Exempt': {'taxID': 1, 'taxName': 'Exempt', 'rate': 0.0, 'code': 1},
            'Zero rated 0%': {'taxID': 2, 'taxName': 'Zero rated 0%', 'rate': 0.0, 'code': 2},
            'Standard rated 15%': {'taxID': 3, 'taxName': 'Standard rated 15%', 'rate': 15.0, 'code': 3},
            'Non-VAT Withholding Tax': {'taxID': 514, 'taxName': 'Non-VAT Withholding Tax', 'rate': 10.0,
                                        'code': 514},
        }

        for rec in self:
            selected = tax_lookup.get(rec.zimra_tax_type)
            if selected:
                rec.zimra_tax_code = selected['code']
                rec.zimra_tax_name = selected['taxName']
                rec.zimra_tax_rate = selected['rate']
                rec.tax_description = f"Auto-filled: {selected['taxName']} ({selected['rate']}%)"


def save_line_taxmapping(self):
    for rec in self:
        if rec.config_id:
            rec.config_id.save_taxmapping(rec)
    return {
        'type': 'ir.actions.client',
        'tag': 'display_notification',
        'params': {
            'title': 'Tax Mapping Saved',
            'message': f'Tax {rec.zimra_tax_type} saved successfully.',
            'type': 'success',
            'sticky': False,
        }
    }
