# Clavis · Cadastro Veicular (CRLV-Vision)

Demo do módulo `patrimonio/veiculos`: cadastro de veículos com OCR de CRLV via Claude Vision (Haiku 4.5).

## Quick start

```bash
# 1. Configure
echo "ANTHROPIC_API_KEY=sk-..." > .env

# 2. Suba
docker compose up --build

# 3. Acesse
open http://127.0.0.1:8761
# PIN: 1234
```

## Deploy

Ver `DEPLOY.md` — passo-a-passo Coolify pra `frota.demos.napel.com.br`.

## Contrato API (oil-change)

```
GET    /api/v1/fleet/vehicles?user_id=&filial_id=&ativo=
GET    /api/v1/fleet/vehicles/{id}
GET    /api/v1/fleet/vehicles/{id}/documents
PATCH  /api/v1/fleet/vehicles/{id}                  body parcial dos 6 campos manutenção
```

Auth: `Authorization: Bearer <PIN>` (PIN demo = `1234`).
