#!/usr/bin/env python3
"""Build the shipped default catalogue (``waifu/data/characters.seed.json``).

Why a generator instead of a hand-typed 200-entry JSON: the reference bot's
collection is *the same characters re-issued as editions* (Valentine, Summer,
Halloween, Christmas, Winter, New Year, Festival, Event, AMV, Celestial, Luxury,
Limited). Typing that by hand is how you end up with tier tables that disagree with
the roster, which is one of Summon-bot's actual bugs (its shop priced "💮 Special
Edition" while its pull table said "Special"). Here the base roster is authored once
and the editions are derived, so counts, prices and ids can never drift — and
re-running the script is how an owner regenerates the file after editing a name.

Usage::

    python scripts/build_catalogue.py            # regenerate + print a summary
    python scripts/build_catalogue.py --check    # fail if the file is stale (CI)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "waifu" / "data" / "characters.seed.json"

#: (name, series, one-line description, voice line, tags) — the base roster.
#: Descriptions are written to be usable as /ai persona hints, so they carry a
#: concrete detail rather than a genre label.
BASE: dict[int, list[tuple[str, str, str, str, str]]] = {
    1: [  # ⚪️ Common
        (
            "Naruto Uzumaki",
            "Naruto",
            "The knucklehead who runs late and never loses an argument about it.",
            "Believe it — ramen first, then the fight.",
            "knucklehead,ramen,ninja",
        ),
        (
            "Monkey D. Luffy",
            "One Piece",
            "Wants the treasure, not the paperwork. Eats meat mid-battle.",
            "I'm gonna be King of the Pirates!",
            "captain,gum-gum,appetite",
        ),
        (
            "Ichigo Kurosaki",
            "Bleach",
            "Substitute soul reaper with a curfew problem and a very loud inner voice.",
            "Sorry I'm late. Somebody was dying.",
            "substitute,hollow,sword",
        ),
        (
            "Yusuke Urameshi",
            "Yu Yu Hakusho",
            "Delinquent turned detective, mostly out of spite.",
            "I didn't say I'd save you. I said I'd win.",
            "detective,spoiled",
        ),
        (
            "Kenshin Himura",
            "Rurouni Kenshin",
            "Pacifist with a reverse-blade sword and a very long list of people to apologise to.",
            "Must not kill. Must not kill. …Fine.",
            "wanderer,oath",
        ),
        (
            "Yuki Sohma",
            "Fruits Basket",
            "Straight-A student terrified of being seen clearly.",
            "Please don't look at me too long.",
            "school,secret",
        ),
        (
            "Tanjiro Kamado",
            "Demon Slayer",
            "Sells charcoal, apologises to demons, keeps walking.",
            "I'll carry you home. Both of us.",
            "kindness,headbutt",
        ),
        (
            "Edward Elric",
            "Fullmetal Alchemist",
            "Short, furious, and genuinely the best alchemist in the country.",
            "Call me small again. I dare you.",
            "alchemy,automail",
        ),
        (
            "Hajime Iwaizumi",
            "Haikyuu!!",
            "Dedicated ace, terrible at feelings, excellent at reads.",
            "One more ball. Then I'll talk.",
            "volleyball,ace",
        ),
        (
            "Kanna Kamui",
            "Miss Kobayashi's Dragon Maid",
            "Chaos dragon in a school uniform, fluent in menace.",
            "I made you a friend. It's a knife.",
            "chaos,dragon",
        ),
        (
            "Satsuki Kiryuin",
            "Kill la Kill",
            "Student council president who rules by uniform policy.",
            "Confidence is a uniform. Put it on.",
            "uniform,authority",
        ),
        (
            "Gowther",
            "Seven Deadly Sins",
            "Memory weaver who builds illusions to avoid being perceived.",
            "Look away. That's the whole trick.",
            "illusion,quiet",
        ),
        (
            "Zenitsu Agatsuma",
            "Demon Slayer",
            "Screams, cries, then sleeps through the part where he wins.",
            "I don't want to die! (…fine.)",
            "coward,thunder",
        ),
        (
            "Inuyasha",
            "Inuyasha",
            "Half-demon who complains about the walk the entire time.",
            "Don't get used to me.",
            "fangs,rat",
        ),
        (
            "Nanami Kento",
            "Jujutsu Kaisen",
            "Salaryman sorcerer who clocks out on principle, never on time.",
            "This is unpaid overtime. Leave.",
            "suit,seven-twenty",
        ),
        (
            "Koro-sensei",
            "Assassination Classroom",
            "Machs-20 tentacled octopus who grades on a curve.",
            "Your homework, and your assassination attempt.",
            "teacher,tentacles",
        ),
        (
            "Hange Zoë",
            "Attack on Titan",
            "Will hug a Titan if the data is good enough.",
            "Hold still. For SCIENCE.",
            "titan,notebook",
        ),
        (
            "Benimaru",
            "That Time I Got Reincarnated as a Slime",
            "General of the Jura Tempest Federation, enthusiastic about storms.",
            "The storm obeys me. Do you?",
            "lightning,general",
        ),
    ],
    2: [  # 🔵 Rare
        (
            "Levi Ackerman",
            "Attack on Titan",
            "Clean freak with the deadliest blade in the scout regiment.",
            "Tch. Die after you clean up.",
            "captain,cleaning",
        ),
        (
            "Anya Forger",
            "Spy x Family",
            "Reads minds, fails spelling, knows exactly what she's doing.",
            "Waku waku! (I know your secrets.)",
            "telepath,peanuts",
        ),
        (
            "Power",
            "Chainsaw Man",
            "Self-appointed god, terrible at money, excellent at blood.",
            "I, Power, permit you to breathe.",
            "cat,blood,oink",
        ),
        (
            "Mikasa Ackerman",
            "Attack on Titan",
            "Scarves, slices, and does not explain herself.",
            "I'll carry the blades. You carry the plan.",
            "scarf,guardian",
        ),
        (
            "Nami",
            "One Piece",
            "The only honest person on a ship of thieves, and the best navigator alive.",
            "Twenty percent. Non-negotiable.",
            "cartographer,tangerines",
        ),
        (
            "Roronoa Zoro",
            "One Piece",
            "Gets lost going downhill. Cuts mountains going anywhere.",
            "Lost. But on my way.",
            "swords,naps",
        ),
        (
            "Shoto Todoroki",
            "My Hero Academia",
            "Half-and-half on purpose, and done inheriting grudges.",
            "I'll decide what my left side does.",
            "icefire,inherited",
        ),
        (
            "Katsuki Bakugo",
            "My Hero Academia",
            "Explosions as a communication strategy.",
            "DIE! (…good morning.)",
            "explosions,pride",
        ),
        (
            "Yor Forger",
            "Spy x Family",
            "Assassin by night, worst cook by every hour.",
            "I did not poison it. It's just like that.",
            "merciless,domestic",
        ),
        (
            "Toshiro Hitsugaya",
            "Bleach",
            "Short king with a winter hobby and zero patience.",
            "Say it again. Slower.",
            "ice,prodigy",
        ),
        (
            "Esdeath",
            "Akame ga Kill!",
            "War is her idea of a date, and she means it.",
            "Break me or join me.",
            "frost,general",
        ),
        (
            "Killua Zoldyick",
            "Hunter x Hunter",
            "Ex-assassin learning to be a friend, badly, on purpose.",
            "I'll be your lightning. Don't ask why.",
            "thunder,reformed",
        ),
        (
            "Gon Freecss",
            "Hunter x Hunter",
            "Chases his father by fishing up mountains.",
            "My dad fishes. So I'll be a Hunter.",
            "reckless,angler",
        ),
        (
            "Nico Robin",
            "One Piece",
            "Reads history that the world would burn to keep unread.",
            "I want to live. Say it with me.",
            "archaeologist,quiet",
        ),
        (
            "Sanji",
            "One Piece",
            "Never kicks a lady, will kick the rest of you.",
            "Order for the lady first.",
            "chef,devoted",
        ),
        (
            "Kyojuro Rengoku",
            "Demon Slayer",
            "Yells encouragement like it's a weapon. It is.",
            "SET YOUR HEART ABLAZE!",
            "flame,hashira",
        ),
        (
            "Mayuri Kurotsuchi",
            "Bleach",
            "Scientist whose ethics committee is also him.",
            "Hold still. This will be educational.",
            "poison,curious",
        ),
        (
            "Speed-o-Sound Sonic",
            "One Punch Man",
            "Ninja who is second-fastest and hates every word of it.",
            "Ninja art: don't lose.",
            "speed,rival",
        ),
    ],
    3: [  # 💮 Special Edition
        (
            "Sailor Moon",
            "Sailor Moon",
            "Cry first, transform second, win third.",
            "In the name of the Moon, I'll cry and then fight!",
            "moon,princess",
        ),
        (
            "Motoko Kusanagi",
            "Ghost in the Shell",
            "Full cyborg, full authority, no patience for ghosts in the machine.",
            "My shell, my rules.",
            "cyborg,major",
        ),
        (
            "Alphonse Elric",
            "Fullmetal Alchemist",
            "Soul in armour, gentlest hands in Amestris.",
            "I can't hug you. I'll try anyway.",
            "armour,knight",
        ),
        (
            "Roy Mustang",
            "Fullmetal Alchemist",
            "Flame alchemist with a policy brief and a grudge.",
            "I'll burn the way to the top.",
            "flame,ambition",
        ),
        (
            "Kurogane",
            "Tsubasa: Reservoir Chronicle",
            "Ninja with a temper, a blade, and one protective job description.",
            "Say her name again. I dare you.",
            "ninja,guardian",
        ),
        (
            "Yuno Gasai",
            "Future Diary",
            "Timestamps every lie you tell her.",
            "I checked the diary. You lied.",
            "yandere,obsessed",
        ),
        (
            "Zero Two",
            "Darling in the Franxx",
            "Kisses like a threat and calls you darling.",
            "Darling. Inward. Now.",
            "parasite,horns",
        ),
        (
            "Pikachu",
            "Pokémon",
            "Refuses the Poké Ball, chooses to stay.",
            "Pika. (That means I'm not going in the box.)",
            "electric,stubborn",
        ),
        (
            "Usagi Tsukino",
            "Sailor Moon",
            "Late to school, on time for destiny.",
            "I'll be late AND save the world.",
            "moon,idol",
        ),
        (
            "Reinhard van Astrea",
            "Rezero",
            "Sword saint with a cape and no ego, which is somehow worse.",
            "Allow me to handle this.",
            "knight,perfect",
        ),
        (
            "Emilia",
            "Rezero",
            "Half-elf who apologises for existing and then freezes the room.",
            "I'm not like the others. Please believe me.",
            "spirit,ice",
        ),
        (
            "Rem",
            "Rezero",
            "Mace, maid, and a devotion that outlasts the plot.",
            "I love you. That's the whole speech.",
            "maid,devotion",
        ),
        (
            "Asuka Langley",
            "Evangelion",
            "Two-word answers, pilot-first, feelings later.",
            "Synchronisation: perfect. Feelings: later.",
            "pilot,brat",
        ),
        (
            "Shinji Ikari",
            "Evangelion",
            "Gets in the robot every single time.",
            "I'll get in. Don't ask me twice.",
            "pilot,reluctant",
        ),
        (
            "Rei Ayanami",
            "Evangelion",
            "Says three sentences an episode and they all land.",
            "I am not a doll. Ask again.",
            "first-child,quiet",
        ),
        (
            "Albedo",
            "Overlord",
            "Chief librarian, unreasonably in love, terrifying in a dress.",
            "I'll flay them. For you, Ainz-sama.",
            "demon,devoted",
        ),
        (
            "Shinobu Kocho",
            "Demon Slayer",
            "Smiles while injecting enough poison to end a bloodline.",
            "Smile! It's much easier this way.",
            "insect,poison",
        ),
        (
            "Giorno Giovanna",
            "JoJo's Bizarre Adventure",
            "Wants to be a gangster with a code. Somehow works.",
            "I have a dream: silence the drugs.",
            "stand,gold",
        ),
    ],
    4: [  # ⭐ Legendary
        (
            "Son Goku",
            "Dragon Ball Z",
            "Eats after the fight, trains during the funeral, saves everyone.",
            "I like fighting strong guys!",
            "saiyan,ascended",
        ),
        (
            "Goku Black",
            "Dragon Ball Super",
            "A righteous god with a stolen body and a genocidal plan.",
            "Justice is a blade, mortal.",
            "tulip,sadist",
        ),
        (
            "Vegeta",
            "Dragon Ball",
            "Prince of all he refuses to admit he loves.",
            "I do not lose. I *postpone*.",
            "pride,saiyan",
        ),
        (
            "Lelouch vi Britannia",
            "Code Geass",
            "Chessmaster with a god-complex and a crush on his sister.",
            "The pieces are ready. I'll move them.",
            "geass,strategist",
        ),
        (
            "Light Yagami",
            "Death Note",
            "Top of his class, worst possible hobby.",
            "I'll fix the world. Give me a notebook.",
            "notebook,god",
        ),
        (
            "L Lawliet",
            "Death Note",
            "Sits like a bird, solves everything, sleeps never.",
            "There is a 5% chance you're wrong. I like those odds.",
            "detective,sugar",
        ),
        (
            "Madara Uchiha",
            "Naruto",
            "War as a peace plan, meteor as a opening move.",
            "Would you like to dance?",
            "uchiha,madara",
        ),
        (
            "Obito Uchiha",
            "Naruto",
            "Built a fake world because the real one took the girl he loved.",
            "This reality is a lie. I'll replace it.",
            "mask,grief",
        ),
        (
            "Itachi Uchiha",
            "Naruto",
            "Murdered a clan to spare a brother, died mid-apology.",
            "Forgive me, Sasuke. That's the last lie.",
            "crow,sacrifice",
        ),
        (
            "Kisuke Urahara",
            "Bleach",
            "Hat, geta, and the only shop that saves the plot.",
            "Would you like some candy before the fight?",
            "shopkeeper,genius",
        ),
        (
            "Sosuke Aizen",
            "Bleach",
            "Smiled for 300 episodes and then meant it.",
            "Since when were you under the impression…",
            "butterfly,ambition",
        ),
        (
            "Isshin Shiba",
            "Bleach",
            "Father, husband, former captain, terrible at all three on purpose.",
            "I trained you. Badly, but I did.",
            "dad,sword",
        ),
        (
            "Rimuru Tempest",
            "That Time I Got Reincarnated as a Slime",
            "Ate a dragon, gained a name, built a country.",
            "I'll eat your attack and say thanks.",
            "slime,sovereign",
        ),
        (
            "Ainz Ooal Gown",
            "Overlord",
            "Skeleton king, decent manager, terrible at being evil.",
            "I'll consider it. (I have no idea.)",
            "lich,admin",
        ),
        (
            "Escanor",
            "Seven Deadly Sins",
            "Gratitude in the form of an indestructible weapon.",
            "For my beloved goddess, I will break you.",
            "sin,grace",
        ),
        (
            "Meliodas",
            "Seven Deadly Sins",
            "Destroyer of worlds, mediocre bartender, excellent friend.",
            "My sin? I'll show you. Full count.",
            "dragon,sinful",
        ),
        (
            "Erza Scarlet",
            "Fairy Tail",
            "Requip, rage, and a dress code she enforces personally.",
            "Change into steel. Fight me.",
            "titania,armor",
        ),
        (
            "Mirajane Strauss",
            "Fairy Tail",
            "Smiles like an angel, takes the demon form without apology.",
            "The party's over. Literally.",
            "satan,smile",
        ),
    ],
    5: [  # 🛸 Mythic Edition
        (
            "Saitama",
            "One Punch Man",
            "Won so completely that boredom became his only rival.",
            "OK. I'll win in one. Then I'll shop.",
            "bald,limitless",
        ),
        (
            "Garou",
            "One Punch Man",
            "Hero hunter who wanted the system to lose, not people to die.",
            "I'll be the absolute evil you made me.",
            "hunt,monster",
        ),
        (
            "Mob Shigeo",
            "Mob Psycho 100",
            "If his emotions hit 100%, the neighbourhood loses a hill.",
            "I'll keep it at 0. For everyone's sake.",
            "exorcist,quiet",
        ),
        (
            "Reigen Arataka",
            "Mob Psycho 100",
            "Zero powers, all charisma, somehow the best mentor in the show.",
            "That'll be 100,000 yen. Trust me.",
            "fraud,spirit",
        ),
        (
            "Gojo Satoru",
            "Jujutsu Kaisen",
            "Infinite distance, zero chill, the reason the students survive.",
            "Nah, I'd win.",
            "six-eye,limitless",
        ),
        (
            "Sukuna Ryomen",
            "Jujutsu Kaisen",
            "King of curses, tenant of a very unhappy teenager.",
            "Kneel. I said it once.",
            "king,curse",
        ),
        (
            "Yami Sukehiro",
            "Black Clover",
            "Griffon captain who reads the room and cuts it.",
            "I'll cut. You figure out where.",
            "captain,anti-magic",
        ),
        (
            "Asta",
            "Black Clover",
            "No magic, nine hundred push-ups, and a five-leaf luck.",
            "I'll outwork your talent.",
            "anti-magic,never-give-up",
        ),
        (
            "All Might",
            "My Hero Academia",
            "Symbol of Peace on a timer, still shows up smiling.",
            "I am here! (For three more hours.)",
            "symbol,one-for-all",
        ),
        (
            "All For One",
            "My Hero Academia",
            "Collects quirks the way other people collect stamps.",
            "You'll be a villain. I'll make sure of it.",
            "thief,architect",
        ),
        (
            "Ichigo Kurosaki (Hollow)",
            "Bleach",
            "Mask on, restraint off, a very different conversation.",
            "You wanted the monster? Here he is.",
            "hollow,masked",
        ),
        (
            "Kenshin (Battosai)",
            "Rurouni Kenshin",
            "The legend the Shogunate feared, back for one night.",
            "I promised. …One cut.",
            "assassin,oath",
        ),
        (
            "Madoka Kaname",
            "Madoka Magica",
            "Rewrote the laws of the universe so nobody has to sign a contract.",
            "I'll take the wish. All of them.",
            "witch-law,gentle",
        ),
        (
            "Homura Akemi",
            "Madoka Magica",
            "Time loops, grief, and one friendship she refuses to lose.",
            "I'll do it again. Forever, if needed.",
            "shield,loop",
        ),
        (
            "Kaneki Ken",
            "Tokyo Ghoul",
            "Half-ghoul, half-tragedy, fully done with being a victim.",
            "1000 minus 7.",
            "ghoul,masked",
        ),
        (
            "Griffith",
            "Berserk",
            "Made a bargain with God for a kingdom, kept the receipt.",
            "I'll build it. You'll pay for it.",
            "falcon,apostle",
        ),
        (
            "Guts",
            "Berserk",
            "Oversized sword, smaller patience, survives betrayal twice.",
            "I'll swing until the sun comes up.",
            "black-swordsman",
        ),
        (
            "Vanitas",
            "The Case Study of Vanitas",
            "Bones-and-masks vampire doctor, with a stolen grimoire and a hatred of vampires.",
            "Your illness has a name. I'll say it.",
            "noé,curse",
        ),
    ],
}

#: How each event tier re-skins the base roster: prefix/suffix flavour + tag set.
EDITIONS: dict[int, tuple[str, str, str, str]] = {
    6: (
        "💝 Valentine",
        "Confession Edition",
        "chocolate,confession",
        "carries a box they spent three weeks on and will never mention",
    ),
    7: (
        "🏖️ Summer",
        "Beach Edition",
        "beach,heat",
        "swims like a champion, naps like a professional",
    ),
    8: (
        "🌧️ Rainy",
        "Rain Edition",
        "rain,umbrella",
        "moves slower in the rain and hits twice as hard",
    ),
    9: (
        "🎃 Halloween",
        "Lantern Edition",
        "halloween,costume",
        "carves the pumpkin before the funeral, then goes out trick-or-treating alone",
    ),
    10: (
        "🎄 Christmas",
        "Snowfall Edition",
        "christmas,lights",
        "leaves food out for people who never come",
    ),
    11: (
        "❄️ Winter",
        "Frost Edition",
        "winter,frost",
        "breath fogs the moment the temperature drops",
    ),
    12: (
        "🎇 New Year",
        "First Sunrise Edition",
        "newyear,dawn",
        "wakes before everyone to watch the first sunrise alone",
    ),
    13: (
        "🎍 Festival",
        "Lantern Edition",
        "festival,yukata",
        "wins every game at the festival and gives the prizes away",
    ),
    14: (
        "🎥 AMV",
        "Music Video Edition",
        "amv,edit",
        "lives entirely in the edit, cut on the beat",
    ),
    15: (
        "🎉 Event",
        "Anniversary Edition",
        "event,anniversary",
        "the reason the banner exists at all",
    ),
    16: (
        "🌌 Celestial",
        "Starlight Edition",
        "celestial,stars",
        "borrows light from somewhere it refuses to name",
    ),
    17: (
        "💎 Luxury",
        "Gilded Edition",
        "luxury,gold",
        "wears the money, never talks about the price",
    ),
    18: (
        "🔮 Limited",
        "One-Time Edition",
        "limited,sealed",
        "will never be printed again and knows it",
    ),
}

#: Which base characters get an edition (curated, not mechanical — the tiers differ
#: in size on purpose so the odds and the roster match).
EDITION_PICKS: dict[int, list[tuple[int, int]]] = {
    6: [(1, 5), (2, 8), (3, 6), (4, 12), (5, 11), (4, 5), (3, 14)],
    7: [(1, 2), (2, 5), (2, 15), (3, 6), (4, 2), (5, 17), (3, 16)],
    8: [(1, 6), (2, 9), (3, 10), (4, 11), (5, 12), (2, 6)],
    9: [(1, 9), (2, 11), (3, 3), (4, 13), (5, 8), (3, 15), (2, 17)],
    10: [(1, 4), (2, 13), (3, 12), (4, 14), (5, 15), (1, 16)],
    11: [(1, 10), (2, 9), (3, 8), (4, 10), (5, 14), (2, 11)],
    12: [(1, 1), (2, 16), (3, 1), (4, 15), (5, 7), (1, 13)],
    13: [(1, 7), (2, 1), (3, 5), (4, 6), (5, 10), (3, 9), (2, 3)],
    14: [(4, 0), (4, 2), (5, 0), (5, 2), (3, 17)],
    15: [(1, 3), (2, 4), (3, 7), (4, 4), (5, 5), (4, 9), (3, 13), (2, 14)],
    16: [(5, 1), (5, 3), (4, 1), (4, 7), (5, 12), (5, 13), (4, 6)],
    17: [(5, 4), (5, 6), (5, 8), (4, 8), (4, 10), (5, 15), (5, 16)],
    18: [(5, 9), (5, 10), (5, 11), (5, 13), (5, 14), (5, 16), (5, 17)],
}

#: Their actual live character, kept for parity with the migrated database.
LIVE_ROWS: list[dict[str, object]] = [
    {
        "name": "Son Goku",
        "anime": "Dragon Ball",
        "rarity_id": 14,
        "stat_power": 95,
        "tags": "amv,saiyan,live-import",
        "description": "The AMV cut that the reference bot shipped: Goku, ascended, cut to the beat.",
        "voice_line": "I like fighting strong guys!",
        "image_url": "",
        "is_active": True,
    }
]


def base_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for rarity_id, entries in sorted(BASE.items()):
        for index, (name, anime, description, voice, tags) in enumerate(entries):
            rows.append(
                {
                    "name": name,
                    "anime": anime,
                    "rarity_id": rarity_id,
                    "stat_power": 10 + rarity_id * 14 + (index % 7) * 3,
                    "tags": tags,
                    "description": description,
                    "voice_line": voice,
                }
            )
    return rows


def edition_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for rarity_id, picks in sorted(EDITION_PICKS.items()):
        flavour, suffix, tags, detail = EDITIONS[rarity_id]
        for target_tier, index in picks:
            pool = BASE.get(target_tier) or BASE[max(BASE)]
            name, anime, _description, _voice, base_tags = pool[index % len(pool)]
            rows.append(
                {
                    "name": f"{name} {flavour}",
                    "anime": f"{anime} · {suffix}",
                    "rarity_id": rarity_id,
                    "stat_power": 30 + rarity_id * 16 + index * 2,
                    "tags": f"edition,{base_tags},{tags}",
                    "description": f"{edition_word(flavour)} rework of {name} — {detail}.",
                    "voice_line": _voice,
                    "banner_weight": 1.5 if rarity_id >= 16 else 1.0,
                }
            )
    return rows


def edition_word(flavour: str) -> str:
    """ "🎃 Halloween" → "Halloween". Keeps the label and the sentence in sync."""
    return flavour.split(" ", 1)[-1] if " " in flavour else flavour


def build() -> list[dict[str, object]]:
    rows = base_rows() + edition_rows() + LIVE_ROWS
    seen: set[str] = set()
    unique: list[dict[str, object]] = []
    for row in rows:
        key = f"{row['name']}|{row['rarity_id']}"
        if key in seen:
            continue
        seen.add(key)
        row.setdefault("is_active", True)
        unique.append(row)
    return unique


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="verify the committed file matches this script"
    )
    args = parser.parse_args()
    payload = json.dumps(build(), ensure_ascii=False, indent=1) + "\n"
    if args.check:
        current = OUT.read_text() if OUT.exists() else ""
        if current != payload:
            print("characters.seed.json is stale — run scripts/build_catalogue.py", file=sys.stderr)
            return 1
        print("catalogue is up to date")
        return 0
    OUT.write_text(payload)
    counts: dict[int, int] = {}
    for row in json.loads(payload):
        counts[row["rarity_id"]] = counts.get(row["rarity_id"], 0) + 1
    print(
        f"wrote {OUT.relative_to(ROOT)} — {len(payload.splitlines())} lines, {sum(counts.values())} characters"
    )
    for tier in sorted(counts):
        print(f"  tier {tier:>2}: {counts[tier]:>3} characters")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
