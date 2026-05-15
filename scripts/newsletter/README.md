# Newsletter Diária VA Capital — Setup

Pipeline 100% grátis que roda no GitHub Actions toda **Seg-Sex às 07h00 (America/Sao_Paulo)**.

**Como funciona:**
1. Coleta notícias via RSS (InfoMoney, Money Times, Brazil Journal, Valor, CoinDesk, Cointelegraph, Funds Explorer, Clube FII, Suno)
2. Pré-filtra por palavras-chave relevantes pro investidor BR
3. Manda candidatas pro **Gemini Flash** (grátis até 1.500 req/dia)
4. Gemini seleciona 7 por categoria e gera análise ✅❌💡
5. Monta no padrão VA Capital validado
6. Envia via **Resend** (grátis até 3.000 emails/mês)
7. Commita backup `.md` no repo

**Custo:** R$ 0. Tudo no free tier.

---

## Passo 1 — Criar conta no Google AI Studio (Gemini)

1. Vai em https://aistudio.google.com (login com Gmail)
2. Clica em **"Get API key"** no canto superior esquerdo
3. Clica em **"Create API key"** → "Create API key in new project"
4. Copia a chave (começa com `AIzaSy...`)

**Limite gratuito:** 1.500 requests/dia no Gemini 2.0 Flash. A newsletter usa **3 requests/dia** (uma por categoria). Sobra muito.

---

## Passo 2 — Criar conta no Resend (email transacional)

1. Vai em https://resend.com → "Sign up" (login com Gmail/GitHub)
2. Vai em **API Keys** → "Create API Key" → escopo "Sending access" → copia a chave (começa com `re_...`)
3. Adiciona um domínio em **Domains** → "Add Domain"
   - Se já tem `vacapital.com.br`: adiciona e configura os registros DNS (SPF, DKIM) que o Resend te mostra
   - Se NÃO tem domínio próprio: usa o sandbox `onboarding@resend.dev` (limite menor — só pro seu email cadastrado)
4. Anota o email remetente verificado (ex: `noticias@vacapital.com.br`)

**Limite gratuito:** 3.000 emails/mês + 100 emails/dia. Pra 22 dias úteis x N destinatários cabe tranquilo.

---

## Passo 3 — Configurar secrets no GitHub

1. Vai em https://github.com/vacapital01-droid/Newsletter-di-ria-VA-Capital/settings/secrets/actions
2. Clica em **"New repository secret"** e adiciona um por vez:

| Nome do secret | Valor |
|----------------|-------|
| `GEMINI_API_KEY` | Chave do Google AI Studio (Passo 1) |
| `RESEND_API_KEY` | Chave do Resend (Passo 2) |
| `NEWSLETTER_FROM` | Email remetente verificado (ex: `noticias@vacapital.com.br`) |
| `NEWSLETTER_TO` | Lista de destinatários separada por vírgula (ex: `vacapital01@gmail.com,cliente1@gmail.com,cliente2@gmail.com`) |
| `NEWSLETTER_REPLY_TO` | (opcional) Email pra resposta (ex: `vinicius@vacapital.com.br`) |

---

## Passo 4 — Testar manual antes de deixar agendado

1. Vai em https://github.com/vacapital01-droid/Newsletter-di-ria-VA-Capital/actions
2. Clica no workflow **"Newsletter Diária VA Capital"** (lateral esquerda)
3. Clica no botão **"Run workflow"** (direita) → "Run workflow"
4. Acompanha a execução clicando no job rodando

Se rodar sem erro, vai aparecer **"email enviado, id: ..."** no log do step "Gerar e enviar newsletter".
Se quebrar, o log mostra exatamente onde.

---

## Passo 5 — Deixar rolar sozinho

Depois do teste manual, o cron já está armado:
- **Toda Seg-Sex às 07h00 (horário de Brasília)**
- Sem horário de verão na agenda — converti pra `0 10 * * 1-5` em UTC

Pra ativar ou pausar:
- **Pausar:** rename do arquivo `.github/workflows/newsletter-daily.yml.off` ou comenta o bloco `schedule`
- **Reativar:** desfaz a mudança

---

## Rodar localmente (debug — opcional)

```bash
git clone https://github.com/vacapital01-droid/Newsletter-di-ria-VA-Capital.git
cd Newsletter-di-ria-VA-Capital
python -m venv venv-newsletter
source venv-newsletter/bin/activate
pip install -r scripts/newsletter/requirements.txt

export GEMINI_API_KEY="..."
# DRY-RUN: sem RESEND_API_KEY, só imprime o bloco gerado
python scripts/newsletter/gerador_newsletter.py
```

Se quiser testar envio real local, exporta também `RESEND_API_KEY`, `NEWSLETTER_FROM`, `NEWSLETTER_TO`.

---

## Customizar

| O que mudar | Onde |
|-------------|------|
| Adicionar/remover RSS feeds | `gerador_newsletter.py` → constantes `FEEDS_BRASIL`, `FEEDS_CRIPTO`, `FEEDS_FII` |
| Mudar palavras-chave de relevância | mesmo arquivo → `PALAVRAS_BRASIL`, `PALAVRAS_CRIPTO`, `PALAVRAS_FII` |
| Mudar o tom da análise IA | `PROMPT_GEMINI` no script |
| Mudar horário ou dias | `.github/workflows/newsletter-daily.yml` → linha `cron:` |
| Mudar destinatários | secret `NEWSLETTER_TO` no GitHub |

---

## Limitações conhecidas

- **Sem horário de verão automático**: se voltar HV no Brasil, ajusta o cron pra `0 11 * * 1-5`
- **Free tier do Resend exige domínio verificado** pra mandar pra terceiros — se ainda não tem domínio, o sandbox `onboarding@resend.dev` só manda pro email cadastrado na conta
- **Gemini Flash pode ser conservador** em análises de notícias polêmicas — ajusta o `PROMPT_GEMINI` se quiser tom mais direto
- **RSS pode falhar pontualmente** (servidor fora, rate limit) — o script ignora feeds quebrados e segue com o resto
