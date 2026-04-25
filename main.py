import argparse
import importlib
import logging
from pathlib import Path

import config

logger = logging.getLogger(__name__)

_SITES = {
    "viandesuisse": "crawlers.viandesuisse",
    "migusto": "crawlers.migusto",
    "qoqa": "crawlers.qoqa",
}

_OUTPUT_DIRS = {
    "viandesuisse": "./pdfs/viandesuisse",
    "migusto": "./pdfs/migusto",
    "qoqa": "./pdfs/qoqa",
}

_WORKSPACE_SLUG = config.ANYTHINGLLM_WORKSPACE.lower()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recipe crawler pipeline: crawl sites and upload new PDFs to AnythingLLM."
    )
    parser.add_argument(
        "--sites",
        nargs="+",
        choices=list(_SITES) + ["all"],
        default=["all"],
        metavar="SITE",
        help="Sites to crawl: viandesuisse, migusto, qoqa, or all (default: all)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Max NEW recipes per site (default: unlimited)",
    )
    parser.add_argument(
        "--skip-upload",
        action="store_true",
        help="Skip AnythingLLM upload after crawling",
    )
    return parser.parse_args()


def run_crawlers(sites: list[str], limit: int | None) -> list[tuple[str, list[Path]]]:
    """Run each crawler; return list of (output_dir, new_pdf_paths) per site."""
    results: list[tuple[str, list[Path]]] = []
    for site in sites:
        out_dir = _OUTPUT_DIRS[site]
        before = set(Path(out_dir).glob("*.pdf")) if Path(out_dir).exists() else set()
        logger.info("=== Crawling %s (limit=%s) -> %s ===", site, limit, out_dir)
        try:
            mod = importlib.import_module(_SITES[site])
            mod.crawl(output_dir=out_dir, limit=limit)
        except Exception as exc:
            logger.error("Crawler %s failed: %s", site, exc, exc_info=True)
        after = set(Path(out_dir).glob("*.pdf")) if Path(out_dir).exists() else set()
        new_pdfs = sorted(after - before)
        logger.info("%s: %d new PDFs downloaded", site, len(new_pdfs))
        results.append((out_dir, new_pdfs))
    return results


def upload_new_pdfs(results: list[tuple[str, list[Path]]], workspace_slug: str) -> None:
    """Upload only the newly downloaded PDFs to AnythingLLM."""
    from pipeline import anythingllm

    for out_dir, new_pdfs in results:
        if not new_pdfs:
            logger.info("No new PDFs in %s, skipping upload", out_dir)
            continue
        logger.info("Uploading %d new PDFs from %s to workspace '%s'", len(new_pdfs), out_dir, workspace_slug)
        for pdf in new_pdfs:
            try:
                anythingllm.upload_pdf(str(pdf), workspace_slug)
            except Exception as exc:
                logger.error("Upload failed for %s: %s", pdf.name, exc)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
        datefmt="%H:%M:%S",
    )

    args = _parse_args()
    sites = list(_SITES) if "all" in args.sites else args.sites
    logger.info("Sites: %s | limit=%s | skip-upload=%s", sites, args.limit, args.skip_upload)

    results = run_crawlers(sites, args.limit)

    if args.skip_upload:
        logger.info("--skip-upload set, skipping AnythingLLM upload")
    else:
        upload_new_pdfs(results, _WORKSPACE_SLUG)

    logger.info("Pipeline complete")


if __name__ == "__main__":
    main()
