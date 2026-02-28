import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict
from urllib.parse import unquote

import requests
from requests.exceptions import ReadTimeout, ConnectTimeout, RequestException

from core.auth_painel import get_session, ensure_logged, login_painel

BASE_URL = "https://crm.hpanel.vip"
BR_TIMEZONE = timezone(timedelta(hours=-3))

OWNER_ID_POR_PAINEL = {
    "T1": 157,
    "T2": 156,
    "T3": 158,
}

# =====================
# HEADERS AJAX
# =====================
def _headers_ajax(painel: str, referer_path: str) -> dict:
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
# GET JSON (retry + timeout + relogin)
# =====================
def _get_json(painel: str, path: str, params=None, headers=None) -> dict:
    ensure_logged(painel)
    s = get_session(painel)
    url = f"{BASE_URL}{path}"

    last_err = None

    for attempt in range(1, 4):  # 3 tentativas
        try:
            r = s.get(url, headers=headers, params=params, timeout=35)

            if r.status_code in (401, 419):
                login_painel(painel, force=True)
                r = s.get(url, headers=headers, params=params, timeout=35)

            if r.status_code != 200:
                txt = (r.text or "")[:200].replace("\n", " ")
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
# PARSE DATE (created_at do LOG)
# =====================
def _parse_created_at_br(created_at: str) -> Optional[datetime]:
    if not created_at:
        return None
    created_at = created_at.strip()
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
    try:
        if v is None:
            return 0.0
        if isinstance(v, (int, float)):
            return float(v)

        s = str(v).strip()
        if not s:
            return 0.0

        s = s.replace("R$", "").replace(" ", "")

        # 1.234,56 -> 1234.56
        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", ".")

        return float(s)
    except Exception:
        return 0.0


def _extrair_valor_item(item: dict) -> float:
    """
    Tenta descobrir o valor financeiro do log usando várias chaves possíveis.
    """
    for chave in (
        "amount",
        "value",
        "valor",
        "price",
        "total",
        "credit",
        "credits",
        "used_credits",
        "sale_value",
        "paid_value",
    ):
        if chave in item and item.get(chave) not in (None, "", "null"):
            valor = _to_float(item.get(chave))
            if valor:
                return valor

    # fallback: algumas APIs jogam dentro de campos aninhados/estranhos
    for _, v in item.items():
        if isinstance(v, (int, float)):
            if float(v) > 0:
                return float(v)

    return 0.0

# =====================
# BUSCAR CRÉDITOS
# =====================
def buscar_creditos(painel: str) -> float:
    data = _get_json(
        painel,
        "/users/credits",
        headers=_headers_ajax(painel, "/users")
    )

    if not data.get("success"):
        raise Exception(f"Resposta inválida ao buscar créditos: {data}")

    creditos = data.get("credits")
    if creditos is None:
        raise Exception(f"Créditos não encontrados: {data}")

    return float(creditos)

# =====================
# BUSCAR CLIENTES ATIVOS
# =====================
def buscar_clientes_ativos(painel: str) -> int:
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
        headers=_headers_ajax(painel, "/customers")
    )

    if "recordsTotal" not in data:
        raise Exception(f"Resposta não contém recordsTotal: {data}")

    return int(data["recordsTotal"])

