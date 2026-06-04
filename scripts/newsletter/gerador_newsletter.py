"""
Gerador de Newsletter VA Capital — versão grátis com GitHub Actions + Gemini Flash + Resend.

Pipeline:
  1. Coleta notícias via RSS (Brasil, Cripto, FIIs) das últimas 24h
  2. Pré-filtra por palavras-chave relevantes pro investidor BR
  3. Manda candidatas pro Gemini Flash (grátis) — IA seleciona 7 por categoria e
     gera análise ✅ positivo / ❌ negativo / 💡 impacto prático
  4. Monta o email no FORMATO APROVADO pelo Vini (29/05/2026): header azul-marinho,
     RESUMO DO DIA com 7 bullets, 3 blocos (Brasil/Cripto/FIIs) com 7 notícias cada,
     e 1 CTA por bloco (YouTube, Instagram, Podcast) em <a href> absoluto
  5. Envia via Resend (TO = dono entregável, BCC = lista de clientes — LGPD)
  6. Salva backup .md em scripts/newsletter/historico/

Variáveis de ambiente necessárias (configuradas como secrets no GitHub Actions):
  GEMINI_API_KEY        - API key do Google AI Studio (aistudio.google.com)
  RESEND_API_KEY        - API key do Resend (resend.com)
  NEWSLETTER_FROM       - email remetente verificado no Resend (ex: noticias@vacapital.com.br)
  NEWSLETTER_TO         - lista de destinatários (clientes) separada por vírgula -> vão no BCC
  NEWSLETTER_OWNER      - caixa real entregável do dono -> vai no TO (padrão vacapital01@gmail.com)
  NEWSLETTER_REPLY_TO   - (opcional) email pra reply-to
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import feedparser
import google.generativeai as genai
import requests
from zoneinfo import ZoneInfo


SP_TZ = ZoneInfo("America/Sao_Paulo")

# Links — SEMPRE absolutos https://, sem espaço/quebra dentro do href.
URL_YOUTUBE = "https://youtube.com/@VACapitalinvestimentos"
URL_INSTAGRAM = "https://instagram.com/vacapital_"
URL_PODCAST = "https://open.spotify.com/show/033iOXGH2JY1MasriN5bzA"
URL_CURSO = "https://viniciuspeta.com"

# Bloco de destaque de vídeo do YouTube — DATE-GATED.
# Só aparece no email se a data local (SP) == VIDEO_DESTAQUE["data"].
# Assim some sozinho no dia seguinte e não vaza pros próximos envios.
# Para destacar outro vídeo no futuro, basta atualizar este dict (data + url + textos).
VIDEO_DESTAQUE = {
    "data": "2026-06-05",
    "titulo": "VENDI Usiminas com 150% de LUCRO 🤑",
    "url": "https://youtu.be/qaawPby5VU0",
    "chamada": (
        "Vendi Usiminas com +150% de lucro e mostro ao vivo como girei a carteira "
        "— 3 empresas novas + reforços. Ação ou FII, qual rende mais?"
    ),
}

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
        "gemini-2.5-flash",
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


# ---------------------------------------------------------------------------
# FORMATO APROVADO (Vini, 29/05/2026): header azul-marinho + RESUMO DO DIA +
# 3 blocos (Brasil/Cripto/FIIs) com ✅❌💡 e 1 CTA por bloco em <a href>.
# Espelho de enviar_avulso_2026-05-29.py.
# ---------------------------------------------------------------------------

DIAS_SEMANA = [
    "Segunda-feira", "Terça-feira", "Quarta-feira", "Quinta-feira",
    "Sexta-feira", "Sábado", "Domingo",
]


def _esc(texto: str) -> str:
    """Escapa apenas o necessário pra texto dentro de HTML (preserva emojis)."""
    return (
        (texto or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _noticia_html(emoji: str, n: int, titulo: str, pos: str, neg: str, imp: str) -> str:
    bloco = f"""
      <div style="margin:0 0 18px 0;">
        <p style="margin:0 0 6px 0;font-size:16px;font-weight:700;color:#0b1f33;">{_esc(emoji)} {n}. {_esc(titulo)}</p>"""
    if pos:
        bloco += f'\n        <p style="margin:0 0 3px 0;font-size:15px;color:#1a1a1a;">✅ {_esc(pos)}</p>'
    if neg:
        bloco += f'\n        <p style="margin:0 0 3px 0;font-size:15px;color:#1a1a1a;">❌ {_esc(neg)}</p>'
    if imp:
        bloco += f'\n        <p style="margin:0;font-size:15px;color:#1a1a1a;">💡 {_esc(imp)}</p>'
    bloco += "\n      </div>"
    return bloco


def _cta_html(texto: str, url: str, rotulo: str) -> str:
    return f"""
      <div style="margin:8px 0 26px 0;padding:14px 16px;background:#f4f7fb;border-left:4px solid #1d4ed8;border-radius:6px;">
        <p style="margin:0;font-size:15px;color:#0b1f33;">{texto}
          <a href="{url}" style="color:#1d4ed8;font-weight:700;text-decoration:none;">{rotulo}</a>
        </p>
      </div>"""


def _secao_titulo_html(emoji: str, texto: str) -> str:
    return f"""
      <h2 style="margin:30px 0 16px 0;padding-bottom:8px;font-size:19px;color:#0b1f33;border-bottom:2px solid #1d4ed8;">{emoji} {texto}</h2>"""


def _bloco_noticias_html(dados: dict, numero_inicial: int) -> str:
    out = ""
    for i, item in enumerate(dados.get("noticias", []), start=numero_inicial):
        out += _noticia_html(
            item.get("emoji", "📰"),
            i,
            item.get("titulo", "").strip(),
            item.get("positivo", "").strip(),
            item.get("negativo", "").strip(),
            item.get("impacto", "").strip(),
        )
    return out


def _resumo_bullets(brasil: dict, cripto: dict, fii: dict) -> list[str]:
    """RESUMO DO DIA: prioriza 1 resumo por categoria + completa com manchetes
    selecionadas pela IA até chegar em 7 bullets cobrindo Brasil/Cripto/FII."""
    bullets: list[str] = []
    for cat in (brasil, cripto, fii):
        r = (cat.get("resumo_categoria") or "").strip()
        if r:
            bullets.append(r)

    # Completa com títulos das notícias (intercalando categorias) até 7.
    pilhas = [
        [it.get("titulo", "").strip() for it in brasil.get("noticias", [])],
        [it.get("titulo", "").strip() for it in cripto.get("noticias", [])],
        [it.get("titulo", "").strip() for it in fii.get("noticias", [])],
    ]
    idx = 0
    while len(bullets) < 7 and any(pilhas):
        pilha = pilhas[idx % 3]
        idx += 1
        if pilha:
            t = pilha.pop(0)
            if t and t not in bullets:
                bullets.append(t)
        if not any(pilhas):
            break

    while len(bullets) < 7:
        bullets.append("Mercado em compasso de espera.")
    return bullets[:7]


def _video_destaque_html(data_local: datetime) -> str:
    """Caixa de destaque de vídeo no topo do email. DATE-GATED: só renderiza se a
    data local de hoje bate com VIDEO_DESTAQUE['data'] (formato YYYY-MM-DD). Fora
    desse dia retorna string vazia, então o bloco some sozinho nos próximos envios."""
    if data_local.strftime("%Y-%m-%d") != VIDEO_DESTAQUE.get("data"):
        return ""
    titulo = _esc(VIDEO_DESTAQUE["titulo"])
    chamada = _esc(VIDEO_DESTAQUE["chamada"])
    url = VIDEO_DESTAQUE["url"]
    return f"""
      <div style="background:#fff7ed;border:1px solid #f5b34a;border-left:5px solid #f59e0b;border-radius:8px;padding:18px 20px;margin:0 0 24px 0;">
        <p style="margin:0 0 8px 0;font-size:13px;font-weight:800;color:#b45309;letter-spacing:0.5px;text-transform:uppercase;">🎥 Vídeo novo no canal</p>
        <p style="margin:0 0 6px 0;font-size:17px;font-weight:800;color:#0b1f33;">{titulo}</p>
        <p style="margin:0 0 14px 0;font-size:14px;color:#33414f;line-height:1.5;">{chamada}</p>
        <a href="{url}" style="display:inline-block;background:#cc0000;color:#ffffff;text-decoration:none;font-size:15px;font-weight:700;padding:11px 22px;border-radius:6px;">▶ Assistir agora</a>
      </div>"""


def montar_html(brasil: dict, cripto: dict, fii: dict, data_local: datetime, assunto: str) -> str:
    dia_str = DIAS_SEMANA[data_local.weekday()]
    data_str = data_local.strftime("%d/%m")

    resumo_html = "".join(
        f'<li style="margin:0 0 6px 0;font-size:15px;color:#1a1a1a;">{_esc(b)}</li>'
        for b in _resumo_bullets(brasil, cripto, fii)
    )

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{_esc(assunto)}</title></head>
<body style="margin:0;padding:0;background:#eef1f5;">
  <div style="max-width:640px;margin:0 auto;padding:0;background:#ffffff;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;line-height:1.55;color:#1a1a1a;">

    <div style="background:#0b1f33;padding:24px 28px;">
      <p style="margin:0;font-size:22px;font-weight:800;color:#ffffff;letter-spacing:0.5px;">VA CAPITAL 📊</p>
      <p style="margin:6px 0 0 0;font-size:15px;color:#9fc0e8;">Notícias do Mercado | {dia_str} {data_str}</p>
    </div>

    <div style="padding:24px 28px;">
      {_video_destaque_html(data_local)}
      <div style="background:#f4f7fb;border-radius:8px;padding:16px 20px;margin:0 0 24px 0;">
        <p style="margin:0 0 10px 0;font-size:16px;font-weight:700;color:#0b1f33;">📋 RESUMO DO DIA</p>
        <ul style="margin:0;padding:0 0 0 20px;">{resumo_html}</ul>
      </div>

      {_secao_titulo_html("🇧🇷", "BRASIL")}
      {_bloco_noticias_html(brasil, 1)}
      {_cta_html("Análises diárias do mercado em vídeo no nosso canal:", URL_YOUTUBE, "YouTube VA Capital ▶")}

      {_secao_titulo_html("🪙", "CRIPTO")}
      {_bloco_noticias_html(cripto, 8)}
      {_cta_html("Conteúdo rápido e visual todo dia no nosso Instagram:", URL_INSTAGRAM, "@vacapital_ no Instagram")}

      {_secao_titulo_html("🏢", "FIIs E REITs")}
      {_bloco_noticias_html(fii, 15)}
      {_cta_html("Ouça o resumo do mercado em áudio no nosso podcast:", URL_PODCAST, "VA Capital — Mercado diário 🎧")}

      <div style="margin:30px 0 0 0;padding:20px;background:#0b1f33;border-radius:8px;">
        <p style="margin:0 0 6px 0;font-size:16px;font-weight:700;color:#ffffff;">🏦 VA Capital — Mentoria de Investimentos</p>
        <p style="margin:0;font-size:14px;color:#cdddf0;">Vinicius Peta — Mentor de Investimentos</p>
        <p style="margin:2px 0 0 0;font-size:13px;color:#9fc0e8;">CPA | C-Pro R | C-Pro I | Pós-graduado em Gestão de Risco (FIA)</p>
        <p style="margin:14px 0 0 0;font-size:13px;color:#cdddf0;">🎓 Curso completo: <a href="{URL_CURSO}" style="color:#9fc0e8;text-decoration:none;">viniciuspeta.com</a></p>
        <p style="margin:2px 0 0 0;font-size:13px;color:#cdddf0;">✉️ Contato: vacapital01@gmail.com</p>
      </div>

      <p style="margin:18px 0 0 0;font-size:12px;color:#8a98a8;line-height:1.5;">
        Este conteúdo é informativo e educacional e não constitui recomendação de compra ou venda de qualquer ativo.
        Rentabilidade passada não garante resultados futuros. Faça sua própria análise antes de investir.
      </p>

    </div>
  </div>
</body>
</html>"""


