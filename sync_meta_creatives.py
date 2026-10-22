"""
Sync diário de criativos do Meta Ads para uma pasta do Google Drive.

Regras:
- Baixa o criativo de TODOS os anúncios da conta, em qualquer status
  (ativos, pausados, arquivados, reprovados, excluídos...).
- Vídeos e imagens. Cada anúncio vira um arquivo no Drive com o nome exato do anúncio.
- Anúncio com mais de uma mídia (carrossel, criativo dinâmico, variações por
  posicionamento) vira uma SUBPASTA com o nome exato do anúncio, com as mídias
  numeradas dentro (01.mp4, 02.jpg...).
- Nomes nunca se repetem: se o nome já existe na pasta do Drive, o anúncio é ignorado.
  Se dois anúncios na conta têm o mesmo nome, fica o mais antigo (created_time).
- Modo append: a cada execução só sobe o que ainda não está no Drive.
  Anúncios que falharam hoje são tentados de novo na próxima execução.

Configuração 100% via variáveis de ambiente (GitHub Secrets). Veja o README.
"""

from __future__ import annotations

import csv
import json
import mimetypes
import os
import sys
import tempfile
import time
from datetime import datetime

import requests
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ====================== CONFIGURAÇÃO ======================
META_TOKEN = os.environ["META_ACCESS_TOKEN"].strip()
AD_ACCOUNT_ID = os.environ["META_AD_ACCOUNT_ID"].strip()
if not AD_ACCOUNT_ID.startswith("act_"):
    AD_ACCOUNT_ID = f"act_{AD_ACCOUNT_ID}"
DRIVE_FOLDER_ID = os.environ["GDRIVE_FOLDER_ID"].strip()

API_VERSION = os.getenv("META_API_VERSION", "v23.0")
BASE_URL = f"https://graph.facebook.com/{API_VERSION}"

# false = nome do arquivo no Drive é exatamente o nome do anúncio (sem .mp4/.jpg)
# true  = adiciona a extensão (facilita abrir o arquivo depois de baixar do Drive)
APPEND_EXTENSION = os.getenv("APPEND_EXTENSION", "false").lower() == "true"

# Para antes do limite de 6h do GitHub Actions. O que sobrar entra na próxima execução.
MAX_MINUTES = float(os.getenv("MAX_MINUTES", "330"))

# Retorna erro (exit 1) se algum anúncio falhar. O GitHub manda e-mail em falha.
FAIL_ON_ERRORS = os.getenv("FAIL_ON_ERRORS", "false").lower() == "true"

DEFAULT_STATUSES = (
    "ACTIVE,PAUSED,CAMPAIGN_PAUSED,ADSET_PAUSED,PENDING_REVIEW,DISAPPROVED,"
    "PREAPPROVED,PENDING_BILLING_INFO,IN_PROCESS,WITH_ISSUES,ARCHIVED,DELETED"
)
STATUSES = [s.strip() for s in os.getenv("AD_STATUSES", DEFAULT_STATUSES).split(",") if s.strip()]

FAILURES_CSV = os.getenv("FAILURES_CSV", "failures.csv")
# ==========================================================

START = time.monotonic()
http = requests.Session()

# Códigos de erro do Graph API que valem nova tentativa (rate limit / temporários)
TRANSIENT_CODES = {2, 4, 17, 32, 341, 613, 80000, 80003, 80004, 80014}

CREATIVE_FIELDS = (
    "id,object_type,video_id,image_hash,image_url,thumbnail_url,"
    "effective_object_story_id,effective_instagram_media_id,"
    "object_story_spec,asset_feed_spec"
)

EXT_BY_MIME = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
KNOWN_EXTS = set(EXT_BY_MIME.values())
FOLDER_MIME = "application/vnd.google-apps.folder"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Meta Graph API
# ---------------------------------------------------------------------------
def meta_get(path: str, params: dict | None = None, retries: int = 6) -> dict:
    url = path if path.startswith("http") else f"{BASE_URL}/{path.lstrip('/')}"
    params = dict(params or {})
    if "access_token=" not in url:
        params["access_token"] = META_TOKEN

    last: dict = {}
    for attempt in range(retries):
        try:
            resp = http.get(url, params=params, timeout=90)
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            last = {"error": {"message": f"falha de rede: {exc}"}}
            time.sleep(min(10 * 2**attempt, 300))
            continue

        err = data.get("error") if isinstance(data, dict) else None
        if not err:
            return data

        last = data
        if err.get("code") in TRANSIENT_CODES or err.get("is_transient"):
            wait = min(60 * (attempt + 1), 600)
            log(f"   API Meta: limite/erro temporário (code {err.get('code')}). Aguardando {wait}s")
            time.sleep(wait)
            continue
        return data
    return last


