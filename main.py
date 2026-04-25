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
        description="Recipe crawler pipeline: crawl sites and upload PDFs to AnythingLLM."
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
        help="Max recipes per site (default: unlimited)",
    )
    parser.add_argument(
        "--skip-upload",
        action="store_true",
        help="Skip AnythingLLM upload after crawling",
    )
    return parser.parse_args()


def _run_crawlers(sites: list[str], limit: int | None) -> list[str]:
    """Run each crawler and return list of output dirs that were populated."""
    populated: list[str] = []
    for site in sites:
        out_dir = _OUTPUT_DIRS[site]
        logger.info("=== Crawling %s (limit=%s) -> %s ===", site, limit, out_dir)
        try:
            mod = importlib.import_module(_SITES[site])
            mod.crawl(output_dir=out_dir, limit=limit)
            populated.append(out_dir)
        except Exception as exc:
            logger.error("Crawler %s failed: %s", site, exc, exc_info=True)
    return populated


def _upload_pdfs(output_dirs: list[str], workspace_slug: str) -> None:
    """Upload every PDF found in output_dirs to the AnythingLLM workspace."""
    from pipeline import anythingllm

    for out_dir in output_dirs:
        pdfs = sorted(Path(out_dir).glob("*.pdf"))
        if not pdfs:
            logger.info("No PDFs found in %s, skipping upload", out_dir)
            continue
        logger.info("Uploading %d PDFs from %s to workspace '%s'", len(pdfs), out_dir, workspace_slug)
        for pdf in pdfs:
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

    populated = _run_crawlers(sites, args.limit)

    if args.skip_upload:
        logger.info("--skip-upload set, skipping AnythingLLM upload")
    else:
        _upload_pdfs(populated, _WORKSPACE_SLUG)

    logger.info("Pipeline complete")


if __name__ == "__main__":
    main()
