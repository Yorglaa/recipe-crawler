import logging
from crawlers.viandesuisse import get_recipe_links, get_pdf_url, download_pdf

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s — %(message)s")

links = get_recipe_links()
print(f"\n=== {len(links)} recettes trouvees. Premieres 5 : ===")
for url in links[:5]:
    print(" ", url)

first = links[0]
print(f"\n=== PDF URL pour : {first} ===")
pdf_url = get_pdf_url(first)
print(" ", pdf_url)

if pdf_url:
    print("\n=== Telechargement dans ./pdfs/test/ ===")
    path = download_pdf(pdf_url, "./pdfs/test", first)
    print(f"  Sauvegarde : {path}")
