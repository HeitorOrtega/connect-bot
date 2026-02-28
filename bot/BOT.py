from __future__ import annotations

import os
import asyncio
from pathlib import Path
from datetime import datetime, timedelta, timezone, date
from typing import Optional
from datetime import time as dtime

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

# =====================
# PATH + ENV
# =====================
BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(dotenv_path=BASE_DIR / ".env")

INSTANCE_ID = os.getenv("INSTANCE_ID") or str(os.getpid())
print("🤖 Instance:", INSTANCE_ID)

# =====================
# IMPORTS DO PROJETO
# =====================
from config import CANAIS_FECHAMENTOS
from core.database import criar_tabela, salvar_fechamento
from core.painel_api import (
    buscar_creditos,
    buscar_clientes_ativos,
    buscar_testes_de_hoje,
    buscar_novos_clientes_de_hoje,
    buscar_renovacoes_de_hoje,
    buscar_status_api,
    buscar_financeiro_de_hoje,
)

from core.rh_db import (
    rh_setup,
    rh_upsert_staff,
    rh_is_staff_active,
    rh_open_session,
    rh_close_session,
    rh_fechar_dia,
    rh_criar_pool_comissao,
    rh_resumo_mes,
    rh_formatar_resumo_diario,
    rh_formatar_resumo_mensal,
)

# =====================
# CONFIG BÁSICA
# =====================
TOKEN = os.getenv("DISCORD_TOKEN")
CANAL_ALERTA = int(os.getenv("CANAL_ALERTA", "1470879727152664616"))

# RH
TRACK_VOICE_ID = int(os.getenv("TRACK_VOICE_ID", "1333438883442065569"))
CANAL_RH = int(os.getenv("CANAL_RH", "1470879494419251383"))
MINIMO_RH = float(os.getenv("MINIMO_RH", "0.50"))

# comissão: qual painel define o pool às 23:00
COMMISSION_PAINEL = (os.getenv("COMMISSION_PAINEL", "T1") or "T1").upper().strip()

HORARIOS_FINANCEIRO = {(15, 0), (23, 0)}
CANAL_FINANCEIRO = int(os.getenv("CANAL_FINANCEIRO", "1470879727152664616"))

BR_TIMEZONE = timezone(timedelta(hours=-3))
COR_PRINCIPAL = 0x7A2EFF

BANNER_PATH = "assets/banner.png"
THUMB_PATH = "assets/connect-logo.png"

# Fechamentos
HORARIOS_FECHAR = {(10, 0), (13, 0), (19, 0), (22, 0), (0, 0)}

# RH
HORARIO_RH_DIARIO = {(23, 5)}
HORARIO_RH_MENSAL = {(23, 10)}

# Anti-duplicação por minuto (processo)
ULTIMO_ENVIO: dict[str, str] = {}
_ULTIMO_ALERTA: dict[str, str] = {}
_LOCK_FECH: dict[str, asyncio.Lock] = {}

# =====================
# DISCORD
# =====================
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

# =====================
# TURNOS (texto do FECHAMENTO)
# =====================
TURNOS = [
    ("Manhã", dtime(0, 0), dtime(8, 0)),
    ("Tarde", dtime(8, 0), dtime(15, 0)),
    ("Noite", dtime(15, 0), dtime(23, 0)),
]

def get_turno(dt: datetime) -> tuple[str, str]:
    t = dt.time()
    for nome, ini, fim in TURNOS:
        if ini <= t < fim:
            return nome, f"{ini.strftime('%H:%M')}–{fim.strftime('%H:%M')}"
    return "Noite", "15:00–23:00"

def _fmt_money(v: float) -> str:
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def _ultimo_dia_do_mes(d: date) -> date:
    if d.month == 12:
        prox = date(d.year + 1, 1, 1)
    else:
        prox = date(d.year, d.month + 1, 1)
    return prox - timedelta(days=1)

# =====================
# ALERTA
# =====================
async def alertar_erro(msg: str, painel: Optional[str] = None) -> None:
    try:
        if painel:
            if _ULTIMO_ALERTA.get(painel) == msg:
                return
            _ULTIMO_ALERTA[painel] = msg

        canal = bot.get_channel(CANAL_ALERTA)
        if canal is None:
            canal = await bot.fetch_channel(CANAL_ALERTA)

        await canal.send(f"🚨 **ALERTA**\n{msg}")
    except Exception as e:
        print(f"❌ Falha ao enviar alerta: {e}")
        
