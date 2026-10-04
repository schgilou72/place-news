"""Tests for the BOM, the distributor clients (canned answers shaped like the
real APIs) and the sourcing logic — no KiCad, no network."""
import csv
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from plugin import distributors as dist  # noqa: E402
from plugin.bom import compact_refs, find_field, group_parts, package_of  # noqa: E402
from plugin.distributors import (Distributor, DistributorError, Offer, PriceBreak,  # noqa: E402
                                 lifecycle_of, parse_price)
from plugin.sourcing import (Settings, Sourcer, bom_rows, build_report, choose, fields_for,  # noqa: E402
                             order_csv, package_tokens, parse_value, ratings_of,
                             replacement_issues, same_package, write_bom_csv)


def offer(dist_name, sku, mpn, stock=1000, prices=((1, 0.10), (10, 0.08), (100, 0.05)), moq=1,
          multiple=1, lifecycle='active', package='0805', params=None, desc='', note=''):
    return Offer(distributor=dist_name, sku=sku, mpn=mpn, manufacturer='ACME', description=desc,
                 stock=stock, moq=moq, multiple=multiple,
                 prices=[PriceBreak(q, p) for q, p in prices], lifecycle=lifecycle,
                 package=package, params=dict(params or {}), note=note)


class FakeDist(Distributor):
    """A distributor with canned answers."""

    def __init__(self, name, by_mpn=None, by_kw=None, by_sku=None, alts=None):
        super().__init__()
        self.name = name
        self.min_interval = 0
        self.by_mpn, self.by_kw = by_mpn or {}, by_kw or {}
        self.by_sku, self.alts = by_sku or {}, alts or {}
        self.asked = []

    def _search_mpn(self, mpn, manufacturer=''):
        self.asked.append(('mpn', mpn))
        return [o for o in self.by_mpn.get(mpn.upper(), [])]

    def _search_keyword(self, query):
        self.asked.append(('kw', query))
        return list(self.by_kw.get('*', [])) + list(self.by_kw.get(query, []))

    def _lookup_sku(self, sku):
        return list(self.by_sku.get(sku.upper(), []))

    def _alternates(self, o):
        return list(self.alts.get(o.mpn.upper(), []))


# ---------------------------------------------------------------------------
# Offers, prices, quantities
# ---------------------------------------------------------------------------

class TestOffer(unittest.TestCase):
    def test_minimum_order_and_multiple(self):
        o = offer('LCSC', 'C1', 'X', prices=((100, 0.002), (1000, 0.0015)), moq=100, multiple=100)
        self.assertEqual(o.order_qty(4), 100)
        self.assertEqual(o.order_qty(150), 200)
        q, unit, total = o.cost(4)
        self.assertEqual((q, unit), (100, 0.002))
        self.assertAlmostEqual(total, 0.2)

    def test_price_break_reached(self):
        o = offer('Mouser', 'M1', 'X')
        self.assertEqual(o.cost(9)[1], 0.10)
        self.assertEqual(o.cost(10)[1], 0.08)
        self.assertEqual(o.cost(250)[1], 0.05)
        five = offer('DigiKey', 'D1', 'X', prices=((5, 0.3), (50, 0.2)), moq=1)
        self.assertEqual(five.cost(2)[0], 5)        # the first break is a minimum too

    def test_parse_price(self):
        for text, value in (('0,103 €', 0.103), ('$0.10', 0.10), ('1 234,56 €', 1234.56),
                            ('1.234,5 €', 1234.5), ('€1,234.56', 1234.56), ('12', 12.0)):
            self.assertAlmostEqual(parse_price(text), value, msg=text)
        self.assertIsNone(parse_price('n/a'))

    def test_lifecycle(self):
        for text, state in (('Active', 'active'), ('Not For New Designs', 'nrnd'),
                            ('End of Life', 'eol'), ('Last Time Buy', 'eol'), ('Obsolete', 'obsolete'),
                            ('NO_LONGER_MANUFACTURED', 'obsolete'), ('New Product', 'new'),
                            ('normal', 'active'), ('', 'unknown')):
            self.assertEqual(lifecycle_of(text), state, text)


# ---------------------------------------------------------------------------
# Distributor clients on canned answers
# ---------------------------------------------------------------------------

def fake_http(routes):
    """http_request replacement: the first route whose key is in the URL answers."""
    calls = []

    def _http(method, url, params=None, form=None, body=None, headers=None, timeout=0):
        calls.append((method, url, params, form, body, headers))
        for key, answer in routes.items():
            if key in url:
                ans = answer(method, url, params, form, body) if callable(answer) else answer
                return ans if len(ans) == 3 else (ans[0], ans[1], {})
        return 404, {'message': 'no route'}, {}
    return _http, calls


