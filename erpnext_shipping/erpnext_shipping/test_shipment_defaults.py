# Copyright (c) 2026, Frappe Technologies and Contributors
# See license.txt

from datetime import date
from unittest.mock import patch

import frappe
from erpnext.stock.doctype.item.item import get_uom_conv_factor
from frappe.model.mapper import make_mapped_doc
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, get_datetime

from erpnext_shipping.erpnext_shipping import shipment_defaults
from erpnext_shipping.erpnext_shipping.shipment_defaults import (
	get_description_of_content,
	get_parcels,
	get_pickup_date,
	get_shipped_items,
	get_weight_in_kg,
	make_shipment,
)
from erpnext_shipping.erpnext_shipping.test_shipping import make_delivery_note

ERPNEXT_MAKE_SHIPMENT = "erpnext.stock.doctype.delivery_note.delivery_note.make_shipment"


def make_item(item_name: str, is_stock_item: int = 1, **values) -> str:
	"""An Item, with the package and weight given as values"""
	item = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": f"_Test Shipping {item_name}",
			"item_name": item_name,
			"item_group": "All Item Groups",
			"stock_uom": "Nos",
			"is_stock_item": is_stock_item,
			**values,
		}
	)
	return item.insert().name


def add_line(delivery_note: str, item_code: str, qty: float, stock_qty: float | None = None):
	"""A row of a Delivery Note. Only its row is needed, so ERPNext's checks are left out."""
	frappe.get_doc(
		{
			"doctype": "Delivery Note Item",
			"parent": delivery_note,
			"parenttype": "Delivery Note",
			"parentfield": "items",
			"idx": frappe.db.count("Delivery Note Item", {"parent": delivery_note}) + 1,
			"item_code": item_code,
			"item_name": frappe.db.get_value("Item", item_code, "item_name"),
			"qty": qty,
			"stock_qty": qty if stock_qty is None else stock_qty,
		}
	).db_insert()


def make_submitted_delivery_note(*lines: tuple[str, float]) -> str:
	"""A Delivery Note of (item code, quantity) lines, submitted as ERPNext's make_shipment asks"""
	delivery_note = make_delivery_note()
	for item_code, qty in lines:
		add_line(delivery_note, item_code, qty)
	frappe.db.set_value("Delivery Note", delivery_note, {"docstatus": 1, "is_return": 0})
	return delivery_note


def made_at(now: str):
	"""The time a Shipment is made at, in the system's timezone"""
	return patch.object(shipment_defaults, "now_datetime", return_value=get_datetime(now))


def make_holiday_list(from_date: str, to_date: str, holidays: list[str]) -> str:
	holiday_list = frappe.get_doc(
		{
			"doctype": "Holiday List",
			"holiday_list_name": f"_Test Shipping {frappe.generate_hash(length=8)}",
			"from_date": from_date,
			"to_date": to_date,
			"holidays": [{"holiday_date": day, "description": "_Test"} for day in holidays],
		}
	)
	return holiday_list.insert().name


