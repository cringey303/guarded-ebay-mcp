import unittest
from decimal import Decimal

import httpx2 as httpx

from ebay_mcp.auth import TokenManager
from ebay_mcp.http import EbayHttp
from ebay_mcp.rest import RestClient, summarize_fees, summarize_order, validate_order_id
from ebay_mcp.store import Store
from ebay_mcp.trading import TradingClient, TradingError, build_request, parse_response, validate_item_id

from helpers import Recorder, is_token, json_resp, make_settings, token_ok

NS = 'xmlns="urn:ebay:apis:eBLBaseComponents"'

MY_SELLING = f"""<?xml version="1.0" encoding="UTF-8"?>
<GetMyeBaySellingResponse {NS}><Ack>Success</Ack>
<ActiveList><ItemArray>
 <Item><ItemID>123456789012</ItemID><Title>Supreme Camp Cap</Title>
  <BuyItNowPrice currencyID="USD">70.0</BuyItNowPrice>
  <ListingDetails><StartTime>2026-08-01T12:00:00.000Z</StartTime></ListingDetails>
  <SellingStatus><CurrentPrice currencyID="USD">70.0</CurrentPrice></SellingStatus>
  <WatchCount>4</WatchCount><QuantityAvailable>1</QuantityAvailable><ListingType>FixedPriceItem</ListingType>
 </Item>
</ItemArray><PaginationResult><TotalNumberOfPages>1</TotalNumberOfPages></PaginationResult></ActiveList>
</GetMyeBaySellingResponse>""".encode()

GET_ITEM = f"""<?xml version="1.0"?><GetItemResponse {NS}><Ack>Success</Ack><Item>
<ItemID>123456789012</ItemID><Title>Supreme Camp Cap</Title><StartPrice>70.0</StartPrice>
<SellingStatus><CurrentPrice>70.0</CurrentPrice><ListingStatus>Active</ListingStatus></SellingStatus>
<PrimaryCategory><CategoryID>52365</CategoryID><CategoryName>Clothing:Men:Hats</CategoryName></PrimaryCategory>
<ListingType>FixedPriceItem</ListingType>
<ShippingPackageDetails><WeightMajor unit="lbs">25</WeightMajor><WeightMinor unit="oz">0</WeightMinor>
<PackageLength unit="inches">10.0</PackageLength><ShippingPackage>PackageThickEnvelope</ShippingPackage></ShippingPackageDetails>
<WatchCount>4</WatchCount></Item></GetItemResponse>""".encode()

FAILURE = f"""<?xml version="1.0"?><ReviseInventoryStatusResponse {NS}><Ack>Failure</Ack>
<Errors><ShortMessage>Limit</ShortMessage><LongMessage>Call usage limit exceeded</LongMessage>
<ErrorCode>518</ErrorCode><SeverityCode>Error</SeverityCode></Errors></ReviseInventoryStatusResponse>""".encode()

TOKEN_EXPIRED = f"""<?xml version="1.0"?><GetItemResponse {NS}><Ack>Failure</Ack>
<Errors><ShortMessage>Expired</ShortMessage><LongMessage>IAF token supplied is expired.</LongMessage>
<ErrorCode>21917053</ErrorCode><SeverityCode>Error</SeverityCode></Errors></GetItemResponse>""".encode()

BILLION_LAUGHS = b"""<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">
<!ENTITY lol2 "&lol;&lol;&lol;&lol;">]><GetItemResponse><Ack>&lol2;</Ack></GetItemResponse>"""

XXE = b"""<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>
<GetItemResponse><Ack>&e;</Ack></GetItemResponse>"""


async def no_sleep(_):
    return None


def client_with(routes):
    rec = Recorder([(is_token, token_ok)] + routes)
    s = make_settings()
    s.requests_per_second = 1000
    http = EbayHttp(s, TokenManager(s, transport=rec.transport()), Store(), transport=rec.transport(), sleep=no_sleep)
    return TradingClient(s, http), RestClient(s, http), rec


def by_call(name):
    return lambda r: r.headers.get("X-EBAY-API-CALL-NAME") == name