class TestClients(unittest.TestCase):
    def test_digikey(self):
        product = {
            'ManufacturerProductNumber': 'GRM21BR71H104KA01L', 'Manufacturer': {'Name': 'Murata'},
            'Description': {'ProductDescription': 'CAP CER 0.1UF 50V X7R 0805'},
            'QuantityAvailable': 50000, 'ProductUrl': 'https://www.digikey.fr/x', 'DatasheetUrl': 'ds.pdf',
            'ProductStatus': {'Status': 'Active'}, 'ManufacturerLeadWeeks': '14',
            'Parameters': [{'ParameterText': 'Capacitance', 'ValueText': '0.1 µF'},
                           {'ParameterText': 'Voltage - Rated', 'ValueText': '50V'},
                           {'ParameterText': 'Temperature Coefficient', 'ValueText': 'X7R'},
                           {'ParameterText': 'Package / Case', 'ValueText': '0805 (2012 Metric)'}],
            'ProductVariations': [
                {'DigiKeyProductNumber': '490-REEL-ND', 'PackageType': {'Name': 'Tape & Reel'},
                 'MinimumOrderQuantity': 4000, 'QuantityAvailableforPackageType': 40000,
                 'StandardPricing': [{'BreakQuantity': 4000, 'UnitPrice': 0.02}]},
                {'DigiKeyProductNumber': '490-1-ND', 'PackageType': {'Name': 'Cut Tape (CT)'},
                 'MinimumOrderQuantity': 1, 'QuantityAvailableforPackageType': 10000,
                 'StandardPricing': [{'BreakQuantity': 1, 'UnitPrice': 0.09},
                                     {'BreakQuantity': 10, 'UnitPrice': 0.05}]}]}
        http, calls = fake_http({
            '/oauth2/token': (200, {'access_token': 'T', 'expires_in': 599}),
            '/search/keyword': (200, {'ExactMatches': [product], 'Products': []})})
        with mock.patch.object(dist, 'http_request', http):
            dk = dist.DigiKey('id', 'secret')
            dk.min_interval = 0
            offers = dk.search_mpn('GRM21BR71H104KA01L')
        self.assertEqual(len(offers), 1)
        o = offers[0]
        self.assertEqual((o.sku, o.moq, o.stock), ('490-1-ND', 1, 10000))   # cut tape, not the reel
        self.assertEqual(o.cost(10)[1], 0.05)
        self.assertEqual((o.lifecycle, o.lead_time, o.currency), ('active', '14 weeks', 'EUR'))
        self.assertIn('0805', o.package)
        headers = calls[-1][5]
        self.assertEqual(headers['Authorization'], 'Bearer T')
        self.assertEqual(headers['X-DIGIKEY-Locale-Currency'], 'EUR')

    def test_digikey_bad_key(self):
        http, _ = fake_http({'/oauth2/token': (401, {'error_description': 'invalid client'})})
        with mock.patch.object(dist, 'http_request', http):
            dk = dist.DigiKey('id', 'bad')
            self.assertIn('invalid client', dk.test())

    def test_mouser(self):
        part = {'MouserPartNumber': '844-IRF840PBF', 'ManufacturerPartNumber': 'IRF840PBF',
                'Manufacturer': 'Vishay', 'Description': 'MOSFET N-Ch 500V 8A TO-220AB',
                'AvailabilityInStock': '1 234', 'Min': '1', 'Mult': '1', 'LeadTime': '84 Days',
                'LifecycleStatus': 'End of Life', 'SuggestedReplacement': 'IRF840APBF',
                'PriceBreaks': [{'Quantity': 1, 'Price': '1,23 €', 'Currency': 'EUR'},
                                {'Quantity': 10, 'Price': '1,05 €', 'Currency': 'EUR'}],
                'ProductAttributes': [{'AttributeName': 'Vds - Drain-Source Breakdown Voltage',
                                       'AttributeValue': '500 V'},
                                      {'AttributeName': 'Package / Case', 'AttributeValue': 'TO-220-3'}],
                'ProductDetailUrl': 'https://www.mouser.fr/x'}
        repl = dict(part, MouserPartNumber='844-IRF840APBF', ManufacturerPartNumber='IRF840APBF',
                    LifecycleStatus=None, SuggestedReplacement='')

        def answer(method, url, params, form, body):
            mpn = body['SearchByPartRequest']['mouserPartNumber']
            return 200, {'Errors': [], 'SearchResults': {'Parts': [part if mpn == 'IRF840PBF' else repl]}}
        http, calls = fake_http({'/search/partnumber': answer})
        with mock.patch.object(dist, 'http_request', http):
            mo = dist.Mouser('KEY')
            mo.min_interval = 0
            o = mo.search_mpn('IRF840PBF')[0]
            self.assertEqual((o.stock, o.lifecycle, o.replacement), (1234, 'eol', 'IRF840APBF'))
            self.assertEqual(o.cost(10)[1], 1.05)
            alts = mo.alternates(o)
        self.assertEqual([a.mpn for a in alts], ['IRF840APBF'])
        self.assertEqual(calls[0][2], {'apiKey': 'KEY'})

    def test_prices_in_another_currency_are_converted(self):
        def part(currency, price):
            return {'MouserPartNumber': '81-GRM188R71H104KA3D', 'ManufacturerPartNumber': 'GRM188R71H104KA93D',
                    'Manufacturer': 'Murata', 'AvailabilityInStock': '5000', 'Min': '1', 'Mult': '1',
                    'PriceBreaks': [{'Quantity': 1, 'Price': price, 'Currency': currency}]}
        for currency, price, rates, expect in (('USD', '$0.10', {'USD': 0.9}, 0.09),
                                               ('EUR', '0,10 €', {}, 0.10),
                                               ('XYZ', '0.10', {'USD': 0.9}, None)):
            http, _ = fake_http({'/search/partnumber': (200, {'Errors': [], 'SearchResults': {
                'Parts': [part(currency, price)]}})})
            with mock.patch.object(dist, 'http_request', http):
                mo = dist.Mouser('KEY')
                mo.min_interval = 0
                mo.rates.update(rates)
                o = mo.search_mpn('GRM188R71H104KA93D')[0]
            self.assertEqual(o.currency, 'EUR')
            if expect is None:                       # no rate: never added to euros
                self.assertEqual(o.prices, [])
                self.assertIn('no exchange rate', o.note)
                self.assertIsNone(choose([o], 10, Settings()))
            else:
                self.assertAlmostEqual(o.cost(1)[1], expect)
                self.assertEqual('converted' in o.note, currency == 'USD')
            # the cached answer is in euros too, and not converted twice
            again = mo.search_mpn('GRM188R71H104KA93D')[0]
            self.assertEqual(again.prices, o.prices)

    def test_ecb_rates(self):
        xml = ("<Cube time='2026-10-02'><Cube currency='USD' rate='1.1000'/>"
               "<Cube currency='GBP' rate='0.8000'/></Cube>")
        rates, day = dist.parse_ecb(xml)
        self.assertEqual(day, '2026-10-02')
        self.assertAlmostEqual(rates['USD'], 1 / 1.1)
        self.assertAlmostEqual(rates['GBP'], 1.25)
        self.assertEqual(rates['EUR'], 1.0)
        cache = dist.Cache()
        http, calls = fake_http({'eurofxref': (200, xml)})
        with mock.patch.object(dist, 'http_request', http):
            self.assertAlmostEqual(dist.usd_to_eur(cache)[0], 1 / 1.1)
            self.assertAlmostEqual(dist.ecb_rates(cache)[0]['GBP'], 1.25)
        self.assertEqual(len(calls), 1)                      # one call, then the cache
        down, _ = fake_http({'eurofxref': (503, 'down')})
        with mock.patch.object(dist, 'http_request', down):
            self.assertEqual(dist.ecb_rates(), ({'EUR': 1.0}, ''))
            self.assertEqual(dist.usd_to_eur(), (0.92, ''))

    def test_mouser_errors(self):
        http, _ = fake_http({'/search/': (200, {'Errors': [{'Code': 'Invalid',
                                                           'Message': 'Invalid unique identifier.'}]})})
        with mock.patch.object(dist, 'http_request', http):
            mo = dist.Mouser('BAD')
            mo.min_interval = 0
            with self.assertRaises(DistributorError):
                mo.search_mpn('X')

    def test_farnell(self):
        data = {'manufacturerPartNumberSearchReturn': {'numberOfResults': 1, 'products': [{
            'sku': '1469912', 'displayName': 'VISHAY IRF840PBF MOSFET', 'brandName': 'VISHAY',
            'translatedManufacturerPartNumber': 'IRF840PBF', 'translatedMinimumOrderQuality': 5,
            'productStatus': 'STOCKED', 'stock': {'level': 321, 'leastLeadTime': 0},
            'prices': [{'from': 5, 'to': 49, 'cost': 1.4}, {'from': 50, 'to': 99, 'cost': 1.2}],
            'attributes': [{'attributeLabel': 'Drain Source Voltage Vds', 'attributeUnit': 'V',
                            'attributeValue': '500'}],
            'datasheets': [{'url': 'https://www.farnell.com/datasheets/1.pdf'}]}]}}
        http, calls = fake_http({'api.element14.com': (200, data)})
        with mock.patch.object(dist, 'http_request', http):
            fa = dist.Farnell('KEY')
            fa.min_interval = 0
            o = fa.search_mpn('IRF840PBF')[0]
        self.assertEqual((o.sku, o.stock, o.moq), ('1469912', 321, 5))
        self.assertEqual(o.cost(2), (5, 1.4, 7.0))
        self.assertEqual(calls[0][2]['term'], 'manuPartNum:IRF840PBF')
        self.assertEqual(calls[0][2]['storeInfo.id'], 'fr.farnell.com')
        # a store in pounds: converted, never taken for euros
        with mock.patch.object(dist, 'http_request', http):
            uk = dist.Farnell('KEY', store='uk.farnell.com')
            uk.min_interval = 0
            uk.rates.update({'GBP': 1.15})
            o = uk.search_mpn('IRF840PBF')[0]
        self.assertEqual((uk.currency, o.currency), ('GBP', 'EUR'))
        self.assertAlmostEqual(o.cost(2)[1], 1.4 * 1.15)

    def test_tme(self):
        http, calls = fake_http({
            '/auth/token': (200, {'access_token': 'A', 'expires_in': 3600}),
            '/products/data': (200, {'status': 'OK', 'data': {'products': [
                {'symbol': 'IRF840PBF-VIS', 'prices': [{'amount': 1, 'price': 1.31}, {'amount': 25, 'price': 1.1}],
                 'stock': 950}]}}),
            '/products': (200, {'status': 'OK', 'data': {'products': [
                {'symbol': 'IRF840PBF-VIS', 'original_symbol': 'IRF840PBF', 'producer': 'VISHAY',
                 'description': 'Transistor N-MOSFET 500V 8A', 'min_amount': 1, 'multiples': 1,
                 'statuses': []}]}})})
        with mock.patch.object(dist, 'http_request', http):
            tm = dist.TME('tok', 'sec')
            tm.min_interval = 0
            o = tm.search_mpn('IRF840PBF')[0]
        self.assertEqual((o.sku, o.mpn, o.stock), ('IRF840PBF-VIS', 'IRF840PBF', 950))
        self.assertEqual(o.cost(30)[1], 1.1)
        auth = calls[0][5]['Authorization']
        self.assertTrue(auth.startswith('Basic '))

    def test_lcsc(self):
        jlc = {'code': 200, 'data': {'componentPageInfo': {'list': [
            {'componentCode': 'C25804', 'componentModelEn': '0603WAF1002T5E',
             'componentBrandEn': 'UNI-ROYAL', 'componentLibraryType': 'base', 'stockCount': 2900000},
            {'componentCode': 'C9900298159', 'componentModelEn': '0603WAF1002T5E',
             'componentBrandEn': 'JLCPCB Assembly'}]}}}
        detail = {'code': 200, 'result': {
            'productCode': 'C25804', 'productModel': '0603WAF1002T5E', 'brandNameEn': 'UNI-ROYAL',
            'productIntroEn': '10kΩ ±1% 100mW 0603 Thick Film Resistor', 'encapStandard': '0603',
            'stockNumber': 150000, 'minBuyNumber': 100, 'split': 100, 'currencyType': 'USD',
            'productCycle': 'normal', 'pdfUrl': 'x.pdf',
            'productPriceList': [{'ladder': 100, 'usdPrice': 0.002}, {'ladder': 1000, 'usdPrice': 0.0015}],
            'paramVOList': [{'paramNameEn': 'Resistance', 'paramValueEn': '10kΩ'},
                            {'paramNameEn': 'Tolerance', 'paramValueEn': '±1%'}],
            'alternatePartList': [{'productCode': 'C965184', 'productModel': 'NQ03WAF1002T5E',
                                   'stockNumber': 349300, 'minBuyNumber': 100, 'split': 100,
                                   'productPriceList': [{'ladder': 100, 'usdPrice': 0.002}]}]}}
        http, _ = fake_http({'selectSmtComponentList': (200, jlc), 'product/detail': (200, detail)})
        with mock.patch.object(dist, 'http_request', http):
            lc = dist.LCSC(usd_to_eur=0.9)
            lc.min_interval = 0
            offers = lc.search_mpn('0603WAF1002T5E')
            alts = lc.alternates(offers[0])
        self.assertEqual(len(offers), 1)                      # not JLCPCB's own consigned stock
        o = offers[0]
        self.assertEqual((o.sku, o.moq, o.multiple, o.stock, o.lifecycle), ('C25804', 100, 100, 150000, 'active'))
        self.assertAlmostEqual(o.prices[0].price, 0.0018)     # USD -> EUR
        self.assertIn('basic', o.note)
        self.assertEqual(alts[0].sku, 'C965184')

    def test_retry_after_429(self):
        answers = [(429, {'message': 'slow down'}, {'Retry-After': '0'}), (200, {'ok': True}, {})]
        with mock.patch.object(dist, 'http_request', lambda *a, **k: answers.pop(0)), \
                mock.patch.object(dist.time, 'sleep', lambda s: None):
            d = Distributor()
            d.name, d.min_interval = 'X', 0
            self.assertEqual(d._call('GET', 'https://x'), {'ok': True})

    def test_cache(self):
        path = os.path.join(tempfile.mkdtemp(), 'c.json')
        c = dist.Cache(path)
        fd = FakeDist('Mouser', by_mpn={'ABC': [offer('Mouser', 'M-ABC', 'ABC')]})
        fd.cache = c
        fd.search_mpn('abc')
        fd.search_mpn('ABC')
        self.assertEqual(len(fd.asked), 1)
        c.save()
        self.assertEqual(dist.Cache(path).get('Mouser|mpn|ABC')[0]['sku'], 'M-ABC')


