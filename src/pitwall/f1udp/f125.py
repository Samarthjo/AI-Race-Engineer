"""F1 25 UDP format (``packetFormat`` 2025): every packet layout as a numpy dtype.

Sources
-------
* EA/Codemasters "F1 25 UDP Specification" (field names follow it, in snake_case).
* Cross-checked against two independent F1 25 implementations, which agree on all 16
  packet sizes and on the field layouts below:
  - Rust crate ``f1-game-library-models-25`` v0.5.0
  - npm ``@deltazeroproduction/f1-udp-parser`` v1.2.2
* Still to do: re-verify against the official document once it is in ``docs/spec/``.

Conventions
-----------
* Little-endian, packed (no padding): numpy structured dtypes built from field lists
  are packed by default.
* Per-wheel arrays are ordered **RL, RR, FL, FR** (``WHEELS``).
* Every packet dtype starts with the 29-byte ``header``.
* Sizes are asserted at import time against ``PACKET_SIZES``.
"""

from __future__ import annotations

import numpy as np

from pitwall.f1udp.header import PacketId

PACKET_FORMAT = 2025
MAX_CARS = 22
WHEELS = ("rl", "rr", "fl", "fr")

MAX_MARSHAL_ZONES = 21
MAX_WEATHER_FORECAST_SAMPLES = 64
MAX_SESSIONS_IN_WEEKEND = 12
MAX_LAPS_IN_HISTORY = 100
MAX_TYRE_STINTS = 8
MAX_TYRE_SETS = 20
MAX_LAP_POSITIONS_LAPS = 50

U8, I8, U16, I16, U32, F32, F64 = "u1", "i1", "<u2", "<i2", "<u4", "<f4", "<f8"


def _wheels(dtype: str) -> tuple[str, tuple[int]]:
    return (dtype, (4,))


HEADER = np.dtype(
    [
        ("packet_format", U16),
        ("game_year", U8),
        ("game_major_version", U8),
        ("game_minor_version", U8),
        ("packet_version", U8),
        ("packet_id", U8),
        ("session_uid", "<u8"),
        ("session_time", F32),
        ("frame_identifier", U32),
        ("overall_frame_identifier", U32),
        ("player_car_index", U8),
        ("secondary_player_car_index", U8),
    ]
)

# --- 0: Motion ------------------------------------------------------------------------
CAR_MOTION = np.dtype(
    [
        ("world_position_x", F32),
        ("world_position_y", F32),
        ("world_position_z", F32),
        ("world_velocity_x", F32),
        ("world_velocity_y", F32),
        ("world_velocity_z", F32),
        ("world_forward_dir_x", I16),  # normalised: divide by 32767
        ("world_forward_dir_y", I16),
        ("world_forward_dir_z", I16),
        ("world_right_dir_x", I16),
        ("world_right_dir_y", I16),
        ("world_right_dir_z", I16),
        ("g_force_lateral", F32),
        ("g_force_longitudinal", F32),
        ("g_force_vertical", F32),
        ("yaw", F32),
        ("pitch", F32),
        ("roll", F32),
    ]
)
PACKET_MOTION = np.dtype([("header", HEADER), ("car", CAR_MOTION, (MAX_CARS,))])

