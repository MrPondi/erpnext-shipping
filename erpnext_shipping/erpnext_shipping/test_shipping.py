# Copyright (c) 2026, Frappe Technologies and Contributors
# See license.txt

import copy
import json
from io import BytesIO
from unittest.mock import patch

import frappe
from frappe.handler import is_valid_http_method
from frappe.model.document import Document
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, today
from pypdf import PdfWriter

from erpnext_shipping.erpnext_shipping import shipping
from erpnext_shipping.erpnext_shipping.doctype.letmeship.letmeship import LetMeShipProvider
from erpnext_shipping.erpnext_shipping.doctype.sendcloud.sendcloud import SendCloudProvider
from erpnext_shipping.erpnext_shipping.providers import (
	ShipmentContext,
	ShippingProvider,
	get_provider,
	get_providers,
)
from erpnext_shipping.erpnext_shipping.shipping import (
	create_shipment,
	fetch_shipping_rates,
	print_shipping_label,
	update_tracking,
)
from erpnext_shipping.erpnext_shipping.utils import update_tracking_info_daily

PARCELS = [{"length": 30, "width": 20, "height": 10, "weight": 2, "count": 1}]


class FakeProvider(ShippingProvider):
	"""Offers nothing, and has neither labels nor tracking"""

	def get_rates(self, context):
		return []

	def create_shipment(self, context, service):
		return None


class OtherFakeProvider(FakeProvider):
	pass


class NotAProvider:
	pass


class CarrierProvider(ShippingProvider):
	"""Offers two services with a price and one without, books them, and has labels and tracking"""

	# The pickup address titles the provider was asked for rates with
	titles_seen = []

	def get_rates(self, context):
		CarrierProvider.titles_seen.append(context.pickup_address.address_title)
		# Change the shipment in place, as LetMeShip does
		context.pickup_address.address_title = "changed by the provider"
		return [
			{
				"carrier": "DHL",
				"service_name": "Paket",
				"total_price": 12.5,
				"currency": "PLN",
				"service_id": "AH",
			},
			{
				"carrier": "DHL",
				"service_name": "Express",
				"total_price": 9.9,
				"currency": "PLN",
				"service_id": "09",
			},
			{"carrier": "DHL", "service_name": "On request", "currency": "PLN"},
		]

	def create_shipment(self, context, service):
		return {
			"shipment_id": f"{service['service_id']}-1",
			"carrier": service["carrier"],
			"carrier_service": service["service_name"],
			"shipment_amount": service["total_price"],
			"awb_number": "AWB-1",
		}

	def get_labels(self, shipment):
		return [make_pdf(), None, "https://example.com/label.pdf"]

	def get_tracking_data(self, shipment):
		return {
			"awb_number": "AWB-2",
			"tracking_status": "In Progress",
			"tracking_url": "https://example.com/AWB-2",
		}


class ErrorProvider(FakeProvider):
	def get_rates(self, context):
		raise ConnectionError("The API is down")

	def get_tracking_data(self, shipment):
		raise ConnectionError("The API is down")


class MalformedProvider(FakeProvider):
	def get_rates(self, context):
		return [None]


class ThrowingProvider(FakeProvider):
	def get_rates(self, context):
		frappe.throw("No service to this country")


class DisabledProvider(FakeProvider):
	asked = []

	def is_enabled(self, context):
		return False

	def get_rates(self, context):
		DisabledProvider.asked.append(context)
		return []


def path(cls) -> str:
	return f"{__name__}.{cls.__name__}"


def make_pdf() -> bytes:
	"""A blank page, as Frappe checks a PDF that is attached"""
	writer = PdfWriter()
	writer.add_blank_page(width=100, height=100)
	pdf = BytesIO()
	writer.write(pdf)
	return pdf.getvalue()


def make_address(title: str) -> str:
	address = frappe.get_doc(
		{
			"doctype": "Address",
			"address_title": title,
			"address_type": "Shipping",
			"address_line1": "ul. Prosta 1",
			"city": "Warszawa",
			"pincode": "00-001",
			"country": "Poland",
		}
	)
	return address.insert().name


def make_contact(first_name: str, last_name: str | None = "_Test Kowalski") -> str:
	contact = frappe.get_doc(
		{
			"doctype": "Contact",
			"first_name": first_name,
			"last_name": last_name,
			"email_ids": [{"email_id": f"{first_name.lower()}@example.com", "is_primary": 1}],
			"phone_nos": [{"phone": "+48 600 100 200", "is_primary_phone": 1}],
		}
	)
	return contact.insert().name


