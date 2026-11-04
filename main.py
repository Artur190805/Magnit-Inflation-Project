"""Еженедельный сбор цен товаров магазина «Магнит».

Python 3.11+ (используется tomllib из стандартной библиотеки).
"""

from __future__ import annotations

import csv
import html
import json
import re
import time
import tomllib
from collections import defaultdict
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests


ROOT = Path(__file__).resolve().parent
REGISTRY_FILE = ROOT / "products.toml"
CURRENT_FILE = ROOT / "products.csv"
HISTORY_FILE = ROOT / "price_history.csv"
WIDE_FILE = ROOT / "dataset_wide.csv"
ITEMS_FILE = ROOT / "items.csv"
REPORT_FILE = ROOT / "basket_report.txt"
ERRORS_FILE = ROOT / "parse_errors.csv"
NORMALIZATION_FILE = ROOT / "normalization_issues.csv"

STORE = "Магнит"
DEFAULT_SHOP_CODE = "992301"
DEFAULT_SHOP_TYPE = "dostavka"
DEFAULT_MROT_RUB = 27093.0  # Федеральный МРОТ с 01.01.2026, ФЗ от 28.11.2025 № 429-ФЗ.
EXPECTED_PRODUCTS = 53

REQUEST_TIMEOUT = 25
REQUEST_DELAY_SECONDS = 0.7
MAX_RETRIES = 2

FIELDS = [
    "date_collected",
    "week",
    "store",
    "product_id",
    "category",
    "product_name",
    "product_url",
    "package_amount",
    "package_unit",
    "package_price_rub",
    "price_per_kg_or_l_rub",
    "normalized_unit",
    "price_source",
    "basket_eligible",
    "basket_price_rub",
    "basket_unit",
]

WIDE_BASE_FIELDS = ["n", "date_collected", "week"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}


# ---------------------------------------------------------------------------
# Реестр и общие функции
# ---------------------------------------------------------------------------


def clean_text(value: Any) -> str:
    value = html.unescape(str(value or ""))
    return re.sub(r"\s+", " ", value).strip()


def parse_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = clean_text(value).replace("\xa0", "").replace("\u202f", "")
        match = re.search(r"\d+(?:[.,]\d+)?", text)
        if not match:
            return None
        try:
            number = float(match.group(0).replace(",", "."))
        except ValueError:
            return None
    return number if 0 < number < 1_000_000 else None


def parse_price(value: Any) -> float | None:
    price = parse_number(value)
    if price is None or price > 1_000_000:
        return None
    return round(price, 2)


def product_id_from_url(url: str) -> str | None:
    match = re.search(r"/product/(\d+)", url)
    return match.group(1) if match else None