# --- 1: Session -----------------------------------------------------------------------
MARSHAL_ZONE = np.dtype([("zone_start", F32), ("zone_flag", I8)])
WEATHER_FORECAST = np.dtype(
    [
        ("session_type", U8),
        ("time_offset", U8),
        ("weather", U8),
        ("track_temperature", I8),
        ("track_temperature_change", I8),
        ("air_temperature", I8),
        ("air_temperature_change", I8),
        ("rain_percentage", U8),
    ]
)
PACKET_SESSION = np.dtype(
    [
        ("header", HEADER),
        ("weather", U8),
        ("track_temperature", I8),
        ("air_temperature", I8),
        ("total_laps", U8),
        ("track_length", U16),
        ("session_type", U8),
        ("track_id", I8),
        ("formula", U8),
        ("session_time_left", U16),
        ("session_duration", U16),
        ("pit_speed_limit", U8),
        ("game_paused", U8),
        ("is_spectating", U8),
        ("spectator_car_index", U8),
        ("sli_pro_native_support", U8),
        ("num_marshal_zones", U8),
        ("marshal_zones", MARSHAL_ZONE, (MAX_MARSHAL_ZONES,)),
        ("safety_car_status", U8),
        ("network_game", U8),
        ("num_weather_forecast_samples", U8),
        ("weather_forecast_samples", WEATHER_FORECAST, (MAX_WEATHER_FORECAST_SAMPLES,)),
        ("forecast_accuracy", U8),
        ("ai_difficulty", U8),
        ("season_link_identifier", U32),
        ("weekend_link_identifier", U32),
        ("session_link_identifier", U32),
        ("pit_stop_window_ideal_lap", U8),
        ("pit_stop_window_latest_lap", U8),
        ("pit_stop_rejoin_position", U8),
        ("steering_assist", U8),
        ("braking_assist", U8),
        ("gearbox_assist", U8),
        ("pit_assist", U8),
        ("pit_release_assist", U8),
        ("ers_assist", U8),
        ("drs_assist", U8),
        ("dynamic_racing_line", U8),
        ("dynamic_racing_line_type", U8),
        ("game_mode", U8),
        ("rule_set", U8),
        ("time_of_day", U32),
        ("session_length", U8),
        ("speed_units_lead_player", U8),
        ("temperature_units_lead_player", U8),
        ("speed_units_secondary_player", U8),
        ("temperature_units_secondary_player", U8),
        ("num_safety_car_periods", U8),
        ("num_virtual_safety_car_periods", U8),
        ("num_red_flag_periods", U8),
        ("equal_car_performance", U8),
        ("recovery_mode", U8),
        ("flashback_limit", U8),
        ("surface_type", U8),
        ("low_fuel_mode", U8),
        ("race_starts", U8),
        ("tyre_temperature", U8),
        ("pit_lane_tyre_sim", U8),
        ("car_damage", U8),
        ("car_damage_rate", U8),
        ("collisions", U8),
        ("collisions_off_for_first_lap_only", U8),
        ("mp_unsafe_pit_release", U8),
        ("mp_off_for_griefing", U8),
        ("corner_cutting_stringency", U8),
        ("parc_ferme_rules", U8),
        ("pit_stop_experience", U8),
        ("safety_car", U8),
        ("safety_car_experience", U8),
        ("formation_lap", U8),
        ("formation_lap_experience", U8),
        ("red_flags", U8),
        ("affects_licence_level_solo", U8),
        ("affects_licence_level_mp", U8),
        ("num_sessions_in_weekend", U8),
        ("weekend_structure", U8, (MAX_SESSIONS_IN_WEEKEND,)),
        ("sector2_lap_distance_start", F32),
        ("sector3_lap_distance_start", F32),
    ]
)

# --- 2: Lap data ----------------------------------------------------------------------
CAR_LAP_DATA = np.dtype(
    [
        ("last_lap_time_in_ms", U32),
        ("current_lap_time_in_ms", U32),
        ("sector1_time_ms_part", U16),
        ("sector1_time_minutes_part", U8),
        ("sector2_time_ms_part", U16),
        ("sector2_time_minutes_part", U8),
        ("delta_to_car_in_front_ms_part", U16),
        ("delta_to_car_in_front_minutes_part", U8),
        ("delta_to_race_leader_ms_part", U16),
        ("delta_to_race_leader_minutes_part", U8),
        ("lap_distance", F32),
        ("total_distance", F32),
        ("safety_car_delta", F32),
        ("car_position", U8),
        ("current_lap_num", U8),
        ("pit_status", U8),
        ("num_pit_stops", U8),
        ("sector", U8),
        ("current_lap_invalid", U8),
        ("penalties", U8),
        ("total_warnings", U8),
        ("corner_cutting_warnings", U8),
        ("num_unserved_drive_through_pens", U8),
        ("num_unserved_stop_go_pens", U8),
        ("grid_position", U8),
        ("driver_status", U8),
        ("result_status", U8),
        ("pit_lane_timer_active", U8),
        ("pit_lane_time_in_lane_in_ms", U16),
        ("pit_stop_timer_in_ms", U16),
        ("pit_stop_should_serve_pen", U8),
        ("speed_trap_fastest_speed", F32),
        ("speed_trap_fastest_lap", U8),
    ]
)
PACKET_LAP_DATA = np.dtype(
    [
        ("header", HEADER),
        ("car", CAR_LAP_DATA, (MAX_CARS,)),
        ("time_trial_pb_car_idx", U8),
        ("time_trial_rival_car_idx", U8),
    ]
)

