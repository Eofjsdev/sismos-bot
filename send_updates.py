"""
Revisa los commits (actualizaciones) del repositorio de MichiHub y publica
en un canal de Telegram una IMAGEN con el diseño de MichiHub (mismo logo,
colores y tipografía) cada vez que hay uno nuevo, mostrando qué cambió.

Pensado para correr cada 5 minutos vía GitHub Actions (cron). Para no
repetir avisos guarda los SHA ya enviados en `sent_updates.json`, que el
workflow "commitea" de vuelta al repositorio (ver
.github/workflows/check-updates.yml).

Primera ejecución: si `sent_updates.json` no existe, solo registra los
commits actuales SIN enviarlos (para no inundar el canal con todo el
historial). A partir de ahí avisa de cada commit nuevo.

Variables de entorno:
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID   (@usuario del canal o chat_id numérico)
- GITHUB_TOKEN       (opcional; sube el límite de la API de GitHub)
- MICHIHUB_REPO      (opcional; por defecto cruzangelsaid34/Michihub)
- MICHIHUB_BRANCH    (opcional; por defecto main)
- TEST_MODE=1        (opcional; envía el último commit sin guardar estado)
- COMMIT_SHA         (modo push: envía justo el commit de ese push, sin estado)
- BEFORE_SHA         (modo push: SHA anterior al push, para incluir todos sus commits)
"""

import base64
import html
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

REPO = os.environ.get("MICHIHUB_REPO", "cruzangelsaid34/Michihub")
BRANCH = os.environ.get("MICHIHUB_BRANCH", "main")
API_URL = f"https://api.github.com/repos/{REPO}/commits"

RAIZ = Path(__file__).parent
ESTADO_PATH = RAIZ / "sent_updates.json"
PLANTILLA_PATH = RAIZ / "plantilla_update.html"
LOGO_PATH = RAIZ / "logo.png"
if not LOGO_PATH.exists():
    LOGO_PATH = RAIZ / "assets" / "logo.png"
MAX_HISTORIAL = 200  # SHAs recordados (para que el archivo no crezca sin fin)