def add_shop_parameters(url: str, shop_code: str, shop_type: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("shopCode", shop_code)
    query.setdefault("shopType", shop_type)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def read_registry() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not REGISTRY_FILE.exists():
        raise FileNotFoundError(f"Не найден файл реестра: {REGISTRY_FILE}")

    with REGISTRY_FILE.open("rb") as file:
        config = tomllib.load(file)

    groups = config.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError(
            "В products.toml не найдены группы [[groups]] и [[groups.products]]. "
            "Используй products.toml из комплекта вместе с этим main.py."
        )

    shop_code = str(config.get("shop_code", DEFAULT_SHOP_CODE))
    shop_type = str(config.get("shop_type", DEFAULT_SHOP_TYPE))
    expected = int(config.get("expected_products", EXPECTED_PRODUCTS))

    products: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()

    for group in groups:
        category = clean_text(group.get("name"))
        group_products = group.get("products", [])
        if not category:
            errors.append("У одной из групп отсутствует name")
            continue
        if not isinstance(group_products, list):
            errors.append(f"Некорректный список products в группе {category}")
            continue

        for item in group_products:
            product_id = clean_text(item.get("id"))
            name = clean_text(item.get("name"))
            raw_url = clean_text(item.get("url"))

            if not product_id or not name or not raw_url:
                errors.append(
                    f"В группе «{category}» запись без id, name или url: {item}"
                )
                continue

            parts = urlsplit(raw_url)
            if parts.scheme != "https" or parts.netloc.lower() not in {
                "magnit.ru", "www.magnit.ru"
            }:
                errors.append(f"Товар {product_id}: ссылка должна вести на magnit.ru: {raw_url}")
                continue

            if product_id in seen_ids:
                errors.append(f"Повторяется id товара: {product_id}")
                continue

            final_url = add_shop_parameters(raw_url, shop_code, shop_type)
            product_path_id = product_id_from_url(final_url)
            if not product_path_id:
                errors.append(f"Товар {product_id}: в URL не найден числовой ID товара")
                continue

            if final_url in seen_urls:
                errors.append(f"Повторяется ссылка товара: {final_url}")
                continue

            seen_ids.add(product_id)
            seen_urls.add(final_url)

            product = {
                "id": product_id,
                "name": name,
                "category": category,
                "url": final_url,
                "page_id": product_path_id,
                "basket_eligible": bool(item.get("basket_eligible", True)),
                "price_basis": str(item.get("price_basis", "package")).lower(),
                "amount_override": item.get("amount"),
                "unit_override": str(item.get("unit", "")).lower(),
            }
            if product["price_basis"] not in {"package", "kg", "l"}:
                errors.append(
                    f"Товар {product_id}: price_basis допускает только package, kg или l"
                )
                continue
            products.append(product)

    if errors:
        raise ValueError("Ошибки в products.toml:\n- " + "\n- ".join(errors))

    if len(products) != expected:
        raise ValueError(
            f"В products.toml найдено {len(products)} товаров, ожидалось {expected}. "
            "Сбор остановлен, чтобы не перезаписать результаты неполным списком. "
            f"Проверь файл: {REGISTRY_FILE}"
        )

    return products, config


# ---------------------------------------------------------------------------
# Разбор HTML и цены
# ---------------------------------------------------------------------------


class PageDataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title_parts: list[str] = []
        self.in_title = False
        self.in_script = False
        self.script_type = ""
        self.script_parts: list[str] = []
        self.scripts: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        tag = tag.lower()

        if tag == "meta":
            key = (
                attrs_dict.get("property")
                or attrs_dict.get("name")
                or attrs_dict.get("itemprop")
            )
            content = attrs_dict.get("content")
            if key and content:
                self.meta[key.lower()] = content

        elif tag == "title":
            self.in_title = True

        elif tag == "script":
            self.in_script = True
            self.script_type = (attrs_dict.get("type") or "").lower()
            self.script_parts = []

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        if self.in_script:
            self.script_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self.in_title = False
        elif tag == "script" and self.in_script:
            self.scripts.append((self.script_type, "".join(self.script_parts)))
            self.in_script = False
            self.script_type = ""
            self.script_parts = []


def walk_json(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def is_product_object(obj: dict[str, Any]) -> bool:
    value = obj.get("@type")
    if isinstance(value, list):
        return "Product" in value
    return value == "Product"


def product_object_matches(obj: dict[str, Any], product: dict[str, Any]) -> bool:
    expected_id = product["page_id"]
    sku = str(obj.get("sku", ""))
    urls = " ".join(
        str(obj.get(key, "")) for key in ("url", "@id")
    )
    if sku or urls.strip():
        return sku == expected_id or expected_id in urls
    # Если sku/url действительно отсутствуют, допускаем точное совпадение названия.
    return clean_text(obj.get("name", "")).casefold() == product["name"].casefold()


def offer_price(offers: Any) -> float | None:
    offer_list = offers if isinstance(offers, list) else [offers]
    for offer in offer_list:
        if not isinstance(offer, dict):
            continue
        currency = offer.get("priceCurrency")
        if currency and str(currency).upper() != "RUB":
            continue
        price = parse_price(offer.get("price"))
        if price is not None:
            return price
    return None


def extract_jsonld_product(page: PageDataParser, product: dict[str, Any]) -> tuple[dict[str, Any] | None, float | None]:
    for script_type, content in page.scripts:
        content = content.strip()
        if not content:
            continue
        if "json" not in script_type and not content.startswith(("{", "[")):
            continue
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            continue

        for obj in walk_json(payload):
            if not is_product_object(obj):
                continue
            if not product_object_matches(obj, product):
                continue
            price = offer_price(obj.get("offers"))
            if price is not None:
                return obj, price
    return None, None


def extract_nuxt_price(page: PageDataParser, product: dict[str, Any]) -> float | None:
    """Резервное извлечение из devalue-массива Nuxt, найденного на magnit.ru."""
    key_prefix = f"goods-detail:{product['page_id']}:"

    for _, content in page.scripts:
        content = content.strip()
        if not content.startswith("["):
            continue
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, list):
            continue

        for obj in data:
            if not isinstance(obj, dict):
                continue
            for key, product_index in obj.items():
                if not str(key).startswith(key_prefix):
                    continue
                if not isinstance(product_index, int) or not (0 <= product_index < len(data)):
                    continue
                product_data = data[product_index]
                if not isinstance(product_data, dict):
                    continue
                price_index = product_data.get("price")
                if not isinstance(price_index, int) or not (0 <= price_index < len(data)):
                    continue
                raw_price = data[price_index]
                if isinstance(raw_price, (int, float)) and not isinstance(raw_price, bool):
                    # В проверенной структуре Nuxt цена передаётся в копейках.
                    price = float(raw_price) / 100
                    if 0 < price < 1_000_000:
                        return round(price, 2)
    return None


# ---------------------------------------------------------------------------
# Масса и объём
# ---------------------------------------------------------------------------


def normalize_measure(amount: float, unit: str) -> tuple[float, str, str, str] | None:
    unit = unit.lower().strip()
    if amount <= 0:
        return None
    if unit in {"kg", "кг", "kilogram", "kilograms", "kgm"}:
        return amount, "kg", amount, "кг"
    if unit in {"g", "г", "гр", "gram", "grams", "grm"}:
        return amount, "g", amount / 1000, "кг"
    if unit in {"l", "л", "liter", "litre", "ltr"}:
        return amount, "l", amount, "л"
    if unit in {"ml", "мл", "milliliter", "millilitre", "mlt"}:
        return amount, "ml", amount / 1000, "л"
    return None


def extract_amount_from_text(text: str) -> tuple[float, str, float, str] | None:
    normalized = clean_text(text).lower().replace(",", ".")
    match = re.search(
        r"(?<!\d)(\d+(?:\.\d+)?)\s*"
        r"(кг|kg|г|гр|g|л|l|мл|ml)\b",
        normalized,
    )
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2)
    return normalize_measure(amount, unit)


