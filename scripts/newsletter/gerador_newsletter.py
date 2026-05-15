"""
Gerador de Newsletter VA Capital — versão grátis com GitHub Actions + Gemini Flash + Resend.

Pipeline:
  1. Coleta notícias via RSS (Brasil, Cripto, FIIs) das últimas 24h
  2. Pré-filtra por palavras-chave relevantes pro investidor BR
  3. Manda candidatas pro Gemini Flash (grátis) — IA seleciona 7 por categoria e
     gera análise ✅ positivo / ❌ negativo / 💡 impacto prático
  4. Monta o bloco no padrão VA Capital validado pelo Vini
  5. Envia via Resend pra lista de destinatários
  6. Salva backup .md em scripts/newsletter/historico/

Variáveis de ambiente necessárias (configuradas como secrets no GitHub Actions):
  GEMINI_API_KEY        - API key do Google AI Studio (aistudio.google.com)
  RESEND_API_KEY        - API key do Resend (resend.com)
  NEWSLETTER_FROM       - email remetente verificado no Resend (ex: vini@vacapital.com.br)
  NEWSLETTER_TO         - lista de destinatários separada por vírgula
  NEWSLETTER_REPLY_TO   - (opcional) email pra reply-to
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import feedparser
import google.generativeai as genai
import requests
from zoneinfo import ZoneInfo


SP_TZ = ZoneInfo("America/Sao_Paulo")

FEEDS_BRASIL = [
    "https://www.infomoney.com.br/mercados/feed/",
    "https://www.infomoney.com.br/economia/feed/",
    "https://www.moneytimes.com.br/feed/",
    "https://braziljournal.com/feed/",
    "https://valor.globo.com/rss/empresas/",
    "https://valor.globo.com/rss/financas/",
]

FEEDS_CRIPTO = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://www.livecoins.com.br/feed/",
    "https://portaldobitcoin.uol.com.br/feed/",
]

FEEDS_FII = [
    "https://www.fundsexplorer.com.br/blog/feed/",
    "https://www.suno.com.br/noticias/feed/",
    "https://clubefii.com.br/feed/",
]

PALAVRAS_BRASIL = [
    "petr4", "petrobras", "vale3", "vale", "itub", "bbas", "bbdc", "banco do brasil",
    "selic", "copom", "ipca", "câmbio", "dólar", "balanço", "dividendo", "ipo",
    "b3", "ibovespa", "ações", "ação", "lucro", "receita", "ebitda", "fusão",
    "aquisição", "tarifa", "reforma", "tributária", "imposto", "haddad", "lula",
    "campos neto", "galípolo", "fed", "powell", "payroll", "inflação", "pib",
]

PALAVRAS_CRIPTO = [
    "bitcoin", "btc", "ethereum", "eth", "etf", "spot", "binance", "coinbase",
    "halving", "trump", "sec", "regulação", "cvm", "tether", "usdt", "ripple",
    "xrp", "solana", "memecoin", "stablecoin", "wallet", "exchange", "mineração",
]

PALAVRAS_FII = [
    "fii", "fundo imobiliário", "fundos imobiliários", "ifix", "cota", "distribuição",
    "rendimento", "kncs11", "mxrf11", "hglg11", "knri11", "vghf11", "ifix",
    "papel", "tijolo", "logística", "lajes corporativas", "cri", "lci", "vacância",
    "shopping", "galpão", "imobiliário", "real estate", "reit",
]


@dataclass
class Noticia:
    titulo: str
    resumo: str
    fonte: str
    url: str
    publicado: datetime
    categoria: str  # "brasil" | "cripto" | "fii"


def _agora_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parsear_data(entry) -> datetime | None:
    for campo in ("published_parsed", "updated_parsed"):
        valor = entry.get(campo)
        if valor:
            try:
                return datetime(*valor[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def _limpar_html(texto: str) -> str:
    if not texto:
        return ""
    sem_tag = re.sub(r"<[^>]+>", "", texto)
    return re.sub(r"\s+", " ", sem_tag).strip()


def _coletar_feed(url: str, categoria: str, limite_horas: int = 24) -> list[Noticia]:
    try:
        feed = feedparser.parse(url, request_headers={"User-Agent": "VA-Capital-Newsletter/1.0"})
    except Exception as exc:
        print(f"  [WARN] erro lendo {url}: {exc}", file=sys.stderr)
        return []

    corte = _agora_utc() - timedelta(hours=limite_horas)
    fonte = feed.feed.get("title", url)
    coletadas = []

    for entry in feed.entries[:40]:
        publicado = _parsear_data(entry)
        if publicado is None or publicado < corte:
            continue
        titulo = _limpar_html(entry.get("title", ""))
        if not titulo:
            continue
        resumo = _limpar_html(entry.get("summary", entry.get("description", "")))[:400]
        coletadas.append(
            Noticia(
                titulo=titulo,
                resumo=resumo,
                fonte=fonte,
                url=entry.get("link", ""),
                publicado=publicado,
                categoria=categoria,
            )
        )
    return coletadas


def coletar_todas(limite_horas: int = 24) -> dict[str, list[Noticia]]:
    grupos = {
        "brasil": (FEEDS_BRASIL, PALAVRAS_BRASIL),
        "cripto": (FEEDS_CRIPTO, PALAVRAS_CRIPTO),
        "fii": (FEEDS_FII, PALAVRAS_FII),
    }
    resultado: dict[str, list[Noticia]] = {}

    for categoria, (feeds, palavras) in grupos.items():
        print(f"[*] Coletando categoria {categoria}...")
        candidatas: list[Noticia] = []
        for url in feeds:
            candidatas.extend(_coletar_feed(url, categoria, limite_horas))

        candidatas = _deduplicar(candidatas)
        relevantes = _filtrar_relevancia(candidatas, palavras)
        relevantes.sort(key=lambda n: n.publicado, reverse=True)
        resultado[categoria] = relevantes[:25]
        print(f"  -> {len(relevantes)} relevantes (mostrando top 25)")

    return resultado


def _deduplicar(noticias: Iterable[Noticia]) -> list[Noticia]:
    vistos: set[str] = set()
    unicas: list[Noticia] = []
    for n in noticias:
        chave = re.sub(r"[^\w]+", "", n.titulo.lower())[:80]
        if chave in vistos:
            continue
        vistos.add(chave)
        unicas.append(n)
    return unicas


def _filtrar_relevancia(noticias: list[Noticia], palavras: list[str]) -> list[Noticia]:
    if not palavras:
        return noticias
    resultado = []
    for n in noticias:
        texto = (n.titulo + " " + n.resumo).lower()
        if any(p in texto for p in palavras):
            resultado.append(n)
    if len(resultado) < 7:
        return noticias[:25]
    return resultado


PROMPT_GEMINI = """Você é o editor da newsletter diária de mercado da VA Capital, casa de mentoria de investimentos focada em renda variável (ações + FIIs) no Brasil. Vinicius Peta — Mentor de Investimentos — assina.

