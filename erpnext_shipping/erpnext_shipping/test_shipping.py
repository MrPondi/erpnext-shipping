# Copyright (c) 2026, Frappe Technologies and Contributors
# See license.txt

import copy
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, today

from erpnext_shipping.erpnext_shipping.providers import (
	ShipmentContext,
	ShippingProvider,
	get_provider,
	get_providers,
)


class FakeProvider(ShippingProvider):
	def get_rates(self, context):
		return []

	def create_shipment(self, context, service):
		return None


class OtherFakeProvider(FakeProvider):
	pass


class NotAProvider:
	pass


def path(cls) -> str:
	return f"{__name__}.{cls.__name__}"


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


def make_context(**kwargs) -> ShipmentContext:
	args = {
		"pickup_from_type": "Customer",
		"delivery_to_type": "Customer",
		"pickup_address_name": make_address("_Test Shipping Pickup"),
		"delivery_address_name": make_address("_Test Shipping Delivery"),
		"parcels": [{"length": 30, "width": 20, "height": 10, "weight": 2, "count": 1}],
		"description_of_content": "_Test goods",
		"pickup_date": add_days(today(), 1),
		"value_of_goods": 100,
		"pickup_contact_name": make_contact("Jan"),
		"delivery_contact_name": make_contact("Anna"),
	}
	args.update(kwargs)
	return ShipmentContext(**args)


class TestProviderRegistry(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()
		super().tearDown()

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
