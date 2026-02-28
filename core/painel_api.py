import time
from datetime import datetime, timedelta, timezone, date
from typing import Optional, Dict
from urllib.parse import unquote

import requests
from requests.exceptions import ReadTimeout, ConnectTimeout, RequestException

from core.auth_painel import get_session, ensure_logged, login_painel

BASE_URL = "https://crm.hpanel.vip"
BR_TIMEZONE = timezone(timedelta(hours=-3))

# =====================
# OWNER_ID POR PAINEL
# =====================
OWNER_ID_POR_PAINEL = {
    "T1": 157,
    "T2": 156,
    "T3": 160,
}


# =====================
# HEADERS AJAX
# =====================
def _headers_ajax(painel: str, referer_path: str) -> dict:
    """
    Monta headers padrão AJAX com XSRF do painel logado.
    """
    s = get_session(painel)
    xsrf = s.cookies.get("XSRF-TOKEN")
    xsrf = unquote(xsrf) if xsrf else None

    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "Mozilla/5.0",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{BASE_URL}{referer_path}",
        "Origin": BASE_URL,
    }

    if xsrf:
        headers["X-XSRF-TOKEN"] = xsrf

    return headers


# =====================
# REQUEST JSON COM RETRY
# =====================
def _get_json(painel: str, path: str, params=None, headers=None) -> dict:
    """
    Faz GET JSON com:
    - ensure_logged
    - retry
    - relogin automático em 401/419
    """
    ensure_logged(painel)
    s = get_session(painel)
    url = f"{BASE_URL}{path}"

    last_err = None

    for attempt in range(1, 4):
        try:
            r = s.get(url, headers=headers, params=params, timeout=35)

            if r.status_code in (401, 419):
                login_painel(painel, force=True)
                r = s.get(url, headers=headers, params=params, timeout=35)

            if r.status_code != 200:
                txt = (r.text or "")[:250].replace("\n", " ")
                raise Exception(f"HTTP {r.status_code} em {path} | resp: {txt} | tentativa {attempt}/3")

            return r.json()

        except (ReadTimeout, ConnectTimeout) as e:
            last_err = e
            time.sleep(attempt)
            continue
        except RequestException as e:
            last_err = e
            break
        except Exception as e:
            last_err = e
            break

    raise Exception(f"Falha ao acessar {path} (tentativas 3/3): {last_err}")


# =====================
# HELPERS
# =====================
def _parse_created_at_br(created_at: str) -> Optional[datetime]:
    """Converte data BR do painel para datetime timezone-aware."""
    if not created_at:
        return None

    created_at = str(created_at).strip()

    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(created_at, fmt).replace(tzinfo=BR_TIMEZONE)
        except Exception:
            pass

    return None


def _to_int(v) -> Optional[int]:
    try:
        return int(v)
    except Exception:
        return None


def _to_float(v) -> float:
    """
    Converte valores monetários:
    - 30.00
    - 30,00
    - R$ 30,00
    """
    try:
        if v is None:
            return 0.0

        if isinstance(v, (int, float)):
            return float(v)

        s = str(v).strip()
        if not s:
            return 0.0

        s = s.replace("R$", "").replace(" ", "")

        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", ".")

        return float(s)
    except Exception:
        return 0.0


def _tipo_transacao(item: dict) -> str:
    """
    Lê o tipo da transação do item.
    Aceita transaction_type ou type.
    """
    return str(item.get("transaction_type") or item.get("type") or "").strip().lower()


def _owner_do_painel(painel: str) -> int:
    """
    Retorna owner_id do painel.
    """
    owner_id = OWNER_ID_POR_PAINEL.get(painel)
    if owner_id in (None, "", 0):
        raise Exception(f"OWNER_ID não configurado para o painel {painel}")
    return int(owner_id)


def _data_referencia_financeiro() -> date:
    """
    Se testar após meia-noite, usa o dia anterior até 02:59.
    Evita financeiro zerado em testes.
    """
    agora = datetime.now(BR_TIMEZONE)
    if agora.hour < 3:
        return (agora - timedelta(days=1)).date()
    return agora.date()


# =====================
# MÉTRICAS DO FECHAMENTO
# =====================
def buscar_creditos(painel: str) -> float:
    """
    Busca créditos disponíveis do painel.
    """
    data = _get_json(
        painel,
        "/users/credits",
        headers=_headers_ajax(painel, "/users"),
    )

    if not data.get("success"):
        raise Exception(f"Resposta inválida ao buscar créditos: {data}")

    creditos = data.get("credits")
    if creditos is None:
        raise Exception(f"Créditos não encontrados: {data}")

    return float(creditos)


def buscar_clientes_ativos(painel: str) -> int:
    """
    Busca total de clientes ativos.
    """
    params = {
        "draw": 1,
        "start": 0,
        "length": 1,
        "search[value]": "",
        "search[regex]": "false",
        "filter_status": "all_active",
        "filter_trial": "",
        "filter_blocked": "",
        "filter_expiration_date": "",
    }

    data = _get_json(
        painel,
        "/customers",
        params=params,
        headers=_headers_ajax(painel, "/customers"),
    )

    if "recordsTotal" not in data:
        raise Exception(f"Resposta não contém recordsTotal: {data}")

    return int(data["recordsTotal"])


