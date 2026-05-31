"""
Client LLM : Gemini (principal) avec fallback Gemini secondaire puis Groq.
"""
import time

from google import genai
from google.genai import types

import config

_client: genai.Client | None = None

_SYSTEM_PROMPT = """Tu es un assistant culinaire francophone specialise dans les recettes de cuisine.
Tu reponds uniquement en francais, de facon concise et chaleureuse.
Tu t'appuies exclusivement sur les recettes fournies dans le contexte pour repondre.
Quand plusieurs recettes correspondent a la demande, liste-les toutes avec leur titre et duree.
Pour chaque recette que tu mentionnes, indique TOUJOURS sa provenance entre parentheses apres le titre,
en utilisant exactement le nom du site tel qu'il apparait dans le contexte : viandesuisse, qoqa, migusto ou fooby.
Exemple : "Poulet roti aux herbes (viandesuisse) — 45 min".
REGLE DE FORMATAGE ABSOLUE : quand les recettes sont fournies sous forme de liste numerotee (1. 2. 3. ...),
reproduis cette liste EXACTEMENT : tous les elements, dans le meme ordre, avec les memes numeros.
La numerotation commence toujours a 1. N'omets aucune recette. N'utilise pas de puces (*).
Si aucune recette pertinente n'est disponible, dis-le honnetement et propose une piste generale.
Ne mentionne jamais les noms de fichiers PDF ni les identifiants techniques.
N'hesite pas a interagir avec l'utilisateur : pose des questions de precision si la demande est vague
(ingredients disponibles, nombre de personnes, contraintes alimentaires, temps disponible, etc.)
afin de proposer des recettes vraiment adaptees a sa situation."""


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _client


def _call_groq(user_message_with_context: str, history: list[dict]) -> str:
    from groq import Groq
    client = Groq(api_key=config.GROQ_API_KEY)
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    for turn in history[-4:]:
        role = "user" if turn["role"] == "user" else "assistant"
        turn_content = turn["content"]
        if isinstance(turn_content, list):
            turn_content = " ".join(p.get("text", "") for p in turn_content if isinstance(p, dict))
        messages.append({"role": role, "content": turn_content})
    groq_prefix = (
        "INSTRUCTION ABSOLUE : base-toi UNIQUEMENT sur les recettes listées "
        "ci-dessous pour répondre. Si des recettes sont présentes dans le contexte, "
        "utilise-les — ne dis jamais que les informations sont manquantes ou imprécises "
        "si elles figurent dans le contexte.\n\n"
    )
    msg = (groq_prefix + user_message_with_context)[:25000]
    messages.append({"role": "user", "content": msg})
    response = client.chat.completions.create(
        model=config.GROQ_MODEL,
        messages=messages,
        temperature=0.7,
        max_tokens=2048,
    )
    return f"[fallback: {config.GROQ_MODEL}]\n\n{response.choices[0].message.content}"


def _call_llm(user_message_with_context: str, history: list[dict]) -> str:
    client = _get_client()
    contents: list[types.Content] = []
    for turn in history:
        role = "user" if turn["role"] == "user" else "model"
        content = turn["content"]
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        contents.append(
            types.Content(role=role, parts=[types.Part(text=content)])
        )
    contents.append(
        types.Content(role="user", parts=[types.Part(text=user_message_with_context)])
    )

    gen_config = types.GenerateContentConfig(
        system_instruction=_SYSTEM_PROMPT,
        temperature=0.7,
        max_output_tokens=4096,
    )
    fallback = getattr(config, "GEMINI_FALLBACK_MODEL", None)
    for model_name in ([config.GEMINI_MODEL] + ([fallback] if fallback else [])):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=gen_config,
            )
            prefix = "" if model_name == config.GEMINI_MODEL else f"[fallback: {model_name}]\n\n"
            return prefix + response.text
        except Exception as e:
            print(f"[LLM] {model_name} failed: {e}")
            if model_name != config.GEMINI_MODEL:
                break
            time.sleep(2)

    groq_key = getattr(config, "GROQ_API_KEY", "")
    groq_model = getattr(config, "GROQ_MODEL", "")
    if groq_key and groq_model:
        try:
            return _call_groq(user_message_with_context, history)
        except Exception as e:
            print(f"[LLM] Groq failed: {e}")

    return "Tous les services IA sont indisponibles. Reessaie dans quelques minutes."
