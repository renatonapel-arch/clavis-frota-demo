# Deploy do Cadastro Veicular → `frota.demos.napel.com.br` (Coolify)

Guia passo-a-passo pra subir essa demo na VPS Napel via Coolify.
Tempo estimado: **~1 hora** (a maior parte é UI clica-clica).

---

## Pré-requisitos

- Acesso ao painel Coolify
- `ANTHROPIC_API_KEY` válida (já está em `~/.claude/.env`)
- Repo GitHub criado (próximo passo)

---

## 1. Repositório GitHub (5 min)

A demo está em `docs/modulos/cadastro-veicular/demo-local/`. Pra Coolify funcionar bem, sobe num repo dedicado:

```bash
# Da raiz do clavis
cp -r docs/modulos/cadastro-veicular/demo-local /tmp/clavis-frota-demo
cd /tmp/clavis-frota-demo
git init && git add -A && git commit -m "initial: cadastro veicular demo"
gh repo create renatonapel-arch/clavis-frota-demo --private --source=. --push
```

Alternativa: cria via UI do GitHub, depois `git remote add origin ... && git push`.

> ⚠ Vai ter `demo.sqlite3` e `uploads/` no `.dockerignore` — não preocupa.

---

## 2. Coolify — criar a aplicação (10 min)

No painel Coolify:

1. **+ New** → **Application**
2. **Source:** GitHub (autorizar se primeira vez)
3. **Repository:** `renatonapel-arch/clavis-frota-demo`
4. **Branch:** `main`
5. **Build Pack:** **Dockerfile** (auto-detecta o `Dockerfile` na raiz)
6. **Port (Container):** `8761`
7. **Name:** `clavis-frota-demo`
8. **Save** (não faz deploy ainda)

---

## 3. Coolify — Environment Variables (5 min)

Aba **Environment Variables**, adiciona:

| Nome | Valor | Marca como Secret? |
|---|---|---|
| `ANTHROPIC_API_KEY` | (cole do `~/.claude/.env`) | ✅ Sim |
| `DEMO_PIN` | `1234` (ou um PIN seu) | ✅ Sim |
| `USE_REAL_VISION` | `true` | ❌ |
| `VISION_MODEL` | `claude-haiku-4-5` | ❌ |

> `HOST`, `PORT`, `DATA_DIR` já vêm no Dockerfile — não precisa setar.

---

## 4. Coolify — Persistent Storage (5 min)

Aba **Storages** → **+ Add**:

- **Name:** `frota-data`
- **Source Path (host):** deixar vazio (Coolify cria volume nomeado)
- **Destination Path (container):** `/data`

Isso preserva `demo.sqlite3` e `uploads/` entre deploys. **Sem isso, cada deploy zera tudo.**

---

## 5. Coolify — Domínio (5 min)

Aba **Domains** → **+ Add Domain**:

- **Domain:** `frota.demos.napel.com.br`
- **HTTPS:** ✅ ativo (Coolify gera cert Let's Encrypt automático via Traefik)
- **WWW Redirect:** desligado

> DNS `*.demos.napel.com.br` já é wildcard apontando pra VPS — não precisa pedir nada pro Cesar.
> Se der erro de cert, espera 1–2 min e tenta de novo (Let's Encrypt rate limit).

---

## 6. Deploy (5 min + tempo de build)

Botão **Deploy** no canto superior direito.

Acompanhar os logs:
- `Building Dockerfile…` (~2 min)
- `Pushing image…`
- `Starting container…`
- Healthcheck verde

Quando aparecer **"Application is running"**, testa:

```bash
curl https://frota.demos.napel.com.br/api/patrimonio/health
# {"status":"ok","checks":{"sqlite":"ok","fixtures":"ok","claude_vision":"real","vision_model":"claude-haiku-4-5"}}
```

---

## 7. Smoke test ponta-a-ponta (5 min)

```bash
# 1. Health
curl https://frota.demos.napel.com.br/api/patrimonio/health

# 2. Login
curl -X POST https://frota.demos.napel.com.br/api/auth/login \
  -H "Content-Type: application/json" -d '{"pin":"1234"}'

# 3. Listar veículos (vazio até cadastrar)
curl https://frota.demos.napel.com.br/api/v1/fleet/vehicles \
  -H "Authorization: Bearer 1234"

# 4. Abrir no navegador
open https://frota.demos.napel.com.br
```

Cadastra 1 veículo pela UI pra confirmar Vision real + persistência.

---

## 8. Atualizar oil-change

Manda recado pra outra sessão:

> Frota está em `https://frota.demos.napel.com.br`. Mesma API,
> mesmo PIN demo `1234`. Pode trocar `http://127.0.0.1:8761` por essa URL
> no seu cliente.

---

## Operação contínua

### Push pra `main` → auto-deploy

Coolify detecta push e redeploya automático. Dados em `/data` ficam preservados.

### Logs

Painel Coolify → aba **Logs** (stdout do uvicorn + magic).
Filtrar por `[vision]` pra ver chamadas reais ao Claude.

### Reset do banco (cuidado — destrutivo)

Painel Coolify → aba **Storages** → **Delete** no volume `frota-data` → próximo deploy cria limpo.

### Custo Vision

Cada extração com Haiku 4.5 = ~$0,004. 100 CRLVs/mês = ~$0,40. Logs mostram `cache_read/cache_write` do prompt caching (sistema cacheado).

---

## Troubleshooting

| Sintoma | Causa provável | Fix |
|---|---|---|
| Healthcheck falha no deploy | `ANTHROPIC_API_KEY` errada → backend até sobe, health OK; mas se libmagic faltar, dá ImportError | Conferir variável + ver logs de build |
| `503` no SW offline | Esperado quando rede cai — fallback do service worker | Normal |
| CRLV não abre no navegador | Browser bloqueando `data:` URL ou popup | Permitir popups no domínio |
| `demo.sqlite3` sumiu após deploy | Volume `/data` não foi montado | Voltar no passo 4 |
| Cert HTTPS pendente | Let's Encrypt rate limit ou DNS não propagado | Esperar 5 min + retry |

---

## Quando promover pra `clavis.napel.com.br` (prod real)

Esse deploy é demo. Pra produção real precisa dos 5 gates da auditoria:

1. JWT real (não PIN compartilhado) — `# TODO[ADR-auth-definitivo]`
2. PostgreSQL no Clavis shared DB (não SQLite)
3. Sync SIGE real das filiais — `# TODO[ADR-sync-SIGE-filiais]`
4. S3/Minio pros uploads (não volume local) — `# TODO[ADR-storage-uploads]`
5. RBAC formalizado com os 6 roles Clavis — `# TODO[ADR-rbac-matrix-clavis]`

Esses são bloqueios humanos (Renato + Cesar + Hudson).