def shipment_args(**kwargs) -> dict:
	"""What the Shipment form sends for rates and for booking"""
	args = {
		"pickup_from_type": "Customer",
		"delivery_to_type": "Customer",
		"pickup_address_name": make_address("_Test Shipping Pickup"),
		"delivery_address_name": make_address("_Test Shipping Delivery"),
		"pickup_contact_name": make_contact("Jan"),
		"delivery_contact_name": make_contact("Anna"),
		"parcels": copy.deepcopy(PARCELS),
		"description_of_content": "_Test goods",
		"pickup_date": add_days(today(), 1),
		"value_of_goods": 100,
	}
	args.update(kwargs)
	return args


def make_context(**kwargs) -> ShipmentContext:
	return ShipmentContext(**shipment_args(**kwargs))


def make_shipment(delivery_note: str | None = None, **values) -> Document:
	"""
	A submitted Shipment. It's made without ERPNext's checks, which need a company and parties: only the
	fields the shipping endpoints read matter here.
	"""
	args = shipment_args()
	parcels = args.pop("parcels")
	shipment = frappe.get_doc(
		{
			**args,
			"doctype": "Shipment",
			"shipment_parcel": parcels,
			"shipment_delivery_note": [{"delivery_note": delivery_note}] if delivery_note else [],
			"pickup_from": "09:00",
			"pickup_to": "17:00",
			**values,
		}
	)
	shipment.flags.ignore_validate = True
	shipment.flags.ignore_links = True
	shipment.flags.ignore_mandatory = True
	shipment.insert()
	shipment.db_set("docstatus", 1)
	return shipment


def make_delivery_note() -> str:
	"""A Delivery Note to write the shipping fields to. Only its row is needed, so ERPNext's checks are left out."""
	delivery_note = frappe.new_doc("Delivery Note")
	delivery_note.name = f"_Test-Shipping-{frappe.generate_hash(length=8)}"
	delivery_note.db_insert()
	return delivery_note.name


