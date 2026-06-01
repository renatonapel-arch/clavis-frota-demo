"""
Demo local — Cadastro Veicular (CRLV-Vision)
Backend FastAPI single-file com SQLite e mock do Claude Vision.

Rodar:
    python -m pip install -r requirements.txt
    python backend.py

Trocaveis para a fase staging:
    TODO[ADR-sync-SIGE-filiais]     -> sync incremental SIGE
    TODO[ADR-auth-definitivo]       -> JWT + Redis blacklist + 6 roles RBAC
    TODO[ADR-claude-vision-integracao] -> chamada real Anthropic Vision
    TODO[ADR-storage-uploads]       -> volume Coolify / S3
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Timezones — armazenamento em UTC, exibição em BRT (UTC-3)
TZ_UTC = timezone.utc
TZ_BRT = timezone(timedelta(hours=-3), name="BRT")

import uvicorn
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Vision real (Claude API) — opcional, ativa quando ANTHROPIC_API_KEY existe
try:
    from dotenv import load_dotenv
    # carrega ~/.claude/.env primeiro
    _home_env = Path.home() / ".claude" / ".env"
    if _home_env.exists():
        load_dotenv(_home_env, override=True, encoding="utf-8")
    load_dotenv(override=False)  # .env local se houver, sem sobrescrever
except ImportError:
    pass

# Migrado 2026-06: Anthropic Claude Vision -> Gemini Flash 2.5
# (PDF e imagem ambos suportados; Gemini converte PDF internamente)
try:
    from google import genai
    from google.genai import types as genai_types
    _gemini_available = True
except ImportError:
    _gemini_available = False

# Magic bytes validation (5. da auditoria — bloqueia exe disfarçado de pdf/jpg)
try:
    import magic  # python-magic-bin no Windows
    _magic_available = True
except ImportError:
    _magic_available = False

# ---------- config ----------
BASE_DIR = Path(__file__).parent

# Diretório de dados persistentes (volume `/data` no container Coolify).
# Local: defaults pra BASE_DIR; container: setar DATA_DIR=/data.
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "demo.sqlite3")))
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", str(DATA_DIR / "uploads")))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Fixtures + seeds são código (ficam no container junto com backend.py)
FIXTURES_PATH = BASE_DIR / "fixtures" / "claude_vision_responses.json"

# Bind do servidor — local 127.0.0.1, container 0.0.0.0
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8761"))

DEMO_PIN = os.getenv("DEMO_PIN", "1234")
DEMO_USER_ID = 1
DEMO_USER_NOME = "Renato Napel"
DEMO_USER_FILIAL = 100
DEMO_USER_ROLES = {"patrimonio:cadastrar", "patrimonio:listar", "patrimonio:reatribuir", "patrimonio:admin"}

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10MB
ALLOWED_MIME = {"application/pdf", "image/jpeg", "image/jpg", "image/png"}

# Vision toggle — Gemini (migrado 2026-06)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", "")
USE_REAL_VISION = bool(GEMINI_API_KEY) and _gemini_available and \
    os.getenv("USE_REAL_VISION", "true").lower() == "true"
VISION_MODEL = os.getenv("VISION_MODEL", "gemini-2.5-flash")
_vision_client = genai.Client(api_key=GEMINI_API_KEY) if USE_REAL_VISION else None

# Alertas automáticos de vencimento via WhatsApp (Evolution API)
EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL", "").rstrip("/")
EVOLUTION_API_TOKEN = os.getenv("EVOLUTION_API_TOKEN", "")
EVOLUTION_INSTANCE = os.getenv("EVOLUTION_INSTANCE", "")
ALERTA_WHATSAPP = os.getenv("ALERTA_WHATSAPP") or os.getenv("RENATO_WHATSAPP", "")
ENABLE_ALERTS = os.getenv("ENABLE_ALERTS", "false").lower() == "true"
ALERTA_HORA = int(os.getenv("ALERTA_HORA", "8"))    # hora BRT do envio diário
ALERTA_DIAS = int(os.getenv("ALERTA_DIAS", "30"))   # antecedência do aviso

PLACA_MERCOSUL_RE = re.compile(r"^[A-Z]{3}[0-9][A-Z][0-9]{2}$|^[A-Z]{3}[0-9][A-Z][0-9]{3}$")
PLACA_ANTIGA_RE = re.compile(r"^[A-Z]{3}[0-9]{4}$")
RENAVAM_RE = re.compile(r"^[0-9]{11}$")
CHASSI_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

# ---------- categorias de bem (modelo híbrido) ----------
# Núcleo compartilhado = tabela `bens`. Extensão por tipo:
#   - 'veiculo'  -> tabela `veiculos` (ÚNICA categoria que aparece no contrato /fleet
#                   do oil-change; a empilhadeira fica aqui, com exige_crlv=0).
#   - demais     -> campos próprios em bens.atributos (JSON) + tabelas auxiliares
#                   (imóvel: manutencao_predial). NUNCA entram em `veiculos`/`/fleet`.
CATEGORIAS = ("veiculo", "maquina", "informatica", "moveis", "imovel")
CATEGORIAS_NAO_VEICULO = ("maquina", "informatica", "moveis", "imovel")
CAT_LABEL = {"veiculo": "Veículo", "maquina": "Máquina/Equipamento",
             "informatica": "Informática/TI", "moveis": "Móveis e utensílios", "imovel": "Imóvel"}
CAT_PREFIXO = {"veiculo": "PAT-VEI", "maquina": "PAT-MAQ", "informatica": "PAT-TI",
               "moveis": "PAT-MOV", "imovel": "PAT-IMO"}
# Vida útil contábil padrão (meses) por categoria — editável por bem.
# Imóvel ALUGADO não deprecia (vida_util fica NULL, tratado no cadastro).
VIDA_UTIL_PADRAO = {"veiculo": 60, "maquina": 120, "informatica": 60, "moveis": 120, "imovel": 300}

# Idempotency cache em memoria (em prod -> Redis)
IDEMPOTENCY_CACHE: dict[str, tuple[float, dict]] = {}
IDEMPOTENCY_TTL = 86400  # 24h


# ---------- DB ----------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = db()
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS bens (
          cod_interno          TEXT PRIMARY KEY,
          codigo_patrimonial   TEXT UNIQUE,
          filial               INTEGER NOT NULL,
          centro_custo         TEXT NOT NULL,
          responsavel_user_id  INTEGER NOT NULL,
          responsavel_nome     TEXT NOT NULL,
          data_aquisicao       TEXT,
          valor_aquisicao      REAL,
          fornecedor           TEXT,
          nf_numero            TEXT,
          nf_chave             TEXT,
          valor_residual       REAL DEFAULT 0,
          vida_util_meses      INTEGER,
          metodo_depreciacao   TEXT DEFAULT 'linear',
          estado_operacional   TEXT DEFAULT 'disponivel'
                               CHECK (estado_operacional IN ('disponivel','em_uso','em_manutencao','quebrado','a_venda','baixado')),
          tipo_bem             TEXT NOT NULL DEFAULT 'veiculo'
                               CHECK (tipo_bem IN ('veiculo','equipamento')),
          status               TEXT NOT NULL DEFAULT 'ativo'
                               CHECK (status IN ('ativo','manutencao','vencido','baixado')),
          created_at           TEXT NOT NULL,
          updated_at           TEXT NOT NULL,
          deleted_at           TEXT
        );

        -- histórico imutável de movimentações patrimoniais
        CREATE TABLE IF NOT EXISTS movimentacoes (
          id                 TEXT PRIMARY KEY,
          bem_id             TEXT NOT NULL REFERENCES bens(cod_interno) ON DELETE CASCADE,
          tipo               TEXT NOT NULL,  -- aquisicao, transferencia, troca_responsavel, mudanca_estado, baixa, venda, reativacao
          de_filial          INTEGER,
          para_filial        INTEGER,
          de_responsavel     INTEGER,
          para_responsavel   INTEGER,
          de_estado          TEXT,
          para_estado        TEXT,
          motivo             TEXT,
          valor              REAL,
          comprador          TEXT,
          resultado_contabil REAL,
          data_evento        TEXT,
          executado_por      INTEGER,
          executado_por_nome TEXT,
          created_at         TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_mov_bem ON movimentacoes(bem_id, created_at DESC);

        -- placa/renavam/chassi/vencimento_crlv são nullable: equipamentos
        -- (empilhadeira etc.) não têm CRLV. UNIQUE permite múltiplos NULL no SQLite.
        CREATE TABLE IF NOT EXISTS veiculos (
          bem_id               TEXT PRIMARY KEY REFERENCES bens(cod_interno) ON DELETE CASCADE,
          placa                TEXT,
          renavam              TEXT UNIQUE,
          chassi               TEXT UNIQUE,
          marca                TEXT,
          modelo               TEXT,
          ano_fabricacao       INTEGER,
          ano_modelo           INTEGER,
          cor                  TEXT,
          combustivel          TEXT,
          cilindradas          INTEGER,
          km_atual             INTEGER DEFAULT 0,
          vencimento_crlv      TEXT,
          dados_raw            TEXT NOT NULL,
          dados_origem         TEXT NOT NULL,
          intervalo_km         INTEGER,
          intervalo_meses      INTEGER,
          km_ultima_troca      INTEGER,
          data_ultima_troca    TEXT,
          margem_km_aviso      INTEGER,
          margem_dias_aviso    INTEGER,
          created_at           TEXT NOT NULL,
          updated_at           TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_veiculos_placa ON veiculos(placa);
        CREATE INDEX IF NOT EXISTS idx_veiculos_vencimento ON veiculos(vencimento_crlv);
        CREATE INDEX IF NOT EXISTS idx_bens_filial_status ON bens(filial, status);

        CREATE TABLE IF NOT EXISTS usuarios (
          id          INTEGER PRIMARY KEY AUTOINCREMENT,
          nome        TEXT NOT NULL,
          pin         TEXT NOT NULL UNIQUE,
          filial      INTEGER,
          roles       TEXT NOT NULL,
          ativo       INTEGER NOT NULL DEFAULT 1,
          created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS documentos (
          id                TEXT PRIMARY KEY,
          bem_id            TEXT NOT NULL REFERENCES bens(cod_interno) ON DELETE CASCADE,
          tipo              TEXT NOT NULL,  -- crlv, ipva, seguro, vistoria, foto, outro
          upload_id         TEXT,
          filename_original TEXT,
          vencimento        TEXT,
          observacao        TEXT,
          criado_por        INTEGER,
          criado_por_nome   TEXT,
          criado_em         TEXT NOT NULL,
          ativo             INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_doc_bem ON documentos(bem_id, tipo, ativo);

        -- Manutenção predial leve (categoria 'imovel'). Elétrica, hidráulica,
        -- ar-condicionado, dedetização, alvará/bombeiros. `proximo_vencimento`
        -- alimenta a agenda de avisos (dedetização/alvará renovam periodicamente).
        -- TODO[#1187]: converge com o módulo de Manutenção Veicular (fusão adiada
        -- por decisão do Renato — por ora a manutenção predial vive aqui).
        CREATE TABLE IF NOT EXISTS manutencao_predial (
          id                 TEXT PRIMARY KEY,
          bem_id             TEXT NOT NULL REFERENCES bens(cod_interno) ON DELETE CASCADE,
          tipo               TEXT NOT NULL,  -- eletrica, hidraulica, ar_condicionado, dedetizacao, alvara_bombeiros, outro
          descricao          TEXT,
          fornecedor         TEXT,
          custo              REAL,
          data_servico       TEXT,
          proximo_vencimento TEXT,
          observacao         TEXT,
          criado_por         INTEGER,
          criado_por_nome    TEXT,
          criado_em          TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_manut_predial_bem ON manutencao_predial(bem_id, criado_em DESC);
    """)

    # migration idempotente — adiciona colunas novas em DBs já existentes
    cur.execute("PRAGMA table_info(veiculos)")
    vinfo = {row["name"]: row for row in cur.fetchall()}
    cols = set(vinfo.keys())
    migrations = [
        ("intervalo_km",      "ALTER TABLE veiculos ADD COLUMN intervalo_km INTEGER"),
        ("intervalo_meses",   "ALTER TABLE veiculos ADD COLUMN intervalo_meses INTEGER"),
        ("km_ultima_troca",   "ALTER TABLE veiculos ADD COLUMN km_ultima_troca INTEGER"),
        ("data_ultima_troca", "ALTER TABLE veiculos ADD COLUMN data_ultima_troca TEXT"),
        ("margem_km_aviso",   "ALTER TABLE veiculos ADD COLUMN margem_km_aviso INTEGER"),
        ("margem_dias_aviso", "ALTER TABLE veiculos ADD COLUMN margem_dias_aviso INTEGER"),
    ]
    for col, sql in migrations:
        if col not in cols:
            cur.execute(sql)

    # migration: colunas novas em bens (tipo_bem + bloco patrimonial)
    cur.execute("PRAGMA table_info(bens)")
    bcols = {r["name"] for r in cur.fetchall()}
    bens_migrations = [
        ("tipo_bem",            "ALTER TABLE bens ADD COLUMN tipo_bem TEXT NOT NULL DEFAULT 'veiculo'"),
        ("codigo_patrimonial",  "ALTER TABLE bens ADD COLUMN codigo_patrimonial TEXT"),
        ("valor_aquisicao",     "ALTER TABLE bens ADD COLUMN valor_aquisicao REAL"),
        ("fornecedor",          "ALTER TABLE bens ADD COLUMN fornecedor TEXT"),
        ("nf_numero",           "ALTER TABLE bens ADD COLUMN nf_numero TEXT"),
        ("nf_chave",            "ALTER TABLE bens ADD COLUMN nf_chave TEXT"),
        ("valor_residual",      "ALTER TABLE bens ADD COLUMN valor_residual REAL DEFAULT 0"),
        ("vida_util_meses",     "ALTER TABLE bens ADD COLUMN vida_util_meses INTEGER"),
        ("metodo_depreciacao",  "ALTER TABLE bens ADD COLUMN metodo_depreciacao TEXT DEFAULT 'linear'"),
        ("estado_operacional",  "ALTER TABLE bens ADD COLUMN estado_operacional TEXT DEFAULT 'disponivel'"),
        # categorias (modelo híbrido). categoria default 'veiculo' preserva todos os
        # bens já existentes como veículos; exige_crlv default 1 (veículo de rua).
        ("categoria",           "ALTER TABLE bens ADD COLUMN categoria TEXT NOT NULL DEFAULT 'veiculo'"),
        ("exige_crlv",          "ALTER TABLE bens ADD COLUMN exige_crlv INTEGER NOT NULL DEFAULT 1"),
        ("atributos",           "ALTER TABLE bens ADD COLUMN atributos TEXT"),
        ("descricao",           "ALTER TABLE bens ADD COLUMN descricao TEXT"),
    ]
    for col, sql in bens_migrations:
        if col not in bcols:
            cur.execute(sql)

    # backfill único (só quando exige_crlv acabou de ser criada): a antiga
    # "gambiarra" tipo_bem='equipamento' (empilhadeira) vira categoria='veiculo'
    # + exige_crlv=0 — fica em `veiculos`/`/fleet`, mas sem exigir placa/CRLV.
    if "exige_crlv" not in bcols:
        cur.execute("UPDATE bens SET exige_crlv = 0 WHERE tipo_bem = 'equipamento'")

    # migration: remove CHECK antigo de documentos (pra aceitar vistoria/foto/outro)
    drow = cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='documentos'").fetchone()
    if drow and drow["sql"] and "CHECK" in drow["sql"] and "tipo IN" in drow["sql"]:
        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")
        cur.executescript("""
            CREATE TABLE documentos_new (
              id TEXT PRIMARY KEY,
              bem_id TEXT NOT NULL REFERENCES bens(cod_interno) ON DELETE CASCADE,
              tipo TEXT NOT NULL, upload_id TEXT, filename_original TEXT,
              vencimento TEXT, observacao TEXT, criado_por INTEGER,
              criado_por_nome TEXT, criado_em TEXT NOT NULL, ativo INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO documentos_new SELECT id, bem_id, tipo, upload_id, filename_original,
              vencimento, observacao, criado_por, criado_por_nome, criado_em, ativo FROM documentos;
            DROP TABLE documentos;
            ALTER TABLE documentos_new RENAME TO documentos;
            CREATE INDEX IF NOT EXISTS idx_doc_bem ON documentos(bem_id, tipo, ativo);
        """)
        conn.execute("PRAGMA foreign_keys=ON")

    # backfill codigo_patrimonial: PAT-VEI-NNN (veiculo) / PAT-EQP-NNN (equipamento)
    falta_codigo = cur.execute(
        "SELECT cod_interno, tipo_bem FROM bens WHERE codigo_patrimonial IS NULL ORDER BY created_at, cod_interno"
    ).fetchall()
    if falta_codigo:
        # próximo número por prefixo, considerando os já existentes
        def _prox(prefixo):
            row = cur.execute(
                "SELECT codigo_patrimonial FROM bens WHERE codigo_patrimonial LIKE ? ORDER BY codigo_patrimonial DESC LIMIT 1",
                (prefixo + "%",)).fetchone()
            if row and row["codigo_patrimonial"]:
                try:
                    return int(row["codigo_patrimonial"].rsplit("-", 1)[1]) + 1
                except Exception:
                    pass
            return 1
        contadores = {"PAT-VEI": _prox("PAT-VEI"), "PAT-EQP": _prox("PAT-EQP")}
        for r in falta_codigo:
            pref = "PAT-EQP" if (r["tipo_bem"] == "equipamento") else "PAT-VEI"
            codigo = f"{pref}-{contadores[pref]:03d}"
            contadores[pref] += 1
            cur.execute("UPDATE bens SET codigo_patrimonial = ? WHERE cod_interno = ?",
                        (codigo, r["cod_interno"]))

    # migration: relaxa NOT NULL de placa/renavam/chassi/vencimento_crlv
    # (DBs antigos têm NOT NULL — rebuild da tabela preservando os dados)
    if vinfo.get("placa") and vinfo["placa"]["notnull"] == 1:
        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")
        cur.executescript("""
            CREATE TABLE veiculos_new (
              bem_id TEXT PRIMARY KEY REFERENCES bens(cod_interno) ON DELETE CASCADE,
              placa TEXT, renavam TEXT UNIQUE, chassi TEXT UNIQUE,
              marca TEXT, modelo TEXT, ano_fabricacao INTEGER, ano_modelo INTEGER,
              cor TEXT, combustivel TEXT, cilindradas INTEGER,
              km_atual INTEGER DEFAULT 0, vencimento_crlv TEXT,
              dados_raw TEXT NOT NULL, dados_origem TEXT NOT NULL,
              intervalo_km INTEGER, intervalo_meses INTEGER, km_ultima_troca INTEGER,
              data_ultima_troca TEXT, margem_km_aviso INTEGER, margem_dias_aviso INTEGER,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT INTO veiculos_new SELECT
              bem_id, placa, renavam, chassi, marca, modelo, ano_fabricacao, ano_modelo,
              cor, combustivel, cilindradas, km_atual, vencimento_crlv, dados_raw, dados_origem,
              intervalo_km, intervalo_meses, km_ultima_troca, data_ultima_troca,
              margem_km_aviso, margem_dias_aviso, created_at, updated_at
            FROM veiculos;
            DROP TABLE veiculos;
            ALTER TABLE veiculos_new RENAME TO veiculos;
            CREATE INDEX IF NOT EXISTS idx_veiculos_placa ON veiculos(placa);
            CREATE INDEX IF NOT EXISTS idx_veiculos_vencimento ON veiculos(vencimento_crlv);
        """)
        conn.execute("PRAGMA foreign_keys=ON")

    # seed filiais
    seed_sql = (BASE_DIR / "seed_filiais.sql").read_text(encoding="utf-8")
    cur.executescript(seed_sql)

    # seed dos 5 veículos demo (idempotente — só insere se tabela vazia)
    veiculos_count = cur.execute("SELECT COUNT(*) FROM veiculos").fetchone()[0]
    if veiculos_count == 0:
        seed_veic = (BASE_DIR / "seed_veiculos.sql")
        if seed_veic.exists():
            cur.executescript(seed_veic.read_text(encoding="utf-8"))

    # seed usuário admin (Renato) — idempotente. PIN = DEMO_PIN.
    usuarios_count = cur.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]
    if usuarios_count == 0:
        cur.execute("""
            INSERT INTO usuarios (id, nome, pin, filial, roles, ativo, created_at)
            VALUES (1, 'Renato Napel', ?, 100,
                    'patrimonio:cadastrar,patrimonio:listar,patrimonio:reatribuir,patrimonio:admin',
                    1, ?)
        """, (DEMO_PIN, now_iso()))

    # migration: CRLV dos veículos existentes -> tabela documentos (uma vez)
    doc_count = cur.execute("SELECT COUNT(*) FROM documentos").fetchone()[0]
    if doc_count == 0 and veiculos_count > 0:
        for v in cur.execute("""
            SELECT bem_id, vencimento_crlv, dados_origem FROM veiculos
        """).fetchall():
            origem = {}
            try:
                origem = json.loads(v["dados_origem"]) if v["dados_origem"] else {}
            except Exception:
                pass
            cur.execute("""
                INSERT INTO documentos (id, bem_id, tipo, upload_id, filename_original,
                                        vencimento, criado_por, criado_por_nome, criado_em, ativo)
                VALUES (?,?,'crlv',?,?,?,?,?,?,1)
            """, (uuid.uuid4().hex, v["bem_id"], origem.get("upload_id"),
                  origem.get("filename_original"), v["vencimento_crlv"],
                  origem.get("user_id"), None, origem.get("timestamp") or now_iso()))

    conn.commit()
    conn.close()


