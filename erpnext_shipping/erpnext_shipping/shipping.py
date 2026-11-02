# Copyright (c) 2020, Frappe Technologies and contributors
# For license information, please see license.txt
import copy
import json

import frappe
from frappe import _

from erpnext_shipping.erpnext_shipping.providers import ShipmentContext, get_provider, get_providers
from erpnext_shipping.erpnext_shipping.utils import match_parcel_service_type_carrier, show_error_alert

# The keys of tracking data, which are Shipment fields, and the Delivery Note fields they are written to
TRACKING_FIELDS = {
	"awb_number": "tracking_number",
	"tracking_status": "tracking_status",
	"tracking_status_info": "tracking_status_info",
	"tracking_url": "tracking_url",
}


@frappe.whitelist(methods=["POST"])
def fetch_shipping_rates(
	pickup_from_type,
	delivery_to_type,
	pickup_address_name,
	delivery_address_name,
	parcels,
	description_of_content,
	pickup_date,
	value_of_goods,
	pickup_contact_name=None,
	delivery_contact_name=None,
	shipment=None,
):
	# Return Shipping Rates for the various Shipping Providers
	frappe.has_permission("Shipment", "write", doc=shipment, throw=True)
	context = ShipmentContext(
		pickup_from_type=pickup_from_type,
		delivery_to_type=delivery_to_type,
		pickup_address_name=pickup_address_name,
		delivery_address_name=delivery_address_name,
		parcels=parcels,
		description_of_content=description_of_content,
		pickup_date=pickup_date,
		value_of_goods=value_of_goods,
		pickup_contact_name=pickup_contact_name,
		delivery_contact_name=delivery_contact_name,
		shipment=shipment,
	)

	shipment_prices = []
	for provider in get_providers():
		# Each provider gets a copy, as providers change the addresses and contacts to suit their APIs
		provider_context = copy.deepcopy(context)
		try:
			if not provider.is_enabled(provider_context):
				continue
			prices = [
				frappe._dict(price, service_provider=provider.name)
				for price in provider.get_rates(provider_context) or []
			]
		except frappe.ValidationError:
			# The provider's message is shown with the other providers' rates
			continue
		except Exception:
			show_error_alert(f"fetching {provider.name} prices")
			continue

		shipment_prices += prices

	shipment_prices = match_parcel_service_type_carrier(shipment_prices, "carrier", "service_name")
	shipment_prices = [item for item in shipment_prices if "total_price" in item]
	shipment_prices = sorted(shipment_prices, key=lambda k: k["total_price"])
	return shipment_prices


@frappe.whitelist(methods=["POST"])
def create_shipment(
	shipment,
	pickup_from_type,
	delivery_to_type,
	pickup_address_name,
	delivery_address_name,
	shipment_parcel,
	description_of_content,
	pickup_date,
	value_of_goods,
	service_data,
	shipment_notific_email=None,
	tracking_notific_email=None,
	pickup_contact_name=None,
	delivery_contact_name=None,
	delivery_notes=None,
):
	frappe.has_permission("Shipment", "write", doc=shipment, throw=True)
	if isinstance(delivery_notes, str):
		delivery_notes = json.loads(delivery_notes)

	if delivery_notes is None:
		delivery_notes = []

	service_info = json.loads(service_data)
	provider = get_provider(service_info.get("service_provider"))
	context = ShipmentContext(
		pickup_from_type=pickup_from_type,
		delivery_to_type=delivery_to_type,
		pickup_address_name=pickup_address_name,
		delivery_address_name=delivery_address_name,
		parcels=shipment_parcel,
		description_of_content=description_of_content,
		pickup_date=pickup_date,
		value_of_goods=value_of_goods,
		pickup_contact_name=pickup_contact_name,
		delivery_contact_name=delivery_contact_name,
		shipment=shipment,
	)
	shipment_info = provider.create_shipment(context, service_info)

	if shipment_info:
		shipment_info["service_provider"] = provider.name
		shipment = frappe.get_doc("Shipment", shipment)
		shipment.db_set(
			{
				"service_provider": shipment_info.get("service_provider"),
				"carrier": shipment_info.get("carrier"),
				"carrier_service": shipment_info.get("carrier_service"),
				"shipment_id": shipment_info.get("shipment_id"),
				"shipment_amount": shipment_info.get("shipment_amount"),
				"awb_number": shipment_info.get("awb_number"),
				"status": "Booked",
			}
		)

		if delivery_notes:
			update_delivery_note(delivery_notes=delivery_notes, shipment_info=shipment_info)

	return shipment_info