@bot.command()
async def testfat(ctx, painel: str = "T3"):
    painel = (painel or "T3").upper().strip()

    if painel not in CANAIS_FECHAMENTOS:
        await ctx.send(f"❌ Painel inválido: `{painel}`")
        return

    msg = await ctx.send(f"⏳ Testando financeiro do **{painel}**...")

    try:
        ok = await enviar_financeiro_diario(painel)
        if ok:
            await msg.edit(content=f"✅ Financeiro do **{painel}** enviado com sucesso.")
        else:
            await msg.edit(content=f"⚠️ Falha ao enviar financeiro do **{painel}**.")
    except Exception as e:
        await msg.edit(content=f"❌ Erro no financeiro do **{painel}**: `{e}`")        

# =====================
# COMANDO ÚNICO: TESTAR T2
# =====================
@bot.command()
async def testar(ctx, painel: str = "t2"):
    """
    Uso:
      !testar
      !testar t2
      !testar t1
    """
    painel = (painel or "t2").upper().strip()

    if painel not in CANAIS_FECHAMENTOS:
        await ctx.send(f"❌ Painel inválido: `{painel}`. Disponíveis: {', '.join(CANAIS_FECHAMENTOS.keys())}")
        return

    msg = await ctx.send(f"⏳ Testando fechamento do **{painel}**...")

    try:
        ok = await fechamento_automatico(painel)
        if ok:
            await msg.edit(content=f"✅ Fechamento do **{painel}** enviado com sucesso.")
        else:
            await msg.edit(content=f"⚠️ Falha no fechamento do **{painel}**. Veja o canal de alertas.")
    except Exception as e:
        await msg.edit(content=f"❌ Erro ao testar **{painel}**: `{e}`")


# =====================
# COMANDO ÚNICO: TEST RH
# =====================
@bot.command()
async def testrh(ctx):
    dia = datetime.now(BR_TIMEZONE).date()
    await enviar_rh_diario(dia)
    await ctx.send("✅ RH enviado no canal de RH (popup embed).")

# =====================
# COLETA SYNC
# =====================
def _pegar_total_hoje(resp) -> int:
    if resp is None:
        return 0
    if isinstance(resp, (int, float)):
        return int(resp)

    if isinstance(resp, dict):
        for k in ("total_hoje", "totalHoje", "hoje", "today", "total", "count", "qtd", "qtd_hoje"):
            v = resp.get(k)
            if v is not None:
                try:
                    return int(v)
                except Exception:
                    pass
    return 0

def coletar_metricas_sync(painel: str) -> dict:
    r_testes = buscar_testes_de_hoje(painel)
    r_novos = buscar_novos_clientes_de_hoje(painel)
    r_reno = buscar_renovacoes_de_hoje(painel)

    return {
        "creditos": buscar_creditos(painel),
        "clientes_ativos": buscar_clientes_ativos(painel),
        "testes_hoje": _pegar_total_hoje(r_testes),
        "novos_clientes_hoje": _pegar_total_hoje(r_novos),
        "renovacoes_hoje": _pegar_total_hoje(r_reno),
        "status": buscar_status_api(painel),
    }

async def enviar_financeiro_diario(painel: str) -> bool:
    painel = painel.upper().strip()

    try:
        dados = await asyncio.to_thread(buscar_financeiro_de_hoje, painel)
    except Exception as e:
        await alertar_erro(
            f"Painel **{painel}**: falha ao buscar financeiro diário.\nErro: `{e}`",
            painel=f"FIN_{painel}",
        )
        return False

    canal = bot.get_channel(CANAL_FINANCEIRO)
    if canal is None:
        canal = await bot.fetch_channel(CANAL_FINANCEIRO)

    embed = discord.Embed(
        title=f"💰 Financeiro Diário — Painel {painel}",
        color=0x2ECC71,
        timestamp=datetime.now(BR_TIMEZONE),
    )

    embed.add_field(
        name="🆕 Novos",
        value=(
            f"Qtd: **{dados['novos_qtd']}**\n"
            f"Valor: **{_fmt_money(dados['novos_total'])}**"
        ),
        inline=False,
    )

    embed.add_field(
        name="🔄 Renovações",
        value=(
            f"Qtd: **{dados['renov_qtd']}**\n"
            f"Valor: **{_fmt_money(dados['renov_total'])}**"
        ),
        inline=False,
    )

    embed.add_field(
        name="📌 Total do dia",
        value=f"**{_fmt_money(dados['total_geral'])}**",
        inline=False,
    )

    embed.set_footer(text=f"Connect Financeiro • {INSTANCE_ID}")

    await canal.send(embed=embed)
    return True


# =====================
# FECHAMENTO (painel)
# =====================
async def fechamento_automatico(painel: str) -> bool:
    painel = painel.upper().strip()

    lock = _LOCK_FECH.setdefault(painel, asyncio.Lock())
    async with lock:
        agora = datetime.now(BR_TIMEZONE)
        chave_minuto = agora.strftime("%Y-%m-%d %H:%M")
        key_dedupe = f"FECH_SEND_{painel}"

        # dedupe por minuto (dentro do MESMO processo)
        if ULTIMO_ENVIO.get(key_dedupe) == chave_minuto:
            return True
        ULTIMO_ENVIO[key_dedupe] = chave_minuto

        try:
            dados = await asyncio.to_thread(coletar_metricas_sync, painel)

            creditos = dados["creditos"]
            clientes_ativos = dados["clientes_ativos"]
            testes_hoje = dados["testes_hoje"]
            novos_clientes_hoje = dados["novos_clientes_hoje"]
            renovacoes_hoje = dados["renovacoes_hoje"]

            status = dados["status"]
            api_ok = int(status.get("ok", 0))
            raw_status = (status.get("status") or "unknown").strip().lower()
            status_txt = "✅ Ok" if api_ok == 1 else f"❌ Erro ({raw_status})"

        except Exception as e:
            err = str(e)

            tipo = "ERRO"
            if "HTTP 401" in err or "Unauthenticated" in err:
                tipo = "401 (sessão/login)"
            elif "timeout" in err.lower():
                tipo = "TIMEOUT"
            elif "HTTP 5" in err:
                tipo = "SERVIDOR (5xx)"

            await alertar_erro(
                f"Painel **{painel}**: falha ao buscar métricas.\n"
                f"Tipo: **{tipo}**\n"
                f"Erro: `{err}`",
                painel=painel,
            )
            return False

        canal_id = CANAIS_FECHAMENTOS[painel]
        canal = bot.get_channel(canal_id)
        if canal is None:
            canal = await bot.fetch_channel(canal_id)

        turno_nome, turno_periodo = get_turno(agora)

        periodo_txt = (
            f"📅 **{agora.strftime('%d/%m/%Y %H:%M')}** ({turno_nome})\n"
            f"⏱️ **Período analisado:** {agora.strftime('%d/%m/%Y')} {turno_periodo}"
        )

        embed = discord.Embed(
            title=f"📌 **Fechamento Automático — Painel {painel.replace('T','')}**",
            description=periodo_txt,
            color=COR_PRINCIPAL,
            timestamp=agora,
        )

        embed.add_field(
            name="💳 Créditos disponíveis",
            value=f"**{creditos:,.2f}**".replace(",", "X").replace(".", ",").replace("X", "."),
            inline=False,
        )
        embed.add_field(
            name="👤 Clientes ativos",
            value=f"**{clientes_ativos:,}**".replace(",", "."),
            inline=False,
        )
        embed.add_field(name="🧪 Testes (hoje)", value=f"**{testes_hoje}**", inline=False)
        embed.add_field(name="🆕 Novos clientes (hoje)", value=f"**{novos_clientes_hoje}**", inline=False)
        embed.add_field(name="🔄 Renovações (hoje)", value=f"**{renovacoes_hoje}**", inline=False)
        embed.add_field(name="📡 Status da API", value=f"**{status_txt}**", inline=False)

        embed.set_footer(text=f"Connect Company • Sistema Automático • {INSTANCE_ID}")

        files = []
        if os.path.exists(THUMB_PATH):
            f = discord.File(THUMB_PATH, filename="logo.png")
            files.append(f)
            embed.set_thumbnail(url="attachment://logo.png")

        if os.path.exists(BANNER_PATH):
            f = discord.File(BANNER_PATH, filename="banner.png")
            files.append(f)
            embed.set_image(url="attachment://banner.png")

        if files:
            await canal.send(embed=embed, files=files)
        else:
            await canal.send(embed=embed)

        salvar_fechamento(
            painel=painel,
            creditos=creditos,
            clientes=clientes_ativos,
            testes=testes_hoje,
            novos=novos_clientes_hoje,
            renovacoes=renovacoes_hoje,
            usuario="AUTO",
            api_ok=api_ok,
        )

        if painel == COMMISSION_PAINEL and (agora.hour, agora.minute) == (23, 0):
            rh_criar_pool_comissao(agora.strftime("%Y-%m-%d"), int(novos_clientes_hoje))

        print(f"✅ Fechamento enviado: {painel}")
        return True

# =====================
# RH (POPUP)
# =====================
async def enviar_rh_diario(dia: date):
    resultados = await asyncio.to_thread(rh_fechar_dia, TRACK_VOICE_ID, dia, MINIMO_RH)
    titulo, linhas, total_confirmados = rh_formatar_resumo_diario(dia, resultados)

    embed = discord.Embed(title=titulo, color=0xF1C40F)
    embed.description = "\n".join(linhas) if linhas else "Sem dados hoje."
    embed.add_field(
        name="Total do dia (confirmados)",
        value=f"**{_fmt_money(total_confirmados)}**",
        inline=False,
    )
    embed.set_footer(text=f"Connect RH • {INSTANCE_ID}")

    canal_rh = bot.get_channel(CANAL_RH)
    if canal_rh is None:
        canal_rh = await bot.fetch_channel(CANAL_RH)

    await canal_rh.send(embed=embed)

async def enviar_rh_mensal(ano: int, mes: int):
    resumo = await asyncio.to_thread(rh_resumo_mes, ano, mes)
    titulo, linhas, total_geral = rh_formatar_resumo_mensal(resumo)

    embed = discord.Embed(title=titulo, color=0x2ECC71)
    embed.description = "\n".join(linhas) if linhas else "Sem dados nesse mês."
    embed.add_field(
        name="Total geral do mês",
        value=f"**{_fmt_money(total_geral)}**",
        inline=False,
    )
    embed.set_footer(text=f"Connect RH • {INSTANCE_ID}")

    canal_rh = bot.get_channel(CANAL_RH)
    if canal_rh is None:
        canal_rh = await bot.fetch_channel(CANAL_RH)

    await canal_rh.send(embed=embed)

# =====================
# SCHEDULER
# =====================
@tasks.loop(seconds=35)
async def scheduler_loop():
    agora = datetime.now(BR_TIMEZONE)
    chave_minuto = agora.strftime("%Y-%m-%d %H:%M")
    hm = (agora.hour, agora.minute)

    # FECHAMENTOS
    if hm in HORARIOS_FECHAR:
        for painel in CANAIS_FECHAMENTOS:
            key = f"FECH_SCHED_{painel}"
            if ULTIMO_ENVIO.get(key) == chave_minuto:
                continue
            ULTIMO_ENVIO[key] = chave_minuto
            await fechamento_automatico(painel)

    # RH DIÁRIO
    if hm in HORARIO_RH_DIARIO:
        key = "RH_DIARIO"
        if ULTIMO_ENVIO.get(key) != chave_minuto:
            ULTIMO_ENVIO[key] = chave_minuto
            await enviar_rh_diario(agora.date())

    # RH MENSAL (ÚLTIMO DIA DO MÊS)
    if hm in HORARIO_RH_MENSAL:
        key = "RH_MENSAL"
        if ULTIMO_ENVIO.get(key) != chave_minuto:
            ULTIMO_ENVIO[key] = chave_minuto
            if agora.date() == _ultimo_dia_do_mes(agora.date()):
                await enviar_rh_mensal(agora.year, agora.month)
        # FINANCEIRO DIÁRIO
    if hm in HORARIOS_FINANCEIRO:
        for painel in CANAIS_FECHAMENTOS:
            key = f"FIN_SCHED_{painel}"
            if ULTIMO_ENVIO.get(key) == chave_minuto:
                continue
            ULTIMO_ENVIO[key] = chave_minuto
            await enviar_financeiro_diario(painel)

# =====================
# READY
# =====================
@bot.event
async def on_ready():
    criar_tabela()
    rh_setup()

    # staff
    rh_upsert_staff(1432896071213908120, "Igor", 1300, "08:00", "15:00", ativo=1)
    rh_upsert_staff(1435742153178353818, "Jaina", 650, "08:00", "15:00", ativo=1)
    rh_upsert_staff(1341566143873417276, "Biel", 1300, "15:00", "22:00", ativo=1)
    rh_upsert_staff(1333791388248051845, "Arthur", 1300, "15:00", "22:00", ativo=1, tolerancia_fim="23:00")

    print("✅ Banco pronto")

    if getattr(bot, "_scheduler_started", False):
        print("ℹ️ Scheduler já iniciado. Ignorando on_ready duplicado.")
        return

    bot._scheduler_started = True
    scheduler_loop.start()
    print("✅ Scheduler iniciado")

# =====================
# VOICE TRACK (ponto)
# =====================
@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    if not rh_is_staff_active(member.id):
        return

    before_id = before.channel.id if before.channel else None
    after_id = after.channel.id if after.channel else None

    if before_id != TRACK_VOICE_ID and after_id == TRACK_VOICE_ID:
        rh_open_session(member.id, TRACK_VOICE_ID)
        return

    if before_id == TRACK_VOICE_ID and after_id != TRACK_VOICE_ID:
        rh_close_session(member.id, TRACK_VOICE_ID)
        return

# =====================
# START
# =====================
def main():
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN não configurado no .env / Railway Variables")
    bot.run(TOKEN)

if __name__ == "__main__":
    main()
