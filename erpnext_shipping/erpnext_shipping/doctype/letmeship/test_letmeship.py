# Copyright (c) 2020, Frappe and Contributors
# See license.txt

from unittest.mock import create_autospec, patch

import frappe
from frappe.tests import IntegrationTestCase

from erpnext_shipping.erpnext_shipping.doctype.letmeship import letmeship
from erpnext_shipping.erpnext_shipping.doctype.letmeship.letmeship import LetMeShipProvider, LetMeShipUtils
from erpnext_shipping.erpnext_shipping.test_shipping import make_context


class TestLetMeShipProvider(IntegrationTestCase):
	"""The provider hands the shipment to LetMeShipUtils as shipping.py used to"""

	def setUp(self):
		super().setUp()
		self.utils = create_autospec(LetMeShipUtils, instance=True)
		patcher = patch.object(letmeship, "get_letmeship_utils", return_value=self.utils)
		patcher.start()
		self.addCleanup(patcher.stop)

	def tearDown(self):
		frappe.db.rollback()
		super().tearDown()

	def test_rates(self):
		context = make_context()

		LetMeShipProvider().get_rates(context)

		self.utils.get_available_services.assert_called_once_with(
			delivery_to_type=context.delivery_to_type,
			pickup_address=context.pickup_address,
			delivery_address=context.delivery_address,
			parcels=context.parcels,
			description_of_content=context.description_of_content,
			pickup_date=context.pickup_date,
			value_of_goods=context.value_of_goods,
			pickup_contact=context.pickup_contact,
			delivery_contact=context.delivery_contact,
		)

	def test_booking(self):
		context = make_context()
		service = {"service_provider": "LetMeShip", "id": 1, "service_name": "Paket", "carrier": "DHL"}

		LetMeShipProvider().create_shipment(context, service)

		self.utils.create_shipment.assert_called_once_with(
			pickup_address=context.pickup_address,
			delivery_company_name=None,
			delivery_address=context.delivery_address,
			parcels=context.parcels,
			description_of_content=context.description_of_content,
			pickup_date=context.pickup_date,
			value_of_goods=context.value_of_goods,
			pickup_contact=context.pickup_contact,
			delivery_contact=context.delivery_contact,
			service_info=service,
		)

	def test_label_bytes_wrap_as_in_the_browser(self):
		self.utils.get_label.return_value = [37, 80, 68, 70, -1]

		self.assertEqual(LetMeShipProvider().get_labels(frappe._dict(shipment_id="1")), [b"%PDF\xff"])
		self.utils.get_label.assert_called_once_with("1")

	def test_no_label(self):
		self.utils.get_label.return_value = None

		self.assertEqual(LetMeShipProvider().get_labels(frappe._dict(shipment_id="1")), [])

	def test_tracking(self):
		LetMeShipProvider().get_tracking_data(frappe._dict(shipment_id="1"))

		self.utils.get_tracking_data.assert_called_once_with("1")
