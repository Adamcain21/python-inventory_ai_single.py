"""
Inventory AI — regional supplier catalog (Québec, Ontario)
==========================================================
Real foodservice distributors per region (names/cities from public directories:
foodcodirectory.com Québec & Ontario lists, groupex.com "Top 30 food distributors
in Canada"), with ESTIMATED prices.

IMPORTANT: foodservice distributors don't publish prices (they are negotiated per
customer). Every price generated here is an estimate (price_source="estimate"),
shown as such in the UI and by the AI, until the restaurant enters its real price
(from an invoice / price list) -- then it becomes "confirmed" and is never
overwritten by the catalog again.

To open a new region: add it to REGIONS and SUPPLIERS_BY_REGION below.
To add products: add them to PRODUCTS (and French/English words to PRODUCT_ALIASES).
Existing databases pick up new products/suppliers automatically at startup.
"""

import difflib
import hashlib
from typing import Dict, List, Optional

REGIONS = [
    # code, name, currency, price factor vs. the base price list
    ("QC", "Québec", "CAD", 1.00),
    ("ON", "Ontario", "CAD", 0.97),
]

# Estimated wholesale base prices (CAD, 2026) per unit, and the usual pack size.
# name: (unit, base_price, case_size or None, category)
PRODUCTS: Dict[str, tuple] = {
    # ── produce ──
    "carrot": ("unit", 0.22, 50, "produce"),
    "green_beans": ("kg", 7.00, 5, "produce"),
    "onion": ("kg", 2.00, 10, "produce"),
    "red_onion": ("kg", 2.40, 10, "produce"),
    "green_onion": ("bunch", 0.90, 48, "produce"),
    "garlic": ("kg", 11.00, 1, "produce"),
    "lettuce": ("kg", 5.50, 2.5, "produce"),
    "spinach": ("kg", 9.00, 1.1, "produce"),
    "potato": ("kg", 1.60, 22.7, "produce"),
    "sweet_potato": ("kg", 3.00, 18, "produce"),
    "tomato": ("kg", 4.50, 11, "produce"),
    "cucumber": ("unit", 0.80, 24, "produce"),
    "bell_pepper": ("kg", 6.50, 5, "produce"),
    "jalapeno": ("kg", 6.00, 4.5, "produce"),
    "mushroom": ("kg", 8.00, 2.3, "produce"),
    "broccoli": ("kg", 5.50, 9, "produce"),
    "cabbage": ("kg", 1.80, 22, "produce"),
    "celery": ("bunch", 2.20, 24, "produce"),
    "zucchini": ("kg", 4.50, 9, "produce"),
    "avocado": ("unit", 1.40, 48, "produce"),
    "lemon": ("unit", 0.45, 100, "produce"),
    "lime": ("unit", 0.35, 100, "produce"),
    "orange": ("unit", 0.60, 88, "produce"),
    "apple": ("kg", 3.20, 18, "produce"),
    "banana": ("kg", 1.90, 18, "produce"),
    "strawberry": ("kg", 9.00, 3.6, "produce"),
    "cilantro": ("bunch", 1.10, 30, "produce"),
    "parsley": ("bunch", 1.10, 30, "produce"),
    "basil": ("bunch", 2.50, 12, "produce"),
    # ── meat ──
    "chicken": ("unit", 13.00, 6, "meat"),
    "chicken_breast": ("kg", 15.00, 4, "meat"),
    "chicken_thigh": ("kg", 10.50, 4, "meat"),
    "chicken_wings": ("kg", 10.00, 4.5, "meat"),
    "steak": ("kg", 38.00, 5, "meat"),
    "ground_beef": ("kg", 11.50, 5, "meat"),
    "beef_brisket": ("kg", 17.00, 6, "meat"),
    "smoked_meat": ("kg", 30.00, 2, "meat"),
    "veal": ("kg", 30.00, 3, "meat"),
    "lamb": ("kg", 28.00, 3, "meat"),
    "pork_chop": ("kg", 12.00, 5, "meat"),
    "pork_belly": ("kg", 11.00, 5, "meat"),
    "pork_ribs": ("kg", 12.00, 5, "meat"),
    "bacon": ("kg", 14.00, 2.3, "meat"),
    "ham": ("kg", 12.00, 3, "meat"),
    "sausage": ("kg", 11.00, 4.5, "meat"),
    "turkey_breast": ("kg", 16.00, 4, "meat"),
    "hot_dog": ("unit", 0.55, 48, "meat"),
    # ── seafood ──
    "salmon": ("kg", 26.00, 5, "seafood"),
    "cod": ("kg", 20.00, 5, "seafood"),
    "tuna": ("kg", 35.00, 3, "seafood"),
    "mussels": ("kg", 6.00, 2.3, "seafood"),
    "scallops": ("kg", 55.00, 1, "seafood"),
    # ── frozen ──
    "shrimp": ("kg", 22.00, 2, "frozen"),
    "french_fries": ("kg", 3.20, 13.6, "frozen"),
    "onion_rings": ("kg", 6.00, 9, "frozen"),
    "burger_patty": ("unit", 1.60, 40, "frozen"),
    "frozen_vegetables": ("kg", 4.00, 9, "frozen"),
    "frozen_berries": ("kg", 7.00, 5, "frozen"),
    "ice_cream": ("L", 5.00, 11.4, "frozen"),
    # ── dairy & eggs ──
    "eggs": ("dozen", 4.20, 15, "dairy"),
    "milk": ("L", 1.90, 4, "dairy"),
    "cream": ("L", 6.50, 2, "dairy"),
    "sour_cream": ("L", 5.00, 2, "dairy"),
    "yogurt": ("kg", 4.50, 2, "dairy"),
    "butter": ("kg", 11.00, 1, "dairy"),
    "cheese": ("kg", 16.00, 2.3, "dairy"),
    "mozzarella": ("kg", 13.00, 2.3, "dairy"),
    "parmesan": ("kg", 30.00, 1, "dairy"),
    "cream_cheese": ("kg", 12.00, 1.4, "dairy"),
    "cheese_curds": ("kg", 16.00, 2.3, "dairy"),
    # ── bakery ──
    "bread": ("loaf", 3.25, 12, "bakery"),
    "burger_buns": ("unit", 0.45, 48, "bakery"),
    "hot_dog_buns": ("unit", 0.35, 48, "bakery"),
    "baguette": ("unit", 2.50, 20, "bakery"),
    "bagel": ("unit", 0.80, 36, "bakery"),
    "croissant": ("unit", 1.10, 48, "bakery"),
    "tortilla": ("unit", 0.35, 72, "bakery"),
    "pita": ("unit", 0.50, 60, "bakery"),
    "pizza_dough": ("unit", 1.20, 30, "bakery"),
    # ── dry goods & spices ──
    "flour": ("kg", 1.40, 20, "dry"),
    "sugar": ("kg", 1.70, 20, "dry"),
    "brown_sugar": ("kg", 2.50, 10, "dry"),
    "rice": ("kg", 2.60, 20, "dry"),
    "oats": ("kg", 2.80, 10, "dry"),
    "breadcrumbs": ("kg", 4.00, 5, "dry"),
    "cornstarch": ("kg", 3.50, 2, "dry"),
    "baking_powder": ("kg", 9.00, 1, "dry"),
    "yeast": ("kg", 12.00, 0.5, "dry"),
    "salt": ("kg", 1.20, 10, "dry"),
    "black_pepper": ("kg", 22.00, 1, "dry"),
    "paprika": ("kg", 18.00, 0.5, "dry"),
    "cumin": ("kg", 20.00, 0.5, "dry"),
    "oregano": ("kg", 25.00, 0.5, "dry"),
    "cinnamon": ("kg", 20.00, 0.5, "dry"),
    "garlic_powder": ("kg", 14.00, 0.5, "dry"),
    "chili_flakes": ("kg", 18.00, 0.5, "dry"),
    "soup_base": ("kg", 15.00, 1, "dry"),
    "gravy_mix": ("kg", 11.00, 1, "dry"),
    "honey": ("kg", 10.00, 3, "dry"),
    "maple_syrup": ("L", 22.00, 4, "dry"),
    "coffee": ("kg", 24.00, 1, "dry"),
    "tea": ("box", 6.00, 6, "dry"),
    "hot_chocolate": ("kg", 12.00, 1, "dry"),
    # ── oils, canned, Mediterranean ──
    "oil": ("L", 5.50, 16, "mediterranean"),
    "olive_oil": ("L", 10.50, 12, "mediterranean"),
    "pasta": ("kg", 3.20, 10, "mediterranean"),
    "crushed_tomatoes_can": ("can", 3.20, 6, "mediterranean"),
    "tomato_sauce_can": ("can", 4.50, 6, "mediterranean"),
    "tomato_paste_can": ("can", 5.00, 12, "mediterranean"),
    "chickpeas_can": ("can", 2.50, 12, "mediterranean"),
    "coconut_milk_can": ("can", 2.50, 12, "mediterranean"),
    "olives": ("kg", 9.00, 2, "mediterranean"),
    "balsamic_vinegar": ("L", 9.00, 4, "mediterranean"),
    # ── condiments & sauces ──
    "mayonnaise": ("L", 4.50, 4, "condiment"),
    "ketchup": ("L", 3.20, 4, "condiment"),
    "mustard": ("L", 4.20, 4, "condiment"),
    "relish": ("L", 4.50, 4, "condiment"),
    "hot_sauce": ("L", 9.00, 4, "condiment"),
    "bbq_sauce": ("L", 5.50, 4, "condiment"),
    "soy_sauce": ("L", 4.00, 4, "condiment"),
    "vinegar": ("L", 1.50, 4, "condiment"),
    "salad_dressing": ("L", 7.00, 4, "condiment"),
    "pickles": ("L", 4.00, 4, "condiment"),
    # ── beverages (soft drinks and water counted by the case of 24) ──
    "coke": ("case", 19.00, None, "beverage"),
    "diet_coke": ("case", 19.00, None, "beverage"),
    "sprite": ("case", 19.00, None, "beverage"),
    "pepsi": ("case", 18.50, None, "beverage"),
    "diet_pepsi": ("case", 18.50, None, "beverage"),
    "ginger_ale": ("case", 18.50, None, "beverage"),
    "water_bottle": ("case", 6.50, None, "beverage"),
    "sparkling_water": ("case", 17.00, None, "beverage"),
    "orange_juice": ("L", 3.50, 4, "beverage"),
    "apple_juice": ("L", 2.50, 4, "beverage"),
    # ── packaging & supplies ──
    "takeout_container": ("unit", 0.25, 200, "packaging"),
    "paper_cup": ("unit", 0.12, 1000, "packaging"),
    "napkin": ("unit", 0.015, 4000, "packaging"),
    "gloves": ("unit", 0.05, 1000, "packaging"),
    "aluminum_foil": ("roll", 25.00, 1, "packaging"),
    "plastic_wrap": ("roll", 22.00, 1, "packaging"),
}