class TestPickupDate(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()
		super().tearDown()

	def pickup_date(self, now: str, holiday_list: str | None = None, cutoff: str | None = "12:00"):
		with self.change_settings("Shipping Settings", {"same_day_pickup_until": cutoff}), made_at(now):
			return get_pickup_date(holiday_list)

	def test_today_before_the_cut_off(self):
		# 4 November 2026 is a Wednesday
		self.assertEqual(self.pickup_date("2026-11-04 11:59"), date(2026, 11, 4))

	def test_next_workday_from_the_cut_off(self):
		self.assertEqual(self.pickup_date("2026-11-04 12:00"), date(2026, 11, 5))
		# From a Friday afternoon, the Monday after
		self.assertEqual(self.pickup_date("2026-11-06 13:00"), date(2026, 11, 9))

	def test_weekend_morning_gives_monday(self):
		self.assertEqual(self.pickup_date("2026-11-07 09:00"), date(2026, 11, 9))

	def test_never_today_without_a_cut_off(self):
		self.assertEqual(self.pickup_date("2026-11-04 08:00", cutoff=None), date(2026, 11, 5))

	def test_holidays_of_the_list_are_skipped(self):
		# Independence Day, and the weekend after it, added to the list as weekly offs
		holiday_list = make_holiday_list(
			"2026-11-01", "2026-11-30", ["2026-11-11", "2026-11-14", "2026-11-15"]
		)

		self.assertEqual(self.pickup_date("2026-11-10 13:00", holiday_list), date(2026, 11, 12))
		self.assertEqual(self.pickup_date("2026-11-13 13:00", holiday_list), date(2026, 11, 16))

	def test_the_list_decides_on_the_days_it_covers(self):
		# A list without weekly offs has its Saturdays as workdays. After its To Date, weekends are off.
		holiday_list = make_holiday_list("2026-11-01", "2026-11-07", [])

		self.assertEqual(self.pickup_date("2026-11-06 13:00", holiday_list), date(2026, 11, 7))
		self.assertEqual(self.pickup_date("2026-11-07 13:00", holiday_list), date(2026, 11, 9))

	def test_without_a_workday_it_stays_empty(self):
		holiday_list = make_holiday_list(
			"2026-11-01", "2026-11-30", [add_days("2026-11-01", offset) for offset in range(30)]
		)

		self.assertIsNone(self.pickup_date("2026-11-04 13:00", holiday_list))


class TestParcels(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		if not get_uom_conv_factor("Gram", "Kg"):
			frappe.get_doc(
				{
					"doctype": "UOM Conversion Factor",
					"category": "Mass",
					"from_uom": "Kg",
					"to_uom": "Gram",
					"value": 1000,
				}
			).insert()

	def tearDown(self):
		frappe.db.rollback()
		super().tearDown()

	def test_a_parcel_for_each_stock_item_as_its_package(self):
		mower = make_item(
			"Mower",
			package_length=60,
			package_width=40,
			package_height=30,
			weight_per_unit=12,
			weight_uom="Kg",
		)
		oil = make_item("Oil", weight_per_unit=600, weight_uom="Gram")
		service = make_item("Assembly", is_stock_item=0, weight_per_unit=1)
		delivery_note = make_submitted_delivery_note((mower, 1), (service, 1), (oil, 2), (mower, 1))

		self.assertEqual(
			get_parcels(get_shipped_items([delivery_note])),
			[
				{"length": 60, "width": 40, "height": 30, "weight": 12, "count": 2},
				{"length": 0, "width": 0, "height": 0, "weight": 0.6, "count": 2},
			],
		)

	def test_weights_in_kilograms(self):
		self.assertEqual(get_weight_in_kg(2.5, None), 2.5)
		self.assertEqual(get_weight_in_kg(2.5, "Kg"), 2.5)
		self.assertEqual(get_weight_in_kg(2500, "Gram"), 2.5)
		# A UOM that isn't a weight
		self.assertEqual(get_weight_in_kg(2, "Nos"), 0)

	def test_parcels_for_whole_stock_units(self):
		box = make_item("Box", weight_per_unit=1)
		delivery_note = make_delivery_note()
		add_line(delivery_note, box, qty=1, stock_qty=1.5)

		self.assertEqual(get_parcels(get_shipped_items([delivery_note]))[0]["count"], 2)

	def test_items_without_package_or_weight_are_named(self):
		delivery_note = make_submitted_delivery_note((make_item("Blade"), 1))

		with patch.object(frappe, "msgprint") as msgprint:
			self.assertEqual(get_parcels(get_shipped_items([delivery_note])), [])

		self.assertIn("Blade", msgprint.call_args.args[0])


class TestDescriptionOfContent(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()
		super().tearDown()

	def test_items_by_name_after_how_many(self):
		mower, oil = make_item("Mower"), make_item("Oil")
		service = make_item("Assembly", is_stock_item=0)
		delivery_note = make_submitted_delivery_note((mower, 2), (service, 1), (oil, 1))

		self.assertEqual(get_description_of_content(get_shipped_items([delivery_note])), "2 x Mower, Oil")

	def test_a_fraction_keeps_its_decimals(self):
		items = [frappe._dict(item_code="Cable", item_name="Cable", qty=2.5)]
		self.assertEqual(get_description_of_content(items), "2.5 x Cable")

	def test_items_in_the_order_of_the_delivery_notes(self):
		first = make_submitted_delivery_note((make_item("Mower"), 1))
		second = make_submitted_delivery_note((make_item("Oil"), 1))

		self.assertEqual([item.item_name for item in get_shipped_items([second, first])], ["Oil", "Mower"])


class TestMakeShipment(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.clear_messages()

	def tearDown(self):
		frappe.db.rollback()
		super().tearDown()

	def test_erpnext_make_shipment_is_overridden(self):
		self.assertEqual(
			frappe.override_whitelisted_method(ERPNEXT_MAKE_SHIPMENT),
			"erpnext_shipping.erpnext_shipping.shipment_defaults.make_shipment",
		)

	def test_shipment_of_a_delivery_note_is_filled_in(self):
		mower = make_item("Mower", package_length=60, package_width=40, package_height=30, weight_per_unit=12)
		delivery_note = make_submitted_delivery_note((mower, 2))

		with (
			self.change_settings("Shipping Settings", {"same_day_pickup_until": "12:00"}),
			made_at("2026-11-04 09:00"),
		):
			# As the Delivery Note's Create > Shipment asks for it
			shipment = make_mapped_doc(ERPNEXT_MAKE_SHIPMENT, delivery_note)

		self.assertEqual(shipment.shipment_delivery_note[0].delivery_note, delivery_note)
		self.assertEqual(
			[(row.length, row.width, row.height, row.weight, row.count) for row in shipment.shipment_parcel],
			[(60, 40, 30, 12, 2)],
		)
		self.assertEqual(str(shipment.pickup_date), "2026-11-04")
		self.assertEqual(shipment.description_of_content, "2 x Mower")

	def test_values_the_shipment_has_are_kept(self):
		delivery_note = make_submitted_delivery_note((make_item("Mower", weight_per_unit=12), 1))

		# As the browser sends a Shipment that is open already
		shipment = make_shipment(
			delivery_note,
			frappe.as_json(
				{"doctype": "Shipment", "pickup_date": "2026-12-01", "description_of_content": "Spare parts"}
			),
		)

		self.assertEqual(str(shipment.pickup_date), "2026-12-01")
		self.assertEqual(shipment.description_of_content, "Spare parts")
		self.assertEqual(len(shipment.shipment_parcel), 1)

	def test_a_failing_default_still_gives_the_shipment(self):
		delivery_note = make_submitted_delivery_note((make_item("Mower", weight_per_unit=12), 1))

		with (
			patch.object(shipment_defaults, "get_pickup_date", side_effect=ValueError("broken")),
			patch.object(frappe, "log_error") as log_error,
		):
			shipment = make_shipment(delivery_note)

		self.assertEqual(shipment.shipment_delivery_note[0].delivery_note, delivery_note)
		log_error.assert_called_once()
