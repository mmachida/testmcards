import os
import random
import requests
from twitchio.ext import commands
from pymongo import MongoClient
from datetime import datetime, timezone, timedelta

# --- Configurações ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_NAME = os.getenv("CHANNEL_NAME", "GrandeMOficial")  # opcionalmente com valor padrão
MONGO_URI = os.getenv("MONGO_URI")

if not BOT_TOKEN or not CHANNEL_NAME or not MONGO_URI:
    raise RuntimeError("❌ Variáveis de ambiente ausentes! Verifique BOT_TOKEN, CHANNEL_NAME e MONGO_URI.")

# --- Conexão MongoDB ---
client = MongoClient(MONGO_URI)
db = client["gacha"]
users_col = db["users"]
cards_col = db["cards"]
inventory_col = db["inventory"]
log_history_col = db["log_history"]

# --- Rates (probabilidades) ---
RATES = {
    "common": 0.60,
    "uncommon": 0.20,
    "rare": 0.12,
    "epic": 0.06,
    "legendary": 0.02
}

# --- Cache de cartas por raridade ---
CARD_CACHE = {}

def load_card_cache():
    """Carrega todas as cartas e agrupa por raridade."""
    global CARD_CACHE
    CARD_CACHE.clear()
    for card in cards_col.find({}, {"_id": 1, "name": 1, "rarity": 1, "image_url": 1}):
        rarity = card.get("rarity", "common")
        CARD_CACHE.setdefault(rarity, []).append(card)
    print(f"✅ Cache carregado: {sum(len(v) for v in CARD_CACHE.values())} cartas em memória.")

load_card_cache()

# --- Função para logar eventos ---
def log_event(twitch_id, twitch_name, action, details=None):
    """
    Insere o log no Mongo e imprime no console.
    Aceita detalhes nos formatos antigos (name, got_new, tokens_gained, pity_forced)
    ou novos (card_name, nova_carta, tokens_ganhos).
    """
    br_tz = timezone(timedelta(hours=-3))  # Horário de Brasília
    timestamp = datetime.now(br_tz)

    details = details or {}

    # Normaliza nomes diferentes possíveis vindo de partes distintas do código
    card_name = details.get("name") or details.get("card_name") or details.get("card") or "Desconhecida"
    rarity = details.get("rarity") or details.get("raridade") or "???"

    # booleano se foi nova carta
    got_new = details.get("got_new")
    if got_new is None:
        # aceitar "nova_carta" (pt) ou inferir pela presença de tokens_ganhos==0
        got_new = details.get("nova_carta")
    if got_new is None:
        # fallback: se tokens_ganhos > 0 and tokens_ganhos present, assume repetida => got_new False
        if "tokens_ganhos" in details or "tokens_gained" in details:
            # if tokens > 0 and we expect tokens only on repeats, set accordingly
            # but keep safer default False when tokens present
            got_new = False
        else:
            got_new = False

    # normaliza tokens
    tokens = details.get("tokens_gained")
    if tokens is None:
        tokens = details.get("tokens_ganhos")
    if tokens is None:
        tokens = details.get("tokens") or 0

    # pity
    pity_forced = details.get("pity_forced") or details.get("pity") or False

    # Insere no Mongo (mantém o documento original em details para consulta futura)
    entry = {
        "twitch_id": str(twitch_id),
        "twitch_name": twitch_name,
        "action": action,
        "details": details,
        "timestamp": timestamp
    }
    log_history_col.insert_one(entry)

    # Formatado para console (legível)
    pity = " 🌟 PITY" if pity_forced else ""
    status = "NOVA" if got_new else f"REPETIDA (+{tokens} tokens)"
    print(f"{timestamp.strftime('%Y-%m-%d %H:%M:%S')} - {action} - {card_name} ({rarity}){pity} - {status}")


# --- Função para sortear raridade considerando os rates ---
def choose_rarity_with_rates():
    rarities = list(RATES.keys())
    weights = [RATES[r] for r in rarities]
    return random.choices(rarities, weights=weights, k=1)[0]

# --- Função para escolher uma carta aleatória do cache ---
def get_random_card_from_cache(rarity):
    cards = CARD_CACHE.get(rarity)
    if not cards:
        all_cards = [c for cards in CARD_CACHE.values() for c in cards]
        return random.choice(all_cards) if all_cards else None
    return random.choice(cards)

