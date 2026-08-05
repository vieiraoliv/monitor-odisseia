#!/usr/bin/env python3
"""
Monitor de sessoes - A Odisseia no Cinesystem Pompeia (Ingresso.com)

Navega pelas abas de data (o site NAO aceita ?data= na URL) e le a grade
com um parser linha a linha. Notifica no Telegram so quando aparece
sessao nova de sabado ou domingo.
"""

import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import requests
from playwright.sync_api import sync_playwright

# ─────────────────────────── CONFIG ───────────────────────────

CINEMA_URL = "https://www.ingresso.com/cinema/cinesystem-pompeia"

FILME_KEYWORDS = ["odisseia"]      # sem acento, minusculo
DIAS_DESEJADOS = {5, 6}            # 5=sab, 6=dom
SOMENTE_IMAX = True

ESTADO_ARQUIVO = "sessoes_vistas.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

BRT = timezone(timedelta(hours=-3))
DIAS_PT = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sab", "Dom"]

# Marcadores que aparecem como linha propria na grade
TECNOLOGIAS = {"imax", "3d", "xd", "4dx", "d-box", "vip"}
AUDIOS = {"legendado", "dublado", "nacional"}

RE_HORA = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
RE_DURACAO = re.compile(r"^\d{1,2}h\d{2}$")
RE_DATA_ABA = re.compile(r"^(\d{2})/(\d{2})$")

# ──────────────────────────────────────────────────────────────


def sem_acento(t):
    nfkd = unicodedata.normalize("NFKD", t.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def enviar_telegram(texto):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        print("[aviso] Telegram nao configurado.", file=sys.stderr)
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": texto,
                "parse_mode": "Markdown",
            },
            timeout=20,
        )
        if r.status_code != 200:
            print(f"[erro telegram] {r.status_code} {r.text}", file=sys.stderr)
    except Exception as e:
        print(f"[erro telegram] {e}", file=sys.stderr)


def carregar_estado():
    if os.path.exists(ESTADO_ARQUIVO):
        try:
            with open(ESTADO_ARQUIVO, encoding="utf-8") as f:
                d = json.load(f)
            return set(d.get("sessoes", [])), d.get("baseline_feito", False)
        except Exception:
            pass
    return set(), False


def salvar_estado(vistas, baseline_feito):
    with open(ESTADO_ARQUIVO, "w", encoding="utf-8") as f:
        json.dump(
            {
                "atualizado_em": datetime.now(BRT).isoformat(),
                "baseline_feito": baseline_feito,
                "sessoes": sorted(vistas),
            },
            f,
            ensure_ascii=False,
            indent=2,
        )


# ───────────────────────── PARSER ─────────────────────────


def parsear_grade(texto):
    """
    Le a grade linha a linha. Formato real do site:

        14                <- classificacao etaria
        A Odisseia        <- titulo
        2h52              <- duracao (confirma que a linha anterior e titulo)
        LEGENDADO         <- marcador de audio
        14:30             <- horarios
        18:00
        IMAX              <- marcador de tecnologia
        LEGENDADO
        13:30
        ...

    Um marcador de tecnologia vale para o proximo grupo de audio.
    Retorna [{'titulo', 'hora', 'tech', 'audio'}]
    """
    # Corta a secao de recomendacoes do rodape, que repete titulos sem horario
    corte = texto.find("Você também pode gostar")
    if corte > 0:
        texto = texto[:corte]

    linhas = [l.strip() for l in texto.split("\n")]
    linhas = [l for l in linhas if l]

    sessoes = []
    titulo_atual = None
    audio_atual = None
    tech_atual = "Normal"
    tech_pendente = []

    for i, linha in enumerate(linhas):
        norm = sem_acento(linha)

        # Titulo = linha seguida por uma duracao (ex: "2h52")
        if i + 1 < len(linhas) and RE_DURACAO.match(linhas[i + 1]):
            titulo_atual = linha
            audio_atual = None
            tech_atual = "Normal"
            tech_pendente = []
            continue

        if RE_DURACAO.match(linha):
            continue

        if norm in TECNOLOGIAS:
            tech_pendente.append(linha.upper())
            continue

        if norm in AUDIOS:
            audio_atual = linha.upper()
            tech_atual = " ".join(tech_pendente) if tech_pendente else "Normal"
            tech_pendente = []
            continue

        m = RE_HORA.match(linha)
        if m and titulo_atual:
            sessoes.append(
                {
                    "titulo": titulo_atual,
                    "hora": f"{int(m.group(1)):02d}:{m.group(2)}",
                    "tech": tech_atual,
                    "audio": audio_atual or "-",
                }
            )

    return sessoes


def filtrar(sessoes):
    saida = []
    for s in sessoes:
        t = sem_acento(s["titulo"])
        if not any(k in t for k in FILME_KEYWORDS):
            continue
        eh_imax = "IMAX" in s["tech"].upper()
        if SOMENTE_IMAX and not eh_imax:
            continue
        s["imax"] = eh_imax
        saida.append(s)
    return saida


