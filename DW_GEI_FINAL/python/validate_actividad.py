# -*- coding: utf-8 -*-
"""Validacion fact_actividad_emisora."""

import sys
from config import get_connection

HR = "-" * 70

QUERIES = {
    "total_filas": "SELECT COUNT(*) AS total FROM public.fact_actividad_emisora;",

    "null_sk": """
        SELECT
            COUNT(*) FILTER (WHERE fuente_datos_id IS NULL) AS null_fuente,
            COUNT(*) FILTER (WHERE tiempo_id IS NULL)       AS null_tiempo,
            COUNT(*) FILTER (WHERE ubigeo_id IS NULL)       AS null_ubigeo,
            COUNT(*) FILTER (WHERE gas_id IS NULL)          AS null_gas,
            COUNT(*) FILTER (WHERE sector_id IS NULL)       AS null_sector,
            COUNT(*) FILTER (WHERE unidad_emision_id IS NULL) AS null_unidad
        FROM public.fact_actividad_emisora;
    """,

    "muestra": """
        SELECT f.* FROM public.fact_actividad_emisora f LIMIT 5;
    """,

    "top_actividad_por_sector": """
        SELECT ds.sector, ds.subsector,
               ROUND(SUM(f.cantidad_actividad_base)::numeric, 2) AS act_total_base
        FROM public.fact_actividad_emisora f
        JOIN public.dim_sector ds ON f.sector_id = ds.sector_sk
        GROUP BY ds.sector, ds.subsector
        ORDER BY act_total_base DESC
        LIMIT 8;
    """,

    "top_capacidad_por_sector": """
        SELECT ds.sector, ds.subsector,
               ROUND(SUM(f.cantidad_capacidad_base)::numeric, 2) AS cap_total_base
        FROM public.fact_actividad_emisora f
        JOIN public.dim_sector ds ON f.sector_id = ds.sector_sk
        GROUP BY ds.sector, ds.subsector
        ORDER BY cap_total_base DESC
        LIMIT 8;
    """,

    "unidades_usadas": """
        SELECT du.unidad_codigo, du.unidad_nombre,
               du.unidad_categoria, du.unidad_base,
               COUNT(*) AS uso
        FROM public.fact_actividad_emisora f
        JOIN public.dim_unidad du ON f.unidad_emision_id = du.unidad_sk
        GROUP BY du.unidad_codigo, du.unidad_nombre, du.unidad_categoria, du.unidad_base
        ORDER BY uso DESC;
    """,

    "actividad_por_anio": """
        SELECT dt.anio,
               ROUND(SUM(f.cantidad_actividad_base)::numeric, 2) AS act_total
        FROM public.fact_actividad_emisora f
        JOIN public.dim_tiempo dt ON f.tiempo_id = dt.id_tiempo
        GROUP BY dt.anio ORDER BY dt.anio;
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
    print("  VALIDACION fact_actividad_emisora")
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