# Words staff use -> catalog product (French and English). Used to find the right product.
PRODUCT_ALIASES: Dict[str, str] = {
    "carotte": "carrot", "carottes": "carrot", "haricots verts": "green_beans", "oignon": "onion",
    "oignons": "onion", "oignon rouge": "red_onion", "oignon vert": "green_onion", "échalote": "green_onion",
    "ail": "garlic", "laitue": "lettuce", "épinards": "spinach", "patate": "potato", "patates": "potato",
    "pomme de terre": "potato", "pommes de terre": "potato", "patate douce": "sweet_potato", "tomate": "tomato",
    "tomates": "tomato", "concombre": "cucumber", "poivron": "bell_pepper", "piment jalapeño": "jalapeno",
    "champignon": "mushroom", "champignons": "mushroom", "brocoli": "broccoli", "chou": "cabbage",
    "céleri": "celery", "courgette": "zucchini", "avocat": "avocado", "citron": "lemon", "lime": "lime",
    "orange": "orange", "pomme": "apple", "pommes": "apple", "banane": "banana", "fraise": "strawberry",
    "fraises": "strawberry", "coriandre": "cilantro", "persil": "parsley", "basilic": "basil",
    "poulet": "chicken", "poitrine de poulet": "chicken_breast", "cuisse de poulet": "chicken_thigh",
    "ailes de poulet": "chicken_wings", "wings": "chicken_wings", "boeuf haché": "ground_beef",
    "bœuf haché": "ground_beef", "poitrine de boeuf": "beef_brisket", "viande fumée": "smoked_meat",
    "veau": "veal", "agneau": "lamb", "côtelette de porc": "pork_chop", "flanc de porc": "pork_belly",
    "côtes levées": "pork_ribs", "ribs": "pork_ribs", "jambon": "ham", "saucisse": "sausage",
    "saucisses": "sausage", "dinde": "turkey_breast", "hot-dog": "hot_dog", "saucisse hot-dog": "hot_dog",
    "saumon": "salmon", "morue": "cod", "thon": "tuna", "moules": "mussels", "pétoncles": "scallops",
    "crevettes": "shrimp", "crevette": "shrimp", "frites": "french_fries", "fries": "french_fries",
    "rondelles d'oignon": "onion_rings", "boulettes": "burger_patty", "galette de burger": "burger_patty",
    "patty": "burger_patty", "légumes surgelés": "frozen_vegetables", "petits fruits": "frozen_berries",
    "crème glacée": "ice_cream", "oeufs": "eggs", "œufs": "eggs", "oeuf": "eggs", "lait": "milk",
    "crème": "cream", "crème 35": "cream", "crème sure": "sour_cream", "yogourt": "yogurt", "beurre": "butter",
    "fromage": "cheese", "cheddar": "cheese", "parmesan": "parmesan", "fromage à la crème": "cream_cheese",
    "fromage en grains": "cheese_curds", "fromage en crottes": "cheese_curds", "curds": "cheese_curds",
    "pain": "bread", "pains à burger": "burger_buns", "pain à burger": "burger_buns", "buns": "burger_buns",
    "pains à hot-dog": "hot_dog_buns", "croissants": "croissant", "tortillas": "tortilla",
    "pâte à pizza": "pizza_dough", "farine": "flour", "sucre": "sugar", "cassonade": "brown_sugar", "riz": "rice",
    "gruau": "oats", "chapelure": "breadcrumbs", "fécule de maïs": "cornstarch", "poudre à pâte": "baking_powder",
    "levure": "yeast", "sel": "salt", "poivre": "black_pepper", "cumin": "cumin", "origan": "oregano",
    "cannelle": "cinnamon", "ail en poudre": "garlic_powder", "flocons de piment": "chili_flakes",
    "base de soupe": "soup_base", "bouillon": "soup_base", "sauce poutine": "gravy_mix", "sauce brune": "gravy_mix",
    "gravy": "gravy_mix", "miel": "honey", "sirop d'érable": "maple_syrup", "café": "coffee", "thé": "tea",
    "chocolat chaud": "hot_chocolate", "huile": "oil", "huile de canola": "oil", "huile végétale": "oil",
    "huile d'olive": "olive_oil", "pâtes": "pasta", "spaghetti": "pasta", "tomates broyées": "crushed_tomatoes_can",
    "tomates concassées": "crushed_tomatoes_can", "sauce tomate": "tomato_sauce_can", "pâte de tomate": "tomato_paste_can",
    "pois chiches": "chickpeas_can", "lait de coco": "coconut_milk_can", "olives": "olives",
    "vinaigre balsamique": "balsamic_vinegar", "mayo": "mayonnaise", "mayonnaise": "mayonnaise",
    "moutarde": "mustard", "sauce piquante": "hot_sauce", "sriracha": "hot_sauce", "sauce bbq": "bbq_sauce",
    "sauce soya": "soy_sauce", "vinaigre": "vinegar", "vinaigrette": "salad_dressing", "cornichons": "pickles",
    "coca": "coke", "coca-cola": "coke", "coca cola": "coke", "coke diète": "diet_coke", "coke diet": "diet_coke",
    "coke zero": "diet_coke", "7up": "sprite", "pepsi diète": "diet_pepsi", "ginger ale": "ginger_ale",
    "eau": "water_bottle", "bouteilles d'eau": "water_bottle", "eau pétillante": "sparkling_water",
    "jus d'orange": "orange_juice", "jus de pomme": "apple_juice", "contenants": "takeout_container",
    "contenant": "takeout_container", "verres en carton": "paper_cup", "gobelets": "paper_cup",
    "serviettes": "napkin", "gants": "gloves", "papier d'aluminium": "aluminum_foil", "papier alu": "aluminum_foil",
    "pellicule plastique": "plastic_wrap", "saran": "plastic_wrap",
}