def cargar_estado() -> dict | None:
    """Devuelve el estado guardado, o None si es la primera ejecución."""
    if not ESTADO_PATH.exists():
        return None
    try:
        data = json.loads(ESTADO_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def guardar_estado(enviados: dict):
    # Conserva solo los MAX_HISTORIAL más recientes.
    recientes = dict(list(enviados.items())[-MAX_HISTORIAL:])
    ESTADO_PATH.write_text(
        json.dumps(recientes, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _headers() -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def obtener_commits() -> list:
    resp = requests.get(
        API_URL,
        params={"sha": BRANCH, "per_page": 30},
        headers=_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def commits_del_push(sha: str, antes: str) -> list:
    """Commits de un push, del más antiguo al más nuevo (máx. 5)."""
    commits = []
    if antes and set(antes) != {"0"}:
        try:
            resp = requests.get(
                f"https://api.github.com/repos/{REPO}/compare/{antes}...{sha}",
                headers=_headers(),
                timeout=30,
            )
            resp.raise_for_status()
            commits = resp.json().get("commits", [])
        except Exception as exc:
            print(f"⚠️ No se pudo comparar {antes[:7]}...{sha[:7]}: {exc}", file=sys.stderr)
    if not commits:
        resp = requests.get(f"{API_URL}/{sha}", headers=_headers(), timeout=30)
        resp.raise_for_status()
        commits = [resp.json()]
    return commits[-5:]


def obtener_archivos(sha: str) -> list:
    """Archivos que cambió el commit. Si falla, devuelve lista vacía."""
    try:
        resp = requests.get(f"{API_URL}/{sha}", headers=_headers(), timeout=30)
        resp.raise_for_status()
        return resp.json().get("files", [])
    except Exception as exc:
        print(f"⚠️ No se pudo leer los archivos de {sha[:7]}: {exc}", file=sys.stderr)
        return []


def _datos_commit(commit: dict) -> dict:
    info = commit.get("commit", {})
    mensaje = (info.get("message") or "(sin mensaje)").strip()
    titulo, _, cuerpo = mensaje.partition("\n")
    autor = (
        (commit.get("author") or {}).get("login")
        or (info.get("author") or {}).get("name")
        or "desconocido"
    )
    fecha_iso = (info.get("author") or {}).get("date", "")
    try:
        fecha = datetime.fromisoformat(fecha_iso.replace("Z", "+00:00"))
        try:
            from zoneinfo import ZoneInfo
            fecha = fecha.astimezone(ZoneInfo("America/Mexico_City"))
        except Exception:
            fecha = fecha.astimezone(timezone.utc)
        fecha_txt = fecha.strftime("%d/%m/%Y %H:%M")
    except ValueError:
        fecha_txt = "N/D"
    return {
        "titulo": titulo.strip() or "Actualización",
        "cuerpo": cuerpo.strip(),
        "autor": autor,
        "fecha": fecha_txt,
        "sha": commit.get("sha", "")[:7],
        "url": commit.get("html_url", ""),
    }


def formatear_caption(commit: dict) -> str:
    d = _datos_commit(commit)
    lineas = [f"🐾 <b>{html.escape(d['titulo'])}</b>"]
    if d["url"]:
        lineas.append(f'🔗 <a href="{html.escape(d["url"], quote=True)}">Ver cambios en GitHub</a>')
    return "\n".join(lineas)


# ---------------------------------------------------------------------------
# Imagen con el diseño de MichiHub
# ---------------------------------------------------------------------------

ICONO_ESTADO = {"added": "✨", "removed": "🗑️", "renamed": "🔀"}
MAX_ARCHIVOS = 4


def _html_archivos(archivos: list) -> str:
    filas = []
    for f in archivos[:MAX_ARCHIVOS]:
        icono = ICONO_ESTADO.get(f.get("status"), "✏️")
        nombre = html.escape(f.get("filename", ""))
        filas.append(f'<div class="archivo">{icono} {nombre}</div>')
    resto = len(archivos) - MAX_ARCHIVOS
    if resto > 0:
        filas.append(f'<div class="archivo mas">y {resto} más…</div>')
    return "".join(filas)


def construir_html(commit: dict, archivos: list, logo_b64: str) -> str:
    d = _datos_commit(commit)
    cuerpo = d["cuerpo"]
    if len(cuerpo) > 240:
        cuerpo = cuerpo[:240].rstrip() + "…"
    return (
        PLANTILLA_PATH.read_text(encoding="utf-8")
        .replace("{{LOGO_B64}}", logo_b64)
        .replace("{{TITULO}}", html.escape(d["titulo"]))
        .replace("{{DESCRIPCION}}", html.escape(cuerpo))
        .replace("{{ARCHIVOS}}", _html_archivos(archivos))
        .replace("{{AUTOR}}", html.escape(d["autor"]))
        .replace("{{FECHA}}", d["fecha"])
        .replace("{{SHA}}", html.escape(d["sha"]))
    )


def generar_imagen(commit: dict, archivos: list, navegador) -> bytes:
    logo_b64 = base64.b64encode(LOGO_PATH.read_bytes()).decode("ascii")
    pagina = navegador.new_page(viewport={"width": 1080, "height": 1350})
    pagina.set_content(construir_html(commit, archivos, logo_b64), wait_until="load")
    try:
        pagina.wait_for_selector("body[data-listo='1']", timeout=8000)
    except Exception:
        pass  # si tarda demasiado, se toma la captura igual
    imagen = pagina.screenshot(type="png")
    pagina.close()
    return imagen


def enviar_foto_telegram(token: str, chat_id: str, imagen: bytes, caption: str) -> bool:
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendPhoto",
        data={"chat_id": chat_id, "caption": caption[:1024], "parse_mode": "HTML"},
        files={"photo": ("actualizacion.png", imagen, "image/png")},
        timeout=60,
    )
    if not resp.ok:
        print(f"⚠️ Error enviando foto: {resp.status_code} {resp.text}", file=sys.stderr)
        return False
    return True


def enviar_texto_telegram(token: str, chat_id: str, texto: str) -> bool:
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={
            "chat_id": chat_id,
            "text": texto,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    if not resp.ok:
        print(f"⚠️ Error enviando mensaje: {resp.status_code} {resp.text}", file=sys.stderr)
        return False
    return True


def publicar_commit(token: str, chat_id: str, commit: dict, navegador) -> bool:
    """Envía la imagen; si no se puede generar, manda el aviso como texto."""
    caption = formatear_caption(commit)
    archivos = obtener_archivos(commit["sha"])
    try:
        imagen = generar_imagen(commit, archivos, navegador)
        return enviar_foto_telegram(token, chat_id, imagen, caption)
    except Exception:
        import traceback
        print("⚠️ Falló la imagen, se envía como texto:", file=sys.stderr)
        traceback.print_exc()
        return enviar_texto_telegram(token, chat_id, caption)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        sys.exit("❌ Faltan TELEGRAM_BOT_TOKEN y/o TELEGRAM_CHAT_ID")

    # Modo push: lo dispara el workflow del repo de MichiHub en cada commit.
    commit_sha = os.environ.get("COMMIT_SHA", "").strip()
    if commit_sha:
        try:
            del_push = commits_del_push(commit_sha, os.environ.get("BEFORE_SHA", "").strip())
        except Exception as exc:
            sys.exit(f"❌ No se pudo leer el commit {commit_sha[:7]}: {exc}")
        fallos = 0
        with sync_playwright() as p:
            navegador = p.chromium.launch()
            for commit in del_push:
                if publicar_commit(token, chat_id, commit, navegador):
                    print(f"✅ Enviado: {commit['sha'][:7]}")
                else:
                    fallos += 1
                    print(f"❌ No se pudo enviar: {commit['sha'][:7]}", file=sys.stderr)
            navegador.close()
        if fallos:
            sys.exit(1)
        return

    try:
        commits = obtener_commits()
    except Exception as exc:
        sys.exit(f"❌ No se pudo consultar GitHub: {exc}")

    if not commits:
        print("El repositorio no devolvió commits.")
        return

    if os.environ.get("TEST_MODE", "").strip().lower() in ("1", "true"):
        print("🧪 TEST_MODE: enviando el último commit (no se guarda estado)")
        with sync_playwright() as p:
            navegador = p.chromium.launch()
            ok = publicar_commit(token, chat_id, commits[0], navegador)
            navegador.close()
        print("✅ Enviado" if ok else "❌ Falló el envío")
        return

    estado = cargar_estado()
    ahora = datetime.now(timezone.utc).isoformat()

    # Primera ejecución: registrar el historial actual sin avisar.
    if estado is None:
        guardar_estado({c["sha"]: ahora for c in reversed(commits)})
        print(f"Primera ejecución: {len(commits)} commits registrados sin enviar.")
        return

    # La API devuelve del más nuevo al más antiguo; enviamos en orden cronológico.
    nuevos = [c for c in reversed(commits) if c["sha"] not in estado]
    if not nuevos:
        print("Sin actualizaciones nuevas.")
        return

    with sync_playwright() as p:
        navegador = p.chromium.launch()
        for commit in nuevos:
            sha = commit["sha"]
            if publicar_commit(token, chat_id, commit, navegador):
                estado[sha] = ahora
                print(f"✅ Enviado: {sha[:7]}")
            else:
                print(f"❌ No se pudo enviar: {sha[:7]} (se reintenta en la próxima)", file=sys.stderr)
        navegador.close()

    guardar_estado(estado)


if __name__ == "__main__":
    main()