# ---------------------------------------------------------------------------
# BOM
# ---------------------------------------------------------------------------

class TestBom(unittest.TestCase):
    def test_grouping_and_fields(self):
        parts = [('R2', '10k', 'Resistor_SMD:R_0805_2012Metric', {'Mfr. No': 'RC0805FR-0710KL'}, '/'),
                 ('R1', '10k', 'Resistor_SMD:R_0805_2012Metric', {'MFR_PN': 'RC0805FR-0710KL',
                                                                   'LCSC Part #': 'C84376'}, '/'),
                 ('R10', '10k', 'Resistor_SMD:R_0805_2012Metric', {}, '/'),
                 ('C1', '10k', 'Resistor_SMD:R_0805_2012Metric', {}, '/')]
        lines = group_parts(parts)
        self.assertEqual([l.designators for l in lines], ['C1', 'R1, R2', 'R10'])
        r = lines[1]
        self.assertEqual((r.mpn, r.skus.get('LCSC'), r.package, r.kind), ('RC0805FR-0710KL', 'C84376', '0805', 'R'))

    def test_compact_refs(self):
        self.assertEqual(compact_refs(['R5', 'R1', 'R2', 'R3', 'R10', 'R11', 'C1']),
                         'C1, R1-R3, R5, R10, R11')

    def test_packages(self):
        self.assertEqual(package_of('Package_TO_SOT_THT:TO-220-3_Vertical'), 'TO-220-3')
        self.assertEqual(package_of('Capacitor_SMD:CP_Elec_6.3x5.4'), 'SMD elec 6.3x5.4')
        self.assertEqual(find_field({'Fabricant': 'Vishay'}, ('Manufacturer', 'Fabricant')),
                         ('Fabricant', 'Vishay'))


