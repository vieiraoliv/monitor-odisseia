#!/usr/bin/env python3
"""
Monitor de sessoes - A Odisseia no Cinesystem Pompeia (Ingresso.com)

Roda UMA vez por execucao. O agendamento fica por conta do GitHub Actions.
Notifica no Telegram quando aparece uma sessao nova de sabado ou domingo.
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

# Palavras que identificam o filme (sem acento, minusculo)
FILME_KEYWORDS = ["odisseia"]

# Dias da semana desejados (0=seg ... 5=sab, 6=dom)
DIAS_DESEJADOS = {5, 6}

# Avisar so de sessoes IMAX?
SOMENTE_IMAX = True

# Quantos dias a frente procurar
DIAS_A_FRENTE = 21

ESTADO_ARQUIVO = "sessoes_vistas.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Fuso de Sao Paulo (sem depender de tzdata do runner)
BRT = timezone(timedelta(hours=-3))

DIAS_PT = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sab", "Dom"]

# ──────────────────────────────────────────────────────────────


def sem_acento(texto):
    nfkd = unicodedata.normalize("NFKD", texto.lower())
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
                "disable_web_page_preview": False,
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
                dados = json.load(f)
            return set(dados.get("sessoes", [])), dados.get("baseline_feito", False)
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


def datas_alvo():
    hoje = datetime.now(BRT).date()
    return [
        hoje + timedelta(days=i)
        for i in range(DIAS_A_FRENTE)
        if (hoje + timedelta(days=i)).weekday() in DIAS_DESEJADOS
    ]


def abrir_pagina(page, url, tentativas=3):
    """Abre a URL sem depender de networkidle e espera o conteudo renderizar."""
    ultimo_erro = None
    for tentativa in range(1, tentativas + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)

            # Espera aparecer QUALQUER horario no formato HH:MM na pagina,
            # que e o sinal de que o React terminou de montar a grade.
            try:
                page.wait_for_function(
                    "() => /\\b([01]?\\d|2[0-3]):[0-5]\\d\\b/.test(document.body.innerText)",
                    timeout=30_000,
                )
            except Exception:
                # Pode ser um dia legitimamente sem sessoes. Segue adiante.
                pass

            time.sleep(2)
            return True
        except Exception as e:
            ultimo_erro = e
            print(f"[retry {tentativa}/{tentativas}] {url}: {e}", file=sys.stderr)
            time.sleep(3 * tentativa)

    print(f"[falhou] {url}: {ultimo_erro}", file=sys.stderr)
    return False


def coletar_sessoes(page, data):
    url = f"{CINEMA_URL}?data={data.isoformat()}"
    if not abrir_pagina(page, url):
        return []

    texto = page.inner_text("body")
    blocos = re.split(r"\n(?=[A-ZÁÉÍÓÚÂÊÔÃÕÇ][^\n]{3,80}\n)", texto)

    sessoes, ids = [], set()
    for bloco in blocos:
        norm = sem_acento(bloco)
        if not any(k in norm for k in FILME_KEYWORDS):
            continue
        eh_imax = "imax" in norm
        if SOMENTE_IMAX and not eh_imax:
            continue

        for h, m in re.findall(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", bloco):
            sid = f"{data.isoformat()}|{int(h):02d}:{m}|{'imax' if eh_imax else 'std'}"
            if sid in ids:
                continue
            ids.add(sid)
            sessoes.append(
                {
                    "data": data.isoformat(),
                    "hora": f"{int(h):02d}:{m}",
                    "imax": eh_imax,
                    "id": sid,
                }
            )
    return sessoes


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
            viewport={"width": 1366, "height": 900},
        )
        page = ctx.new_page()

        for data in datas_alvo():
            try:
                for s in coletar_sessoes(page, data):
                    encontradas.append(s)
                    if s["id"] not in vistas:
                        novas.append(s)
                        vistas.add(s["id"])
            except Exception as e:
                print(f"[erro] {data}: {e}", file=sys.stderr)
            time.sleep(1.5)

        browser.close()

    agora = datetime.now(BRT).strftime("%d/%m %H:%M")

    if not baseline_feito:
        if not encontradas:
            # Nao marca baseline com grade vazia: pode ter sido falha de rede,
            # e na proxima rodada tudo pareceria "novo" (spam de notificacao).
            print(f"[{agora}] nenhuma sessao encontrada; baseline adiado.")
            return
        salvar_estado(vistas, True)
        print(f"[{agora}] baseline criado com {len(vistas)} sessoes.")
        enviar_telegram(
            "*Monitor ativado* \U0001F3AC\n\n"
            f"Grade atual registrada ({len(vistas)} sessoes de sab/dom).\n"
            "A partir de agora aviso so quando abrir horario novo."
        )
        return

    if novas:
        linhas = []
        for s in sorted(novas, key=lambda x: (x["data"], x["hora"])):
            d = datetime.fromisoformat(s["data"])
            tag = " - IMAX" if s["imax"] else ""
            linhas.append(f"- {DIAS_PT[d.weekday()]} {d.strftime('%d/%m')} as {s['hora']}{tag}")

        enviar_telegram(
            "\U0001F3AC *Sessao nova de A Odisseia!*\n\n"
            "Cinesystem Pompeia:\n" + "\n".join(linhas) + "\n\n"
            f"Comprar: {CINEMA_URL}"
        )
        print(f"[{agora}] {len(novas)} sessao(oes) nova(s) notificada(s).")
    else:
        print(f"[{agora}] nada novo ({len(encontradas)} sessoes na grade).")

    salvar_estado(vistas, True)


if __name__ == "__main__":
    main()