# --- 3: Event (4-char code + 12-byte union; decoded in decoder.EVENT_LAYOUTS) -----------
PACKET_EVENT = np.dtype([("header", HEADER), ("event_string_code", "S4"), ("event_details", "V12")])

# --- 4: Participants ------------------------------------------------------------------
LIVERY_COLOUR = np.dtype([("red", U8), ("green", U8), ("blue", U8)])
PARTICIPANT = np.dtype(
    [
        ("ai_controlled", U8),
        ("driver_id", U8),
        ("network_id", U8),
        ("team_id", U8),
        ("my_team", U8),
        ("race_number", U8),
        ("nationality", U8),
        ("name", "S32"),
        ("your_telemetry", U8),
        ("show_online_names", U8),
        ("tech_level", U16),
        ("platform", U8),
        ("num_colours", U8),
        ("livery_colours", LIVERY_COLOUR, (4,)),
    ]
)
PACKET_PARTICIPANTS = np.dtype(
    [("header", HEADER), ("num_active_cars", U8), ("car", PARTICIPANT, (MAX_CARS,))]
)

# --- 5: Car setups --------------------------------------------------------------------
CAR_SETUP = np.dtype(
    [
        ("front_wing", U8),
        ("rear_wing", U8),
        ("on_throttle", U8),
        ("off_throttle", U8),
        ("front_camber", F32),
        ("rear_camber", F32),
        ("front_toe", F32),
        ("rear_toe", F32),
        ("front_suspension", U8),
        ("rear_suspension", U8),
        ("front_anti_roll_bar", U8),
        ("rear_anti_roll_bar", U8),
        ("front_suspension_height", U8),
        ("rear_suspension_height", U8),
        ("brake_pressure", U8),
        ("brake_bias", U8),
        ("engine_braking", U8),
        ("tyre_pressure", *_wheels(F32)),  # spec: RL, RR, FL, FR
        ("ballast", U8),
        ("fuel_load", F32),
    ]
)
PACKET_CAR_SETUPS = np.dtype(
    [("header", HEADER), ("car", CAR_SETUP, (MAX_CARS,)), ("next_front_wing_value", F32)]
)

# --- 6: Car telemetry -----------------------------------------------------------------
CAR_TELEMETRY = np.dtype(
    [
        ("speed", U16),  # km/h
        ("throttle", F32),  # 0..1
        ("steer", F32),  # -1 (full left) .. 1 (full right)
        ("brake", F32),  # 0..1
        ("clutch", U8),  # 0..100
        ("gear", I8),  # 1-8, N=0, R=-1
        ("engine_rpm", U16),
        ("drs", U8),  # 0 off, 1 on
        ("rev_lights_percent", U8),
        ("rev_lights_bit_value", U16),
        ("brakes_temperature", *_wheels(U16)),  # Celsius
        ("tyres_surface_temperature", *_wheels(U8)),  # Celsius
        ("tyres_inner_temperature", *_wheels(U8)),  # Celsius
        ("engine_temperature", U16),
        ("tyres_pressure", *_wheels(F32)),  # PSI
        ("surface_type", *_wheels(U8)),
    ]
)
PACKET_CAR_TELEMETRY = np.dtype(
    [
        ("header", HEADER),
        ("car", CAR_TELEMETRY, (MAX_CARS,)),
        ("mfd_panel_index", U8),
        ("mfd_panel_index_secondary_player", U8),
        ("suggested_gear", I8),
    ]
)

