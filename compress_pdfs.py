"""
Compresse les PDFs d'un site avec Ghostscript (reduction images haute-resolution).

Prerequis : Ghostscript installe (https://ghostscript.com/releases/gsdnld.html)
  - Windows : gswin64c doit etre dans le PATH, ou specifier --gs-path

Usage :
  # Test sur 5 fichiers, affiche le gain sans modifier
  python compress_pdfs.py --site qoqa --dry-run --limit 5

  # Test reel sur 10 fichiers (modifie en place, avec sauvegarde .bak)
  python compress_pdfs.py --site qoqa --limit 10

  # Compression complete (sans sauvegarde .bak pour economiser l'espace)
  python compress_pdfs.py --site qoqa --no-backup

  # Verifier que pdfplumber lit toujours le texte apres compression
  python compress_pdfs.py --site qoqa --verify --limit 20
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pdfplumber

PDF_DIRS = {
    "qoqa":         "./pdfs/qoqa",
    "migusto":      "./pdfs/migusto",
    "viandesuisse": "./pdfs/viandesuisse",
    "fooby":        "./pdfs/fooby",
}

# /ebook = 150 dpi, bon compromis taille/lisibilite ecran.
# /screen = 72 dpi (plus petit), /printer = 300 dpi (quasi-identique a l'original)
GS_QUALITY = "/ebook"

GS_DEFAULT = r"C:\Program Files\gs\gs10.07.0\bin\gswin64c.exe"


def find_gs(gs_path: str | None) -> str:
    if gs_path:
        return gs_path
    for candidate in ("gswin64c", "gswin32c", "gs"):
        if shutil.which(candidate):
            return candidate
    if Path(GS_DEFAULT).exists():
        return GS_DEFAULT
    raise FileNotFoundError(
        "Ghostscript introuvable. Utilisez --gs-path pour specifier le chemin."
    )


def compress_pdf(gs: str, src: Path, dst: Path, quality: str) -> bool:
    result = subprocess.run(
        [
            gs,
            "-dBATCH", "-dNOPAUSE", "-dQUIET",
            "-sDEVICE=pdfwrite",
            "-dCompatibilityLevel=1.4",
            f"-dPDFSETTINGS={quality}",
            f"-sOutputFile={dst}",
            str(src),
        ],
        capture_output=True,
        timeout=60,
    )
    return result.returncode == 0 and dst.exists() and dst.stat().st_size > 0


def verify_text(path: Path) -> tuple[bool, int]:
    try:
        with pdfplumber.open(path) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        return True, len(text)
    except Exception:
        return False, 0


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Compresse les PDFs avec Ghostscript.")
    parser.add_argument("--site", required=True, choices=list(PDF_DIRS))
    parser.add_argument("--gs-path", help="Chemin vers gswin64c.exe")
    parser.add_argument("--quality", default=GS_QUALITY,
                        choices=["/screen", "/ebook", "/printer"])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    gs = find_gs(args.gs_path)
    print(f"Ghostscript : {gs}")

    pdf_dir = Path(PDF_DIRS[args.site])
    pdfs = sorted(pdf_dir.glob("*.pdf"))
    if args.limit:
        pdfs = pdfs[:args.limit]

    print(f"{len(pdfs)} fichiers a traiter dans {pdf_dir}\n")

    total_before = total_after = 0
    errors = skipped = 0

    for i, src in enumerate(pdfs, 1):
        size_before = src.stat().st_size
        total_before += size_before

        if args.dry_run:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            ok = compress_pdf(gs, src, tmp_path, args.quality)
            if ok:
                size_after = tmp_path.stat().st_size
                ratio = (1 - size_after / size_before) * 100
                print(f"[{i:4d}/{len(pdfs)}] {src.name:<40} "
                      f"{size_before/1024:>7.0f} KB -> {size_after/1024:>7.0f} KB "
                      f"({ratio:.0f}%)")
                total_after += size_after
                tmp_path.unlink(missing_ok=True)
            else:
                print(f"[{i:4d}/{len(pdfs)}] {src.name:<40} ERREUR Ghostscript")
                total_after += size_before
                errors += 1
            continue

        # Compression reelle : fichier temporaire dans le meme dossier
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False, dir=pdf_dir) as tmp:
            tmp_path = Path(tmp.name)

        ok = compress_pdf(gs, src, tmp_path, args.quality)
        if not ok:
            tmp_path.unlink(missing_ok=True)
            print(f"[{i:4d}/{len(pdfs)}] {src.name:<40} ERREUR - fichier inchange")
            total_after += size_before
            errors += 1
            continue

        size_after = tmp_path.stat().st_size

        if args.verify:
            before_ok, chars_before = verify_text(src)
            after_ok, chars_after = verify_text(tmp_path)
            if not after_ok or (before_ok and chars_after < chars_before * 0.8):
                tmp_path.unlink(missing_ok=True)
                print(f"[{i:4d}/{len(pdfs)}] {src.name:<40} "
                      f"ECHEC texte ({chars_before} -> {chars_after} chars) - ignore")
                total_after += size_before
                skipped += 1
                continue

        if not args.no_backup:
            src.rename(src.with_suffix(".pdf.bak"))
        else:
            src.unlink()
        tmp_path.rename(src)

        ratio = (1 - size_after / size_before) * 100
        total_after += size_after
        print(f"[{i:4d}/{len(pdfs)}] {src.name:<40} "
              f"{size_before/1024:>7.0f} KB -> {size_after/1024:>7.0f} KB ({ratio:.0f}%)")

    saved = total_before - total_after
    ratio_total = (saved / total_before * 100) if total_before else 0
    print(f"\n{'-'*70}")
    print(f"Total avant  : {total_before/1024/1024:.1f} MB")
    print(f"Total apres  : {total_after/1024/1024:.1f} MB")
    print(f"Economie     : {saved/1024/1024:.1f} MB ({ratio_total:.0f}%)")
    if errors:
        print(f"Erreurs      : {errors}")
    if skipped:
        print(f"Ignores      : {skipped}")
    if not args.no_backup and not args.dry_run:
        bak_count = len(list(pdf_dir.glob("*.pdf.bak")))
        if bak_count:
            print(f"\nSauvegardes .bak : {bak_count} fichiers dans {pdf_dir}")
            print(r"Pour supprimer apres validation : del pdfs\qoqa\*.bak")


if __name__ == "__main__":
    main()
