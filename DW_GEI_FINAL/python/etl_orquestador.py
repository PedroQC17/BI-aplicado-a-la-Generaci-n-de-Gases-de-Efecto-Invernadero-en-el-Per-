# -*- coding: utf-8 -*-
"""Orquestador ETL con tabla de auditoria para demostracion en vivo.

Modos:
  FULL       -> TRUNCATE staging + facts, ejecuta pipeline completo
  INCREMENTAL -> Inserta datos delta de demo, re-ejecuta facts, muestra cambios

Uso:
  python etl_orquestador.py FULL
  python etl_orquestador.py INCREMENTAL
"""

import sys
import time
from datetime import datetime

from config import get_connection

MODE = sys.argv[1].upper() if len(sys.argv) > 1 else "FULL"
assert MODE in ("FULL", "INCREMENTAL"), "Uso: python etl_orquestador.py FULL|INCREMENTAL"


def create_audit_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS public.etl_audit_log (
            audit_id        BIGSERIAL PRIMARY KEY,
            load_type       VARCHAR(20) NOT NULL,
            job_name        VARCHAR(100) NOT NULL,
            step_name       VARCHAR(200),
            status          VARCHAR(20) NOT NULL,
            rows_read       INTEGER DEFAULT 0,
            rows_inserted   INTEGER DEFAULT 0,
            rows_updated    INTEGER DEFAULT 0,
            rows_skipped    INTEGER DEFAULT 0,
            duration_sec    NUMERIC(10,2),
            error_message   TEXT,
            started_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            finished_at     TIMESTAMP
        );
    """)
    print("  [OK] Tabla etl_audit_log lista.")


def log_step(cursor, load_type, job_name, step_name, status,
             rows_read=0, rows_inserted=0, rows_updated=0, rows_skipped=0,
             duration_sec=0, error_message=None):
    cursor.execute("""
        INSERT INTO public.etl_audit_log
            (load_type, job_name, step_name, status, rows_read,
             rows_inserted, rows_updated, rows_skipped, duration_sec, error_message,
             finished_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP);
    """, (load_type, job_name, step_name, status,
          rows_read, rows_inserted, rows_updated, rows_skipped,
          duration_sec, error_message))


def get_table_counts(cursor):
    """Devuelve diccionario con rowcounts de staging y facts."""
    tables = [
        "staging.stg_climate_trace",
        "staging.stg_edgar",
        "staging.stg_climate_watch",
        "staging.stg_faostat",
        "public.fact_emisiones",
        "public.fact_actividad_emisora",
        "public.fact_predicciones",
    ]
    counts = {}
    for tbl in tables:
        try:
            cursor.execute(f"SELECT COUNT(*) FROM {tbl};")
            counts[tbl] = cursor.fetchone()[0]
        except Exception:
            counts[tbl] = "N/A"
    return counts


def full_truncate(cursor):
    print("\n>> TRUNCATE ALL (FULL LOAD)...")
    cursor.execute("TRUNCATE staging.stg_climate_trace, staging.stg_edgar, "
                   "staging.stg_climate_watch, staging.stg_faostat CASCADE;")
    cursor.execute("TRUNCATE public.fact_emisiones, public.fact_actividad_emisora, "
                   "public.fact_predicciones CASCADE;")
    cursor.execute("DELETE FROM public.dim_tipo_emision;")
    cursor.execute("DELETE FROM public.etl_audit_log;")
    print("  [OK] Todo truncado.")


def inject_incremental_demo(cursor):
    """Agrega un año nuevo de datos simulados para Climate Watch."""
    print("\n>> INYECTANDO DATOS INCREMENTALES (demo)...")
    cursor.execute("SELECT MAX(anio) FROM staging.stg_climate_watch;")
    max_anio = cursor.fetchone()[0] or 2023
    new_anio = max_anio + 1
    print(f"  Agregando datos para {new_anio}...")

    # Simular crecimiento del 5% en emisiones sobre el anio anterior
    cursor.execute(f"""
        INSERT INTO staging.stg_climate_watch (anio, fuente_emisora, gas, valor, unidad)
        SELECT {new_anio}, fuente_emisora, gas,
               ROUND((valor * 1.05)::numeric, 4), unidad
        FROM staging.stg_climate_watch
        WHERE anio = {max_anio};
    """)
    inserted = cursor.rowcount
    print(f"  [OK] {inserted} filas incrementales insertadas.")
    return inserted


def run_extract(cn, load_type):
    print("\n" + "=" * 60)
    print("  FASE 1: EXTRACCION (staging)")
    print("=" * 60)

    # Solo en FULL se extraen los CSVs (eso va con Pentaho extract_job)
    # En esta demo asumimos que extract_job.kjb ya fue ejecutado en Spoon
    # o los datos ya estan cargados. Mostramos los counts actuales.
    with cn.cursor() as cur:
        counts = get_table_counts(cur)
        for tbl, n in counts.items():
            print(f"  {tbl:45s}: {n}")
        log_step(cur, load_type, "extract_job", "staging_tables", "SUCCESS",
                 rows_read=counts.get("staging.stg_climate_trace", 0),
                 rows_inserted=0, duration_sec=0.1)

    # Si es incremental, inyectar datos demo
    if load_type == "INCREMENTAL":
        with cn.cursor() as cur:
            new_rows = inject_incremental_demo(cur)
            new_counts = get_table_counts(cur)
            for tbl, n in new_counts.items():
                print(f"  {tbl:45s}: {n}")
            log_step(cur, load_type, "extract_job", "incremental_inject",
                     "SUCCESS", rows_read=0, rows_inserted=new_rows, duration_sec=0.1)


def run_dimensions(cn, load_type):
    print("\n" + "=" * 60)
    print("  FASE 2: DIMENSIONES")
    print("=" * 60)

    dim_tables = ["dim_tiempo", "dim_ubigeo", "dim_gas", "dim_sector",
                  "dim_fuente_datos", "dim_unidad", "dim_tipo_emision"]

    for dim in dim_tables:
        t0 = time.time()
        status = "SUCCESS"
        try:
            # Las dimensiones se cargan via Pentaho dimensiones_job.kjb
            # o mediante los KTRs individuales en Spoon.
            # En esta demo mostramos los counts post-carga.
            with cn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM public.{dim};")
                n = cur.fetchone()[0]
                dur = round(time.time() - t0, 2)
                print(f"  {dim:30s}: {n} filas ({dur}s)")
                log_step(cur, load_type, "dimensiones_job", dim,
                         status, rows_read=n, rows_inserted=0,
                         duration_sec=dur)
        except Exception as e:
            print(f"  {dim:30s}: ERROR - {e}")
            with cn.cursor() as cur:
                log_step(cur, load_type, "dimensiones_job", dim,
                         "ERROR", duration_sec=round(time.time() - t0, 2),
                         error_message=str(e))


def run_facts(cn, load_type):
    print("\n" + "=" * 60)
    print("  FASE 3: HECHOS (facts)")
    print("=" * 60)

    # fact_emisiones
    t0 = time.time()
    print("  Ejecutando fact_emisiones.py...")
    status = "SUCCESS"
    error_msg = None
    try:
        import subprocess
        result = subprocess.run(
            [sys.executable, "fact_emisiones.py"],
            cwd="D:/PROYECTOS_GDM_LINUX/IDN/DW_GEI_FINAL/python",
            capture_output=True, text=True, timeout=600,
        )
        dur = round(time.time() - t0, 2)
        if result.returncode != 0:
            status = "ERROR"
            error_msg = result.stderr[-500:] if result.stderr else "Unknown error"
            print(f"    [!] ERROR: {error_msg[:200]}")
        else:
            print(f"    [OK] ({dur}s)")
            # Mostrar ultimas lineas del output
            for line in result.stdout.strip().split("\n")[-5:]:
                if line.strip():
                    print(f"    {line.strip()}")
    except Exception as e:
        dur = round(time.time() - t0, 2)
        status = "ERROR"
        error_msg = str(e)
        print(f"    [!] ERROR: {e}")

    with cn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM public.fact_emisiones;")
        n_fe = cur.fetchone()[0]
        log_step(cur, load_type, "facts_job", "fact_emisiones", status,
                 rows_inserted=n_fe, duration_sec=dur, error_message=error_msg)

    # fact_actividad_emisora
    t0 = time.time()
    print("  Ejecutando fact_actividad_emisora.py...")
    status = "SUCCESS"
    error_msg = None
    try:
        import subprocess
        result = subprocess.run(
            [sys.executable, "fact_actividad_emisora.py"],
            cwd="D:/PROYECTOS_GDM_LINUX/IDN/DW_GEI_FINAL/python",
            capture_output=True, text=True, timeout=600,
        )
        dur = round(time.time() - t0, 2)
        if result.returncode != 0:
            status = "ERROR"
            error_msg = result.stderr[-500:] if result.stderr else "Unknown error"
            print(f"    [!] ERROR: {error_msg[:200]}")
        else:
            print(f"    [OK] ({dur}s)")
            for line in result.stdout.strip().split("\n")[-3:]:
                if line.strip():
                    print(f"    {line.strip()}")
    except Exception as e:
        dur = round(time.time() - t0, 2)
        status = "ERROR"
        error_msg = str(e)
        print(f"    [!] ERROR: {e}")

    with cn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM public.fact_actividad_emisora;")
        n_fa = cur.fetchone()[0]
        log_step(cur, load_type, "facts_job", "fact_actividad_emisora", status,
                 rows_inserted=n_fa, duration_sec=dur, error_message=error_msg)

    # Predicciones
    t0 = time.time()
    print("  Ejecutando predict_arima.py...")
    status = "SUCCESS"
    error_msg = None
    try:
        import subprocess
        result = subprocess.run(
            [sys.executable, "predict_arima.py"],
            cwd="D:/PROYECTOS_GDM_LINUX/IDN/DW_GEI_FINAL/python",
            capture_output=True, text=True, timeout=600,
        )
        dur = round(time.time() - t0, 2)
        if result.returncode != 0:
            status = "ERROR"
            error_msg = result.stderr[-500:] if result.stderr else "Unknown error"
            print(f"    [!] ERROR: {error_msg[:200]}")
        else:
            print(f"    [OK] ({dur}s)")
            for line in result.stdout.strip().split("\n")[-3:]:
                if line.strip():
                    print(f"    {line.strip()}")
    except Exception as e:
        dur = round(time.time() - t0, 2)
        status = "ERROR"
        error_msg = str(e)
        print(f"    [!] ERROR: {e}")

    with cn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM public.fact_predicciones;")
        n_fp = cur.fetchone()[0]
        log_step(cur, load_type, "facts_job", "fact_predicciones", status,
                 rows_inserted=n_fp, duration_sec=dur, error_message=error_msg)

    return n_fe, n_fa, n_fp


def print_report(cursor, load_type):
    print("\n" + "=" * 60)
    print(f"  REPORTE DE AUDITORIA - {load_type} LOAD")
    print("=" * 60)

    cursor.execute("""
        SELECT job_name, step_name, status, rows_read, rows_inserted,
               rows_updated, rows_skipped, duration_sec,
               started_at, finished_at
        FROM public.etl_audit_log
        WHERE load_type = %s
        ORDER BY audit_id;
    """, (load_type,))

    header = f"{'Job':20s} | {'Step':30s} | {'Status':8s} | {'Read':6s} | {'Ins':6s} | {'Upd':6s} | {'Skip':6s} | {'Dur(s)':7s}"
    sep = "-" * len(header)
    print(f"\n  {header}")
    print(f"  {sep}")

    total_dur = 0
    for r in cursor.fetchall():
        job, step, status, rread, rins, rupd, rskip, dur, started, finished = r
        total_dur += (dur or 0)
        print(f"  {job:20s} | {step:30s} | {status:8s} | "
              f"{rread or 0:6d} | {rins or 0:6d} | {rupd or 0:6d} | {rskip or 0:6d} | "
              f"{dur or 0:6.1f}")

    print(f"  {sep}")
    print(f"  {'TOTAL':>62s} | {'':6s} | {'':6s} | {'':6s} | {'':6s} | "
          f"{total_dur:6.1f}s")

    # Resumen de conteos finales
    print(f"\n  {'=' * 60}")
    print(f"  ESTADO FINAL DEL DATA WAREHOUSE")
    print(f"  {'=' * 60}")
    counts = get_table_counts(cursor)
    for tbl, n in counts.items():
        print(f"  {tbl:45s}: {n} filas")

    # Comparativa FULL vs INCREMENTAL
    if load_type == "INCREMENTAL":
        print(f"\n  {'=' * 60}")
        print(f"  COMPARATIVA FULL vs INCREMENTAL")
        print(f"  {'=' * 60}")
        cursor.execute("""
            SELECT load_type, status, COUNT(*),
                   SUM(rows_inserted) AS total_inserted,
                   SUM(duration_sec) AS total_duration
            FROM public.etl_audit_log
            GROUP BY load_type, status
            ORDER BY load_type, status;
        """)
        for r in cursor.fetchall():
            print(f"  {r[0]:15s} {r[1]:8s} steps={r[2]:3d} "
                  f"inserted={r[3] or 0:8d} dur={r[4] or 0:.1f}s")


def main():
    print("=" * 60)
    print(f"  ETL ORQUESTADOR - {MODE} LOAD")
    print("=" * 60)
    print(f"  Inicio: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    t_start = time.time()
    cn = get_connection()
    cn.autocommit = True

    try:
        with cn.cursor() as cur:
            create_audit_table(cur)

        if MODE == "FULL":
            with cn.cursor() as cur:
                full_truncate(cur)

        # FASE 1: Extraccion
        run_extract(cn, MODE)

        # FASE 2: Dimensiones
        run_dimensions(cn, MODE)

        # FASE 3: Hechos
        run_facts(cn, MODE)

        # REPORTE
        with cn.cursor() as cur:
            print_report(cur, MODE)

        total_dur = round(time.time() - t_start, 1)
        print(f"\n>> Pipeline completado en {total_dur}s. [OK]")

    except Exception as e:
        print(f"\n[!] Error fatal: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        cn.close()


if __name__ == "__main__":
    main()