# ---------- Claude Vision (real) ----------
CRLV_SYSTEM_PROMPT = """Você é um especialista em extração de dados de CRLV (Certificado de Registro e Licenciamento de Veículo) brasileiro.

Sua tarefa: ler a imagem ou PDF do CRLV anexado e retornar um JSON estruturado com os campos do veículo.

Regras de extração:
- placa: formato Mercosul (3 letras + dígito + letra + 2 dígitos, ex "ABC1D23") ou antiga (3 letras + 4 dígitos, ex "ABC1234"). Sem hífen, sem espaço, MAIÚSCULAS.
- renavam: 11 dígitos numéricos. Se o documento mostrar 10 dígitos, prefixe com "0".
- chassi: 17 caracteres alfanuméricos, MAIÚSCULAS. Não contém as letras I, O ou Q (substituídas por 1, 0).
- vencimento_crlv: data no formato ISO "YYYY-MM-DD".
- ano_fabricacao / ano_modelo: inteiros de 4 dígitos.
- cilindradas: inteiro em cm³ (sem unidade).
- combustivel: um de DIESEL, GASOLINA, ETANOL, FLEX, ELETRICO, GNV.

Para cada campo crítico (placa, renavam, chassi, vencimento_crlv), forneça um score de confiança entre 0.0 e 1.0 indicando o quanto você tem certeza da leitura. Use confiança baixa (<0.85) quando o documento estiver borrado, parcialmente coberto, ou houver ambiguidade visual entre caracteres (0/O, 1/I/L, 5/S, 8/B).

Se um campo não estiver legível, retorne string vazia para texto ou null para número, e confiança 0.0.

Responda APENAS com o JSON, sem comentários extras."""

CRLV_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "placa": {"type": "string", "description": "Placa do veículo, sem hífen"},
        "renavam": {"type": "string", "description": "RENAVAM, 11 dígitos"},
        "chassi": {"type": "string", "description": "Chassi, 17 caracteres"},
        "marca": {"type": "string"},
        "modelo": {"type": "string"},
        "ano_fabricacao": {"type": ["integer", "null"]},
        "ano_modelo": {"type": ["integer", "null"]},
        "cor": {"type": "string"},
        "combustivel": {"type": "string"},
        "cilindradas": {"type": ["integer", "null"]},
        "vencimento_crlv": {"type": "string", "description": "YYYY-MM-DD"},
        "confidence": {
            "type": "object",
            "properties": {
                "placa": {"type": "number"},
                "renavam": {"type": "number"},
                "chassi": {"type": "number"},
                "vencimento_crlv": {"type": "number"},
            },
            "required": ["placa", "renavam", "chassi", "vencimento_crlv"],
            "additionalProperties": False,
        },
    },
    "required": [
        "placa", "renavam", "chassi", "marca", "modelo",
        "ano_fabricacao", "ano_modelo", "cor", "combustivel",
        "cilindradas", "vencimento_crlv", "confidence",
    ],
    "additionalProperties": False,
}


def call_claude_vision_real(file_bytes: bytes, mime_type: str) -> dict:
    """
    Chama Gemini Vision real (Flash 2.5).
    Suporta PDF e imagem nativamente. Cache automatico por prefixo.
    Schema JSON estruturado via responseSchema garante shape da resposta.

    Mantem o nome historico `call_claude_vision_real` (chamado em varios lugares).
    """
    # Normaliza mime
    mt = "image/jpeg" if mime_type in ("image/jpg",) else mime_type

    last_err = None
    for tentativa in range(3):
        try:
            # Gemini structured output via response_schema
            response = _vision_client.models.generate_content(
                model=VISION_MODEL,
                contents=[
                    genai_types.Part.from_bytes(data=file_bytes, mime_type=mt),
                    "Extraia os dados deste CRLV e retorne o JSON.\n\n" + CRLV_SYSTEM_PROMPT,
                ],
                config=genai_types.GenerateContentConfig(
                    max_output_tokens=2048,
                    response_mime_type="application/json",
                    response_schema=CRLV_OUTPUT_SCHEMA,
                ),
            )
            usage = getattr(response, "usage_metadata", None)
            if usage:
                print(f"[vision {now_brt_display()}] in={getattr(usage,'prompt_token_count','?')} "
                      f"out={getattr(usage,'candidates_token_count','?')}")
            return json.loads(response.text or "{}")
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Vision retornou JSON inválido: {e}") from e
        except Exception as e:
            last_err = e
            if tentativa < 2:
                time.sleep(2 ** tentativa)
                continue
            raise

    raise RuntimeError(f"Vision falhou após 3 tentativas: {last_err}")


def call_claude_vision_mock(filename: str) -> dict:
    """Fallback quando ANTHROPIC_API_KEY não está disponível."""
    fixtures = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
    fixtures.pop("_doc", None)
    if filename in fixtures:
        return fixtures[filename]
    for name, data in fixtures.items():
        placa = data.get("placa", "")
        if placa and placa in filename.upper().replace("-", ""):
            return data
    return next(iter(fixtures.values()))


def extract_crlv(file_bytes: bytes, mime_type: str, filename: str) -> tuple[dict, str]:
    """Roteia para Vision real ou mock. Retorna (dados, fonte).

    IMPORTANTE: com Vision real ligado, se a chamada falha NÃO cai no mock.
    Mascarar erro com dados falsos plausíveis faz o funcionário cadastrar
    veículo errado. Retorna dados vazios + fonte 'erro: <motivo>'.
    """
    if USE_REAL_VISION:
        try:
            return call_claude_vision_real(file_bytes, mime_type), "claude-vision-real"
        except Exception as e:
            msg = str(e)
            if "credit balance is too low" in msg:
                amigavel = "Sem créditos na conta de IA — avise o gestor (painel Anthropic)."
            elif "rate_limit" in msg or "429" in msg:
                amigavel = "IA recebendo muitas requisições — tente de novo em instantes."
            elif "401" in msg or "authentication" in msg.lower():
                amigavel = "Chave da IA inválida — avise o gestor."
            elif "overloaded" in msg or "529" in msg:
                amigavel = "Serviço de IA sobrecarregado — tente de novo em instantes."
            else:
                amigavel = "Leitura automática indisponível — preencha os campos manualmente."
            print(f"[vision] real falhou: {msg}")
            return {}, f"erro: {amigavel}"
    return call_claude_vision_mock(filename), "claude-vision-mock"


# ---------- helpers ----------
def now_iso() -> str:
    """ISO 8601 com offset (UTC). Para storage e API — nunca para display."""
    return datetime.now(TZ_UTC).isoformat(timespec="seconds")


def now_brt_display() -> str:
    """DD/MM/YYYY HH:MM:SS em BRT — só para logs/console."""
    return datetime.now(TZ_BRT).strftime("%d/%m/%Y %H:%M:%S")


def derive_status(vencimento_crlv: str) -> str:
    try:
        v = date.fromisoformat(vencimento_crlv)
        return "ativo" if v >= date.today() else "vencido"
    except Exception:
        return "ativo"


