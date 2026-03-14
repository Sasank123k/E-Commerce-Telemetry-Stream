"""
generate_test_data.py -- Static Data Builder for Deterministic Testing.

Generates exactly 10,000 mock events as raw dictionaries and computes
the exact baseline math (total_revenue, revenue_by_category,
revenue_by_region).  Saves to test_data.json.

Usage:
    python generate_test_data.py
"""

import json
import random

# ---------------------------------------------------------------------------
# Same constants as main.py — kept in sync
# ---------------------------------------------------------------------------
CATEGORIES = ["Electronics", "Clothing", "Home & Kitchen", "Books", "Sports", "Toys", "Beauty", "Automotive"]
REGIONS = ["North America", "Europe", "Asia Pacific", "South America", "Middle East", "Africa"]
PRICE_RANGES = {
    "Electronics":      (19.99, 1499.99),
    "Clothing":         (9.99, 299.99),
    "Home & Kitchen":   (4.99, 599.99),
    "Books":            (2.99, 49.99),
    "Sports":           (14.99, 499.99),
    "Toys":             (4.99, 149.99),
    "Beauty":           (3.99, 199.99),
    "Automotive":       (9.99, 799.99),
}

EVENT_COUNT = 10_000
OUTPUT_FILE = "test_data.json"


def main():
    random.seed(42)  # deterministic seed for reproducibility

    events: list[dict] = []
    total_revenue = 0.0
    revenue_by_category: dict[str, float] = {cat: 0.0 for cat in CATEGORIES}
    revenue_by_region: dict[str, float] = {reg: 0.0 for reg in REGIONS}

    for _ in range(EVENT_COUNT):
        cat = random.choice(CATEGORIES)
        lo, hi = PRICE_RANGES[cat]
        price = round(random.uniform(lo, hi), 2)
        quantity = random.randint(1, 5)
        region = random.choice(REGIONS)

        evt = {
            "price": price,
            "quantity": quantity,
            "category": cat,
            "region": region,
        }
        events.append(evt)

        # Exact baseline math with round() to prevent float drift
        rev = round(price * quantity, 2)
        total_revenue = round(total_revenue + rev, 2)
        revenue_by_category[cat] = round(revenue_by_category[cat] + rev, 2)
        revenue_by_region[region] = round(revenue_by_region[region] + rev, 2)

    baseline = {
        "total_events": EVENT_COUNT,
        "total_revenue": total_revenue,
        "revenue_by_category": revenue_by_category,
        "revenue_by_region": revenue_by_region,
    }

    data = {
        "baseline": baseline,
        "events": events,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    print(f"Generated {EVENT_COUNT} events -> {OUTPUT_FILE}")
    print(f"  Total Revenue:  ${total_revenue:,.2f}")
    print(f"  Categories:     {len(revenue_by_category)}")
    print(f"  Regions:        {len(revenue_by_region)}")
    print()
    print("Baseline by Category:")
    for cat, rev in sorted(revenue_by_category.items(), key=lambda x: -x[1]):
        print(f"  {cat:<20s}  ${rev:>12,.2f}")
    print()
    print("Baseline by Region:")
    for reg, rev in sorted(revenue_by_region.items(), key=lambda x: -x[1]):
        print(f"  {reg:<20s}  ${rev:>12,.2f}")


if __name__ == "__main__":
    main()
