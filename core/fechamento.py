from datetime import datetime
import sqlite3

from core.painel_api import buscar_creditos, buscar_clientes_ativos
from discord_fechamento import enviar_fechamento


async def fechar_painel(bot, painel_nome: str):
    creditos = buscar_creditos(painel_nome)
    clientes_ativos = buscar_clientes_ativos(painel_nome)

    agora = datetime.now()
    data = agora.strftime("%d/%m/%Y")
    horario = agora.strftime("%H:%M")

    conn = sqlite3.connect("connect.db")
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO fechamentos (painel, creditos, clientes, data, horario)
        VALUES (?, ?, ?, ?, ?)
    """, (painel_nome, creditos, clientes_ativos, data, horario))

    conn.commit()
    conn.close()

    dados = {
        "painel": painel_nome,
        "creditos": creditos,
        "clientes": clientes_ativos,
        "data": data,
        "horario": horario
    }

    await enviar_fechamento(bot, dados)