# --- 7: Car status --------------------------------------------------------------------
CAR_STATUS = np.dtype(
    [
        ("traction_control", U8),  # 0 off, 1 medium, 2 full
        ("anti_lock_brakes", U8),  # 0 off, 1 on
        ("fuel_mix", U8),
        ("front_brake_bias", U8),
        ("pit_limiter_status", U8),
        ("fuel_in_tank", F32),
        ("fuel_capacity", F32),
        ("fuel_remaining_laps", F32),
        ("max_rpm", U16),
        ("idle_rpm", U16),
        ("max_gears", U8),
        ("drs_allowed", U8),
        ("drs_activation_distance", U16),
        ("actual_tyre_compound", U8),
        ("visual_tyre_compound", U8),
        ("tyres_age_laps", U8),
        ("vehicle_fia_flags", I8),
        ("engine_power_ice", F32),
        ("engine_power_mguk", F32),
        ("ers_store_energy", F32),
        ("ers_deploy_mode", U8),
        ("ers_harvested_this_lap_mguk", F32),
        ("ers_harvested_this_lap_mguh", F32),
        ("ers_deployed_this_lap", F32),
        ("network_paused", U8),
    ]
)
PACKET_CAR_STATUS = np.dtype([("header", HEADER), ("car", CAR_STATUS, (MAX_CARS,))])

# --- 8: Final classification ----------------------------------------------------------
FINAL_CLASSIFICATION = np.dtype(
    [
        ("position", U8),
        ("num_laps", U8),
        ("grid_position", U8),
        ("points", U8),
        ("num_pit_stops", U8),
        ("result_status", U8),
        ("result_reason", U8),
        ("best_lap_time_in_ms", U32),
        ("total_race_time", F64),
        ("penalties_time", U8),
        ("num_penalties", U8),
        ("num_tyre_stints", U8),
        ("tyre_stints_actual", U8, (MAX_TYRE_STINTS,)),
        ("tyre_stints_visual", U8, (MAX_TYRE_STINTS,)),
        ("tyre_stints_end_laps", U8, (MAX_TYRE_STINTS,)),
    ]
)
PACKET_FINAL_CLASSIFICATION = np.dtype(
    [("header", HEADER), ("num_cars", U8), ("car", FINAL_CLASSIFICATION, (MAX_CARS,))]
)

# --- 9: Lobby info --------------------------------------------------------------------
LOBBY_PLAYER = np.dtype(
    [
        ("ai_controlled", U8),
        ("team_id", U8),
        ("nationality", U8),
        ("platform", U8),
        ("name", "S32"),
        ("car_number", U8),
        ("your_telemetry", U8),
        ("show_online_names", U8),
        ("tech_level", U16),
        ("ready_status", U8),
    ]
)
PACKET_LOBBY_INFO = np.dtype(
    [("header", HEADER), ("num_players", U8), ("car", LOBBY_PLAYER, (MAX_CARS,))]
)

# --- 10: Car damage -------------------------------------------------------------------
CAR_DAMAGE = np.dtype(
    [
        ("tyres_wear", *_wheels(F32)),  # percent
        ("tyres_damage", *_wheels(U8)),
        ("brakes_damage", *_wheels(U8)),
        ("tyre_blisters", *_wheels(U8)),
        ("front_left_wing_damage", U8),
        ("front_right_wing_damage", U8),
        ("rear_wing_damage", U8),
        ("floor_damage", U8),
        ("diffuser_damage", U8),
        ("sidepod_damage", U8),
        ("drs_fault", U8),
        ("ers_fault", U8),
        ("gear_box_damage", U8),
        ("engine_damage", U8),
        ("engine_mguh_wear", U8),
        ("engine_es_wear", U8),
        ("engine_ce_wear", U8),
        ("engine_ice_wear", U8),
        ("engine_mguk_wear", U8),
        ("engine_tc_wear", U8),
        ("engine_blown", U8),
        ("engine_seized", U8),
    ]
)
PACKET_CAR_DAMAGE = np.dtype([("header", HEADER), ("car", CAR_DAMAGE, (MAX_CARS,))])

