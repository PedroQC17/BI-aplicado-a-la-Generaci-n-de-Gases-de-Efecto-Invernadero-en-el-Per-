# -*- coding: utf-8 -*-
"""Configuracion de conexion a PostgreSQL y constantes del proyecto."""

import psycopg2

DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "dbname": "dw_gei",
    "user": "postgres",
    "password": "root",
}

SCHEMA_STAGING = "staging"
SCHEMA_PUBLIC = "public"

FUENTES = {
    "CT": "Climate Trace",
    "EDGAR": "EDGAR",
    "CW": "Climate Watch",
    "FAOSTAT": "FAOSTAT",
}

UNIDAD_BASE = "t"

GWP100 = {
    "CO2": 1,
    "CH4": 28,
    "N2O": 265,
}

GWP20 = {
    "CO2": 1,
    "CH4": 84,
    "N2O": 264,
}

MESES_TEXTO = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4,
    "May": 5, "Jun": 6, "Jul": 7, "Aug": 8,
    "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

GAS_FROM_NOMBRE_TIPO_EMISION = {
    "N2O": "N2O",
    "CH4": "CH4",
    "CO2": "CO2",
}


def get_connection():
    """Devuelve una conexion con autocommit para evitar transacciones manuales."""
    cn = psycopg2.connect(**DB_CONFIG)
    cn.autocommit = True
    return cn