def measurement_from_product_object(obj: dict[str, Any] | None) -> tuple[float, str, float, str] | None:
    if not obj:
        return None

    weight_result = None
    volume_result = None

    properties = obj.get("additionalProperty", [])
    if isinstance(properties, dict):
        properties = [properties]
    for prop in properties if isinstance(properties, list) else []:
        if not isinstance(prop, dict):
            continue
        label = clean_text(prop.get("name", "")).lower()
        value = prop.get("value")
        number = parse_number(value)
        if number is None:
            continue
        if any(word in label for word in ("вес", "масса", "weight")):
            unit = "g" if number > 20 else "kg"
            weight_result = normalize_measure(number, unit)
        elif any(word in label for word in ("объем", "объём", "volume")):
            unit = "ml" if number > 20 else "l"
            volume_result = normalize_measure(number, unit)

    # Schema.org weight may be a number/string in kg or QuantitativeValue.
    weight = obj.get("weight")
    if isinstance(weight, dict):
        number = parse_number(weight.get("value"))
        unit_code = clean_text(weight.get("unitCode") or weight.get("unitText") or "kg")
        if number is not None:
            weight_result = normalize_measure(number, unit_code) or weight_result
    elif weight is not None:
        number = parse_number(weight)
        if number is not None:
            weight_result = normalize_measure(number, "g" if number > 20 else "kg") or weight_result

    volume = obj.get("volume")
    if isinstance(volume, dict):
        number = parse_number(volume.get("value"))
        unit_code = clean_text(volume.get("unitCode") or volume.get("unitText") or "l")
        if number is not None:
            volume_result = normalize_measure(number, unit_code) or volume_result
    elif volume is not None:
        number = parse_number(volume)
        if number is not None:
            volume_result = normalize_measure(number, "ml" if number > 20 else "l") or volume_result

    # Для обычных продуктов предпочтителен вес: у товара могут быть и вес,
    # и расчётный объём (например, объём зёрен в банке с горошком).
    return weight_result or volume_result