class TradingTests(unittest.IsolatedAsyncioTestCase):
    def test_item_id_validation_blocks_injection(self):
        with self.assertRaises(ValueError):
            validate_item_id("123</ItemID><Evil/>")
        self.assertEqual(validate_item_id(" 123456789012 "), "123456789012")

    def test_item_id_must_be_digits_only(self):
        for bad in ("", "abc", "12345", "1234567890ab", "123456789012; DROP", "../../etc/passwd", "１２３４５６７８９０１２3"):
            with self.assertRaises(ValueError, msg=bad):
                validate_item_id(bad)

    def test_xxe_rejected(self):
        from defusedxml import DefusedXmlException
        with self.assertRaises(DefusedXmlException):
            parse_response(XXE)
        with self.assertRaises(DefusedXmlException):
            parse_response(BILLION_LAUGHS)

    async def test_xml_values_are_validated_before_they_reach_the_request(self):
        t, _, rec = client_with([])
        before = len(rec.requests)
        with self.assertRaises(ValueError):
            await t.get_item("123</ItemID><Evil/>")
        with self.assertRaises(ValueError):
            await t.revise_package("123456789012", 0, 9, package_type="</ShippingPackage><Evil/>")
        with self.assertRaises(ValueError):
            await t.revise_package("123456789012", "1</WeightMajor>", 9)
        with self.assertRaises(ValueError):
            await t.revise_prices([("123456789012", Decimal("59.999"))])
        self.assertEqual(len(rec.requests), before)  # nothing was sent

    async def test_expired_iaf_token_error_triggers_one_reauth(self):
        seq = iter([TOKEN_EXPIRED, GET_ITEM])
        t, _, rec = client_with([(by_call("GetItem"), lambda r: httpx.Response(200, content=next(seq)))])
        item = await t.get_item("123456789012")
        self.assertEqual(item["item_id"], "123456789012")
        self.assertEqual(sum(1 for r in rec.requests if is_token(r)), 2)

    def test_failure_raises_with_code(self):
        with self.assertRaises(TradingError) as cm:
            parse_response(FAILURE)
        self.assertIn("518", str(cm.exception))

    def test_build_request_namespace(self):
        self.assertIn(b'<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">', build_request("GetItem", ""))

    async def test_active_listings_and_item(self):
        t, _, rec = client_with([
            (by_call("GetMyeBaySelling"), lambda r: httpx.Response(200, content=MY_SELLING)),
            (by_call("GetItem"), lambda r: httpx.Response(200, content=GET_ITEM)),
        ])
        ls = await t.get_active_listings()
        self.assertEqual(ls[0]["watchers"], 4)
        self.assertEqual(ls[0]["price"], "70.0")
        item = await t.get_item("123456789012")
        sent = [r for r in rec.requests if by_call("GetItem")(r)][0]
        self.assertEqual(item["package"]["weight_lb"], 25)
        self.assertEqual(item["status"], "Active")
        self.assertEqual(item["package"]["length_in"], 10)
        self.assertIsNone(item["package"]["width_in"])
        self.assertEqual((item["category_id"], item["listing_type"]), ("52365", "FixedPriceItem"))
        self.assertEqual(sent.headers["X-EBAY-API-IAF-TOKEN"], "v^1.1#access-123")
        self.assertNotIn("Authorization", sent.headers)
        self.assertNotIn(b"RequesterCredentials", sent.content)
        sent = [r for r in rec.requests if by_call("GetItem")(r)][0]
        self.assertEqual(sent.headers["X-EBAY-API-COMPATIBILITY-LEVEL"], "1489")

    async def test_revise_prices_body(self):
        ok = f'<?xml version="1.0"?><ReviseInventoryStatusResponse {NS}><Ack>Success</Ack><InventoryStatus><ItemID>123456789012</ItemID><StartPrice>59.99</StartPrice></InventoryStatus></ReviseInventoryStatusResponse>'
        t, _, rec = client_with([(by_call("ReviseInventoryStatus"), lambda r: httpx.Response(200, content=ok.encode()))])
        res = await t.revise_prices([("123456789012", Decimal("59.99"))])
        self.assertEqual(res[0]["price"], "59.99")
        body = rec.requests[-1].content.decode()
        self.assertIn("<StartPrice>59.99</StartPrice>", body)
        with self.assertRaises(ValueError):
            await t.revise_prices([("123456789012", Decimal(1))] * 5)

    async def test_revise_package_body(self):
        ok = f'<?xml version="1.0"?><ReviseFixedPriceItemResponse {NS}><Ack>Success</Ack><ItemID>123456789012</ItemID></ReviseFixedPriceItemResponse>'
        t, _, rec = client_with([(by_call("ReviseFixedPriceItem"), lambda r: httpx.Response(200, content=ok.encode()))])
        await t.revise_package("123456789012", 0, 9, 10, 8, 2, "PackageThickEnvelope")
        body = rec.requests[-1].content.decode()
        self.assertIn('<WeightMajor unit="lbs">0</WeightMajor><WeightMinor unit="oz">9</WeightMinor>', body)
        self.assertIn("<Item><ItemID>123456789012</ItemID><ShippingPackageDetails>", body)
        self.assertIn('<PackageLength unit="inches">10</PackageLength>', body)
        self.assertIn("<ShippingPackage>PackageThickEnvelope</ShippingPackage>", body)