def fetch_all_ads() -> list[dict]:
    params = {
        "fields": f"id,name,created_time,effective_status,creative{{{CREATIVE_FIELDS}}}",
        "limit": 100,
        "filtering": json.dumps(
            [{"field": "effective_status", "operator": "IN", "value": STATUSES}]
        ),
    }
    ads: list[dict] = []
    while True:
        data = meta_get(f"{AD_ACCOUNT_ID}/ads", params)
        if "error" in data:
            # Payload grande demais costuma voltar como erro genérico: reduz a página
            if params["limit"] > 10:
                params["limit"] //= 2
                log(f"   Erro na listagem, reduzindo página para {params['limit']}")
                continue
            msg = data["error"].get("message")
            raise RuntimeError(f"Erro ao listar anúncios no Meta: {msg}")

        ads.extend(data.get("data", []))
        log(f"   {len(ads)} anúncios listados")

        paging = data.get("paging") or {}
        after = (paging.get("cursors") or {}).get("after")
        if not after or not paging.get("next"):
            break
        params["after"] = after
    return ads


def collect_assets(cr: dict) -> list[tuple[str, str]]:
    """Lista ordenada de mídias do criativo: ("video", id) | ("hash", hash) | ("url", url)."""
    assets: list[tuple[str, str]] = []

    def add(kind: str, value: str | None) -> None:
        if value and (kind, value) not in assets:
            assets.append((kind, value))

    spec = cr.get("object_story_spec") or {}
    video_data = spec.get("video_data") or {}
    link_data = spec.get("link_data") or {}
    photo_data = spec.get("photo_data") or {}

    # Vídeo simples (a image_hash dentro de video_data é só a thumbnail, ignorada)
    add("video", video_data.get("video_id"))

    # Carrossel
    children = link_data.get("child_attachments") or []
    for ch in children:
        if ch.get("video_id"):
            add("video", ch["video_id"])
        elif ch.get("image_hash"):
            add("hash", ch["image_hash"])
        elif ch.get("picture"):
            add("url", ch["picture"])

    # Imagem simples
    if not children:
        if link_data.get("image_hash"):
            add("hash", link_data["image_hash"])
        elif link_data.get("picture"):
            add("url", link_data["picture"])
    if photo_data.get("image_hash"):
        add("hash", photo_data["image_hash"])
    elif photo_data.get("url"):
        add("url", photo_data["url"])

    # Criativo dinâmico / Advantage+ / customização por posicionamento
    feed = cr.get("asset_feed_spec") or {}
    for v in feed.get("videos") or []:
        add("video", v.get("video_id"))
    for im in feed.get("images") or []:
        add("hash", im.get("hash"))

    # Campos de topo do creative, só se nada acima foi encontrado
    if not assets:
        if cr.get("video_id"):
            add("video", cr["video_id"])
        elif cr.get("image_hash"):
            add("hash", cr["image_hash"])
    return assets


def video_source(video_id: str) -> str | None:
    return meta_get(video_id, {"fields": "source"}).get("source")


def image_url_from_hash(image_hash: str) -> str | None:
    data = meta_get(
        f"{AD_ACCOUNT_ID}/adimages",
        {"hashes": json.dumps([image_hash]), "fields": "hash,url"},
    )
    for row in data.get("data", []):
        if row.get("url"):
            return row["url"]
    return None


def story_media_urls(story_id: str) -> list[str]:
    """Fallback: mídia do post publicado (inclui posts de parceria / impulsionados)."""
    data = meta_get(
        story_id,
        {"fields": "attachments{media,media_type,target,subattachments{media,media_type,target}}"},
    )
    urls: list[str] = []
    for att in (data.get("attachments") or {}).get("data", []):
        items = (att.get("subattachments") or {}).get("data") or [att]
        for it in items:
            mtype = (it.get("media_type") or "").lower()
            media = it.get("media") or {}
            if media.get("source"):
                urls.append(media["source"])
                continue
            target_id = (it.get("target") or {}).get("id")
            if "video" in mtype:
                src = video_source(target_id) if target_id else None
                if src:
                    urls.append(src)
                continue  # nunca salva a thumbnail no lugar do vídeo
            img = (media.get("image") or {}).get("src")
            if img:
                urls.append(img)
    return urls