# ---------------------------------------------------------------------------
# Matching and replacement rules
# ---------------------------------------------------------------------------

class TestRules(unittest.TestCase):
    def test_values_and_packages(self):
        self.assertEqual(parse_value('4k7', 'R'), 4700)
        self.assertAlmostEqual(parse_value('2n2', 'C'), 2.2e-9)
        self.assertEqual(package_tokens('8-SOIC (0.154", 3.90mm Width)'), {'SOIC-8'})
        self.assertEqual(package_tokens('TO-220F-3'), {'TO-220F-3'})
        self.assertEqual(package_tokens('TO-220-3 Full Pack, Isolated Tab'), {'TO-220F-3'})
        self.assertEqual(package_tokens('ITO-220AB'), {'TO-220F-3'})
        self.assertEqual(package_tokens('TO-247-4'), {'TO-247-4'})
        self.assertEqual(package_tokens('16-SOIC (0.295", 7.50mm Width)'), {'SOIC-16W'})
        self.assertEqual(package_tokens('TO-252-3, DPak (2 Leads + Tab), SC-63') & {'TO-252-2'}, {'TO-252-2'})
        to220 = 'Package_TO_SOT_THT:TO-220-3_Vertical'
        self.assertFalse(same_package(to220, offer('X', 'a', 'b', package='TO-220F-3')))
        self.assertFalse(same_package(to220, offer('X', 'a', 'b', package='TO-220-3 Full Pack, Isolated Tab')))
        self.assertTrue(same_package(to220, offer('X', 'a', 'b', package='TO-220AB')))
        sot = 'Package_TO_SOT_SMD:SOT-23'
        self.assertTrue(same_package(sot, offer('X', 'a', 'b', package='SOT-23-3')))
        self.assertFalse(same_package(sot, offer('X', 'a', 'b', package='SOT-23-5')))
        self.assertTrue(same_package('Package_TO_SOT_SMD:TO-252-2',
                                     offer('X', 'a', 'b', package='TO-252-3, DPak (2 Leads + Tab)')))

    def test_through_hole_packages(self):
        radial = 'Capacitor_THT:CP_Radial_D10.0mm_P5.00mm'
        self.assertTrue(same_package(radial, offer('LCSC', 'a', 'b', package='Plugin,D10xL20mm')))
        self.assertFalse(same_package(radial, offer('LCSC', 'a', 'b', package='Plugin,D18xL32mm')))
        self.assertTrue(same_package(radial, offer('DigiKey', 'a', 'b', package='Radial, Can', params={
            'Lead Spacing': '0.197" (5.00mm)', 'Size / Dimension': '0.394" Dia (10.00mm)',
            'Mounting Type': 'Through Hole'})))
        axial = 'Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal'
        self.assertFalse(same_package(axial, offer('LCSC', 'a', 'b', package='0603')))
        disc = 'Capacitor_THT:C_Disc_D7.5mm_W5.0mm_P5.00mm'
        self.assertFalse(same_package(disc, offer('LCSC', 'a', 'b', package='Plugin,P=7.5mm')))
        self.assertTrue(same_package(disc, offer('LCSC', 'a', 'b', package='Plugin,P=5mm')))
        self.assertFalse(same_package('Capacitor_SMD:CP_Elec_6.3x5.4',
                                      offer('LCSC', 'a', 'b', package='SMD,D6.3xL7.7mm')))
        # a footprint that tells nothing: cannot be checked, so not proposed
        self.assertIsNone(same_package('MyLib:CAP_D10', offer('LCSC', 'a', 'b', package='Plugin,D10xL20mm')))

    def test_body_size_and_row_spacing(self):
        qfn = 'Package_DFN_QFN:QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm'
        dip28 = 'Package_DIP:DIP-28_W15.24mm'

        def o(pkg, **params):
            return offer('X', 's', 'm', package=pkg, params=dict(params, **{'Package / Case': pkg}))
        self.assertTrue(same_package(qfn, o('32-VFQFN Exposed Pad', **{'Supplier Device Package': '32-QFN (5x5)'})))
        self.assertFalse(same_package(qfn, o('32-VFQFN Exposed Pad', **{'Supplier Device Package': '32-VQFN (7x7)'})))
        self.assertTrue(same_package(qfn, o('QFN-32')))                  # body unknown: the code decides
        self.assertFalse(same_package('Package_QFP:LQFP-64_10x10mm_P0.5mm', o('LQFP-64(14x14)')))
        self.assertTrue(same_package(dip28, o('28-DIP (0.600", 15.24mm)')))
        self.assertFalse(same_package(dip28, o('28-DIP (0.300", 7.62mm)')))
        self.assertTrue(same_package('Package_DIP:DIP-8_W7.62mm', o('PDIP-8')))
        self.assertFalse(same_package('Package_DIP:DIP-4_W7.62mm', o('4-DIP (0.400", 10.16mm)')))

    def test_capital_milli_and_safety_spelling(self):
        r = ratings_of('U', 'IC REG LINEAR 3.3V 150MA SOT23-5')
        self.assertAlmostEqual(r.current, 0.15)
        self.assertEqual(ratings_of('C', '', value_text='2.2nF X1Y2').safety, 'Y2')
        self.assertEqual(ratings_of('C', 'CAP_Y2 2.2nF').safety, 'Y2')
        self.assertEqual(ratings_of('Q', '', {'Drive Voltage (Max Rds On, Min Rds On)': '10V',
                                              'Rds On (Max) @ Id, Vgs': '160 MOHM @ 5A, 10V'}).rds_on, 0.16)

    def test_power_electronics_rules(self):
        y2 = ratings_of('C', '', {'Capacitance': '2200pF', 'Voltage - Rated': '300VAC', 'Ratings': 'X1, Y2'})
        x2 = ratings_of('C', '', {'Capacitance': '2200pF', 'Voltage - Rated': '310VAC', 'Ratings': 'X2'})
        y1 = ratings_of('C', '', {'Capacitance': '2200pF', 'Voltage - Rated': '400VAC', 'Ratings': 'Y1'})
        self.assertTrue(replacement_issues('C', y2, x2))       # an X2 is no Y capacitor
        self.assertEqual(replacement_issues('C', y2, y1), [])  # Y1 is better than Y2
        m600 = ratings_of('Q', '', {'Drain to Source Voltage (Vdss)': '600V',
                                    'Current - Continuous Drain (Id) @ 25°C': '10A'})
        m500 = ratings_of('Q', '', {'Drain to Source Voltage (Vdss)': '500V',
                                    'Current - Continuous Drain (Id) @ 25°C': '12A'})
        self.assertIn('voltage 500 V < 600 V', replacement_issues('Q', m600, m500))
        r1 = ratings_of('R', '', {'Resistance': '10 kOhms', 'Tolerance': '±1%', 'Power (Watts)': '0.25W'})
        r5 = ratings_of('R', '', {'Resistance': '10 kOhms', 'Tolerance': '±5%', 'Power (Watts)': '0.25W'})
        r1w = ratings_of('R', '', {'Resistance': '10 kOhms', 'Tolerance': '±1%', 'Power (Watts)': '0.125W'})
        self.assertTrue(replacement_issues('R', r1, r5))
        self.assertTrue(replacement_issues('R', r1, r1w))
        x5 = ratings_of('C', 'CAP CER 1UF 25V X5R 0805')
        y5 = ratings_of('C', 'CAP CER 1UF 25V Y5V 0805')
        self.assertTrue(replacement_issues('C', x5, y5))


