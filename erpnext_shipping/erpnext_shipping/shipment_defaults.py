# Copyright (c) 2026, Frappe Technologies and contributors
# For license information, please see license.txt
"""
What a Shipment made from a Delivery Note starts with: a parcel for each of its stock items, sized and weighed as
the Item's package, the first day it can be picked up, and the items as its description of content. ERPNext's
Create > Shipment of the Delivery Note opens it with them (see override_whitelisted_methods in hooks.py), and the
user changes what doesn't fit before saving.
"""

import math
from datetime import date

import frappe
from erpnext.setup.doctype.holiday_list.holiday_list import is_holiday
from erpnext.stock.doctype.delivery_note.delivery_note import make_shipment as make_erpnext_shipment
from erpnext.stock.doctype.item.item import get_uom_conv_factor
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, escape_html, flt, get_time, getdate, now_datetime

# The UOM of Shipment Parcel weights, as ERPNext names it
KILOGRAM = "Kg"
# How many days a pickup day is looked for
PICKUP_DAYS = 14
# The first day of a weekend, for the days no Holiday List covers
SATURDAY = 5


@frappe.whitelist()
def make_shipment(source_name: str, target_doc: str | dict | Document | None = None):
	"""ERPNext's Shipment of a Delivery Note, with its parcels, pickup date and description of content"""
	shipment = make_erpnext_shipment(source_name, target_doc)
	try:
		set_shipment_defaults(shipment)
	except Exception:
		# The Shipment opens all the same, and the user fills in what's missing, as without this app
		frappe.log_error(title=f"Defaults of the Shipment of Delivery Note {source_name} could not be set")
	return shipment


def set_shipment_defaults(shipment: Document) -> None:
	"""
	Fill in the parcels, pickup date and description of content that the Shipment doesn't have yet, from the
	stock items of its Delivery Notes
	"""
	items = get_shipped_items([row.delivery_note for row in shipment.shipment_delivery_note])
	if not shipment.shipment_parcel:
		for parcel in get_parcels(items):
			shipment.append("shipment_parcel", parcel)

	if not shipment.pickup_date:
		holiday_list = (
			frappe.get_cached_value("Company", shipment.pickup_company, "default_holiday_list")
			if shipment.pickup_company
			else None
		)
		shipment.pickup_date = get_pickup_date(holiday_list)

	if not shipment.description_of_content:
		shipment.description_of_content = get_description_of_content(items)


def get_shipped_items(delivery_notes: list[str]) -> list[frappe._dict]:
	"""
	The stock items of the Delivery Notes, which are what the parcels hold: one for each item code, in the order
	of the notes and their rows, with the package and weight of its Item and the quantities of its rows summed,
	`qty` as sold and `stock_qty` in the stock UOM
	"""
	if not delivery_notes:
		return []

	dn_item = frappe.qb.DocType("Delivery Note Item")
	item = frappe.qb.DocType("Item")
	lines = (
		frappe.qb.from_(dn_item)
		.inner_join(item)
		.on(item.name == dn_item.item_code)
		.select(
			dn_item.parent,
			dn_item.item_code,
			dn_item.item_name,
			dn_item.qty,
			dn_item.stock_qty,
			item.package_length,
			item.package_width,
			item.package_height,
			item.weight_per_unit,
			item.weight_uom,
		)
		.where(
			(dn_item.parenttype == "Delivery Note")
			& dn_item.parent.isin(delivery_notes)
			& (item.is_stock_item == 1)
			& (dn_item.stock_qty > 0)
		)
		.orderby(dn_item.idx)
		.run(as_dict=True)
	)
	# The sort keeps the order of the rows within a note
	lines.sort(key=lambda line: delivery_notes.index(line.parent))

	items = {}
	for line in lines:
		if shipped := items.get(line.item_code):
			shipped.qty = flt(shipped.qty) + flt(line.qty)
			shipped.stock_qty = flt(shipped.stock_qty) + flt(line.stock_qty)
		else:
			items[line.item_code] = line
	return list(items.values())


def get_parcels(items: list[frappe._dict]) -> list[dict]:
	"""
	A parcel for each item, sized as its package and weighed in kilograms, as many as there are stock units. An
	item whose package has neither a size nor a weight gets none, and the user is told which, as rows without
	values couldn't be told apart.
	"""
	parcels, unknown = [], []
	for item in items:
		size = {side: flt(item.get(f"package_{side}")) for side in ("length", "width", "height")}
		weight = get_weight_in_kg(item.weight_per_unit, item.weight_uom)
		if not (weight or any(size.values())):
			unknown.append(item.item_name or item.item_code)
			continue

		# A package holds one stock unit
		parcels.append({**size, "weight": weight, "count": max(1, math.ceil(flt(item.stock_qty, 6)))})

	if unknown:
		frappe.msgprint(
			_(
				"Items without a package size or weight have no parcels: {0}. Set them on the Items, or add the parcels here."
			).format(", ".join(frappe.bold(escape_html(name)) for name in unknown)),
			title=_("Parcels"),
			indicator="orange",
		)
	return parcels


def get_weight_in_kg(weight: float | None, uom: str | None) -> float:
	"""
	A weight in kilograms, as Shipment Parcel has them. A weight without a UOM is taken as kilograms, and one in a
	UOM that can't be converted to them is unknown: 0.
	"""
	if not uom or uom == KILOGRAM:
		return flt(weight)
	return flt(flt(weight) * flt(get_uom_conv_factor(uom, KILOGRAM)), 3)


def get_pickup_date(holiday_list: str | None = None) -> date | None:
	"""
	The first day a Shipment made now can be picked up: today, if it's a workday and still before the cut-off of
	Shipping Settings, else the next workday. None when there is none in the next two weeks.
	"""
	now = now_datetime()
	day = now.date()
	cutoff = frappe.get_cached_doc("Shipping Settings").same_day_pickup_until
	if not cutoff or now.time() >= get_time(cutoff):
		day = add_days(day, 1)

	last_day = add_days(day, PICKUP_DAYS)
	while day < last_day:
		if is_workday(day, holiday_list):
			return day
		day = add_days(day, 1)
	return None


def is_workday(day: date, holiday_list: str | None = None) -> bool:
	"""
	Whether parcels are picked up on the day: on a day the Holiday List covers, unless the list has it as a
	holiday, and on the other days from Monday to Friday
	"""
	if holiday_list:
		from_date, to_date = frappe.get_cached_value("Holiday List", holiday_list, ["from_date", "to_date"])
		if getdate(from_date) <= day <= getdate(to_date):
			return not is_holiday(holiday_list, day)
	return day.weekday() < SATURDAY


def get_description_of_content(items: list[frappe._dict]) -> str:
	"""
	What the parcels hold, on one line, as carriers may take only the first: each item by its name on the
	Delivery Note, after how many there are unless it's one, as in "2 x Lawn Mower, Engine Oil"
	"""
	names = []
	for item in items:
		name = item.item_name or item.item_code
		qty = flt(item.qty, 6)
		if qty == 1:
			names.append(name)
		else:
			names.append(f"{int(qty) if qty.is_integer() else qty} x {name}")
	return ", ".join(names)
