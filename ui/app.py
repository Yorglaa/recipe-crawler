"""
Interface Gradio du chatbot de recettes.
Lancement : python ui/app.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import gradio as gr

from pipeline.chat import chat
from pipeline.database import init_db, count_recipes
from pipeline.embeddings import init_chroma, count as chroma_count


def _recipe_count_label() -> str:
    try:
        return f"{count_recipes()} recettes indexees · {chroma_count()} embeddings"
    except Exception:
        return "Base de donnees vide — lance d'abord `python index_recipes.py`"


def build_app() -> gr.Blocks:
    with gr.Blocks(title="Assistant Recettes") as demo:
        gr.Markdown("# Assistant Recettes")
        gr.Markdown(
            "Pose une question ouverte (*quelque chose de cremeux avec du poulet*) "
            "ou une recherche precise (*toutes les soupes en moins de 20 min*)."
        )
        gr.Markdown(_recipe_count_label())

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

    return demo


if __name__ == "__main__":
    init_db()
    init_chroma()
    app = build_app()
    app.launch(inbrowser=True, theme=gr.themes.Soft())
