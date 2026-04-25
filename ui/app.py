"""
Interface Gradio du chatbot de recettes.
Lancement : python ui/app.py
"""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import gradio as gr

from pipeline.chat import chat
from pipeline.database import init_db, count_recipes
from pipeline.embeddings import init_chroma, count as chroma_count

_ROOT = Path(__file__).parent.parent
_PDF_DIRS = {
    "viandesuisse": _ROOT / "pdfs" / "viandesuisse",
    "migusto":      _ROOT / "pdfs" / "migusto",
    "qoqa":         _ROOT / "pdfs" / "qoqa",
}


def _count_pdfs(site: str) -> int:
    d = _PDF_DIRS[site]
    return len(list(d.glob("*.pdf"))) if d.exists() else 0


def _status() -> str:
    parts = [f"{s}: {_count_pdfs(s)} PDFs" for s in _PDF_DIRS]
    try:
        db = count_recipes()
        chroma = chroma_count()
        parts.append(f"DB: {db} recettes · {chroma} embeddings")
    except Exception:
        parts.append("DB: non initialisee")
    return "  |  ".join(parts)


def _stream(cmd: list[str]):
    """Lance un subprocess et yielde les logs accumulés ligne par ligne."""
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=str(_ROOT),
        env=env,
    )
    output = f"$ {' '.join(cmd)}\n\n"
    yield output
    for line in proc.stdout:
        output += line
        yield output
    proc.wait()
    yield output + f"\n[terminé — code {proc.returncode}]\n"


def run_crawl(sites: list[str], limit: int, renew: bool, do_index: bool):
    if not sites:
        yield "Aucun site sélectionné."
        return
    cmd = [sys.executable, "main.py", "--sites"] + sites + ["--limit", str(int(limit))]
    if renew:
        cmd.append("--renew")
    if do_index:
        cmd.append("--index")
    yield from _stream(cmd)


def run_index(sites: list[str], limit: int, loop: bool):
    if not sites:
        yield "Aucun site sélectionné."
        return

    batch_limit = int(limit)
    cmd = [sys.executable, "index_recipes.py", "--sites"] + sites
    if batch_limit > 0:
        cmd += ["--limit", str(batch_limit)]

    if not loop or batch_limit == 0:
        yield from _stream(cmd)
        return

    # Mode boucle : lots successifs jusqu'à 0 nouvelles recettes
    all_output = ""
    batch = 0
    while True:
        batch += 1
        sep = f"{'='*44}\n Lot {batch}\n{'='*44}\n"
        all_output += sep
        yield all_output

        before = count_recipes()
        batch_output = ""
        for chunk in _stream(cmd):
            batch_output = chunk
            yield all_output + batch_output
        all_output += batch_output

        new = count_recipes() - before
        summary = f"→ Lot {batch} : {new} nouvelles recettes  (total DB : {count_recipes()})\n"
        all_output += summary
        yield all_output

        if new == 0:
            all_output += "\n✓ Indexation complète — aucune nouvelle recette trouvée."
            yield all_output
            break


_CSS = """
body, .gradio-container { background: #f2ede4 !important; }
.block, .panel { background: #faf7f2 !important; }

/* Cases à cocher plus visibles */
input[type="checkbox"] {
    width: 18px !important;
    height: 18px !important;
    accent-color: #7c5c3e !important;
    border: 2px solid #7c5c3e !important;
    cursor: pointer;
}
.checkbox-group label span,
label.svelte-1l3ixkv span {
    font-size: 0.95rem !important;
}
"""

_THEME = gr.themes.Soft(
    primary_hue=gr.themes.colors.stone,
    neutral_hue=gr.themes.colors.stone,
)