# --- Função principal: dar carta (com pity + tokens) ---
def give_random_card(twitch_id, twitch_name):
    # --- RATES DE RARIDADE ---
    rarity_rates = {
        "common": 0.60,
        "uncommon": 0.20,
        "rare": 0.12,
        "epic": 0.06,
        "legendary": 0.02
    }

    # --- LÓGICA DE PITY ---
    user = users_col.find_one({"twitch_id": twitch_id})
    pity_counter = 0
    if not user:
        user_id = users_col.insert_one({
            "twitch_id": twitch_id,
            "twitch_name": twitch_name,
            "total_unique_cards": 0,
            "pity_counter": 0
        }).inserted_id
    else:
        user_id = user["_id"]
        pity_counter = user.get("pity_counter", 0)

    # Determina raridade
    if pity_counter >= 14:
        # pity garantido
        rarity = random.choices(["epic", "legendary"], weights=[0.7, 0.3])[0]
        pity_counter = 0
    else:
        rarity = random.choices(
            list(rarity_rates.keys()), weights=list(rarity_rates.values())
        )[0]
        if rarity in ["epic", "legendary"]:
            pity_counter = 0
        else:
            pity_counter += 1

    # Atualiza pity no banco
    users_col.update_one(
        {"_id": user_id},
        {"$set": {"pity_counter": pity_counter, "twitch_name": twitch_name}}
    )

    # Pega carta da raridade escolhida
    card = cards_col.aggregate([
        {"$match": {"rarity": rarity}},
        {"$sample": {"size": 1}}
    ]).next()

    # Atualiza inventário
    inv = inventory_col.find_one({"user_id": user_id, "card_id": card["_id"]})
    nova_carta = False
    tokens_ganhos = 0

    if inv:
        inventory_col.update_one({"_id": inv["_id"]}, {"$inc": {"quantity": 1}})
        tokens_ganhos = 5  # exemplo: carta repetida dá 5 tokens
    else:
        inventory_col.insert_one({"user_id": user_id, "card_id": card["_id"], "quantity": 1})
        users_col.update_one({"_id": user_id}, {"$inc": {"total_unique_cards": 1}})
        nova_carta = True
        tokens_ganhos = 20  # exemplo: nova carta dá 20 tokens

    # Atualiza tokens
    users_col.update_one({"_id": user_id}, {"$inc": {"tokens": tokens_ganhos}})

    # Log completo (no Mongo e console)
    log_msg = f"Recebeu carta - {card['name']} ({rarity}) - {'Nova' if nova_carta else 'Repetida'} - +{tokens_ganhos} tokens"
    print(f"🪙 {twitch_name}: {log_msg}")

    log_event(
        twitch_id,
        twitch_name,
        "Recebeu carta",
        {
            "card_name": card["name"],
            "rarity": rarity,
            "nova_carta": nova_carta,
            "tokens_ganhos": tokens_ganhos
        }
    )

    # Overlay
    image_url = card.get("image_url") or "https://via.placeholder.com/285x380?text=Sem+Imagem"
    payload = {
        "user": twitch_name,
        "name": card["name"],
        "rarity": rarity,
        "image_url": image_url
    }
    try:
        response = requests.post("http://127.0.0.1:5000/show_card", json=payload, timeout=1.5)
        if response.status_code != 200:
            print(f"[Overlay] ⚠️ Servidor respondeu com status {response.status_code}")
    except requests.exceptions.RequestException:
        print("[Overlay] 🚫 Erro ao enviar carta para o overlay.")

    return card


# --- Classe do Bot ---
class MGachaBot(commands.Bot):
    def __init__(self):
        super().__init__(token=BOT_TOKEN, prefix="!", initial_channels=[CHANNEL_NAME])

    async def event_ready(self):
        print(f"✅ Bot conectado como: {self.nick}")

    async def event_message(self, message):
        print(f"💬 {message.author.name}: {message.content}")
        if message.echo:
            return

        twitch_id = message.author.id
        twitch_name = message.author.name

        tags = message.tags or {}
        bits = 0
        if isinstance(tags, dict) and "bits" in tags:
            try:
                bits = int(tags["bits"])
            except Exception:
                bits = 0
                
        if bits == 0:
            result = give_random_card(twitch_id, twitch_name)
            if not result:
                return

        if bits > 0:
            result = give_random_card(twitch_id, twitch_name)
            if not result:
                return
            card = result["card"]
            log_event(
                twitch_id,
                twitch_name,
                f"Doou {bits} bits e recebeu",
                {
                    "name": card.get("name"),
                    "rarity": card.get("rarity"),
                    "card_id": str(card.get("_id")),
                    "bits": bits,
                    "pity_triggered": result["pity_triggered"]
                }
            )

        await self.handle_commands(message)

bot = MGachaBot()

# --- Comando !test ---
@commands.command(name="test")
async def test(ctx):
    twitch_id = ctx.author.id
    twitch_name = ctx.author.name
    result = give_random_card(twitch_id, twitch_name)
    if not result:
        await ctx.send("⚠️ Nenhuma carta disponível!")
        return
bot.add_command(test)

# --- Rodar bot ---
if __name__ == "__main__":
    bot.run()

