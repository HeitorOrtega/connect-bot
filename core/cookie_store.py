import sqlite3
from datetime import datetime
from pathlib import Path
from core.crypto import encrypt_str, decrypt_str

BASE_DIR = Path(__file__).resolve().parents[1]   
DB_PATH = BASE_DIR / "connect.db"

def _conn():
    return sqlite3.connect(str(DB_PATH))

def criar_tabela_cookies():
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cookies_painel (
            painel TEXT PRIMARY KEY,
            cookie_enc TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def salvar_cookie(painel: str, cookie: str):
    """
    Salva/atualiza cookie criptografado no sqlite.
    """
    painel = (painel or "").upper().strip()
    if not painel:
        raise ValueError("painel inválido")
    if not cookie or not cookie.strip():
        raise ValueError("cookie vazio")

    criar_tabela_cookies()
    enc = encrypt_str(cookie.strip())

    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO cookies_painel (painel, cookie_enc, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(painel) DO UPDATE SET
            cookie_enc=excluded.cookie_enc,
            updated_at=excluded.updated_at
    """, (painel, enc, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

def carregar_cookie(painel: str):
    """
    Carrega cookie do sqlite (descriptografa). Retorna None se não existir.
    """
    painel = (painel or "").upper().strip()
    if not painel:
        return None

    criar_tabela_cookies()
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT cookie_enc FROM cookies_painel WHERE painel=?", (painel,))
    row = cur.fetchone()
    conn.close()

    if not row:
        return None

    return decrypt_str(row[0])
