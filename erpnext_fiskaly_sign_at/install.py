from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from erpnext_fiskaly_sign_at.access_control import setup_pos_access_control

RKSV_PRINT_FORMAT = "POS Invoice RKSV"
RKSV_CLOSING_PRINT_FORMAT = "Fiskaly Closing Receipt RKSV"
RKSV_OFFLINE_NOTICE = "Sicherheitseinrichtung ausgefallen"


def before_install():
	setup_pos_access_control()


def after_install():
	setup_pos_access_control()
	setup_custom_fields()
	setup_print_format()
	setup_pos_profile_print_formats()


def after_app_install(app_name):
	"""Finish navigation cleanup after Frappe auto-generates desktop icons."""

	if app_name == "erpnext_fiskaly_sign_at":
		setup_pos_access_control()


def before_migrate():
	setup_pos_access_control()


def after_migrate():
	setup_pos_access_control()
	setup_custom_fields()
	setup_print_format()
	setup_pos_profile_print_formats()


def setup_custom_fields():
	"""Install the immutable POS print snapshots and explicit RKSV payment classification.

	The fiscalization service owns the values on POS Invoice. Keeping them on the
	submitted invoice makes later prints deterministic and avoids reconstructing a
	fiscal receipt from mutable master data or live provider calls.
	"""

	common = {"read_only": 1, "allow_on_submit": 1, "no_copy": 1, "print_hide": 1}
	create_custom_fields(
		{
			"POS Invoice": [
				{
					"fieldname": "fiskaly_tab",
					"label": "Fiskaly / RKSV",
					"fieldtype": "Tab Break",
					# ``title`` is the final ERPNext field in the More Info tab. Inserting
					# here keeps every core field in its original tab and moves only our
					# custom fields into the dedicated Fiskaly tab.
					"insert_after": "title",
				},
				{
					"fieldname": "fiskaly_section",
					"label": "Status und Zuordnung",
					"fieldtype": "Section Break",
					"insert_after": "fiskaly_tab",
					"collapsible": 0,
				},
				{
					"fieldname": "fiskaly_status_summary",
					"label": "Fiskaly-Statusübersicht",
					"fieldtype": "HTML",
					"insert_after": "fiskaly_section",
				},
				{
					**common,
					"fieldname": "fiskaly_receipt",
					"label": "Fiskaly-Beleg",
					"fieldtype": "Link",
					"options": "Fiskaly Receipt",
					"insert_after": "fiskaly_status_summary",
				},
				{
					**common,
					"fieldname": "fiskaly_status",
					"label": "Fiskalisierungsstatus",
					"fieldtype": "Data",
					"insert_after": "fiskaly_receipt",
				},
				{
					"fieldname": "fiskaly_assignment_column",
					"fieldtype": "Column Break",
					"insert_after": "fiskaly_status",
				},
				{
					**common,
					"fieldname": "fiskaly_provider",
					"label": "API-Generation",
					"fieldtype": "Data",
					"description": "Zum Signaturzeitpunkt verwendete fiskaly API-Generation.",
					"insert_after": "fiskaly_assignment_column",
				},
				{
					**common,
					"fieldname": "fiskaly_environment",
					"label": "Umgebung",
					"fieldtype": "Data",
					"description": "TEST oder LIVE zum Zeitpunkt der Fiskalisierung.",
					"insert_after": "fiskaly_provider",
				},
				{
					**common,
					"fieldname": "fiskaly_register",
					"label": "Registrierkasse",
					"fieldtype": "Link",
					"options": "Fiskaly Register",
					"insert_after": "fiskaly_environment",
				},
				{
					**common,
					"fieldname": "fiskaly_receipt_type",
					"label": "Provider-Belegtyp",
					"fieldtype": "Data",
					"insert_after": "fiskaly_register",
				},
				{
					**common,
					"fieldname": "fiskaly_receipt_kind",
					"label": "RKSV-Belegart",
					"fieldtype": "Data",
					"insert_after": "fiskaly_receipt_type",
				},
				{
					"fieldname": "fiskaly_bmf_section",
					"label": "BMF-/RKSV-Belegdaten",
					"fieldtype": "Section Break",
					"description": (
						"Unveränderliche Belegdaten für Nachweise und eigene Druckformate. "
						"Die Werte stammen aus dem signierten Fiskaly-Beleg."
					),
					"insert_after": "fiskaly_receipt_kind",
				},
				{
					**common,
					"fieldname": "fiskaly_company_name",
					"label": "Unternehmen (RKSV-Beleg-Snapshot)",
					"fieldtype": "Data",
					"description": "Unveränderliche Unternehmerbezeichnung für spätere Nachdrucke.",
					"insert_after": "fiskaly_bmf_section",
				},
				{
					**common,
					"fieldname": "fiskaly_company_address",
					"label": "Unternehmensanschrift (RKSV-Beleg-Snapshot)",
					"fieldtype": "Small Text",
					"description": "Unveränderliche Anschrift für spätere Nachdrucke und Notbelege.",
					"insert_after": "fiskaly_company_name",
				},
				{
					"fieldname": "fiskaly_bmf_column",
					"fieldtype": "Column Break",
					"insert_after": "fiskaly_company_address",
				},
				{
					**common,
					"fieldname": "fiskaly_cash_register_id",
					"label": "RKSV-Kassen-ID",
					"fieldtype": "Data",
					"description": (
						"Die im RKSV-Datensatz und QR-Code ausgewiesene Kassen-ID bzw. "
						"Provider-Seriennummer."
					),
					"insert_after": "fiskaly_bmf_column",
				},
				{
					**common,
					"fieldname": "fiskaly_receipt_number",
					"label": "Fiskaly-Belegnummer",
					"fieldtype": "Data",
					"insert_after": "fiskaly_cash_register_id",
				},
				{
					**common,
					"fieldname": "fiskaly_signed_at",
					"label": "Signaturzeitpunkt",
					"fieldtype": "Datetime",
					"insert_after": "fiskaly_receipt_number",
				},
				{
					**common,
					"fieldname": "fiskaly_cash_amount",
					"label": "Tatsächlicher RKSV-Barzahlungsbetrag",
					"fieldtype": "Currency",
					"options": "currency",
					"description": (
						"Unveränderlicher Snapshot des tatsächlich bar bzw. baräquivalent bezahlten "
						"Betrags; bei gemischter Zahlung nicht mit dem Rechnungsbetrag gleichsetzen."
					),
					"insert_after": "fiskaly_signed_at",
				},
				{
					"fieldname": "fiskaly_amounts_section",
					"label": "Gesetzliche RKSV-Betragsfelder",
					"fieldtype": "Section Break",
					"description": (
						"Fiskalisierter Bruttobetrag der Bar- bzw. Baräquivalentzahlung, "
						"aufgeteilt auf die fünf gesetzlichen RKSV-Betragsfelder."
					),
					"insert_after": "fiskaly_cash_amount",
				},
				{
					**common,
					"fieldname": "fiskaly_gross_standard",
					"label": "Normalsteuersatz (standard)",
					"fieldtype": "Currency",
					"options": "currency",
					"description": "Üblicherweise österreichischer Normalsteuersatz 20 %.",
					"insert_after": "fiskaly_amounts_section",
				},
				{
					**common,
					"fieldname": "fiskaly_gross_reduced_1",
					"label": "Ermäßigter Satz 1 (reduced1)",
					"fieldtype": "Currency",
					"options": "currency",
					"description": "Üblicherweise dem Steuersatz 10 % zugeordnet.",
					"insert_after": "fiskaly_gross_standard",
				},
				{
					**common,
					"fieldname": "fiskaly_gross_reduced_2",
					"label": "Ermäßigter Satz 2 (reduced2)",
					"fieldtype": "Currency",
					"options": "currency",
					"description": "Üblicherweise dem Steuersatz 13 % zugeordnet.",
					"insert_after": "fiskaly_gross_reduced_1",
				},
				{
					"fieldname": "fiskaly_amounts_column",
					"fieldtype": "Column Break",
					"insert_after": "fiskaly_gross_reduced_2",
				},
				{
					**common,
					"fieldname": "fiskaly_gross_zero",
					"label": "Nullsatz / steuerfrei (zero)",
					"fieldtype": "Currency",
					"options": "currency",
					"description": "Umsätze mit 0 % bzw. die entsprechend zugeordnete Steuerbefreiung.",
					"insert_after": "fiskaly_amounts_column",
				},
				{
					**common,
					"fieldname": "fiskaly_gross_special",
					"label": "Besonderer Satz (special)",
					"fieldtype": "Currency",
					"options": "currency",
					"description": "Besonderer Steuersatz, z. B. 19 % oder 4,9 % gemäß Zuordnung der Kasse.",
					"insert_after": "fiskaly_gross_zero",
				},
				{
					**common,
					"fieldname": "fiskaly_vat_breakdown",
					"label": "Detaillierte Steueraufteilung (JSON)",
					"fieldtype": "Small Text",
					"description": (
						"Druckformatfreundliche JSON-Liste mit Netto-, Steuer- und Bruttobetrag, "
						"Steuersatz, RKSV-Gruppe und einer allfälligen Steuerbefreiung."
					),
					"insert_after": "fiskaly_gross_special",
				},
				{
					"fieldname": "fiskaly_qr_section",
					"label": "RKSV-QR-Code und Signatur",
					"fieldtype": "Section Break",
					"insert_after": "fiskaly_vat_breakdown",
				},
				{
					"fieldname": "fiskaly_qr_preview",
					"label": "QR-Code-Vorschau",
					"fieldtype": "HTML",
					"insert_after": "fiskaly_qr_section",
				},
				{
					"fieldname": "fiskaly_qr_column",
					"fieldtype": "Column Break",
					"insert_after": "fiskaly_qr_preview",
				},
				{
					**common,
					"fieldname": "fiskaly_qr_format",
					"label": "RKSV-QR-Format",
					"fieldtype": "Data",
					"description": "Kennung des österreichischen maschinenlesbaren Belegformats, z. B. R1-AT3.",
					"insert_after": "fiskaly_qr_column",
				},
				{
					**common,
					"fieldname": "fiskaly_qr_code_data",
					"label": "RKSV-QR-Code-Daten",
					"fieldtype": "Long Text",
					"description": "Vollständiger, unveränderlicher Inhalt des signierten RKSV-QR-Codes.",
					"insert_after": "fiskaly_qr_format",
				},
				{
					**common,
					"fieldname": "fiskaly_offline_qr_data",
					"label": "Ausfall-QR-Daten (unveränderlicher Snapshot)",
					"fieldtype": "Long Text",
					"description": (
						"Bei API-/Kassenausfall (OFFLINE_PENDING) exakt der QR-Inhalt "
						"„Sicherheitseinrichtung ausgefallen“; dies ist ein Notbeleg und kein fiskaler "
						"RKSV-Code. Bei TSP-Ausfall (SUBSTITUTE_SIGNED) der vollständige, vom Provider "
						"gelieferte _R1-AT…-Ersatzsignaturcode."
					),
					"insert_after": "fiskaly_qr_code_data",
				},
				{
					**common,
					"fieldname": "fiskaly_encrypted_turnover_counter",
					"label": "Verschlüsselter Umsatzzähler",
					"fieldtype": "Long Text",
					"description": "Aus dem signierten RKSV-Datensatz; der Klartext-Umsatzzähler wird nicht gespeichert.",
					"insert_after": "fiskaly_offline_qr_data",
				},
				{
					**common,
					"fieldname": "fiskaly_certificate_serial_number",
					"label": "Zertifikatsseriennummer",
					"fieldtype": "Data",
					"description": "Seriennummer des für die Belegsignatur verwendeten Zertifikats.",
					"insert_after": "fiskaly_encrypted_turnover_counter",
				},
				{
					**common,
					"fieldname": "fiskaly_previous_receipt_signature",
					"label": "Verkettungswert des Vorbelegs",
					"fieldtype": "Long Text",
					"description": "Im RKSV-Datensatz gespeicherter Verkettungswert zum vorherigen Barumsatz.",
					"insert_after": "fiskaly_certificate_serial_number",
				},
				{
					**common,
					"fieldname": "fiskaly_signature_value",
					"label": "Signaturwert",
					"fieldtype": "Long Text",
					"insert_after": "fiskaly_previous_receipt_signature",
				},
				{
					"fieldname": "fiskaly_audit_section",
					"label": "Technische Details und Drucknachweis",
					"fieldtype": "Section Break",
					"collapsible": 1,
					"insert_after": "fiskaly_signature_value",
				},
				{
					**common,
					"fieldname": "fiskaly_receipt_uuid",
					"label": "Beleg-UUID",
					"fieldtype": "Data",
					"insert_after": "fiskaly_audit_section",
				},
				{
					**common,
					"fieldname": "fiskaly_provider_receipt_id",
					"label": "Provider-Beleg-ID",
					"fieldtype": "Data",
					"insert_after": "fiskaly_receipt_uuid",
				},
				{
					**common,
					"fieldname": "fiskaly_provider_register_id",
					"label": "Provider-Kassenressource",
					"fieldtype": "Data",
					"description": "Technische Ressourcen-ID der Registrierkasse beim Provider.",
					"insert_after": "fiskaly_provider_receipt_id",
				},
				{
					**common,
					"fieldname": "fiskaly_signature_creation_unit_id",
					"label": "Signaturerstellungseinheit (SCU)",
					"fieldtype": "Data",
					"description": "Technische ID der dem Beleg zugeordneten Signature Creation Unit.",
					"insert_after": "fiskaly_provider_register_id",
				},
				{
					**common,
					"fieldname": "fiskaly_serial_number",
					"label": "Provider-Kassenseriennummer",
					"fieldtype": "Data",
					"insert_after": "fiskaly_signature_creation_unit_id",
				},
				{
					"fieldname": "fiskaly_audit_column",
					"fieldtype": "Column Break",
					"insert_after": "fiskaly_serial_number",
				},
				{
					**common,
					"fieldname": "fiskaly_fon_validation_status",
					"label": "FinanzOnline-Prüfstatus",
					"fieldtype": "Data",
					"description": "Für normale Kassenbelege üblicherweise NOT_REQUIRED.",
					"insert_after": "fiskaly_audit_column",
				},
				{
					**common,
					"fieldname": "fiskaly_fon_validation_at",
					"label": "FinanzOnline geprüft am",
					"fieldtype": "Datetime",
					"insert_after": "fiskaly_fon_validation_status",
				},
				{
					**common,
					"fieldname": "fiskaly_provider_hints",
					"label": "Provider-Hinweise",
					"fieldtype": "Small Text",
					"description": (
						"Unveränderliche Druckhinweise des Providers. Ausfallhinweise werden auch bei "
						"Status SIGNED sichtbar am Beleg ausgegeben."
					),
					"insert_after": "fiskaly_fon_validation_at",
				},
				{
					**common,
					"fieldname": "fiskaly_first_printed_at",
					"label": "Erstmals gedruckt am",
					"fieldtype": "Datetime",
					"description": "Audit-Snapshot des ersten serverseitig ausgelösten RKSV-Drucks.",
					"insert_after": "fiskaly_provider_hints",
				},
				{
					**common,
					"fieldname": "fiskaly_print_count",
					"label": "RKSV-Druckanzahl",
					"fieldtype": "Int",
					"description": "Wird nur bei einem tatsächlich ausgelösten serverseitigen Druck erhöht.",
					"insert_after": "fiskaly_first_printed_at",
				},
				{
					**common,
					"fieldname": "fiskaly_print_lines",
					"label": "RKSV-Druckzeilen (Legacy)",
					"fieldtype": "Small Text",
					"description": "Kompatibilitätsfeld; das RKSV-Druckformat nutzt die strukturierten Snapshot-Felder.",
					"insert_after": "fiskaly_print_count",
				},
			],
			"POS Profile": [
				{
					"fieldname": "fiskaly_manual_cash_entry",
					"label": "Erhaltenen Barbetrag manuell eingeben",
					"fieldtype": "Check",
					"default": "0",
					"insert_after": "set_grand_total_to_default_mop",
					"description": (
						"Wenn aktiviert, bleibt eine ausgewählte Zahlungsart vom ERPNext-Typ „Cash“ "
						"zunächst bei 0,00. Der tatsächlich erhaltene Barbetrag wird anschließend über "
						"das POS-Nummernfeld eingegeben; ERPNext berechnet daraus das Rückgeld. "
						"Die automatische Zuordnung der Gesamtsumme zur Standard-Zahlungsart muss dafür "
						"deaktiviert sein. Kartenzahlungen bleiben unverändert."
					),
				},
			],
			"Mode of Payment": [
				{
					"fieldname": "fiskaly_rksv_payment_type",
					"label": "RKSV-Zahlungsart",
					"fieldtype": "Select",
					"options": "\nRKSV Cash Equivalent\nNon-Cash",
					"insert_after": "type",
					"description": (
						"<strong>Bestimmt, welcher Zahlungsanteil als RKSV-Barumsatz fiskalisiert wird.</strong>"
						"<br><br><strong>RKSV Cash Equivalent</strong><br>"
						"Vor Ort erhaltene Barzahlungen und Baräquivalente: Bargeld, Debit- oder "
						"Kreditkarte am POS, Barscheck sowie eingelöste eigene Gutscheine. Diese Beträge "
						"werden in den RKSV-Beleg aufgenommen."
						"<br><br><strong>Non-Cash</strong><br>"
						"Nicht unmittelbar am POS vereinnahmte Zahlungen: Banküberweisung, SEPA-Lastschrift, "
						"PayPal sowie Remote- oder Online-Kartenzahlung. Diese Beträge zählen nicht zum "
						"RKSV-Barumsatz."
						"<br><br><strong>Wichtig:</strong> Nur beim ERPNext-Typ <strong>Cash</strong> darf "
						"das Feld leer bleiben; dieser Typ wird automatisch als RKSV-Baräquivalent behandelt. "
						"Bei allen anderen Zahlungsarten ist eine ausdrückliche Auswahl erforderlich, "
						"andernfalls wird der Beleg blockiert."
					),
				},
			],
		},
		update=True,
	)
	frappe.clear_cache(doctype="POS Invoice")
	frappe.clear_cache(doctype="POS Profile")
	frappe.clear_cache(doctype="Mode of Payment")