def get_user_by_pin(pin: str) -> Optional[dict]:
    """Resolve um usuário ativo pelo PIN. Retorna None se inválido."""
    if not pin:
        return None
    conn = db()
    row = conn.execute(
        "SELECT id, nome, filial, roles FROM usuarios WHERE pin = ? AND ativo = 1",
        (pin,)).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "user_id": row["id"],
        "nome": row["nome"],
        "filial": row["filial"],
        "roles": set(r.strip() for r in (row["roles"] or "").split(",") if r.strip()),
    }


def auth_check(authorization: Optional[str]) -> dict:
    """Auth por PIN individual — cada funcionário tem o seu.
    O token Bearer é o próprio PIN. Em staging trocar por JWT real."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Não autenticado")
    token = authorization.removeprefix("Bearer ").strip()
    user = get_user_by_pin(token)
    if not user:
        raise HTTPException(401, "PIN inválido")
    return user


def require_role(user: dict, role: str) -> None:
    if role not in user["roles"]:
        raise HTTPException(403, f"Permissão negada: {role}")


def sanitize_vision(raw: dict) -> dict:
    """Filtra apenas campos esperados antes de gravar."""
    allowed = {"placa", "renavam", "chassi", "marca", "modelo", "ano_fabricacao",
               "ano_modelo", "cor", "combustivel", "cilindradas", "vencimento_crlv"}
    return {k: v for k, v in raw.items() if k in allowed}


def validar_dados(d: dict, categoria: str = "veiculo", exige_crlv: bool = True) -> list[str]:
    """Valida dados do bem segundo categoria e se exige CRLV.

    - categoria 'veiculo' + exige_crlv=True: placa/renavam/chassi/vencimento_crlv
      obrigatórios e no formato (veículo de rua).
    - categoria 'veiculo' + exige_crlv=False (empilhadeira): nada de CRLV obrigatório;
      se placa/renavam vierem, valida o formato. Recomenda marca/modelo ou descrição.
    - categorias não-veículo (maquina/informatica/moveis/imovel): sem campos de CRLV;
      exige `descricao`. Imóvel exige sub-flag próprio/alugado em atributos.
    """
    erros = []
    eh_veic_crlv = (categoria == "veiculo" and exige_crlv)
    placa = (d.get("placa") or "").upper().replace("-", "").replace(" ", "")
    renavam = d.get("renavam") or ""
    chassi = (d.get("chassi") or "").upper()

    if placa:
        if not (PLACA_MERCOSUL_RE.match(placa) or PLACA_ANTIGA_RE.match(placa)):
            erros.append(f"placa inválida: {placa}")
    elif eh_veic_crlv:
        erros.append("placa obrigatória")

    if renavam:
        if not RENAVAM_RE.match(renavam):
            erros.append("renavam deve ter 11 dígitos")
    elif eh_veic_crlv:
        erros.append("renavam obrigatório")

    if chassi:
        # veículo de rua exige VIN de 17 chars; demais aceitam série livre
        if eh_veic_crlv and not CHASSI_RE.match(chassi):
            erros.append("chassi inválido (17 chars alfanuméricos sem I/O/Q)")
    elif eh_veic_crlv:
        erros.append("chassi obrigatório")

    if eh_veic_crlv and not d.get("vencimento_crlv"):
        erros.append("vencimento_crlv obrigatório")

    # veículo sem CRLV (empilhadeira): precisa de algo que o identifique
    if categoria == "veiculo" and not exige_crlv and not (
            d.get("marca") or d.get("modelo") or (d.get("descricao") or "").strip()):
        erros.append("informe ao menos marca, modelo ou descrição do equipamento")

    # bens não-veículo: exigem descrição (não têm placa/marca/modelo no núcleo)
    if categoria in CATEGORIAS_NAO_VEICULO and not (d.get("descricao") or "").strip():
        erros.append("informe a descrição do bem")

    # imóvel: situação = próprio uso / próprio alugado a terceiros / alugado de terceiros
    # (proprio/alugado mantidos por compatibilidade com dados legados)
    if categoria == "imovel":
        ti = ((d.get("atributos") or {}).get("tipo_imovel") or "").strip()
        if ti not in ("proprio_uso", "proprio_alugado", "terceiros_alugado", "proprio", "alugado"):
            erros.append("imóvel: informe a situação (próprio uso / próprio alugado / alugado de terceiros)")
    return erros


def validar_e_salvar_arquivo(content: bytes, content_type: str, filename: str) -> str:
    """Valida (MIME, tamanho, magic bytes) e salva o arquivo no UPLOAD_DIR.
    Retorna o upload_id. Levanta HTTPException em qualquer falha."""
    if content_type not in ALLOWED_MIME:
        raise HTTPException(415, f"Tipo não suportado: {content_type}. Use PDF, JPG ou PNG.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Arquivo maior que 10MB")
    if len(content) < 100:
        raise HTTPException(400, "Arquivo vazio ou corrompido")
    if _magic_available:
        detected = magic.from_buffer(content[:2048], mime=True)
        norm_declared = "image/jpeg" if content_type == "image/jpg" else content_type
        norm_detected = "image/jpeg" if detected == "image/jpg" else detected
        if norm_detected != norm_declared and not (
            norm_declared == "application/pdf" and norm_detected.startswith("application/")
        ):
            raise HTTPException(415, f"Conteúdo do arquivo ({detected}) não bate com o tipo declarado ({content_type})")
    upload_id = uuid.uuid4().hex
    suffix = Path(filename or "doc.bin").suffix or ".bin"
    (UPLOAD_DIR / f"{upload_id}{suffix}").write_bytes(content)
    return upload_id


def gerar_codigo_patrimonial(cur, categoria: str = "veiculo") -> str:
    """Próximo código patrimonial por categoria: PAT-VEI/MAQ/TI/MOV/IMO-NNN.
    (Códigos legados PAT-EQP-NNN da empilhadeira antiga são preservados como estão.)"""
    pref = CAT_PREFIXO.get(categoria, "PAT-VEI")
    row = cur.execute(
        "SELECT codigo_patrimonial FROM bens WHERE codigo_patrimonial LIKE ? "
        "ORDER BY codigo_patrimonial DESC LIMIT 1", (pref + "%",)).fetchone()
    n = 1
    if row and row["codigo_patrimonial"]:
        try:
            n = int(row["codigo_patrimonial"].rsplit("-", 1)[1]) + 1
        except Exception:
            pass
    return f"{pref}-{n:03d}"


def meses_entre(d1: date, d2: date) -> int:
    """Meses cheios decorridos entre d1 e d2 (d2 >= d1)."""
    return (d2.year - d1.year) * 12 + (d2.month - d1.month) - (1 if d2.day < d1.day else 0)


def calcular_depreciacao(bem: dict) -> Optional[dict]:
    """Depreciação linear. Retorna None se faltam dados (valor + data + vida útil)."""
    valor = bem.get("valor_aquisicao")
    data_aq = bem.get("data_aquisicao")
    vida = bem.get("vida_util_meses")
    if not valor or not data_aq or not vida:
        return None
    try:
        d_aq = date.fromisoformat(data_aq)
    except Exception:
        return None
    residual = bem.get("valor_residual") or 0
    meses = max(0, meses_entre(d_aq, date.today()))
    depreciavel = max(0.0, float(valor) - float(residual))
    deprec_mensal = depreciavel / vida if vida else 0
    deprec_acum = min(deprec_mensal * meses, depreciavel)
    valor_contabil = round(float(valor) - deprec_acum, 2)
    return {
        "valor_aquisicao": round(float(valor), 2),
        "data_aquisicao": data_aq,
        "valor_residual": round(float(residual), 2),
        "vida_util_meses": vida,
        "metodo": bem.get("metodo_depreciacao") or "linear",
        "meses_decorridos": meses,
        "depreciacao_mensal": round(deprec_mensal, 2),
        "depreciacao_acumulada": round(deprec_acum, 2),
        "valor_contabil_atual": valor_contabil,
        "percentual_depreciado": round((deprec_acum / depreciavel * 100) if depreciavel else 0, 1),
    }


def add_meses(d: date, m: int) -> date:
    import calendar
    total = d.month - 1 + m
    y = d.year + total // 12
    mo = total % 12 + 1
    dia = min(d.day, calendar.monthrange(y, mo)[1])
    return date(y, mo, dia)


_DOC_NOME = {"crlv": "CRLV", "ipva": "IPVA", "seguro": "Seguro", "vistoria": "Vistoria",
             "troca_oleo": "Troca de óleo",
             # manutenção predial (imóveis)
             "eletrica": "Elétrica", "hidraulica": "Hidráulica", "ar_condicionado": "Ar-condicionado",
             "dedetizacao": "Dedetização", "alvara_bombeiros": "Alvará/Bombeiros", "outro": "Manut. predial"}


def coletar_alertas(dias: int = 30) -> list[dict]:
    """Varre a frota ativa e retorna documentos/troca de óleo vencidos ou
    vencendo em até `dias`. Ordenado: mais crítico primeiro."""
    hoje = date.today()
    conn = db()
    veics = conn.execute("""
        SELECT b.cod_interno AS bem_id, b.codigo_patrimonial, b.filial,
               v.placa, v.marca, v.modelo, v.intervalo_meses, v.data_ultima_troca,
               v.margem_dias_aviso
        FROM bens b JOIN veiculos v ON v.bem_id = b.cod_interno
        WHERE b.deleted_at IS NULL
    """).fetchall()
    docs = conn.execute(
        "SELECT bem_id, tipo, vencimento FROM documentos WHERE ativo = 1 AND vencimento IS NOT NULL"
    ).fetchall()
    # manutenção predial dos imóveis (alvará/bombeiros, dedetização) com próximo vencimento
    predial = conn.execute("""
        SELECT b.cod_interno AS bem_id, b.codigo_patrimonial, b.filial, b.descricao,
               m.tipo, m.proximo_vencimento
        FROM manutencao_predial m JOIN bens b ON b.cod_interno = m.bem_id
        WHERE b.deleted_at IS NULL AND m.proximo_vencimento IS NOT NULL
    """).fetchall()
    conn.close()

    docs_by = {}
    for d in docs:
        docs_by.setdefault(d["bem_id"], []).append(d)

    alertas = []
    for vr in veics:
        v = dict(vr)
        ident = v["placa"] or v["codigo_patrimonial"] or v["bem_id"][:6]
        nome = (f"{v.get('marca') or ''} {v.get('modelo') or ''}").strip()
        for d in docs_by.get(v["bem_id"], []):
            try:
                venc = date.fromisoformat(d["vencimento"])
            except Exception:
                continue
            dd = (venc - hoje).days
            if dd <= dias:
                alertas.append({
                    "bem_id": v["bem_id"], "ident": ident, "nome": nome, "filial": v["filial"],
                    "codigo_patrimonial": v["codigo_patrimonial"], "tipo": d["tipo"],
                    "vencimento": d["vencimento"], "dias": dd,
                    "categoria": "vencido" if dd < 0 else "vencendo",
                })
        # troca de óleo (por tempo)
        if v.get("data_ultima_troca") and v.get("intervalo_meses"):
            try:
                ult = date.fromisoformat(v["data_ultima_troca"])
                prox = add_meses(ult, int(v["intervalo_meses"]))
                dd = (prox - hoje).days
                margem = v.get("margem_dias_aviso") or 15
                if dd <= margem:
                    alertas.append({
                        "bem_id": v["bem_id"], "ident": ident, "nome": nome, "filial": v["filial"],
                        "codigo_patrimonial": v["codigo_patrimonial"], "tipo": "troca_oleo",
                        "vencimento": prox.isoformat(), "dias": dd,
                        "categoria": "vencido" if dd < 0 else "vencendo",
                    })
            except Exception:
                pass

    for pr in predial:
        p = dict(pr)
        try:
            venc = date.fromisoformat(p["proximo_vencimento"])
        except Exception:
            continue
        dd = (venc - hoje).days
        if dd <= dias:
            alertas.append({
                "bem_id": p["bem_id"], "ident": p["codigo_patrimonial"] or p["bem_id"][:6],
                "nome": p.get("descricao") or "", "filial": p["filial"],
                "codigo_patrimonial": p["codigo_patrimonial"], "tipo": p["tipo"],
                "vencimento": p["proximo_vencimento"], "dias": dd,
                "categoria": "vencido" if dd < 0 else "vencendo",
            })
    alertas.sort(key=lambda a: a["dias"])
    return alertas


def montar_digest_texto(alertas: list[dict]) -> str:
    vencidos = [a for a in alertas if a["categoria"] == "vencido"]
    vencendo = [a for a in alertas if a["categoria"] == "vencendo"]

    def linha(a):
        nome = f"{a['ident']}" + (f" {a['nome']}" if a["nome"] else "")
        doc = _DOC_NOME.get(a["tipo"], a["tipo"])
        if a["dias"] < 0:
            quando = f"venceu há {-a['dias']}d"
        elif a["dias"] == 0:
            quando = "vence hoje"
        else:
            quando = f"vence em {a['dias']}d"
        return f"• {nome} — {doc} {quando} (filial {a['filial']})"

    partes = ["🚗 *Frota Napel — vencimentos*", ""]
    if vencidos:
        partes.append(f"🔴 *VENCIDOS ({len(vencidos)})*")
        partes += [linha(a) for a in vencidos]
        partes.append("")
    if vencendo:
        partes.append(f"🟡 *VENCE EM ATÉ {ALERTA_DIAS} DIAS ({len(vencendo)})*")
        partes += [linha(a) for a in vencendo]
    partes.append("")
    partes.append("Acesse: https://frota.demos.napel.com.br")
    return "\n".join(partes)


def enviar_whatsapp(numero: str, texto: str) -> int:
    import urllib.request
    body = json.dumps({"number": numero, "text": texto}).encode()
    req = urllib.request.Request(
        f"{EVOLUTION_API_URL}/message/sendText/{EVOLUTION_INSTANCE}",
        data=body, method="POST",
        headers={"apikey": EVOLUTION_API_TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def enviar_digest_alertas() -> dict:
    if not (EVOLUTION_API_URL and ALERTA_WHATSAPP):
        return {"enviado": False, "motivo": "Evolution não configurado"}
    alertas = coletar_alertas(ALERTA_DIAS)
    if not alertas:
        print(f"[alertas {now_brt_display()}] nada a notificar")
        return {"enviado": False, "motivo": "sem alertas"}
    texto = montar_digest_texto(alertas)
    try:
        status = enviar_whatsapp(ALERTA_WHATSAPP, texto)
        print(f"[alertas {now_brt_display()}] digest enviado ({len(alertas)} itens) http={status}")
        return {"enviado": True, "qtd": len(alertas), "http": status}
    except Exception as e:
        print(f"[alertas] falha no envio: {e}")
        return {"enviado": False, "motivo": str(e)}


async def _alert_loop():
    """Loop diário: dorme até ALERTA_HORA BRT e envia o digest."""
    while True:
        agora = datetime.now(TZ_BRT)
        alvo = agora.replace(hour=ALERTA_HORA, minute=0, second=0, microsecond=0)
        if alvo <= agora:
            alvo = alvo + timedelta(days=1)
        espera = (alvo - agora).total_seconds()
        try:
            await asyncio.sleep(espera)
            enviar_digest_alertas()
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[alertas] loop erro: {e}")
            await asyncio.sleep(3600)


def status_documento(vencimento: Optional[str]) -> tuple[str, Optional[int]]:
    """Retorna (status, dias_para_vencer) de um documento pela data de vencimento."""
    if not vencimento:
        return "sem_validade", None
    try:
        v = date.fromisoformat(vencimento)
        delta = (v - date.today()).days
        if delta < 0:
            return "vencido", delta
        if delta <= 30:
            return "vencendo", delta
        return "vigente", delta
    except Exception:
        return "desconhecido", None


# ---------- lifespan ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    alert_task = None
    if ENABLE_ALERTS and EVOLUTION_API_URL and ALERTA_WHATSAPP:
        alert_task = asyncio.create_task(_alert_loop())
        print(f"[alertas] agendado: diário {ALERTA_HORA}:00 BRT -> {ALERTA_WHATSAPP[:6]}***")
    yield
    if alert_task:
        alert_task.cancel()


# ---------- API ----------
app = FastAPI(title="Cadastro Veicular — Demo Local", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/api/patrimonio/health")
def health():
    return {
        "status": "ok",
        "checks": {
            "sqlite": "ok" if DB_PATH.exists() else "warn",
            "fixtures": "ok" if FIXTURES_PATH.exists() else "fail",
            "claude_vision": "real" if USE_REAL_VISION else "mock",
            "vision_model": VISION_MODEL if USE_REAL_VISION else "—",
            "alertas_whatsapp": "on" if (ENABLE_ALERTS and EVOLUTION_API_URL and ALERTA_WHATSAPP) else "off",
        },
    }


@app.get("/api/patrimonio/alertas")
def listar_alertas(dias: int = 30, authorization: Optional[str] = Header(None)):
    """Agenda de vencimentos da frota (CRLV/IPVA/seguro/vistoria/troca de óleo)."""
    user = auth_check(authorization)
    require_role(user, "patrimonio:listar")
    alertas = coletar_alertas(dias)
    return {
        "alertas": alertas,
        "resumo": {
            "total": len(alertas),
            "vencidos": sum(1 for a in alertas if a["categoria"] == "vencido"),
            "vencendo": sum(1 for a in alertas if a["categoria"] == "vencendo"),
        },
        "whatsapp_ativo": bool(ENABLE_ALERTS and EVOLUTION_API_URL and ALERTA_WHATSAPP),
    }


@app.post("/api/patrimonio/alertas/enviar")
def enviar_alertas_agora(authorization: Optional[str] = Header(None)):
    """Dispara o digest de vencimentos no WhatsApp agora (teste/manual)."""
    user = auth_check(authorization)
    require_role(user, "patrimonio:admin")
    if not (EVOLUTION_API_URL and ALERTA_WHATSAPP):
        raise HTTPException(422, "WhatsApp não configurado (faltam EVOLUTION_API_URL / número).")
    return enviar_digest_alertas()


@app.post("/api/auth/login")
def login(body: dict):
    pin = (body.get("pin") or "").strip()
    user = get_user_by_pin(pin)
    if not user:
        raise HTTPException(401, "PIN inválido")
    return {
        "token": pin,
        "user": {"id": user["user_id"], "nome": user["nome"],
                 "filial": user["filial"], "roles": sorted(user["roles"])},
    }


# ---------- gestão de usuários (admin) ----------
@app.get("/api/patrimonio/usuarios")
def listar_usuarios(authorization: Optional[str] = Header(None)):
    user = auth_check(authorization)
    require_role(user, "patrimonio:admin")
    conn = db()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, nome, pin, filial, roles, ativo FROM usuarios ORDER BY id").fetchall()]
    conn.close()
    return {"usuarios": rows}


class UsuarioIn(BaseModel):
    nome: str
    pin: str
    filial: Optional[int] = None
    roles: list[str] = Field(default_factory=lambda: ["patrimonio:cadastrar", "patrimonio:listar"])


@app.post("/api/patrimonio/usuarios")
def criar_usuario(body: UsuarioIn, authorization: Optional[str] = Header(None)):
    user = auth_check(authorization)
    require_role(user, "patrimonio:admin")
    pin = body.pin.strip()
    if not pin.isdigit() or not (4 <= len(pin) <= 8):
        raise HTTPException(422, "PIN deve ter de 4 a 8 dígitos numéricos")
    valid_roles = {"patrimonio:cadastrar", "patrimonio:listar", "patrimonio:reatribuir", "patrimonio:admin"}
    roles = [r for r in body.roles if r in valid_roles]
    if not roles:
        raise HTTPException(422, "informe ao menos uma role válida")
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO usuarios (nome, pin, filial, roles, ativo, created_at)
            VALUES (?,?,?,?,1,?)
        """, (body.nome.strip(), pin, body.filial, ",".join(roles), now_iso()))
        conn.commit()
        new_id = cur.lastrowid
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(409, f"PIN {pin} já está em uso")
    conn.close()
    return {"id": new_id, "nome": body.nome.strip()}