def build_app() -> gr.Blocks:
    with gr.Blocks(title="Assistant Recettes") as demo:
        with gr.Tabs():

            # ── Onglet Chat ───────────────────────────────────────────────────
            with gr.Tab("Chat"):
                gr.Markdown("# Assistant Recettes")
                gr.Markdown(
                    "Pose une question ouverte (*quelque chose de cremeux avec du poulet*) "
                    "ou une recherche precise (*toutes les soupes en moins de 20 min*)."
                )
                gr.ChatInterface(
                    fn=chat,
                    chatbot=gr.Chatbot(autoscroll=True),
                    textbox=gr.Textbox(
                        placeholder="Ex : une recette rapide avec des restes de poulet...",
                        container=False,
                        submit_btn="Envoyer",
                    ),
                    examples=[
                        "Quelque chose de cremeux avec du poulet",
                        "Toutes les recettes de moins de 30 minutes",
                        "Une bonne soupe reconfortante",
                        "Une entree legere sans gluten",
                        "Idee de dessert rapide pour ce soir",
                    ],
                )

                gr.Markdown("---")
                gr.Button("Fermer l'application", variant="stop").click(
                    fn=lambda: os._exit(0), inputs=[], outputs=[]
                )

            # ── Onglet Admin ──────────────────────────────────────────────────
            with gr.Tab("Admin"):
                with gr.Row():
                    status_box = gr.Textbox(label="Etat", interactive=False, value=_status, scale=5)
                    refresh_btn = gr.Button("↻", size="sm", scale=1)

                # Crawling ────────────────────────────────────────────────────
                gr.Markdown("---\n## Crawling")
                with gr.Row():
                    crawl_sites = gr.CheckboxGroup(
                        choices=["viandesuisse", "migusto", "qoqa"],
                        value=["migusto"],
                        label="Sites",
                    )
                    with gr.Column():
                        crawl_limit = gr.Number(
                            value=50, label="Taille du lot (nouveaux PDFs)",
                            precision=0, minimum=1,
                        )
                        crawl_renew = gr.Checkbox(label="--renew  (re-fetch la liste de slugs)", value=False)
                        crawl_index = gr.Checkbox(label="--index  (indexer après le crawl)", value=False)
                crawl_btn = gr.Button("Lancer un lot", variant="primary")
                crawl_log = gr.Textbox(label="Logs crawl", lines=20, max_lines=30, interactive=False)

                # Indexation ──────────────────────────────────────────────────
                gr.Markdown("---\n## Indexation")
                with gr.Row():
                    index_sites = gr.CheckboxGroup(
                        choices=["viandesuisse", "migusto", "qoqa"],
                        value=["viandesuisse", "migusto", "qoqa"],
                        label="Sites",
                    )
                    with gr.Column():
                        index_limit = gr.Number(
                            value=200, label="Taille du lot (0 = tout en une passe)",
                            precision=0, minimum=0,
                        )
                        index_loop = gr.Checkbox(
                            label="Boucler jusqu'à complet  (lots successifs)", value=False
                        )
                index_btn = gr.Button("Indexer", variant="secondary")
                index_log = gr.Textbox(label="Logs indexation", lines=25, max_lines=40, interactive=False)

                # Fermeture ──────────────────────────────────────────────────
                gr.Markdown("---")
                gr.Button("Fermer l'application", variant="stop").click(
                    fn=lambda: os._exit(0), inputs=[], outputs=[]
                )

                # Events ──────────────────────────────────────────────────────
                refresh_btn.click(fn=_status, outputs=status_box)

                crawl_btn.click(
                    fn=run_crawl,
                    inputs=[crawl_sites, crawl_limit, crawl_renew, crawl_index],
                    outputs=crawl_log,
                ).then(fn=_status, outputs=status_box)

                index_btn.click(
                    fn=run_index,
                    inputs=[index_sites, index_limit, index_loop],
                    outputs=index_log,
                ).then(fn=_status, outputs=status_box)

    return demo


if __name__ == "__main__":
    init_db()
    init_chroma()
    app = build_app()
    app.launch(inbrowser=True, theme=_THEME, css=_CSS)
