import logging
from pathlib import Path

import httpx

import config

logger = logging.getLogger(__name__)

_BASE = config.ANYTHINGLLM_API_URL.rstrip("/") + "/api/v1"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {config.ANYTHINGLLM_API_KEY}"}


def get_workspace(workspace_slug: str) -> dict | None:
    """Retourne les infos du workspace ou None s'il n'existe pas."""
    url = f"{_BASE}/workspace/{workspace_slug}"
    logger.info("GET workspace: %s", url)

    with httpx.Client() as client:
        response = client.get(url, headers=_headers())

    if response.status_code == 200:
        data = response.json()
        workspace = data.get("workspace")
        if workspace:
            logger.info("Workspace found: %s", workspace_slug)
            return workspace[0] if isinstance(workspace, list) else workspace
    logger.warning("Workspace not found: %s (HTTP %d)", workspace_slug, response.status_code)
    return None


def _embed_document(doc_name: str, workspace_slug: str, client: httpx.Client) -> None:
    """Attache un document deja uploade au workspace via update-embeddings."""
    location = f"custom-documents/{doc_name}"
    url = f"{_BASE}/workspace/{workspace_slug}/update-embeddings"
    logger.info("Embedding %s into workspace %s", location, workspace_slug)
    response = client.post(url, headers=_headers(), json={"adds": [location], "deletes": []})
    response.raise_for_status()
    logger.info("Embedding done")


def upload_pdf(file_path: str, workspace_slug: str) -> dict | None:
    """Uploade un PDF, l'embedding dans le workspace, et retourne le dict document."""
    path = Path(file_path)
    upload_url = f"{_BASE}/document/upload"
    logger.info("Uploading %s to workspace %s", path.name, workspace_slug)

    with httpx.Client(timeout=120) as client:
        with path.open("rb") as f:
            response = client.post(
                upload_url,
                headers=_headers(),
                files={"file": (path.name, f, "application/pdf")},
            )
        response.raise_for_status()
        data = response.json()

        if not data.get("success"):
            logger.error("Upload failed: %s", data.get("error"))
            return None

        doc = data["documents"][0] if data.get("documents") else {}
        logger.info("Upload successful: %s", doc.get("title", path.name))

        # location est un chemin absolu — on extrait juste le nom du fichier JSON
        doc_filename = Path(doc["location"]).name
        _embed_document(doc_filename, workspace_slug, client)

    return doc


def chat(message: str, workspace_slug: str, mode: str = "chat") -> str | None:
    """Envoie un message au workspace et retourne la reponse textuelle."""
    url = f"{_BASE}/workspace/{workspace_slug}/chat"
    logger.info("Chat [%s] → %s: %s", mode, workspace_slug, message[:80])

    with httpx.Client(timeout=180) as client:
        response = client.post(
            url,
            headers=_headers(),
            json={"message": message, "mode": mode},
        )

    try:
        data = response.json()
    except Exception:
        logger.error("Chat HTTP %d — body non-JSON: %s", response.status_code, response.text[:200])
        response.raise_for_status()
        return None

    error = data.get("error")
    if error and error not in (None, "null"):
        logger.error("Chat error (HTTP %d): %s", response.status_code, error)
        return None

    text = data.get("textResponse")
    logger.info("Chat response received (%d chars)", len(text or ""))
    return text
