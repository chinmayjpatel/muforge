from typing import Annotated, Dict, List, Optional, Any

import muforge
import jwt
import uuid
import random

from fastapi import APIRouter, Depends, Body, HTTPException, status, Request, Query
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel

from muforge.shared.models.auth import TokenResponse, UserLogin, RefreshTokenModel

from ..db import users as users_db, auth as auth_db
from muforge.shared.utils import crypt_context
from .utils import get_real_ip

router = APIRouter()

# ---------- sessions ----------
sessions: Dict[str, Dict[str, Any]] = {}

# ---------- models ----------
class CommandRequest(BaseModel):
    session_id: str
    command: str
    args: Optional[List[str]] = []

class ShopBuyRequest(BaseModel):
    session_id: str
    item_name: str

class SearchRequest(BaseModel):
    session_id: str

# ---------- command execution (fallback) ----------
execute_command_real = None
try:
    from muforge.remaining.web_api import execute_command_simple
except ImportError:
    def execute_command_simple(command: str, args, player: dict, session: dict) -> dict:
        return {"ok": False, "msg": "Command execution not available"}

# ---------- scrap stacking helper ----------
SCRAP_STACK_SIZE = 64  # 64 per stack

def add_stacked_item(inventory, name, qty):
    # First, fill any existing partial stacks
    for item in inventory:
        if item.get("name") != name:
            continue

        existing_qty = item.get("qty", item.get("count", 0)) or 0
        if existing_qty >= SCRAP_STACK_SIZE:
            continue

        space = SCRAP_STACK_SIZE - existing_qty
        add_now = min(space, qty)
        new_qty = existing_qty + add_now

        item["qty"] = new_qty
        item["count"] = new_qty  # keep both keys in sync
        qty -= add_now

        if qty <= 0:
            return

    # If we still have leftover qty, start new stacks
    while qty > 0:
        add_now = min(SCRAP_STACK_SIZE, qty)
        inventory.append({"name": name, "qty": add_now, "count": add_now})
        qty -= add_now

@router.get("/api/ping")
async def ping():
    return {"status": "ok"}

@router.post("/start")
async def start_game():
    session_id = str(uuid.uuid4())

    player = {
        "name": "Traveler",
        "health": 100,
        "max_health": 100,
        "xp": 0,
        "xp_to_next": 50,
        "level": 1,
        "credits": 0,
        "inventory": [],
        "plasma_durability": 0,
    }

    node = {
        "id": "terra",
        "name": "Terra",
        "desc": "The home planet. Your journey begins here.",
    }

    sessions[session_id] = {
        "player": player,
        "node": node,
        "combat": None,
        "unclaimed_loot": [],
    }

    return {"session_id": session_id}

@router.get("/state")
async def get_game_state(session_id: str = Query(...)):
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    session = sessions[session_id]
    player = session["player"]
    
    # Normalize old items: ensure qty exists if only count was stored
    for item in player.get("inventory", []):
        if "qty" not in item and "count" in item:
            item["qty"] = item["count"]
    
    return {
        "player": player,
        "node": session["node"],
        "combat": session["combat"],
        "loot": session.get("unclaimed_loot", []),
    }

@router.post("/command")
async def run_command(req: CommandRequest):
    if req.session_id not in sessions:
        raise HTTPException(404, "Session not found")
    session = sessions[req.session_id]
    player = session["player"]

    # Handle deposit_scrap command
    if req.command == "deposit_scrap":
        scrap_type = req.args[0] if req.args else None
        if scrap_type not in ["Scrap", "Iron Scrap"]:
            return {"ok": False, "msg": "Invalid scrap type."}

        rate = 2 if scrap_type == "Scrap" else 5

        # Count total (read both qty and count)
        total = sum(i.get("qty", i.get("count", 0)) or 0 for i in player["inventory"] if i.get("name") == scrap_type)

        # Remove stacks
        player["inventory"] = [
            i for i in player["inventory"] if i.get("name") != scrap_type
        ]

        # Add credits
        player["credits"] += total * rate

        session["player"] = player

        return {
            "ok": True,
            "msg": f"Deposited {total} {scrap_type} for {total * rate} credits.",
            "player": player
        }

    if execute_command_real:
        result = execute_command_real(
            command=req.command,
            args=req.args or [],
            player=player,
            session=session,
        )
        # Legacy format conversion
        return {
            "success": result.get("success", True),
            "message": result.get("message", ""),
            "data": result.get("data", {}),
        }
    else:
        result = execute_command_simple(req.command, req.args or [], player, session)
        # New format: {ok, msg, player}
        return {
            "ok": result.get("ok", False),
            "msg": result.get("msg", ""),
            "error": result.get("msg", "") if not result.get("ok") else None,
            "player": result.get("player", player),
        }
    