# ---------------------------------------------------------------------------
# The whole sourcing
# ---------------------------------------------------------------------------

def line(refs, value, footprint, **fields):
    return group_parts([(r, value, footprint, dict(fields), '/') for r in refs])[0]


class TestReviewFixes(unittest.TestCase):
    def test_grouping_keeps_ratings_apart(self):
        parts = [('C1', '100nF', 'Capacitor_SMD:C_0805_2012Metric', {'Voltage': '16V'}, '/'),
                 ('C7', '100nF', 'Capacitor_SMD:C_0805_2012Metric', {'Voltage': '100V'}, '/'),
                 ('C8', '100nF', 'Capacitor_SMD:C_0805_2012Metric', {'Voltage': '100 V'}, '/')]
        self.assertEqual([l.designators for l in group_parts(parts)], ['C1', 'C7, C8'])

    def test_kicost_fields(self):
        self.assertEqual(find_field({'manf#': 'IRF840PBF', 'manf': 'Vishay'}, ('MPN', 'Manf#'))[1], 'IRF840PBF')
        self.assertEqual(find_field({'manf#': 'IRF840PBF', 'manf': 'Vishay'}, ('Manufacturer', 'Manf'))[1], 'Vishay')

    def test_one_minimum_order_per_part(self):
        a = line(['R1', 'R2'], '10k', 'Resistor_SMD:R_0805_2012Metric', MPN='RC0805')
        b = line(['R7'], '10K', 'Resistor_SMD:R_0805_2012Metric', MPN='RC0805')
        lc = FakeDist('LCSC', by_mpn={'RC0805': [offer('LCSC', 'C17414', 'RC0805', prices=((100, 0.002),),
                                                       moq=100, multiple=100)]})
        res = Sourcer([lc], Settings(boards=1, spare_percent=0)).run([a, b])
        rows = list(csv.reader(order_csv(res).splitlines()))
        self.assertEqual(len(rows), 2)                       # one row for the part
        self.assertEqual(rows[1][1], '100')                  # 3 needed, one minimum order
        self.assertEqual(rows[1][2], 'R1, R2, R7')
        self.assertAlmostEqual(sum(r.order[2] for r in res), 0.2)
        self.assertAlmostEqual(sum(r.extra_cost for r in res), 97 * 0.002)

    def test_stock_against_quantity_ordered(self):
        q = line(['R1'], '10k', 'Resistor_SMD:R_0805_2012Metric', MPN='X')
        lc = FakeDist('LCSC', by_mpn={'X': [offer('LCSC', 'C1', 'X', stock=150, moq=100, multiple=100,
                                                   prices=((100, 0.01),))]})
        r = Sourcer([lc], Settings(boards=120, spare_percent=0)).run([q])[0]
        self.assertEqual(r.order[0], 200)
        self.assertEqual(r.status, 'short')

    def test_a_failing_line_keeps_the_others(self):
        class Bad(FakeDist):
            def _search_mpn(self, mpn, manufacturer=''):
                if mpn == 'BOOM':
                    raise RuntimeError('IncompleteRead')
                return super()._search_mpn(mpn)
        d = Bad('LCSC', by_mpn={'OK1': [offer('LCSC', 'C1', 'OK1', package='TO-220-3')]})
        lines = [line(['Q1'], 'x', 'Package_TO_SOT_THT:TO-220-3_Vertical', MPN='BOOM'),
                 line(['Q2'], 'x', 'Package_TO_SOT_THT:TO-220-3_Vertical', MPN='OK1')]
        res = Sourcer([d], Settings()).run(lines)
        self.assertEqual([r.status for r in res], ['not found', 'ok'])
        self.assertTrue(any('IncompleteRead' in e for e in res[0].errors))

    def test_lcsc_prices_always_converted_from_dollars(self):
        lc = dist.LCSC(usd_to_eur=0.9)
        o = lc._offer({'productCode': 'C1', 'currencyType': 'EUR', 'productPriceList': [
            {'ladder': 100, 'usdPrice': 0.0019, 'currencyPrice': 0.0017}]})
        self.assertAlmostEqual(o.prices[0].price, 0.00171)

    def test_jlcpcb_errors_are_not_cached_as_not_found(self):
        http, _ = fake_http({'selectSmtComponentList': (200, {'code': 101, 'message': 'retry later'})})
        with mock.patch.object(dist, 'http_request', http):
            lc = dist.LCSC()
            lc.min_interval = 0
            with self.assertRaises(DistributorError):
                lc.search_mpn('ABC')
        self.assertIsNone(lc.cache.get('LCSC|mpn|ABC'))

    def test_write_fields_keeps_the_designers_figures(self):
        from plugin.bom import write_fields

        class F:
            def __init__(self, name, text):
                self.name, self.text, self.visible = name, text, True

            def GetName(self):
                return self.name

            def GetText(self):
                return self.text

            def SetText(self, t):
                self.text = t

        class FP:
            def __init__(self):
                self.fields = [F('Voltage', '63V'), F('MPN', 'OLD')]

            def GetReference(self):
                return 'C1'

            def GetFields(self):
                return self.fields

        class B:
            def __init__(self):
                self.fp = FP()

            def GetFootprints(self):
                return [self.fp]
        b = B()
        n = write_fields(b, ['C1'], {'Voltage': '100 V', 'MPN': 'NEW'}, fill_only=('Voltage',))
        self.assertEqual(n, 1)
        self.assertEqual([f.text for f in b.fp.fields], ['63V', 'NEW'])