@app.delete("/api/patrimonio/usuarios/{user_id}")
def desativar_usuario(user_id: int, authorization: Optional[str] = Header(None)):
    user = auth_check(authorization)
    require_role(user, "patrimonio:admin")
    if user_id == user["user_id"]:
        raise HTTPException(422, "Você não pode desativar o próprio usuário")
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE usuarios SET ativo = 0 WHERE id = ?", (user_id,))
    if cur.rowcount == 0:
        conn.close()
        raise HTTPException(404, "Usuário não encontrado")
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/patrimonio/filiais")
def listar_filiais(authorization: Optional[str] = Header(None)):
    auth_check(authorization)
    conn = db()
    filiais = [dict(r) for r in conn.execute(
        "SELECT filial, sigla, nome FROM configuracao_filiais ORDER BY filial").fetchall()]
    centros = [dict(r) for r in conn.execute(
        "SELECT codigo, nome, filial FROM configuracao_centros_custo ORDER BY codigo").fetchall()]
    conn.close()
    return {"filiais": filiais, "centros_custo": centros}


@app.post("/api/patrimonio/vision/extract")
async def vision_extract(
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
):
    """
    Etapa 1 do cadastro: usuário sobe o CRLV e a IA extrai os dados.
    NÃO grava no banco. Devolve preview para o usuário confirmar.
    """
    auth_check(authorization)

    if file.content_type not in ALLOWED_MIME:
        raise HTTPException(415, f"Tipo não suportado: {file.content_type}. Use PDF, JPG ou PNG.")

    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Arquivo maior que 10MB")
    if len(content) < 100:
        raise HTTPException(400, "Arquivo vazio ou corrompido")

    # magic bytes — checa que o conteúdo bate com a extensão (anti-spoof)
    if _magic_available:
        detected = magic.from_buffer(content[:2048], mime=True)
        # tolera image/jpeg vs image/jpg
        norm_declared = "image/jpeg" if file.content_type == "image/jpg" else file.content_type
        norm_detected = "image/jpeg" if detected == "image/jpg" else detected
        if norm_detected != norm_declared and not (
            norm_declared == "application/pdf" and norm_detected.startswith("application/")
        ):
            raise HTTPException(415, f"Conteúdo do arquivo ({detected}) não bate com o tipo declarado ({file.content_type})")

    # salva localmente (em prod -> volume Coolify / S3)
    upload_id = uuid.uuid4().hex
    suffix = Path(file.filename or "doc.bin").suffix or ".bin"
    saved_path = UPLOAD_DIR / f"{upload_id}{suffix}"
    saved_path.write_bytes(content)

    t0 = time.time()
    raw, fonte = extract_crlv(content, file.content_type, file.filename or "")
    latency_ms = int((time.time() - t0) * 1000)

    # falha na IA — arquivo fica salvo, mas form vem vazio + aviso
    erro_vision = fonte[5:].strip() if fonte.startswith("erro:") else None

    # campos críticos com confiança <0.85 -> exige confirmação manual
    conf = raw.get("confidence", {})
    criticos = {"placa", "renavam", "chassi", "vencimento_crlv"}
    incertos = [k for k in criticos if conf.get(k, 1.0) < 0.85]

    return {
        "upload_id": upload_id,
        "dados_extraidos": sanitize_vision(raw),
        "confidence": conf,
        "campos_incertos": incertos,
        "bloqueia_save_automatico": len(incertos) > 2,
        "latency_ms": latency_ms,
        "filename": file.filename,
        "bytes": len(content),
        "fonte": fonte,
        "erro_vision": erro_vision,
        "modelo": VISION_MODEL if USE_REAL_VISION and "real" in fonte else "fixture",
    }


class CadastroIn(BaseModel):
    # tipo 'veiculo' exige CRLV; 'equipamento' (empilhadeira etc.) não
    tipo_bem: str = "veiculo"
    upload_id: Optional[str] = None          # opcional — cadastro manual não tem
    placa: Optional[str] = None
    renavam: Optional[str] = None
    chassi: Optional[str] = None
    marca: Optional[str] = None
    modelo: Optional[str] = None
    ano_fabricacao: Optional[int] = None
    ano_modelo: Optional[int] = None
    cor: Optional[str] = None
    combustivel: Optional[str] = None
    cilindradas: Optional[int] = None
    vencimento_crlv: Optional[str] = None
    filial: int
    centro_custo: str
    km_atual: int = 0
    # bloco patrimonial (todos opcionais — preenche no cadastro ou depois)
    data_aquisicao: Optional[str] = None
    valor_aquisicao: Optional[float] = None
    fornecedor: Optional[str] = None
    nf_numero: Optional[str] = None
    nf_chave: Optional[str] = None
    valor_residual: Optional[float] = None
    vida_util_meses: Optional[int] = None
    estado_operacional: Optional[str] = None
    dados_raw: dict = Field(default_factory=dict)
    # metadados do upload (pra trilhar de onde veio a extração)
    fonte: Optional[str] = None             # "claude-vision-real" | "claude-vision-mock" | "manual"
    filename_original: Optional[str] = None # nome do arquivo enviado pelo usuário
    # campos de manutenção (contrato com oil-change) — todos opcionais.
    # Veículo pode ser cadastrado sem entrar no programa oil-change ainda.
    intervalo_km: Optional[int] = None
    intervalo_meses: Optional[int] = None
    km_ultima_troca: Optional[int] = None
    data_ultima_troca: Optional[str] = None
    margem_km_aviso: Optional[int] = None
    margem_dias_aviso: Optional[int] = None


