from config import CANAIS_FECHAMENTOS
import discord

async def enviar_fechamento(bot, dados):
    painel = dados["painel"]
    canal_id = CANAIS_FECHAMENTOS[painel]

    canal = bot.get_channel(canal_id)

    if canal is None:
        try:
            canal = await bot.fetch_channel(canal_id)
        except Exception as e:
            print(f"❌ Não consegui buscar o canal {canal_id} ({painel}): {e}")
            return

    mensagem = (
        f"📊 **Fechamento Automático — Painel {painel}**\n\n"
        f"💳 Créditos: **{dados['creditos']:.2f}**\n"
        f"👥 Clientes ativos: **{dados.get('clientes', 0)}**\n"
        f"📅 Data: {dados['data']}\n"
        f"⏰ Hora: {dados['horario']}"
    )

    try:
        await canal.send(mensagem)
        print(f"✅ Enviado no canal {canal_id} ({painel})")
    except discord.Forbidden:
        print(f"❌ Sem permissão para enviar no canal {canal_id} ({painel})")
    except Exception as e:
        print(f"❌ Erro ao enviar no canal {canal_id} ({painel}): {e}")