class TestSourcer(unittest.TestCase):
    def test_priority_and_cheapest(self):
        q = line(['Q1'], 'IRF840', 'Package_TO_SOT_THT:TO-220-3_Vertical', MPN='IRF840PBF')
        mouser = FakeDist('Mouser', by_mpn={'IRF840PBF': [offer('Mouser', 'M1', 'IRF840PBF', stock=5,
                                                                prices=((1, 1.0),), package='TO-220-3')]})
        farnell = FakeDist('Farnell', by_mpn={'IRF840PBF': [offer('Farnell', 'F1', 'IRF840PBF', stock=500,
                                                                  prices=((1, 1.5),), package='TO-220-3')]})
        lcsc = FakeDist('LCSC', by_mpn={'IRF840PBF': [offer('LCSC', 'C1', 'IRF840PBF', stock=500,
                                                            prices=((1, 0.6),), package='TO-220-3')]})
        s = Settings(boards=10, spare_percent=0, order=('Mouser', 'Farnell', 'LCSC'))
        r = Sourcer([mouser, farnell, lcsc], s).source_line(q)
        self.assertEqual(r.chosen.distributor, 'Farnell')      # Mouser first, but only 5 in stock
        self.assertEqual(r.status, 'ok')
        s.strategy = 'cheapest'
        self.assertEqual(Sourcer([mouser, farnell, lcsc], s).source_line(q).chosen.distributor, 'LCSC')

    def test_end_of_life_gets_checked_replacements(self):
        q = line(['Q1'], 'MOSFET', 'Package_TO_SOT_THT:TO-220-3_Vertical', MPN='OLD600')
        old = offer('Mouser', 'M-OLD', 'OLD600', lifecycle='obsolete', package='TO-220-3',
                    params={'Drain to Source Voltage (Vdss)': '600V'})
        good = offer('Mouser', 'M-NEW', 'NEW650', package='TO-220-3',
                     params={'Drain to Source Voltage (Vdss)': '650V'}, note='Mouser suggested replacement')
        weak = offer('Mouser', 'M-W', 'WEAK500', package='TO-220-3', params={'Drain to Source Voltage (Vdss)': '500V'})
        fullpack = offer('Mouser', 'M-F', 'FP650', package='TO-220F-3', params={'Drain to Source Voltage (Vdss)': '650V'})
        mo = FakeDist('Mouser', by_mpn={'OLD600': [old]}, alts={'OLD600': [weak, fullpack, good]})
        r = Sourcer([mo], Settings()).source_line(q)
        self.assertEqual(r.status, 'risk')
        self.assertEqual([a.offer.mpn for a in r.alternates], ['NEW650'])

    def test_no_mpn_resistor_search(self):
        rl = line(['R1', 'R2'], '10k', 'Resistor_SMD:R_0805_2012Metric')
        five = offer('LCSC', 'C5', 'R5PCT', package='0805', params={'Resistance': '10kΩ', 'Tolerance': '±5%'})
        one = offer('LCSC', 'C1', 'R1PCT', package='0805', params={'Resistance': '10kΩ', 'Tolerance': '±1%'})
        wrong = offer('LCSC', 'C9', 'R1K', package='0805', params={'Resistance': '1kΩ', 'Tolerance': '±1%'})
        big = offer('LCSC', 'C7', 'R1206', package='1206', params={'Resistance': '10kΩ', 'Tolerance': '±1%'})
        lc = FakeDist('LCSC', by_kw={'*': [five, wrong, big, one]})
        r = Sourcer([lc], Settings()).source_line(rl)
        self.assertEqual(r.status, 'proposed')
        self.assertEqual([c.mpn for c in r.candidates], ['R1PCT'])       # 1 % by default
        self.assertIn('10k 0805 resistor', lc.asked[0][1])

    def test_value_as_part_number_and_sku_field(self):
        u = line(['U1'], 'UC3843BD1R2G', 'Package_SO:SOIC-8_3.9x4.9mm_P1.27mm')
        lc = FakeDist('LCSC', by_mpn={'UC3843BD1R2G': [offer('LCSC', 'C16414', 'UC3843BD1R2G', package='SOIC-8')]})
        r = Sourcer([lc], Settings()).source_line(u)
        self.assertEqual((r.status, r.chosen.sku), ('ok', 'C16414'))
        # a distributor part number in the fields finds the MPN, then the others are asked
        r5 = line(['R5'], '10k', 'Resistor_SMD:R_0603_1608Metric', LCSC='C25804')
        lc2 = FakeDist('LCSC', by_sku={'C25804': [offer('LCSC', 'C25804', '0603WAF1002T5E', package='0603')]})
        mo = FakeDist('Mouser', by_mpn={'0603WAF1002T5E': [offer('Mouser', 'M5', '0603WAF1002T5E', package='0603')]})
        res = Sourcer([lc2, mo], Settings()).source_line(r5)
        self.assertEqual(sorted(o.distributor for o in res.offers), ['LCSC', 'Mouser'])

    def test_package_variant_that_fits_the_footprint(self):
        u = line(['U2'], 'PC817C', 'Package_DIP:DIP-4_W7.62mm', MPN='PC817C')
        smd = offer('LCSC', 'C-SMD', 'PC817C', prices=((1, 0.05),), package='SMD-4P')
        dip = offer('Mouser', 'M-DIP', 'PC817C', prices=((1, 0.20),), package='DIP-4')
        self.assertIs(same_package(u.footprint, smd), False)     # through-hole footprint, SMD part
        s = Settings(order=('LCSC', 'Mouser'))
        r = Sourcer([FakeDist('LCSC', by_mpn={'PC817C': [smd]}),
                     FakeDist('Mouser', by_mpn={'PC817C': [dip]})], s).source_line(u)
        self.assertEqual((r.chosen.sku, r.package_warning), ('M-DIP', ''))   # LCSC first, but SMD
        # only the wrong variant: it is kept, and flagged everywhere
        r = Sourcer([FakeDist('LCSC', by_mpn={'PC817C': [smd]})], s).source_line(u)
        self.assertEqual(r.chosen.sku, 'C-SMD')
        self.assertIn('check the package: SMD-4P', r.package_warning)
        self.assertIn('check the package', bom_rows([r])[0][-1])
        self.assertIn('package to check', build_report('demo', [r], s, '2026-10-04'))

    def test_errors_do_not_stop_the_run(self):
        class Broken(FakeDist):
            def _search_mpn(self, mpn, manufacturer=''):
                raise DistributorError('Broken: HTTP 401 invalid key')
        q = line(['Q1'], 'X', 'Package_TO_SOT_THT:TO-220-3_Vertical', MPN='ABC')
        ok = FakeDist('LCSC', by_mpn={'ABC': [offer('LCSC', 'C1', 'ABC', package='TO-220-3')]})
        r = Sourcer([Broken('DigiKey'), ok], Settings()).source_line(q)
        self.assertEqual(r.chosen.sku, 'C1')
        self.assertIn('Broken: HTTP 401 invalid key', r.errors)