def resolve_measurement(product: dict[str, Any], product_object: dict[str, Any] | None, page_title: str) -> tuple[float, str, float, str] | None:
    # Для взвешиваемых товаров Магнит показывает цену за килограмм.
    if product["price_basis"] == "kg":
        return 1.0, "kg", 1.0, "кг"
    if product["price_basis"] == "l":
        return 1.0, "l", 1.0, "л"

    # Явные overrides применяются только для записей, где объём известен из URL/карточки.
    amount_override = product.get("amount_override")
    unit_override = product.get("unit_override")
    if amount_override is not None and unit_override:
        result = normalize_measure(float(amount_override), unit_override)
        if result:
            return result

    # Явное указание объёма в литрах/миллилитрах в названии сохраняем как объём.
    text_measure = extract_amount_from_text(product["name"]) or extract_amount_from_text(page_title)
    if text_measure and text_measure[1] in {"l", "ml"}:
        return text_measure

    # Структурированные характеристики карточки исправляют расхождения
    # между названием и фактическим весом упаковки.
    result = measurement_from_product_object(product_object)
    if result:
        return result

    return text_measure


# ---------------------------------------------------------------------------
# Сбор одного товара
# ---------------------------------------------------------------------------


def fetch_product(session: requests.Session, product: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    last_error = "неизвестная ошибка"

    for attempt in range(1, MAX_RETRIES + 2):
        try:
            response = session.get(
                product["url"],
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code in (401, 403, 404):
                return None, f"HTTP {response.status_code}: запрос отклонён или товар недоступен"

            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
                if attempt <= MAX_RETRIES:
                    time.sleep(attempt * 2)
                    continue
                return None, last_error

            response.raise_for_status()

            parser = PageDataParser()
            parser.feed(response.text)
            page_title = clean_text(
                parser.meta.get("og:title")
                or parser.meta.get("twitter:title")
                or "".join(parser.title_parts)
                or product["name"]
            )

            product_object, price = extract_jsonld_product(parser, product)
            source = "JSON-LD Product offers"

            if price is None:
                price = extract_nuxt_price(parser, product)
                source = "Nuxt product data"

            if price is None:
                return None, "страница открылась, но цена конкретного товара не найдена"

            measure = resolve_measurement(product, product_object, page_title)
            if measure:
                amount, package_unit, normalized_amount, normalized_unit = measure
                normalized_price = round(price / normalized_amount, 2)
                package_amount = f"{amount:g}"
                package_unit_value = package_unit
            else:
                normalized_price = None
                normalized_unit = ""
                package_amount = ""
                package_unit_value = ""

            # Цена по весу/объёму считается ценой одного кг/л, а не случайной упаковки.
            if product["price_basis"] == "kg":
                basket_unit = "1 кг"
                basket_price = price
            elif product["price_basis"] == "l":
                basket_unit = "1 л"
                basket_price = price
            else:
                basket_unit = "1 упаковка"
                basket_price = price

            today = date.today()
            iso = today.isocalendar()
            row = {
                "date_collected": today.isoformat(),
                "week": f"{iso.year}-W{iso.week:02d}",
                "store": STORE,
                "product_id": product["id"],
                "category": product["category"],
                "product_name": product["name"],
                "product_url": product["url"],
                "package_amount": package_amount,
                "package_unit": package_unit_value,
                "package_price_rub": f"{price:.2f}",
                "price_per_kg_or_l_rub": (
                    f"{normalized_price:.2f}" if normalized_price is not None else ""
                ),
                "normalized_unit": normalized_unit,
                "price_source": source,
                "basket_eligible": "1" if product["basket_eligible"] else "0",
                "basket_price_rub": f"{basket_price:.2f}" if product["basket_eligible"] else "",
                "basket_unit": basket_unit if product["basket_eligible"] else "",
            }
            return row, f"OK ({source})"

        except requests.RequestException as error:
            last_error = str(error)
            if attempt <= MAX_RETRIES:
                time.sleep(attempt * 2)

    return None, f"ошибка запроса: {last_error}"


# ---------------------------------------------------------------------------
# CSV и история
# ---------------------------------------------------------------------------


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def history_key(row: dict[str, Any]) -> tuple[str, str]:
    # Канонический ключ — ID карточки в URL. Он одинаков для старого CSV
    # без product_id и нового формата, где product_id — порядковый ID реестра.
    url_id = product_id_from_url(str(row.get("product_url", "")))
    product_id = url_id or str(row.get("product_id") or "").strip()
    return str(row.get("week", "")), product_id


def update_history(current_rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    history = read_csv(HISTORY_FILE)
    current_keys = {history_key(row) for row in current_rows}
    history = [row for row in history if history_key(row) not in current_keys]
    history.extend(current_rows)
    history.sort(key=lambda row: (row.get("week", ""), row.get("product_id", "")))
    write_csv(HISTORY_FILE, history, FIELDS)
    return history


def make_items_dictionary(products: list[dict[str, Any]], history: list[dict[str, str]]) -> None:
    unit_by_id: dict[str, str] = {}
    url_to_id = {urlsplit(p["url"]).path: p["id"] for p in products}
    for row in history:
        if row.get("normalized_unit"):
            product_id = str(row.get("product_id", ""))
            if product_id not in {p["id"] for p in products}:
                product_id = url_to_id.get(urlsplit(row.get("product_url", "")).path, product_id)
            unit_by_id[product_id] = row["normalized_unit"]

    rows = []
    for product in products:
        rows.append({
            "item_column": f"item_{int(product['id']):03d}",
            "product_id": product["id"],
            "product_name": product["name"],
            "category": product["category"],
            "normalized_unit": unit_by_id.get(product["id"], ""),
            "product_url": product["url"],
        })
    write_csv(
        ITEMS_FILE,
        rows,
        ["item_column", "product_id", "product_name", "category", "normalized_unit", "product_url"],
    )


def make_wide_dataset(history: list[dict[str, str]], products: list[dict[str, Any]]) -> None:
    """Индексная таблица: n, item_001 ...; цены в ячейках — числа руб./кг или руб./л."""
    product_ids = [p["id"] for p in products]
    fields = WIDE_BASE_FIELDS + [f"item_{int(pid):03d}" for pid in product_ids]
    url_to_id = {urlsplit(p["url"]).path: p["id"] for p in products}
    valid_ids = set(product_ids)

    by_week: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    for row in history:
        week = row.get("week", "")
        product_id = str(row.get("product_id") or "")
        if product_id not in valid_ids:
            product_id = url_to_id.get(urlsplit(row.get("product_url", "")).path, "")
        if not week or not product_id:
            continue
        existing = by_week[week].get(product_id)
        if existing is None or row.get("date_collected", "") >= existing.get("date_collected", ""):
            by_week[week][product_id] = row

    wide_rows: list[dict[str, Any]] = []
    for index, week in enumerate(sorted(by_week), start=1):
        values = by_week[week]
        dates = [r.get("date_collected", "") for r in values.values() if r.get("date_collected")]
        record: dict[str, Any] = {
            "n": index,
            "date_collected": max(dates) if dates else "",
            "week": week,
        }
        for pid in product_ids:
            row = values.get(pid, {})
            field = f"item_{int(pid):03d}"
            raw_price = row.get("price_per_kg_or_l_rub", "")
            try:
                record[field] = f"{float(raw_price):.2f}" if raw_price else ""
            except (ValueError, TypeError):
                record[field] = ""
        wide_rows.append(record)

    write_csv(WIDE_FILE, wide_rows, fields)


def write_normalization_issues(rows: list[dict[str, Any]]) -> None:
    issues = [
        {
            "product_id": row["product_id"],
            "product_name": row["product_name"],
            "category": row["category"],
            "product_url": row["product_url"],
            "package_price_rub": row["package_price_rub"],
            "issue": "В карточке и реестре не найден достоверный вес или объём; цена за кг/л не рассчитывалась.",
        }
        for row in rows
        if not row.get("price_per_kg_or_l_rub")
    ]
    if issues:
        write_csv(
            NORMALIZATION_FILE,
            issues,
            ["product_id", "product_name", "category", "product_url", "package_price_rub", "issue"],
        )
    elif NORMALIZATION_FILE.exists():
        NORMALIZATION_FILE.unlink()


def write_errors(errors: list[tuple[str, str, str]]) -> None:
    if errors:
        write_csv(
            ERRORS_FILE,
            [
                {"product_id": pid, "product_name": name, "error": error}
                for pid, name, error in errors
            ],
            ["product_id", "product_name", "error"],
        )
    elif ERRORS_FILE.exists():
        ERRORS_FILE.unlink()


# ---------------------------------------------------------------------------
# Набор товаров: 1/4 МРОТ ±5%
# ---------------------------------------------------------------------------


def optimize_basket(rows: list[dict[str, Any]], mrot_rub: float) -> tuple[list[dict[str, Any]], float, float, float]:
    """Подбирает реальный набор покупок в заданном диапазоне.

    Один фиксированный товар = одна упаковка. Для взвешиваемых товаров с
    price_basis='kg' одна единица корзины = 1 кг. Выбор максимизирует число
    представленных товарных категорий, затем число товаров, затем близость
    суммы к середине диапазона.
    """
    quarter = mrot_rub / 4
    lower = round(quarter * 0.95, 2)
    upper = round(quarter * 1.05, 2)
    lower_cents = int(round(lower * 100))
    upper_cents = int(round(upper * 100))
    midpoint_cents = (lower_cents + upper_cents) // 2

    eligible = []
    for row in rows:
        if str(row.get("basket_eligible", "1")) not in {"1", "True", "true"}:
            continue
        try:
            price = float(row.get("basket_price_rub", ""))
        except (ValueError, TypeError):
            continue
        if price <= 0:
            continue
        cents = int(round(price * 100))
        if cents <= upper_cents:
            eligible.append((row, cents))

    # сумма в копейках -> индексы выбранных товаров
    dp: dict[int, tuple[int, ...]] = {0: ()}

    def state_score(selected: tuple[int, ...]) -> tuple[int, int]:
        category_count = len({eligible[i][0].get("category", "") for i in selected})
        return category_count, len(selected)

    for item_index, (_, cents) in enumerate(eligible):
        old_states = list(dp.items())
        for total, selected in old_states:
            new_total = total + cents
            if new_total > upper_cents:
                continue
            candidate = selected + (item_index,)
            existing = dp.get(new_total)
            if existing is None or state_score(candidate) > state_score(existing):
                dp[new_total] = candidate

    feasible = [
        (total, selected)
        for total, selected in dp.items()
        if lower_cents <= total <= upper_cents
    ]

    if not feasible:
        return [], 0.0, lower, upper

    def final_score(item: tuple[int, tuple[int, ...]]) -> tuple[int, int, int]:
        total, selected = item
        categories, count = state_score(selected)
        return categories, count, -abs(total - midpoint_cents)

    best_total, best_selected = max(feasible, key=final_score)
    chosen = [eligible[i][0] for i in best_selected]
    return chosen, best_total / 100, lower, upper


def make_basket_report(rows: list[dict[str, Any]], mrot_rub: float) -> None:
    quarter = mrot_rub / 4
    chosen, total, lower, upper = optimize_basket(rows, mrot_rub)

    lines = [
        "РАСЧЁТ ПРОДУКТОВОГО НАБОРА",
        f"Дата сбора: {date.today().isoformat()}",
        f"Магазин: {STORE}",
        f"Федеральный МРОТ: {mrot_rub:.2f} руб.",
        f"1/4 МРОТ: {quarter:.2f} руб.",
        f"Допустимый диапазон ±5%: {lower:.2f}–{upper:.2f} руб.",
        "",
        "Методика: для упакованных товаров учитывается одна упаковка;",
        "для товаров с ценой за килограмм — 1 кг. Непродовольственные товары",
        "и корм для животных не включаются в продуктовый набор.",
        "Алгоритм подбирает набор в диапазоне, максимизируя число категорий,",
        "затем количество товаров. Это расчётный учебный набор, не официальный",
        "состав потребительской корзины.",
        "",
    ]

    if not chosen:
        lines.extend([
            "Не удалось подобрать набор в допустимом диапазоне по текущим ценам.",
            "Проверь ассортимент, цены и допустимые единицы покупки.",
        ])
    else:
        for row in chosen:
            lines.append(
                f"[{row['product_id']}] {row['category']}: {row['product_name']} — "
                f"{float(row['basket_price_rub']):.2f} руб. ({row.get('basket_unit') or '1 упаковка'})"
            )
        category_count = len({row["category"] for row in chosen})
        lines.extend([
            "",
            f"Итого: {total:.2f} руб.",
            f"Выбрано товаров: {len(chosen)}",
            f"Представлено категорий: {category_count}",
            f"Целевой диапазон: {lower:.2f}–{upper:.2f} руб.",
            "Результат: в пределах ±5% от 1/4 МРОТ.",
        ])

    REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")
    print("\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# Главный запуск
# ---------------------------------------------------------------------------


def main() -> None:
    print("=== ЕЖЕНЕДЕЛЬНЫЙ СБОР ЦЕН МАГНИТ ===")

    try:
        products, config = read_registry()
    except (OSError, ValueError, tomllib.TOMLDecodeError) as error:
        print("Ошибка реестра:", error)
        return

    mrot_rub = float(config.get("mrot_rub", DEFAULT_MROT_RUB))
    print(f"Товаров в TOML: {len(products)} (проверка пройдена)")
    print("Категорий:", len({p["category"] for p in products}))

    session = requests.Session()
    current_rows: list[dict[str, Any]] = []
    errors: list[tuple[str, str, str]] = []

    for index, product in enumerate(products, start=1):
        print(f"[{index}/{len(products)}] {product['name']}")
        row, status = fetch_product(session, product)

        if row:
            current_rows.append(row)
            if row["price_per_kg_or_l_rub"]:
                print(
                    f"  Цена: {row['package_price_rub']} руб.; "
                    f"за кг/л: {row['price_per_kg_or_l_rub']} руб./{row['normalized_unit']}"
                )
            else:
                print(
                    f"  Цена: {row['package_price_rub']} руб.; "
                    "вес/объём не определён — нормированная цена не записана"
                )
            print(f"  Источник: {status}")
        else:
            errors.append((product["id"], product["name"], status))
            print("  Не получено:", status)

        time.sleep(REQUEST_DELAY_SECONDS)

    print("\nУспешно получено:", len(current_rows), "из", len(products))
    print("Ошибок сбора:", len(errors))

    if not current_rows:
        print("Нет актуальных цен. История и существующие CSV не изменены.")
        write_errors(errors)
        return

    write_csv(CURRENT_FILE, current_rows, FIELDS)
    history = update_history(current_rows)
    make_items_dictionary(products, history)
    make_wide_dataset(history, products)
    write_normalization_issues(current_rows)
    write_errors(errors)
    make_basket_report(current_rows, mrot_rub)

    print("\nСозданы/обновлены файлы:")
    print("- products.csv — цены текущего запуска")
    print("- price_history.csv — история по неделям")
    print("- dataset_wide.csv — индексная таблица, числовые цены за кг/л")
    print("- items.csv — расшифровка item_001 ... item_053")
    print("- basket_report.txt — проверка набора относительно 1/4 МРОТ ±5%")
    if NORMALIZATION_FILE.exists():
        print("- normalization_issues.csv — товары без достоверной массы/объёма")
    if ERRORS_FILE.exists():
        print("- parse_errors.csv — товары, которые не удалось обработать")

    if len(current_rows) != len(products):
        print(
            "\nВнимание: не все товары собраны. История успешных товаров сохранена; "
            "ошибки перечислены в parse_errors.csv."
        )
    else:
        print("\nВсе товары реестра обработаны успешно.")


if __name__ == "__main__":
    main()