# French display names (shown in the web UI, searched by the filters).
FRENCH_NAMES: Dict[str, str] = {
    "carrot": "Carotte", "green_beans": "Haricots verts", "onion": "Oignon", "red_onion": "Oignon rouge",
    "green_onion": "Oignon vert", "garlic": "Ail", "lettuce": "Laitue", "spinach": "Épinards",
    "potato": "Pomme de terre", "sweet_potato": "Patate douce", "tomato": "Tomate", "cucumber": "Concombre",
    "bell_pepper": "Poivron", "jalapeno": "Jalapeño", "mushroom": "Champignon", "broccoli": "Brocoli",
    "cabbage": "Chou", "celery": "Céleri", "zucchini": "Courgette", "avocado": "Avocat", "lemon": "Citron",
    "lime": "Lime", "orange": "Orange", "apple": "Pomme", "banana": "Banane", "strawberry": "Fraise",
    "cilantro": "Coriandre", "parsley": "Persil", "basil": "Basilic",
    "chicken": "Poulet entier", "chicken_breast": "Poitrine de poulet", "chicken_thigh": "Cuisse de poulet",
    "chicken_wings": "Ailes de poulet", "steak": "Steak (bœuf)", "ground_beef": "Bœuf haché",
    "beef_brisket": "Poitrine de bœuf (brisket)", "smoked_meat": "Viande fumée", "veal": "Veau", "lamb": "Agneau",
    "pork_chop": "Côtelette de porc", "pork_belly": "Flanc de porc", "pork_ribs": "Côtes levées",
    "bacon": "Bacon", "ham": "Jambon", "sausage": "Saucisse", "turkey_breast": "Poitrine de dinde",
    "hot_dog": "Saucisse à hot-dog",
    "salmon": "Saumon", "cod": "Morue", "tuna": "Thon", "mussels": "Moules", "scallops": "Pétoncles",
    "shrimp": "Crevettes", "french_fries": "Frites (surgelées)", "onion_rings": "Rondelles d'oignon",
    "burger_patty": "Galette de burger", "frozen_vegetables": "Légumes surgelés",
    "frozen_berries": "Petits fruits surgelés", "ice_cream": "Crème glacée",
    "eggs": "Œufs", "milk": "Lait", "cream": "Crème 35 %", "sour_cream": "Crème sure", "yogurt": "Yogourt",
    "butter": "Beurre", "cheese": "Fromage cheddar", "mozzarella": "Fromage mozzarella", "parmesan": "Fromage parmesan",
    "cream_cheese": "Fromage à la crème", "cheese_curds": "Fromage en grains",
    "bread": "Pain", "burger_buns": "Pains à burger", "hot_dog_buns": "Pains à hot-dog", "baguette": "Baguette",
    "bagel": "Bagel", "croissant": "Croissant", "tortilla": "Tortilla", "pita": "Pita",
    "pizza_dough": "Pâte à pizza",
    "flour": "Farine", "sugar": "Sucre", "brown_sugar": "Cassonade", "rice": "Riz", "oats": "Gruau (avoine)",
    "breadcrumbs": "Chapelure", "cornstarch": "Fécule de maïs", "baking_powder": "Poudre à pâte",
    "yeast": "Levure", "salt": "Sel", "black_pepper": "Poivre noir", "paprika": "Paprika", "cumin": "Cumin",
    "oregano": "Origan", "cinnamon": "Cannelle", "garlic_powder": "Ail en poudre",
    "chili_flakes": "Flocons de piment", "soup_base": "Base de soupe (bouillon)",
    "gravy_mix": "Sauce brune / poutine (mélange)", "honey": "Miel", "maple_syrup": "Sirop d'érable",
    "coffee": "Café", "tea": "Thé", "hot_chocolate": "Chocolat chaud",
    "oil": "Huile (canola / végétale)", "olive_oil": "Huile d'olive", "pasta": "Pâtes",
    "crushed_tomatoes_can": "Tomates broyées (conserve)", "tomato_sauce_can": "Sauce tomate (conserve)",
    "tomato_paste_can": "Pâte de tomate (conserve)", "chickpeas_can": "Pois chiches (conserve)",
    "coconut_milk_can": "Lait de coco (conserve)", "olives": "Olives", "balsamic_vinegar": "Vinaigre balsamique",
    "mayonnaise": "Mayonnaise", "ketchup": "Ketchup", "mustard": "Moutarde", "relish": "Relish",
    "hot_sauce": "Sauce piquante", "bbq_sauce": "Sauce BBQ", "soy_sauce": "Sauce soya", "vinegar": "Vinaigre",
    "salad_dressing": "Vinaigrette", "pickles": "Cornichons",
    "coke": "Coke (Coca-Cola)", "diet_coke": "Coke diète / zéro", "sprite": "Sprite", "pepsi": "Pepsi",
    "diet_pepsi": "Pepsi diète", "ginger_ale": "Ginger ale", "water_bottle": "Eau en bouteille",
    "sparkling_water": "Eau pétillante", "orange_juice": "Jus d'orange", "apple_juice": "Jus de pomme",
    "takeout_container": "Contenant pour emporter", "paper_cup": "Verre en carton", "napkin": "Serviette de table",
    "gloves": "Gants", "aluminum_foil": "Papier d'aluminium", "plastic_wrap": "Pellicule plastique",
}


