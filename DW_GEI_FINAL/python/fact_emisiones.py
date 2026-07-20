# -*- coding: utf-8 -*-
"""Construye fact_emisiones desde las 4 staging tables con las 7 dimensiones."""

import sys

from config import get_connection, GWP100, MESES_TEXTO


FACT_COLUMNAS = (
    "fuente_datos_id", "tiempo_id", "ubigeo_id", "gas_id", "sector_id",
    "tipo_emision_id", "unidad_emision_id",
    "cantidad_emisiones", "cantidad_emisiones_co2eq",
)

FACT_CONFLICT_KEYS = (
    "fuente_datos_id", "tiempo_id", "ubigeo_id",
    "gas_id", "sector_id", "tipo_emision_id",
)


def create_fact_table(cursor):
    print(">> Creando/vaciando public.fact_emisiones...")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS public.fact_emisiones (
            fact_id                  BIGSERIAL PRIMARY KEY,
            tiempo_id                INTEGER NOT NULL,
            ubigeo_id                INTEGER NOT NULL,
            gas_id                   INTEGER NOT NULL,
            sector_id                INTEGER NOT NULL,
            fuente_datos_id           INTEGER NOT NULL,
            tipo_emision_id           INTEGER NOT NULL,
            unidad_emision_id         INTEGER NOT NULL,
            cantidad_emisiones        NUMERIC(18,6),
            cantidad_emisiones_co2eq  NUMERIC(18,6),
            UNIQUE (fuente_datos_id, tiempo_id, ubigeo_id, gas_id, sector_id, tipo_emision_id)
        );
    """)
    cursor.execute("TRUNCATE public.fact_emisiones;")
    print("  [OK] Tabla fact_emisiones lista y vaciada.")


def ensure_national_ubigeo(cursor):
    """Asegura que exista un ubigeo nacional 'PER'."""
    cursor.execute(
        "SELECT id_ubigeo FROM public.dim_ubigeo WHERE codigo_ubigeo = 'PER';")
    row = cursor.fetchone()
    if row:
        return row[0]
    cursor.execute("""
        INSERT INTO public.dim_ubigeo
            (codigo_ubigeo, departamento, provincia, distrito,
             lat, lon, nivel_geografico, flag_detalle_geografico)
        VALUES ('PER', 'Peru', 'Peru', 'Nacional',
                -9.19, -75.0, 'Nacional', 0)
        RETURNING id_ubigeo;
    """)
    sk = cursor.fetchone()[0]
    print(f"  [+] Ubigeo nacional 'PER' creado (sk={sk}).")
    return sk


def build_dim_cache(cursor, table, sk_col, code_col):
    cursor.execute(f"SELECT {sk_col}, {code_col} FROM public.{table};")
    cache = {}
    for sk, code in cursor.fetchall():
        key = str(code).strip() if code else None
        if key:
            cache[key] = sk
    return cache


def build_dim_tiempo_cache(cursor):
    cursor.execute("SELECT id_tiempo, anio, mes FROM public.dim_tiempo;")
    cache = {}
    for sk, anio, mes in cursor.fetchall():
        cache[(anio, mes if mes else 0)] = sk
    return cache


def build_mapeo_sector_cache(cursor):
    """Devuelve (cache_clave, cache_sec_index) donde:
       cache_clave: (origen, sec_orig, sub_orig) -> (sector_unificado, subsector_unificado)
       cache_sec_index: (origen, sec_orig) -> (sector_unificado, subsector_unificado) primer match"""
    cursor.execute("""
        SELECT clasificacion_origen, sector_original, subsector_original,
               sector_unificado, subsector_unificado
        FROM staging.mapeo_sector;
    """)
    cache = {}
    sec_index = {}
    for co, sec_orig, sub_orig, sec_unif, sub_unif in cursor.fetchall():
        co = (co or "").strip()
        sec_orig = (sec_orig or "").strip().lower()
        sub_orig = (sub_orig or "").strip().lower()
        key = (co, sec_orig, sub_orig)
        val = ((sec_unif or "").strip(), (sub_unif or "").strip())
        cache[key] = val
        if (co, sec_orig) not in sec_index:
            sec_index[(co, sec_orig)] = val
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
    """Devuelve (codigo -> sk, province_index) donde
    province_index = {province_prefix: [(lat, lon, sk), ...]} para busqueda rapida."""
    cursor.execute(
        "SELECT id_ubigeo, codigo_ubigeo, lat, lon FROM public.dim_ubigeo;")
    rows = cursor.fetchall()
    cache = {}
    province_index = {}
    for sk, codigo, lat, lon in rows:
        if codigo:
            codigo = codigo.strip()
            cache[codigo] = sk
            if lat is not None and lon is not None:
                # Extraer prefijo provincial: PER.X.Y
                parts = codigo.split(".")
                if len(parts) >= 3:
                    prefix = ".".join(parts[:3])
                    if prefix not in province_index:
                        province_index[prefix] = []
                    province_index[prefix].append((float(lat), float(lon), sk))
    return cache, province_index


def resolve_ubigeo_ct(geometry_ref, ubigeo_cache, province_index):
    """Resuelve ubigeo para CT: geometry_ref 'gadm_PER.X.Y_Z' -> provincia PER.X.Y,
    busca el distrito mas cercano dentro de esa provincia usando province_index."""
    import math
    geo = (geometry_ref or "").strip()
    if geo.startswith("gadm_"):
        geo = geo[5:]

    # PER.X.Y_Z -> prefix = PER.X.Y
    parts = geo.split("_")
    prov_prefix = parts[0] if parts else ""

    candidates = province_index.get(prov_prefix, [])
    if not candidates:
        return ubigeo_cache.get("PER")

    # CT no nos da lat/lon aqui, asi que devolvemos el primero
    # de la provincia como mejor aproximacion
    return candidates[0][2]


def resolve_ubigeo_ct_with_coords(geometry_ref, lat, lon, ubigeo_cache, province_index):
    """Resuelve ubigeo para CT con coordenadas: geometry_ref da la provincia,
    lat/lon se usan para encontrar el distrito mas cercano dentro de esa provincia."""
    import math
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


def infer_gas_from_tipo_emision(nombre):
    if not nombre:
        return None
    upper = nombre.upper()
    if "N2O" in upper:
        return "N2O"
    if "CH4" in upper:
        return "CH4"
    if "CO2" in upper:
        return "CO2"
    return None


def resolve_sector(co, sec_raw, sub_raw, mapeo_cache, mapeo_sec_index, dim_sector_cache):
    """Traduce sector original -> unificado -> sector_sk."""
    sec_low = (sec_raw or "").strip().lower()
    sub_low = (sub_raw or "").strip().lower()

    unif = mapeo_cache.get((co, sec_low, sub_low))
    if unif is None:
        unif = mapeo_cache.get((co, sec_low, ""))
    if unif is None:
        # fallback: buscar cualquier entrada de este sector en el indice
        unif = mapeo_sec_index.get((co, sec_low))
    if unif is None:
        return None

    sec_unif, sub_unif = unif
    sk = dim_sector_cache.get((co, sec_unif, sub_unif))
    if sk is None:
        sk = dim_sector_cache.get((co, sec_unif, ""))
    return sk


def normalize_gas_edgar(gas_raw):
    """Normaliza gas EDGAR: CO2bio -> CO2."""
    g = (gas_raw or "").strip()
    if g == "CO2bio":
        return "CO2"
    return g


def batch_insert(cn, batch):
    if not batch:
        return 0
    from psycopg2.extras import execute_values
    cols = ", ".join(FACT_COLUMNAS)
    conflicto = ", ".join(FACT_CONFLICT_KEYS)
    template = f"""
        INSERT INTO public.fact_emisiones ({cols})
        VALUES %s
        ON CONFLICT ({conflicto}) DO NOTHING;
    """
    with cn.cursor() as cur:
        execute_values(cur, template, batch)
    return len(batch)


# --- procesamiento por fuente ---

def process_climate_trace(cur, caches, cn):
    print("\n--- Climate Trace ---")
    cur.execute("SELECT COUNT(*) FROM staging.stg_climate_trace;")
    total = cur.fetchone()[0]
    print(f"  Filas en staging: {total}")

    cur.execute("""
        SELECT gas, sector, subsector, geometry_ref, lat, lon,
               emissions_quantity, start_time, temporal_granularity
        FROM staging.stg_climate_trace;
    """)

    fuente_sk = caches["fuente_datos"]["CT"]
    tipo_em_sk = caches["tipo_emision"]["EMISIONES"]
    gas_cache = caches["gas"]
    tiempo_cache = caches["tiempo"]
    ubigeo_code = caches["ubigeo_code"]
    province_index = caches["province_index"]
    unidad_cache = caches["unidad"]
    mapeo_cache = caches["mapeo_sector"]
    mapeo_sec_index = caches["mapeo_sec_index"]
    dim_sector_cache = caches["dim_sector"]

    co = "Climate Trace"

    batch = []
    inserted = 0
    stats = {"tiempo": 0, "ubigeo": 0, "gas": 0, "sector": 0, "unidad": 0, "sin_valor": 0}
    BATCH_SIZE = 5000

    for row in cur:
        gas, sector, subsector, geometry_ref, c_lat, c_lon, em_qty, start_time, temp_gran = row

        try:
            valor = float(em_qty) if em_qty else None
        except (ValueError, TypeError):
            valor = None
        if valor is None:
            stats["sin_valor"] += 1
            continue

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

        # ubigeo: usar lat/lon con filtro por provincia
        id_ubigeo = resolve_ubigeo_ct_with_coords(
            geometry_ref, float(c_lat) if c_lat else None,
            float(c_lon) if c_lon else None,
            ubigeo_code, province_index)
        if id_ubigeo is None:
            stats["ubigeo"] += 1
            continue

        gas_sk = gas_cache.get((gas or "").strip().upper())
        if gas_sk is None:
            stats["gas"] += 1
            continue

        sector_sk = resolve_sector(co, sector, subsector, mapeo_cache, mapeo_sec_index, dim_sector_cache)
        if sector_sk is None:
            stats["sector"] += 1
            continue

        unidad_sk = unidad_cache.get("T")
        if unidad_sk is None:
            stats["unidad"] += 1
            continue

        gwp = GWP100.get((gas or "").strip().upper(), 1)
        batch.append((
            fuente_sk, id_tiempo, id_ubigeo, gas_sk, sector_sk,
            tipo_em_sk, unidad_sk,
            valor, valor * gwp,
        ))
        if len(batch) >= BATCH_SIZE:
            inserted += batch_insert(cn, batch)
            batch = []

    inserted += batch_insert(cn, batch)
    print(f"  Insertados: {inserted}")
    print(f"  Faltantes por dim: {stats}")
    return inserted


def process_edgar(cur, caches, cn):
    print("\n--- EDGAR ---")
    cur.execute("SELECT COUNT(*) FROM staging.stg_edgar WHERE codigo_pais='PER';")
    total = cur.fetchone()[0]
    print(f"  Filas en staging (PER): {total}")

    cur.execute("""
        SELECT gas, mes, anio, emision_gg,
               codigo_fuente_emisora, nombre_fuente_emisora
        FROM staging.stg_edgar
        WHERE codigo_pais = 'PER'
          AND gas IN ('CO2', 'CH4', 'N2O', 'CO2bio');
    """)

    fuente_sk = caches["fuente_datos"]["EDGAR"]
    tipo_em_sk = caches["tipo_emision"]["EMISIONES"]
    gas_cache = caches["gas"]
    tiempo_cache = caches["tiempo"]
    ubigeo_sk = caches["ubigeo_code"].get("PER")
    unidad_cache = caches["unidad"]
    mapeo_cache = caches["mapeo_sector"]
    mapeo_sec_index = caches["mapeo_sec_index"]
    dim_sector_cache = caches["dim_sector"]

    co = "EDGAR"

    batch = []
    inserted = 0
    stats = {"tiempo": 0, "ubigeo": 0, "gas": 0, "sector": 0, "unidad": 0}
    BATCH_SIZE = 5000

    for row in cur:
        gas, mes_text, anio, emision_gg, cod_fte, nom_fte = row

        valor = float(emision_gg) if emision_gg else 0.0
        mes_num = MESES_TEXTO.get((mes_text or "").strip())
        if mes_num is None:
            stats["tiempo"] += 1
            continue

        id_tiempo = tiempo_cache.get((int(anio), mes_num))
        if id_tiempo is None:
            stats["tiempo"] += 1
            continue

        gas_norm = normalize_gas_edgar(gas)
        gas_sk = gas_cache.get(gas_norm)
        if gas_sk is None:
            stats["gas"] += 1
            continue

        sector_sk = resolve_sector(co, cod_fte, nom_fte, mapeo_cache, mapeo_sec_index, dim_sector_cache)
        if sector_sk is None:
            stats["sector"] += 1
            continue

        unidad_sk = unidad_cache.get("Gg")
        if unidad_sk is None:
            stats["unidad"] += 1
            continue

        gwp = GWP100.get(gas_norm, 1)
        factor = 1000.0  # Gg -> t
        batch.append((
            fuente_sk, id_tiempo, ubigeo_sk, gas_sk, sector_sk,
            tipo_em_sk, unidad_sk,
            valor, valor * factor * gwp,
        ))
        if len(batch) >= BATCH_SIZE:
            inserted += batch_insert(cn, batch)
            batch = []

    inserted += batch_insert(cn, batch)
    print(f"  Insertados: {inserted}")
    print(f"  Faltantes por dim: {stats}")
    return inserted


def process_climate_watch(cur, caches, cn):
    print("\n--- Climate Watch ---")
    cur.execute("SELECT COUNT(*) FROM staging.stg_climate_watch;")
    total = cur.fetchone()[0]
    print(f"  Filas en staging: {total}")

    cur.execute("""
        SELECT anio, gas, valor, unidad, fuente_emisora
        FROM staging.stg_climate_watch;
    """)

    fuente_sk = caches["fuente_datos"]["CW"]
    tipo_em_sk = caches["tipo_emision"]["EMISIONES"]
    gas_cache = caches["gas"]
    tiempo_cache = caches["tiempo"]
    ubigeo_sk = caches["ubigeo_code"].get("PER")
    unidad_cache = caches["unidad"]
    mapeo_cache = caches["mapeo_sector"]
    mapeo_sec_index = caches["mapeo_sec_index"]
    dim_sector_cache = caches["dim_sector"]

    co = "Climate Watch"

    batch = []
    inserted = 0
    stats = {"tiempo": 0, "ubigeo": 0, "gas": 0, "sector": 0, "unidad": 0, "sin_valor": 0}
    BATCH_SIZE = 5000

    for row in cur:
        anio, gas, valor, unidad, fuente_emisora = row

        try:
            valor_num = float(valor) if valor else None
        except (ValueError, TypeError):
            valor_num = None
        if valor_num is None:
            stats["sin_valor"] += 1
            continue

        id_tiempo = tiempo_cache.get((int(anio), 0))
        if id_tiempo is None:
            stats["tiempo"] += 1
            continue

        gas_sk = gas_cache.get((gas or "").strip())
        if gas_sk is None:
            stats["gas"] += 1
            continue

        sector_sk = resolve_sector(
            co, fuente_emisora, "", mapeo_cache, mapeo_sec_index, dim_sector_cache)
        if sector_sk is None:
            stats["sector"] += 1
            continue

        un_sk = unidad_cache.get((unidad or "MtCO2e").strip())
        if un_sk is None:
            stats["unidad"] += 1
            continue

        batch.append((
            fuente_sk, id_tiempo, ubigeo_sk, gas_sk, sector_sk,
            tipo_em_sk, un_sk,
            valor_num, valor_num * 1_000_000.0,
        ))
        if len(batch) >= BATCH_SIZE:
            inserted += batch_insert(cn, batch)
            batch = []

    inserted += batch_insert(cn, batch)
    print(f"  Insertados: {inserted}")
    print(f"  Faltantes por dim: {stats}")
    return inserted


def process_faostat(cur, caches, cn):
    print("\n--- FAOSTAT ---")
    cur.execute("SELECT COUNT(*) FROM staging.stg_faostat;")
    total = cur.fetchone()[0]
    print(f"  Filas en staging: {total}")

    cur.execute("""
        SELECT codigo_tipo_emision, nombre_tipo_emision,
               codigo_fuente_emision, nombre_fuente_emision,
               anio, unidad, valor
        FROM staging.stg_faostat;
    """)

    fuente_sk = caches["fuente_datos"]["FAOSTAT"]
    gas_cache = caches["gas"]
    tiempo_cache = caches["tiempo"]
    ubigeo_sk = caches["ubigeo_code"].get("PER")
    tipo_em_cache = caches["tipo_emision"]
    unidad_cache = caches["unidad"]
    mapeo_cache = caches["mapeo_sector"]
    mapeo_sec_index = caches["mapeo_sec_index"]
    dim_sector_cache = caches["dim_sector"]

    co = "FAOSTAT"

    batch = []
    inserted = 0
    stats = {"tiempo": 0, "ubigeo": 0, "gas": 0, "sector": 0,
             "unidad": 0, "tipo_em": 0, "sin_valor": 0}
    BATCH_SIZE = 5000

    for row in cur:
        cod_tipo, nom_tipo, cod_fte, nom_fte, anio, unidad, valor = row

        try:
            valor_num = float(valor) if valor else None
        except (ValueError, TypeError):
            valor_num = None
        if valor_num is None:
            stats["sin_valor"] += 1
            continue

        id_tiempo = tiempo_cache.get((int(anio), 0))
        if id_tiempo is None:
            stats["tiempo"] += 1
            continue

        tipo_cod = (cod_tipo or "").strip()
        tipo_em_sk = tipo_em_cache.get(tipo_cod)
        if tipo_em_sk is None:
            stats["tipo_em"] += 1
            continue

        gas_inferido = infer_gas_from_tipo_emision(nom_tipo)
        gas_sk = gas_cache.get(gas_inferido) if gas_inferido else None
        if gas_sk is None:
            stats["gas"] += 1
            continue

        sector_sk = resolve_sector(co, nom_fte, "", mapeo_cache, mapeo_sec_index, dim_sector_cache)
        if sector_sk is None and cod_fte:
            sector_sk = resolve_sector(co, str(cod_fte), "", mapeo_cache, mapeo_sec_index, dim_sector_cache)
        if sector_sk is None:
            stats["sector"] += 1
            continue

        un_sk = unidad_cache.get((unidad or "kt").strip())
        if un_sk is None:
            stats["unidad"] += 1
            continue

        gwp = GWP100.get(gas_inferido, 1) if gas_inferido else 1
        batch.append((
            fuente_sk, id_tiempo, ubigeo_sk, gas_sk, sector_sk,
            tipo_em_sk, un_sk,
            valor_num, valor_num * 1000.0 * gwp,
        ))
        if len(batch) >= BATCH_SIZE:
            inserted += batch_insert(cn, batch)
            batch = []

    inserted += batch_insert(cn, batch)
    print(f"  Insertados: {inserted}")
    print(f"  Faltantes por dim: {stats}")
    return inserted


def main():
    print("=" * 60)
    print("  fact_emisiones - Construccion desde staging")
    print("=" * 60)

    cn = get_connection()

    try:
        with cn.cursor() as cur:
            create_fact_table(cur)

            # Asegurar ubigeo nacional
            per_sk = ensure_national_ubigeo(cur)
            print(f"  Ubigeo nacional 'PER': sk={per_sk}")

            print("\n>> Construyendo caches...")
            caches = {}
            caches["fuente_datos"] = build_dim_cache(
                cur, "dim_fuente_datos", "fuente_datos_sk", "fuente_datos_codigo")
            caches["gas"] = build_dim_cache(
                cur, "dim_gas", "gas_sk", "gas_codigo")
            caches["tiempo"] = build_dim_tiempo_cache(cur)
            caches["dim_sector"] = build_dim_sector_cache(cur)
            caches["mapeo_sector"], caches["mapeo_sec_index"] = build_mapeo_sector_cache(cur)
            caches["tipo_emision"] = build_dim_cache(
                cur, "dim_tipo_emision", "tipo_emision_sk", "tipo_emision_codigo")
            caches["unidad"] = build_dim_cache(
                cur, "dim_unidad", "unidad_sk", "unidad_codigo")
            ubigeo_code, province_index = build_dim_ubigeo_cache(cur)
            caches["ubigeo_code"] = ubigeo_code
            caches["province_index"] = province_index

            print(f"  fuentes={len(caches['fuente_datos'])} "
                  f"gases={len(caches['gas'])} "
                  f"tiempos={len(caches['tiempo'])} "
                  f"sectores={len(caches['dim_sector'])} "
                  f"mapeo_sector={len(caches['mapeo_sector'])} "
                  f"tipo_em={len(caches['tipo_emision'])} "
                  f"unidades={len(caches['unidad'])} "
                  f"ubigeos={len(ubigeo_code)} provincias={len(province_index)}")

            print("\n>> Procesando fuentes...")

        total = 0
        with cn.cursor() as cur:
            total += process_climate_trace(cur, caches, cn)
        with cn.cursor() as cur:
            total += process_edgar(cur, caches, cn)
        with cn.cursor() as cur:
            total += process_climate_watch(cur, caches, cn)
        with cn.cursor() as cur:
            total += process_faostat(cur, caches, cn)

        print(f"\n>> Total insertadas en fact_emisiones: {total}")
        print(">> [OK] fact_emisiones completado.")

    except Exception as e:
        print(f"\n[!] Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        cn.close()


if __name__ == "__main__":
    main()