class TestProviderRegistry(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()
		super().tearDown()

	def test_built_in_providers_are_registered(self):
		self.assertIsInstance(get_provider("LetMeShip"), LetMeShipProvider)
		self.assertIsInstance(get_provider("SendCloud"), SendCloudProvider)

	def test_name_gives_the_registered_class(self):
		with self.patch_hooks({"shipping_providers": {"Fake": path(FakeProvider)}}):
			provider = get_provider("Fake")

		self.assertIsInstance(provider, FakeProvider)
		self.assertEqual(provider.name, "Fake")

	def test_app_loaded_later_replaces_a_provider(self):
		hook = {"Fake": [path(FakeProvider), path(OtherFakeProvider)]}
		with self.patch_hooks({"shipping_providers": hook}):
			self.assertIsInstance(get_provider("Fake"), OtherFakeProvider)

	def test_unknown_provider_throws(self):
		with self.patch_hooks({"shipping_providers": {}}):
			self.assertRaises(frappe.ValidationError, get_provider, "Fake")

	def test_provider_that_cannot_load_is_left_out(self):
		hook = {
			"Fake": path(FakeProvider),
			"Missing": "erpnext_shipping.erpnext_shipping.no_such_module.Provider",
			"Not a provider": path(NotAProvider),
		}
		with self.patch_hooks({"shipping_providers": hook}), patch.object(frappe, "log_error") as log_error:
			self.assertEqual([provider.name for provider in get_providers()], ["Fake"])
			self.assertEqual(log_error.call_count, 2)
			self.assertRaises(TypeError, get_provider, "Not a provider")

	def test_enabled_without_settings_or_by_their_enabled_check(self):
		provider = FakeProvider()
		self.assertTrue(provider.is_enabled(None))

		provider.settings_doctype = "LetMeShip"
		frappe.db.set_single_value("LetMeShip", "enabled", 0)
		self.assertFalse(provider.is_enabled(None))
		frappe.db.set_single_value("LetMeShip", "enabled", 1)
		self.assertTrue(provider.is_enabled(None))


class TestShipmentContext(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()
		super().tearDown()

	def test_addresses_are_loaded_when_made(self):
		context = make_context(parcels='[{"weight": 2, "count": 1}]', value_of_goods="12.5")

		self.assertEqual(context.pickup_address.country_code, "pl")
		self.assertEqual(context.delivery_address.city, "Warszawa")
		self.assertEqual(context.parcels, [{"weight": 2, "count": 1}])
		self.assertEqual(context.value_of_goods, 12.5)

	def test_contacts_are_loaded_on_first_use(self):
		# A contact without a last name only fails the provider that reads it
		context = make_context(delivery_contact_name=make_contact("Ewa", last_name=None))

		self.assertEqual(context.pickup_contact.first_name, "Jan")
		self.assertRaises(frappe.ValidationError, getattr, context, "delivery_contact")

	def test_pickup_contact_of_a_company_is_its_user(self):
		context = make_context(pickup_from_type="Company", pickup_contact_name="Administrator")

		self.assertEqual(
			context.pickup_contact.email_id, frappe.db.get_value("User", "Administrator", "email")
		)
		self.assertNotIn("email", context.pickup_contact)

	def test_a_copy_shares_no_values(self):
		context = make_context()
		context_copy = copy.deepcopy(context)
		context_copy.pickup_address.address_title = "changed"
		context_copy.parcels[0]["weight"] = 99
		context_copy.delivery_contact.phone = "0"

		self.assertNotEqual(context.pickup_address.address_title, "changed")
		self.assertEqual(context.parcels[0]["weight"], 2)
		self.assertNotEqual(context.delivery_contact.phone, "0")


class TestShippingRates(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.clear_messages()
		CarrierProvider.titles_seen.clear()
		DisabledProvider.asked.clear()

	def tearDown(self):
		frappe.db.rollback()
		super().tearDown()

	def fetch_rates(self, providers: dict):
		args = shipment_args()
		# As the browser sends them
		args["parcels"] = json.dumps(args["parcels"])
		with (
			self.patch_hooks({"shipping_providers": providers}),
			patch.object(shipping, "show_error_alert") as show_error_alert,
		):
			rates = fetch_shipping_rates(**args)

		return rates, show_error_alert

	def test_rates_of_the_enabled_providers_by_price(self):
		rates, _ = self.fetch_rates({"DHL": path(CarrierProvider), "Off": path(DisabledProvider)})

		self.assertEqual(
			[(rate.service_provider, rate.service_name) for rate in rates],
			[("DHL", "Express"), ("DHL", "Paket")],
		)
		# The provider's own keys come back to it when the rate is booked
		self.assertEqual(rates[0].service_id, "09")
		self.assertEqual(DisabledProvider.asked, [])

	def test_failing_provider_keeps_the_others_rates(self):
		rates, show_error_alert = self.fetch_rates(
			{"Error": path(ErrorProvider), "Throwing": path(ThrowingProvider), "DHL": path(CarrierProvider)}
		)

		self.assertEqual(len(rates), 2)
		show_error_alert.assert_called_once_with("fetching Error prices")
		self.assertIn("No service to this country", str(frappe.local.message_log))

	def test_provider_with_malformed_rates_keeps_the_others_rates(self):
		rates, show_error_alert = self.fetch_rates(
			{"Malformed": path(MalformedProvider), "DHL": path(CarrierProvider)}
		)

		self.assertEqual(len(rates), 2)
		show_error_alert.assert_called_once_with("fetching Malformed prices")

	def test_each_provider_gets_a_copy_of_the_shipment(self):
		self.fetch_rates({"One": path(CarrierProvider), "Two": path(CarrierProvider)})

		self.assertEqual(CarrierProvider.titles_seen, ["_Test Shipping Pickup", "_Test Shipping Pickup"])


class TestShippingBooking(IntegrationTestCase):
	providers = {"DHL": path(CarrierProvider), "Fake": path(FakeProvider), "Error": path(ErrorProvider)}

	def tearDown(self):
		frappe.db.rollback()
		super().tearDown()

	def book(self, shipment: str, service: dict, delivery_notes: list[str] | None = None):
		args = shipment_args()
		with self.patch_hooks({"shipping_providers": self.providers}):
			return create_shipment(
				shipment=shipment,
				shipment_parcel=json.dumps(args.pop("parcels")),
				service_data=json.dumps(service),
				delivery_notes=json.dumps(delivery_notes or []),
				**args,
			)

	def test_booking_goes_to_the_provider_of_the_rate(self):
		delivery_note = make_delivery_note()
		shipment = make_shipment(delivery_note=delivery_note)
		service = {
			"service_provider": "DHL",
			"carrier": "DHL",
			"service_name": "Paket",
			"total_price": 12.5,
			"service_id": "AH",
		}

		booked = self.book(shipment.name, service, [delivery_note])

		self.assertEqual(booked["service_provider"], "DHL")
		shipment.reload()
		self.assertEqual(
			(shipment.status, shipment.service_provider, shipment.shipment_id, shipment.awb_number),
			("Booked", "DHL", "AH-1", "AWB-1"),
		)
		self.assertEqual(
			(shipment.carrier, shipment.carrier_service, shipment.shipment_amount), ("DHL", "Paket", 12.5)
		)
		self.assertEqual(
			frappe.db.get_value(
				"Delivery Note", delivery_note, ["delivery_type", "parcel_service", "parcel_service_type"]
			),
			("Parcel Service", "DHL", "Paket"),
		)

	def test_booking_with_a_provider_no_app_registers_throws(self):
		shipment = make_shipment()

		self.assertRaises(frappe.ValidationError, self.book, shipment.name, {"service_provider": "Gone"})

	def test_label_contents_are_attached_and_urls_kept(self):
		shipment = make_shipment(service_provider="DHL", shipment_id="AH-1")

		with self.patch_hooks({"shipping_providers": self.providers}):
			urls = print_shipping_label(shipment.name)

		self.assertEqual(len(urls), 2)
		self.assertEqual(urls[1], "https://example.com/label.pdf")
		label = frappe.get_doc("File", {"file_url": urls[0]})
		self.assertEqual(
			(label.attached_to_doctype, label.attached_to_name, label.is_private, label.file_name),
			("Shipment", shipment.name, 1, f"label_{shipment.name}_1.pdf"),
		)

	def test_provider_without_labels_says_so(self):
		shipment = make_shipment(service_provider="Fake", shipment_id="1")

		with self.patch_hooks({"shipping_providers": self.providers}):
			self.assertRaises(frappe.ValidationError, print_shipping_label, shipment.name)

	def test_tracking_is_written_to_the_shipment_and_its_delivery_notes(self):
		delivery_note = make_delivery_note()
		shipment = make_shipment(
			delivery_note=delivery_note,
			service_provider="DHL",
			shipment_id="AH-1",
			awb_number="AWB-1",
			tracking_status_info="Picked up",
		)

		with self.patch_hooks({"shipping_providers": self.providers}):
			update_tracking(shipment.name)

		shipment.reload()
		self.assertEqual(
			(shipment.awb_number, shipment.tracking_status, shipment.tracking_url),
			("AWB-2", "In Progress", "https://example.com/AWB-2"),
		)
		# The provider sent no tracking_status_info, so it stays
		self.assertEqual(shipment.tracking_status_info, "Picked up")
		self.assertEqual(
			frappe.db.get_value("Delivery Note", delivery_note, ["tracking_number", "tracking_status"]),
			("AWB-2", "In Progress"),
		)

	def test_provider_without_tracking_is_skipped(self):
		shipment = make_shipment(service_provider="Fake", shipment_id="1", awb_number="AWB-1")

		with self.patch_hooks({"shipping_providers": self.providers}):
			update_tracking(shipment.name)

		self.assertEqual(frappe.db.get_value("Shipment", shipment.name, "awb_number"), "AWB-1")

	def test_daily_update_carries_on_past_a_failing_shipment(self):
		shipment = make_shipment(service_provider="DHL", shipment_id="AH-1", status="Booked")
		# Made last, so the job comes to it first
		make_shipment(service_provider="Error", shipment_id="E-1", status="Booked")

		with (
			self.patch_hooks({"shipping_providers": self.providers}),
			patch.object(frappe, "log_error") as log_error,
		):
			update_tracking_info_daily()

		self.assertEqual(frappe.db.get_value("Shipment", shipment.name, "awb_number"), "AWB-2")
		log_error.assert_called_once()


class TestShippingPermissions(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()
		super().tearDown()

	def test_endpoints_need_permission_on_the_shipment(self):
		shipment = make_shipment(service_provider="DHL", shipment_id="AH-1")
		rates_args = shipment_args(shipment=shipment.name)
		rates_args["parcels"] = json.dumps(rates_args["parcels"])
		booking_args = shipment_args(
			shipment=shipment.name, service_data=json.dumps({"service_provider": "DHL"})
		)
		booking_args["shipment_parcel"] = json.dumps(booking_args.pop("parcels"))
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": "shipping-tester@example.com",
				"first_name": "Shipping Tester",
				"send_welcome_email": 0,
				"roles": [{"role": "Website Manager"}],
			}
		).insert()

		with (
			self.set_user(user.name),
			self.patch_hooks({"shipping_providers": {"DHL": path(CarrierProvider)}}),
		):
			self.assertRaises(frappe.PermissionError, fetch_shipping_rates, **rates_args)
			self.assertRaises(frappe.PermissionError, create_shipment, **booking_args)
			self.assertRaises(frappe.PermissionError, print_shipping_label, shipment.name)
			self.assertRaises(frappe.PermissionError, update_tracking, shipment.name)

	def test_endpoints_refuse_get(self):
		with patch.object(frappe.local, "request", frappe._dict(method="GET"), create=True):
			for endpoint in (fetch_shipping_rates, create_shipment, print_shipping_label, update_tracking):
				self.assertRaises(frappe.PermissionError, is_valid_http_method, endpoint)
