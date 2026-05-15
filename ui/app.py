"""
Interface Gradio du chatbot de recettes.
Lancement : python ui/app.py
"""

import os
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import gradio as gr

from pipeline.chat import chat, reset_context
from pipeline.database import init_db, count_recipes, get_all_recipes, delete_recipes
from pipeline.embeddings import init_chroma, count as chroma_count

_ROOT = Path(__file__).parent.parent
_current_proc: subprocess.Popen | None = None
_proc_lock = threading.Lock()
_PDF_DIRS = {
    "viandesuisse": _ROOT / "pdfs" / "viandesuisse",
    "migusto":      _ROOT / "pdfs" / "migusto",
    "qoqa":         _ROOT / "pdfs" / "qoqa",
    "fooby":        _ROOT / "pdfs" / "fooby",
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
    global _current_proc
    """Lance un subprocess et yielde les logs accumules ligne par ligne."""
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
    with _proc_lock:
        _current_proc = proc
    output = f"$ {' '.join(cmd)}" + chr(10) * 2
    yield output
    try:
        _SKIP = ("GLib-GIO-WARNING", "GLib-GObject-WARNING", "GLib-WARNING", "(process:")
        for line in proc.stdout:
            if not any(line.startswith(s) or s in line for s in _SKIP):
                output += line
                yield output
    except Exception:
        pass
    proc.wait()
    with _proc_lock:
        if _current_proc is proc:
            _current_proc = None
    status = "arrete" if (proc.returncode is not None and proc.returncode < 0) else f"code {proc.returncode}"
    yield output + chr(10) + f"[termine â {status}]" + chr(10)


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


def stop_process(current_log: str = "") -> str:
    global _current_proc
    with _proc_lock:
        proc = _current_proc
    if proc and proc.poll() is None:
        proc.terminate()
        return current_log + chr(10) + "⏹ Arret demande."
    return current_log + chr(10) + "(Aucun processus en cours.)"


def _shutdown() -> str:
    threading.Timer(0.4, lambda: os._exit(0)).start()
    return "Fermeture..."


def _nouvelle_conversation() -> tuple:
    reset_context()
    return [], []


def run_sync_embeddings():
    cmd = [sys.executable, "index_recipes.py", "--sync-embeddings"]
    yield from _stream(cmd)


def run_cleanup_orphans() -> str:
    from pipeline.embeddings import _get_collection
    recipes = get_all_recipes()
    orphans = [r for r in recipes if not Path(r["pdf_path"]).exists()]
    if not orphans:
        return "Aucun orphelin trouvé."
    ids = [r["id"] for r in orphans]
    n_db = delete_recipes(ids)
    col = _get_collection()
    col.delete(ids=[str(i) for i in ids])
    lines = [f"{n_db} recette(s) supprimée(s) :"]
    for r in orphans:
        lines.append(f"  - [{r['site']}] {r['title']}")
    return "\n".join(lines)


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


_JS = """
() => {
    function styleSubmitBtn(btn) {
        if (!btn || btn._styled) return;
        btn._styled = true;
        const row = btn.closest("div");
        if (row) {
            row.style.alignItems = "stretch";
            row.style.display = "flex";
        }
        Object.assign(btn.style, {
            alignSelf: "stretch",
            minWidth: "100px",
            paddingLeft: "1.2rem",
            paddingRight: "1.2rem",
            height: "auto",
        });
    }

    function findSubmitBtn() {
        for (const btn of document.querySelectorAll("button")) {
            if (!btn.disabled && (btn.innerText || "").trim() === "Envoyer") return btn;
        }
        return null;
    }

    function applyStyles() {
        styleSubmitBtn(findSubmitBtn());
    }

    function setupAutoFocus() {
        function tryAttach() {
            const chatbot = document.querySelector(".chatbot");
            if (!chatbot) { setTimeout(tryAttach, 500); return; }
            let debounce = null;
            new MutationObserver(() => {
                clearTimeout(debounce);
                debounce = setTimeout(() => {
                    const ta = document.querySelector("textarea");
                    if (ta && document.activeElement !== ta) ta.focus();
                }, 300);
            }).observe(chatbot, { childList: true, subtree: true, characterData: true });
        }
        tryAttach();
    }

    setTimeout(() => {
        applyStyles();
        setupAutoFocus();
        new MutationObserver(() => { applyStyles(); }).observe(document.body, {childList: true, subtree: true});
    }, 2000);
}
"""


def build_app() -> gr.Blocks:
    with gr.Blocks(title="Assistant Recettes", js=_JS) as demo:
        with gr.Tabs():

            # ── Onglet Chat ───────────────────────────────────────────────────
            with gr.Tab("Chat"):
                gr.Markdown("# Assistant Recettes")
                gr.Markdown(
                    "Pose une question ouverte (*quelque chose de cremeux avec du poulet*) "
                    "ou une recherche precise (*toutes les soupes en moins de 20 min*)."
                )
                _chatbot = gr.Chatbot(autoscroll=True)
                _ci = gr.ChatInterface(
                    fn=chat,
                    chatbot=_chatbot,
                    textbox=gr.Textbox(
                        placeholder="Ex : une recette rapide avec des restes de poulet...",
                        container=False,
                        submit_btn="Envoyer",
                        lines=1,
                        max_lines=5,
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
                with gr.Row():
                    _shutdown_msg_chat = gr.Textbox(visible=False)
                    gr.Button("Nouvelle conversation", variant="secondary").click(
                        fn=_nouvelle_conversation,
                        inputs=[],
                        outputs=[_chatbot, _ci.chatbot_state],
                    )
                    gr.Button("Fermer l'application", variant="stop").click(
                        fn=_shutdown, inputs=[], outputs=[_shutdown_msg_chat]
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
                        choices=["viandesuisse", "migusto", "qoqa", "fooby"],
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
                with gr.Row():
                    crawl_log = gr.Textbox(label="Logs crawl", lines=20, max_lines=30, interactive=False)
                    stop_crawl_btn = gr.Button("⏹ Arreter", variant="stop", size="sm", scale=0, min_width=120)

                # Indexation ──────────────────────────────────────────────────
                gr.Markdown("---\n## Indexation")
                with gr.Row():
                    index_sites = gr.CheckboxGroup(
                        choices=["viandesuisse", "migusto", "qoqa", "fooby"],
                        value=["viandesuisse", "migusto", "qoqa", "fooby"],
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
                with gr.Row():
                    index_log = gr.Textbox(label="Logs indexation", lines=25, max_lines=40, interactive=False)
                    stop_index_btn = gr.Button("⏹ Arreter", variant="stop", size="sm", scale=0, min_width=120)

                # Nettoyage orphelins ─────────────────────────────────────
                gr.Markdown("---\n## Nettoyage")
                gr.Markdown("Supprime de la DB et ChromaDB les recettes dont le PDF n'existe plus.")
                cleanup_btn = gr.Button("Supprimer les orphelins", variant="stop")
                cleanup_out = gr.Textbox(label="Résultat", interactive=False, lines=5)

                # Sync embeddings ────────────────────────────────────────
                gr.Markdown("---\n## Synchronisation des embeddings")
                gr.Markdown(
                    "Ajoute dans ChromaDB les recettes présentes en SQLite mais sans embedding. "
                    "Utile si SQLite et ChromaDB sont désynchronisés."
                )
                sync_btn = gr.Button("Synchroniser", variant="secondary")
                sync_log = gr.Textbox(label="Logs sync", lines=10, max_lines=20, interactive=False)

                # Fermeture ──────────────────────────────────────────────────
                gr.Markdown("---")
                _shutdown_msg_admin = gr.Textbox(visible=False)
                gr.Button("Fermer l'application", variant="stop").click(
                    fn=_shutdown, inputs=[], outputs=[_shutdown_msg_admin]
                )

                # Events ──────────────────────────────────────────────────────
                refresh_btn.click(fn=_status, outputs=status_box)

                cleanup_btn.click(
                    fn=run_cleanup_orphans,
                    inputs=[],
                    outputs=cleanup_out,
                ).then(fn=_status, outputs=status_box)

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

                stop_crawl_btn.click(fn=stop_process, inputs=[crawl_log], outputs=[crawl_log])
                stop_index_btn.click(fn=stop_process, inputs=[index_log], outputs=[index_log])

                sync_btn.click(
                    fn=run_sync_embeddings,
                    inputs=[],
                    outputs=sync_log,
                ).then(fn=_status, outputs=status_box)


    return demo


if __name__ == "__main__":
    init_db()
    init_chroma()
    app = build_app()
    app.launch(inbrowser=True, theme=_THEME, css=_CSS)