def plain(text: str) -> str:
    """Lowercase without accents: 'Crème Brûlée' -> 'creme brulee'."""
    import unicodedata
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def search_words(product: str) -> List[str]:
    """Every word/phrase that should find this product: key, French name, aliases."""
    words = {product.replace("_", " "), FRENCH_NAMES.get(product, "")}
    words |= {alias for alias, p in PRODUCT_ALIASES.items() if p == product}
    return sorted(plain(w) for w in words if w)


# Supplier types: which categories they carry and how they price/deliver.
# Delivery terms are per DELIVERY (one per supplier per order), all ESTIMATED:
#   min $ = minimum value of goods for the supplier to deliver, fee = delivery fee,
#   free over $ = goods value from which delivery is free.
SUPPLIER_TYPES = {
    #                categories carried (None = everything)   price  days  reliab.  min $  fee   free over $
    "broadline":     (None,                                    1.00, 1.0,  0.95,   300,   0.0,  None),
    "cash_carry":    (None,                                    0.98, 1.0,  0.95,     0,  15.0,  250),
    "produce":       ({"produce"},                             0.88, 1.0,  0.90,   150,  20.0,  300),
    "mediterranean": ({"mediterranean", "condiment"},          0.93, 3.0,  0.90,   200,  25.0,  500),
    "frozen":        ({"frozen"},                              0.85, 3.0,  0.90,   200,  25.0,  500),
    "beverage":      (set(),                                   0.93, 2.0,  0.97,   100,   0.0,  None),   # explicit list
}
FROZEN_EXTRA = {"green_beans"}   # frozen-veg specialists also carry frozen green beans
PRICE_SPREAD = 0.12              # each supplier's estimate varies +-12% per product