# =====================
# TESTES DO DIA
# =====================
def buscar_testes_de_hoje(painel: str, page_size: int = 100) -> Dict:
    hoje = datetime.now(BR_TIMEZONE).date()
    total = 0
    start = 0
    owner_id = OWNER_ID_POR_PAINEL[painel]

    while True:
        data = _get_json(
            painel,
            "/logs-credit-consumptions",
            params={"draw": 1, "start": start, "length": page_size},
            headers=_headers_ajax(painel, "/logs-credit-consumptions")
        )

        itens = data.get("data") or []
        if not itens:
            break

        for item in itens:
            t = (item.get("type") or "").strip().lower()
            if t != "novo_teste":
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
# NOVOS CLIENTES DO DIA
# =====================
def buscar_novos_clientes_de_hoje(painel: str, page_size: int = 200) -> Dict:
    hoje = datetime.now(BR_TIMEZONE).date()
    owner_id = OWNER_ID_POR_PAINEL[painel]

    total = 0
    unicos = set()
    start = 0

    while True:
        data = _get_json(
            painel,
            "/logs-credit-consumptions",
            params={"draw": 1, "start": start, "length": page_size},
            headers=_headers_ajax(painel, "/logs-credit-consumptions")
        )

        itens = data.get("data") or []
        if not itens:
            break

        for item in itens:
            t = (item.get("type") or "").strip().lower()
            if t != "novo_cliente":
                continue

            oid = _to_int(item.get("owner_id"))
            if oid != owner_id:
                continue

            dt = _parse_created_at_br(item.get("created_at", ""))
            if not dt or dt.date() != hoje:
                continue

            total += 1
            rid = item.get("recipient_id")
            if rid is not None:
                unicos.add(str(rid))

        start += page_size

    return {"total_hoje": total, "unicos_hoje": len(unicos)}


# =====================
# RENOVAÇÕES DO DIA
# =====================
def buscar_financeiro_de_hoje(painel: str, page_size: int = 200) -> Dict:
    hoje = datetime.now(BR_TIMEZONE).date()
    owner_id = OWNER_ID_POR_PAINEL[painel]

    novos_qtd = 0
    novos_total = 0.0
    renov_qtd = 0
    renov_total = 0.0
    start = 0

    while True:
        data = _get_json(
            painel,
            "/logs-credit-consumptions",
            params={"draw": 1, "start": start, "length": page_size},
            headers=_headers_ajax(painel, "/logs-credit-consumptions")
        )

        itens = data.get("data") or []
        if not itens:
            break

        for item in itens:
            oid = _to_int(item.get("owner_id"))
            if oid != owner_id:
                continue

            dt = _parse_created_at_br(item.get("created_at", ""))
            if not dt or dt.date() != hoje:
                continue

            t = (item.get("type") or "").strip().lower()
            valor = _extrair_valor_item(item)

            if t == "novo_cliente":
                novos_qtd += 1
                novos_total += valor

            elif t == "renovacao":
                renov_qtd += 1
                renov_total += valor

        start += page_size

    total_geral = novos_total + renov_total

    return {
        "painel": painel,
        "novos_qtd": novos_qtd,
        "novos_total": round(novos_total, 2),
        "renov_qtd": renov_qtd,
        "renov_total": round(renov_total, 2),
        "total_geral": round(total_geral, 2),
    }


# =====================
# STATUS DA API
# =====================
def buscar_status_api(painel: str) -> dict:
    data = _get_json(
        painel,
        "/zapipro-instances",
        headers=_headers_ajax(painel, "/zapipro-instances")
    )

    itens = data.get("data") or []
    if not itens:
        return {"ok": 0, "status": "unknown"}

    for it in itens:
        st = (it.get("status") or "").lower().strip()
        if st == "connected":
            return {"ok": 1, "status": "connected"}

    # senão, devolve o primeiro status válido
    st0 = (itens[0].get("status") or "unknown").lower().strip()
    return {"ok": 0, "status": st0}

def debug_logs(painel: str):
    data = _get_json(
        painel,
        "/logs-credit-consumptions",
        params={"draw": 1, "start": 0, "length": 5},
        headers=_headers_ajax(painel, "/logs-credit-consumptions")
    )
    print("CHAVES:", list(data.keys()))
    print("recordsTotal:", data.get("recordsTotal"))
    print("recordsFiltered:", data.get("recordsFiltered"))
    itens = data.get("data") or []
    print("len(data):", len(itens))
    if itens:
        print("ITEM 0:", itens[0])
        print("TIPOS:", sorted({(i.get('type') or '') for i in itens}))
        print("OWNER_IDS:", sorted({str(i.get('owner_id')) for i in itens}))
        print("CREATED_AT:", [i.get("created_at") for i in itens])
    return data
