# Copyright (c) 2026, Frappe Technologies and Contributors
# See license.txt

from datetime import time

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import get_time


class TestShippingSettings(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()
		super().tearDown()

	def test_settings_never_saved_have_the_default_cut_off(self):
		# The defaults that pickup dates rely on, before anyone opens the settings
		frappe.db.delete("Singles", {"doctype": "Shipping Settings"})
		frappe.clear_document_cache("Shipping Settings", "Shipping Settings")

		settings = frappe.get_cached_doc("Shipping Settings")
		self.assertEqual(get_time(settings.same_day_pickup_until), time(12, 0))