@router.post("/heal")
async def heal_player(session_id: str = Query(...)):
    if session_id not in sessions:
        raise HTTPException(404, "Session not found")
    session = sessions[session_id]
    player = session["player"]

    amount = 15
    player["health"] = min(player["max_health"], player["health"] + amount)

    return {
        "message": f"Healed for {amount}",
        "player": player
    }

@router.post("/shop/buy")
async def shop_buy(req: ShopBuyRequest):
    if req.session_id not in sessions:
        raise HTTPException(404, "Session not found")
    session = sessions[req.session_id]
    player = session["player"]

    # Basic prices – keep in sync with your front-end ITEM_DATABASE
    PRICES = {
        "Medpack": 25,
        "Nano Repair Kit": 50,
        "Energy Cell": 10,
        "Charge Cell": 5,
        "Armor": 80,
        "Energy Shield": 100,
        "Weapon": 60,
        "Blaster": 90,
        "Plasma Blaster": 90,
    }

    name = req.item_name
    cost = PRICES.get(name)
    if cost is None:
        return {"ok": False, "msg": f"{name} cannot be bought here."}

    credits = player.get("credits", 0)
    if credits < cost:
        return {"ok": False, "msg": "Not enough credits."}

    credits -= cost
    player["credits"] = credits

    inv = player.get("inventory") or []
    # normalize to the same shape we use in /state
    inv.append({"name": name, "qty": 1})
    if name == "Plasma Blaster":
        player["plasma_durability"] = 2
    player["inventory"] = inv

    session["player"] = player

    return {
        "ok": True,
        "msg": f"Purchased {name} for {cost} credits!",
        "player": player,
    }

MAX_INVENTORY_SLOTS = 6   # keep in sync with frontend

@router.post("/search")
async def search(req: SearchRequest):
    if req.session_id not in sessions:
        raise HTTPException(404, "Session not found")
    session = sessions[req.session_id]
    player = session["player"]

    inventory = player.get("inventory") or []

    # inventory full → nothing added
    if len(inventory) >= MAX_INVENTORY_SLOTS:
        return {
            "ok": False,
            "msg": "Your inventory is full. You leave any scraps you find behind.",
            "player": player,
            "items": [],
            "credits_gained": 0,
        }

    # simple loot table – adjust however you like
    loot_table = [
        ("Scrap", 1),
        ("Iron Scrap", 1),
        ("Energy Cell", 1),
        ("Nano Repair Kit", 1),
        ("Medpack", 1),
    ]

    # 1–2 rolls of random junk
    rolls = random.randint(1, 2)
    found_items = []
    for _ in range(rolls):
        if len(inventory) >= MAX_INVENTORY_SLOTS:
            break
        name, qty = random.choice(loot_table)
        add_stacked_item(inventory, name, qty)
        found_items.append({"name": name, "qty": qty})

    # small credit bonus
    credits_gain = random.randint(5, 25)
    credits = player.get("credits", 0) + credits_gain
    player["credits"] = credits

    player["inventory"] = inventory
    session["player"] = player

    return {
        "ok": True,
        "msg": "You scour the area and find some useful scraps.",
        "player": player,
        "items": found_items,
        "credits_gained": credits_gain,
    }

@router.post("/adventure")
async def start_adventure(session_id: str = Query(...)):
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    session = sessions[session_id]
    player = session["player"]

    # Plasma Blaster durability system
    if any(i.get("name") == "Plasma Blaster" for i in player.get("inventory", [])):
        if player.get("plasma_durability", 0) == 1:
            # Warn player
            if "messages" not in session:
                session["messages"] = []
            session["messages"].append(
                "⚠️ Your Plasma Blaster will break after this adventure unless repaired with a Nano Repair Kit."
            )

        if player.get("plasma_durability", 0) > 0:
            player["plasma_durability"] -= 1

            # Break when it reaches 0
            if player["plasma_durability"] == 0:
                player["inventory"] = [
                    i for i in player["inventory"] if i.get("name") != "Plasma Blaster"
                ]
                if "messages" not in session:
                    session["messages"] = []
                session["messages"].append("💥 Your Plasma Blaster breaks!")

    # simple 1–2 raiders
    enemy_count = random.randint(1, 2)
    enemies = []
    for i in range(enemy_count):
        level = random.randint(player["level"], player["level"] + 1)
        max_hp = 40 + level * 10
        enemies.append({
            "id": i,
            "name": f"Raider L{level}",
            "health": max_hp,
            "max_health": max_hp,
            "attack": 8 + level * 2,
            "level": level,
            "credit_reward": 10 + level * 5,  # Base reward scales with level
        })

    combat = {"enemies": enemies}
    session["combat"] = combat

    # Pre-generate loot for this fight
    loot = [
        {"name": "Credits", "count": random.randint(20, 60)},
        {"name": "Scrap", "count": random.randint(1, 4)},
        {"name": "Iron Scrap", "count": random.randint(0, 2)},
    ]
    session["unclaimed_loot"] = loot

    return {
        "enemies": enemies,
        "description": "You encounter hostile raiders in the outskirts.",
        "loot": loot,
    }

