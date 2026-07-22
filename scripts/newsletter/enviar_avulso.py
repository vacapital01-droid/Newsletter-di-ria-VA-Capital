#!/usr/bin/env python3
"""Envio avulso da VA Capital.

Manda um email pontual (fora da newsletter automática das 7h) para a mesma
lista de destinatários, mantendo o padrão de privacidade: TO = caixa do dono,
BCC = clientes.

Conteúdo: lido de `scripts/newsletter/avulso.html` (corpo) e da variável de
ambiente AVULSO_ASSUNTO (assunto).

Variáveis de ambiente esperadas (mesmas do gerador automático):
  RESEND_API_KEY, NEWSLETTER_FROM, NEWSLETTER_TO, NEWSLETTER_OWNER (opcional),
  NEWSLETTER_REPLY_TO (opcional), AVULSO_ASSUNTO
"""

import os
import re
import sys
from pathlib import Path

import requests

CORPO = Path(__file__).parent / "avulso.html"


def html_para_texto(html: str) -> str:
    texto = re.sub(r"<br\s*/?>", "\n", html)
    texto = re.sub(r"</(p|div|h1|h2|h3|li|tr)>", "\n", texto)
    texto = re.sub(r"<[^>]+>", "", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    return texto.strip()


def main() -> int:
    html = CORPO.read_text(encoding="utf-8")
    assunto = os.environ["AVULSO_ASSUNTO"].strip()

    api_key = os.environ["RESEND_API_KEY"].strip()
    remetente = os.environ["NEWSLETTER_FROM"].strip()
    destinatarios = [e.strip() for e in os.environ["NEWSLETTER_TO"].split(",") if e.strip()]
    reply_to = (os.environ.get("NEWSLETTER_REPLY_TO") or "").strip() or None

    to_proprio = (os.environ.get("NEWSLETTER_OWNER") or "vacapital01@gmail.com").strip()
    destinatarios = [e for e in destinatarios if e.lower() != to_proprio.lower()]

    payload = {
        "from": remetente,
        "to": [to_proprio],
        "bcc": destinatarios,
        "subject": assunto,
        "html": html,
        "text": html_para_texto(html),
    }
    if reply_to:
        payload["reply_to"] = reply_to

    if os.environ.get("AVULSO_DRY_RUN") == "1":
        print(f"[dry-run] assunto: {assunto}")
        print(f"[dry-run] TO: {to_proprio} | BCC: {len(destinatarios)} clientes")
        print(html_para_texto(html))
        return 0

    resposta = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    resposta.raise_for_status()
    print(f"Enviado. TO: {to_proprio} | BCC: {len(destinatarios)} clientes")
    print(resposta.json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
