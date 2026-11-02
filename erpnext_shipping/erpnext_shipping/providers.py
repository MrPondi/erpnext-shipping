# Copyright (c) 2026, Frappe Technologies and contributors
# For license information, please see license.txt
"""
Shipping providers: the carriers and shipping platforms a Shipment can get rates from and be booked with. Each
one subclasses `ShippingProvider` and is registered under the `shipping_providers` hook (see hooks.py), so
another app can add a provider without changing this one.
"""

import json
from abc import ABC, abstractmethod
from functools import cached_property
from typing import NotRequired, TypedDict

import frappe
from erpnext.stock.doctype.shipment.shipment import get_company_contact
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from erpnext_shipping.erpnext_shipping.utils import get_address, get_contact, get_delivery_company_name

PROVIDERS_HOOK = "shipping_providers"

# The options of Shipment.tracking_status
TRACKING_STATUSES = ("In Progress", "Delivered", "Returned", "Lost")


class ServiceRate(TypedDict):
	"""
	A service a provider offers for a shipment, as `get_rates` returns it. Keys of its own that a provider adds
	come back to its `create_shipment` with the rate the user picked.
	"""

	carrier: str
	service_name: str
	total_price: float
	currency: str
	# Set by this app
	service_provider: NotRequired[str]
	is_preferred: NotRequired[int]


class BookedShipment(TypedDict):
	"""What `create_shipment` returns for a shipment it booked"""

	shipment_id: str
	carrier: NotRequired[str]
	carrier_service: NotRequired[str]
	shipment_amount: NotRequired[float]
	awb_number: NotRequired[str]
	# Set by this app
	service_provider: NotRequired[str]


class TrackingInfo(TypedDict, total=False):
	"""What `get_tracking_data` returns. Only the keys it has are written to the Shipment."""

	awb_number: str
	tracking_status: str  # one of TRACKING_STATUSES
	tracking_status_info: str
	tracking_url: str


class ShipmentContext:
	"""
	The shipment a provider is asked to rate or book: its parties, parcels and goods.

	The addresses are loaded and checked when it is made. The contacts, the delivery company's name and the
	Shipment itself are loaded when a provider first reads them, so a provider that has no use for them can't
	fail on them. Each provider gets a copy of its own and may change the values in place.
	"""

	def __init__(
		self,
		pickup_from_type: str,
		delivery_to_type: str,
		pickup_address_name: str,
		delivery_address_name: str,
		parcels: list[dict] | str,
		description_of_content: str,
		pickup_date: str,
		value_of_goods: float | str,
		pickup_contact_name: str | None = None,
		delivery_contact_name: str | None = None,
		shipment: str | None = None,
	):
		self.shipment = shipment
		self.pickup_from_type = pickup_from_type
		self.delivery_to_type = delivery_to_type
		# For a pickup from the Company, the User who is the pickup contact person
		self.pickup_contact_name = pickup_contact_name
		self.delivery_contact_name = delivery_contact_name
		self.parcels = json.loads(parcels) if isinstance(parcels, str) else parcels
		self.description_of_content = description_of_content
		self.pickup_date = pickup_date
		self.value_of_goods = flt(value_of_goods)
		self.pickup_address = get_address(pickup_address_name)
		self.delivery_address = get_address(delivery_address_name)

	@cached_property
	def pickup_contact(self) -> frappe._dict:
		if self.pickup_from_type != "Company":
			return get_contact(self.pickup_contact_name)

		contact = get_company_contact(user=self.pickup_contact_name)
		contact.email_id = contact.pop("email", None)
		return contact

	@cached_property
	def delivery_contact(self) -> frappe._dict:
		return get_contact(self.delivery_contact_name)

	@cached_property
	def delivery_company_name(self) -> str | None:
		"""The name of the Customer, Supplier or Company the shipment goes to"""
		return get_delivery_company_name(self.shipment) if self.shipment else None

	@cached_property
	def doc(self) -> Document | None:
		"""The Shipment, for the fields other apps add to it"""
		return frappe.get_doc("Shipment", self.shipment) if self.shipment else None


