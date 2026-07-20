# -*- coding: utf-8 -*-
"""Construye fact_actividad_emisora desde staging.stg_climate_trace.

Dimensiones (6):
  dim_fuente_datos, dim_tiempo, dim_ubigeo, dim_gas, dim_sector, dim_unidad

Medidas:
  cantidad_actividad        - valor en unidad original
  cantidad_actividad_base   - normalizado a unidad base (via dim_unidad.factor_conversion)
  cantidad_capacidad        - valor en unidad original
  cantidad_capacidad_base   - normalizado a unidad base
  factor_emision_valor      - valor original (ratio, no se normaliza)

Solo fuente Climate Trace.
"""

import sys

from config import get_connection, GWP100, MESES_TEXTO


FACT_COLUMNAS = (
    "fuente_datos_id", "tiempo_id", "ubigeo_id", "gas_id", "sector_id",
    "unidad_emision_id",
    "cantidad_actividad", "cantidad_actividad_base",
    "cantidad_capacidad", "cantidad_capacidad_base",
    "factor_emision_valor",
)

FACT_CONFLICT_KEYS = (
    "fuente_datos_id", "tiempo_id", "ubigeo_id",
    "gas_id", "sector_id", "unidad_emision_id",
)


def create_fact_table(cursor):
    print(">> Recreando public.fact_actividad_emisora...")
    cursor.execute("DROP TABLE IF EXISTS public.fact_actividad_emisora;")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS public.fact_actividad_emisora (
            fact_id                  BIGSERIAL PRIMARY KEY,
            fuente_datos_id           INTEGER NOT NULL REFERENCES public.dim_fuente_datos(fuente_datos_sk),
            tiempo_id                INTEGER NOT NULL REFERENCES public.dim_tiempo(id_tiempo),
            ubigeo_id                INTEGER NOT NULL REFERENCES public.dim_ubigeo(id_ubigeo),
            gas_id                   INTEGER NOT NULL REFERENCES public.dim_gas(gas_sk),
            sector_id                INTEGER NOT NULL REFERENCES public.dim_sector(sector_sk),
            unidad_emision_id         INTEGER NOT NULL REFERENCES public.dim_unidad(unidad_sk),
            cantidad_actividad        NUMERIC(18,6),
            cantidad_actividad_base   NUMERIC(18,6),
            cantidad_capacidad        NUMERIC(18,6),
            cantidad_capacidad_base   NUMERIC(18,6),
            factor_emision_valor      NUMERIC(18,6),
            UNIQUE (fuente_datos_id, tiempo_id, ubigeo_id, gas_id, sector_id, unidad_emision_id)
        );
    """)
    print("  [OK] Tabla lista y vaciada.")


def build_dim_cache(cursor, table, sk_col, code_col):
    cursor.execute(f"SELECT {sk_col}, {code_col} FROM public.{table};")
    cache = {}
    for sk, code in cursor.fetchall():
        key = str(code).strip() if code else None
        if key:
            cache[key] = sk
    return cache


def build_dim_unidad_cache(cursor):
    """Cache: codigo -> (sk, factor_conversion, unidad_base)."""
    cursor.execute("""
        SELECT unidad_sk, unidad_codigo, factor_conversion, unidad_base
        FROM public.dim_unidad;
    """)
    cache = {}
    for sk, codigo, fac, base in cursor.fetchall():
        key = str(codigo).strip() if codigo else None
        if key:
            cache[key] = (sk, float(fac) if fac else 1.0, base or key)
    return cache


def build_dim_tiempo_cache(cursor):
    cursor.execute("SELECT id_tiempo, anio, mes FROM public.dim_tiempo;")
    cache = {}
    for sk, anio, mes in cursor.fetchall():
        cache[(anio, mes if mes else 0)] = sk
    return cache


def build_mapeo_sector_cache(cursor):
    cursor.execute("""
        SELECT clasificacion_origen, sector_original, subsector_original,
               sector_unificado, subsector_unificado
        FROM staging.mapeo_sector;
    """)
    cache = {}
    sec_index = {}
    for co, sec_orig, sub_orig, sec_unif, sub_unif in cursor.fetchall():
        co = (co or "").strip()
        sec_orig_l = (sec_orig or "").strip().lower()
        sub_orig_l = (sub_orig or "").strip().lower()
        val = ((sec_unif or "").strip(), (sub_unif or "").strip())
        cache[(co, sec_orig_l, sub_orig_l)] = val
        if (co, sec_orig_l) not in sec_index:
            sec_index[(co, sec_orig_l)] = val
    return cache, sec_index


def build_dim_sector_cache(cursor):
    cursor.execute("""
        SELECT sector_sk, clasificacion_origen, sector, subsector
        FROM public.dim_sector;
    """)
    cache = {}
    for sk, co, sec, sub in cursor.fetchall():
        co = co.strip() if co else ""
        sec = sec.strip() if sec else ""
        sub = sub.strip() if sub else ""
        cache[(co, sec, sub)] = sk
    return cache


def build_dim_ubigeo_cache(cursor):
    """Devuelve (codigo -> sk, province_index) para busqueda rapida."""
    cursor.execute("SELECT id_ubigeo, codigo_ubigeo, lat, lon FROM public.dim_ubigeo;")
    rows = cursor.fetchall()
    cache = {}
    province_index = {}
    for sk, codigo, lat, lon in rows:
        codigo = (codigo or "").strip()
        if codigo:
            cache[codigo] = sk
            if lat is not None and lon is not None:
                parts = codigo.split(".")
                if len(parts) >= 3:
                    prefix = ".".join(parts[:3])
                    province_index.setdefault(prefix, []).append((float(lat), float(lon), sk))
    return cache, province_index


def resolve_ubigeo_ct(geometry_ref, lat, lon, ubigeo_cache, province_index):
    geo = (geometry_ref or "").strip()
    if geo.startswith("gadm_"):
        geo = geo[5:]
    parts = geo.split("_")
    prov_prefix = parts[0] if parts else ""
    candidates = province_index.get(prov_prefix, [])
    if not candidates:
        return ubigeo_cache.get("PER")
    if lat is None or lon is None:
        return candidates[0][2]
    best_dist = float("inf")
    best_sk = None
    for clat, clon, sk in candidates:
        d = (lat - clat) ** 2 + (lon - clon) ** 2
        if d < best_dist:
            best_dist = d
            best_sk = sk
    return best_sk if best_sk else candidates[0][2]


def resolve_sector(co, sec_raw, sub_raw, mapeo_cache, mapeo_sec_index, dim_sector_cache):
    sec_low = (sec_raw or "").strip().lower()
    sub_low = (sub_raw or "").strip().lower()
    unif = mapeo_cache.get((co, sec_low, sub_low))
    if unif is None:
        unif = mapeo_cache.get((co, sec_low, ""))
    if unif is None:
        unif = mapeo_sec_index.get((co, sec_low))
    if unif is None:
        return None
    sec_unif, sub_unif = unif
    sk = dim_sector_cache.get((co, sec_unif, sub_unif))
    if sk is None:
        sk = dim_sector_cache.get((co, sec_unif, ""))
    return sk


def batch_insert(cn, batch):
    if not batch:
        return 0
    from psycopg2.extras import execute_values
    cols = ", ".join(FACT_COLUMNAS)
    conflicto = ", ".join(FACT_CONFLICT_KEYS)
    template = f"""
        INSERT INTO public.fact_actividad_emisora ({cols})
        VALUES %s
        ON CONFLICT ({conflicto}) DO NOTHING;
    """
    with cn.cursor() as cur:
        execute_values(cur, template, batch)
    return len(batch)


def to_float(val):
    try:
        return float(val) if val else 0.0
    except (ValueError, TypeError):
        return 0.0


def main():
    print("=" * 60)
    print("  fact_actividad_emisora - Construccion")
    print("=" * 60)

    cn = get_connection()

    try:
        with cn.cursor() as cur:
            create_fact_table(cur)

            print("\n>> Construyendo caches...")
            fuente_sk = build_dim_cache(cur, "dim_fuente_datos", "fuente_datos_sk", "fuente_datos_codigo")["CT"]
            gas_cache = build_dim_cache(cur, "dim_gas", "gas_sk", "gas_codigo")
            tiempo_cache = build_dim_tiempo_cache(cur)
            dim_sector_cache = build_dim_sector_cache(cur)
            mapeo_cache, mapeo_sec_index = build_mapeo_sector_cache(cur)
            ubigeo_cache, province_index = build_dim_ubigeo_cache(cur)
            unidad_cache = build_dim_unidad_cache(cur)

            print(f"  fuentes OK, gases={len(gas_cache)}, tiempos={len(tiempo_cache)}, "
                  f"sectores={len(dim_sector_cache)}, mapeo={len(mapeo_cache)}, "
                  f"ubigeos={len(ubigeo_cache)}, unidades={len(unidad_cache)}")

            print("\n>> Procesando Climate Trace...")
            cur.execute("SELECT COUNT(*) FROM staging.stg_climate_trace;")
            total = cur.fetchone()[0]
            print(f"  Filas en staging: {total}")

            cur.execute("""
                SELECT gas, sector, subsector, geometry_ref, lat, lon,
                       activity, activity_units,
                       capacity, capacity_units,
                       emissions_factor,
                       start_time, temporal_granularity
                FROM staging.stg_climate_trace;
            """)

            co = "Climate Trace"

            batch = []
            inserted = 0
            stats = {"tiempo": 0, "ubigeo": 0, "gas": 0, "sector": 0,
                     "unidad": 0, "sin_datos": 0}
            BATCH_SIZE = 5000

            for row in cur:
                (gas, sector, subsector, geometry_ref, c_lat, c_lon,
                 activity, activity_units,
                 capacity, capacity_units,
                 emissions_factor,
                 start_time, temp_gran) = row

                act_val = to_float(activity)
                cap_val = to_float(capacity)
                ef_val = to_float(emissions_factor)

                if act_val == 0.0 and cap_val == 0.0 and ef_val == 0.0:
                    stats["sin_datos"] += 1
                    continue

                # tiempo
                anio = None
                mes = None
                if start_time and len(start_time) >= 10:
                    try:
                        anio = int(start_time[:4])
                        if temp_gran and temp_gran.strip().lower() == "month":
                            mes = int(start_time[5:7])
                    except (ValueError, IndexError):
                        pass
                if anio is None:
                    stats["tiempo"] += 1
                    continue

                id_tiempo = tiempo_cache.get((anio, mes if mes else 0))
                if id_tiempo is None:
                    stats["tiempo"] += 1
                    continue

                # ubigeo
                id_ubigeo = resolve_ubigeo_ct(
                    geometry_ref,
                    float(c_lat) if c_lat else None,
                    float(c_lon) if c_lon else None,
                    ubigeo_cache, province_index)
                if id_ubigeo is None:
                    stats["ubigeo"] += 1
                    continue

                # gas
                gas_sk = gas_cache.get((gas or "").strip().upper())
                if gas_sk is None:
                    stats["gas"] += 1
                    continue

                # sector
                sector_sk = resolve_sector(
                    co, sector, subsector, mapeo_cache, mapeo_sec_index, dim_sector_cache)
                if sector_sk is None:
                    stats["sector"] += 1
                    continue

                # unidad (para actividad como medida principal)
                act_unit_key = (activity_units or "").strip()
                # limpiar trailing spaces de CT
                if act_unit_key == "Vehicle*km ":
                    act_unit_key = "Vehicle*km"
                un_info = unidad_cache.get(act_unit_key)
                if un_info is None:
                    stats["unidad"] += 1
                    continue
                unidad_sk, act_factor, act_base = un_info

                # normalizar actividad a unidad base
                act_base_val = act_val * act_factor

                # normalizar capacidad a unidad base
                cap_unit_key = (capacity_units or "").strip()
                cap_info = unidad_cache.get(cap_unit_key)
                cap_base_val = cap_val
                if cap_info:
                    _, cap_factor, _ = cap_info
                    cap_base_val = cap_val * cap_factor

                batch.append((
                    fuente_sk, id_tiempo, id_ubigeo, gas_sk, sector_sk,
                    unidad_sk,
                    act_val, act_base_val,
                    cap_val, cap_base_val,
                    ef_val,
                ))

                if len(batch) >= BATCH_SIZE:
                    inserted += batch_insert(cn, batch)
                    batch = []

            inserted += batch_insert(cn, batch)

            print(f"  Insertados: {inserted}")
            print(f"  Faltantes por dim: {stats}")

        print(f"\n>> Total insertadas en fact_actividad_emisora: {inserted}")
        print(">> [OK] fact_actividad_emisora completado.")

    except Exception as e:
        print(f"\n[!] Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        cn.close()


if __name__ == "__main__":
    main()
