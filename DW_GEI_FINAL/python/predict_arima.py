# -*- coding: utf-8 -*-
"""Predicciones ARIMA (top 5 EDGAR) + regresion lineal (resto) para fact_emisiones.

EDGAR mensual: 5 series mas largas con ARIMA estacional
EDGAR resto + CW + FAOSTAT: regresion lineal simple

Predecir 10 anios (2025-2034). Guardar en public.fact_predicciones.
"""

import sys
from datetime import datetime

import pandas as pd
import numpy as np
from pmdarima import auto_arima
from statsmodels.regression.linear_model import OLS
from statsmodels.tools import add_constant

from config import get_connection

HORIZONTE_ANIOS = 10
ANIO_INICIO_PRED = 2025
N_ARIMA_SERIES = 5


def create_pred_table(cursor):
    print(">> Creando public.fact_predicciones...")
    cursor.execute("DROP TABLE IF EXISTS public.fact_predicciones;")
    cursor.execute("""
        CREATE TABLE public.fact_predicciones (
            prediccion_id            BIGSERIAL PRIMARY KEY,
            fuente_datos_id           INTEGER NOT NULL REFERENCES dim_fuente_datos(fuente_datos_sk),
            tiempo_id                INTEGER NOT NULL REFERENCES dim_tiempo(id_tiempo),
            gas_id                   INTEGER NOT NULL REFERENCES dim_gas(gas_sk),
            sector_id                INTEGER NOT NULL REFERENCES dim_sector(sector_sk),
            cantidad_emisiones_co2eq  NUMERIC(18,6),
            co2eq_lower              NUMERIC(18,6),
            co2eq_upper              NUMERIC(18,6),
            es_historico              BOOLEAN DEFAULT FALSE,
            modelo                   VARCHAR(50),
            fecha_prediccion          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (fuente_datos_id, tiempo_id, gas_id, sector_id, es_historico)
        );
    """)
    print("  [OK] Tabla lista.")


