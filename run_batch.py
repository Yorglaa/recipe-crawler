"""
Production batch runner.

Calls main.py in a loop for each site until no new recipes are found.
"""
import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_SITES = ["viandesuisse", "migusto", "qoqa", "fooby"]
_PDF_DIRS = {
    "viandesuisse": "./pdfs/viandesuisse",
    "migusto": "./pdfs/migusto",
    "qoqa": "./pdfs/qoqa",
    "fooby": "./pdfs/fooby",
}


def _count_pdfs(directory: str) -> int:
    p = Path(directory)
    return len(list(p.glob("*.pdf"))) if p.exists() else 0


def _run_batch(site: str, batch_size: int, renew: bool) -> int:
    """Run one batch via main.py; return number of new PDFs downloaded."""
    before = _count_pdfs(_PDF_DIRS[site])
    cmd = [sys.executable, "main.py", "--sites", site, "--limit", str(batch_size)]
    if renew:
        cmd.append("--renew")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        logger.warning("main.py exited with code %d for site %s", result.returncode, site)
    after = _count_pdfs(_PDF_DIRS[site])
    return after - before


def _crawl_site(site: str, batch_size: int, delay: int, renew: bool) -> int:
    """Run batches for one site until exhausted. Returns total new PDFs."""
    total = 0
    batch = 0
    first_batch = True
    while True:
        batch += 1
        logger.info("=" * 60)
        logger.info("Site: %s | batch %d | batch-size %d", site, batch, batch_size)
        logger.info("=" * 60)

        # --renew only on first batch to refresh cache once, not on every batch
        new = _run_batch(site, batch_size, renew=renew and first_batch)
        first_batch = False
        total += new
        logger.info("Batch %d done: +%d new PDFs (running total for %s: %d)", batch, new, site, total)

        if new == 0:
            logger.info("Site %s is complete -- %d PDFs downloaded in total", site, total)
            break

        logger.info("Waiting %ds before next batch...", delay)
        time.sleep(delay)

    return total


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Production batch crawler. Runs main.py in a loop per site until all new recipes are downloaded."
    )
    parser.add_argument(
        "--sites",
        nargs="+",
        choices=_SITES + ["all"],
        default=["all"],
        metavar="SITE",
        help="Sites to process: viandesuisse, migusto, qoqa, fooby, or all (default: all)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        metavar="N",
        help="Number of NEW recipes to fetch per batch (default: 50)",
    )
    parser.add_argument(
        "--delay",
        type=int,
        default=30,
        metavar="SEC",
        help="Seconds to wait between batches (default: 30)",
    )
    parser.add_argument(
        "--renew",
        action="store_true",
        help="Re-fetch link/slug list from source on first batch (ignores cache)",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
        datefmt="%H:%M:%S",
    )

    args = _parse_args()
    sites = _SITES if "all" in args.sites else args.sites

    logger.info(
        "Production crawl starting -- sites: %s | batch-size: %d | delay: %ds | renew: %s",
        sites, args.batch_size, args.delay, args.renew,
    )

    grand_total = 0
    for site in sites:
        n = _crawl_site(site, args.batch_size, args.delay, args.renew)
        grand_total += n

    logger.info("All sites complete. Grand total: %d new PDFs downloaded.", grand_total)


if __name__ == "__main__":
    main()