class TestExports(unittest.TestCase):
    def results(self):
        q = line(['Q1', 'Q2'], 'IRF840', 'Package_TO_SOT_THT:TO-220-3_Vertical', MPN='IRF840PBF')
        rl = line(['R%d' % i for i in range(1, 30)], '10k', 'Resistor_SMD:R_0805_2012Metric', MPN='RC0805')
        lc = FakeDist('LCSC', by_mpn={
            'IRF840PBF': [offer('LCSC', 'C537842', 'IRF840PBF', prices=((1, 0.63),), package='TO-220-3',
                                params={'Drain to Source Voltage (Vdss)': '500V'})],
            'RC0805': [offer('LCSC', 'C17414', 'RC0805', prices=((100, 0.002),), moq=100, multiple=100)]})
        return Sourcer([lc], Settings(boards=2, spare_percent=0)).run([q, rl])

    def test_bom_and_order_lists(self):
        res = self.results()
        rows = bom_rows(res)
        self.assertEqual(rows[0][0], 'Q1, Q2')
        self.assertEqual(rows[1][3], '100')                     # 58 needed, 100 minimum
        self.assertEqual(rows[1][12], '100')
        self.assertAlmostEqual(res[1].extra_cost, 42 * 0.002)
        text = order_csv(res)
        reader = list(csv.reader(text.splitlines()))
        self.assertEqual(reader[0][:3], ['Distributor PN', 'Quantity', 'Customer reference'])
        self.assertEqual(reader[1][:3], ['C537842', '4', 'Q1, Q2'])
        self.assertLessEqual(len(reader[2][2]), 48)              # designators fit the customer reference
        path = os.path.join(tempfile.mkdtemp(), 'bom.csv')
        write_bom_csv(path, res)
        with open(path, encoding='utf-8-sig') as f:
            first = f.readline()
        self.assertIn(';', first)
        page = build_report('demo', res, Settings(boards=2), '2026-10-04')
        self.assertIn('due to minimum orders', page)
        self.assertIn('Q1, Q2', page)
        fields = fields_for(res[0])
        self.assertEqual((fields['MPN'], fields['LCSC'], fields['Voltage']), ('IRF840PBF', 'C537842', '500 V'))

    def test_choose_skips_obsolete(self):
        a = offer('Mouser', 'a', 'X', lifecycle='obsolete', prices=((1, 0.01),))
        b = offer('Farnell', 'b', 'X', prices=((1, 0.5),))
        self.assertEqual(choose([a, b], 1, Settings(strategy='cheapest')).sku, 'b')


if __name__ == '__main__':
    unittest.main()
