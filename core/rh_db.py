import sqlite3
from datetime import datetime, timedelta, timezone, date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BASE_DIR / "connect.db"

BR_TIMEZONE = timezone(timedelta(hours=-3))


def _conn():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def rh_setup():
    with _conn() as conn:
        cur = conn.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS rh_staff (
            user_id INTEGER PRIMARY KEY,
            nome TEXT NOT NULL,
            ativo INTEGER NOT NULL DEFAULT 1,
            salario_mensal REAL NOT NULL DEFAULT 0,
            turno_ini TEXT NOT NULL,   -- "08:00"
            turno_fim TEXT NOT NULL,   -- "15:00"
            tolerancia_fim TEXT,       -- opcional (ex: "23:00")
            comissao_por_cliente REAL NOT NULL DEFAULT 0.75
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS rh_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            join_at TEXT NOT NULL,
            leave_at TEXT,
            FOREIGN KEY(user_id) REFERENCES rh_staff(user_id)
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS rh_daily (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dia TEXT NOT NULL,               -- "YYYY-MM-DD"
            user_id INTEGER NOT NULL,
            minutos_total INTEGER NOT NULL DEFAULT 0,
            minutos_turno INTEGER NOT NULL DEFAULT 0,
            trabalhou INTEGER NOT NULL DEFAULT 0,     -- >= minimo_percentual
            diaria REAL NOT NULL DEFAULT 0,           -- salario/26 se trabalhou, senão 0
            comissao REAL NOT NULL DEFAULT 0,         -- comissão do dia (sáb/dom)
            override INTEGER NOT NULL DEFAULT 0,      -- 1 = forçado manualmente
            UNIQUE(dia, user_id)
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS rh_commission_pool (
            dia TEXT PRIMARY KEY,            -- "YYYY-MM-DD"
            clientes_fechados INTEGER NOT NULL DEFAULT 0
        );
        """)


def rh_upsert_staff(
    user_id: int,
    nome: str,
    salario_mensal: float,
    turno_ini: str,
    turno_fim: str,
    ativo: int = 1,
    tolerancia_fim: str | None = None,
    comissao_por_cliente: float = 0.75
):
    rh_setup()
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO rh_staff (user_id, nome, ativo, salario_mensal, turno_ini, turno_fim, tolerancia_fim, comissao_por_cliente)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                nome=excluded.nome,
                ativo=excluded.ativo,
                salario_mensal=excluded.salario_mensal,
                turno_ini=excluded.turno_ini,
                turno_fim=excluded.turno_fim,
                tolerancia_fim=excluded.tolerancia_fim,
                comissao_por_cliente=excluded.comissao_por_cliente
        """, (
            int(user_id), str(nome), int(ativo), float(salario_mensal),
            str(turno_ini), str(turno_fim), tolerancia_fim, float(comissao_por_cliente)
        ))


def rh_is_staff_active(user_id: int) -> bool:
    rh_setup()
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT ativo FROM rh_staff WHERE user_id=?", (int(user_id),))
        row = cur.fetchone()
    return bool(row and int(row[0]) == 1)


def rh_open_session(user_id: int, channel_id: int):
    rh_setup()
    with _conn() as conn:
        cur = conn.cursor()

        # evita abrir 2 sessões se já tiver uma aberta
        cur.execute("""
            SELECT id FROM rh_sessions
            WHERE user_id=? AND channel_id=? AND leave_at IS NULL
            ORDER BY id DESC LIMIT 1
        """, (int(user_id), int(channel_id)))
        if cur.fetchone():
            return

        cur.execute("""
            INSERT INTO rh_sessions (user_id, channel_id, join_at)
            VALUES (?, ?, ?)
        """, (int(user_id), int(channel_id), datetime.now(BR_TIMEZONE).isoformat()))


def rh_close_session(user_id: int, channel_id: int):
    rh_setup()
    with _conn() as conn:
        cur = conn.cursor()

        cur.execute("""
            SELECT id FROM rh_sessions
            WHERE user_id=? AND channel_id=? AND leave_at IS NULL
            ORDER BY id DESC LIMIT 1
        """, (int(user_id), int(channel_id)))
        row = cur.fetchone()
        if not row:
            return

        sess_id = row[0]
        cur.execute(
            "UPDATE rh_sessions SET leave_at=? WHERE id=?",
            (datetime.now(BR_TIMEZONE).isoformat(), int(sess_id))
        )


def rh_criar_pool_comissao(dia: str, clientes: int):
    rh_setup()
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO rh_commission_pool (dia, clientes_fechados)
            VALUES (?, ?)
            ON CONFLICT(dia) DO UPDATE SET clientes_fechados=excluded.clientes_fechados
        """, (str(dia), int(clientes)))


# ---------- helpers ----------
def _parse_time_hhmm(hhmm: str):
    h, m = hhmm.split(":")
    return int(h), int(m)


def _dt(d: date, hhmm: str) -> datetime:
    h, m = _parse_time_hhmm(hhmm)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=BR_TIMEZONE)


def _overlap_minutes(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> int:
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    if end <= start:
        return 0
    return int((end - start).total_seconds() // 60)


def _fmt_money(v: float) -> str:
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# ==========================
# FECHAR DIA (REGRA A + override)
# ==========================
def rh_fechar_dia(channel_id: int, dia: date, minimo_percentual: float = 0.50) -> list[dict]:
    """
    Regra A (atual):
    - Se >= minimo_percentual do turno: recebe diária (salario/26)
    - Senão: diária 0
    - Sem penalidade automática
    - Comissão (Sáb/Dom): pool do dia dividido entre quem trabalhou (minimo_percentual+)
    - Override: se override=1 no dia, conta como trabalhou e paga diária (mesmo sem minutos)
    """
    rh_setup()
    dia_str = dia.strftime("%Y-%m-%d")

    with _conn() as conn:
        cur = conn.cursor()

        # staff ativo
        cur.execute("""
            SELECT user_id, nome, salario_mensal, turno_ini, turno_fim, comissao_por_cliente
            FROM rh_staff
            WHERE ativo=1
        """)
        staff = cur.fetchall()

        resultados = []
        start_day = datetime(dia.year, dia.month, dia.day, 0, 0, tzinfo=BR_TIMEZONE)
        end_day = start_day + timedelta(days=1)

        for (user_id, nome, salario_mensal, turno_ini, turno_fim, comissao_por_cliente) in staff:
            user_id = int(user_id)

            # se já tem override no dia, respeita
            cur.execute("SELECT override FROM rh_daily WHERE dia=? AND user_id=? LIMIT 1", (dia_str, user_id))
            row_ov = cur.fetchone()
            is_override = bool(row_ov and int(row_ov[0]) == 1)

            turno_inicio_dt = _dt(dia, str(turno_ini))
            turno_fim_dt = _dt(dia, str(turno_fim))

            # sessões que intersectam o dia
            cur.execute("""
                SELECT join_at, COALESCE(leave_at, ?) as leave_at
                FROM rh_sessions
                WHERE user_id=? AND channel_id=?
                  AND join_at < ?
                  AND COALESCE(leave_at, ?) > ?
            """, (
                end_day.isoformat(),
                user_id, int(channel_id),
                end_day.isoformat(),
                end_day.isoformat(),
                start_day.isoformat()
            ))
            sessions = cur.fetchall()

            minutos_total = 0
            minutos_turno = 0

            for join_at, leave_at in sessions:
                j = datetime.fromisoformat(join_at)
                l = datetime.fromisoformat(leave_at)

                if j < start_day:
                    j = start_day
                if l > end_day:
                    l = end_day

                mins = int((l - j).total_seconds() // 60)
                if mins <= 0:
                    continue

                minutos_total += mins
                minutos_turno += _overlap_minutes(j, l, turno_inicio_dt, turno_fim_dt)

            duracao_turno = int((turno_fim_dt - turno_inicio_dt).total_seconds() // 60)
            trabalhou_calc = 1 if (duracao_turno > 0 and (minutos_turno / duracao_turno) >= float(minimo_percentual)) else 0

            trabalhou = 1 if (is_override or trabalhou_calc == 1) else 0
            diaria = (float(salario_mensal) / 26.0) if trabalhou else 0.0

            # grava daily (comissao calculada depois)
            cur.execute("""
                INSERT INTO rh_daily (dia, user_id, minutos_total, minutos_turno, trabalhou, diaria, comissao, override)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT(dia, user_id) DO UPDATE SET
                    minutos_total=excluded.minutos_total,
                    minutos_turno=excluded.minutos_turno,
                    trabalhou=excluded.trabalhou,
                    diaria=excluded.diaria
            """, (
                dia_str, user_id,
                int(minutos_total), int(minutos_turno),
                int(trabalhou), float(diaria),
                1 if is_override else 0
            ))

            resultados.append({
                "user_id": user_id,
                "nome": str(nome),
                "min_total": int(minutos_total),
                "min_turno": int(minutos_turno),
                "turno": f"{turno_ini}-{turno_fim}",
                "trabalhou": bool(trabalhou),
                "override": bool(is_override),
                "diaria": float(diaria),
                "comissao": 0.0,
                "total": float(diaria),
            })

        # ---- comissão Sáb/Dom (depois de salvar daily) ----
        wd = dia.weekday()  # 5=sab 6=dom
        if wd in (5, 6):
            cur.execute("SELECT clientes_fechados FROM rh_commission_pool WHERE dia=?", (dia_str,))
            row = cur.fetchone()
            clientes = int(row[0]) if row else 0

            if clientes > 0:
                cur.execute("""
                    SELECT d.user_id, s.comissao_por_cliente
                    FROM rh_daily d
                    JOIN rh_staff s ON s.user_id = d.user_id
                    WHERE d.dia=? AND d.trabalhou=1 AND s.ativo=1
                """, (dia_str,))
                workers = cur.fetchall()

                if workers:
                    comissao_por_cliente = float(workers[0][1])
                    total_comissao = clientes * comissao_por_cliente
                    por_pessoa = total_comissao / len(workers)

                    for (uid, _) in workers:
                        cur.execute("""
                            UPDATE rh_daily
                            SET comissao=?
                            WHERE dia=? AND user_id=?
                        """, (float(por_pessoa), dia_str, int(uid)))

                    for r in resultados:
                        if r["trabalhou"]:
                            r["comissao"] = float(por_pessoa)
                            r["total"] = float(r["diaria"]) + float(r["comissao"])

    return resultados


# ==========================
# RESUMO MENSAL
# ==========================
def rh_resumo_mes(ano: int, mes: int) -> dict:
    rh_setup()
    mes_str = f"{ano:04d}-{mes:02d}"

    with _conn() as conn:
        cur = conn.cursor()

        cur.execute("SELECT user_id, nome FROM rh_staff")
        staff = cur.fetchall()

        linhas = []
        total_geral = 0.0

        for (user_id, nome) in staff:
            cur.execute("""
                SELECT
                    SUM(CASE WHEN trabalhou=1 THEN 1 ELSE 0 END) as dias_ok,
                    SUM(diaria) as diaria_total,
                    SUM(comissao) as comissao_total
                FROM rh_daily
                WHERE user_id=?
                  AND substr(dia,1,7)=?
            """, (int(user_id), mes_str))
            row = cur.fetchone() or (0, 0, 0)

            dias_ok = int(row[0] or 0)
            diaria_total = float(row[1] or 0.0)
            comissao_total = float(row[2] or 0.0)
            total = diaria_total + comissao_total

            total_geral += total

            linhas.append({
                "user_id": int(user_id),
                "nome": str(nome),
                "dias_ok": dias_ok,
                "diaria_total": diaria_total,
                "comissao_total": comissao_total,
                "total": total,
            })

    linhas.sort(key=lambda x: x["total"], reverse=True)
    return {"mes": mes_str, "linhas": linhas, "total_geral": float(total_geral)}


# ==========================
# FORMATADORES (pra embed)
# ==========================
def rh_formatar_resumo_diario(dia: date, resultados: list[dict]) -> tuple[str, list[str], float]:
    titulo = f"📌 RH — Resumo do dia ({dia.strftime('%d/%m/%Y')})"

    linhas = []
    total_confirmados = 0.0

    resultados = sorted(resultados, key=lambda x: x["nome"].lower())

    for r in resultados:
        ok = bool(r.get("trabalhou"))
        ov = bool(r.get("override"))
        icone = "✅" if ok else "❌"

        diaria = float(r.get("diaria", 0.0) or 0.0)
        comissao = float(r.get("comissao", 0.0) or 0.0)
        soma = diaria + comissao

        if ok:
            total_confirmados += soma

        extra = " (override)" if ov else ""
        if comissao > 0:
            linhas.append(f"{icone} **{r['nome']}**{extra} — {_fmt_money(diaria)} + {_fmt_money(comissao)} → **{_fmt_money(soma)}**")
        else:
            linhas.append(f"{icone} **{r['nome']}**{extra} — {_fmt_money(diaria)} → **{_fmt_money(soma)}**")

    return titulo, linhas, float(total_confirmados)


def rh_formatar_resumo_mensal(resumo_mes: dict) -> tuple[str, list[str], float]:
    mes = resumo_mes["mes"]
    titulo = f"📌 RH — Fechamento mensal ({mes})"

    linhas = []
    total_geral = float(resumo_mes["total_geral"] or 0.0)

    for r in resumo_mes["linhas"]:
        diaria_total = float(r["diaria_total"])
        comissao_total = float(r["comissao_total"])
        total = float(r["total"])
        dias_ok = int(r["dias_ok"])

        if comissao_total > 0:
            linhas.append(f"✅ **{r['nome']}** — {dias_ok} dias | {_fmt_money(diaria_total)} + {_fmt_money(comissao_total)} → **{_fmt_money(total)}**")
        else:
            linhas.append(f"✅ **{r['nome']}** — {dias_ok} dias | {_fmt_money(diaria_total)} → **{_fmt_money(total)}**")

    return titulo, linhas, total_geral


# ==========================
# OVERRIDE (forçar trabalhou)
# ==========================
def rh_forcar_trabalhado(dia: date, user_id: int) -> dict:
    """
    Marca o dia como TRABALHOU manualmente (override=1).
    - trabalhou=1
    - diaria = salario/26
    - comissao fica 0 aqui (a comissão do sab/dom vem no fechamento do dia)
    """
    rh_setup()
    dia_str = dia.strftime("%Y-%m-%d")

    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT nome, salario_mensal FROM rh_staff WHERE user_id=? LIMIT 1", (int(user_id),))
        row = cur.fetchone()
        if not row:
            raise Exception("Usuário não está cadastrado no rh_staff.")

        nome, salario_mensal = row
        diaria = float(salario_mensal or 0) / 26.0

        cur.execute("""
            INSERT INTO rh_daily (dia, user_id, minutos_total, minutos_turno, trabalhou, diaria, comissao, override)
            VALUES (?, ?, 0, 0, 1, ?, 0, 1)
            ON CONFLICT(dia, user_id) DO UPDATE SET
                trabalhou=1,
                diaria=excluded.diaria,
                override=1
        """, (dia_str, int(user_id), float(diaria)))

    return {"dia": dia_str, "user_id": int(user_id), "nome": str(nome), "diaria": float(diaria)}