PRINT_FORMAT_HTML = r"""
<style>
@page { size: 80mm auto; margin: 3mm; }
.rksv-receipt { color: #111; font-family: Arial, Helvetica, sans-serif; font-size: 9.5pt; line-height: 1.3; max-width: 74mm; margin: 0 auto; }
.rksv-receipt * { box-sizing: border-box; }
.rksv-receipt .center { text-align: center; }
.rksv-receipt .merchant-name { font-size: 13pt; font-weight: 700; margin-bottom: 1mm; }
.rksv-receipt .merchant-address { font-size: 8.5pt; white-space: normal; }
.rksv-receipt .meta { border-bottom: 1px dashed #555; border-top: 1px dashed #555; margin: 3mm 0; padding: 2mm 0; }
.rksv-receipt table { border-collapse: collapse; table-layout: fixed; width: 100%; }
.rksv-receipt th, .rksv-receipt td { padding: 1.1mm 0; vertical-align: top; }
.rksv-receipt th { border-bottom: 1px solid #777; font-size: 8pt; text-align: left; }
.rksv-receipt .qty { padding-right: 2mm; width: 24mm; }
.rksv-receipt .amount { text-align: right; white-space: nowrap; }
.rksv-receipt .totals { border-top: 1px solid #555; margin-top: 2mm; padding-top: 1mm; }
.rksv-receipt .cash-total { border-bottom: 2px solid #111; border-top: 2px solid #111; font-size: 11pt; font-weight: 700; margin: 2mm 0; }
.rksv-receipt .section-title { border-bottom: 1px solid #777; font-size: 8pt; font-weight: 700; margin-top: 2mm; padding: 1mm 0; }
.rksv-receipt .tax-table td:first-child { padding-right: 2mm; }
.rksv-receipt .vat-snapshot { font-size: 8pt; }
.rksv-receipt .vat-snapshot th:not(:first-child), .rksv-receipt .vat-snapshot td:not(:first-child) { text-align: right; }
.rksv-receipt .qr-wrap { background: #fff; display: inline-block; margin: 3mm auto 1mm; padding: 2mm; }
.rksv-receipt .qr-wrap svg { display: block; height: auto; max-width: 46mm; overflow: visible; width: 46mm; }
.rksv-receipt .fiscal-details { border-top: 1px dashed #555; font-size: 8.5pt; margin-top: 2mm; padding-top: 2mm; word-break: break-word; }
.rksv-receipt .notice { border: 2px solid #111; font-weight: 700; margin: 3mm 0 1mm; padding: 2.5mm; text-align: center; }
.rksv-receipt .preview { background: #eee; border-style: double; font-size: 12pt; letter-spacing: .04em; }
.rksv-receipt .info { border: 1px solid #777; margin: 3mm 0 1mm; padding: 2.5mm; text-align: center; }
.rksv-receipt .hint { border: 1px solid #111; font-size: 8.5pt; margin-top: 2mm; padding: 2mm; white-space: pre-line; }
.rksv-receipt .missing { color: #900; font-weight: 700; }
@media print {
  .rksv-receipt { max-width: 74mm; width: 74mm; }
  .rksv-receipt .qr-wrap { break-inside: avoid; page-break-inside: avoid; }
}
</style>
<div class="rksv-receipt">
  {% set merchant_name = doc.fiskaly_company_name if doc.fiskaly_status != "NOT_REQUIRED" and doc.fiskaly_company_name else doc.company %}
  {% set merchant_address = doc.fiskaly_company_address if doc.fiskaly_status != "NOT_REQUIRED" and doc.fiskaly_company_address else doc.company_address_display %}
  {% if doc.fiskaly_is_preview %}
    <div class="notice preview">VORSCHAU &ndash; KEIN BELEG</div>
  {% elif doc.fiskaly_is_duplicate %}
    <div class="notice">DUPLIKAT</div>
  {% endif %}
  <header class="center">
    <div class="merchant-name">{{ merchant_name }}</div>
    {% if merchant_address %}
      <div class="merchant-address">{{ frappe.sanitize_html(merchant_address) | safe }}</div>
    {% else %}
      <div class="missing">Unternehmensadresse fehlt</div>
    {% endif %}
  </header>

  <div class="meta">
    <div><strong>ERP-Beleg-Nr.:</strong> {{ doc.name }}</div>
    <div><strong>Datum/Uhrzeit:</strong> {{ frappe.utils.format_datetime(doc.posting_date ~ " " ~ doc.posting_time, "dd.MM.yyyy HH:mm:ss") }}</div>
  </div>

  <table aria-label="Leistungen">
    <thead><tr><th class="qty">Menge / Einheit</th><th>Bezeichnung</th></tr></thead>
    <tbody>
    {% for item in doc.items %}
      <tr>
        <td class="qty">{{ item.qty }} {{ item.uom or item.stock_uom or "" }}</td>
        <td>{{ frappe.utils.strip_html(item.item_name or item.description or item.item_code) }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>

  <div class="totals">
    <table>
      <tr>
        <td>Rechnungsbetrag</td>
        <td class="amount">{{ frappe.utils.fmt_money(doc.grand_total, currency=doc.currency) }}</td>
      </tr>
      {% if doc.fiskaly_status != "NOT_REQUIRED" %}
        <tr class="cash-total">
          <td>Tatsächlicher Barzahlungsbetrag</td>
          <td class="amount">
            {% if doc.fiskaly_cash_amount is not none and doc.fiskaly_cash_amount != "" %}
              {{ frappe.utils.fmt_money(doc.fiskaly_cash_amount, currency=doc.currency) }}
            {% else %}<span class="missing">FEHLT</span>{% endif %}
          </td>
        </tr>
      {% endif %}
    </table>
  </div>

  <div class="section-title">Zahlungsarten</div>
  <table aria-label="Zahlungsarten und Beträge">
    <thead><tr><th>Zahlungsart</th><th class="amount">Betrag</th></tr></thead>
    <tbody>
    {% for payment in doc.payments or [] %}
      <tr>
        <td>{{ payment.mode_of_payment or payment.type or "Zahlungsart fehlt" }}</td>
        <td class="amount">{{ frappe.utils.fmt_money(payment.amount, currency=doc.currency) }}</td>
      </tr>
    {% else %}
      <tr><td colspan="2" class="missing">Zahlungsaufstellung fehlt im POS-Invoice-Snapshot</td></tr>
    {% endfor %}
    </tbody>
  </table>
  {% if doc.change_amount %}
    <table><tr><td>Rückgeld</td><td class="amount">{{ frappe.utils.fmt_money(doc.change_amount, currency=doc.currency) }}</td></tr></table>
  {% endif %}

  <div class="section-title">Rechnungs-/USt-Aufschlüsselung</div>
  {% if doc.item_wise_tax_details %}
    <table class="vat-snapshot" aria-label="Umsatzsteuer je Position und Steuersatz aus dem gebuchten POS-Invoice-Snapshot">
      <thead><tr><th>Satz</th><th>Netto</th><th>USt</th><th>Brutto</th></tr></thead>
      <tbody>
      {% for detail in doc.item_wise_tax_details %}
        <tr>
          <td>{{ detail.rate }} %</td>
          <td>{{ frappe.utils.fmt_money(detail.taxable_amount, currency=doc.currency) }}</td>
          <td>{{ frappe.utils.fmt_money(detail.amount, currency=doc.currency) }}</td>
          <td>
            {% if detail.taxable_amount is not none and detail.amount is not none %}
              {{ frappe.utils.fmt_money(detail.taxable_amount + detail.amount, currency=doc.currency) }}
            {% else %}<span class="missing">FEHLT</span>{% endif %}
          </td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  {% endif %}
  <table class="tax-table" aria-label="Rechnungs- und Umsatzsteueraufteilung aus dem gebuchten POS-Invoice-Snapshot">
    <thead><tr><th>Position</th><th class="amount">Betrag</th></tr></thead>
    <tbody>
      <tr>
        <td>Nettobetrag</td>
        <td class="amount">
          {% if doc.net_total is not none %}
            {{ frappe.utils.fmt_money(doc.net_total, currency=doc.currency) }}
          {% else %}<span class="missing">FEHLT</span>{% endif %}
        </td>
      </tr>
      {% for tax in doc.taxes or [] %}
        <tr>
          <td>
            {{ frappe.utils.strip_html(tax.description or tax.account_head or "Steuer/Abgabe") }}
            {% if tax.rate is not none %} ({{ tax.rate }} %){% endif %}
          </td>
          <td class="amount">
            {% if tax.tax_amount_after_discount_amount is not none %}
              {{ frappe.utils.fmt_money(tax.tax_amount_after_discount_amount, currency=doc.currency) }}
            {% elif tax.tax_amount is not none %}
              {{ frappe.utils.fmt_money(tax.tax_amount, currency=doc.currency) }}
            {% else %}<span class="missing">FEHLT</span>{% endif %}
          </td>
        </tr>
      {% else %}
        <tr><td colspan="2" class="missing">Steuer-/Abgabenaufschlüsselung fehlt im POS-Invoice-Snapshot</td></tr>
      {% endfor %}
      <tr>
        <td>Steuern/Abgaben/Zuschläge gesamt</td>
        <td class="amount">
          {% if doc.total_taxes_and_charges is not none %}
            {{ frappe.utils.fmt_money(doc.total_taxes_and_charges, currency=doc.currency) }}
          {% else %}<span class="missing">FEHLT</span>{% endif %}
        </td>
      </tr>
      <tr>
        <td><strong>Bruttobetrag</strong></td>
        <td class="amount"><strong>{{ frappe.utils.fmt_money(doc.grand_total, currency=doc.currency) }}</strong></td>
      </tr>
    </tbody>
  </table>

  {% if doc.fiskaly_status != "NOT_REQUIRED" %}
    {% set vat_rows = json.loads(doc.fiskaly_vat_breakdown or "[]") %}
    <div class="section-title">RKSV-Bar-Bucket-Aufschlüsselung</div>
    <table class="tax-table" aria-label="Barumsätze nach Steuersatz">
      <thead><tr><th>Fiskalisierter Bruttobetrag</th><th class="amount">Betrag</th></tr></thead>
      <tbody>
      {% for row in vat_rows %}
        <tr>
          <td>
            {% if row.label %}{{ row.label }}
            {% elif row.rate is not none %}Steuersatz {{ row.rate }} %
            {% else %}{{ row.code or "Steuergruppe" }}{% endif %}
          </td>
          <td class="amount">{{ frappe.utils.fmt_money(row.gross, currency=doc.currency) }}</td>
        </tr>
      {% else %}
        <tr><td colspan="2" class="missing">RKSV-Steueraufteilung fehlt</td></tr>
      {% endfor %}
      </tbody>
    </table>
  {% endif %}

  {% if doc.fiskaly_status == "NOT_REQUIRED" %}
    <div class="info">Keine RKSV-Baräquivalentzahlung &ndash; keine Fiskalisierung erforderlich</div>
  {% elif doc.fiskaly_status == "SIGNED" %}
    {% if doc.fiskaly_is_preview %}
      <div class="notice preview">RKSV-Code in der Vorschau unterdrückt</div>
    {% elif doc.fiskaly_qr_code_data %}
      <div class="center"><div class="qr-wrap">{{ fiskaly_qr_svg(doc.fiskaly_qr_code_data) }}</div></div>
    {% else %}
      <div class="notice missing">RKSV-QR-Code fehlt</div>
    {% endif %}
    <div class="fiscal-details">
      <div><strong>Kassen-ID:</strong> {{ doc.fiskaly_cash_register_id or doc.fiskaly_serial_number or "FEHLT" }}</div>
      {% if doc.fiskaly_receipt_number %}<div><strong>Fiskaly-Beleg-Nr.:</strong> {{ doc.fiskaly_receipt_number }}</div>{% endif %}
      {% if doc.fiskaly_signed_at %}<div><strong>Signaturzeit:</strong> {{ frappe.utils.format_datetime(doc.fiskaly_signed_at, "dd.MM.yyyy HH:mm:ss") }}</div>{% endif %}
    </div>
    {% if doc.fiskaly_provider_hints %}
      <div class="hint"><strong>Hinweis der Sicherheitseinrichtung:</strong><br>{{ doc.fiskaly_provider_hints }}</div>
    {% endif %}
  {% elif doc.fiskaly_status == "SUBSTITUTE_SIGNED" %}
    <div class="notice">{{ RKSV_OFFLINE_NOTICE }}<br><small>TSP-Ausfall · RKSV-Ersatzsignaturbeleg</small></div>
    {% if doc.fiskaly_is_preview %}
      <div class="notice preview">RKSV-Code in der Vorschau unterdrückt</div>
    {% elif doc.fiskaly_offline_qr_data %}
      <div class="center"><div class="qr-wrap">{{ fiskaly_qr_svg(doc.fiskaly_offline_qr_data) }}</div></div>
    {% else %}
      <div class="notice missing">Provider-Ersatzsignaturcode fehlt</div>
    {% endif %}
    <div class="fiscal-details">
      <div><strong>Kassen-ID:</strong> {{ doc.fiskaly_cash_register_id or doc.fiskaly_serial_number or "FEHLT" }}</div>
      {% if doc.fiskaly_receipt_number %}<div><strong>Fiskaly-Beleg-Nr.:</strong> {{ doc.fiskaly_receipt_number }}</div>{% endif %}
      {% if doc.fiskaly_signed_at %}<div><strong>Signaturzeit:</strong> {{ frappe.utils.format_datetime(doc.fiskaly_signed_at, "dd.MM.yyyy HH:mm:ss") }}</div>{% endif %}
    </div>
    {% if doc.fiskaly_provider_hints %}<div class="hint"><strong>Provider-Hinweis:</strong><br>{{ doc.fiskaly_provider_hints }}</div>{% endif %}
  {% elif doc.fiskaly_status == "OFFLINE_PENDING" %}
    <div class="notice">{{ RKSV_OFFLINE_NOTICE }}<br><small>API-/Kassenausfall · nicht fiskaler Notbeleg</small></div>
    {% if doc.fiskaly_is_preview %}
      <div class="notice preview">Ausfallhinweis-Code in der Vorschau unterdrückt</div>
    {% elif doc.fiskaly_offline_qr_data == RKSV_OFFLINE_NOTICE %}
      <div class="center"><div class="qr-wrap">{{ fiskaly_qr_svg(doc.fiskaly_offline_qr_data) }}</div></div>
    {% else %}
      <div class="notice missing">Unveränderlicher Ausfallhinweis-QR fehlt oder ist ungültig</div>
    {% endif %}
  {% else %}
    <div class="notice missing">Nicht druckbereit - Fiskalisierungsstatus: {{ doc.fiskaly_status or "FEHLT" }}</div>
  {% endif %}
</div>
""".replace("RKSV_OFFLINE_NOTICE", f'"{RKSV_OFFLINE_NOTICE}"')


