import argparse
import importlib
import logging
from pathlib import Path

import config
from index_recipes import PDF_DIRS, index_site

logger = logging.getLogger(__name__)

_SITES = {
    "viandesuisse": "crawlers.viandesuisse",
    "migusto":      "crawlers.migusto",
    "qoqa":         "crawlers.qoqa",
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recipe crawler: download PDFs. Use index_recipes.py to index them."
    )
    parser.add_argument(
        "--sites", nargs="+",
        choices=list(_SITES) + ["all"],
        default=["all"], metavar="SITE",
        help="Sites to crawl: viandesuisse, migusto, qoqa, or all (default: all)",
    )
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Max NEW recipes per site (default: unlimited)",
    )
    parser.add_argument(
        "--renew", action="store_true",
        help="Ignore local link/slug cache and re-fetch from source",
    )
    parser.add_argument(
        "--index", action="store_true",
        help="Run indexer after crawling (SQLite + ChromaDB)",
    )
    return parser.parse_args()


def run_crawlers(
    sites: list[str], limit: int | None, renew: bool = False
) -> list[tuple[str, list[Path]]]:
    results: list[tuple[str, list[Path]]] = []
    for site in sites:
        out_dir = PDF_DIRS[site]
        before = set(Path(out_dir).glob("*.pdf")) if Path(out_dir).exists() else set()
        logger.info("=== Crawling %s (limit=%s, renew=%s) -> %s ===", site, limit, renew, out_dir)
        try:
            mod = importlib.import_module(_SITES[site])
            mod.crawl(output_dir=out_dir, limit=limit, renew=renew)
        except Exception as exc:
            logger.error("Crawler %s failed: %s", site, exc, exc_info=True)
        after = set(Path(out_dir).glob("*.pdf")) if Path(out_dir).exists() else set()
        new_pdfs = sorted(after - before)
        logger.info("%s: %d new PDFs downloaded", site, len(new_pdfs))
        results.append((out_dir, new_pdfs))
    return results


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )

    class _QuietFilter(logging.Filter):
        _noisy = ("fontTools", "weasyprint")
        def filter(self, record):
            return record.levelno >= logging.WARNING or not record.name.startswith(self._noisy)

    _f = _QuietFilter()
    for _h in logging.root.handlers:
        _h.addFilter(_f)
    logging.getLogger("fontTools").setLevel(logging.WARNING)
    logging.getLogger("weasyprint").setLevel(logging.WARNING)

    args = _parse_args()
    sites = list(_SITES) if "all" in args.sites else args.sites
    logger.info("Sites: %s | limit=%s | renew=%s | index=%s", sites, args.limit, args.renew, args.index)

    run_crawlers(sites, args.limit, renew=args.renew)

    if args.index:
        logger.info("--index flag set, running indexer...")
        from pipeline.database import init_db
        from pipeline.embeddings import init_chroma
        init_db()
        init_chroma()
        for site in sites:
            n = index_site(site)
            logger.info("%s: %d recipes indexed", site, n)

    logger.info("Pipeline complete")


if __name__ == "__main__":
    main()
