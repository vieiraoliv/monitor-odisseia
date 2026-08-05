#!/usr/bin/env python3
"""
Diagnostico: despeja o que o Playwright realmente enxerga na pagina
do Cinesystem Pompeia. Nao notifica nada, so gera arquivos para inspecao.

Os arquivos vao para a pasta debug/ e sao publicados como artifact do Actions.
"""

import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone

from playwright.sync_api import sync_playwright

CINEMA_URL = "https://www.ingresso.com/cinema/cinesystem-pompeia"
BRT = timezone(timedelta(hours=-3))
SAIDA = "debug"


def sem_acento(t):
    nfkd = unicodedata.normalize("NFKD", t.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def proximo_fim_de_semana():
    hoje = datetime.now(BRT).date()
    for i in range(14):
        d = hoje + timedelta(days=i)
        if d.weekday() in (5, 6):
            return d
    return hoje


def main():
    os.makedirs(SAIDA, exist_ok=True)
    sabado = proximo_fim_de_semana()

    alvos = [
        ("sem_parametro", CINEMA_URL),
        ("com_data", f"{CINEMA_URL}?data={sabado.isoformat()}"),
    ]

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

        for nome, url in alvos:
            print(f"\n{'=' * 70}\n{nome}: {url}\n{'=' * 70}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                time.sleep(8)  # tempo generoso para o React montar

                texto = page.inner_text("body")
                html = page.content()

                with open(f"{SAIDA}/{nome}.txt", "w", encoding="utf-8") as f:
                    f.write(texto)
                with open(f"{SAIDA}/{nome}.html", "w", encoding="utf-8") as f:
                    f.write(html)
                page.screenshot(path=f"{SAIDA}/{nome}.png", full_page=True)

                norm = sem_acento(texto)
                horarios = re.findall(r"\b([01]?\d|2[0-3]):[0-5]\d\b", texto)

                print(f"  titulo da aba .......: {page.title()}")
                print(f"  tamanho do texto ....: {len(texto)} caracteres")
                print(f"  contem 'odisseia' ...: {'odisseia' in norm}")
                print(f"  contem 'imax' .......: {'imax' in norm}")
                print(f"  horarios HH:MM ......: {len(horarios)}")
                print(f"  contem 'cinesystem' .: {'cinesystem' in norm}")

                # Sinais de bloqueio anti-bot
                for sinal in ["captcha", "acesso negado", "forbidden",
                              "cloudflare", "verificando", "robot"]:
                    if sinal in norm:
                        print(f"  !! possivel bloqueio: '{sinal}' na pagina")

                # Mostra as primeiras linhas nao vazias, para eu ver a estrutura
                linhas = [l.strip() for l in texto.split("\n") if l.strip()]
                print(f"\n  --- primeiras 40 linhas ---")
                for l in linhas[:40]:
                    print(f"  | {l[:100]}")

                # Se achou o filme, mostra o contexto ao redor
                if "odisseia" in norm:
                    pos = norm.find("odisseia")
                    trecho = texto[max(0, pos - 300): pos + 800]
                    print(f"\n  --- contexto ao redor de 'Odisseia' ---")
                    for l in trecho.split("\n"):
                        if l.strip():
                            print(f"  > {l.strip()[:100]}")

            except Exception as e:
                print(f"  ERRO: {e}")

        browser.close()

    print(f"\nArquivos salvos em {SAIDA}/ — baixe o artifact para inspecionar.")


if __name__ == "__main__":
    main()
