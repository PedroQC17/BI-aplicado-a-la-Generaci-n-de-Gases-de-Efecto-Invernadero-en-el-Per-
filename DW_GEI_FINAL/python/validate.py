# -*- coding: utf-8 -*-
"""Script de validacion para fact_emisiones + dimensiones.

Ejecuta queries de integridad, totales y consistencia.
"""

import sys
from config import get_connection

HR = "-" * 70

QUERIES = {
    "total_filas": """
        SELECT COUNT(*) AS total FROM public.fact_emisiones;
    """,

    "por_fuente": """
        SELECT df.fuente_datos_codigo, COUNT(*) AS filas
        FROM public.fact_emisiones fe
        JOIN public.dim_fuente_datos df ON fe.fuente_datos_id = df.fuente_datos_sk
        GROUP BY df.fuente_datos_codigo
        ORDER BY filas DESC;
    """,

    "null_sk": """
        SELECT
            COUNT(*) FILTER (WHERE fuente_datos_id IS NULL)   AS null_fuente,
            COUNT(*) FILTER (WHERE tiempo_id IS NULL)         AS null_tiempo,
            COUNT(*) FILTER (WHERE ubigeo_id IS NULL)         AS null_ubigeo,
            COUNT(*) FILTER (WHERE gas_id IS NULL)            AS null_gas,
            COUNT(*) FILTER (WHERE sector_id IS NULL)         AS null_sector,
            COUNT(*) FILTER (WHERE tipo_emision_id IS NULL)    AS null_tipo_em,
            COUNT(*) FILTER (WHERE unidad_emision_id IS NULL)  AS null_unidad,
            COUNT(*) FILTER (WHERE cantidad_emisiones IS NULL)        AS null_cant_em,
            COUNT(*) FILTER (WHERE cantidad_emisiones_co2eq IS NULL)  AS null_co2eq
        FROM public.fact_emisiones;
    """,

    "totales_por_fuente_tco2e": """
        SELECT df.fuente_datos_codigo,
               ROUND(SUM(fe.cantidad_emisiones_co2eq)::numeric, 2) AS total_tco2e
        FROM public.fact_emisiones fe
        JOIN public.dim_fuente_datos df ON fe.fuente_datos_id = df.fuente_datos_sk
        GROUP BY df.fuente_datos_codigo
        ORDER BY total_tco2e DESC;
    """,

    "totales_por_gas_y_fuente_tco2e": """
        SELECT dg.gas_codigo,
               df.fuente_datos_codigo,
               ROUND(SUM(fe.cantidad_emisiones_co2eq)::numeric, 2) AS total_tco2e
        FROM public.fact_emisiones fe
        JOIN public.dim_gas dg ON fe.gas_id = dg.gas_sk
        JOIN public.dim_fuente_datos df ON fe.fuente_datos_id = df.fuente_datos_sk
        GROUP BY dg.gas_codigo, df.fuente_datos_codigo
        ORDER BY dg.gas_codigo, total_tco2e DESC;
    """,

    "totales_por_anio": """
        SELECT dt.anio,
               ROUND(SUM(fe.cantidad_emisiones_co2eq)::numeric, 2) AS total_tco2e
        FROM public.fact_emisiones fe
        JOIN public.dim_tiempo dt ON fe.tiempo_id = dt.id_tiempo
        GROUP BY dt.anio
        ORDER BY dt.anio;
    """,

    "top10_sectores": """
        SELECT ds.sector,
               ds.subsector,
               ROUND(SUM(fe.cantidad_emisiones_co2eq)::numeric, 2) AS total_tco2e
        FROM public.fact_emisiones fe
        JOIN public.dim_sector ds ON fe.sector_id = ds.sector_sk
        WHERE ds.subsector IS NOT NULL AND ds.subsector <> ''
        GROUP BY ds.sector, ds.subsector
        ORDER BY total_tco2e DESC
        LIMIT 10;
    """,

    "top10_mayores_emisores": """
        SELECT df.fuente_datos_codigo,
               ds.sector,
               dg.gas_codigo,
               ROUND(SUM(fe.cantidad_emisiones_co2eq)::numeric, 2) AS total_tco2e
        FROM public.fact_emisiones fe
        JOIN public.dim_fuente_datos df ON fe.fuente_datos_id = df.fuente_datos_sk
        JOIN public.dim_sector ds ON fe.sector_id = ds.sector_sk
        JOIN public.dim_gas dg ON fe.gas_id = dg.gas_sk
        GROUP BY df.fuente_datos_codigo, ds.sector, dg.gas_codigo
        ORDER BY total_tco2e DESC
        LIMIT 10;
    """,

    "staging_vs_fact_ct": """
        SELECT 'staging' AS origen, COUNT(*) AS filas
        FROM staging.stg_climate_trace
        UNION ALL
        SELECT 'fact', COUNT(*)
        FROM public.fact_emisiones fe
        JOIN public.dim_fuente_datos df ON fe.fuente_datos_id = df.fuente_datos_sk
        WHERE df.fuente_datos_codigo = 'CT';
    """,

    "staging_vs_fact_edgar": """
        SELECT 'staging (PER/GHG)' AS origen, COUNT(*) AS filas
        FROM staging.stg_edgar
        WHERE codigo_pais = 'PER' AND gas IN ('CO2','CH4','N2O')
        UNION ALL
        SELECT 'fact', COUNT(*)
        FROM public.fact_emisiones fe
        JOIN public.dim_fuente_datos df ON fe.fuente_datos_id = df.fuente_datos_sk
        WHERE df.fuente_datos_codigo = 'EDGAR';
    """,

    "staging_vs_fact_cw": """
        SELECT 'staging' AS origen, COUNT(*) AS filas
        FROM staging.stg_climate_watch
        UNION ALL
        SELECT 'fact', COUNT(*)
        FROM public.fact_emisiones fe
        JOIN public.dim_fuente_datos df ON fe.fuente_datos_id = df.fuente_datos_sk
        WHERE df.fuente_datos_codigo = 'CW';
    """,

    "staging_vs_fact_faostat": """
        SELECT 'staging' AS origen, COUNT(*) AS filas
        FROM staging.stg_faostat
        UNION ALL
        SELECT 'fact', COUNT(*)
        FROM public.fact_emisiones fe
        JOIN public.dim_fuente_datos df ON fe.fuente_datos_id = df.fuente_datos_sk
        WHERE df.fuente_datos_codigo = 'FAOSTAT';
    """,

    "dim_tipo_emision": """
        SELECT tipo_emision_sk, tipo_emision_codigo, tipo_emision_nombre
        FROM public.dim_tipo_emision
        ORDER BY tipo_emision_sk;
    """,

    "dim_sector_por_origen": """
        SELECT clasificacion_origen, COUNT(*) AS sectores
        FROM public.dim_sector
        GROUP BY clasificacion_origen
        ORDER BY clasificacion_origen;
    """,

    "dim_ubigeo_niveles": """
        SELECT nivel_geografico, COUNT(*) AS total
        FROM public.dim_ubigeo
        GROUP BY nivel_geografico
        ORDER BY nivel_geografico;
    """,

    "unidades_usadas_en_fact": """
        SELECT du.unidad_codigo, du.unidad_nombre, COUNT(*) AS uso
        FROM public.fact_emisiones fe
        JOIN public.dim_unidad du ON fe.unidad_emision_id = du.unidad_sk
        GROUP BY du.unidad_codigo, du.unidad_nombre
        ORDER BY uso DESC;
    """,

    "gas_usados_en_fact": """
        SELECT dg.gas_codigo, dg.gas_nombre, COUNT(*) AS uso
        FROM public.fact_emisiones fe
        JOIN public.dim_gas dg ON fe.gas_id = dg.gas_sk
        GROUP BY dg.gas_codigo, dg.gas_nombre
        ORDER BY uso DESC;
    """,

    "tipo_emision_usados_en_fact": """
        SELECT dte.tipo_emision_codigo, dte.tipo_emision_nombre, COUNT(*) AS uso
        FROM public.fact_emisiones fe
        JOIN public.dim_tipo_emision dte ON fe.tipo_emision_id = dte.tipo_emision_sk
        GROUP BY dte.tipo_emision_codigo, dte.tipo_emision_nombre
        ORDER BY uso DESC;
    """,
}


