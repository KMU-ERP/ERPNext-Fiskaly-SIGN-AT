app_name = "erpnext_fiskaly_sign_at"
app_title = "Erpnext Fiskaly Sign At"
app_publisher = "RAN Soft GmbH & Co KG"
app_description = (
	"Integration of the Austrian cash register system (RKSV) into ERPNext using the fiskaly SIGN AT API"
)
app_email = "office@ransoft.at"
app_license = "mit"

required_apps = ["erpnext"]

add_to_apps_screen = [
	{
		"name": "erpnext_fiskaly_sign_at",
		"logo": "/assets/erpnext_fiskaly_sign_at/images/kmu_erp_logo.svg",
		"title": "Fiskaly RKSV",
		"route": "/desk/fiskaly-rksv",
		"has_permission": "erpnext_fiskaly_sign_at.permissions.has_app_permission",
	}
]

doctype_js = {
	"POS Invoice": "public/js/pos_invoice.js",
	"POS Profile": "public/js/pos_profile.js",
}

page_js = {
	"point-of-sale": "public/js/rksv_print.js",
	"print": "public/js/rksv_print.js",
}

doc_events = {
	"User": {
		"before_validate": "erpnext_fiskaly_sign_at.access_control.apply_pos_module_profile",
	},
	"POS Invoice": {
		"before_submit": "erpnext_fiskaly_sign_at.services.fiscalization.validate_pos_invoice",
		"on_submit": "erpnext_fiskaly_sign_at.services.fiscalization.create_outbox_receipt",
		"before_cancel": "erpnext_fiskaly_sign_at.services.fiscalization.prevent_signed_cancellation",
		"before_print": "erpnext_fiskaly_sign_at.services.compliance.validate_pos_print",
		"before_update_after_submit": "erpnext_fiskaly_sign_at.services.compliance.protect_pos_fiscal_snapshot_update",
	},
	"POS Profile": {
		"validate": "erpnext_fiskaly_sign_at.services.compliance.validate_pos_profile",
	},
	"Payment Entry": {
		"before_submit": "erpnext_fiskaly_sign_at.services.compliance.prevent_unfiscalized_cash_payment_entry",
	},
	"Sales Invoice": {
		"before_submit": "erpnext_fiskaly_sign_at.services.compliance.prevent_unfiscalized_sales_invoice",
	},
	"File": {
		"on_trash": "erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype.fiskaly_dep7_export.fiskaly_dep7_export.prevent_compliance_file_deletion",
	},
	"Print Format": {
		"validate": "erpnext_fiskaly_sign_at.services.compliance.protect_rksv_print_format",
		"on_trash": "erpnext_fiskaly_sign_at.services.compliance.prevent_rksv_print_format_deletion",
	},
}

boot_session = "erpnext_fiskaly_sign_at.access_control.configure_pos_boot"

permission_query_conditions = {
	"POS Invoice": "erpnext_fiskaly_sign_at.access_control.get_pos_user_permission_query",
	"POS Opening Entry": "erpnext_fiskaly_sign_at.access_control.get_pos_user_permission_query",
	"POS Closing Entry": "erpnext_fiskaly_sign_at.access_control.get_pos_user_permission_query",
}

has_permission = {
	"POS Invoice": "erpnext_fiskaly_sign_at.access_control.has_pos_user_permission",
	"POS Opening Entry": "erpnext_fiskaly_sign_at.access_control.has_pos_user_permission",
	"POS Closing Entry": "erpnext_fiskaly_sign_at.access_control.has_pos_user_permission",
}

scheduler_events = {
	"cron": {
		"*/5 * * * *": ["erpnext_fiskaly_sign_at.services.fiscalization.retry_pending_receipts"],
	},
	"hourly": [
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype.fiskaly_register.fiskaly_register.sync_all_automatic_receipts",
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype.fiskaly_register.fiskaly_register.check_all_outage_deadlines",
	],
	"daily": [
		"erpnext_fiskaly_sign_at.services.fiscalization.purge_old_api_logs",
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype.fiskaly_receipt.fiskaly_receipt.check_annual_receipt_requirements",
		"erpnext_fiskaly_sign_at.api.exports.reconcile_processing_exports",
		"erpnext_fiskaly_sign_at.api.exports.ensure_quarterly_backups",
		"erpnext_fiskaly_sign_at.api.exports.check_quarterly_backup_requirements",
		"erpnext_fiskaly_sign_at.api.exports.check_decommission_requirements",
	],
}

before_install = "erpnext_fiskaly_sign_at.install.before_install"
after_install = "erpnext_fiskaly_sign_at.install.after_install"
after_app_install = "erpnext_fiskaly_sign_at.install.after_app_install"
before_migrate = "erpnext_fiskaly_sign_at.install.before_migrate"
after_migrate = "erpnext_fiskaly_sign_at.install.after_migrate"

jinja = {"methods": ["erpnext_fiskaly_sign_at.print_utils.fiskaly_qr_svg"]}

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "erpnext_fiskaly_sign_at",
# 		"logo": "/assets/erpnext_fiskaly_sign_at/logo.png",
# 		"title": "Erpnext Fiskaly Sign At",
# 		"route": "/erpnext_fiskaly_sign_at",
# 		"has_permission": "erpnext_fiskaly_sign_at.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/erpnext_fiskaly_sign_at/css/erpnext_fiskaly_sign_at.css"
# app_include_js = "/assets/erpnext_fiskaly_sign_at/js/erpnext_fiskaly_sign_at.js"

# include js, css files in header of web template
# web_include_css = "/assets/erpnext_fiskaly_sign_at/css/erpnext_fiskaly_sign_at.css"
# web_include_js = "/assets/erpnext_fiskaly_sign_at/js/erpnext_fiskaly_sign_at.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "erpnext_fiskaly_sign_at/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "erpnext_fiskaly_sign_at/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "erpnext_fiskaly_sign_at.utils.jinja_methods",
# 	"filters": "erpnext_fiskaly_sign_at.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "erpnext_fiskaly_sign_at.install.before_install"
# after_install = "erpnext_fiskaly_sign_at.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "erpnext_fiskaly_sign_at.uninstall.before_uninstall"
# after_uninstall = "erpnext_fiskaly_sign_at.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "erpnext_fiskaly_sign_at.utils.before_app_install"
# after_app_install = "erpnext_fiskaly_sign_at.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "erpnext_fiskaly_sign_at.utils.before_app_uninstall"
# after_app_uninstall = "erpnext_fiskaly_sign_at.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "erpnext_fiskaly_sign_at.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "erpnext_fiskaly_sign_at.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"erpnext_fiskaly_sign_at.tasks.all"
# 	],
# 	"daily": [
# 		"erpnext_fiskaly_sign_at.tasks.daily"
# 	],
# 	"hourly": [
# 		"erpnext_fiskaly_sign_at.tasks.hourly"
# 	],
# 	"weekly": [
# 		"erpnext_fiskaly_sign_at.tasks.weekly"
# 	],
# 	"monthly": [
# 		"erpnext_fiskaly_sign_at.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "erpnext_fiskaly_sign_at.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "erpnext_fiskaly_sign_at.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "erpnext_fiskaly_sign_at.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "erpnext_fiskaly_sign_at.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["erpnext_fiskaly_sign_at.utils.before_request"]
# after_request = ["erpnext_fiskaly_sign_at.utils.after_request"]

# Job Events
# ----------
# before_job = ["erpnext_fiskaly_sign_at.utils.before_job"]
# after_job = ["erpnext_fiskaly_sign_at.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"erpnext_fiskaly_sign_at.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []
