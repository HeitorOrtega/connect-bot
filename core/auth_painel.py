import os
import re
import requests
from urllib.parse import unquote
from typing import Optional, Dict

BASE_URL = "https://crm.hpanel.vip"
LOGIN_PATH = "/login"
LOGIN_URL = f"{BASE_URL}{LOGIN_PATH}"

_SESSIONS: Dict[str, requests.Session] = {}
_LOGGED: Dict[str, bool] = {}


def get_session(painel: str) -> requests.Session:
    painel = painel.upper().strip()
    if painel not in _SESSIONS:
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0"})
        _SESSIONS[painel] = s
        _LOGGED[painel] = False
    return _SESSIONS[painel]


def _creds(painel: str) -> tuple[str, str]:
    painel = painel.upper().strip()
    user = (os.getenv(f"PAINEL_{painel}_USER") or "").strip()
    pw = (os.getenv(f"PAINEL_{painel}_PASS") or "").strip()
    if not user or not pw:
        raise RuntimeError(f"Credenciais do {painel} não configuradas (PAINEL_{painel}_USER/PASS).")
    return user, pw


def _xsrf_from_session(s: requests.Session) -> Optional[str]:
    xsrf = s.cookies.get("XSRF-TOKEN")
    return unquote(xsrf) if xsrf else None


def _csrf_from_html(html: str) -> Optional[str]:
    m = re.search(r'name="csrf-token"\s+content="([^"]+)"', html)
    if m:
        return m.group(1)

    m = re.search(r'name="_token"\s+value="([^"]+)"', html)
    if m:
        return m.group(1)

    return None


def _infer_login_fields(html: str) -> tuple[str, str]:
    """
    Tenta descobrir o name= do campo de usuário e senha no HTML do /login.
    """
    pass_m = re.search(r'type="password"\s+name="([^"]+)"', html)
    pass_field = pass_m.group(1) if pass_m else "password"

    user_m = re.search(r'name="(email|username|user|login)"', html, flags=re.I)
    user_field = user_m.group(1) if user_m else "email"

    return user_field, pass_field


def login_painel(painel: str, force: bool = False) -> None:
    painel = painel.upper().strip()
    s = get_session(painel)

    if _LOGGED.get(painel) and not force:
        return

    user, pw = _creds(painel)

    r = s.get(LOGIN_URL, timeout=25)
    r.raise_for_status()

    csrf = _csrf_from_html(r.text)
    xsrf = _xsrf_from_session(s)
    user_field, pass_field = _infer_login_fields(r.text)

    headers = {
        "Referer": LOGIN_URL,
        "Origin": BASE_URL,
        "X-Requested-With": "XMLHttpRequest",
    }
    if xsrf:
        headers["X-XSRF-TOKEN"] = xsrf

    payload = {
        user_field: user,
        pass_field: pw,
    }
    if csrf:
        payload["_token"] = csrf

    r2 = s.post(LOGIN_URL, data=payload, headers=headers, timeout=25, allow_redirects=False)

    if r2.status_code == 302:
        loc = (r2.headers.get("Location") or "").lower()
        if "login" in loc and "logout" not in loc:
            raise RuntimeError(f"Login falhou no {painel}: redirect para login.")
        _LOGGED[painel] = True
        return

    if r2.status_code == 200:
        txt = (r2.text or "").lower()
        if "invalid" in txt or "credentials" in txt or "erro" in txt:
            raise RuntimeError(f"Login parece ter falhado no {painel} (HTTP 200 mas com erro na página).")
        _LOGGED[painel] = True
        return

    raise RuntimeError(f"Login falhou no {painel}: HTTP {r2.status_code} resp={(r2.text or '')[:120]}")


def ensure_logged(painel: str) -> None:
    login_painel(painel, force=False)
