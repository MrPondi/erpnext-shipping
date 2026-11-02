# Copyright (c) 2020, Frappe and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from requests.exceptions import HTTPError

from erpnext_shipping.erpnext_shipping.doctype.sendcloud import sendcloud
from erpnext_shipping.erpnext_shipping.doctype.sendcloud.sendcloud import SendCloudProvider, SendCloudUtils
from erpnext_shipping.erpnext_shipping.test_shipping import make_context


class TestSendCloudProvider(IntegrationTestCase):
	"""The provider hands the shipment to SendCloudUtils as shipping.py used to"""

	def setUp(self):
		super().setUp()
		patcher = patch.object(sendcloud, "SendCloudUtils", autospec=True)
		self.utils = patcher.start().return_value
		self.addCleanup(patcher.stop)

	def tearDown(self):
		frappe.db.rollback()
		super().tearDown()

	def test_rates(self):
		context = make_context()

		SendCloudProvider().get_rates(context)

		self.utils.get_available_services.assert_called_once_with(
			delivery_address=context.delivery_address,
			pickup_address=context.pickup_address,
			parcels=context.parcels,
		)

	def test_booking(self):
		context = make_context(shipment="SHIPMENT-00001")
		service = {"service_provider": "SendCloud", "service_id": 8, "carrier": "dhl", "total_price": 5}

		SendCloudProvider().create_shipment(context, service)

		self.utils.create_shipment.assert_called_once_with(
			shipment="SHIPMENT-00001",
			pickup_address=context.pickup_address,
			pickup_contact=context.pickup_contact,
			delivery_address=context.delivery_address,
			delivery_contact=context.delivery_contact,
			service_info=service,
			shipment_parcels=context.parcels,
		)

	def test_labels_are_downloaded(self):
		self.utils.get_label.return_value = ["https://example.com/1", "https://example.com/2"]
		self.utils.download_label.side_effect = [b"%PDF-1", None]

		labels = SendCloudProvider().get_labels(frappe._dict(shipment_id="1, 2"))

		self.assertEqual(labels, [b"%PDF-1", None])
		self.utils.get_label.assert_called_once_with("1, 2")

	def test_tracking(self):
		SendCloudProvider().get_tracking_data(frappe._dict(shipment_id="1"))

		self.utils.get_tracking_data.assert_called_once_with("1")


class TestSendCloudTracking(IntegrationTestCase):
	def get_tracking_data(self, *statuses):
		"""The tracking of a shipment whose parcels have these statuses, None for one SendCloud can't read"""
		responses = []
		for index, status in enumerate(statuses):
			response = MagicMock()
			if status is None:
				response.raise_for_status.side_effect = HTTPError("502 Bad Gateway")
			response.json.return_value = {
				"parcel": {
					"tracking_number": f"T{index}",
					"tracking_url": f"https://example.com/T{index}",
					"status": {"message": status},
				}
			}
			responses.append(response)

		# Made without its settings, which only load while SendCloud is enabled
		utils = SendCloudUtils.__new__(SendCloudUtils)
		utils.api_key, utils.api_secret = "key", "secret"
		with (
			patch.object(sendcloud.requests, "get", side_effect=responses),
			patch.object(sendcloud, "show_error_alert"),
		):
			return utils.get_tracking_data(", ".join(str(index) for index in range(len(statuses))))

	def test_delivered_when_every_parcel_is(self):
		tracking = self.get_tracking_data("Delivered", "Delivered")

		self.assertEqual(tracking["tracking_status"], "Delivered")
		self.assertEqual(tracking["tracking_status_info"], "Delivered, Delivered")

	def test_in_progress_until_then(self):
		tracking = self.get_tracking_data("Delivered", "En route to sorting center")

		self.assertEqual(tracking["tracking_status"], "In Progress")
		self.assertEqual(tracking["tracking_status_info"], "Delivered, En route to sorting center")

	def test_a_parcel_that_cannot_be_read_is_not_delivered(self):
		self.assertEqual(self.get_tracking_data("Delivered", None)["tracking_status"], "In Progress")

	def test_nothing_when_no_parcel_can_be_read(self):
		self.assertIsNone(self.get_tracking_data(None, None))