def run_queries(cursor):
    for title, query in QUERIES.items():
        print(f"\n{HR}")
        print(f"  {title}")
        print(f"{HR}")
        try:
            cursor.execute(query)
            cols = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()

            if not rows:
                print("  (sin resultados)")
                continue

            widths = [len(c) for c in cols]
            for row in rows:
                for i, val in enumerate(row):
                    widths[i] = max(widths[i], len(str(val)))

            header = " | ".join(c.ljust(widths[i]) for i, c in enumerate(cols))
            sep = "-+-".join("-" * w for w in widths)
            print(f"  {header}")
            print(f"  {sep}")

            for row in rows:
                line = " | ".join(str(v).ljust(widths[i]) for i, v in enumerate(row))
                print(f"  {line}")

            print(f"  ({len(rows)} filas)")
        except Exception as e:
            print(f"  [!] Error: {e}")


def main():
    print("=" * 60)
    print("  VALIDACION fact_emisiones + dimensiones")
    print("=" * 60)

    cn = get_connection()
    try:
        with cn.cursor() as cur:
            run_queries(cur)
        print(f"\n{HR}")
        print(">> [OK] Validacion completada.")
    except Exception as e:
        print(f"\n[!] Error: {e}")
        sys.exit(1)
    finally:
        cn.close()


if __name__ == "__main__":
    main()