# --- 11: Session history (one car per packet, cycling through cars) --------------------
LAP_HISTORY = np.dtype(
    [
        ("lap_time_in_ms", U32),
        ("sector1_time_ms_part", U16),
        ("sector1_time_minutes_part", U8),
        ("sector2_time_ms_part", U16),
        ("sector2_time_minutes_part", U8),
        ("sector3_time_ms_part", U16),
        ("sector3_time_minutes_part", U8),
        ("lap_valid_bit_flags", U8),  # 0x01 lap, 0x02 S1, 0x04 S2, 0x08 S3 valid
    ]
)
TYRE_STINT_HISTORY = np.dtype(
    [("end_lap", U8), ("tyre_actual_compound", U8), ("tyre_visual_compound", U8)]
)
PACKET_SESSION_HISTORY = np.dtype(
    [
        ("header", HEADER),
        ("car_idx", U8),
        ("num_laps", U8),
        ("num_tyre_stints", U8),
        ("best_lap_time_lap_num", U8),
        ("best_sector1_lap_num", U8),
        ("best_sector2_lap_num", U8),
        ("best_sector3_lap_num", U8),
        ("lap_history_data", LAP_HISTORY, (MAX_LAPS_IN_HISTORY,)),
        ("tyre_stints_history_data", TYRE_STINT_HISTORY, (MAX_TYRE_STINTS,)),
    ]
)

# --- 12: Tyre sets --------------------------------------------------------------------
TYRE_SET = np.dtype(
    [
        ("actual_tyre_compound", U8),
        ("visual_tyre_compound", U8),
        ("wear", U8),
        ("available", U8),
        ("recommended_session", U8),
        ("life_span", U8),
        ("usable_life", U8),
        ("lap_delta_time", I16),
        ("fitted", U8),
    ]
)
PACKET_TYRE_SETS = np.dtype(
    [
        ("header", HEADER),
        ("car_idx", U8),
        ("tyre_set_data", TYRE_SET, (MAX_TYRE_SETS,)),
        ("fitted_idx", U8),
    ]
)

# --- 13: Motion ex (player car only; nested as "player", same bytes as the spec) -----
MOTION_EX = np.dtype(
    [
        ("suspension_position", *_wheels(F32)),
        ("suspension_velocity", *_wheels(F32)),
        ("suspension_acceleration", *_wheels(F32)),
        ("wheel_speed", *_wheels(F32)),
        ("wheel_slip_ratio", *_wheels(F32)),
        ("wheel_slip_angle", *_wheels(F32)),
        ("wheel_lat_force", *_wheels(F32)),
        ("wheel_long_force", *_wheels(F32)),
        ("height_of_cog_above_ground", F32),
        ("local_velocity_x", F32),
        ("local_velocity_y", F32),
        ("local_velocity_z", F32),
        ("angular_velocity_x", F32),
        ("angular_velocity_y", F32),
        ("angular_velocity_z", F32),
        ("angular_acceleration_x", F32),
        ("angular_acceleration_y", F32),
        ("angular_acceleration_z", F32),
        ("front_wheels_angle", F32),  # radians
        ("wheel_vert_force", *_wheels(F32)),
        ("front_aero_height", F32),
        ("rear_aero_height", F32),
        ("front_roll_angle", F32),
        ("rear_roll_angle", F32),
        ("chassis_yaw", F32),
        ("chassis_pitch", F32),
        ("wheel_camber", *_wheels(F32)),
        ("wheel_camber_gain", *_wheels(F32)),
    ]
)
PACKET_MOTION_EX = np.dtype([("header", HEADER), ("player", MOTION_EX)])

