import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta

from core.crypto import encrypt_str, decrypt_str

# ==========================
# DB PATH (mais seguro no Railway)
# ==========================
BASE_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BASE_DIR / "connect.db"

BR_TIMEZONE = timezone(timedelta(hours=-3))


def conectar() -> sqlite3.Connection:
    """
    Conexão com boas configs para evitar 'database is locked'
    """
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


# ==========================
# FECHAMENTOS
# ==========================
def criar_tabela():
    """
    Cria a tabela fechamentos e garante colunas necessárias.
    (migração segura: se a coluna já existir, ignora)
    """
    with conectar() as conn:
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS fechamentos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                painel TEXT NOT NULL,
                creditos REAL DEFAULT 0
            )
        """)

        colunas = [
            ("clientes", "INTEGER DEFAULT 0"),
            ("testes", "INTEGER DEFAULT 0"),
            ("novos", "INTEGER DEFAULT 0"),
            ("renovacoes", "INTEGER DEFAULT 0"),
            ("usuario", "TEXT"),
            ("api_ok", "INTEGER DEFAULT 1"),
            ("data", "TEXT"),
            ("horario", "TEXT"),
        ]

        for nome, tipo in colunas:
            try:
                cur.execute(f"ALTER TABLE fechamentos ADD COLUMN {nome} {tipo}")
            except sqlite3.OperationalError:
                pass


def salvar_fechamento(painel, creditos, clientes, testes, novos, renovacoes, usuario="AUTO", api_ok=1):
    agora = datetime.now(BR_TIMEZONE)
    data_str = agora.strftime("%Y-%m-%d")
    hora_str = agora.strftime("%H:%M")

    with conectar() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO fechamentos
            (painel, creditos, clientes, testes, novos, renovacoes, usuario, api_ok, data, horario)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(painel),
            float(creditos or 0),
            int(clientes or 0),
            int(testes or 0),
            int(novos or 0),
            int(renovacoes or 0),
            str(usuario),
            int(api_ok),
            data_str,
            hora_str
        ))


def buscar_totais_fechamentos():
    with conectar() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                SUM(creditos),
                SUM(clientes),
                SUM(testes),
                SUM(novos),
                SUM(renovacoes)
            FROM fechamentos
        """)
        row = cur.fetchone() or (0, 0, 0, 0, 0)

    return {
        "cr": row[0] or 0,
        "cl": row[1] or 0,
        "ts": row[2] or 0,
        "nv": row[3] or 0,
        "rn": row[4] or 0
    }


# ==========================
# COOKIES DO PAINEL (CRIPTOGRAFADO)
# ==========================
def criar_tabela_cookies():
    with conectar() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cookies_painel (
                painel TEXT PRIMARY KEY,
                cookie_enc TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)


def salvar_cookie_painel(painel: str, cookie: str):
    painel = (painel or "").upper().strip()
    if not painel:
        raise ValueError("painel inválido")
    if not cookie or not cookie.strip():
        raise ValueError("cookie vazio")

    criar_tabela_cookies()
    enc = encrypt_str(cookie.strip())

    with conectar() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO cookies_painel (painel, cookie_enc, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(painel) DO UPDATE SET
                cookie_enc=excluded.cookie_enc,
                updated_at=excluded.updated_at
        """, (painel, enc, datetime.utcnow().isoformat()))


def carregar_cookie_painel(painel: str):
    painel = (painel or "").upper().strip()
    if not painel:
        return None

    criar_tabela_cookies()

    with conectar() as conn:
        cur = conn.cursor()
        cur.execute("SELECT cookie_enc FROM cookies_painel WHERE painel=?", (painel,))
        row = cur.fetchone()

    if not row:
        return None

    return decrypt_str(row[0])