ORDER = {
    "orderId": "12-34567-89012", "creationDate": "2026-10-20T15:00:00.000Z",
    "orderFulfillmentStatus": "NOT_STARTED", "orderPaymentStatus": "PAID",
    "buyer": {"username": "buyer1", "buyerRegistrationAddress": {
        "fullName": "Jane Doe", "contactAddress": {"addressLine1": "1 Main St"}}},
    "pricingSummary": {"priceSubtotal": {"value": "70.00"}, "deliveryCost": {"value": "6.25"},
                       "total": {"value": "81.85"}},
    "fulfillmentStartInstructions": [{"shippingStep": {"shipTo": {"fullName": "Jane Doe"}}}],
    "lineItems": [{"legacyItemId": "123456789012", "title": "Supreme Camp Cap", "quantity": 1,
                   "lineItemCost": {"value": "70.00"},
                   "ebayCollectAndRemitTaxes": [{"amount": {"value": "5.60"}}],
                   "lineItemFulfillmentInstructions": {"shipByDate": "2026-10-23T06:59:59.000Z"}}],
}


class RestTests(unittest.IsolatedAsyncioTestCase):
    def test_order_summary_drops_pii(self):
        o = summarize_order(ORDER)
        self.assertEqual(o["buyer_shipping_paid"], "6.25")
        self.assertEqual(o["ship_by"], "2026-10-23T06:59:59.000Z")
        self.assertEqual(o["ebay_collected_tax"], "5.60")
        self.assertNotIn("Jane", str(o))

    def test_fee_summary(self):
        tx = [
            {"transactionType": "SALE", "bookingEntry": "CREDIT", "amount": {"value": "71.23"},
             "totalFeeAmount": {"value": "10.97"},
             "orderLineItems": [{"marketplaceFees": [{"feeType": "FINAL_VALUE_FEE", "amount": {"value": "10.57"}},
                                                     {"feeType": "FINAL_VALUE_FEE_FIXED_PER_ORDER", "amount": {"value": "0.40"}}]}]},
            {"transactionType": "SHIPPING_LABEL", "bookingEntry": "DEBIT", "amount": {"value": "9.10"}},
            {"transactionType": "SHIPPING_LABEL", "bookingEntry": "CREDIT", "amount": {"value": "9.10"}},
            {"transactionType": "SHIPPING_LABEL", "bookingEntry": "DEBIT", "amount": {"value": "5.49"}},
        ]
        f = summarize_fees(tx)
        self.assertEqual(f["transaction_fees"], "10.97")
        self.assertEqual(f["label_cost"], "5.49")  # voided 25 lb label refunded, re-bought at the right weight
        self.assertEqual(f["fee_breakdown"]["FINAL_VALUE_FEE"], "10.57")

    def test_order_id_validation(self):
        for bad in ("12}&filter=x", "12-34567-89012/../x", "abc", "", "12-34567-89012-1-2", "1" * 50):
            with self.assertRaises(ValueError, msg=bad):
                validate_order_id(bad)
        self.assertEqual(validate_order_id(" 12-34567-89012 "), "12-34567-89012")

    def test_order_summary_has_no_buyer_fields_at_all(self):
        o = summarize_order(ORDER)
        text = str(o)
        for leaked in ("buyer1", "Jane", "Doe", "1 Main St", "shipTo", "buyerRegistrationAddress"):
            self.assertNotIn(leaked, text)
        self.assertEqual(o["order_id"], "12-34567-89012")
        self.assertEqual(o["items"][0]["price"], "70.00")

    async def test_get_orders_request(self):
        _, r, rec = client_with([(lambda q: q.url.path.endswith("/sell/fulfillment/v1/order"),
                                  json_resp({"orders": [ORDER]}))])
        orders = await r.get_orders(days=7, limit=10)
        self.assertEqual(orders[0]["ship_by"], "2026-10-23T06:59:59.000Z")
        req = rec.requests[-1]
        self.assertEqual(req.url.host, "api.sandbox.ebay.com")
        self.assertRegex(req.url.params["filter"], r"^creationdate:\[\d{4}-\d\d-\d\dT[\d:.]+Z\.\.\]$")
        self.assertEqual(req.headers["Authorization"], "Bearer v^1.1#access-123")

    async def test_find_eligible_items_paginates(self):
        pages = iter([{"eligibleItems": [{"listingId": "123456789012"}], "next": "x"},
                      {"eligibleItems": [{"listingId": "223456789012"}]}])
        _, r, rec = client_with([(lambda q: q.url.path.endswith("find_eligible_items"),
                                  lambda q: httpx.Response(200, json=next(pages)))])
        self.assertEqual(await r.find_eligible_items(), {"123456789012", "223456789012"})
        self.assertEqual(rec.requests[-1].url.params["offset"], "200")

    async def test_rest_error_is_redacted(self):
        from ebay_mcp.http import EbayApiError
        _, r, _ = client_with([(lambda q: "/sell/fulfillment/" in q.url.path,
                                json_resp({"errors": [{"errorId": 1, "message": "bad Bearer v^1.1#leak"}]}, 400))])
        with self.assertRaises(EbayApiError) as cm:
            await r.get_order("12-34567-89012")
        self.assertNotIn("leak", str(cm.exception))

    async def test_finances_uses_apiz_and_filter(self):
        _, r, rec = client_with([(lambda q: "/sell/finances/" in q.url.path,
                                  json_resp({"transactions": [{"transactionType": "SALE"}]}))])
        tx = await r.get_order_transactions("12-34567-89012")
        self.assertEqual(len(tx), 1)
        req = rec.requests[-1]
        self.assertEqual(req.url.host, "apiz.sandbox.ebay.com")
        self.assertEqual(req.url.params["filter"], "orderId:{12-34567-89012}")

    async def test_send_offer_payload(self):
        captured = {}

        def handler(q):
            import json
            captured.update(json.loads(q.content))
            return httpx.Response(200, json={"offers": [{"offerId": "1"}]})

        _, r, rec = client_with([(lambda q: q.url.path.endswith("send_offer_to_interested_buyers"), handler)])
        res = await r.send_offer("123456789012", Decimal("63"), "Thanks for watching")
        self.assertEqual(res["offers_sent"], 1)
        self.assertEqual(captured["offeredItems"][0]["price"]["value"], "63.00")
        self.assertEqual(captured["offeredItems"][0]["listingId"], "123456789012")
        self.assertIs(captured["allowCounterOffer"], False)
        self.assertEqual(captured["offerDuration"], {"unit": "DAY", "value": 2})
        self.assertEqual(captured["message"], "Thanks for watching")
        self.assertEqual(rec.requests[-1].headers["X-EBAY-C-MARKETPLACE-ID"], "EBAY_US")

    async def test_browse_uses_app_token(self):
        _, r, rec = client_with([(lambda q: "/buy/browse/" in q.url.path,
                                  json_resp({"itemSummaries": [{"title": "x", "price": {"value": "60"}}]}))])
        await r.browse_search("supreme camp cap")
        self.assertEqual(rec.requests[-1].url.params["q"], "supreme camp cap")
        self.assertIn("buyingOptions:{FIXED_PRICE}", rec.requests[-1].url.params["filter"])
        token_req = [q for q in rec.requests if is_token(q)][-1]
        self.assertIn("client_credentials", token_req.content.decode())


if __name__ == "__main__":
    unittest.main()
