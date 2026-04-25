import logging
from crawlers.migusto import get_recipe_slugs, get_recipe_data, generate_pdf

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s - %(message)s")

print("=== 1. Recuperation de 3 slugs ===")
slugs = get_recipe_slugs(limit=3)
print(f"Slugs: {slugs}")
assert len(slugs) == 3, f"Expected 3 slugs, got {len(slugs)}"

for slug in slugs:
    print(f"\n=== 2. Recette : {slug} ===")
    data = get_recipe_data(slug)
    assert data is not None, f"No data for {slug}"
    assert data.get("@type") == "Recipe", "Not a Recipe JSON-LD"

    print(f"  Titre      : {data.get('name')}")
    print(f"  Duree      : {data.get('totalTime')}")
    print(f"  Portions   : {data.get('recipeYield')}")

    ingr = data.get("recipeIngredient", [])
    steps = data.get("recipeInstructions", [])
    nutr = data.get("nutrition") or {}
    print(f"  Ingredients: {len(ingr)}")
    for i in ingr[:4]:
        print(f"    - {i}")
    print(f"  Etapes     : {len(steps)}")
    for j, s in enumerate(steps[:2], 1):
        text = s.get('text', s) if isinstance(s, dict) else str(s)
        print(f"    {j}. {text[:90]}...")
    print(f"  Nutrition  : {nutr}")

    assert len(ingr) > 0, f"No ingredients for {slug}"
    assert len(steps) > 0, f"No steps for {slug}"

    data["slug"] = slug

    print(f"\n=== 3. Generation PDF pour {slug} ===")
    path = generate_pdf(data, "./pdfs/test_migusto")
    assert path is not None, f"PDF generation failed for {slug}"
    print(f"  PDF sauvegarde : {path}")

print("\n=== Tous les tests passes ===")