COCA_COLA = {"coke", "diet_coke", "sprite", "ginger_ale", "water_bottle", "sparkling_water", "orange_juice"}
PEPSICO = {"pepsi", "diet_pepsi", "ginger_ale", "water_bottle", "sparkling_water", "orange_juice", "apple_juice"}

SUPPLIERS_BY_REGION: Dict[str, List[tuple]] = {
    # name, city, type, delivery days override (None = type default), explicit product list (None = by type)
    "QC": [
        ("Sysco Québec", "Grand Montréal, QC", "broadline", None, None),
        ("Gordon Food Service (GFS) Québec", "QC", "broadline", None, None),
        ("Colabor", "Boucherville, QC", "broadline", None, None),
        ("FLB Solutions Alimentaires", "QC", "broadline", None, None),
        ("Mayrand Plus", "Anjou, QC", "broadline", None, None),
        ("Multi Plus DM", "Dorval, QC", "broadline", 2.0, None),
        ("Dorfin", "Saint-Laurent, QC", "broadline", 2.0, None),
        ("Zast Foods", "QC", "broadline", 2.0, None),
        ("Denali Foods", "Montréal, QC", "broadline", 2.0, None),
        ("Costco Entreprise", "QC", "cash_carry", None, None),
        ("Chenail Import-Export", "Montréal, QC", "produce", None, None),
        ("Thomas Fruits et Légumes", "Québec, QC", "produce", None, None),
        ("Mantab Food Group", "Dorval, QC", "mediterranean", None, None),
        ("Fantis Foods Canada", "Montréal, QC", "mediterranean", None, None),
        ("Krinos Foods Canada", "Montréal, QC", "mediterranean", None, None),
        ("Aldo Foods", "Montréal, QC", "mediterranean", None, None),
        ("Ferma Food Products", "Montréal, QC", "mediterranean", None, None),
        ("Alasko Food", "Montréal, QC", "frozen", None, None),
        ("Brecon Foods", "Pointe-Claire, QC", "frozen", None, None),
        ("Coca-Cola Canada Bottling", "QC", "beverage", None, COCA_COLA),
        ("PepsiCo Canada", "QC", "beverage", None, PEPSICO),
    ],
    "ON": [
        ("Sysco Ontario", "ON", "broadline", None, None),
        ("Gordon Food Service (GFS)", "Milton, ON", "broadline", None, None),
        ("Flanagan Foodservice", "Kitchener, ON", "broadline", None, None),
        ("Findlay Foods", "Kingston, ON", "broadline", 2.0, None),
        ("Quattrocchi Food Services", "Smiths Falls, ON", "broadline", 2.0, None),
        ("Mercury Foodservice", "Hamilton, ON", "broadline", None, None),
        ("Stewart Foodservice", "ON", "broadline", 2.0, None),
        ("Kronos Foods", "Toronto, ON", "broadline", None, None),
        ("Lorenz Food Distributors", "Mississauga, ON", "broadline", None, None),
        ("Morton Food Service", "Windsor, ON", "broadline", 2.0, None),
        ("Reality Foods Service", "Concord, ON", "broadline", None, None),
        ("Demenz Hotel & Restaurant Supplies", "Scarborough, ON", "broadline", None, None),
        ("H & H Quality Foods", "Brampton, ON", "broadline", None, None),
        ("Costco Business Centre", "ON", "cash_carry", None, None),
        ("Canadian Fruit & Produce Company", "Ontario Food Terminal, Toronto", "produce", None, None),
        ("North American Produce Buyers", "Ontario Food Terminal, Toronto", "produce", None, None),
        ("Amodeo Produce", "Ontario Food Terminal, Toronto", "produce", None, None),
        ("Tomato King 2010", "Toronto, ON", "produce", None, None),
        ("J.E. Russell Produce", "Toronto, ON", "produce", None, None),
        ("Gambles Produce", "Toronto, ON", "produce", None, None),
        ("F.G. Lister", "Toronto, ON", "produce", None, None),
        ("Bamford Produce", "Mississauga, ON", "produce", None, None),
        ("Koornneef Produce", "Grimsby, ON", "produce", 2.0, None),
        ("Sabrina Wholesale Foods", "Vaughan, ON", "mediterranean", None, None),
        ("Fantis Foods Canada", "Etobicoke, ON", "mediterranean", None, None),
        ("Krinos Foods Canada", "Vaughan, ON", "mediterranean", None, None),
        ("Legacy Distributors", "Woodbridge, ON", "mediterranean", None, None),
        ("Nobel Importing & Distributing", "Woodbridge, ON", "mediterranean", None, None),
        ("Ricco Food Distributors", "Strathroy, ON", "mediterranean", 2.0, None),
        ("Coca-Cola Canada Bottling", "ON", "beverage", None, COCA_COLA),
        ("PepsiCo Canada", "ON", "beverage", None, PEPSICO),
    ],
}