def extend_dim_tiempo(cursor):
    print(f">> Extendiendo dim_tiempo para {ANIO_INICIO_PRED}-{ANIO_INICIO_PRED + HORIZONTE_ANIOS - 1}...")
    for anio in range(ANIO_INICIO_PRED, ANIO_INICIO_PRED + HORIZONTE_ANIOS):
        cursor.execute("""
            INSERT INTO dim_tiempo (anio, mes, granularidad, flag_mensual, fecha_inicio)
            VALUES (%s, NULL, 'year', 0, %s::date)
            ON CONFLICT DO NOTHING;
        """, (anio, f'{anio}-01-01'))
        for mes in range(1, 13):
            cursor.execute("""
                INSERT INTO dim_tiempo (anio, mes, trimestre, granularidad, flag_mensual, fecha_inicio)
                VALUES (%s, %s, %s, 'month', 1, %s::date)
                ON CONFLICT DO NOTHING;
            """, (anio, mes, (mes - 1) // 3 + 1, f'{anio}-{mes:02d}-01'))
    print("  [OK] dim_tiempo extendido.")


def build_cache(cursor):
    c = {"fuente": {}, "gas": {}, "sector": {}, "tiempo": {}}
    cursor.execute("SELECT fuente_datos_sk, fuente_datos_codigo FROM dim_fuente_datos;")
    for sk, code in cursor.fetchall():
        c["fuente"][(code or "").strip()] = sk
    cursor.execute("SELECT gas_sk, gas_codigo FROM dim_gas;")
    for sk, code in cursor.fetchall():
        c["gas"][(code or "").strip()] = sk
    cursor.execute("SELECT sector_sk, clasificacion_origen, sector, subsector FROM dim_sector;")
    for sk, co, sec, sub in cursor.fetchall():
        co = (co or "").strip()
        sec = (sec or "").strip()
        sub = (sub or "").strip()
        c["sector"][(co, sec, sub)] = sk
    cursor.execute("SELECT id_tiempo, anio, mes FROM dim_tiempo;")
    for sk, anio, mes in cursor.fetchall():
        c["tiempo"][(anio, mes if mes else 0)] = sk
    return c


def fetch_edgar_series(cursor):
    cursor.execute("""
        SELECT dg.gas_codigo, ds.sector,
               dt.anio, dt.mes, SUM(fe.cantidad_emisiones_co2eq) AS tco2e
        FROM fact_emisiones fe
        JOIN dim_gas dg ON fe.gas_id = dg.gas_sk
        JOIN dim_sector ds ON fe.sector_id = ds.sector_sk
        JOIN dim_tiempo dt ON fe.tiempo_id = dt.id_tiempo
        JOIN dim_fuente_datos df ON fe.fuente_datos_id = df.fuente_datos_sk
        WHERE df.fuente_datos_codigo = 'EDGAR' AND dt.mes IS NOT NULL AND dt.mes != 0
        GROUP BY dg.gas_codigo, ds.sector, dt.anio, dt.mes
        ORDER BY dg.gas_codigo, ds.sector, dt.anio, dt.mes;
    """)
    cols = [d[0] for d in cursor.description]
    df = pd.DataFrame(cursor.fetchall(), columns=cols)
    print(f"  EDGAR: {len(df)} filas, {df.groupby(['gas_codigo','sector']).ngroups} series")
    return df


def fetch_anual_series(cursor):
    cursor.execute("""
        SELECT dfd.fuente_datos_codigo, ds.clasificacion_origen,
               dg.gas_codigo, ds.sector, ds.subsector, dt.anio,
               SUM(fe.cantidad_emisiones_co2eq) AS tco2e
        FROM fact_emisiones fe
        JOIN dim_gas dg ON fe.gas_id = dg.gas_sk
        JOIN dim_sector ds ON fe.sector_id = ds.sector_sk
        JOIN dim_tiempo dt ON fe.tiempo_id = dt.id_tiempo
        JOIN dim_fuente_datos dfd ON fe.fuente_datos_id = dfd.fuente_datos_sk
        WHERE dfd.fuente_datos_codigo IN ('CW','FAOSTAT')
          AND (dt.mes IS NULL OR dt.mes = 0)
        GROUP BY dfd.fuente_datos_codigo, ds.clasificacion_origen,
                 dg.gas_codigo, ds.sector, ds.subsector, dt.anio
        ORDER BY dfd.fuente_datos_codigo, dg.gas_codigo, ds.sector, dt.anio;
    """)
    cols = [d[0] for d in cursor.description]
    df = pd.DataFrame(cursor.fetchall(), columns=cols)
    print(f"  CW+FAOSTAT: {len(df)} filas, {df.groupby(['fuente_datos_codigo','gas_codigo','sector']).ngroups} series")
    return df


def fit_arima(ts):
    try:
        model = auto_arima(
            ts, seasonal=True, m=12,
            trace=False, error_action='ignore',
            suppress_warnings=True, stepwise=True,
            max_p=3, max_q=3, max_P=1, max_Q=1, max_d=1, max_D=1,
            n_jobs=1,
        )
        fc, ci = model.predict(n_periods=120, return_conf_int=True)
        return fc, ci[:, 0], ci[:, 1]
    except Exception as e:
        print(f"[!] ARIMA: {e}")
        return None, None, None


def fit_lineal(series, horizon):
    try:
        y = series.values.astype(float)
        x = np.arange(len(y)).reshape(-1, 1).astype(float)
        m = OLS(y, add_constant(x)).fit()
        x_fut = np.arange(len(y), len(y) + horizon).reshape(-1, 1).astype(float)
        pred = m.get_prediction(add_constant(x_fut))
        fc = pred.predicted_mean
        ci = pred.conf_int()
        ci_low = np.nan_to_num(ci[:, 0], nan=fc)
        ci_up = np.nan_to_num(ci[:, 1], nan=fc)
        return fc, ci_low, ci_up
    except Exception as e:
        print(f"[!] LINEAL: {e}")
        return None, None, None


def insert_batch(cn, rows):
    if not rows:
        return 0
    from psycopg2.extras import execute_values
    tpl = """
        INSERT INTO fact_predicciones
            (fuente_datos_id, tiempo_id, gas_id, sector_id,
             cantidad_emisiones_co2eq, co2eq_lower, co2eq_upper,
             es_historico, modelo, fecha_prediccion)
        VALUES %s
        ON CONFLICT DO NOTHING;
    """
    with cn.cursor() as cur:
        execute_values(cur, tpl, rows)
    return len(rows)


def get_sks(caches, fuente, gas, co, sector, subsector):
    fsk = caches["fuente"][fuente]
    gsk = caches["gas"][gas]
    ssk = caches["sector"].get((co, sector, subsector))
    if ssk is None:
        ssk = caches["sector"].get((co, sector, ""))
    if ssk is None:
        for (c, s, su), sk in caches["sector"].items():
            if c == co and s == sector:
                ssk = sk
                break
    return fsk, gsk, ssk


# --- EDGAR ---

def process_edgar(df, caches, cn):
    print("\n=== EDGAR ===")
    grupos = {k: v for k, v in df.groupby(['gas_codigo','sector'])}
    sorted_keys = sorted(grupos.keys(), key=lambda k: len(grupos[k]), reverse=True)
    arima_keys = sorted_keys[:N_ARIMA_SERIES]
    lineal_keys = sorted_keys[N_ARIMA_SERIES:]
    print(f"  ARIMA: {len(arima_keys)}, LINEAL: {len(lineal_keys)}")
    total = 0
    co = "EDGAR"  # clasificacion_origen fijo

    for idx, (gas, sector) in enumerate(arima_keys, 1):
        g = grupos[(gas, sector)].copy()
        g['fecha'] = pd.to_datetime(
            g['anio'].astype(str)+'-'+g['mes'].astype(str).str.zfill(2)+'-01')
        ts = g.set_index('fecha')['tco2e'].sort_index().asfreq('MS')
        print(f"  ARIMA [{idx}] {gas}/{sector} ({len(ts)} pts)", end=" ", flush=True)
        fc, cl, cu = fit_arima(ts)
        if fc is not None:
            print("[OK]")
            total += insert_ts(caches, cn, ts, fc, cl, cu, 'EDGAR', gas, co, sector, '', 'ARIMA', True)
        else:
            total += insert_ts_fallback(caches, cn, ts, 'EDGAR', gas, co, sector, '', True)

    for idx, (gas, sector) in enumerate(lineal_keys, 1):
        g = grupos[(gas, sector)].copy()
        g['fecha'] = pd.to_datetime(
            g['anio'].astype(str)+'-'+g['mes'].astype(str).str.zfill(2)+'-01')
        ts = g.set_index('fecha')['tco2e'].sort_index().asfreq('MS')
        print(f"  LINEAL [{idx}] {gas}/{sector} ({len(ts)} pts)", end=" ", flush=True)
        total += insert_ts_fallback(caches, cn, ts, 'EDGAR', gas, co, sector, '', True)
        print("[OK]")
    return total


def insert_ts(caches, cn, ts, forecast, ci_low, ci_up, fuente, gas, co, sector, subsector, modelo, monthly):
    fsk, gsk, ssk = get_sks(caches, fuente, gas, co, sector, subsector)
    if ssk is None:
        return 0
    rows = []
    now = datetime.now()
    for fecha, val in ts.items():
        tid = caches["tiempo"].get((fecha.year, fecha.month))
        if tid:
            rows.append((fsk, tid, gsk, ssk, float(val), float(val), float(val), True, modelo, now))
    base = ts.index[-1]
    for i in range(120):
        pd_date = base + pd.DateOffset(months=i + 1)
        tid = caches["tiempo"].get((pd_date.year, pd_date.month))
        if tid:
            f_val = float(forecast.iloc[i]) if hasattr(forecast, 'iloc') else float(forecast[i])
            rows.append((fsk, tid, gsk, ssk, f_val, float(ci_low[i]), float(ci_up[i]),
                         False, modelo, now))
    return insert_batch(cn, rows)


def insert_ts_fallback(caches, cn, ts, fuente, gas, co, sector, subsector, monthly):
    vals = pd.Series(ts.values.astype(float), index=np.arange(len(ts)))
    fc, cl, cu = fit_lineal(vals, 120)
    if fc is None:
        return 0
    return insert_ts(caches, cn, ts, fc, cl, cu, fuente, gas, co, sector, subsector, 'LINEAL', monthly)


# --- ANUAL (CW + FAOSTAT) ---

def process_anual(df, caches, cn):
    print("\n=== CW + FAOSTAT (LINEAL) ===")
    grupos = {k: v for k, v in df.groupby(
        ['fuente_datos_codigo','clasificacion_origen','gas_codigo','sector','subsector'])}
    total = 0
    idx = 0
    for (fuente, co, gas, sector, subsector), group in grupos.items():
        g = group.copy()
        ts = g.set_index('anio')['tco2e'].sort_index()
        if len(ts) < 5:
            continue
        idx += 1
        print(f"  [{idx}] {fuente}/{gas}/{sector} ({len(ts)} pts)", end=" ", flush=True)
        fc, cl, cu = fit_lineal(ts, HORIZONTE_ANIOS)
        if fc is None:
            print("[!]")
            continue
        print("[OK]")
        fsk, gsk, ssk = get_sks(caches, fuente, gas, co, sector, subsector)
        if ssk is None:
            continue
        rows = []
        now = datetime.now()
        for anio, val in ts.items():
            tid = caches["tiempo"].get((anio, 0))
            if tid:
                rows.append((fsk, tid, gsk, ssk, float(val), float(val), float(val), True, 'LINEAL', now))
        last = ts.index[-1]
        for i in range(HORIZONTE_ANIOS):
            py = last + i + 1
            tid = caches["tiempo"].get((py, 0))
            if tid:
                rows.append((fsk, tid, gsk, ssk, float(fc[i]), float(cl[i]), float(cu[i]), False, 'LINEAL', now))
        total += insert_batch(cn, rows)
    return total


def show_summary(cursor):
    print("\n>> Resumen fact_predicciones:")
    cursor.execute("""
        SELECT dfd.fuente_datos_codigo, modelo, es_historico, COUNT(*),
               ROUND(SUM(cantidad_emisiones_co2eq)::numeric, 2)
        FROM fact_predicciones fp
        JOIN dim_fuente_datos dfd ON fp.fuente_datos_id = dfd.fuente_datos_sk
        GROUP BY dfd.fuente_datos_codigo, modelo, es_historico
        ORDER BY dfd.fuente_datos_codigo, modelo, es_historico;
    """)
    for r in cursor.fetchall():
        tipo = "HIST" if r[2] else "PRED"
        print(f"  {r[0]:10s} {r[1]:6s} {tipo:4s}  filas={r[3]:6d}  tCO2e={r[4]}")


def main():
    print("=" * 60)
    print("  PREDICCIONES - ARIMA + LINEAL")
    print("=" * 60)
    cn = get_connection()
    try:
        with cn.cursor() as cur:
            create_pred_table(cur)
            extend_dim_tiempo(cur)

        with cn.cursor() as cur:
            caches = build_cache(cur)
            print(f"\n  Caches: fuentes={len(caches['fuente'])} gases={len(caches['gas'])} "
                  f"sectores={len(caches['sector'])} tiempos={len(caches['tiempo'])}")
            df_edgar = fetch_edgar_series(cur)
            df_anual = fetch_anual_series(cur)

        total = process_edgar(df_edgar, caches, cn)
        total += process_anual(df_anual, caches, cn)
        print(f"\n>> Total filas insertadas: {total}")

        with cn.cursor() as cur:
            show_summary(cur)

        print("\n>> [OK] Predicciones completadas.")
    except Exception as e:
        print(f"\n[!] Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        cn.close()


if __name__ == "__main__":
    main()
