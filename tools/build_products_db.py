#!/usr/bin/env python3
"""Baut aus dem Open-Food-Facts-Export einen kompakten SQLite-Auszug.

Ergebnis im Ausgabeordner:
  products.sqlite.gz   die Datenbank für die App (gzip-komprimiert)
  version.json         Version, Anzahl Produkte, Größe, Prüfsumme

Der Export (mehrere GB entpackt) wird beim Lesen gestreamt und nie
vollständig gespeichert. Es werden nur Python-Bordmittel benutzt.

Aufruf:
  python3 tools/build_products_db.py                 # lädt den Export
  python3 tools/build_products_db.py --input x.csv.gz  # lokale Datei

Daten: Open Food Facts, Open Database License (ODbL).
"""
import argparse
import collections
import datetime
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import urllib.request

SOURCE_URL = (
    "https://static.openfoodfacts.org/data/en.openfoodfacts.org.products.csv.gz"
)
USER_AGENT = "essens-inventar-db-build/1.0"
COLUMNS = ("code", "product_name", "brands", "quantity", "countries_tags",
           "categories_tags")
CODE_RE = re.compile(r"^\d{8,14}$")
SPACE_RE = re.compile(r"\s+")

# Nährwerte pro 100 g bzw. 100 ml: Spalte im Export -> (Spalte im Auszug,
# größter plausibler Wert). Fehlt eine Spalte im Export, bleibt sie leer.
NUTRIENTS = [
    ("energy-kcal_100g", "kcal", 900.0),
    ("fat_100g", "fat", 100.0),
    ("saturated-fat_100g", "sat_fat", 100.0),
    ("carbohydrates_100g", "carbs", 100.0),
    ("sugars_100g", "sugars", 100.0),
    ("fiber_100g", "fiber", 100.0),
    ("proteins_100g", "protein", 100.0),
    ("salt_100g", "salt", 100.0),
]
OPTIONAL_COLUMNS = [n[0] for n in NUTRIENTS] + [
    "product_quantity", "product_quantity_unit",
]

AMOUNT_RE = re.compile(
    r"(?:(\d+)\s*[x×*]\s*)?(\d+(?:[.,]\d+)?)\s*(kg|g|ml|cl|dl|l)\b",
    re.IGNORECASE,
)
UNIT_FACTORS = {
    "g": (1, "g"), "kg": (1000, "g"),
    "ml": (1, "ml"), "cl": (10, "ml"), "dl": (100, "ml"), "l": (1000, "ml"),
}


def number(text, maximum):
    """Zahl zwischen 0 und maximum, sonst None (auch bei Tippfehlern)."""
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if value != value or value < 0 or value > maximum:
        return None
    return round(value, 2)


def parse_amount(text):
    """'6 x 330 ml' -> (1980.0, 'ml'); None, wenn nicht lesbar."""
    match = AMOUNT_RE.search(text or "")
    if not match:
        return None
    count = int(match.group(1) or 1)
    value = float(match.group(2).replace(",", "."))
    factor, unit = UNIT_FACTORS[match.group(3).lower()]
    amount = count * value * factor
    if amount <= 0 or amount > 50000:
        return None
    return round(amount, 1), unit


def pack_amount(quantity_text, product_quantity, product_quantity_unit):
    """Inhalt einer Packung in g oder ml als (Menge, Einheit) oder (None, None).

    Bevorzugt den von Open Food Facts berechneten Wert, sonst wird der
    Text von der Packung gelesen."""
    parsed = parse_amount(quantity_text)
    amount = number(product_quantity, 50000)
    if amount:
        unit = (product_quantity_unit or "").strip().lower()
        if unit not in ("g", "ml"):
            unit = parsed[1] if parsed else "g"
        return amount, unit
    return parsed if parsed else (None, None)

