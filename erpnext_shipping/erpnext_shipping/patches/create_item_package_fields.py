from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from erpnext_shipping.custom_fields import get_fields_for_patch


def execute():
	"""Add the package size of Items, which Shipments made from a Delivery Note take their parcels from."""
	create_custom_fields(get_fields_for_patch("Item", ["package_length", "package_width", "package_height"]))