@app.post("/api/patrimonio/veiculos/cadastrar")
def cadastrar_veiculo(
    body: CadastroIn,
    authorization: Optional[str] = Header(None),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    user = auth_check(authorization)
    require_role(user, "patrimonio:cadastrar")

    # idempotency: mesma key em <24h -> devolve a resposta anterior
    now = time.time()
    if idempotency_key and idempotency_key in IDEMPOTENCY_CACHE:
        ts, cached = IDEMPOTENCY_CACHE[idempotency_key]
        if now - ts < IDEMPOTENCY_TTL:
            return JSONResponse(cached, headers={"X-Idempotent-Replay": "true"})

    # validações
    data = body.model_dump()
    tipo_bem = data.get("tipo_bem") or "veiculo"
    if tipo_bem not in ("veiculo", "equipamento"):
        raise HTTPException(422, "tipo_bem deve ser 'veiculo' ou 'equipamento'")
    # Este endpoint cadastra SÓ a categoria 'veiculo' (carros/motos/caminhões e a
    # empilhadeira). tipo_bem='equipamento' = empilhadeira: continua categoria veículo
    # (fica no /fleet do oil-change), porém sem exigir placa/RENAVAM/chassi/CRLV.
    categoria = "veiculo"
    exige_crlv = (tipo_bem == "veiculo")
    erros = validar_dados(data, categoria, exige_crlv)
    if erros:
        raise HTTPException(422, {"erros": erros})

    placa_norm = ((data.get("placa") or "").upper().replace("-", "").replace(" ", "")) or None
    renavam = data.get("renavam") or None
    chassi = ((data.get("chassi") or "").upper()) or None
    venc = data.get("vencimento_crlv") or None

    # transação atômica bens + veiculos
    conn = db()
    try:
        cur = conn.cursor()
        # checa duplicata por placa+filial ativo (só se tiver placa)
        if placa_norm:
            dup = cur.execute("""
                SELECT b.cod_interno FROM bens b
                JOIN veiculos v ON v.bem_id = b.cod_interno
                WHERE v.placa = ? AND b.filial = ? AND b.deleted_at IS NULL
            """, (placa_norm, data["filial"])).fetchone()
            if dup:
                raise HTTPException(409, f"Placa {placa_norm} já cadastrada na filial {data['filial']}")

        bem_id = uuid.uuid4().hex
        ts = now_iso()
        status = derive_status(venc) if venc else "ativo"
        codigo_pat = gerar_codigo_patrimonial(cur, categoria)
        # vida útil default: 60 meses (5 anos) p/ veículo de rua, 120 (10 anos) p/ empilhadeira
        vida_util = data.get("vida_util_meses")
        if vida_util is None:
            vida_util = 60 if exige_crlv else 120
        estado_op = data.get("estado_operacional") or "disponivel"
        # empilhadeira (sem placa) ganha uma descrição p/ aparecer nomeada na listagem
        descricao = None
        if not exige_crlv:
            descricao = (f"{data.get('marca') or ''} {data.get('modelo') or ''}").strip() or None

        cur.execute("""
            INSERT INTO bens (cod_interno, codigo_patrimonial, filial, centro_custo,
                              responsavel_user_id, responsavel_nome, data_aquisicao,
                              valor_aquisicao, fornecedor, nf_numero, nf_chave,
                              valor_residual, vida_util_meses, metodo_depreciacao,
                              estado_operacional, tipo_bem, categoria, exige_crlv, descricao,
                              status, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (bem_id, codigo_pat, data["filial"], data["centro_custo"], user["user_id"],
              user["nome"], data.get("data_aquisicao"), data.get("valor_aquisicao"),
              data.get("fornecedor"), data.get("nf_numero"), data.get("nf_chave"),
              data.get("valor_residual") or 0, vida_util, "linear",
              estado_op, tipo_bem, categoria, 1 if exige_crlv else 0, descricao,
              status, ts, ts))

        # movimentação inicial de aquisição (timeline)
        cur.execute("""
            INSERT INTO movimentacoes (id, bem_id, tipo, para_filial, para_responsavel,
                                       para_estado, valor, data_evento, executado_por,
                                       executado_por_nome, created_at)
            VALUES (?,?,'aquisicao',?,?,?,?,?,?,?,?)
        """, (uuid.uuid4().hex, bem_id, data["filial"], user["user_id"], estado_op,
              data.get("valor_aquisicao"), data.get("data_aquisicao") or ts[:10],
              user["user_id"], user["nome"], ts))

        cur.execute("""
            INSERT INTO veiculos (bem_id, placa, renavam, chassi, marca, modelo,
                                  ano_fabricacao, ano_modelo, cor, combustivel,
                                  cilindradas, km_atual, vencimento_crlv,
                                  dados_raw, dados_origem,
                                  intervalo_km, intervalo_meses, km_ultima_troca,
                                  data_ultima_troca, margem_km_aviso, margem_dias_aviso,
                                  created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            bem_id, placa_norm, renavam, chassi,
            data.get("marca"), data.get("modelo"),
            data.get("ano_fabricacao"), data.get("ano_modelo"),
            data.get("cor"), data.get("combustivel"), data.get("cilindradas"),
            data.get("km_atual", 0), venc,
            json.dumps(data.get("dados_raw", {})),
            json.dumps({
                "user_id": user["user_id"],
                "timestamp": ts,
                "upload_id": data.get("upload_id"),
                "fonte": data.get("fonte") or ("manual" if not data.get("upload_id") else "claude-vision-mock"),
                "filename_original": data.get("filename_original"),
            }),
            data.get("intervalo_km"),
            data.get("intervalo_meses"),
            data.get("km_ultima_troca"),
            data.get("data_ultima_troca"),
            data.get("margem_km_aviso"),
            data.get("margem_dias_aviso"),
            ts, ts,
        ))
        # registra o CRLV na tabela documentos — só se houve upload + vencimento
        if data.get("upload_id") and venc:
            cur.execute("""
                INSERT INTO documentos (id, bem_id, tipo, upload_id, filename_original,
                                        vencimento, criado_por, criado_por_nome, criado_em, ativo)
                VALUES (?,?,'crlv',?,?,?,?,?,?,1)
            """, (uuid.uuid4().hex, bem_id, data["upload_id"], data.get("filename_original"),
                  venc, user["user_id"], user["nome"], ts))
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        raise HTTPException(409, f"Conflito de unique constraint: {e}")
    finally:
        conn.close()

    resp = {
        "bem_id": bem_id,
        "codigo_patrimonial": codigo_pat,
        "placa": placa_norm,
        "status": status,
        "responsavel": user["nome"],
        "created_at": ts,
    }
    if idempotency_key:
        IDEMPOTENCY_CACHE[idempotency_key] = (now, resp)
    return resp


@app.get("/api/patrimonio/veiculos")
def listar_veiculos(
    filial: Optional[int] = None,
    status: Optional[str] = None,
    busca: Optional[str] = None,
    incluir_baixados: bool = False,
    authorization: Optional[str] = Header(None),
):
    user = auth_check(authorization)
    require_role(user, "patrimonio:listar")

    sql = """
        SELECT b.cod_interno AS bem_id, v.placa, v.modelo, v.marca,
               b.filial, b.responsavel_nome, b.status, b.deleted_at, b.tipo_bem,
               v.vencimento_crlv, v.km_atual
        FROM bens b
        JOIN veiculos v ON v.bem_id = b.cod_interno
        WHERE 1=1
    """
    params: list = []
    # baixados: por padrão escondidos; aparecem se incluir_baixados OU filtro status=baixado
    if status == "baixado":
        sql += " AND b.deleted_at IS NOT NULL"
    elif not incluir_baixados:
        sql += " AND b.deleted_at IS NULL"
    if filial:
        sql += " AND b.filial = ?"
        params.append(filial)
    if status and status != "baixado":
        sql += " AND b.status = ? AND b.deleted_at IS NULL"
        params.append(status)
    if busca:
        sql += " AND (v.placa LIKE ? OR v.modelo LIKE ? OR v.renavam LIKE ?)"
        like = f"%{busca.upper()}%"
        params += [like, like, like]
    sql += " ORDER BY v.vencimento_crlv ASC"

    conn = db()
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    # KPIs
    today = date.today().isoformat()
    kpis = {
        "total": len(rows),
        "ativos": sum(1 for r in rows if r["status"] == "ativo"),
        "vencendo_30d": sum(1 for r in rows if r["vencimento_crlv"] >= today
                            and (date.fromisoformat(r["vencimento_crlv"]) - date.today()).days <= 30),
        "vencidos": sum(1 for r in rows if r["status"] == "vencido"),
    }
    conn.close()
    return {"veiculos": rows, "kpis": kpis}


@app.get("/api/patrimonio/veiculos/{bem_id}")
def obter_veiculo(bem_id: str, authorization: Optional[str] = Header(None)):
    user = auth_check(authorization)
    require_role(user, "patrimonio:listar")
    conn = db()
    # permite ver detalhe de baixados também (pra poder reativar)
    row = conn.execute("""
        SELECT b.*, v.*
        FROM bens b JOIN veiculos v ON v.bem_id = b.cod_interno
        WHERE b.cod_interno = ?
    """, (bem_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Veículo não encontrado")
    data = dict(row)
    data["dados_raw"] = json.loads(data["dados_raw"])
    data["dados_origem"] = json.loads(data["dados_origem"])
    # esconde dados_raw se não for admin
    if "patrimonio:admin" not in user["roles"]:
        data.pop("dados_raw", None)
    return data


class VeiculoFullPatch(BaseModel):
    """PATCH parcial pra editar QUALQUER campo do veículo.
    Diferente do PATCH /fleet/{id} (que é só manutenção pro oil-change),
    esse aqui edita campos base: filial, placa, modelo, responsável etc.
    """
    # Bens
    filial: Optional[int] = None
    centro_custo: Optional[str] = None
    responsavel_user_id: Optional[int] = None
    responsavel_nome: Optional[str] = None
    status: Optional[str] = None
    # Veículos
    placa: Optional[str] = None
    renavam: Optional[str] = None
    chassi: Optional[str] = None
    marca: Optional[str] = None
    modelo: Optional[str] = None
    ano_fabricacao: Optional[int] = Field(default=None, ge=1900, le=2100)
    ano_modelo: Optional[int] = Field(default=None, ge=1900, le=2100)
    cor: Optional[str] = None
    combustivel: Optional[str] = None
    cilindradas: Optional[int] = Field(default=None, ge=0)
    vencimento_crlv: Optional[str] = None
    km_atual: Optional[int] = Field(default=None, ge=0)
    # bloco patrimonial
    data_aquisicao: Optional[str] = None
    valor_aquisicao: Optional[float] = Field(default=None, ge=0)
    fornecedor: Optional[str] = None
    nf_numero: Optional[str] = None
    nf_chave: Optional[str] = None
    valor_residual: Optional[float] = Field(default=None, ge=0)
    vida_util_meses: Optional[int] = Field(default=None, ge=1)
    estado_operacional: Optional[str] = None


_BENS_FIELDS = {"filial", "centro_custo", "responsavel_user_id", "responsavel_nome", "status",
                "data_aquisicao", "valor_aquisicao", "fornecedor", "nf_numero", "nf_chave",
                "valor_residual", "vida_util_meses", "estado_operacional"}
_VEICULOS_FIELDS = {"placa", "renavam", "chassi", "marca", "modelo", "ano_fabricacao",
                    "ano_modelo", "cor", "combustivel", "cilindradas", "vencimento_crlv", "km_atual"}


@app.patch("/api/patrimonio/veiculos/{bem_id}")
def editar_veiculo(
    bem_id: str,
    body: VeiculoFullPatch,
    authorization: Optional[str] = Header(None),
):
    """Edita campos base do veículo. Valida placa/RENAVAM/chassi se mudarem.
    Verifica duplicata placa+filial (excluindo self)."""
    user = auth_check(authorization)
    require_role(user, "patrimonio:cadastrar")

    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(422, "body vazio — informe ao menos um campo")

    # normaliza placa
    if "placa" in updates:
        updates["placa"] = updates["placa"].upper().replace("-", "").replace(" ", "")

    # validações se mudar placa/renavam/chassi
    if any(k in updates for k in ("placa", "renavam", "chassi", "vencimento_crlv")):
        # busca atual pra fazer merge e validar
        conn = db()
        atual = conn.execute("""
            SELECT v.placa, v.renavam, v.chassi, v.vencimento_crlv,
                   v.marca, v.modelo, b.tipo_bem, b.categoria, b.exige_crlv
            FROM veiculos v JOIN bens b ON b.cod_interno = v.bem_id
            WHERE v.bem_id = ?
        """, (bem_id,)).fetchone()
        conn.close()
        if not atual:
            raise HTTPException(404, "Veículo não encontrado")
        merged = {
            "placa": updates.get("placa", atual["placa"]),
            "renavam": updates.get("renavam", atual["renavam"]),
            "chassi": updates.get("chassi", atual["chassi"]),
            "vencimento_crlv": updates.get("vencimento_crlv", atual["vencimento_crlv"]),
            "marca": updates.get("marca", atual["marca"]),
            "modelo": updates.get("modelo", atual["modelo"]),
        }
        # empilhadeira (categoria veículo, exige_crlv=0) NÃO exige placa/RENAVAM/chassi/CRLV
        erros = validar_dados(merged, atual["categoria"] or "veiculo", bool(atual["exige_crlv"]))
        if erros:
            raise HTTPException(422, {"erros": erros})

    # valida status enum se mudou
    if "status" in updates and updates["status"] not in ("ativo", "manutencao", "vencido", "baixado"):
        raise HTTPException(422, "status deve ser: ativo, manutencao, vencido ou baixado")

    # valida data ISO se mudou
    if "vencimento_crlv" in updates:
        try:
            date.fromisoformat(updates["vencimento_crlv"])
        except ValueError:
            raise HTTPException(422, "vencimento_crlv deve ser YYYY-MM-DD")

    # se mudou placa OU filial, checa duplicata
    if "placa" in updates or "filial" in updates:
        conn = db()
        atual = conn.execute("""
            SELECT v.placa, b.filial FROM veiculos v JOIN bens b ON b.cod_interno = v.bem_id
            WHERE v.bem_id = ?
        """, (bem_id,)).fetchone()
        conn.close()
        nova_placa = updates.get("placa", atual["placa"]) if atual else updates.get("placa")
        nova_filial = updates.get("filial", atual["filial"]) if atual else updates.get("filial")
        conn = db()
        dup = conn.execute("""
            SELECT b.cod_interno FROM bens b JOIN veiculos v ON v.bem_id = b.cod_interno
            WHERE v.placa = ? AND b.filial = ? AND b.deleted_at IS NULL AND b.cod_interno != ?
        """, (nova_placa, nova_filial, bem_id)).fetchone()
        conn.close()
        if dup:
            raise HTTPException(409, f"Placa {nova_placa} já cadastrada na filial {nova_filial}")

    # split entre bens e veiculos
    bens_upd = {k: v for k, v in updates.items() if k in _BENS_FIELDS}
    veiculos_upd = {k: v for k, v in updates.items() if k in _VEICULOS_FIELDS}

    conn = db()
    cur = conn.cursor()
    try:
        ts = now_iso()
        if veiculos_upd:
            sets = ", ".join(f"{k} = ?" for k in veiculos_upd) + ", updated_at = ?"
            params = list(veiculos_upd.values()) + [ts, bem_id]
            cur.execute(f"UPDATE veiculos SET {sets} WHERE bem_id = ?", params)
            if cur.rowcount == 0:
                conn.rollback(); conn.close()
                raise HTTPException(404, "Veículo não encontrado")
        if bens_upd:
            sets = ", ".join(f"{k} = ?" for k in bens_upd) + ", updated_at = ?"
            params = list(bens_upd.values()) + [ts, bem_id]
            cur.execute(f"UPDATE bens SET {sets} WHERE cod_interno = ?", params)
            if cur.rowcount == 0:
                conn.rollback(); conn.close()
                raise HTTPException(404, "Veículo não encontrado")
        # toca updated_at de ambos mesmo se só um foi mudado
        cur.execute("UPDATE bens SET updated_at = ? WHERE cod_interno = ?", (ts, bem_id))
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        raise HTTPException(409, f"Conflito de unique constraint: {e}")
    finally:
        conn.close()

    return {"id": bem_id, "updated": updates, "updated_at": now_iso()}


@app.delete("/api/patrimonio/veiculos/{bem_id}")
def baixar_veiculo(bem_id: str, authorization: Optional[str] = Header(None)):
    """Baixa simples (sem motivo) — mantida pra compat. Registra movimentação."""
    user = auth_check(authorization)
    require_role(user, "patrimonio:admin")
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE bens SET deleted_at = ?, status = 'baixado', estado_operacional='baixado' WHERE cod_interno = ?",
                (now_iso(), bem_id))
    if cur.rowcount == 0:
        conn.close()
        raise HTTPException(404, "Veículo não encontrado")
    cur.execute("""INSERT INTO movimentacoes (id, bem_id, tipo, para_estado, motivo,
                   data_evento, executado_por, executado_por_nome, created_at)
                   VALUES (?,?,'baixa','baixado','baixa simples',?,?,?,?)""",
                (uuid.uuid4().hex, bem_id, now_iso()[:10], user["user_id"], user["nome"], now_iso()))
    conn.commit()
    conn.close()
    return {"ok": True}


class BaixaIn(BaseModel):
    motivo: str = "venda"   # venda, sucata, perda, roubo, fim_vida, outro
    valor: Optional[float] = Field(default=None, ge=0)   # valor de venda
    comprador: Optional[str] = None
    data: Optional[str] = None   # data do evento YYYY-MM-DD
    observacao: Optional[str] = None


@app.post("/api/patrimonio/veiculos/{bem_id}/baixar")
def baixar_veiculo_completo(bem_id: str, body: BaixaIn, authorization: Optional[str] = Header(None)):
    """Baixa patrimonial com motivo (venda/sucata/perda/roubo/fim_vida).
    Pra venda: calcula resultado contábil (valor de venda - valor contábil depreciado)."""
    user = auth_check(authorization)
    require_role(user, "patrimonio:admin")
    motivos_validos = {"venda", "sucata", "perda", "roubo", "fim_vida", "outro"}
    if body.motivo not in motivos_validos:
        raise HTTPException(422, f"motivo deve ser um de: {', '.join(motivos_validos)}")
    if body.data:
        try:
            date.fromisoformat(body.data)
        except ValueError:
            raise HTTPException(422, "data deve ser YYYY-MM-DD")

    conn = db()
    bem = conn.execute("SELECT * FROM bens WHERE cod_interno = ? AND deleted_at IS NULL", (bem_id,)).fetchone()
    if not bem:
        conn.close()
        raise HTTPException(404, "Veículo não encontrado ou já baixado")
    bem = dict(bem)

    # resultado contábil (só faz sentido na venda, e se há valor + depreciação calculável)
    resultado = None
    deprec = calcular_depreciacao(bem)
    if body.motivo == "venda" and body.valor is not None and deprec:
        resultado = round(float(body.valor) - deprec["valor_contabil_atual"], 2)

    ts = now_iso()
    tipo_mov = "venda" if body.motivo == "venda" else "baixa"
    cur = conn.cursor()
    cur.execute("""UPDATE bens SET deleted_at = ?, status = 'baixado',
                   estado_operacional = 'baixado', updated_at = ? WHERE cod_interno = ?""",
                (ts, ts, bem_id))
    cur.execute("""INSERT INTO movimentacoes (id, bem_id, tipo, de_estado, para_estado,
                   motivo, valor, comprador, resultado_contabil, data_evento,
                   executado_por, executado_por_nome, created_at)
                   VALUES (?,?,?,?,'baixado',?,?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex, bem_id, tipo_mov, bem.get("estado_operacional"),
                 body.motivo, body.valor, body.comprador, resultado,
                 body.data or ts[:10], user["user_id"], user["nome"], ts))
    conn.commit()
    conn.close()
    return {
        "id": bem_id, "motivo": body.motivo, "valor": body.valor,
        "comprador": body.comprador, "resultado_contabil": resultado,
        "valor_contabil_na_baixa": deprec["valor_contabil_atual"] if deprec else None,
    }


@app.post("/api/patrimonio/veiculos/{bem_id}/transferir")
def transferir_veiculo(bem_id: str, body: dict, authorization: Optional[str] = Header(None)):
    """Transfere o bem pra outra filial, registrando a movimentação."""
    user = auth_check(authorization)
    require_role(user, "patrimonio:reatribuir")
    para_filial = body.get("para_filial")
    if not para_filial:
        raise HTTPException(422, "informe para_filial")
    conn = db()
    bem = conn.execute("SELECT filial, centro_custo FROM bens WHERE cod_interno = ? AND deleted_at IS NULL",
                       (bem_id,)).fetchone()
    if not bem:
        conn.close()
        raise HTTPException(404, "Veículo não encontrado")
    de_filial = bem["filial"]
    if int(para_filial) == de_filial:
        conn.close()
        raise HTTPException(422, "filial de destino igual à atual")
    novo_cc = body.get("centro_custo")
    ts = now_iso()
    cur = conn.cursor()
    if novo_cc:
        cur.execute("UPDATE bens SET filial = ?, centro_custo = ?, updated_at = ? WHERE cod_interno = ?",
                    (para_filial, novo_cc, ts, bem_id))
    else:
        cur.execute("UPDATE bens SET filial = ?, updated_at = ? WHERE cod_interno = ?",
                    (para_filial, ts, bem_id))
    cur.execute("""INSERT INTO movimentacoes (id, bem_id, tipo, de_filial, para_filial,
                   motivo, data_evento, executado_por, executado_por_nome, created_at)
                   VALUES (?,?,'transferencia',?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex, bem_id, de_filial, para_filial, body.get("motivo"),
                 ts[:10], user["user_id"], user["nome"], ts))
    conn.commit()
    conn.close()
    return {"id": bem_id, "de_filial": de_filial, "para_filial": para_filial}


@app.get("/api/patrimonio/veiculos/{bem_id}/movimentacoes")
def listar_movimentacoes(bem_id: str, authorization: Optional[str] = Header(None)):
    user = auth_check(authorization)
    require_role(user, "patrimonio:listar")
    conn = db()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM movimentacoes WHERE bem_id = ? ORDER BY created_at DESC", (bem_id,)).fetchall()]
    conn.close()
    return {"movimentacoes": rows}


@app.get("/api/patrimonio/veiculos/{bem_id}/depreciacao")
def depreciacao_veiculo(bem_id: str, authorization: Optional[str] = Header(None)):
    user = auth_check(authorization)
    require_role(user, "patrimonio:listar")
    conn = db()
    bem = conn.execute("SELECT * FROM bens WHERE cod_interno = ?", (bem_id,)).fetchone()
    conn.close()
    if not bem:
        raise HTTPException(404, "Veículo não encontrado")
    deprec = calcular_depreciacao(dict(bem))
    if not deprec:
        return {"calculavel": False, "motivo": "faltam dados de aquisição (valor, data e vida útil)"}
    return {"calculavel": True, **deprec}


# ---------- documentos do veículo (CRLV / IPVA / Seguro) ----------
def _doc_to_dto(row: dict) -> dict:
    """Converte uma linha da tabela documentos -> DTO de API."""
    upload_id = row.get("upload_id")
    arquivo_existe = bool(upload_id and list(UPLOAD_DIR.glob(f"{upload_id}.*")))
    status, dias = status_documento(row.get("vencimento"))
    return {
        "id": row["id"],
        "type": row["tipo"],
        "vencimento": row.get("vencimento"),
        "status": status,
        "dias_para_vencer": dias,
        "observacao": row.get("observacao"),
        "upload_id": upload_id,
        "filename_original": row.get("filename_original"),
        "arquivo_disponivel": arquivo_existe,
        "download_url": f"/api/patrimonio/uploads/{upload_id}" if (upload_id and arquivo_existe) else None,
        "criado_por_nome": row.get("criado_por_nome"),
        "criado_em": row.get("criado_em"),
        "ativo": bool(row.get("ativo", 1)),
    }


def _documentos_do_veiculo(bem_id: str, incluir_historico: bool = False) -> list[dict]:
    conn = db()
    sql = "SELECT * FROM documentos WHERE bem_id = ?"
    if not incluir_historico:
        sql += " AND ativo = 1"
    sql += " ORDER BY tipo, criado_em DESC"
    rows = [dict(r) for r in conn.execute(sql, (bem_id,)).fetchall()]
    conn.close()
    return [_doc_to_dto(r) for r in rows]


@app.get("/api/patrimonio/veiculos/{bem_id}/documentos")
def listar_documentos(
    bem_id: str,
    incluir_historico: bool = False,
    authorization: Optional[str] = Header(None),
):
    user = auth_check(authorization)
    require_role(user, "patrimonio:listar")
    conn = db()
    veic = conn.execute(
        "SELECT categoria FROM bens WHERE cod_interno = ? AND deleted_at IS NULL",
        (bem_id,)).fetchone()
    conn.close()
    if not veic:
        raise HTTPException(404, "Bem não encontrado")
    docs = _documentos_do_veiculo(bem_id, incluir_historico)
    # placeholders de CRLV/IPVA/seguro/vistoria só fazem sentido para veículo
    if (veic["categoria"] or "veiculo") == "veiculo":
        presentes = {d["type"] for d in docs if d["ativo"]}
        for tipo in ("crlv", "ipva", "seguro", "vistoria"):
            if tipo not in presentes:
                docs.append({"type": tipo, "status": "nao_cadastrado", "ativo": True})
    return {"vehicle_id": bem_id, "documentos": docs}


@app.post("/api/patrimonio/veiculos/{bem_id}/documentos")
async def adicionar_documento(
    bem_id: str,
    tipo: str = Form(...),
    vencimento: Optional[str] = Form(None),
    observacao: Optional[str] = Form(None),
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
):
    """
    Adiciona/renova documento ou foto.
    Docs com vencimento (crlv/ipva/seguro/vistoria): renovação — o anterior do
    mesmo tipo vira histórico (ativo=0). Fotos/outros: múltiplos coexistem.
    Se for CRLV com vencimento, atualiza também veiculos.vencimento_crlv.
    """
    user = auth_check(authorization)
    require_role(user, "patrimonio:cadastrar")
    tipos_validos = ("crlv", "ipva", "seguro", "vistoria", "foto", "outro")
    if tipo not in tipos_validos:
        raise HTTPException(422, f"tipo deve ser um de: {', '.join(tipos_validos)}")
    if vencimento:
        try:
            date.fromisoformat(vencimento)
        except ValueError:
            raise HTTPException(422, "vencimento deve ser YYYY-MM-DD")

    conn = db()
    veic = conn.execute(
        "SELECT cod_interno FROM bens WHERE cod_interno = ? AND deleted_at IS NULL",
        (bem_id,)).fetchone()
    conn.close()
    if not veic:
        raise HTTPException(404, "Veículo não encontrado")

    content = await file.read()
    upload_id = validar_e_salvar_arquivo(content, file.content_type, file.filename or "")

    ts = now_iso()
    doc_id = uuid.uuid4().hex
    eh_renovavel = tipo in ("crlv", "ipva", "seguro", "vistoria")
    conn = db()
    cur = conn.cursor()
    # renovação só pra documentos com validade; fotos/outros coexistem
    if eh_renovavel:
        cur.execute("UPDATE documentos SET ativo = 0 WHERE bem_id = ? AND tipo = ? AND ativo = 1",
                    (bem_id, tipo))
    cur.execute("""
        INSERT INTO documentos (id, bem_id, tipo, upload_id, filename_original,
                                vencimento, observacao, criado_por, criado_por_nome, criado_em, ativo)
        VALUES (?,?,?,?,?,?,?,?,?,?,1)
    """, (doc_id, bem_id, tipo, upload_id, file.filename, vencimento, observacao,
          user["user_id"], user["nome"], ts))
    # CRLV com vencimento -> sincroniza veiculos.vencimento_crlv + status
    if tipo == "crlv" and vencimento:
        novo_status = derive_status(vencimento)
        cur.execute("UPDATE veiculos SET vencimento_crlv = ?, updated_at = ? WHERE bem_id = ?",
                    (vencimento, ts, bem_id))
        cur.execute("UPDATE bens SET status = ?, updated_at = ? WHERE cod_interno = ? AND status != 'baixado'",
                    (novo_status, ts, bem_id))
    conn.commit()
    conn.close()
    return {"id": doc_id, "tipo": tipo, "upload_id": upload_id, "vencimento": vencimento}


@app.get("/api/v1/fleet/vehicles/{vehicle_id}/documents")
def fleet_documents(vehicle_id: str, authorization: Optional[str] = Header(None)):
    """Alias /fleet (contrato oil-change) — lê da tabela documentos."""
    user = auth_check(authorization)
    require_role(user, "patrimonio:listar")
    conn = db()
    row = conn.execute(
        "SELECT v.placa FROM bens b JOIN veiculos v ON v.bem_id = b.cod_interno "
        "WHERE b.cod_interno = ?", (vehicle_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Veículo não encontrado")
    docs = _documentos_do_veiculo(vehicle_id, incluir_historico=False)
    presentes = {d["type"] for d in docs}
    for tipo in ("crlv", "ipva", "seguro", "vistoria"):
        if tipo not in presentes:
            docs.append({"type": tipo, "status": "nao_cadastrado", "ativo": True})
    return {"vehicle_id": vehicle_id, "placa": row["placa"], "documents": docs}


@app.delete("/api/patrimonio/documentos/{doc_id}")
def remover_documento(doc_id: str, authorization: Optional[str] = Header(None)):
    """Remove um documento/foto (hard delete do registro; arquivo fica no disco)."""
    user = auth_check(authorization)
    require_role(user, "patrimonio:cadastrar")
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM documentos WHERE id = ?", (doc_id,))
    if cur.rowcount == 0:
        conn.close()
        raise HTTPException(404, "Documento não encontrado")
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/patrimonio/veiculos/{bem_id}/reativar")
def reativar_veiculo(bem_id: str, authorization: Optional[str] = Header(None)):
    """Reativa um veículo baixado (desfaz o soft-delete)."""
    user = auth_check(authorization)
    require_role(user, "patrimonio:cadastrar")
    conn = db()
    # LEFT JOIN: bens não-veículo não têm linha em `veiculos`
    row = conn.execute(
        "SELECT b.categoria, v.vencimento_crlv FROM bens b "
        "LEFT JOIN veiculos v ON v.bem_id = b.cod_interno WHERE b.cod_interno = ?",
        (bem_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Bem não encontrado")
    novo_status = (derive_status(row["vencimento_crlv"])
                   if (row["categoria"] == "veiculo" and row["vencimento_crlv"]) else "ativo")
    cur = conn.cursor()
    cur.execute("UPDATE bens SET deleted_at = NULL, status = ?, updated_at = ? WHERE cod_interno = ?",
                (novo_status, now_iso(), bem_id))
    conn.commit()
    conn.close()
    return {"id": bem_id, "status": novo_status, "reativado": True}


@app.get("/api/patrimonio/uploads/{upload_id}")
def download_upload(
    upload_id: str,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = None,
    inline: bool = True,
):
    """
    Download/visualização do arquivo CRLV cru.
    RBAC: bem.filial == user.filial OU admin.
    Aceita auth via header OU `?token=` (pra abrir direto no browser via <a>).
    Por padrão serve inline (renderiza no browser); ?inline=false força download.
    """
    # auth — aceita header ou query param
    auth_header = authorization or (f"Bearer {token}" if token else None)
    user = auth_check(auth_header)

    # busca o veículo dono do upload — procura na tabela documentos E em dados_origem
    conn = db()
    row = conn.execute("""
        SELECT b.filial, d.filename_original FROM documentos d
        JOIN bens b ON b.cod_interno = d.bem_id
        WHERE d.upload_id = ?
    """, (upload_id,)).fetchone()
    if not row:
        # fallback: uploads antigos referenciados só em dados_origem
        row = conn.execute("""
            SELECT b.filial, json_extract(v.dados_origem, '$.filename_original') AS filename_original
            FROM bens b JOIN veiculos v ON v.bem_id = b.cod_interno
            WHERE json_extract(v.dados_origem, '$.upload_id') = ?
        """, (upload_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Upload não encontrado")
    if "patrimonio:admin" not in user["roles"] and row["filial"] != user["filial"]:
        raise HTTPException(403, "Filial diferente da sua")

    # encontra o arquivo no disco
    matches = list(UPLOAD_DIR.glob(f"{upload_id}.*"))
    if not matches:
        raise HTTPException(404, "Arquivo não está mais no disco")
    arquivo = matches[0]

    nome_original = row["filename_original"] or arquivo.name

    # MIME por extensão
    ext_to_mime = {
        ".pdf": "application/pdf",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png",
    }
    media_type = ext_to_mime.get(arquivo.suffix.lower(), "application/octet-stream")

    disposition = "inline" if inline else "attachment"
    return FileResponse(
        arquivo,
        media_type=media_type,
        headers={"Content-Disposition": f'{disposition}; filename="{nome_original}"'},
    )


# ============================================================
# Bens multi-categoria (máquina / informática / móveis / imóvel)
# Núcleo em `bens`; campos próprios em bens.atributos (JSON).
# 'veiculo' continua com seus próprios endpoints (acima) + tabela `veiculos`.
# Estes endpoints NUNCA tocam `veiculos` — logo, não afetam o contrato /fleet.
# ============================================================

def _parse_atributos(raw) -> dict:
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _bem_to_dto(row: dict) -> dict:
    """Row unificado (bens LEFT JOIN veiculos) -> DTO de bem (qualquer categoria)."""
    cat = row.get("categoria") or "veiculo"
    placa = row.get("placa")
    marca, modelo = row.get("marca"), row.get("modelo")
    if cat == "veiculo":
        nome = placa or (f"{marca or ''} {modelo or ''}").strip() or row.get("descricao") or "—"
    else:
        nome = row.get("descricao") or (f"{marca or ''} {modelo or ''}").strip() or "—"
    return {
        "id": row.get("bem_id") or row.get("cod_interno"),
        "codigo_patrimonial": row.get("codigo_patrimonial"),
        "categoria": cat,
        "categoria_label": CAT_LABEL.get(cat, cat),
        "tipo_bem": row.get("tipo_bem") or "veiculo",
        "exige_crlv": bool(row.get("exige_crlv", 1)),
        "descricao": row.get("descricao"),
        "nome_exibicao": nome,
        "filial": row.get("filial"),
        "filial_id": row.get("filial"),
        "centro_custo": row.get("centro_custo"),
        "responsavel_nome": row.get("responsavel_nome"),
        "responsavel_user_id": row.get("responsavel_user_id"),
        "estado_operacional": row.get("estado_operacional"),
        "status": row.get("status"),
        "ativo": row.get("deleted_at") is None and row.get("status") != "baixado",
        "data_aquisicao": row.get("data_aquisicao"),
        "valor_aquisicao": row.get("valor_aquisicao"),
        "fornecedor": row.get("fornecedor"),
        "nf_numero": row.get("nf_numero"),
        "nf_chave": row.get("nf_chave"),
        "valor_residual": row.get("valor_residual"),
        "vida_util_meses": row.get("vida_util_meses"),
        "metodo_depreciacao": row.get("metodo_depreciacao"),
        "atributos": _parse_atributos(row.get("atributos")),
        # campos de veículo — None para as demais categorias
        "placa": placa, "marca": marca, "modelo": modelo,
        "ano": row.get("ano_fabricacao"), "km_atual": row.get("km_atual"),
        "vencimento_crlv": row.get("vencimento_crlv"),
        "km_ultima_troca": row.get("km_ultima_troca"),
        "intervalo_meses": row.get("intervalo_meses"),
        "data_ultima_troca": row.get("data_ultima_troca"),
        "margem_dias_aviso": row.get("margem_dias_aviso"),
    }


_BEM_SELECT = """
    SELECT b.cod_interno AS bem_id, b.codigo_patrimonial, b.categoria, b.exige_crlv,
           b.tipo_bem, b.filial, b.centro_custo, b.responsavel_user_id, b.responsavel_nome,
           b.estado_operacional, b.status, b.deleted_at, b.descricao,
           b.data_aquisicao, b.valor_aquisicao, b.fornecedor, b.nf_numero, b.nf_chave,
           b.valor_residual, b.vida_util_meses, b.metodo_depreciacao, b.atributos,
           v.placa, v.marca, v.modelo, v.ano_fabricacao, v.km_atual, v.vencimento_crlv,
           v.km_ultima_troca, v.intervalo_meses, v.data_ultima_troca, v.margem_dias_aviso
    FROM bens b LEFT JOIN veiculos v ON v.bem_id = b.cod_interno
"""


@app.get("/api/patrimonio/bens")
def listar_bens(
    categoria: Optional[str] = None,
    filial: Optional[int] = None,
    status: Optional[str] = None,
    busca: Optional[str] = None,
    incluir_baixados: bool = False,
    authorization: Optional[str] = Header(None),
):
    """Listagem unificada de TODAS as categorias de bem (inclui veículos)."""
    user = auth_check(authorization)
    require_role(user, "patrimonio:listar")
    # filtros compartilhados (tudo MENOS categoria) — usados também na contagem das abas
    conds: list = []
    params: list = []
    if status == "baixado":
        conds.append("b.deleted_at IS NOT NULL")
    elif not incluir_baixados:
        conds.append("b.deleted_at IS NULL")
    if filial:
        conds.append("b.filial = ?")
        params.append(filial)
    if status and status != "baixado":
        conds.append("b.status = ? AND b.deleted_at IS NULL")
        params.append(status)
    if busca:
        like = f"%{busca.upper()}%"
        conds.append("(UPPER(COALESCE(v.placa,'')) LIKE ? OR UPPER(COALESCE(v.modelo,'')) LIKE ?"
                     " OR UPPER(COALESCE(v.marca,'')) LIKE ? OR UPPER(COALESCE(b.descricao,'')) LIKE ?"
                     " OR UPPER(COALESCE(b.codigo_patrimonial,'')) LIKE ?)")
        params += [like, like, like, like, like]
    where = " AND ".join(conds) if conds else "1=1"

    sql = _BEM_SELECT + " WHERE " + where
    main_params = list(params)
    if categoria and categoria in CATEGORIAS:
        sql += " AND b.categoria = ?"
        main_params.append(categoria)
    sql += " ORDER BY b.categoria, b.codigo_patrimonial"

    conn = db()
    rows = [dict(r) for r in conn.execute(sql, main_params).fetchall()]
    # contagem por categoria sobre TODO o inventário (mesmos filtros, menos categoria)
    por_categoria = {c: 0 for c in CATEGORIAS}
    cnt_sql = ("SELECT b.categoria AS categoria, COUNT(*) AS n "
               "FROM bens b LEFT JOIN veiculos v ON v.bem_id = b.cod_interno "
               "WHERE " + where + " GROUP BY b.categoria")
    for r in conn.execute(cnt_sql, params).fetchall():
        por_categoria[r["categoria"]] = r["n"]
    conn.close()

    bens = [_bem_to_dto(r) for r in rows]
    today = date.today()
    def _venc_30(b):
        if b["categoria"] != "veiculo" or not b.get("vencimento_crlv"):
            return False
        try:
            return 0 <= (date.fromisoformat(b["vencimento_crlv"]) - today).days <= 30
        except Exception:
            return False
    kpis = {
        "total": len(bens),
        "ativos": sum(1 for b in bens if b["ativo"]),
        "valor_total": round(sum(b["valor_aquisicao"] or 0 for b in bens), 2),
        "vencendo_30d": sum(1 for b in bens if _venc_30(b)),
        "em_manutencao": sum(1 for b in bens if b["estado_operacional"] == "em_manutencao"),
    }
    return {"bens": bens, "kpis": kpis, "por_categoria": por_categoria}


@app.get("/api/patrimonio/bens/{bem_id}")
def obter_bem(bem_id: str, authorization: Optional[str] = Header(None)):
    """Detalhe unificado de um bem (qualquer categoria) + depreciação + manut. predial."""
    user = auth_check(authorization)
    require_role(user, "patrimonio:listar")
    conn = db()
    row = conn.execute(_BEM_SELECT + " WHERE b.cod_interno = ?", (bem_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Bem não encontrado")
    row = dict(row)
    dto = _bem_to_dto(row)
    # depreciação (None p/ imóvel alugado ou bem sem dados de aquisição)
    dto["depreciacao"] = calcular_depreciacao({
        "valor_aquisicao": row.get("valor_aquisicao"),
        "data_aquisicao": row.get("data_aquisicao"),
        "vida_util_meses": row.get("vida_util_meses"),
        "valor_residual": row.get("valor_residual"),
        "metodo_depreciacao": row.get("metodo_depreciacao"),
    })
    if dto["categoria"] == "imovel":
        mps = conn.execute(
            "SELECT * FROM manutencao_predial WHERE bem_id = ? ORDER BY COALESCE(data_servico,'') DESC, criado_em DESC",
            (bem_id,)).fetchall()
        dto["manutencao_predial"] = [dict(m) for m in mps]
    conn.close()
    return dto


class BemIn(BaseModel):
    categoria: str
    descricao: str
    filial: int
    centro_custo: str
    data_aquisicao: Optional[str] = None
    valor_aquisicao: Optional[float] = Field(default=None, ge=0)
    fornecedor: Optional[str] = None
    nf_numero: Optional[str] = None
    nf_chave: Optional[str] = None
    valor_residual: Optional[float] = Field(default=None, ge=0)
    vida_util_meses: Optional[int] = Field(default=None, ge=1)
    estado_operacional: Optional[str] = None
    atributos: dict = Field(default_factory=dict)


@app.post("/api/patrimonio/bens/cadastrar")
def cadastrar_bem(
    body: BemIn,
    authorization: Optional[str] = Header(None),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """Cadastra um bem NÃO-veículo (máquina/informática/móveis/imóvel).
    Veículo (e empilhadeira) continuam em /api/patrimonio/veiculos/cadastrar."""
    user = auth_check(authorization)
    require_role(user, "patrimonio:cadastrar")
    now = time.time()
    if idempotency_key and idempotency_key in IDEMPOTENCY_CACHE:
        ts0, cached = IDEMPOTENCY_CACHE[idempotency_key]
        if now - ts0 < IDEMPOTENCY_TTL:
            return JSONResponse(cached, headers={"X-Idempotent-Replay": "true"})

    data = body.model_dump()
    categoria = data["categoria"]
    if categoria not in CATEGORIAS_NAO_VEICULO:
        raise HTTPException(422, f"categoria deve ser uma de: {', '.join(CATEGORIAS_NAO_VEICULO)}")
    erros = validar_dados(data, categoria, exige_crlv=False)
    if erros:
        raise HTTPException(422, {"erros": erros})

    atributos = data.get("atributos") or {}
    # só imóvel ALUGADO DE TERCEIROS não imobiliza (próprio — mesmo alugado a terceiros — deprecia)
    nao_imobiliza = (categoria == "imovel"
                     and atributos.get("tipo_imovel") in ("terceiros_alugado", "alugado"))
    vida_util = data.get("vida_util_meses")
    if nao_imobiliza:
        vida_util, metodo = None, "nao_aplicavel"
    else:
        if vida_util is None:
            vida_util = VIDA_UTIL_PADRAO.get(categoria, 120)
        metodo = "linear"
    estado_op = data.get("estado_operacional") or "disponivel"

    conn = db()
    try:
        cur = conn.cursor()
        bem_id = uuid.uuid4().hex
        ts = now_iso()
        codigo_pat = gerar_codigo_patrimonial(cur, categoria)
        cur.execute("""
            INSERT INTO bens (cod_interno, codigo_patrimonial, filial, centro_custo,
                              responsavel_user_id, responsavel_nome, data_aquisicao,
                              valor_aquisicao, fornecedor, nf_numero, nf_chave,
                              valor_residual, vida_util_meses, metodo_depreciacao,
                              estado_operacional, tipo_bem, categoria, exige_crlv, descricao,
                              atributos, status, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,'ativo',?,?)
        """, (bem_id, codigo_pat, data["filial"], data["centro_custo"], user["user_id"],
              user["nome"], data.get("data_aquisicao"), data.get("valor_aquisicao"),
              data.get("fornecedor"), data.get("nf_numero"), data.get("nf_chave"),
              data.get("valor_residual") or 0, vida_util, metodo, estado_op,
              "equipamento", categoria, (data.get("descricao") or "").strip(),
              json.dumps(atributos), ts, ts))
        cur.execute("""
            INSERT INTO movimentacoes (id, bem_id, tipo, para_filial, para_responsavel,
                                       para_estado, valor, data_evento, executado_por,
                                       executado_por_nome, created_at)
            VALUES (?,?,'aquisicao',?,?,?,?,?,?,?,?)
        """, (uuid.uuid4().hex, bem_id, data["filial"], user["user_id"], estado_op,
              data.get("valor_aquisicao"), data.get("data_aquisicao") or ts[:10],
              user["user_id"], user["nome"], ts))
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        raise HTTPException(409, f"Conflito: {e}")
    finally:
        conn.close()

    resp = {"bem_id": bem_id, "codigo_patrimonial": codigo_pat, "categoria": categoria,
            "descricao": (data.get("descricao") or "").strip(), "created_at": ts}
    if idempotency_key:
        IDEMPOTENCY_CACHE[idempotency_key] = (now, resp)
    return resp


class BemPatch(BaseModel):
    descricao: Optional[str] = None
    filial: Optional[int] = None
    centro_custo: Optional[str] = None
    responsavel_user_id: Optional[int] = None
    responsavel_nome: Optional[str] = None
    estado_operacional: Optional[str] = None
    status: Optional[str] = None
    data_aquisicao: Optional[str] = None
    valor_aquisicao: Optional[float] = Field(default=None, ge=0)
    fornecedor: Optional[str] = None
    nf_numero: Optional[str] = None
    nf_chave: Optional[str] = None
    valor_residual: Optional[float] = Field(default=None, ge=0)
    vida_util_meses: Optional[int] = Field(default=None, ge=1)
    atributos: Optional[dict] = None


_BEM_PATCH_FIELDS = {"descricao", "filial", "centro_custo", "responsavel_user_id",
                     "responsavel_nome", "estado_operacional", "status", "data_aquisicao",
                     "valor_aquisicao", "fornecedor", "nf_numero", "nf_chave",
                     "valor_residual", "vida_util_meses"}


@app.patch("/api/patrimonio/bens/{bem_id}")
def editar_bem(bem_id: str, body: BemPatch, authorization: Optional[str] = Header(None)):
    """Edita núcleo + atributos de um bem NÃO-veículo. (Veículo usa o PATCH /veiculos/{id}.)"""
    user = auth_check(authorization)
    require_role(user, "patrimonio:cadastrar")
    raw = {k: v for k, v in body.model_dump().items() if v is not None}
    if not raw:
        raise HTTPException(422, "body vazio — informe ao menos um campo")
    if "status" in raw and raw["status"] not in ("ativo", "manutencao", "vencido", "baixado"):
        raise HTTPException(422, "status inválido")
    if "data_aquisicao" in raw:
        try:
            date.fromisoformat(raw["data_aquisicao"])
        except ValueError:
            raise HTTPException(422, "data_aquisicao deve ser YYYY-MM-DD")

    conn = db()
    atual = conn.execute("SELECT categoria FROM bens WHERE cod_interno = ?", (bem_id,)).fetchone()
    if not atual:
        conn.close()
        raise HTTPException(404, "Bem não encontrado")
    if (atual["categoria"] or "veiculo") == "veiculo":
        conn.close()
        raise HTTPException(422, "Bem da categoria veículo — edite por /api/patrimonio/veiculos/{id}")

    sets, params = [], []
    for k in _BEM_PATCH_FIELDS:
        if k in raw:
            sets.append(f"{k} = ?")
            params.append(raw[k])
    if "atributos" in raw:
        sets.append("atributos = ?")
        params.append(json.dumps(raw["atributos"]))
    sets.append("updated_at = ?")
    params.append(now_iso())
    params.append(bem_id)
    cur = conn.cursor()
    cur.execute(f"UPDATE bens SET {', '.join(sets)} WHERE cod_interno = ?", params)
    conn.commit()
    conn.close()
    return {"id": bem_id, "updated": raw}


# ---------- manutenção predial (categoria imóvel) ----------
_MP_TIPOS = ("eletrica", "hidraulica", "ar_condicionado", "dedetizacao", "alvara_bombeiros", "outro")
MP_LABEL = {"eletrica": "Elétrica", "hidraulica": "Hidráulica", "ar_condicionado": "Ar-condicionado",
            "dedetizacao": "Dedetização", "alvara_bombeiros": "Alvará / Bombeiros", "outro": "Outro"}


class ManutPredialIn(BaseModel):
    tipo: str
    descricao: Optional[str] = None
    fornecedor: Optional[str] = None
    custo: Optional[float] = Field(default=None, ge=0)
    data_servico: Optional[str] = None
    proximo_vencimento: Optional[str] = None
    observacao: Optional[str] = None


@app.get("/api/patrimonio/bens/{bem_id}/manutencao-predial")
def listar_manut_predial(bem_id: str, authorization: Optional[str] = Header(None)):
    user = auth_check(authorization)
    require_role(user, "patrimonio:listar")
    conn = db()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM manutencao_predial WHERE bem_id = ? ORDER BY COALESCE(data_servico,'') DESC, criado_em DESC",
        (bem_id,)).fetchall()]
    conn.close()
    return {"bem_id": bem_id, "manutencao_predial": rows}


@app.post("/api/patrimonio/bens/{bem_id}/manutencao-predial")
def add_manut_predial(bem_id: str, body: ManutPredialIn, authorization: Optional[str] = Header(None)):
    user = auth_check(authorization)
    require_role(user, "patrimonio:cadastrar")
    if body.tipo not in _MP_TIPOS:
        raise HTTPException(422, f"tipo deve ser um de: {', '.join(_MP_TIPOS)}")
    for campo, val in (("data_servico", body.data_servico), ("proximo_vencimento", body.proximo_vencimento)):
        if val:
            try:
                date.fromisoformat(val)
            except ValueError:
                raise HTTPException(422, f"{campo} deve ser YYYY-MM-DD")
    conn = db()
    bem = conn.execute(
        "SELECT categoria FROM bens WHERE cod_interno = ? AND deleted_at IS NULL", (bem_id,)).fetchone()
    if not bem:
        conn.close()
        raise HTTPException(404, "Bem não encontrado")
    if (bem["categoria"] or "") != "imovel":
        conn.close()
        raise HTTPException(422, "Manutenção predial só se aplica a imóveis")
    mp_id = uuid.uuid4().hex
    ts = now_iso()
    conn.execute("""
        INSERT INTO manutencao_predial (id, bem_id, tipo, descricao, fornecedor, custo,
                                        data_servico, proximo_vencimento, observacao,
                                        criado_por, criado_por_nome, criado_em)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (mp_id, bem_id, body.tipo, body.descricao, body.fornecedor, body.custo,
          body.data_servico, body.proximo_vencimento, body.observacao,
          user["user_id"], user["nome"], ts))
    conn.commit()
    conn.close()
    return {"id": mp_id, "tipo": body.tipo, "proximo_vencimento": body.proximo_vencimento}


@app.delete("/api/patrimonio/manutencao-predial/{mp_id}")
def remover_manut_predial(mp_id: str, authorization: Optional[str] = Header(None)):
    user = auth_check(authorization)
    require_role(user, "patrimonio:cadastrar")
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM manutencao_predial WHERE id = ?", (mp_id,))
    if cur.rowcount == 0:
        conn.close()
        raise HTTPException(404, "Registro não encontrado")
    conn.commit()
    conn.close()
    return {"ok": True}


# ============================================================
# Contrato com módulo OIL-CHANGE (sessão paralela do Renato)
# Alias /api/v1/fleet/* roteado pra patrimonio/veiculos.
# Resposta JSON renomeada: bem_id->id, filial->filial_id,
# responsavel_user_id->user_id, status->ativo (bool).
# ============================================================

def _to_fleet_dto(row: dict) -> dict:
    """Converte row patrimonio.veiculos -> contrato fleet.
    Campos de manutenção podem ser null (veículo não entrou no oil-change ainda).
    """
    return {
        "id": row["bem_id"] if "bem_id" in row else row.get("cod_interno"),
        "placa": row.get("placa"),
        "marca": row.get("marca"),
        "modelo": row.get("modelo"),
        "ano": row.get("ano_fabricacao"),
        "ano_modelo": row.get("ano_modelo"),
        "cor": row.get("cor"),
        "combustivel": row.get("combustivel"),
        "cilindradas": row.get("cilindradas"),
        "renavam": row.get("renavam"),
        "chassi": row.get("chassi"),
        "filial_id": row.get("filial"),
        "user_id": row.get("responsavel_user_id"),
        "responsavel_nome": row.get("responsavel_nome"),
        "centro_custo": row.get("centro_custo"),
        "tipo_bem": row.get("tipo_bem") or "veiculo",
        "codigo_patrimonial": row.get("codigo_patrimonial"),
        "estado_operacional": row.get("estado_operacional"),
        # bloco patrimonial
        "data_aquisicao": row.get("data_aquisicao"),
        "valor_aquisicao": row.get("valor_aquisicao"),
        "fornecedor": row.get("fornecedor"),
        "nf_numero": row.get("nf_numero"),
        "nf_chave": row.get("nf_chave"),
        "valor_residual": row.get("valor_residual"),
        "vida_util_meses": row.get("vida_util_meses"),
        "km_atual": row.get("km_atual"),
        "intervalo_km": row.get("intervalo_km"),
        "intervalo_meses": row.get("intervalo_meses"),
        "km_ultima_troca": row.get("km_ultima_troca"),
        "data_ultima_troca": row.get("data_ultima_troca"),
        "margem_km_aviso": row.get("margem_km_aviso"),
        "margem_dias_aviso": row.get("margem_dias_aviso"),
        "ativo": row.get("status") not in ("baixado",) and row.get("deleted_at") is None,
        "status": row.get("status"),
        "vencimento_crlv": row.get("vencimento_crlv"),
    }


@app.get("/api/v1/fleet/vehicles")
def fleet_list(
    user_id: Optional[int] = None,
    filial_id: Optional[int] = None,
    ativo: Optional[bool] = None,
    authorization: Optional[str] = Header(None),
):
    user = auth_check(authorization)
    require_role(user, "patrimonio:listar")

    sql = """
        SELECT b.cod_interno AS bem_id, b.filial, b.responsavel_user_id, b.status, b.deleted_at, b.tipo_bem,
               b.codigo_patrimonial, b.estado_operacional, b.responsavel_nome,
               v.placa, v.marca, v.modelo, v.ano_fabricacao, v.km_atual,
               v.intervalo_km, v.intervalo_meses, v.km_ultima_troca,
               v.data_ultima_troca, v.margem_km_aviso, v.margem_dias_aviso,
               v.vencimento_crlv
        FROM bens b JOIN veiculos v ON v.bem_id = b.cod_interno
        WHERE b.deleted_at IS NULL
    """
    params: list = []
    if user_id is not None:
        sql += " AND b.responsavel_user_id = ?"
        params.append(user_id)
    if filial_id is not None:
        sql += " AND b.filial = ?"
        params.append(filial_id)
    if ativo is True:
        sql += " AND b.status != 'baixado'"
    elif ativo is False:
        sql += " AND b.status = 'baixado'"

    conn = db()
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    return {"vehicles": [_to_fleet_dto(r) for r in rows]}


@app.get("/api/v1/fleet/vehicles/{vehicle_id}")
def fleet_detail(vehicle_id: str, authorization: Optional[str] = Header(None)):
    user = auth_check(authorization)
    require_role(user, "patrimonio:listar")
    conn = db()
    row = conn.execute("""
        SELECT b.cod_interno AS bem_id, b.filial, b.responsavel_user_id, b.responsavel_nome,
               b.status, b.deleted_at, b.tipo_bem, b.centro_custo,
               b.codigo_patrimonial, b.estado_operacional, b.data_aquisicao,
               b.valor_aquisicao, b.fornecedor, b.nf_numero, b.nf_chave,
               b.valor_residual, b.vida_util_meses, b.metodo_depreciacao,
               v.placa, v.marca, v.modelo, v.ano_fabricacao, v.ano_modelo, v.cor,
               v.combustivel, v.cilindradas, v.km_atual, v.renavam, v.chassi,
               v.intervalo_km, v.intervalo_meses, v.km_ultima_troca,
               v.data_ultima_troca, v.margem_km_aviso, v.margem_dias_aviso,
               v.vencimento_crlv
        FROM bens b JOIN veiculos v ON v.bem_id = b.cod_interno
        WHERE b.cod_interno = ?
    """, (vehicle_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Veículo não encontrado")
    dto = _to_fleet_dto(dict(row))
    dto["depreciacao"] = calcular_depreciacao(dict(row))
    return dto


class FleetPatch(BaseModel):
    """
    Aceita qualquer subset dos campos de manutenção.
    O caso comum (oil-change confirma troca) manda km_ultima_troca + data_ultima_troca,
    mas o gestor da frota também pode editar intervalo / margens pela UI.
    """
    intervalo_km: Optional[int] = Field(default=None, ge=0)
    intervalo_meses: Optional[int] = Field(default=None, ge=0)
    km_ultima_troca: Optional[int] = Field(default=None, ge=0)
    data_ultima_troca: Optional[str] = None  # YYYY-MM-DD
    margem_km_aviso: Optional[int] = Field(default=None, ge=0)
    margem_dias_aviso: Optional[int] = Field(default=None, ge=0)


@app.patch("/api/v1/fleet/vehicles/{vehicle_id}")
def fleet_patch(
    vehicle_id: str,
    body: FleetPatch,
    authorization: Optional[str] = Header(None),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """
    PATCH parcial — atualiza só os campos enviados no body.
    - oil-change usa pra confirmar troca (km_ultima_troca + data_ultima_troca)
    - gestor usa pra ajustar intervalo / margens
    Idempotency-Key opcional reusa cache existente.
    """
    user = auth_check(authorization)
    require_role(user, "patrimonio:listar")

    # extrai apenas os campos enviados (não-None)
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(422, "body vazio — informe ao menos um campo")

    # valida data se veio
    if "data_ultima_troca" in updates:
        try:
            date.fromisoformat(updates["data_ultima_troca"])
        except ValueError:
            raise HTTPException(422, "data_ultima_troca deve ser YYYY-MM-DD")

    # idempotency
    if idempotency_key and idempotency_key in IDEMPOTENCY_CACHE:
        ts, cached = IDEMPOTENCY_CACHE[idempotency_key]
        if time.time() - ts < IDEMPOTENCY_TTL:
            return JSONResponse(cached, headers={"X-Idempotent-Replay": "true"})

    set_clause = ", ".join(f"{k} = ?" for k in updates) + ", updated_at = ?"
    params = list(updates.values()) + [now_iso(), vehicle_id]

    conn = db()
    cur = conn.cursor()
    cur.execute(f"UPDATE veiculos SET {set_clause} WHERE bem_id = ?", params)
    if cur.rowcount == 0:
        conn.close()
        raise HTTPException(404, "Veículo não encontrado")
    cur.execute("UPDATE bens SET updated_at = ? WHERE cod_interno = ?", (now_iso(), vehicle_id))
    conn.commit()
    conn.close()

    resp = {"id": vehicle_id, "updated": updates, "updated_at": now_iso()}
    if idempotency_key:
        IDEMPOTENCY_CACHE[idempotency_key] = (time.time(), resp)
    return resp


# ---------- frontend estático ----------
@app.get("/")
def root():
    return FileResponse(BASE_DIR / "frontend.html")


@app.get("/manifest.json")
def manifest():
    return FileResponse(BASE_DIR / "manifest.json", media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    # Service-Worker-Allowed header pra escopo de "/"
    return FileResponse(
        BASE_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


# servir fixtures para o front baixar exemplos
app.mount("/fixtures", StaticFiles(directory=BASE_DIR / "fixtures"), name="fixtures")


if __name__ == "__main__":
    print(f"\n  Demo Cadastro Veicular  ->  http://{HOST}:{PORT}")
    print(f"  PIN de acesso           ->  {DEMO_PIN}")
    print(f"  Claude Vision           ->  {'REAL (' + VISION_MODEL + ')' if USE_REAL_VISION else 'MOCK (fixtures)'}")
    print(f"  Dados em                ->  {DATA_DIR}")
    print(f"  Subiu em                ->  {now_brt_display()} BRT\n")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