# --- 14: Time trial -------------------------------------------------------------------
TIME_TRIAL_SET = np.dtype(
    [
        ("car_idx", U8),
        ("team_id", U8),
        ("lap_time_in_ms", U32),
        ("sector1_time_in_ms", U32),
        ("sector2_time_in_ms", U32),
        ("sector3_time_in_ms", U32),
        ("traction_control", U8),
        ("gearbox_assist", U8),
        ("anti_lock_brakes", U8),
        ("equal_car_performance", U8),
        ("custom_setup", U8),
        ("valid", U8),
    ]
)
PACKET_TIME_TRIAL = np.dtype(
    [
        ("header", HEADER),
        ("player_session_best", TIME_TRIAL_SET),
        ("personal_best", TIME_TRIAL_SET),
        ("rival", TIME_TRIAL_SET),
    ]
)

# --- 15: Lap positions ----------------------------------------------------------------
PACKET_LAP_POSITIONS = np.dtype(
    [
        ("header", HEADER),
        ("num_laps", U8),
        ("lap_start", U8),
        ("position_for_vehicle_idx", U8, (MAX_LAP_POSITIONS_LAPS, MAX_CARS)),
    ]
)

PACKET_DTYPES: dict[int, np.dtype] = {
    PacketId.MOTION: PACKET_MOTION,
    PacketId.SESSION: PACKET_SESSION,
    PacketId.LAP_DATA: PACKET_LAP_DATA,
    PacketId.EVENT: PACKET_EVENT,
    PacketId.PARTICIPANTS: PACKET_PARTICIPANTS,
    PacketId.CAR_SETUPS: PACKET_CAR_SETUPS,
    PacketId.CAR_TELEMETRY: PACKET_CAR_TELEMETRY,
    PacketId.CAR_STATUS: PACKET_CAR_STATUS,
    PacketId.FINAL_CLASSIFICATION: PACKET_FINAL_CLASSIFICATION,
    PacketId.LOBBY_INFO: PACKET_LOBBY_INFO,
    PacketId.CAR_DAMAGE: PACKET_CAR_DAMAGE,
    PacketId.SESSION_HISTORY: PACKET_SESSION_HISTORY,
    PacketId.TYRE_SETS: PACKET_TYRE_SETS,
    PacketId.MOTION_EX: PACKET_MOTION_EX,
    PacketId.TIME_TRIAL: PACKET_TIME_TRIAL,
    PacketId.LAP_POSITIONS: PACKET_LAP_POSITIONS,
}

# Packet sizes in bytes for packetFormat 2025 (spec; both cross-check sources agree).
PACKET_SIZES: dict[int, int] = {
    PacketId.MOTION: 1349,
    PacketId.SESSION: 753,
    PacketId.LAP_DATA: 1285,
    PacketId.EVENT: 45,
    PacketId.PARTICIPANTS: 1284,
    PacketId.CAR_SETUPS: 1133,
    PacketId.CAR_TELEMETRY: 1352,
    PacketId.CAR_STATUS: 1239,
    PacketId.FINAL_CLASSIFICATION: 1042,
    PacketId.LOBBY_INFO: 954,
    PacketId.CAR_DAMAGE: 1041,
    PacketId.SESSION_HISTORY: 1460,
    PacketId.TYRE_SETS: 231,
    PacketId.MOTION_EX: 273,
    PacketId.TIME_TRIAL: 101,
    PacketId.LAP_POSITIONS: 1131,
}

# Packets that carry one record per car in a field named "car".
PER_CAR_PACKETS: frozenset[int] = frozenset(
    pid for pid, dt in PACKET_DTYPES.items() if dt.names is not None and "car" in dt.names
)


def _check_sizes() -> None:
    assert HEADER.itemsize == 29, HEADER.itemsize
    for pid, dt in PACKET_DTYPES.items():
        if dt.itemsize != PACKET_SIZES[pid]:
            raise AssertionError(
                f"{PacketId(pid).name}: dtype is {dt.itemsize} bytes, spec says {PACKET_SIZES[pid]}"
            )


_check_sizes()
