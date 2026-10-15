# Produktdaten für „Vorräte“

Kompakter Auszug aus [Open Food Facts](https://openfoodfacts.org) (nur Produkte
aus Deutschland) für die Vorratsverwaltungs-App „Vorräte“.

Die Dateien liegen im Release [`data`](../../releases/tag/data):

- `products.sqlite.gz` – SQLite-Datenbank (gzip-komprimiert)
- `version.json` – Version, Anzahl Produkte, Größe, SHA-256-Prüfsumme

Der Workflow „Produktdatenbank bauen“ erzeugt den Auszug jeden Montag neu
(`tools/build_products_db.py`, nur Python-Bordmittel). Lokal:

```
python3 tools/build_products_db.py --out dist
```

## Lizenz

Die Daten stammen von Open Food Facts und stehen unter der
[Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/);
einzelne Inhalte unter der
[Database Contents License](https://opendatacommons.org/licenses/dbcl/1-0/).