class ShippingProvider(ABC):
	"""
	A carrier or shipping platform. Subclass it in any app and register the class in that app's hooks.py:

		shipping_providers = {"My Carrier": "my_app.shipping.MyCarrierProvider"}

	The registry makes the class with no arguments for every call, and before it asks whether the provider is
	enabled, so keep `__init__` cheap and don't throw in it. Build API clients in the methods.
	"""

	# The name the class is registered under, which Shipments store in service_provider. Set by the registry.
	name: str = ""
	# A Single DocType with an `enabled` Check, read by the default `is_enabled` and by `get_settings`
	settings_doctype: str | None = None

	def is_enabled(self, context: ShipmentContext) -> bool:
		"""
		Whether to ask the provider for rates for the shipment. Override it for settings that differ per company
		(the Shipment is `context.doc`).
		"""
		if not self.settings_doctype:
			return True
		return bool(frappe.db.get_single_value(self.settings_doctype, "enabled"))

	def get_settings(self) -> Document:
		return frappe.get_cached_doc(self.settings_doctype)

	@abstractmethod
	def get_rates(self, context: ShipmentContext) -> list[ServiceRate]:
		"""
		The services the provider offers for the shipment. Throw (frappe.throw) for a problem the user can fix:
		the message is shown with the other providers' rates. Any other error is logged, and the other
		providers' rates are still shown.
		"""

	@abstractmethod
	def create_shipment(self, context: ShipmentContext, service: ServiceRate) -> BookedShipment | None:
		"""
		Book the shipment with `service`, the rate the user picked. It is the rate as the browser sent it back,
		so don't rely on its price. Return None when nothing was booked.
		"""

	def get_labels(self, shipment: Document) -> list[bytes | str]:
		"""
		The labels of the booked Shipment: PDF contents, which are attached to the Shipment, or URLs. A
		provider without labels leaves this as it is.
		"""
		raise NotImplementedError

	def get_tracking_data(self, shipment: Document) -> TrackingInfo | None:
		"""
		Where the booked Shipment is now, or None when nothing is known. A provider without tracking leaves
		this as it is.
		"""
		raise NotImplementedError


def get_provider_paths() -> dict[str, str]:
	"""
	The providers the installed apps register, as {name: dotted path to the class}. Frappe lists a name's
	paths from every app, so when two apps register a name, the one loaded last wins, as with Frappe's own
	overrides.
	"""
	return {
		name: paths[-1] if isinstance(paths, list | tuple) else paths
		for name, paths in frappe.get_hooks(PROVIDERS_HOOK, default={}).items()
		if paths
	}


def get_provider(name: str) -> ShippingProvider:
	"""The provider registered under `name`, the name Shipment.service_provider stores"""
	path = get_provider_paths().get(name)
	if not path:
		frappe.throw(
			_("Shipping provider {0} is not available. Is the app that provides it installed?").format(
				frappe.bold(name)
			)
		)

	return _load_provider(name, path)


def get_providers() -> list[ShippingProvider]:
	"""
	Every registered provider. One that can't be loaded is logged and left out, so that a broken app doesn't
	keep the other providers from working.
	"""
	providers = []
	for name, path in get_provider_paths().items():
		try:
			providers.append(_load_provider(name, path))
		except Exception:
			frappe.log_error(title=f"Shipping provider {name} could not be loaded")

	return providers


def _load_provider(name: str, path: str) -> ShippingProvider:
	provider_class = frappe.get_attr(path)
	if not (isinstance(provider_class, type) and issubclass(provider_class, ShippingProvider)):
		raise TypeError(f"{path}, registered as shipping provider {name}, is not a ShippingProvider")

	provider = provider_class()
	provider.name = name
	return provider