SPECIAL_RECEIPT_HTML = r"""
<style>
@page { size: 80mm auto; margin: 3mm; }
.rksv-special { color: #111; font-family: Arial, Helvetica, sans-serif; font-size: 9.5pt; line-height: 1.3; max-width: 74mm; margin: auto; text-align: center; }
.rksv-special .qr { background: #fff; display: inline-block; margin: 3mm auto; padding: 2mm; }
.rksv-special .qr svg { display: block; height: auto; max-width: 46mm; overflow: visible; width: 46mm; }
.rksv-special .details { border-top: 1px dashed #555; margin-top: 2mm; padding-top: 2mm; text-align: left; }
.rksv-special .notice { border: 2px solid #111; font-weight: bold; margin-top: 3mm; padding: 2.5mm; }
</style>
<div class="rksv-special">
  {% set receipt_kind_labels = {
    "START": "Startbeleg",
    "MONTHLY": "Monatsbeleg",
    "YEARLY": "Jahresbeleg",
    "MANUAL_ZERO": "Nullbeleg",
    "RECOVERY": "Wiederanlaufbeleg",
    "CLOSING": "Schlussbeleg",
    "CONTROL": "Kontrollbeleg"
  } %}
  {% set fon_status_labels = {
    "PENDING": "Prüfung offen",
    "SUCCESS": "erfolgreich",
    "FAILED": "fehlgeschlagen",
    "ACTION_REQUIRED": "Maßnahme erforderlich"
  } %}
  <header>
    <div><strong>{{ doc.company or "Unternehmen fehlt" }}</strong></div>
    {% if doc.company_address_display %}
      <div>{{ frappe.sanitize_html(doc.company_address_display) | safe }}</div>
    {% else %}
      <div class="notice">Unternehmensanschrift fehlt</div>
    {% endif %}
  </header>
  <h2>{{ receipt_kind_labels.get(doc.receipt_kind, doc.receipt_kind) }} · RKSV</h2>
  {% if doc.fiscal_period %}<div>{{ doc.fiscal_period }}</div>{% endif %}
  {% if doc.status == "SUBSTITUTE_SIGNED" %}
    <div class="notice">Sicherheitseinrichtung ausgefallen · Provider-Ersatzsignatur</div>
  {% endif %}
  {% if doc.status in ("SIGNED", "SUBSTITUTE_SIGNED") and doc.qr_code_data %}
    <div class="qr">{{ fiskaly_qr_svg(doc.qr_code_data) }}</div>
    <div class="details">
      <div><strong>Barzahlungsbeträge:</strong> 20 %: 0,00 EUR · 10 %: 0,00 EUR · 13 %: 0,00 EUR · Besonders (19 % / 4,9 %): 0,00 EUR · Null: 0,00 EUR</div>
      {% if doc.receipt_number %}<div><strong>Belegnummer:</strong> {{ doc.receipt_number }}</div>{% endif %}
      {% if doc.signed_at %}<div><strong>Signaturzeit:</strong> {{ frappe.utils.format_datetime(doc.signed_at, "dd.MM.yyyy HH:mm:ss") }}</div>{% endif %}
      {% if doc.serial_number %}<div><strong>Kassen-ID:</strong> {{ doc.serial_number }}</div>{% endif %}
      {% if doc.fon_validation_status and doc.fon_validation_status != "NOT_REQUIRED" %}<div><strong>FinanzOnline-Prüfung:</strong> {{ fon_status_labels.get(doc.fon_validation_status, doc.fon_validation_status) }}</div>{% endif %}
      {% if doc.hints %}<div><strong>Provider-Hinweise:</strong><br>{{ doc.hints }}</div>{% endif %}
    </div>
  {% else %}
    <div class="notice">Nicht druckbereit · Status {{ doc.status }}</div>
  {% endif %}
</div>
"""


