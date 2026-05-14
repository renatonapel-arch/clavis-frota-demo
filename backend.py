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

try:
    import anthropic
    _anthropic_available = True
except ImportError:
    _anthropic_available = False

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

# Vision toggle
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
USE_REAL_VISION = bool(ANTHROPIC_API_KEY) and _anthropic_available and \
    os.getenv("USE_REAL_VISION", "true").lower() == "true"
VISION_MODEL = os.getenv("VISION_MODEL", "claude-haiku-4-5")
_vision_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if USE_REAL_VISION else None

PLACA_MERCOSUL_RE = re.compile(r"^[A-Z]{3}[0-9][A-Z][0-9]{2}$|^[A-Z]{3}[0-9][A-Z][0-9]{3}$")
PLACA_ANTIGA_RE = re.compile(r"^[A-Z]{3}[0-9]{4}$")
RENAVAM_RE = re.compile(r"^[0-9]{11}$")
CHASSI_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

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
          filial               INTEGER NOT NULL,
          centro_custo         TEXT NOT NULL,
          responsavel_user_id  INTEGER NOT NULL,
          responsavel_nome     TEXT NOT NULL,
          data_aquisicao       TEXT,
          status               TEXT NOT NULL DEFAULT 'ativo'
                               CHECK (status IN ('ativo','manutencao','vencido','baixado')),
          created_at           TEXT NOT NULL,
          updated_at           TEXT NOT NULL,
          deleted_at           TEXT
        );

        CREATE TABLE IF NOT EXISTS veiculos (
          bem_id               TEXT PRIMARY KEY REFERENCES bens(cod_interno) ON DELETE CASCADE,
          placa                TEXT NOT NULL,
          renavam              TEXT NOT NULL UNIQUE,
          chassi               TEXT NOT NULL UNIQUE,
          marca                TEXT,
          modelo               TEXT,
          ano_fabricacao       INTEGER,
          ano_modelo           INTEGER,
          cor                  TEXT,
          combustivel          TEXT,
          cilindradas          INTEGER,
          km_atual             INTEGER DEFAULT 0,
          vencimento_crlv      TEXT NOT NULL,
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
    """)

    # migration idempotente — adiciona colunas novas em DBs já existentes
    cur.execute("PRAGMA table_info(veiculos)")
    cols = {row["name"] for row in cur.fetchall()}
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

    # seed filiais
    seed_sql = (BASE_DIR / "seed_filiais.sql").read_text(encoding="utf-8")
    cur.executescript(seed_sql)

    # seed dos 5 veículos demo (idempotente — só insere se tabela vazia)
    veiculos_count = cur.execute("SELECT COUNT(*) FROM veiculos").fetchone()[0]
    if veiculos_count == 0:
        seed_veic = (BASE_DIR / "seed_veiculos.sql")
        if seed_veic.exists():
            cur.executescript(seed_veic.read_text(encoding="utf-8"))

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
    Chama Claude Vision real (Opus 4.7).
    System prompt cacheado (prefix invariante = ~90% off em chamadas subsequentes).
    Schema JSON estruturado garante shape da resposta.
    """
    data_b64 = base64.standard_b64encode(file_bytes).decode("utf-8")
    if mime_type == "application/pdf":
        media_block = {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": data_b64},
        }
    else:
        # normaliza image/jpg -> image/jpeg
        mt = "image/jpeg" if mime_type in ("image/jpg",) else mime_type
        media_block = {
            "type": "image",
            "source": {"type": "base64", "media_type": mt, "data": data_b64},
        }

    last_err = None
    for tentativa in range(3):
        try:
            response = _vision_client.messages.create(
                model=VISION_MODEL,
                max_tokens=2048,
                system=[{
                    "type": "text",
                    "text": CRLV_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                output_config={"format": {"type": "json_schema", "schema": CRLV_OUTPUT_SCHEMA}},
                messages=[{
                    "role": "user",
                    "content": [
                        media_block,
                        {"type": "text", "text": "Extraia os dados deste CRLV e retorne o JSON."},
                    ],
                }],
            )
            # cache hit info (debug)
            usage = getattr(response, "usage", None)
            if usage:
                print(f"[vision {now_brt_display()}] in={usage.input_tokens} out={usage.output_tokens} "
                      f"cache_read={getattr(usage, 'cache_read_input_tokens', 0)} "
                      f"cache_write={getattr(usage, 'cache_creation_input_tokens', 0)}")

            for block in response.content:
                if block.type == "text":
                    return json.loads(block.text)
            raise RuntimeError("Vision retornou resposta sem bloco de texto")
        except (anthropic.APIConnectionError, anthropic.APIStatusError) as e:
            last_err = e
            if tentativa < 2:
                time.sleep(2 ** tentativa)
                continue
            raise
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Vision retornou JSON inválido: {e}") from e

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
    """Roteia para Vision real ou mock. Retorna (dados, fonte)."""
    if USE_REAL_VISION:
        try:
            return call_claude_vision_real(file_bytes, mime_type), "claude-vision-real"
        except Exception as e:
            print(f"[vision] real falhou ({e}), caindo no mock")
            return call_claude_vision_mock(filename), f"mock-fallback ({type(e).__name__})"
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


def auth_check(authorization: Optional[str]) -> dict:
    """PIN auth simples — em staging trocar por JWT real."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Não autenticado")
    token = authorization.removeprefix("Bearer ")
    if token != DEMO_PIN:
        raise HTTPException(401, "PIN inválido")
    return {
        "user_id": DEMO_USER_ID,
        "nome": DEMO_USER_NOME,
        "filial": DEMO_USER_FILIAL,
        "roles": DEMO_USER_ROLES,
    }


def require_role(user: dict, role: str) -> None:
    if role not in user["roles"]:
        raise HTTPException(403, f"Permissão negada: {role}")


def sanitize_vision(raw: dict) -> dict:
    """Filtra apenas campos esperados antes de gravar."""
    allowed = {"placa", "renavam", "chassi", "marca", "modelo", "ano_fabricacao",
               "ano_modelo", "cor", "combustivel", "cilindradas", "vencimento_crlv"}
    return {k: v for k, v in raw.items() if k in allowed}


def validar_dados(d: dict) -> list[str]:
    erros = []
    placa = d.get("placa", "").upper().replace("-", "").replace(" ", "")
    if not (PLACA_MERCOSUL_RE.match(placa) or PLACA_ANTIGA_RE.match(placa)):
        erros.append(f"placa inválida: {placa}")
    if not RENAVAM_RE.match(d.get("renavam", "")):
        erros.append("renavam deve ter 11 dígitos")
    if not CHASSI_RE.match(d.get("chassi", "")):
        erros.append("chassi inválido (17 chars alfanuméricos sem I/O/Q)")
    if not d.get("vencimento_crlv"):
        erros.append("vencimento_crlv obrigatório")
    return erros


# ---------- lifespan ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


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
        },
    }


@app.post("/api/auth/login")
def login(body: dict):
    if body.get("pin") != DEMO_PIN:
        raise HTTPException(401, "PIN inválido")
    return {
        "token": DEMO_PIN,
        "user": {"id": DEMO_USER_ID, "nome": DEMO_USER_NOME, "filial": DEMO_USER_FILIAL,
                 "roles": list(DEMO_USER_ROLES)},
    }


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
        "modelo": VISION_MODEL if USE_REAL_VISION and "real" in fonte else "fixture",
    }


class CadastroIn(BaseModel):
    upload_id: str
    placa: str
    renavam: str
    chassi: str
    marca: Optional[str] = None
    modelo: Optional[str] = None
    ano_fabricacao: Optional[int] = None
    ano_modelo: Optional[int] = None
    cor: Optional[str] = None
    combustivel: Optional[str] = None
    cilindradas: Optional[int] = None
    vencimento_crlv: str
    filial: int
    centro_custo: str
    km_atual: int = 0
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
    erros = validar_dados(data)
    if erros:
        raise HTTPException(422, {"erros": erros})

    placa_norm = data["placa"].upper().replace("-", "").replace(" ", "")

    # transação atômica bens + veiculos
    conn = db()
    try:
        cur = conn.cursor()
        # checa duplicata por placa+filial ativo (soft-delete aware)
        dup = cur.execute("""
            SELECT b.cod_interno FROM bens b
            JOIN veiculos v ON v.bem_id = b.cod_interno
            WHERE v.placa = ? AND b.filial = ? AND b.deleted_at IS NULL
        """, (placa_norm, data["filial"])).fetchone()
        if dup:
            raise HTTPException(409, f"Placa {placa_norm} já cadastrada na filial {data['filial']}")

        bem_id = uuid.uuid4().hex
        ts = now_iso()
        status = derive_status(data["vencimento_crlv"])

        cur.execute("""
            INSERT INTO bens (cod_interno, filial, centro_custo, responsavel_user_id,
                              responsavel_nome, status, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
        """, (bem_id, data["filial"], data["centro_custo"], user["user_id"],
              user["nome"], status, ts, ts))

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
            bem_id, placa_norm, data["renavam"], data["chassi"],
            data.get("marca"), data.get("modelo"),
            data.get("ano_fabricacao"), data.get("ano_modelo"),
            data.get("cor"), data.get("combustivel"), data.get("cilindradas"),
            data.get("km_atual", 0), data["vencimento_crlv"],
            json.dumps(data.get("dados_raw", {})),
            json.dumps({
                "user_id": user["user_id"],
                "timestamp": ts,
                "upload_id": data["upload_id"],
                "fonte": data.get("fonte") or "claude-vision-mock",
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
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        raise HTTPException(409, f"Conflito de unique constraint: {e}")
    finally:
        conn.close()

    resp = {
        "bem_id": bem_id,
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
    authorization: Optional[str] = Header(None),
):
    user = auth_check(authorization)
    require_role(user, "patrimonio:listar")

    sql = """
        SELECT b.cod_interno AS bem_id, v.placa, v.modelo, v.marca,
               b.filial, b.responsavel_nome, b.status,
               v.vencimento_crlv, v.km_atual
        FROM bens b
        JOIN veiculos v ON v.bem_id = b.cod_interno
        WHERE b.deleted_at IS NULL
    """
    params: list = []
    if filial:
        sql += " AND b.filial = ?"
        params.append(filial)
    if status:
        sql += " AND b.status = ?"
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
    row = conn.execute("""
        SELECT b.*, v.*
        FROM bens b JOIN veiculos v ON v.bem_id = b.cod_interno
        WHERE b.cod_interno = ? AND b.deleted_at IS NULL
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


_BENS_FIELDS = {"filial", "centro_custo", "responsavel_user_id", "responsavel_nome", "status"}
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
            SELECT v.placa, v.renavam, v.chassi, v.vencimento_crlv
            FROM veiculos v JOIN bens b ON b.cod_interno = v.bem_id
            WHERE v.bem_id = ? AND b.deleted_at IS NULL
        """, (bem_id,)).fetchone()
        conn.close()
        if not atual:
            raise HTTPException(404, "Veículo não encontrado")
        merged = {
            "placa": updates.get("placa", atual["placa"]),
            "renavam": updates.get("renavam", atual["renavam"]),
            "chassi": updates.get("chassi", atual["chassi"]),
            "vencimento_crlv": updates.get("vencimento_crlv", atual["vencimento_crlv"]),
        }
        erros = validar_dados(merged)
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
    user = auth_check(authorization)
    require_role(user, "patrimonio:admin")
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE bens SET deleted_at = ?, status = 'baixado' WHERE cod_interno = ?",
                (now_iso(), bem_id))
    if cur.rowcount == 0:
        conn.close()
        raise HTTPException(404, "Veículo não encontrado")
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------- documentos do veículo (item 4 — contrato com oil-change) ----------
@app.get("/api/v1/fleet/vehicles/{vehicle_id}/documents")
def fleet_documents(vehicle_id: str, authorization: Optional[str] = Header(None)):
    """
    Lista documentos do veículo (CRLV por enquanto; IPVA/seguro depois).
    Inclui status calculado: vigente, vencendo (≤30d), vencido.
    """
    user = auth_check(authorization)
    require_role(user, "patrimonio:listar")
    conn = db()
    row = conn.execute("""
        SELECT b.cod_interno AS bem_id, v.placa, v.vencimento_crlv,
               v.dados_origem
        FROM bens b JOIN veiculos v ON v.bem_id = b.cod_interno
        WHERE b.cod_interno = ? AND b.deleted_at IS NULL
    """, (vehicle_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Veículo não encontrado")

    venc_crlv = row["vencimento_crlv"]
    try:
        v_date = date.fromisoformat(venc_crlv)
        delta = (v_date - date.today()).days
        if delta < 0:
            crlv_status = "vencido"
        elif delta <= 30:
            crlv_status = "vencendo"
        else:
            crlv_status = "vigente"
    except Exception:
        crlv_status = "desconhecido"
        delta = None

    origem = json.loads(row["dados_origem"]) if row["dados_origem"] else {}
    upload_id = origem.get("upload_id")

    # checa se o arquivo ainda existe no disco
    arquivo_existe = bool(upload_id and list(UPLOAD_DIR.glob(f"{upload_id}.*")))

    documents = [{
        "type": "crlv",
        "vencimento": venc_crlv,
        "status": crlv_status,
        "dias_para_vencer": delta,
        "fonte": origem.get("fonte"),
        "upload_id": upload_id,
        "filename_original": origem.get("filename_original"),
        "arquivo_disponivel": arquivo_existe,
        "download_url": f"/api/patrimonio/uploads/{upload_id}" if (upload_id and arquivo_existe) else None,
        "extraido_em": origem.get("timestamp"),
    }]
    # placeholders pra IPVA / seguro (não implementados ainda)
    documents.append({"type": "ipva", "status": "nao_cadastrado"})
    documents.append({"type": "seguro", "status": "nao_cadastrado"})

    return {"vehicle_id": vehicle_id, "placa": row["placa"], "documents": documents}


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

    # busca o veículo dono do upload pra checar filial e pegar metadata
    conn = db()
    row = conn.execute("""
        SELECT b.filial, v.dados_origem FROM bens b
        JOIN veiculos v ON v.bem_id = b.cod_interno
        WHERE json_extract(v.dados_origem, '$.upload_id') = ? AND b.deleted_at IS NULL
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

    # nome original pra Content-Disposition
    origem = json.loads(row["dados_origem"]) if row["dados_origem"] else {}
    nome_original = origem.get("filename_original") or arquivo.name

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
        "filial_id": row.get("filial"),
        "user_id": row.get("responsavel_user_id"),
        "intervalo_km": row.get("intervalo_km"),
        "intervalo_meses": row.get("intervalo_meses"),
        "km_ultima_troca": row.get("km_ultima_troca"),
        "data_ultima_troca": row.get("data_ultima_troca"),
        "margem_km_aviso": row.get("margem_km_aviso"),
        "margem_dias_aviso": row.get("margem_dias_aviso"),
        "ativo": row.get("status") not in ("baixado",) and row.get("deleted_at") is None,
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
        SELECT b.cod_interno AS bem_id, b.filial, b.responsavel_user_id, b.status, b.deleted_at,
               v.placa, v.marca, v.modelo, v.ano_fabricacao,
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
               b.status, b.deleted_at,
               v.placa, v.marca, v.modelo, v.ano_fabricacao, v.ano_modelo, v.cor,
               v.combustivel, v.cilindradas, v.km_atual, v.renavam, v.chassi,
               v.intervalo_km, v.intervalo_meses, v.km_ultima_troca,
               v.data_ultima_troca, v.margem_km_aviso, v.margem_dias_aviso,
               v.vencimento_crlv
        FROM bens b JOIN veiculos v ON v.bem_id = b.cod_interno
        WHERE b.cod_interno = ? AND b.deleted_at IS NULL
    """, (vehicle_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Veículo não encontrado")
    return _to_fleet_dto(dict(row))


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