Sua tarefa: receber até 25 notícias candidatas de UMA categoria ({categoria}) e selecionar as 7 mais relevantes pro investidor brasileiro de longo prazo, gerando análise estruturada.

CATEGORIA HOJE: {categoria_label}

NOTÍCIAS CANDIDATAS (titulo · resumo · fonte):
{lista}

REGRAS DE SELEÇÃO:
- Priorize impacto prático no bolso do investidor BR (dividendos, balanço, M&A, Selic, IPCA, regulação cripto, distribuição de FII)
- Descarte notícias superficiais, opinativas ou puramente políticas sem reflexo no mercado
- Não repita assunto (escolha 7 manchetes distintas)
- Se faltarem boas opções, sinalize com texto curto explicando por quê

FORMATO DE SAÍDA (JSON estrito, sem markdown, sem ```):
{{
  "resumo_categoria": "1 linha em até 90 caracteres resumindo o dia nessa categoria",
  "noticias": [
    {{
      "emoji": "📈|🏦|💰|⚠️|🪙|🏢|...",
      "titulo": "Título curto e impactante, máx 70 chars",
      "positivo": "1 frase com o lado bom da notícia (✅)",
      "negativo": "1 frase com o risco ou ressalva (❌)",
      "impacto": "1 frase com o impacto prático pro investidor BR (💡)",
      "fonte_titulo_original": "título original recebido — pra rastrear",
      "url": "url original recebida"
    }}
  ]
}}

IMPORTANTE:
- Linguagem direta, sem floreio, sem termos técnicos complexos
- Não use o caractere ~ como aproximação (escreva "DY de 9%" e não "DY ~9%")
- Não invente dados — se a notícia não der base pra positivo/negativo, deixe a frase em tom genérico mas honesto
- Retorne EXATAMENTE 7 itens em "noticias"
- JSON válido, sem aspas curvas, sem trailing commas
"""


def selecionar_e_analisar(noticias: list[Noticia], categoria: str, categoria_label: str) -> dict:
    if not noticias:
        return {"resumo_categoria": f"Sem notícias relevantes em {categoria_label} hoje.", "noticias": []}

    lista = "\n".join(
        f"- [{i+1}] {n.titulo} · {n.resumo[:200]} · {n.fonte} · {n.url}"
        for i, n in enumerate(noticias)
    )
    prompt = PROMPT_GEMINI.format(categoria=categoria, categoria_label=categoria_label, lista=lista)

    model = genai.GenerativeModel(
        "gemini-1.5-flash",
        generation_config={
            "response_mime_type": "application/json",
            "temperature": 0.4,
        },
    )
    resposta = model.generate_content(prompt)
    try:
        return json.loads(resposta.text)
    except json.JSONDecodeError:
        texto = re.sub(r"^```(?:json)?|```$", "", resposta.text.strip(), flags=re.MULTILINE).strip()
        return json.loads(texto)


def montar_bloco(
    brasil: dict,
    cripto: dict,
    fii: dict,
    data_local: datetime,
) -> str:
    dias = ["Segunda-feira", "Terça-feira", "Quarta-feira", "Quinta-feira", "Sexta-feira", "Sábado", "Domingo"]
    dia_str = dias[data_local.weekday()]
    data_str = data_local.strftime("%d/%m/%Y")

    resumo_pontos: list[str] = []
    for categoria_resp in (brasil, cripto, fii):
        resumo = categoria_resp.get("resumo_categoria", "").strip()
        if resumo:
            resumo_pontos.append(f"• {resumo}")
    while len(resumo_pontos) < 7:
        resumo_pontos.append("• Mercado em compasso de espera.")
    resumo_pontos = resumo_pontos[:7]

    linhas: list[str] = []
    linhas.append("*VA CAPITAL* 📊")
    linhas.append(f"*Notícias do Mercado | {dia_str} {data_str}*")
    linhas.append("")
    linhas.append("📋 RESUMO DO DIA")
    linhas.extend(resumo_pontos)
    linhas.append("")

    def _bloco(emoji_categoria: str, titulo_categoria: str, dados: dict, numero_inicial: int) -> list[str]:
        out: list[str] = []
        out.append(f"{emoji_categoria} {titulo_categoria}")
        for i, item in enumerate(dados.get("noticias", []), start=numero_inicial):
            emoji = item.get("emoji", "📰")
            titulo = item.get("titulo", "").strip()
            positivo = item.get("positivo", "").strip()
            negativo = item.get("negativo", "").strip()
            impacto = item.get("impacto", "").strip()
            out.append(f"{emoji} {i}. *{titulo}*")
            if positivo:
                out.append(f"✅ {positivo}")
            if negativo:
                out.append(f"❌ {negativo}")
            if impacto:
                out.append(f"💡 {impacto}")
            out.append("")
        return out

    linhas.extend(_bloco("🇧🇷", "BRASIL", brasil, 1))
    linhas.extend(_bloco("🪙", "CRIPTO", cripto, 8))
    linhas.extend(_bloco("🏢", "FIIs e REITs", fii, 15))

    linhas.append("🏦 *VA Capital — Consultoria de Investimentos*")
    linhas.append("_Vinicius Peta · Mentor de Investimentos_")
    linhas.append("_CPA | C-Pro R | C-Pro I | Pós-graduado em Gestão de Risco (FIA)_")
    return "\n".join(linhas)


def montar_html(corpo_texto: str, data_local: datetime) -> str:
    titulo = f"Newsletter VA Capital — {data_local.strftime('%d/%m/%Y')}"
    corpo_html = (
        corpo_texto
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    corpo_html = re.sub(r"\*([^*\n]+)\*", r"<strong>\1</strong>", corpo_html)
    corpo_html = re.sub(r"_([^_\n]+)_", r"<em>\1</em>", corpo_html)
    corpo_html = corpo_html.replace("\n", "<br>\n")

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="utf-8"><title>{titulo}</title></head>
<body style="font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 640px; margin: 0 auto; padding: 24px; line-height: 1.55; color: #1a1a1a;">
<div style="font-size: 15px;">{corpo_html}</div>
</body>
</html>"""


def enviar_email(corpo_texto: str, html: str, data_local: datetime) -> dict:
    api_key = os.environ["RESEND_API_KEY"]
    destinatarios = [e.strip() for e in os.environ["NEWSLETTER_TO"].split(",") if e.strip()]
    remetente = os.environ["NEWSLETTER_FROM"]
    reply_to = os.environ.get("NEWSLETTER_REPLY_TO")

    payload = {
        "from": remetente,
        "to": destinatarios,
        "subject": f"VA Capital — Notícias {data_local.strftime('%d/%m/%Y')}",
        "html": html,
        "text": corpo_texto,
    }
    if reply_to:
        payload["reply_to"] = reply_to

    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def salvar_backup(corpo_texto: str, data_local: datetime) -> Path:
    destino = Path(__file__).parent / "historico" / f"{data_local.strftime('%Y-%m-%d')}-noticias.md"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(corpo_texto + "\n", encoding="utf-8")
    return destino


def main() -> int:
    chave_gemini = os.environ.get("GEMINI_API_KEY")
    if not chave_gemini:
        print("ERRO: variável GEMINI_API_KEY não definida.", file=sys.stderr)
        return 1
    genai.configure(api_key=chave_gemini)

    print("[1/4] Coletando notícias dos RSS feeds...")
    coletadas = coletar_todas(limite_horas=24)

    print("[2/4] Selecionando e analisando com Gemini Flash...")
    brasil = selecionar_e_analisar(coletadas["brasil"], "brasil", "Brasil")
    cripto = selecionar_e_analisar(coletadas["cripto"], "cripto", "Cripto")
    fii = selecionar_e_analisar(coletadas["fii"], "fii", "FIIs e REITs")

    data_local = datetime.now(SP_TZ)
    print("[3/4] Montando bloco final no padrão VA Capital...")
    corpo_texto = montar_bloco(brasil, cripto, fii, data_local)
    html = montar_html(corpo_texto, data_local)

    backup = salvar_backup(corpo_texto, data_local)
    print(f"  -> backup salvo em {backup}")

    print("[4/4] Enviando email via Resend...")
    if not os.environ.get("RESEND_API_KEY"):
        print("  [DRY-RUN] RESEND_API_KEY não definida, pulando envio.")
        print("--- BLOCO GERADO ---")
        print(corpo_texto)
        return 0

    resultado = enviar_email(corpo_texto, html, data_local)
    print(f"  -> email enviado, id: {resultado.get('id')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