def setup_print_format():
	formats = {
		RKSV_PRINT_FORMAT: ("POS Invoice", PRINT_FORMAT_HTML),
		RKSV_CLOSING_PRINT_FORMAT: ("Fiskaly Receipt", SPECIAL_RECEIPT_HTML),
	}
	for name, (doc_type, html) in formats.items():
		values = {
			"doc_type": doc_type,
			"print_format_type": "Jinja",
			"custom_format": 1,
			"disabled": 0,
			"html": html,
		}
		if frappe.db.exists("Print Format", name):
			frappe.db.set_value("Print Format", name, values, update_modified=False)
		else:
			frappe.get_doc({"doctype": "Print Format", "name": name, **values}).insert(
				ignore_permissions=True
			)


def get_pos_profile_print_format_mismatches(
	pos_profile: str | None = None,
) -> list[dict[str, str | None]]:
	"""Return active register profiles that are not locked to the RKSV print format."""

	if not frappe.db.exists("DocType", "Fiskaly Register") or not frappe.db.exists("DocType", "POS Profile"):
		return []
	filters = {"active": 1, "pos_profile": ["is", "set"]}
	if pos_profile:
		filters["pos_profile"] = pos_profile
	profiles = set(
		frappe.get_all(
			"Fiskaly Register",
			filters=filters,
			pluck="pos_profile",
		)
	)
	return [
		{
			"pos_profile": profile,
			"print_format": frappe.db.get_value("POS Profile", profile, "print_format"),
		}
		for profile in sorted(profiles)
		if frappe.db.exists("POS Profile", profile)
		and frappe.db.get_value("POS Profile", profile, "print_format") != RKSV_PRINT_FORMAT
	]


def setup_pos_profile_print_formats(pos_profile: str | None = None) -> list[str]:
	"""Apply the compliant format to every POS Profile used by an active RKSV register."""

	updated = []
	for row in get_pos_profile_print_format_mismatches(pos_profile=pos_profile):
		frappe.db.set_value(
			"POS Profile",
			row["pos_profile"],
			"print_format",
			RKSV_PRINT_FORMAT,
			update_modified=False,
		)
		updated.append(row["pos_profile"])
	if updated:
		frappe.clear_cache(doctype="POS Profile")
	return updated