def montar_texto(brasil: dict, cripto: dict, fii: dict, data_local: datetime) -> str:
    """Versão texto puro (fallback de clientes que não renderizam HTML) e base do backup .md."""
    dia_str = DIAS_SEMANA[data_local.weekday()]
    data_str = data_local.strftime("%d/%m/%Y")

    linhas: list[str] = []
    linhas.append("VA CAPITAL")
    linhas.append(f"Notícias do Mercado | {dia_str} {data_str}")
    linhas.append("")
    linhas.append("RESUMO DO DIA")
    for b in _resumo_bullets(brasil, cripto, fii):
        linhas.append(f"- {b}")
    linhas.append("")

    def _bloco(titulo_categoria: str, dados: dict, numero_inicial: int) -> None:
        linhas.append(titulo_categoria)
        for i, item in enumerate(dados.get("noticias", []), start=numero_inicial):
            linhas.append(f"{item.get('emoji', '')} {i}. {item.get('titulo', '').strip()}")
            if item.get("positivo"):
                linhas.append(f"   + {item['positivo'].strip()}")
            if item.get("negativo"):
                linhas.append(f"   - {item['negativo'].strip()}")
            if item.get("impacto"):
                linhas.append(f"   > {item['impacto'].strip()}")
            linhas.append("")

    _bloco("BRASIL", brasil, 1)
    linhas.append(f"YouTube VA Capital: {URL_YOUTUBE}")
    linhas.append("")
    _bloco("CRIPTO", cripto, 8)
    linhas.append(f"Instagram: {URL_INSTAGRAM}")
    linhas.append("")
    _bloco("FIIs E REITs", fii, 15)
    linhas.append(f"Podcast VA Capital — Mercado diário: {URL_PODCAST}")
    linhas.append("")
    linhas.append("VA Capital — Mentoria de Investimentos")
    linhas.append("Vinicius Peta — Mentor de Investimentos")
    linhas.append("CPA | C-Pro R | C-Pro I | Pós-graduado em Gestão de Risco (FIA)")
    linhas.append(f"Curso completo: {URL_CURSO}")
    linhas.append("Contato: vacapital01@gmail.com")
    linhas.append("")
    linhas.append("Conteúdo informativo/educacional, não é recomendação de compra/venda.")
    return "\n".join(linhas)