@frappe.whitelist(methods=["POST"])
def print_shipping_label(shipment: str):
	frappe.has_permission("Shipment", "print", doc=shipment, throw=True)
	shipment_doc = frappe.get_doc("Shipment", shipment)
	provider = get_provider(shipment_doc.service_provider)
	try:
		labels = provider.get_labels(shipment_doc) or []
	except NotImplementedError:
		frappe.throw(_("{0} does not provide shipping labels.").format(provider.name))

	# PDF contents are attached to the Shipment, URLs are opened as they are
	return [
		save_label_as_attachment(shipment, label, index) if isinstance(label, bytes) else label
		for index, label in enumerate(labels, start=1)
		if label
	]


def save_label_as_attachment(
	shipment: str, content: bytes, index: int | None = None, file_extension: str = "pdf"
) -> str:
	"""Store label as attachment to Shipment and return the URL."""
	attachment = frappe.new_doc("File")
	if index is not None:
		attachment.file_name = f"label_{shipment}_{index}.{file_extension}"
	else:
		attachment.file_name = f"label_{shipment}.{file_extension}"
	attachment.content = content
	attachment.folder = "Home/Attachments"
	attachment.attached_to_doctype = "Shipment"
	attachment.attached_to_name = shipment
	attachment.is_private = 1
	attachment.save()
	return attachment.file_url


@frappe.whitelist(methods=["POST"])
def update_tracking(shipment, service_provider=None, shipment_id=None, delivery_notes=None):
	# The provider and the shipment ID are read from the Shipment; the arguments stay for older callers
	frappe.has_permission("Shipment", "write", doc=shipment, throw=True)
	if isinstance(delivery_notes, str):
		delivery_notes = json.loads(delivery_notes)

	shipment_doc = frappe.get_doc("Shipment", shipment)
	provider = get_provider(shipment_doc.service_provider)
	try:
		tracking_data = provider.get_tracking_data(shipment_doc)
	except NotImplementedError:
		return

	if tracking_data:
		set_tracking_data(shipment_doc, tracking_data, delivery_notes)


def set_tracking_data(shipment, tracking_data: dict, delivery_notes: list[str] | None = None):
	"""
	Write tracking data of a provider to the Shipment and to its Delivery Notes, or to the ones
	`delivery_notes` names. Only the keys the data has are written. For provider apps that get tracking
	from a webhook of their own, too.
	"""
	if isinstance(shipment, str):
		shipment = frappe.get_doc("Shipment", shipment)

	values = {key: tracking_data[key] for key in TRACKING_FIELDS if key in tracking_data}
	if not values:
		return

	shipment.db_set(values)

	if delivery_notes is None:
		delivery_notes = [row.delivery_note for row in shipment.shipment_delivery_note]
	if delivery_notes:
		update_delivery_note(delivery_notes=delivery_notes, tracking_info=tracking_data)


def update_delivery_note(delivery_notes, shipment_info=None, tracking_info=None):
	# Update Shipment Info in Delivery Note
	# Using db_set since some services might not exist
	delivery_notes = list(set(delivery_notes))

	for delivery_note in delivery_notes:
		dl_doc = frappe.get_doc("Delivery Note", delivery_note)
		if shipment_info:
			dl_doc.db_set("delivery_type", "Parcel Service")
			dl_doc.db_set("parcel_service", shipment_info.get("carrier"))
			dl_doc.db_set("parcel_service_type", shipment_info.get("carrier_service"))
		if tracking_info:
			for key, fieldname in TRACKING_FIELDS.items():
				if key in tracking_info:
					dl_doc.db_set(fieldname, tracking_info[key])
