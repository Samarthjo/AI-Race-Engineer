"""Readable names for F1 25 UDP enumerations (from the spec appendices).

Unknown values are shown as ``"<kind> <value>"``; never guessed.
"""

from __future__ import annotations

TRACKS: dict[int, str] = {
    -1: "Unknown track",
    0: "Melbourne",
    2: "Shanghai",
    3: "Sakhir (Bahrain)",
    4: "Catalunya",
    5: "Monaco",
    6: "Montreal",
    7: "Silverstone",
    9: "Hungaroring",
    10: "Spa",
    11: "Monza",
    12: "Singapore",
    13: "Suzuka",
    14: "Abu Dhabi",
    15: "Texas (COTA)",
    16: "Brazil (Interlagos)",
    17: "Austria",
    19: "Mexico",
    20: "Baku",
    26: "Zandvoort",
    27: "Imola",
    29: "Jeddah",
    30: "Miami",
    31: "Las Vegas",
    32: "Losail",
    39: "Silverstone (reverse)",
    40: "Austria (reverse)",
    41: "Zandvoort (reverse)",
}

SESSION_TYPES: dict[int, str] = {
    0: "Unknown",
    1: "Practice 1",
    2: "Practice 2",
    3: "Practice 3",
    4: "Short Practice",
    5: "Qualifying 1",
    6: "Qualifying 2",
    7: "Qualifying 3",
    8: "Short Qualifying",
    9: "One-Shot Qualifying",
    10: "Sprint Shootout 1",
    11: "Sprint Shootout 2",
    12: "Sprint Shootout 3",
    13: "Short Sprint Shootout",
    14: "One-Shot Sprint Shootout",
    15: "Race",
    16: "Race 2",
    17: "Race 3",
    18: "Time Trial",
}

GAME_MODES: dict[int, str] = {
    4: "Grand Prix",
    5: "Time Trial",
    6: "Splitscreen",
    7: "Online Custom",
    15: "Online Weekly Event",
    17: "Story Mode (Braking Point)",
    27: "My Team Career '25",
    28: "Driver Career '25",
    29: "Career '25 Online",
    30: "Challenge Career '25",
    75: "Story Mode (APXGP)",
    127: "Benchmark",
}

FORMULA: dict[int, str] = {
    0: "F1 Modern",
    1: "F1 Classic",
    2: "F2",
    3: "F1 Generic",
    4: "Beta",
    6: "Esports",
    8: "F1 World",
    9: "F1 Elimination",
}

WEATHER: dict[int, str] = {
    0: "Clear",
    1: "Light cloud",
    2: "Overcast",
    3: "Light rain",
    4: "Heavy rain",
    5: "Storm",
}

ACTUAL_COMPOUNDS: dict[int, str] = {
    7: "Inter",
    8: "Wet",
    9: "Classic dry",
    10: "Classic wet",
    11: "F2 super soft",
    12: "F2 soft",
    13: "F2 medium",
    14: "F2 hard",
    15: "F2 wet",
    16: "C5",
    17: "C4",
    18: "C3",
    19: "C2",
    20: "C1",
    21: "C0",
    22: "C6",
}

VISUAL_COMPOUNDS: dict[int, str] = {
    7: "Inter",
    8: "Wet",
    9: "Classic dry",
    10: "Classic wet",
    15: "F2 wet",
    16: "Soft",
    17: "Medium",
    18: "Hard",
    19: "F2 super soft",
    20: "F2 soft",
    21: "F2 medium",
    22: "F2 hard",
}

DRIVER_STATUS: dict[int, str] = {
    0: "in garage",
    1: "flying lap",
    2: "in lap",
    3: "out lap",
    4: "on track",
}

PIT_STATUS: dict[int, str] = {0: "none", 1: "pitting", 2: "in pit area"}

RESULT_STATUS: dict[int, str] = {
    0: "invalid",
    1: "inactive",
    2: "active",
    3: "finished",
    4: "DNF",
    5: "DSQ",
    6: "not classified",
    7: "retired",
}

TRACTION_CONTROL: dict[int, str] = {0: "off", 1: "medium", 2: "full"}
ON_OFF: dict[int, str] = {0: "off", 1: "on"}
BRAKING_ASSIST: dict[int, str] = {0: "off", 1: "low", 2: "medium", 3: "high"}
GEARBOX_ASSIST: dict[int, str] = {1: "manual", 2: "manual + suggested gear", 3: "auto"}
RACING_LINE: dict[int, str] = {0: "off", 1: "corners only", 2: "full"}

SURFACE_TYPES: dict[int, str] = {
    0: "tarmac",
    1: "rumble strip",
    2: "concrete",
    3: "rock",
    4: "gravel",
    5: "mud",
    6: "sand",
    7: "grass",
    8: "water",
    9: "cobblestone",
    10: "metal",
    11: "ridged",
}


def name(table: dict[int, str], value: int | None, kind: str = "value") -> str:
    if value is None:
        return "-"
    return table.get(int(value), f"{kind} {value}")
