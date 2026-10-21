"""Valid race/class combinations for WoW 3.3.5a (build 12340).

Matches pkg/char/profiles_gen.go; names work with agent-wow char create.
Death knights are included, subject to account and server creation restrictions.
"""

RACE_CLASSES = {
    "human": (
        "warrior", "paladin", "rogue", "priest",  "mage", "warlock",
    ),
    "orc": (
        "warrior", "hunter", "rogue",  "shaman", "warlock",
    ),
    "dwarf": (
        "warrior", "paladin", "hunter", "rogue", "priest",
    ),
    "night-elf": (
        "warrior", "hunter", "rogue", "priest",  "druid",
    ),
    "undead": (
        "warrior", "rogue", "priest",  "mage", "warlock",
    ),
    "tauren": (
        "warrior", "hunter",  "shaman", "druid",
    ),
    "gnome": (
        "warrior", "rogue",  "mage", "warlock",
    ),
    "troll": (
        "warrior", "hunter", "rogue", "priest",  "shaman", "mage",
    ),
    "blood-elf": (
        "paladin", "hunter", "rogue", "priest",  "mage", "warlock",
    ),
    "draenei": (
        "warrior", "paladin", "hunter", "priest",  "shaman", "mage",
    ),
}