def _spread(*parts: str, width: float) -> float:
    """Stable pseudo-random value in [-width, +width] (same result on every run)."""
    digest = hashlib.md5("|".join(parts).encode()).digest()
    return (int.from_bytes(digest[:4], "big") / 0xFFFFFFFF * 2 - 1) * width


def _carries(supplier_type: str, product: str, explicit: Optional[set]) -> bool:
    if explicit is not None:
        return product in explicit
    categories = SUPPLIER_TYPES[supplier_type][0]
    if categories is None:
        return True
    if supplier_type == "frozen" and product in FROZEN_EXTRA:
        return True
    return PRODUCTS[product][3] in categories


def build_offers() -> List[dict]:
    """Every (region, supplier, product) offer, ready for the suppliers table."""
    region_factor = {code: factor for code, _, _, factor in REGIONS}
    offers = []
    for region, suppliers in SUPPLIERS_BY_REGION.items():
        for name, city, stype, days_override, explicit in suppliers:
            _, price_factor, days, reliability, _min, _fee, _free = SUPPLIER_TYPES[stype]
            for product, (unit, base, case, _category) in PRODUCTS.items():
                if not _carries(stype, product, explicit):
                    continue
                price = base * price_factor * region_factor[region] * (1 + _spread(region, name, product,
                                                                                   width=PRICE_SPREAD))
                offers.append({
                    "name": name,
                    "ingredient_name": product,
                    "region_code": region,
                    "price_per_unit": round(price, 3 if price < 1 else 2),
                    "delivery_days": days_override if days_override is not None else days,
                    "reliability_score": round(min(0.99, reliability + _spread(region, name, width=0.03)), 2),
                    "delivery_fee": 0.0,   # charged per delivery: see build_terms()
                    "free_shipping_at": None,
                    "min_order": 1.0,
                    "case_size": case,
                    "price_source": "estimate",
                    "notes": f"{city} - {stype} - price per {unit} ESTIMATED, confirm with the supplier",
                })
    return offers