def buscar_testes_de_hoje(painel: str, page_size: int = 100) -> Dict:
    """
    Busca quantidade de testes do dia via logs.
    """
    hoje = datetime.now(BR_TIMEZONE).date()
    total = 0
    start = 0
    owner_id = _owner_do_painel(painel)

    while True:
        data = _get_json(
            painel,
            "/logs-credit-consumptions",
            params={"draw": 1, "start": start, "length": page_size},
            headers=_headers_ajax(painel, "/logs-credit-consumptions"),
        )

        itens = data.get("data") or []
        if not itens:
            break

        for item in itens:
            if _tipo_transacao(item) != "novo_teste":
                continue

            oid = _to_int(item.get("owner_id"))
            if oid != owner_id:
                continue

            dt = _parse_created_at_br(item.get("created_at", ""))
            if dt and dt.date() == hoje:
                total += 1

        start += page_size

    return {"total_hoje": total}


# =====================
# FINANCEIRO / REVENUES
# =====================
def _buscar_revenues_filtrado(
    painel: str,
    transaction_type: str,
    dia_ref: Optional[date] = None,
    page_size: int = 200,
) -> Dict:
    """
    Busca receitas filtrando por:
    - painel (owner_id)
    - transaction_type
    - dia de referência
    """
    if dia_ref is None:
        dia_ref = _data_referencia_financeiro()

    owner_id = _owner_do_painel(painel)
    start = 0
    total_qtd = 0
    total_valor = 0.0

    while True:
        data = _get_json(
            painel,
            "/revenues",
            params={
                "draw": 1,
                "start": start,
                "length": page_size,
            },
            headers=_headers_ajax(painel, "/revenues"),
        )

        itens = data.get("data") or []
        if not itens:
            break

        for item in itens:
            oid = _to_int(item.get("owner_id"))
            if oid != owner_id:
                continue

            t = _tipo_transacao(item)
            if t != transaction_type:
                continue

            dt = _parse_created_at_br(item.get("created_at", ""))
            if not dt or dt.date() != dia_ref:
                continue

            total_qtd += 1
            total_valor += _to_float(item.get("value"))

        start += page_size

    return {
        "qtd": total_qtd,
        "valor": round(total_valor, 2),
        "dia_ref": dia_ref,
    }


def buscar_novos_clientes_de_hoje(painel: str, page_size: int = 200) -> Dict:
    """
    Busca quantidade e valor de novos clientes no dia.
    """
    r = _buscar_revenues_filtrado(painel, "novo_cliente", page_size=page_size)
    return {
        "total_hoje": r["qtd"],
        "valor_total": r["valor"],
        "dia_ref": r["dia_ref"],
    }


def buscar_renovacoes_de_hoje(painel: str, page_size: int = 200) -> Dict:
    """
    Busca quantidade e valor de renovações no dia.
    """
    r = _buscar_revenues_filtrado(painel, "renovacao", page_size=page_size)
    return {
        "total_hoje": r["qtd"],
        "valor_total": r["valor"],
        "dia_ref": r["dia_ref"],
    }


def buscar_financeiro_de_hoje(painel: str, page_size: int = 200) -> Dict:
    """
    Consolida financeiro diário do painel.
    """
    novos = buscar_novos_clientes_de_hoje(painel, page_size=page_size)
    renov = buscar_renovacoes_de_hoje(painel, page_size=page_size)

    novos_qtd = int(novos.get("total_hoje", 0) or 0)
    novos_total = float(novos.get("valor_total", 0.0) or 0.0)

    renov_qtd = int(renov.get("total_hoje", 0) or 0)
    renov_total = float(renov.get("valor_total", 0.0) or 0.0)

    dia_ref = novos.get("dia_ref") or renov.get("dia_ref") or _data_referencia_financeiro()

    return {
        "painel": painel,
        "dia_ref": dia_ref,
        "novos_qtd": novos_qtd,
        "novos_total": round(novos_total, 2),
        "renov_qtd": renov_qtd,
        "renov_total": round(renov_total, 2),
        "total_geral": round(novos_total + renov_total, 2),
    }


# =====================
# STATUS DA API
# =====================
def buscar_status_api(painel: str) -> dict:
    """
    Verifica status da API / instância conectada.
    """
    data = _get_json(
        painel,
        "/zapipro-instances",
        headers=_headers_ajax(painel, "/zapipro-instances"),
    )

    itens = data.get("data") or []
    if not itens:
        return {"ok": 0, "status": "unknown"}

    for it in itens:
        st = (it.get("status") or "").lower().strip()
        if st == "connected":
            return {"ok": 1, "status": "connected"}

    st0 = (itens[0].get("status") or "unknown").lower().strip()
    return {"ok": 0, "status": st0}


# =====================
# DEBUG
# =====================
def debug_logs(painel: str):
    """
    Debug simples dos logs de consumo.
    """
    data = _get_json(
        painel,
        "/logs-credit-consumptions",
        params={"draw": 1, "start": 0, "length": 5},
        headers=_headers_ajax(painel, "/logs-credit-consumptions"),
    )

    print("CHAVES:", list(data.keys()))
    print("recordsTotal:", data.get("recordsTotal"))
    print("recordsFiltered:", data.get("recordsFiltered"))

    itens = data.get("data") or []
    print("len(data):", len(itens))

    if itens:
        print("ITEM 0:", itens[0])
        print("TRANSACTION_TYPES:", sorted({str(i.get('transaction_type') or i.get('type') or '') for i in itens}))
        print("OWNER_IDS:", sorted({str(i.get('owner_id')) for i in itens}))
        print("CREATED_AT:", [i.get("created_at") for i in itens])

    return data