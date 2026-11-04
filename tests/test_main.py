import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import main


class RegistryTests(unittest.TestCase):
    def test_grouped_registry_has_all_products(self):
        products, config = main.read_registry()
        self.assertEqual(len(products), 53)
        self.assertEqual(len({p["category"] for p in products}), 14)
        self.assertEqual(config["expected_products"], 53)
        self.assertEqual(len({p["id"] for p in products}), 53)


class MeasurementTests(unittest.TestCase):
    def test_grams_to_kilograms(self):
        measure = main.extract_amount_from_text("Макароны 450г")
        self.assertEqual(measure, (450.0, "g", 0.45, "кг"))

    def test_millilitres_to_litres(self):
        measure = main.extract_amount_from_text("Молоко 930мл")
        self.assertEqual(measure, (930.0, "ml", 0.93, "л"))

    def test_structured_weight_precedes_unrelated_volume(self):
        product = {
            "additionalProperty": [
                {"name": "Объем, л", "value": "0.9109"},
                {"name": "Вес, кг", "value": "0.45"},
            ]
        }
        measure = main.measurement_from_product_object(product)
        self.assertEqual(measure, (0.45, "kg", 0.45, "кг"))

    def test_explicit_unit_price_uses_one_kilogram(self):
        product = {
            "price_basis": "kg",
            "amount_override": None,
            "unit_override": "",
            "name": "Лук репчатый",
        }
        self.assertEqual(
            main.resolve_measurement(product, None, "Лук репчатый"),
            (1.0, "kg", 1.0, "кг"),
        )


class PriceTests(unittest.TestCase):
    def test_jsonld_price_matches_sku(self):
        product = {
            "page_id": "1000531627",
            "id": "1",
            "name": "Горошек зеленый Green Ribbon 450г/460г",
        }
        page = main.PageDataParser()
        page.scripts = [(
            "application/ld+json",
            '{"@type":"Product","sku":"1000531627",'
            '"offers":{"@type":"Offer","price":110.99,"priceCurrency":"RUB"}}'
        )]
        obj, price = main.extract_jsonld_product(page, product)
        self.assertIsNotNone(obj)
        self.assertEqual(price, 110.99)

    def test_jsonld_does_not_take_another_sku(self):
        product = {
            "page_id": "1000531627",
            "id": "1",
            "name": "Горошек зеленый Green Ribbon 450г/460г",
        }
        page = main.PageDataParser()
        page.scripts = [(
            "application/ld+json",
            '{"@type":"Product","sku":"9999999999",'
            '"offers":{"price":1.00,"priceCurrency":"RUB"}}'
        )]
        obj, price = main.extract_jsonld_product(page, product)
        self.assertIsNone(obj)
        self.assertIsNone(price)


class BasketTests(unittest.TestCase):
    def test_basket_is_selected_inside_mrot_band(self):
        rows = [
            {"product_id": "1", "category": "Овощи", "basket_eligible": "1", "basket_price_rub": "3000"},
            {"product_id": "2", "category": "Молочные", "basket_eligible": "1", "basket_price_rub": "2000"},
            {"product_id": "3", "category": "Бакалея", "basket_eligible": "1", "basket_price_rub": "1800"},
            {"product_id": "4", "category": "Непродовольственные", "basket_eligible": "0", "basket_price_rub": "1000"},
        ]
        chosen, total, lower, upper = main.optimize_basket(rows, 27093.0)
        self.assertTrue(lower <= total <= upper)
        self.assertEqual({r["product_id"] for r in chosen}, {"1", "2", "3"})


if __name__ == "__main__":
    unittest.main()
