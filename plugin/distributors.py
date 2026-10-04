"""Distributor clients: DigiKey, Mouser, Farnell (element14), TME and LCSC.

Each client turns its distributor's answer into the same `Offer`: stock,
price breaks, minimum order quantity and multiple, lifecycle, lead time,
parameters, and replacement hints. Standard library only (urllib); every
call goes from the user's computer to the distributor with the user's own
free API key. LCSC needs no key (public JLCPCB / LCSC catalogue endpoints).

Answers are cached for a day, calls are spaced to stay inside each API's
limits, and a 429 answer is retried once after the delay it asks for.
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

USER_AGENT = 'place-news/0.6 (KiCad plugin; +https://github.com/remiblokker/CadMust-Neo)'
TIMEOUT = 25


class DistributorError(Exception):
    pass


# ---------------------------------------------------------------------------
# Normalised offer
# ---------------------------------------------------------------------------

@dataclass
class PriceBreak:
    qty: int
    price: float


@dataclass
class Offer:
    distributor: str
    sku: str
    mpn: str
    manufacturer: str = ''
    description: str = ''
    stock: Optional[int] = None
    moq: int = 1
    multiple: int = 1
    prices: List[PriceBreak] = field(default_factory=list)
    currency: str = 'EUR'
    lead_time: str = ''
    lifecycle: str = 'unknown'      # active | new | nrnd | eol | obsolete | unknown
    url: str = ''
    datasheet: str = ''
    package: str = ''
    params: Dict[str, str] = field(default_factory=dict)
    replacement: str = ''           # replacement suggested by the distributor
    packaging: str = ''
    note: str = ''                  # e.g. "JLCPCB basic part"

    def order_qty(self, need: int) -> int:
        """What must be bought for `need` parts: at least the minimum order
        and the first price break, in whole multiples."""
        first = min((b.qty for b in self.prices), default=1)
        q = max(int(need), int(self.moq or 1), first, 1)
        mult = max(int(self.multiple or 1), 1)
        if q % mult:
            q += mult - q % mult
        return q

    def unit_price(self, qty: int) -> Optional[float]:
        price = None
        for b in sorted(self.prices, key=lambda b: b.qty):
            if b.qty <= qty:
                price = b.price
        if price is None and self.prices:
            price = sorted(self.prices, key=lambda b: b.qty)[0].price
        return price

    def cost(self, need: int) -> Optional[Tuple[int, float, float]]:
        """(quantity to order, unit price at that quantity, line total)."""
        if not self.prices:
            return None
        q = self.order_qty(need)
        u = self.unit_price(q)
        return (q, u, q * u) if u is not None else None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> 'Offer':
        d = dict(d)
        d['prices'] = [PriceBreak(**p) for p in d.get('prices', [])]
        return Offer(**d)


_LIFECYCLE_WORDS = (
    ('obsolete', 'obsolete'), ('no longer manufactured', 'obsolete'), ('discontinued', 'obsolete'),
    ('end of life', 'eol'), ('last time buy', 'eol'), ('eol', 'eol'), ('while stocks last', 'eol'),
    ('last pieces', 'eol'), ('not recommended', 'nrnd'), ('not for new design', 'nrnd'),
    ('nrnd', 'nrnd'), ('new product', 'new'), ('new', 'new'), ('active', 'active'),
    ('normal', 'active'), ('stocked', 'active'), ('production', 'active'),
)


def lifecycle_of(text: str) -> str:
    t = (text or '').strip().lower().replace('_', ' ')
    if not t:
        return 'unknown'
    for word, state in _LIFECYCLE_WORDS:
        if word in t:
            return state
    return 'unknown'


_NUM_RE = re.compile(r'[-+]?\d[\d\s  .,]*')


def parse_price(text: Any) -> Optional[float]:
    """'0,103 €', '$0.10', '1 234,56 €', '1.234,5' -> float."""
    if isinstance(text, (int, float)):
        return float(text)
    m = _NUM_RE.search(str(text or ''))
    if not m:
        return None
    s = re.sub(r'[\s  ]', '', m.group(0))
    if ',' in s and '.' in s:
        if s.rfind(',') > s.rfind('.'):
            s = s.replace('.', '').replace(',', '.')
        else:
            s = s.replace(',', '')
    elif ',' in s:
        s = s.replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return None


def parse_int(text: Any) -> Optional[int]:
    if isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return int(text)
    m = re.search(r'\d[\d\s  .,]*', str(text or ''))
    if not m:
        return None
    digits = re.sub(r'[^\d]', '', m.group(0))
    return int(digits) if digits else None


def same_mpn(a: str, b: str) -> bool:
    na = re.sub(r'[\s\-_./]', '', a or '').upper()
    nb = re.sub(r'[\s\-_./]', '', b or '').upper()
    return bool(na) and na == nb


# ---------------------------------------------------------------------------
# HTTP, cache, pacing
# ---------------------------------------------------------------------------

def http_request(method: str, url: str, params: Optional[Dict[str, Any]] = None,
                 form: Optional[Dict[str, Any]] = None, body: Any = None,
                 headers: Optional[Dict[str, str]] = None, timeout: float = TIMEOUT
                 ) -> Tuple[int, Any, Dict[str, str]]:
    """(status, decoded JSON or text, response headers)."""
    if params:
        query = urllib.parse.urlencode(params, doseq=True)
        url = url + ('&' if '?' in url else '?') + query
    data = None
    hdrs = {'User-Agent': USER_AGENT, 'Accept': 'application/json'}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs['Content-Type'] = 'application/x-www-form-urlencoded'
    elif body is not None:
        data = json.dumps(body).encode()
        hdrs['Content-Type'] = 'application/json'
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status, rh = resp.status, dict(resp.headers.items())
    except urllib.error.HTTPError as e:
        raw = e.read() if hasattr(e, 'read') else b''
        status, rh = e.code, dict(e.headers.items()) if e.headers else {}
    except urllib.error.URLError as e:
        raise DistributorError(f'network error: {e.reason}') from e
    except (TimeoutError, OSError, http.client.HTTPException, ValueError) as e:
        raise DistributorError(f'network error: {e}') from e
    text = raw.decode('utf-8', errors='replace')
    try:
        return status, json.loads(text), rh
    except ValueError:
        return status, text, rh


class Cache:
    """A small JSON cache of answers (one day by default)."""

    def __init__(self, path: str = '', ttl: float = 24 * 3600):
        self.path, self.ttl = path, ttl
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {}
        if path:
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    self._data = json.load(f)
            except (OSError, ValueError):
                self._data = {}

    def get(self, key: str) -> Any:
        with self._lock:
            item = self._data.get(key)
        if item and time.time() - item[0] < self.ttl:
            return item[1]
        return None

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = [time.time(), value]

    def save(self) -> None:
        if not self.path:
            return
        with self._lock:
            now = time.time()
            data = {k: v for k, v in self._data.items() if now - v[0] < self.ttl}
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f)
            os.replace(tmp, self.path)
        except OSError:
            pass


class Distributor:
    name = ''
    min_interval = 0.5           # seconds between two calls
    needs_key = True
    beta = False

    def __init__(self, cache: Optional[Cache] = None):
        self.cache = cache or Cache()
        self._last = 0.0
        self._lock = threading.Lock()
        self.calls = 0
        # EUR per unit of each currency (ECB): prices in another currency
        # (a Mouser account in dollars, say) are converted, never mixed
        self.rates: Dict[str, float] = {'EUR': 1.0}

    # -- to implement ----------------------------------------------------------
    def configured(self) -> bool:
        return True

    def _search_mpn(self, mpn: str, manufacturer: str = '') -> List[Offer]:
        raise NotImplementedError

    def _search_keyword(self, query: str) -> List[Offer]:
        raise NotImplementedError

    def _lookup_sku(self, sku: str) -> List[Offer]:
        return []

    def _alternates(self, offer: Offer) -> List[Offer]:
        return []

    # -- public, cached --------------------------------------------------------
    def _cached(self, kind: str, arg: str, fn) -> List[Offer]:
        key = f'{self.name}|{kind}|{arg}'
        hit = self.cache.get(key)
        if hit is not None:
            return self.in_eur([Offer.from_dict(d) for d in hit])
        offers = self.in_eur(fn())
        self.cache.put(key, [o.to_dict() for o in offers])
        return offers

    def in_eur(self, offers: List[Offer]) -> List[Offer]:
        """Prices in euros: another currency is converted at the ECB rate;
        without a rate the prices are dropped (the offer cannot be chosen)
        rather than added to euros."""
        for o in offers:
            cur = (o.currency or 'EUR').strip().upper()
            if cur in ('EUR', '€', ''):
                o.currency = 'EUR'
                continue
            rate = self.rates.get(cur)
            if rate:
                o.prices = [PriceBreak(b.qty, round(b.price * rate, 5)) for b in o.prices]
                why = f'prices converted from {cur} at the ECB rate'
            else:
                o.prices = []
                why = f'prices in {cur}: no exchange rate'
            o.note = f'{o.note}; {why}' if o.note else why
            o.currency = 'EUR'
        return offers

    def search_mpn(self, mpn: str, manufacturer: str = '') -> List[Offer]:
        return self._cached('mpn', mpn.upper(), lambda: self._search_mpn(mpn, manufacturer))

    def search_keyword(self, query: str) -> List[Offer]:
        return self._cached('kw', query.lower(), lambda: self._search_keyword(query))

    def lookup_sku(self, sku: str) -> List[Offer]:
        return self._cached('sku', sku.upper(), lambda: self._lookup_sku(sku))

    def alternates(self, offer: Offer) -> List[Offer]:
        return self._cached('alt', (offer.sku or offer.mpn).upper(), lambda: self._alternates(offer))

    def test(self) -> str:
        """A real call with a well-known part: '' if fine, else the error."""
        try:
            offers = self._search_mpn('GRM188R71H104KA93D')
            return '' if offers is not None else 'no answer'
        except DistributorError as e:
            return str(e)

    # -- helpers ---------------------------------------------------------------
    def _pace(self) -> None:
        with self._lock:
            wait = self._last + self.min_interval - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            self.calls += 1

    def _call(self, method: str, url: str, **kw) -> Any:
        for attempt in range(2):
            self._pace()
            status, data, headers = http_request(method, url, **kw)
            if status == 429 and attempt == 0:
                retry = parse_int(headers.get('Retry-After', '')) or 5
                time.sleep(min(retry, 30))
                continue
            break
        if status >= 400:
            raise DistributorError(f'{self.name}: HTTP {status} {self._error_text(data)}'.strip())
        return data

    @staticmethod
    def _error_text(data: Any) -> str:
        if isinstance(data, dict):
            for k in ('detail', 'message', 'Message', 'error_description', 'error', 'title', 'msg'):
                v = data.get(k)
                if v:
                    return str(v)[:200]
            errs = data.get('Errors') or data.get('errors')
            if errs:
                return str(errs)[:200]
        return str(data)[:200] if data else ''


# ---------------------------------------------------------------------------
# DigiKey — Product Information v4 (OAuth2 client credentials)
# ---------------------------------------------------------------------------

class DigiKey(Distributor):
    name = 'DigiKey'
    base = 'https://api.digikey.com'
    min_interval = 0.6

    def __init__(self, client_id: str, client_secret: str, site: str = 'FR',
                 language: str = 'en', currency: str = 'EUR', cache: Optional[Cache] = None):
        # language 'en': parameter names stay in English (Package / Case, Tolerance...)
        super().__init__(cache)
        self.client_id, self.client_secret = client_id.strip(), client_secret.strip()
        self.site, self.language, self.currency = site, language, currency
        self._token, self._expires = '', 0.0

    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def _auth(self) -> Dict[str, str]:
        if not self._token or time.time() > self._expires:
            self._pace()
            status, data, _h = http_request('POST', f'{self.base}/v1/oauth2/token', form={
                'client_id': self.client_id, 'client_secret': self.client_secret,
                'grant_type': 'client_credentials'})
            if status != 200 or not isinstance(data, dict) or 'access_token' not in data:
                raise DistributorError(f'DigiKey: authentication refused (HTTP {status} '
                                       f'{self._error_text(data)})'.strip())
            self._token = data['access_token']
            self._expires = time.time() + max(int(data.get('expires_in', 600)) - 30, 30)
        return {'X-DIGIKEY-Client-Id': self.client_id, 'Authorization': f'Bearer {self._token}',
                'X-DIGIKEY-Locale-Site': self.site, 'X-DIGIKEY-Locale-Language': self.language,
                'X-DIGIKEY-Locale-Currency': self.currency}

    def _keyword(self, words: str, in_stock: bool) -> Dict[str, Any]:
        body: Dict[str, Any] = {'Keywords': words[:250], 'Limit': 20, 'Offset': 0,
                                'FilterOptionsRequest': {'MarketPlaceFilter': 'ExcludeMarketPlace'}}
        if in_stock:
            body['FilterOptionsRequest']['SearchOptions'] = ['InStock']
        return self._call('POST', f'{self.base}/products/v4/search/keyword', body=body,
                          headers=self._auth())

    def _search_mpn(self, mpn: str, manufacturer: str = '') -> List[Offer]:
        data = self._keyword(mpn, in_stock=False)
        products = list(data.get('ExactMatches') or []) or [
            p for p in data.get('Products') or [] if same_mpn(p.get('ManufacturerProductNumber'), mpn)]
        return [o for p in products for o in [self._offer(p)] if o]

    def _search_keyword(self, query: str) -> List[Offer]:
        data = self._keyword(query, in_stock=True)
        return [o for p in data.get('Products') or [] for o in [self._offer(p)] if o]

    def _lookup_sku(self, sku: str) -> List[Offer]:
        data = self._call('GET', f'{self.base}/products/v4/search/'
                          f'{urllib.parse.quote(sku, safe="")}/productdetails', headers=self._auth())
        p = data.get('Product') if isinstance(data, dict) else None
        o = self._offer(p) if p else None
        return [o] if o else []

    def _alternates(self, offer: Offer) -> List[Offer]:
        data = self._call('GET', f'{self.base}/products/v4/search/'
                          f'{urllib.parse.quote(offer.sku or offer.mpn, safe="")}/substitutions',
                          headers=self._auth())
        out: List[Offer] = []
        subs = (data.get('ProductSubstitutes') or []) if isinstance(data, dict) else []
        for sub in subs[:5]:
            mpn = str(sub.get('ManufacturerProductNumber') or '')
            if not mpn:
                continue
            # substitutes carry no part number nor price breaks: ask for the part itself
            for o in self._search_mpn(mpn):
                o.note = f'DigiKey substitute ({sub.get("SubstituteType") or "similar"})'
                out.append(o)
        return out

    def _offer(self, p: Dict[str, Any]) -> Optional[Offer]:
        if not isinstance(p, dict):
            return None
        variations = [v for v in p.get('ProductVariations') or [] if isinstance(v, dict)
                      and not v.get('MarketPlace')]
        # no Digi-Reel (a reeling fee per order) when another packaging exists
        plain = [v for v in variations
                 if 'digi-reel' not in str((v.get('PackageType') or {}).get('Name', '')).lower()]
        variations = plain or variations
        # the variation one can buy in small quantities (cut tape, bulk)
        var = min(variations, key=lambda v: (int(v.get('MinimumOrderQuantity') or 1),
                                             -int(v.get('QuantityAvailableforPackageType') or 0)),
                  default={})
        pack_name = str((var.get('PackageType') or {}).get('Name', ''))
        multiple = 1
        if re.search(r'tape\s*&\s*reel|\(tr\)', pack_name, re.I):
            multiple = int(var.get('StandardPackage') or var.get('MinimumOrderQuantity') or 1)
        prices = [PriceBreak(int(b.get('BreakQuantity') or 1), float(b.get('UnitPrice') or 0))
                  for b in var.get('StandardPricing') or [] if b.get('UnitPrice') is not None]
        if not prices and p.get('UnitPrice'):
            prices = [PriceBreak(1, float(p['UnitPrice']))]
        params = {str(x.get('ParameterText')): str(x.get('ValueText'))
                  for x in p.get('Parameters') or [] if x.get('ParameterText')}
        status = (p.get('ProductStatus') or {}).get('Status', '') if isinstance(
            p.get('ProductStatus'), dict) else str(p.get('ProductStatus') or '')
        life = lifecycle_of(status)
        if p.get('Discontinued'):
            life = 'obsolete'
        elif p.get('EndOfLife') and life in ('active', 'unknown'):
            life = 'eol'
        mfr = p.get('Manufacturer') or {}
        desc = p.get('Description') or {}
        lead = p.get('ManufacturerLeadWeeks')
        return Offer(
            distributor=self.name, sku=str(var.get('DigiKeyProductNumber') or ''),
            mpn=str(p.get('ManufacturerProductNumber') or ''),
            manufacturer=str(mfr.get('Name') if isinstance(mfr, dict) else mfr or ''),
            description=str(desc.get('ProductDescription') if isinstance(desc, dict) else desc or ''),
            stock=parse_int(var.get('QuantityAvailableforPackageType', p.get('QuantityAvailable'))),
            moq=int(var.get('MinimumOrderQuantity') or 1), multiple=multiple, prices=prices,
            currency=self.currency, lead_time=f'{lead} weeks' if lead else '', lifecycle=life,
            url=str(p.get('ProductUrl') or ''), datasheet=str(p.get('DatasheetUrl') or ''),
            package=params.get('Package / Case', params.get('Supplier Device Package', '')),
            params=params, packaging=pack_name)

    def test(self) -> str:
        try:
            self._auth()
            return super().test()
        except DistributorError as e:
            return str(e)


# ---------------------------------------------------------------------------
# Mouser — Search API (API key)
# ---------------------------------------------------------------------------

class Mouser(Distributor):
    name = 'Mouser'
    base = 'https://api.mouser.com/api/v1'
    min_interval = 2.1           # 30 calls per minute

    def __init__(self, api_key: str, cache: Optional[Cache] = None):
        super().__init__(cache)
        self.api_key = api_key.strip()

    def configured(self) -> bool:
        return bool(self.api_key)

    def _post(self, path: str, body: Dict[str, Any]) -> List[Dict[str, Any]]:
        data = self._call('POST', f'{self.base}/{path}', params={'apiKey': self.api_key}, body=body)
        if not isinstance(data, dict):
            raise DistributorError('Mouser: unexpected answer')
        errors = [e for e in data.get('Errors') or [] if e]
        if errors:
            e = errors[0]
            raise DistributorError(f'Mouser: {e.get("Message") or e.get("Code") or e}')
        return list((data.get('SearchResults') or {}).get('Parts') or [])

    def _search_mpn(self, mpn: str, manufacturer: str = '') -> List[Offer]:
        parts = self._post('search/partnumber', {'SearchByPartRequest': {
            'mouserPartNumber': mpn, 'partSearchOptions': 'Exact'}})
        return [o for p in parts for o in [self._offer(p)]
                if o and same_mpn(o.mpn, mpn)]

    def _search_keyword(self, query: str) -> List[Offer]:
        parts = self._post('search/keyword', {'SearchByKeywordRequest': {
            'keyword': query, 'records': 20, 'startingRecord': 0, 'searchOptions': 'InStock',
            'searchWithYourSignUpLanguage': 'false'}})
        return [o for p in parts for o in [self._offer(p)] if o]

    def _lookup_sku(self, sku: str) -> List[Offer]:
        parts = self._post('search/partnumber', {'SearchByPartRequest': {
            'mouserPartNumber': sku, 'partSearchOptions': 'Exact'}})
        return [o for p in parts for o in [self._offer(p)] if o]

    def _alternates(self, offer: Offer) -> List[Offer]:
        if not offer.replacement:
            return []
        alts = self._search_mpn(offer.replacement)
        for a in alts:
            a.note = 'Mouser suggested replacement'
        return alts

    def _offer(self, p: Dict[str, Any]) -> Optional[Offer]:
        if not isinstance(p, dict) or not p.get('ManufacturerPartNumber'):
            return None
        prices, currency = [], 'EUR'
        for b in p.get('PriceBreaks') or []:
            price = parse_price(b.get('Price'))
            if price is not None:
                prices.append(PriceBreak(int(b.get('Quantity') or 1), price))
                currency = str(b.get('Currency') or currency)
        stock = parse_int(p.get('AvailabilityInStock'))
        if stock is None:
            avail = str(p.get('Availability') or '')
            stock = parse_int(avail) if re.search(r'stock', avail, re.I) else 0
        life = lifecycle_of(str(p.get('LifecycleStatus') or ''))
        if str(p.get('IsDiscontinued', '')).lower() == 'true':
            life = 'obsolete'
        elif life == 'unknown':
            life = 'active'
        params = {str(a.get('AttributeName')): str(a.get('AttributeValue'))
                  for a in p.get('ProductAttributes') or [] if a.get('AttributeName')}
        return Offer(
            distributor=self.name, sku=str(p.get('MouserPartNumber') or ''),
            mpn=str(p.get('ManufacturerPartNumber') or ''), manufacturer=str(p.get('Manufacturer') or ''),
            description=str(p.get('Description') or ''), stock=stock,
            moq=parse_int(p.get('Min')) or 1, multiple=parse_int(p.get('Mult')) or 1,
            prices=prices, currency=currency, lead_time=str(p.get('LeadTime') or ''), lifecycle=life,
            url=str(p.get('ProductDetailUrl') or ''), datasheet=str(p.get('DataSheetUrl') or ''),
            package=params.get('Package / Case', params.get('Package/Case', '')), params=params,
            replacement=str(p.get('SuggestedReplacement') or ''))


# ---------------------------------------------------------------------------
# Farnell / element14 — Product Search API (API key)
# ---------------------------------------------------------------------------

# the currency of an element14 store (prices come without one)
_STORE_CURRENCY = {'uk.farnell.com': 'GBP', 'ch.farnell.com': 'CHF', 'se.farnell.com': 'SEK',
                   'dk.farnell.com': 'DKK', 'no.farnell.com': 'NOK', 'pl.farnell.com': 'PLN',
                   'cz.farnell.com': 'CZK', 'hu.farnell.com': 'HUF', 'www.newark.com': 'USD',
                   'canada.newark.com': 'CAD', 'au.element14.com': 'AUD', 'nz.element14.com': 'NZD',
                   'sg.element14.com': 'SGD', 'in.element14.com': 'INR', 'cn.element14.com': 'CNY',
                   'hk.element14.com': 'HKD'}


class Farnell(Distributor):
    name = 'Farnell'
    base = 'https://api.element14.com/catalog/products'
    min_interval = 0.6           # 2 calls per second

    def __init__(self, api_key: str, store: str = 'fr.farnell.com', currency: str = '',
                 cache: Optional[Cache] = None):
        super().__init__(cache)
        self.api_key, self.store = api_key.strip(), store.strip().lower()
        # euro stores (fr, de, it, es, nl, be, at, ie, fi...) unless known otherwise
        self.currency = currency or _STORE_CURRENCY.get(self.store, 'EUR')

    def configured(self) -> bool:
        return bool(self.api_key)

    def _get(self, term: str) -> List[Dict[str, Any]]:
        data = self._call('GET', self.base, params={
            'versionNumber': '1.3', 'term': term, 'storeInfo.id': self.store,
            'resultsSettings.offset': 0, 'resultsSettings.numberOfResults': 20,
            'resultsSettings.responseGroup': 'large', 'callInfo.responseDataFormat': 'JSON',
            'callInfo.apiKey': self.api_key})
        if not isinstance(data, dict):
            raise DistributorError('Farnell: unexpected answer')
        if 'Fault' in data:
            raise DistributorError(f'Farnell: {data["Fault"]}')
        for k, v in data.items():
            if k.endswith('Return') and isinstance(v, dict):
                return list(v.get('products') or [])
        return []

    def _search_mpn(self, mpn: str, manufacturer: str = '') -> List[Offer]:
        return [o for p in self._get(f'manuPartNum:{mpn}') for o in [self._offer(p)]
                if o and same_mpn(o.mpn, mpn)]

    def _search_keyword(self, query: str) -> List[Offer]:
        return [o for p in self._get(f'any:{query}') for o in [self._offer(p)] if o]

    def _lookup_sku(self, sku: str) -> List[Offer]:
        return [o for p in self._get(f'id:{sku}') for o in [self._offer(p)] if o]

    def _offer(self, p: Dict[str, Any]) -> Optional[Offer]:
        if not isinstance(p, dict):
            return None
        prices = [PriceBreak(int(b.get('from') or 1), float(b.get('cost')))
                  for b in p.get('prices') or [] if b.get('cost') is not None]
        stock = p.get('stock') or {}
        status = str(p.get('productStatus') or '')
        life = lifecycle_of(status)
        if life == 'unknown':
            life = 'active'
        attrs = {f'{a.get("attributeLabel")}': f'{a.get("attributeValue")}{a.get("attributeUnit") or ""}'
                 for a in p.get('attributes') or [] if a.get('attributeLabel')}
        sheets = p.get('datasheets') or []
        lead = stock.get('leastLeadTime') if isinstance(stock, dict) else None
        moq = parse_int(p.get('translatedMinimumOrderQuality') or p.get('translatedMinimumOrderQuantity'))
        sku = str(p.get('sku') or '')
        return Offer(
            distributor=self.name, sku=sku,
            mpn=str(p.get('translatedManufacturerPartNumber') or ''),
            manufacturer=str(p.get('brandName') or p.get('vendorName') or ''),
            description=str(p.get('displayName') or ''),
            stock=parse_int(stock.get('level')) if isinstance(stock, dict) else parse_int(p.get('inv')),
            moq=moq or 1, multiple=parse_int(p.get('orderMultiples')) or moq or 1, prices=prices,
            currency=self.currency, lead_time=f'{lead} days' if lead else '', lifecycle=life,
            url=f'https://{self.store}/{sku}' if sku else '',
            datasheet=str(sheets[0].get('url')) if sheets and isinstance(sheets[0], dict) else '',
            package=next((v for k, v in attrs.items() if re.search(r'case|package', k, re.I)), ''),
            params=attrs, packaging=str(p.get('packSize') or ''))


# ---------------------------------------------------------------------------
# TME — API v2 (OAuth2 client credentials, apps created after 2026-05-14)
# ---------------------------------------------------------------------------

def _pick(d: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ''):
            return d[k]
    return default


class TME(Distributor):
    name = 'TME'
    base = 'https://api.tme.eu'
    min_interval = 0.6
    beta = True

    def __init__(self, token: str, secret: str, country: str = 'FR', language: str = 'fr',
                 currency: str = 'EUR', cache: Optional[Cache] = None):
        super().__init__(cache)
        self.token, self.secret = token.strip(), secret.strip()
        self.country, self.language, self.currency = country, language, currency
        self._access, self._expires = '', 0.0

    def configured(self) -> bool:
        return bool(self.token and self.secret)

    def _auth(self) -> Dict[str, str]:
        if not self._access or time.time() > self._expires:
            basic = base64.b64encode(f'{self.token}:{self.secret}'.encode()).decode()
            self._pace()
            status, data, _h = http_request('POST', f'{self.base}/auth/token',
                                            form={'grant_type': 'client_credentials'},
                                            headers={'Authorization': f'Basic {basic}'})
            if status != 200 or not isinstance(data, dict) or 'access_token' not in data:
                raise DistributorError(f'TME: authentication refused (HTTP {status} '
                                       f'{self._error_text(data)})'.strip())
            self._access = data['access_token']
            self._expires = time.time() + max(int(data.get('expires_in', 300)) - 30, 30)
        return {'Authorization': f'Bearer {self._access}', 'Accept-Language': self.language}

    def _get(self, path: str, params: Dict[str, Any]) -> Any:
        query: Dict[str, Any] = {'country': self.country}
        for k, v in params.items():
            query[f'{k}[]' if isinstance(v, list) else k] = v
        data = self._call('GET', f'{self.base}/{path}', params=query, headers=self._auth())
        if isinstance(data, dict):
            status = str(data.get('status', 'OK')).upper()
            if status not in ('OK', ''):
                raise DistributorError(f'TME: {self._error_text(data)}')
            return data.get('data', data)
        return data

    @staticmethod
    def _products(data: Any) -> List[Dict[str, Any]]:
        if isinstance(data, list):
            return [p for p in data if isinstance(p, dict)]
        if isinstance(data, dict):
            for k in ('products', 'ProductList', 'items', 'product_list'):
                if isinstance(data.get(k), list):
                    return [p for p in data[k] if isinstance(p, dict)]
        return []

    def _with_prices(self, products: List[Dict[str, Any]]) -> List[Offer]:
        symbols = [str(_pick(p, 'symbol', 'Symbol')) for p in products if _pick(p, 'symbol', 'Symbol')]
        stock_price: Dict[str, Dict[str, Any]] = {}
        if symbols:
            data = self._get('products/data', {'symbols': symbols[:50], 'scope': ['prices', 'stock'],
                                               'currency': self.currency})
            for item in self._products(data):
                stock_price[str(_pick(item, 'symbol', 'Symbol'))] = item
        out = []
        for p in products:
            sym = str(_pick(p, 'symbol', 'Symbol', default=''))
            extra = stock_price.get(sym, {})
            raw_prices = _pick(extra, 'prices', 'PriceList', 'price_list', default=[]) or []
            if isinstance(raw_prices, dict):
                raw_prices = _pick(raw_prices, 'list', 'items', default=[]) or []
            prices = []
            for b in raw_prices:
                q = parse_int(_pick(b, 'amount', 'Amount', 'quantity', 'Quantity'))
                v = parse_price(_pick(b, 'price', 'PriceValue', 'value', 'net', 'price_value'))
                if q and v is not None:
                    prices.append(PriceBreak(q, v))
            stock = _pick(extra, 'stock', 'Amount', 'amount', 'quantity')
            if isinstance(stock, dict):
                stock = _pick(stock, 'amount', 'quantity', 'total')
            statuses = _pick(p, 'statuses', 'ProductStatusList', 'status', default=[])
            status_text = ' '.join(statuses) if isinstance(statuses, list) else str(statuses)
            life = lifecycle_of(status_text)
            producer = _pick(p, 'producer', 'Producer', 'manufacturer', default='')
            if isinstance(producer, dict):
                producer = _pick(producer, 'name', 'Name', default='')
            out.append(Offer(
                distributor=self.name, sku=sym,
                mpn=str(_pick(p, 'original_symbol', 'OriginalSymbol', 'mpn', default='')),
                manufacturer=str(producer), description=str(_pick(p, 'description', 'Description', default='')),
                stock=parse_int(stock), moq=parse_int(_pick(p, 'min_amount', 'MinAmount', 'minimal_amount')) or 1,
                multiple=parse_int(_pick(p, 'multiples', 'Multiples')) or 1, prices=prices,
                currency=self.currency, lifecycle='active' if life == 'unknown' else life,
                url=f'https://www.tme.eu/{self.country.lower()}/details/{urllib.parse.quote(sym.lower())}/'
                if sym else ''))
        return out

    def _search_mpn(self, mpn: str, manufacturer: str = '') -> List[Offer]:
        products = self._products(self._get('products', {'mpns': [mpn]}))
        products = [p for p in products
                    if same_mpn(str(_pick(p, 'original_symbol', 'OriginalSymbol', 'mpn', default='')), mpn)]
        return self._with_prices(products)

    def _search_keyword(self, query: str) -> List[Offer]:
        data = self._get('products/search', {'phrase': query[:40], 'scope': ['products'], 'limit': 20})
        return self._with_prices(self._products(data)[:20])

    def _lookup_sku(self, sku: str) -> List[Offer]:
        return self._with_prices(self._products(self._get('products', {'symbols': [sku]})))

    def _alternates(self, offer: Offer) -> List[Offer]:
        if not offer.sku:
            return []
        alts = self._with_prices(self._products(self._get('products/similar', {'symbol': offer.sku}))[:10])
        for a in alts:
            a.note = 'TME similar product'
        return alts


# ---------------------------------------------------------------------------
# LCSC — public catalogue (no key): JLCPCB parts search + LCSC product detail
# ---------------------------------------------------------------------------

class LCSC(Distributor):
    name = 'LCSC'
    needs_key = False
    min_interval = 0.6
    search_url = ('https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/'
                  'smtGood/selectSmtComponentList')
    detail_url = 'https://wmsc.lcsc.com/ftps/wm/product/detail'

    def __init__(self, usd_to_eur: float = 0.92, cache: Optional[Cache] = None, enabled: bool = True):
        super().__init__(cache)
        self.rate = usd_to_eur
        self.enabled = enabled

    def configured(self) -> bool:
        return self.enabled

    def _jlc(self, keyword: str, size: int = 10) -> List[Dict[str, Any]]:
        data = self._call('POST', self.search_url, body={'keyword': keyword, 'currentPage': 1,
                                                         'pageSize': size})
        # errors come back as HTTP 200 with another code: not a "not found"
        if not isinstance(data, dict) or data.get('code') != 200:
            raise DistributorError(f'LCSC (JLCPCB search): {self._error_text(data) or "no answer"}')
        try:
            return list(data['data']['componentPageInfo']['list'] or [])
        except (KeyError, TypeError):
            return []

    def _detail(self, code: str) -> Optional[Dict[str, Any]]:
        data = self._call('GET', self.detail_url, params={'productCode': code})
        if not isinstance(data, dict):
            raise DistributorError('LCSC: unexpected answer')
        if data.get('code') == 200 and isinstance(data.get('result'), dict):
            return data['result']
        if data.get('code') == 200:
            return None                                  # no such part
        raise DistributorError(f'LCSC: {self._error_text(data) or data.get("code")}')

    def _offer(self, d: Dict[str, Any], jlc: Optional[Dict[str, Any]] = None) -> Offer:
        prices = []
        for b in d.get('productPriceList') or []:
            usd = parse_price(b.get('usdPrice'))
            if usd is not None:                          # always in US dollars
                v = usd * self.rate
            else:
                local = parse_price(b.get('currencyPrice', b.get('productPrice')))
                if local is None:
                    continue
                v = local * (self.rate if str(d.get('currencyType') or 'USD').upper() == 'USD' else 1.0)
            prices.append(PriceBreak(int(b.get('ladder') or 1), round(v, 5)))
        params = {str(x.get('paramNameEn')): str(x.get('paramValueEn'))
                  for x in d.get('paramVOList') or [] if x.get('paramNameEn')}
        code = str(d.get('productCode') or '')
        note = ''
        if jlc:
            kind = {'base': 'basic', 'expand': 'extended'}.get(str(jlc.get('componentLibraryType')), '')
            note = f'JLCPCB {kind} part, assembly stock {jlc.get("stockCount", "?")}' if kind else ''
        return Offer(
            distributor=self.name, sku=code, mpn=str(d.get('productModel') or ''),
            manufacturer=str(d.get('brandNameEn') or ''),
            description=str(d.get('productIntroEn') or d.get('productDescEn') or d.get('productNameEn') or ''),
            stock=parse_int(d.get('stockNumber')), moq=parse_int(d.get('minBuyNumber')) or 1,
            multiple=parse_int(d.get('split')) or 1, prices=prices, currency='EUR',
            lifecycle=lifecycle_of(str(d.get('productCycle') or '')) if d.get('productCycle') else 'unknown',
            url=f'https://www.lcsc.com/product-detail/{code}.html' if code else '',
            datasheet=str(d.get('pdfUrl') or ''), package=str(d.get('encapStandard') or ''),
            params=params, packaging=str(d.get('productArrange') or ''), note=note)

    def _search_mpn(self, mpn: str, manufacturer: str = '') -> List[Offer]:
        out = []
        for c in self._jlc(mpn):
            if not same_mpn(str(c.get('componentModelEn') or ''), mpn):
                continue
            if 'JLCPCB' in str(c.get('componentBrandEn') or ''):
                continue                       # JLCPCB's own consigned stock, not an LCSC part
            d = self._detail(str(c.get('componentCode') or ''))
            if d:
                out.append(self._offer(d, c))
            if len(out) >= 3:
                break
        return out

    def _search_keyword(self, query: str) -> List[Offer]:
        out = []
        for c in self._jlc(query, size=10)[:6]:
            if 'JLCPCB' in str(c.get('componentBrandEn') or ''):
                continue
            d = self._detail(str(c.get('componentCode') or ''))
            if d:
                out.append(self._offer(d, c))
        return out

    def _lookup_sku(self, sku: str) -> List[Offer]:
        d = self._detail(sku.strip().upper())
        return [self._offer(d)] if d else []

    def _alternates(self, offer: Offer) -> List[Offer]:
        d = self._detail(offer.sku) if offer.sku else None
        out = []
        for a in (d or {}).get('alternatePartList') or []:
            if isinstance(a, dict) and a.get('productCode'):
                o = self._offer(a)
                o.note = 'LCSC alternative'
                out.append(o)
        return out[:8]

    def test(self) -> str:
        try:
            return '' if self._detail('C25804') else 'no answer'
        except DistributorError as e:
            return str(e)


# ---------------------------------------------------------------------------
# Exchange rate (ECB reference rates, no key)
# ---------------------------------------------------------------------------

ECB_URL = 'https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml'


def parse_ecb(text: str) -> Tuple[Dict[str, float], str]:
    """({currency: EUR per unit}, date) from the ECB daily XML, which gives
    '1 EUR = rate units'."""
    rates = {'EUR': 1.0}
    for cur, r in re.findall(r"currency=['\"]([A-Z]{3})['\"]\s+rate=['\"]([\d.]+)['\"]", str(text)):
        if float(r) > 0:
            rates[cur] = 1.0 / float(r)
    d = re.search(r"time=['\"]([\d-]+)['\"]", str(text))
    return rates, d.group(1) if d else ''


def ecb_rates(cache: Optional[Cache] = None) -> Tuple[Dict[str, float], str]:
    """Today's ECB reference rates as EUR per unit of each currency, and
    their date; only {'EUR': 1} when the ECB cannot be reached."""
    if cache is not None:
        hit = cache.get('ecb|rates')
        if hit:
            return {k: float(v) for k, v in hit[0].items()}, str(hit[1])
    try:
        status, text, _h = http_request('GET', ECB_URL, timeout=15)
        rates, day = parse_ecb(str(text))
        if status == 200 and len(rates) > 1:
            if cache is not None:
                cache.put('ecb|rates', [rates, day])
            return rates, day
    except DistributorError:
        pass
    return {'EUR': 1.0}, ''


def usd_to_eur(cache: Optional[Cache] = None, fallback: float = 0.92) -> Tuple[float, str]:
    """(EUR per USD, date) from the ECB daily reference rates."""
    rates, day = ecb_rates(cache)
    if 'USD' in rates:
        return rates['USD'], day
    return fallback, ''