@router.post("/attack")
async def attack_enemy(
    session_id: str = Query(...),
    enemy_id: int = Query(...),
    attack: int = Query(10),
):
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    session = sessions[session_id]
    player = session["player"]
    combat = session.get("combat")
    if not combat:
        raise HTTPException(status_code=400, detail="No active combat")

    enemies = combat["enemies"]

    # enemy_id is the enemy's stable "id" field, not its index
    target_index = None
    for i, enemy in enumerate(enemies):
        if enemy.get("id") == enemy_id:
            target_index = i
            break

    if target_index is None:
        raise HTTPException(status_code=400, detail="Invalid enemy id")

    events: list[str] = []

    # Player damage comes from frontend Attack stat
    attack_power = attack
    dmg = attack_power
    enemy = enemies[target_index]
    enemy["health"] = max(0, enemy["health"] - dmg)
    events.append(f"You strike {enemy['name']} for {dmg} damage.")

    # Remove dead enemy and grant rewards
    if enemy["health"] <= 0:
        events.append(f"{enemy['name']} is defeated!")
        # Grant credit reward
        credit_reward = enemy.get("credit_reward", 0)
        if credit_reward > 0:
            player["credits"] += credit_reward
            events.append(f"You loot {credit_reward} credits from {enemy['name']}.")
        enemies.pop(target_index)

    combat_won = False
    loot_to_send = []

    # Enemy turn (only if any left)
    if enemies:
        enemy = random.choice(enemies)
        # Enemy damage scales from player's attack bar
        player_attack = attack
        edmg = max(4, int(player_attack * 0.40))  # 40% of player's attack
        player["health"] = max(0, player["health"] - edmg)
        events.append(f"{enemy['name']} hits you for {edmg} damage.")
        
        # Check for player death
        if player["health"] <= 0:
            return {
                "events": events + ["You were defeated!"],
                "combat_won": False,
                "player_dead": True,
                "enemies": enemies,
                "player": player,
            }
    else:
        combat_won = True
        session["combat"] = None
        loot_to_send = session.get("unclaimed_loot", [])

    return {
        "events": events,
        "combat_won": combat_won,
        "enemies": enemies,
        "loot": loot_to_send,
        "player": player,
    }

@router.post("/loot/claim")
async def claim_loot(session_id: str = Query(...)):
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    session = sessions[session_id]
    player = session["player"]
    loot = session.get("unclaimed_loot", [])

    if "inventory" not in player or player["inventory"] is None:
        player["inventory"] = []

    # Move loot into inventory
    for item in loot:
        name = item["name"]
        qty = item.get("qty", item.get("count", 1))

        if name == "Credits":
            player["credits"] += qty
        elif name in ("Scrap", "Iron Scrap"):
            add_stacked_item(player["inventory"], name, qty)
        else:
            player["inventory"].append({"name": name, "qty": qty})

    session["unclaimed_loot"] = []

    return {
        "claimed": loot,
        "player": player,
    }

@router.post("/unlock")
async def unlock_location(session_id: str = Query(...), location_id: str = Query(...)):
    if session_id not in sessions:
        raise HTTPException(404, "Session not found")

    session = sessions[session_id]
    player = session["player"]

    # Example costs (Delta Base = 50 credits)
    unlock_costs = {
        "delta_base": 50,
        "node.delta.base": 50
    }

    if location_id not in unlock_costs:
        return {"success": False, "message": "Unknown locked location."}

    cost = unlock_costs[location_id]

    if player["credits"] < cost:
        return {"success": False, "message": "Not enough credits."}

    # Subtract credits
    player["credits"] -= cost

    # Mark as unlocked
    if "unlocked_locations" not in player:
        player["unlocked_locations"] = []

    player["unlocked_locations"].append(location_id)

    return {
        "success": True,
        "message": f"Unlocked {location_id}!",
        "player": player
    }