def instagram_media_urls(ig_media_id: str) -> list[str]:
    """Fallback: mídia do post do Instagram (comum em anúncios de parceria)."""
    data = meta_get(ig_media_id, {"fields": "media_type,media_url,children{media_type,media_url}"})
    if "error" in data:
        return []
    kids = (data.get("children") or {}).get("data")
    if kids:
        return [k["media_url"] for k in kids if k.get("media_url")]
    return [data["media_url"]] if data.get("media_url") else []


def resolve_media_urls(cr: dict) -> tuple[list[str], str]:
    assets = collect_assets(cr)

    urls: list[str] = []
    ok = bool(assets)
    for kind, value in assets:
        if kind == "video":
            u = video_source(value)
        elif kind == "hash":
            u = image_url_from_hash(value)
        else:
            u = value
        if not u:
            ok = False
            break
        urls.append(u)
    if ok:
        return urls, "direto"

    need = max(len(assets), 1)
    has_video = any(k == "video" for k, _ in assets)

    if cr.get("effective_object_story_id"):
        u = story_media_urls(cr["effective_object_story_id"])
        if len(u) >= need:
            return u, "post"

    if cr.get("effective_instagram_media_id"):
        u = instagram_media_urls(cr["effective_instagram_media_id"])
        if len(u) >= need:
            return u, "instagram"

    if not has_video and need == 1 and cr.get("image_url"):
        return [cr["image_url"]], "image_url"

    return [], f"nenhuma URL de mídia encontrada (mídias detectadas: {assets or 'nenhuma'})"


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------
def download(url: str, dest_dir: str, idx: int, retries: int = 3) -> tuple[str, str, str]:
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            with http.get(url, stream=True, timeout=180) as r:
                r.raise_for_status()
                mime = r.headers.get("Content-Type", "application/octet-stream").split(";")[0].strip()
                ext = EXT_BY_MIME.get(mime) or mimetypes.guess_extension(mime) or ""
                path = os.path.join(dest_dir, f"{idx:02d}{ext}")
                with open(path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        f.write(chunk)
            if os.path.getsize(path) == 0:
                raise IOError("arquivo baixado está vazio")
            return path, mime, ext
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"falha no download: {last_exc}")


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------
def drive_client():
    scopes = ["https://www.googleapis.com/auth/drive"]
    sa_json = os.getenv("GDRIVE_SA_JSON", "").strip()
    if sa_json:
        creds = service_account.Credentials.from_service_account_info(json.loads(sa_json), scopes=scopes)
    else:
        creds = Credentials(
            token=None,
            refresh_token=os.environ["GDRIVE_REFRESH_TOKEN"].strip(),
            client_id=os.environ["GDRIVE_CLIENT_ID"].strip(),
            client_secret=os.environ["GDRIVE_CLIENT_SECRET"].strip(),
            token_uri="https://oauth2.googleapis.com/token",
            scopes=scopes,
        )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def name_key(name: str, is_folder: bool) -> str:
    if APPEND_EXTENSION and not is_folder:
        stem, ext = os.path.splitext(name)
        if ext.lower() in KNOWN_EXTS:
            return stem
    return name