# Alltagsgruppen der App -> Kategorien aus Open Food Facts.
# Die Schlüssel links müssen zu lib/models/categories.dart passen.
# Ein Produkt bekommt die Gruppe seiner speziellsten passenden Kategorie
# (categories_tags ist von allgemein nach speziell sortiert).
GROUPS = {
    "nudeln": ["pastas", "noodles", "dry-pastas", "fresh-pastas",
               "instant-noodles", "egg-pastas", "stuffed-pastas"],
    "reis": ["rices", "rice"],
    "huelsenfruechte": ["legumes", "pulses", "lentils", "chickpeas",
                        "legumes-and-their-products", "dried-legumes"],
    "backen": ["flours", "sugars", "baking-mixes", "yeasts", "yeast",
               "baking-powders", "pastry-helpers", "cooking-helpers",
               "baking-decorations", "dessert-mixes", "starches"],
    "muesli": ["breakfast-cereals", "mueslis", "cereal-flakes",
               "rolled-oats", "oat-flakes", "porridges"],
    "brot": ["breads", "crispbreads", "rusks", "viennoiseries", "toasts",
             "wraps", "tortillas", "flatbreads"],
    "konserven": ["canned-foods", "canned-vegetables", "canned-fruits",
                  "canned-fishes", "canned-legumes", "canned-meals",
                  "canned-plant-based-foods", "pickles",
                  "pickled-vegetables"],
    "saucen": ["sauces", "tomato-sauces", "pasta-sauces", "ketchup",
               "mustards", "mayonnaises", "condiments", "dips",
               "salad-dressings", "tomato-purees", "pestos"],
    "gewuerze": ["spices", "salts", "seasonings", "broths", "bouillons",
                 "dehydrated-broths", "herbs-and-spices", "aromatic-herbs",
                 "peppers-spice", "bouillon-cubes"],
    "oel": ["vegetable-oils", "olive-oils", "vinegars", "fats",
            "vegetable-fats", "sunflower-oils", "rapeseed-oils"],
    "aufstriche": ["spreads", "sweet-spreads", "jams", "honeys",
                   "hazelnut-spreads", "chocolate-spreads",
                   "plant-based-spreads", "salted-spreads", "nut-butters",
                   "peanut-butters", "marmalades"],
    "milch": ["dairies", "milks", "yogurts", "fermented-milk-products",
              "creams", "butters", "dairy-desserts", "milk-substitutes",
              "plant-based-milk-alternatives", "dairy-substitutes",
              "desserts", "puddings", "margarines"],
    "kaese": ["cheeses"],
    "eier": ["eggs", "chicken-eggs"],
    "fleisch": ["meats", "prepared-meats", "sausages", "hams", "poultries",
                "meats-and-their-products", "meat-alternatives",
                "meat-analogues"],
    "fisch": ["seafood", "fishes", "fishes-and-their-products",
              "smoked-fishes", "fish-preparations"],
    "tiefkuehl": ["frozen-foods", "frozen-vegetables", "frozen-pizzas",
                  "frozen-ready-made-meals", "frozen-fishes",
                  "frozen-desserts", "frozen-fruits",
                  "frozen-plant-based-foods"],
    "fertig": ["meals", "prepared-meals", "pizzas", "soups",
               "pizzas-pies-and-quiches", "sandwiches", "instant-soups",
               "dehydrated-soups", "prepared-salads"],
    "gemuese": ["vegetables", "fresh-vegetables", "vegetables-based-foods",
                "potatoes", "tomatoes", "mushrooms"],
    "obst": ["fruits", "fruits-based-foods", "dried-fruits", "fresh-fruits",
             "compotes", "apple-compotes", "fruit-purees"],
    "nuesse": ["nuts", "nuts-and-their-products", "seeds", "mixed-nuts"],
    "snacks": ["salty-snacks", "chips-and-fries", "crisps", "appetizers",
               "crackers", "snacks", "popcorn", "pretzels"],
    "suesses": ["sweet-snacks", "confectioneries", "chocolates", "candies",
                "biscuits-and-cakes", "biscuits", "cakes",
                "cocoa-and-its-products", "chocolate-candies", "bars",
                "gummi-candies", "chewing-gum", "pastries"],
    "eis": ["ice-creams-and-sorbets", "ice-creams", "sorbets"],
    "kaffee": ["coffees", "teas", "hot-beverages", "herbal-teas",
               "cocoa-and-chocolate-powders", "instant-beverages",
               "tea-bags", "ground-coffees", "coffee-capsules"],
    "alkohol": ["alcoholic-beverages", "beers", "wines", "spirits",
                "liqueurs", "sparkling-wines"],
    "getraenke": ["beverages", "waters", "sodas", "fruit-juices",
                  "juices-and-nectars", "carbonated-drinks", "syrups",
                  "energy-drinks", "plant-based-beverages", "iced-teas",
                  "mineral-waters", "spring-waters", "fruit-based-beverages",
                  "non-alcoholic-beverages", "sweetened-beverages",
                  "unsweetened-beverages"],
    "baby": ["baby-foods", "baby-milks"],
}
TAG_TO_GROUP = {
    "en:" + tag: group for group, tags in GROUPS.items() for tag in tags
}