# ─────────────────── NAVEGACAO POR ABAS ───────────────────


def abrir_cinema(page):
    page.goto(CINEMA_URL, wait_until="domcontentloaded", timeout=45_000)
    try:
        page.wait_for_function(
            "() => /\\b([01]?\\d|2[0-3]):[0-5]\\d\\b/.test(document.body.innerText)",
            timeout=30_000,
        )
    except Exception:
        pass
    time.sleep(2)


def listar_abas_de_data(page):
    """
    Le as abas de data no topo (ex: '08/08') e devolve
    [{'label': '08/08', 'date': date(...)}] apenas dos dias desejados.
    """
    labels = page.evaluate(
        """() => {
            const out = [];
            document.querySelectorAll('*').forEach(el => {
                if (el.children.length) return;
                const t = (el.innerText || '').trim();
                if (/^\\d{2}\\/\\d{2}$/.test(t) && !out.includes(t)) out.push(t);
            });
            return out;
        }"""
    )

    hoje = datetime.now(BRT).date()
    abas = []
    for label in labels:
        m = RE_DATA_ABA.match(label)
        if not m:
            continue
        dia, mes = int(m.group(1)), int(m.group(2))
        ano = hoje.year
        # Vira o ano se a aba for de um mes anterior ao atual
        if mes < hoje.month:
            ano += 1
        try:
            d = datetime(ano, mes, dia).date()
        except ValueError:
            continue
        if d.weekday() in DIAS_DESEJADOS and d >= hoje:
            abas.append({"label": label, "date": d})

    return abas


def clicar_aba(page, label):
    """Clica na aba de data e espera a grade trocar. True se conseguiu."""
    antes = page.inner_text("body")

    try:
        page.locator(f"text=/^{re.escape(label)}$/").first.click(timeout=10_000)
    except Exception as e:
        print(f"  [falha ao clicar em {label}] {e}", file=sys.stderr)
        return False

    # Espera o conteudo mudar (ate 12s). Se nao mudar, pode ser grade
    # identica entre dois dias — seguimos, mas registramos o aviso.
    for _ in range(24):
        time.sleep(0.5)
        if page.inner_text("body") != antes:
            time.sleep(1.5)
            return True

    print(f"  [aviso] grade nao mudou apos clicar em {label}", file=sys.stderr)
    return True


# ───────────────────────── MAIN ─────────────────────────


def main():
    vistas, baseline_feito = carregar_estado()
    encontradas, novas = [], []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            locale="pt-BR",
            viewport={"width": 1366, "height": 1200},
        )
        page = ctx.new_page()

        try:
            abrir_cinema(page)
            abas = listar_abas_de_data(page)
            print(f"Abas de sab/dom encontradas: {[a['label'] for a in abas]}")

            for aba in abas:
                if not clicar_aba(page, aba["label"]):
                    continue

                todas = parsear_grade(page.inner_text("body"))
                do_filme = filtrar(todas)

                print(
                    f"  {aba['label']}: {len(todas)} sessoes na pagina, "
                    f"{len(do_filme)} de A Odisseia"
                    + (" IMAX" if SOMENTE_IMAX else "")
                )

                for s in do_filme:
                    sid = f"{aba['date'].isoformat()}|{s['hora']}|{s['tech']}|{s['audio']}"
                    encontradas.append(sid)
                    if sid not in vistas:
                        novas.append(
                            {"date": aba["date"], "hora": s["hora"], "tech": s["tech"]}
                        )
                        vistas.add(sid)
        except Exception as e:
            print(f"[erro] {e}", file=sys.stderr)
        finally:
            browser.close()

    agora = datetime.now(BRT).strftime("%d/%m %H:%M")

    if not baseline_feito:
        if not encontradas:
            print(f"[{agora}] nada encontrado; baseline adiado.")
            return
        salvar_estado(vistas, True)
        print(f"[{agora}] baseline criado com {len(vistas)} sessoes.")
        enviar_telegram(
            "*Monitor ativado* \U0001F3AC\n\n"
            f"Grade atual registrada ({len(vistas)} sessoes de sab/dom).\n"
            "Aviso so quando abrir horario novo."
        )
        return

    if novas:
        linhas = []
        for s in sorted(novas, key=lambda x: (x["date"], x["hora"])):
            d = s["date"]
            linhas.append(
                f"- {DIAS_PT[d.weekday()]} {d.strftime('%d/%m')} as {s['hora']} ({s['tech']})"
            )
        enviar_telegram(
            "\U0001F3AC *Sessao nova de A Odisseia!*\n\n"
            "Cinesystem Pompeia:\n" + "\n".join(linhas) + f"\n\n{CINEMA_URL}"
        )
        print(f"[{agora}] {len(novas)} sessao(oes) nova(s) notificada(s).")
    else:
        print(f"[{agora}] nada novo ({len(encontradas)} sessoes na grade).")

    salvar_estado(vistas, True)


if __name__ == "__main__":
    main()