def enviar_email(html: str, texto: str, data_local: datetime) -> dict:
    api_key = os.environ["RESEND_API_KEY"].strip()
    destinatarios = [e.strip() for e in os.environ["NEWSLETTER_TO"].split(",") if e.strip()]
    remetente = os.environ["NEWSLETTER_FROM"].strip()
    reply_to = (os.environ.get("NEWSLETTER_REPLY_TO") or "").strip() or None

    dia_str = DIAS_SEMANA[data_local.weekday()]
    assunto = f"Notícias do Mercado | {dia_str} {data_local.strftime('%d/%m')}"

    # Privacidade (LGPD): TO = caixa real própria (Vini); BCC = lista de clientes.
    # Assim nenhum cliente vê o email dos outros.
    # OBS: o TO precisa ser uma CAIXA DE ENTRADA REAL e entregável — usar um
    # endereço apenas-remetente (ex.: noticias@vacapital.com.br) faz o envio
    # quicar e pode comprometer a entrega do BCC.
    to_proprio = (os.environ.get("NEWSLETTER_OWNER") or "vacapital01@gmail.com").strip()
    # Garante que o dono não fique duplicado no BCC.
    destinatarios = [e for e in destinatarios if e.lower() != to_proprio.lower()]

    payload = {
        "from": remetente,
        "to": [to_proprio],
        "bcc": destinatarios,
        "subject": assunto,
        "html": html,
        "text": texto,
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
    print(f"TO: {to_proprio} | BCC: {len(destinatarios)} clientes")
    return response.json()


def salvar_backup(texto: str, data_local: datetime) -> Path:
    destino = Path(__file__).parent / "historico" / f"{data_local.strftime('%Y-%m-%d')}-noticias.md"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(texto + "\n", encoding="utf-8")
    return destino


def main() -> int:
    # Trava de idempotência: se a newsletter de hoje já foi gerada/enviada
    # (backup .md já existe), não envia de novo. Protege contra o agendamento
    # atrasado do GitHub Actions (que pode disparar horas depois do horário do
    # cron) e contra envio manual + agendado no mesmo dia, evitando duplicar
    # email pros clientes. Use NEWSLETTER_FORCE=1 pra forçar reenvio.
    data_hoje = datetime.now(SP_TZ)
    backup_hoje = Path(__file__).parent / "historico" / f"{data_hoje.strftime('%Y-%m-%d')}-noticias.md"
    if backup_hoje.exists() and os.environ.get("NEWSLETTER_FORCE") != "1":
        print(f"Newsletter de {data_hoje.strftime('%Y-%m-%d')} já foi enviada "
              f"(backup existe em {backup_hoje}). Pulando para não duplicar. "
              f"Use NEWSLETTER_FORCE=1 para forçar reenvio.")
        return 0

    chave_gemini = os.environ.get("GEMINI_API_KEY")
    if not chave_gemini:
        print("ERRO: variável GEMINI_API_KEY não definida.", file=sys.stderr)
        return 1
    genai.configure(api_key=chave_gemini)

    print("[1/4] Coletando notícias dos RSS feeds...")
    coletadas = coletar_todas(limite_horas=24)

    print("[2/4] Selecionando e analisando com Gemini Flash...")
    # Free tier do Gemini permite poucas req/min — sleep entre chamadas evita 429 ResourceExhausted.
    brasil = selecionar_e_analisar(coletadas["brasil"], "brasil", "Brasil")
    time.sleep(15)
    cripto = selecionar_e_analisar(coletadas["cripto"], "cripto", "Cripto")
    time.sleep(15)
    fii = selecionar_e_analisar(coletadas["fii"], "fii", "FIIs e REITs")

    data_local = datetime.now(SP_TZ)
    dia_str = DIAS_SEMANA[data_local.weekday()]
    assunto = f"Notícias do Mercado | {dia_str} {data_local.strftime('%d/%m')}"

    print("[3/4] Montando email no formato aprovado (HTML azul-marinho)...")
    html = montar_html(brasil, cripto, fii, data_local, assunto)
    texto = montar_texto(brasil, cripto, fii, data_local)

    n_total = sum(len(c.get("noticias", [])) for c in (brasil, cripto, fii))
    print(f"  -> {n_total} notícias no total (Brasil {len(brasil.get('noticias', []))} / "
          f"Cripto {len(cripto.get('noticias', []))} / FIIs {len(fii.get('noticias', []))})")

    backup = salvar_backup(texto, data_local)
    print(f"  -> backup salvo em {backup}")

    # Preview do HTML também salvo (útil pra inspeção manual / dry-run).
    preview = Path(__file__).parent / "historico" / f"{data_local.strftime('%Y-%m-%d')}-preview.html"
    preview.write_text(html, encoding="utf-8")
    print(f"  -> preview HTML salvo em {preview}")

    print("[4/4] Enviando email via Resend...")
    if not os.environ.get("RESEND_API_KEY"):
        print("  [DRY-RUN] RESEND_API_KEY não definida, pulando envio.")
        return 0

    resultado = enviar_email(html, texto, data_local)
    print(f"  -> email enviado, id: {resultado.get('id')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