def group_for(categories_tags, unmapped):
    """Speziellste passende Gruppe; None, wenn nichts passt."""
    if not categories_tags:
        return None
    tags = categories_tags.split(",")
    for tag in reversed(tags):
        group = TAG_TO_GROUP.get(tag)
        if group:
            return group
    unmapped[tags[-1]] += 1
    return None


INSERT_SQL = "INSERT OR REPLACE INTO products VALUES (" + ",".join(["?"] * 15) + ")"


def clean(text, limit):
    return SPACE_RE.sub(" ", text).strip()[:limit].strip()


def open_source(path):
    """Liefert einen Text-Stream über die entpackte CSV."""
    if path:
        raw = open(path, "rb")
    else:
        request = urllib.request.Request(
            SOURCE_URL, headers={"User-Agent": USER_AGENT}
        )
        raw = urllib.request.urlopen(request, timeout=120)
    unzipped = gzip.GzipFile(fileobj=raw)
    return io.TextIOWrapper(
        unzipped, encoding="utf-8", errors="replace", newline="\n"
    )


def build(args):
    os.makedirs(args.out, exist_ok=True)
    db_path = os.path.join(args.out, "products.sqlite")
    gz_path = db_path + ".gz"
    for path in (db_path, gz_path):
        if os.path.exists(path):
            os.remove(path)

    country_tag = "en:" + args.country
    db = sqlite3.connect(db_path)
    db.execute(
        """CREATE TABLE products (
             barcode  TEXT PRIMARY KEY,
             name     TEXT NOT NULL,
             brand    TEXT,
             quantity TEXT,
             grp      TEXT,
             kcal REAL, fat REAL, sat_fat REAL, carbs REAL, sugars REAL,
             fiber REAL, protein REAL, salt REAL,
             pack_amount REAL,  -- Inhalt einer Packung in g oder ml
             pack_unit   TEXT   -- 'g' oder 'ml'
           ) WITHOUT ROWID"""
    )
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    read = kept = malformed = grouped = with_kcal = with_amount = 0
    unmapped = collections.Counter()
    batch = []
    with open_source(args.input) as lines:
        header = lines.readline().rstrip("\n").split("\t")
        missing = [c for c in COLUMNS if c not in header]
        if missing:
            sys.exit("Spalten fehlen im Export: " + ", ".join(missing))
        i_code, i_name, i_brand, i_qty, i_countries, i_categories = (
            header.index(c) for c in COLUMNS
        )
        width = len(header)
        optional = {
            c: header.index(c) for c in OPTIONAL_COLUMNS if c in header
        }
        absent = [c for c in OPTIONAL_COLUMNS if c not in optional]
        if absent:
            print("Hinweis: Diese Spalten fehlen im Export und bleiben leer: "
                  + ", ".join(absent), flush=True)

        def opt(fields, column):
            index = optional.get(column)
            return fields[index].strip() if index is not None else ""

        for line in lines:
            read += 1
            if read % 500000 == 0:
                print(f"  {read:,} Zeilen gelesen, {kept:,} übernommen",
                      flush=True)
            fields = line.rstrip("\n").split("\t")
            if len(fields) != width:
                malformed += 1
                continue
            if country_tag not in fields[i_countries].split(","):
                continue
            code = fields[i_code].strip()
            name = clean(fields[i_name], 120)
            if not name or not CODE_RE.match(code):
                continue
            brand = clean(fields[i_brand].split(",")[0], 60)
            quantity = clean(fields[i_qty], 30)
            group = group_for(fields[i_categories].strip(), unmapped)
            if group:
                grouped += 1
            nutrients = [
                number(opt(fields, column), maximum)
                for column, _, maximum in NUTRIENTS
            ]
            if nutrients[0] is not None:
                with_kcal += 1
            amount, amount_unit = pack_amount(
                fields[i_qty],
                opt(fields, "product_quantity"),
                opt(fields, "product_quantity_unit"),
            )
            if amount is not None:
                with_amount += 1
            batch.append((code, name, brand or None, quantity or None, group,
                          *nutrients, amount, amount_unit))
            kept += 1
            if len(batch) >= 20000:
                db.executemany(INSERT_SQL, batch)
                batch.clear()
    if batch:
        db.executemany(INSERT_SQL, batch)

    count = db.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    if count < args.min_products:
        sys.exit(
            f"Nur {count} Produkte gefunden (erwartet mindestens "
            f"{args.min_products}). Abbruch, damit keine kaputte "
            "Datenbank veröffentlicht wird."
        )

    version = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
    db.executemany(
        "INSERT INTO meta VALUES (?,?)",
        [
            ("version", version),
            ("product_count", str(count)),
            ("country", args.country),
            ("source", "Open Food Facts (https://openfoodfacts.org)"),
            ("license", "Open Database License (ODbL) 1.0"),
        ],
    )
    db.commit()
    db.execute("VACUUM")
    db.close()

    with open(db_path, "rb") as src, gzip.open(gz_path, "wb", 9) as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)

    sha = hashlib.sha256()
    with open(gz_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha.update(chunk)

    info = {
        "version": version,
        "products": count,
        "file": "products.sqlite.gz",
        "download_bytes": os.path.getsize(gz_path),
        "installed_bytes": os.path.getsize(db_path),
        "sha256": sha.hexdigest(),
    }
    with open(os.path.join(args.out, "version.json"), "w") as f:
        json.dump(info, f, indent=2)
    os.remove(db_path)

    print(f"{read:,} Zeilen gelesen, {malformed:,} fehlerhafte übersprungen")
    share = 100 * grouped / max(kept, 1)
    print(f"{grouped:,} von {kept:,} Produkten haben eine Gruppe ({share:.0f} %)")
    print(f"{with_kcal:,} Produkte mit Brennwert, "
          f"{with_amount:,} mit lesbarem Packungsinhalt")
    print("Häufigste Kategorien ohne Gruppe (zum Erweitern von GROUPS):")
    for tag, n in unmapped.most_common(40):
        print(f"  {n:>7,}  {tag}")
    print(json.dumps(info, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--input", help="lokale .csv.gz statt Download")
    parser.add_argument("--out", default="dist", help="Ausgabeordner")
    parser.add_argument("--country", default="germany",
                        help="Land wie in countries_tags, ohne 'en:'")
    parser.add_argument("--min-products", type=int, default=10000,
                        help="Untergrenze als Schutz vor kaputten Läufen")
    build(parser.parse_args())