def build_terms() -> List[dict]:
    """ESTIMATED delivery terms (minimum order, fee per delivery) of every supplier, per region."""
    terms = []
    for region, suppliers in SUPPLIERS_BY_REGION.items():
        for name, _city, stype, _days, _explicit in suppliers:
            _, _p, _d, _r, minimum, fee, free_over = SUPPLIER_TYPES[stype]
            terms.append({"name": name, "region_code": region, "min_order_value": float(minimum),
                          "delivery_fee": fee, "free_delivery_over": free_over, "price_source": "estimate"})
    return terms


def exact_product(word: str) -> Optional[str]:
    """Catalog product for an exact French/English word ('miel' -> honey), else None. No guessing."""
    text = " ".join(plain(word).replace("_", " ").replace("-", " ").split())
    for candidate in (text, text[:-1] if text.endswith("s") else None):
        if not candidate:
            continue
        for product in PRODUCTS:
            if candidate in (w.replace("-", " ") for w in search_words(product)):
                return product
    return None


def find_products(query: str, limit: int = 5) -> List[str]:
    """Catalog products matching what the staff wrote ('mayo', 'Coca-Cola', 'frites', 'burger bun'...)."""
    text = " ".join(plain(query).replace("_", " ").replace("-", " ").split())
    key = text.replace(" ", "_")
    found: List[str] = []

    def add(product):
        if product in PRODUCTS and product not in found:
            found.append(product)

    add(key)
    if key.endswith("s"):
        add(key[:-1])                             # "carrots" -> carrot
    if key.endswith("es"):
        add(key[:-2])                             # "tomatoes" -> tomato
    # exact French/English word: "miel", "fromage en grains", "creme" (accents optional)
    words_by_product = {p: search_words(p) for p in PRODUCTS}
    for product, words in words_by_product.items():
        if text in (w.replace("-", " ") for w in words):
            add(product)
    for singular in {text, text.rstrip("s")}:      # "tomates" -> tomato
        for product, words in words_by_product.items():
            if singular in words:
                add(product)
    stem = key.rstrip("s")
    for product in PRODUCTS:                      # "tomato" -> tomato, crushed_tomatoes_can, tomato_sauce_can...
        if stem and stem in product:
            add(product)
    for product, words in words_by_product.items():   # "tomate" inside "tomates broyees (conserve)"
        if len(text) >= 3 and any(text.rstrip("s") in w for w in words):
            add(product)
    lookup = {w: p for p, words in words_by_product.items() for w in words}
    for query_form in (key.replace("_", " "), text):  # typos: "mayonaise", "ketchupp", "mayonaisse"
        for match in difflib.get_close_matches(query_form, list(lookup), n=limit, cutoff=0.8):
            add(lookup[match])
    return found[:limit]
