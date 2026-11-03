## ERPNext Shipping

A Shipping Integration for ERPNext with various platforms. Platforms integrated in this app are:

- [LetMeShip](https://www.letmeship.com/en/)
- [SendCloud](https://www.sendcloud.com/home-new/)

> [!TIP]
> Please make sure to get your API access enabled first, by contacting the LetMeShip support.

## Features
- Creation of shipment to a carrier service (e.g. FedEx, UPS) via LetMeShip and SendCloud. 
- Compare shipping rates. 
- Printing the shipping label is also made available within the Shipment doctype.
- Templates for the parcel dimensions.
- Shipment tracking.
- Shipments made from a Delivery Note start with their parcels, pickup date and description of content.

## Installation

Install [on Frappe Cloud](https://frappecloud.com/marketplace/apps/shipping) or your own server:

```bash
cd ~/frappe-bench
bench get-app https://github.com/frappe/erpnext-shipping.git --branch version-14
bench --site $MY_SITE install-app erpnext_shipping
```

## Setup

Some shipping providers require the contact details of the pickup contact. Please make sure that the **User** selected as the _Pickup Contact Person_ has a first name, last name, email address, and phone number before submitting the **Shipment**.

For the 'compare shipping rates' feature to work as expected, you need to generate an API key from your service provider. Service providers have their own specific doctypes similar to those from the `Integrations`. They can be enabled or disabled depending on your needs.

![LetMeShip 2020-08-05 09-54-28](https://user-images.githubusercontent.com/17470909/89377411-500c4f80-d724-11ea-8fe5-b11fec2a5c27.png)

### Fetch Shipping Rates
![core2](https://user-images.githubusercontent.com/17470909/89377460-70d4a500-d724-11ea-8550-a2813b936651.gif)

You can see the list of shipping rates by clicking the `Fetch Shipping Rates` button. Once you picked a rate, it will create the shipment for you. 

### Shipping Label
![71bcfc9d-9d66-4a58-8238-1eeab4e9a24f 2020-08-05 09-48-32](https://user-images.githubusercontent.com/17470909/89377478-78944980-d724-11ea-8120-a5374c6e4c5e.png)

The service provider will also provide the shipping label and to generate the label, click on the `Print Shipping Label` on top of the doctype.

## Shipments from Delivery Notes

**Create > Shipment** on a Delivery Note opens a Shipment with these filled in:

- **Parcels**: a row for each stock item of the Delivery Note, as many as are delivered. Its size is the Item's package: the **Package Length**, **Package Width** and **Package Height** (cm) this app adds to the Item, under **Weight Per Unit** in the Inventory tab. Its weight is the Weight Per Unit, converted from the Item's Weight UOM to kilograms; a weight without a Weight UOM is taken as kilograms. An item whose package has neither a size nor a weight gets no row, and a message names it.
- **Pickup Date**: today, if today is a workday and it is before **Same-Day Pickup Until** in **Shipping Settings** (12:00 unless you change it), else the next workday. Workdays are the days the pickup Company's **Default Holiday List** doesn't have as holidays. Without a list, and on the days a list doesn't cover, they are Monday to Friday. A Holiday List gets its weekends with **Add to Holidays** and the public holidays with **Add Local Holidays**; extend it, or make the next one, before its To Date.
- **Description of Content**: the items by their names on the Delivery Note, on one line, with how many there are unless it's one: `2 x Lawn Mower, Engine Oil`.

Change what doesn't fit before saving. A Delivery Note added to the Shipment afterwards adds no parcels or names.

This app fills them in by overriding ERPNext's `erpnext.stock.doctype.delivery_note.delivery_note.make_shipment` (`override_whitelisted_methods` in `hooks.py`). If another app overrides it as well, the app loaded last wins.

## Adding a Shipping Provider

Another app can add a provider without changing this one. It subclasses `ShippingProvider` and registers the class in its own `hooks.py`:

```python
# my_app/hooks.py
required_apps = ["erpnext_shipping"]

shipping_providers = {"My Carrier": "my_app.shipping.MyCarrierProvider"}
```

The name is what a Shipment stores as its Service Provider, so keep it once Shipments are booked with it. An app loaded later can register a name again to replace that provider. LetMeShip and SendCloud are registered the same way, in this app's `hooks.py`.

```python
# my_app/shipping.py
from erpnext_shipping.erpnext_shipping.providers import ShippingProvider


class MyCarrierProvider(ShippingProvider):
	settings_doctype = "My Carrier Settings"  # a Single DocType with an "enabled" Check

	def get_rates(self, context):
		return [{"carrier": "DHL", "service_name": "Paket", "total_price": 18.5, "currency": "PLN", "service_id": "AH"}]

	def create_shipment(self, context, service):
		return {"shipment_id": "123", "carrier": "DHL", "carrier_service": "Paket", "shipment_amount": 18.5, "awb_number": "123"}

	def get_labels(self, shipment):
		return [pdf_bytes]  # or URLs

	def get_tracking_data(self, shipment):
		return {"awb_number": "123", "tracking_status": "In Progress", "tracking_status_info": "In transit", "tracking_url": "https://..."}
```

- `context` holds the shipment to quote or book: `pickup_address`, `delivery_address`, `pickup_contact`, `delivery_contact`, `parcels`, `description_of_content`, `pickup_date`, `value_of_goods`, `delivery_company_name`, and `doc`, the Shipment, for fields your app adds to it (a cash-on-delivery amount, a parcel locker). Each provider gets a copy of its own.
- Keys of your own in a rate, such as `service_id`, come back to `create_shipment` in `service`. It is the rate as the browser sent it back, so don't rely on its price.
- `get_labels` and `get_tracking_data` are optional. Labels can be PDF contents, which are attached to the Shipment, or URLs. `tracking_status` is one of In Progress, Delivered, Returned or Lost; any other value is stored as In Progress, with the text in Tracking Status Info.
- Throw (`frappe.throw`) in `get_rates` for a problem the user can fix: the message is shown along with the other providers' rates. Any other error is logged, and the other providers' rates are still shown.
- Override `is_enabled(context)` for settings that differ per company, and keep `__init__` cheap: providers are made for every call.
- `show_error_alert` and `get_tracking_url` (in `erpnext_shipping.erpnext_shipping.utils`) help with errors and tracking links, and `set_tracking_data` (in `erpnext_shipping.erpnext_shipping.shipping`) stores tracking that reaches your app another way, such as a webhook.

The types and docstrings in `erpnext_shipping/erpnext_shipping/providers.py` describe each method in full.

-----------------------
#### License

MIT