def existing_names(svc) -> set[str]:
    names: set[str] = set()
    page_token = None
    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed = false"
    while True:
        resp = svc.files().list(
            q=query,
            fields="nextPageToken, files(name, mimeType)",
            pageSize=1000,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        for f in resp.get("files", []):
            names.add(name_key(f["name"], f["mimeType"] == FOLDER_MIME))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return names


def upload_file(svc, path: str, name: str, mime: str, parent_id: str, ad_id: str) -> str:
    media = MediaFileUpload(path, mimetype=mime, resumable=True, chunksize=16 * 1024 * 1024)
    body = {"name": name, "parents": [parent_id], "appProperties": {"meta_ad_id": ad_id}}
    request = svc.files().create(body=body, media_body=media, fields="id", supportsAllDrives=True)
    response = None
    while response is None:
        _, response = request.next_chunk(num_retries=5)
    return response["id"]


def create_folder(svc, name: str, ad_id: str) -> str:
    body = {
        "name": name,
        "mimeType": FOLDER_MIME,
        "parents": [DRIVE_FOLDER_ID],
        "appProperties": {"meta_ad_id": ad_id},
    }
    return svc.files().create(body=body, fields="id", supportsAllDrives=True).execute()["id"]


def trash(svc, file_id: str) -> None:
    try:
        svc.files().update(fileId=file_id, body={"trashed": True}, supportsAllDrives=True).execute()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Relatórios
# ---------------------------------------------------------------------------
def write_reports(failures: list[dict], stats: dict) -> None:
    if failures:
        with open(FAILURES_CSV, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["ad_id", "ad_name", "status", "motivo"], delimiter=";")
            w.writeheader()
            w.writerows(failures)

    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = [
        "## Sync de criativos Meta Ads -> Google Drive",
        "",
        "| Métrica | Valor |",
        "|---|---|",
    ]
    lines += [f"| {k} | {v} |" for k, v in stats.items()]
    if failures:
        lines += ["", f"### Falhas ({len(failures)})", "", "| Anúncio | Motivo |", "|---|---|"]
        for fl in failures[:200]:
            nome = fl["ad_name"].replace("|", "\\|")
            motivo = fl["motivo"].replace("|", "\\|")[:200]
            lines.append(f"| {nome} | {motivo} |")
        if len(failures) > 200:
            lines.append(f"| ... | +{len(failures) - 200} no artefato failures.csv |")
    with open(summary_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    svc = drive_client()

    log("Lendo o que já existe na pasta do Drive...")
    existing = existing_names(svc)
    log(f"   {len(existing)} nomes já no Drive")

    log(f"Listando anúncios da conta {AD_ACCOUNT_ID}...")
    ads = fetch_all_ads()
    # Mais antigo primeiro: em nomes duplicados, o anúncio original fica com o nome
    ads.sort(key=lambda a: (a.get("created_time") or "", a.get("id") or ""))

    failures: list[dict] = []

    def fail(ad: dict, motivo: str) -> None:
        failures.append({
            "ad_id": ad.get("id", ""),
            "ad_name": ad.get("name", ""),
            "status": ad.get("effective_status", ""),
            "motivo": motivo,
        })
        log(f"   FALHA: {motivo}")

    seen: set[str] = set()
    pending: list[dict] = []
    duplicated = 0
    for ad in ads:
        name = ad.get("name") or ""
        if not name.strip():
            fail(ad, "anúncio sem nome")
            continue
        if name in seen:
            duplicated += 1
            continue
        seen.add(name)
        if name not in existing:
            pending.append(ad)

    log(
        f"{len(ads)} anúncios | {len(seen)} nomes únicos | {duplicated} nomes repetidos ignorados | "
        f"{len(pending)} novos para subir"
    )

    uploaded = 0
    stopped_early = False
    for i, ad in enumerate(pending, 1):
        if (time.monotonic() - START) / 60 > MAX_MINUTES:
            stopped_early = True
            log(f"Limite de {MAX_MINUTES:.0f} min atingido. O restante sobe na próxima execução.")
            break

        name = ad["name"]
        creative = ad.get("creative") or {}
        log(f"[{i}/{len(pending)}] {name}")

        if not creative:
            fail(ad, "anúncio sem creative")
            continue

        urls, how = resolve_media_urls(creative)
        if not urls:
            fail(ad, how)
            continue

        try:
            with tempfile.TemporaryDirectory() as tmp:
                files = [download(u, tmp, n) for n, u in enumerate(urls, 1)]
                if len(files) == 1:
                    path, mime, ext = files[0]
                    drive_name = name + (ext if APPEND_EXTENSION else "")
                    upload_file(svc, path, drive_name, mime, DRIVE_FOLDER_ID, ad["id"])
                else:
                    folder_id = create_folder(svc, name, ad["id"])
                    try:
                        for path, mime, _ in files:
                            upload_file(svc, path, os.path.basename(path), mime, folder_id, ad["id"])
                    except Exception:
                        trash(svc, folder_id)  # não deixa pasta pela metade
                        raise
            existing.add(name)
            uploaded += 1
            log(f"   OK via {how} ({len(files)} arquivo(s))")
        except Exception as exc:  # noqa: BLE001
            fail(ad, str(exc))

        time.sleep(0.3)

    stats = {
        "Anúncios na conta": len(ads),
        "Nomes únicos": len(seen),
        "Nomes repetidos ignorados": duplicated,
        "Novos nesta execução": len(pending),
        "Enviados ao Drive": uploaded,
        "Falhas": len(failures),
        "Parou pelo limite de tempo": "sim" if stopped_early else "não",
        "Duração (min)": f"{(time.monotonic() - START) / 60:.1f}",
    }
    write_reports(failures, stats)
    log(" | ".join(f"{k}: {v}" for k, v in stats.items()))

    return 1 if (FAIL_ON_ERRORS and failures) else 0


if __name__ == "__main__":
    sys.exit(main())
