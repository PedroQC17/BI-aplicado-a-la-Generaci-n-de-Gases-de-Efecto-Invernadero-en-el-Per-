# -*- coding: utf-8 -*-
"""Data Mining sobre DW de emisiones GEI.

Tecnicas:
  1. K-Means: clustering de sectores por perfil de emisiones (3 grupos)
  2. Isolation Forest: deteccion de anomalos por (sector, gas) anual
  3. Decision Tree: factores que predicen emisiones altas vs bajas

Resultados guardados en PostgreSQL. Power BI lee directo.
"""

import sys
from datetime import datetime

import pandas as pd
import numpy as np
from sklearn.cluster import KMeans
from sklearn.ensemble import IsolationForest
from sklearn.tree import DecisionTreeClassifier, export_text
from sklearn.preprocessing import StandardScaler, LabelEncoder

from config import get_connection


def create_mining_tables(cursor):
    """Crea las tablas de resultados de mineria."""
    cursor.execute("DROP TABLE IF EXISTS public.mining_clusters;")
    cursor.execute("""
        CREATE TABLE public.mining_clusters (
            sector_id       INTEGER NOT NULL REFERENCES dim_sector(sector_sk),
            gas_id          INTEGER NOT NULL REFERENCES dim_gas(gas_sk),
            fuente_datos_id  INTEGER NOT NULL REFERENCES dim_fuente_datos(fuente_datos_sk),
            cluster_id      INTEGER NOT NULL,
            cluster_label   VARCHAR(50),
            emision_promedio NUMERIC(18,6),
            tendencia        NUMERIC(18,6),
            PRIMARY KEY (sector_id, gas_id, fuente_datos_id)
        );
    """)

    cursor.execute("DROP TABLE IF EXISTS public.mining_anomalies;")
    cursor.execute("""
        CREATE TABLE public.mining_anomalies (
            sector_id       INTEGER NOT NULL,
            gas_id          INTEGER NOT NULL,
            fuente_datos_id  INTEGER NOT NULL,
            tiempo_id       INTEGER NOT NULL,
            emision_real    NUMERIC(18,6),
            anomaly_score   NUMERIC(18,6),
            es_anomalo      BOOLEAN DEFAULT FALSE,
            PRIMARY KEY (sector_id, gas_id, fuente_datos_id, tiempo_id)
        );
    """)

    cursor.execute("DROP TABLE IF EXISTS public.mining_features;")
    cursor.execute("""
        CREATE TABLE public.mining_features (
            sector_id       INTEGER NOT NULL,
            gas_id          INTEGER NOT NULL,
            anio            INTEGER NOT NULL,
            emision_co2eq   NUMERIC(18,6),
            nivel_emision   VARCHAR(20),
            PRIMARY KEY (sector_id, gas_id, anio)
        );
    """)

    # Decision Tree rules
    cursor.execute("DROP TABLE IF EXISTS public.mining_rules;")
    cursor.execute("""
        CREATE TABLE public.mining_rules (
            rule_id   SERIAL PRIMARY KEY,
            rule_text TEXT,
            accuracy  NUMERIC(5,2)
        );
    """)

    print("  [OK] Tablas de mineria creadas.")


def fetch_annual_series(cursor):
    """Extrae series anuales por (fuente, gas, sector) con suficientes datos."""
    print("\n>> Extrayendo datos anuales para mineria...")
    cursor.execute("""
        SELECT fe.fuente_datos_id, fe.gas_id, fe.sector_id,
               dt.anio, SUM(fe.cantidad_emisiones_co2eq) AS tco2e
        FROM fact_emisiones fe
        JOIN dim_tiempo dt ON fe.tiempo_id = dt.id_tiempo
        WHERE (dt.mes IS NULL OR dt.mes = 0)
        GROUP BY fe.fuente_datos_id, fe.gas_id, fe.sector_id, dt.anio
        ORDER BY fe.fuente_datos_id, fe.gas_id, fe.sector_id, dt.anio;
    """)
    cols = [d[0] for d in cursor.description]
    df = pd.DataFrame(cursor.fetchall(), columns=cols)
    print(f"  {len(df)} filas, {df.groupby(['fuente_datos_id','gas_id','sector_id']).ngroups} series")
    return df


def run_clustering(df, cn):
    """K-Means: agrupar (sector, gas) por perfil de emisiones."""
    print("\n" + "=" * 50)
    print("  TECNICA 1: K-Means Clustering")
    print("=" * 50)

    # Agregar a nivel (sector, gas) anual: promedio + tendencia
    grupos = df.groupby(['fuente_datos_id', 'gas_id', 'sector_id'])
    features = []
    keys = []
    for (fsk, gsk, ssk), g in grupos:
        if len(g) < 5:
            continue
        g = g.sort_values('anio')
        avg = g['tco2e'].mean()
        tendencia = 0
        if avg > 0 and len(g) >= 3:
            x = np.arange(len(g))
            try:
                coef = np.polyfit(x, g['tco2e'].values.astype(float), 1)
                tendencia = coef[0] / avg if avg > 0 else 0
            except Exception:
                tendencia = 0
        features.append([avg, tendencia])
        keys.append((fsk, gsk, ssk))

    if len(features) < 3:
        print("  [!] Muy pocas series para clustering.")
        return

    X = np.array(features)
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # Determinar K optimo simple (silhouette seria mejor pero caro)
    k = 3
    kmeans = KMeans(n_clusters=k, random_state=42, n_init=10)
    labels = kmeans.fit_predict(X_scaled)

    # Etiquetar clusters
    cluster_profiles = {}
    for i in range(k):
        mask = labels == i
        cluster_profiles[i] = {
            "avg_emision": X[mask, 0].mean(),
            "avg_tendencia": X[mask, 1].mean(),
            "size": mask.sum(),
        }

    cluster_names = {}
    for i, p in cluster_profiles.items():
        if p["avg_tendencia"] > 0.01:
            cluster_names[i] = "CRECIMIENTO ALTO"
        elif p["avg_tendencia"] < -0.01:
            cluster_names[i] = "DECRECIENTE"
        elif p["avg_emision"] > np.median([pp["avg_emision"] for pp in cluster_profiles.values()]):
            cluster_names[i] = "ESTABLE ALTO"
        else:
            cluster_names[i] = "ESTABLE BAJO"

    print(f"\n  Clusters encontrados (K={k}):")
    for i in range(k):
        p = cluster_profiles[i]
        print(f"    Cluster {i} ({cluster_names[i]}): "
              f"n={p['size']} avg_tco2e={p['avg_emision']:.1f} tendencia={p['avg_tendencia']:.4f}")

    # Insertar en BD
    rows = []
    for idx, (fsk, gsk, ssk) in enumerate(keys):
        rows.append((ssk, gsk, fsk, int(labels[idx]),
                     cluster_names[int(labels[idx])],
                     float(features[idx][0]),
                     float(features[idx][1])))

    from psycopg2.extras import execute_values
    with cn.cursor() as cur:
        execute_values(cur, """
            INSERT INTO mining_clusters
                (sector_id, gas_id, fuente_datos_id, cluster_id, cluster_label,
                 emision_promedio, tendencia)
            VALUES %s
            ON CONFLICT (sector_id, gas_id, fuente_datos_id) DO UPDATE SET
                cluster_id = EXCLUDED.cluster_id,
                cluster_label = EXCLUDED.cluster_label,
                emision_promedio = EXCLUDED.emision_promedio,
                tendencia = EXCLUDED.tendencia;
        """, rows)

    print(f"  [OK] {len(rows)} series clasificadas en {k} clusters.")
    return len(rows)


def run_anomaly_detection(df, cn):
    """Isolation Forest: detectar anos anomalos por (sector, gas)."""
    print("\n" + "=" * 50)
    print("  TECNICA 2: Deteccion de Anomalias (Isolation Forest)")
    print("=" * 50)

    # Agrupar por (sector, gas) con suficientes anos
    grupos = df.groupby(['fuente_datos_id', 'gas_id', 'sector_id'])
    anomalias = []
    anomaly_rows = []

    for (fsk, gsk, ssk), g in grupos:
        if len(g) < 5:
            continue

        g = g.sort_values('anio')
        values = g['tco2e'].values.astype(float).reshape(-1, 1)

        if values.max() == values.min():
            continue

        model = IsolationForest(contamination=0.05, random_state=42)
        preds = model.fit_predict(values)
        scores = model.decision_function(values)

        for i, (anio, val, pred, score) in enumerate(
            zip(g['anio'], g['tco2e'], preds, scores)):
            es_anomalo = pred == -1
            anomalias.append({
                'sector_id': ssk, 'gas_id': gsk,
                'fuente_datos_id': fsk, 'anio': anio,
                'tco2e': val, 'score': score,
                'es_anomalo': es_anomalo,
                'tiempo_key': (anio, 0),
            })

    print(f"  Total puntos analizados: {len(anomalias)}")
    print(f"  Anomalos detectados: {sum(1 for a in anomalias if a['es_anomalo'])}")
    print(f"  % anómalo: {sum(1 for a in anomalias if a['es_anomalo']) / max(len(anomalias), 1) * 100:.1f}%")

    # Insertar con tiempo_id
    with cn.cursor() as cur:
        cur.execute("SELECT id_tiempo, anio, mes FROM dim_tiempo;")
        tiempo_map = {(anio, mes or 0): tid for tid, anio, mes in cur.fetchall()}

    inserted = 0
    batch = []
    for a in anomalias:
        tid = tiempo_map.get(a['tiempo_key'])
        if tid:
            batch.append((int(a['sector_id']), int(a['gas_id']), int(a['fuente_datos_id']),
                         int(tid), float(a['tco2e']), float(a['score']),
                         True if a['es_anomalo'] else False))

    from psycopg2.extras import execute_values
    with cn.cursor() as cur:
        execute_values(cur, """
            INSERT INTO mining_anomalies
                (sector_id, gas_id, fuente_datos_id, tiempo_id,
                 emision_real, anomaly_score, es_anomalo)
            VALUES %s
            ON CONFLICT DO NOTHING;
        """, batch)
        inserted = len(batch)

    print(f"  [OK] {inserted} filas insertadas en mining_anomalies.")
    return inserted


def run_decision_tree(df, cn):
    """Decision Tree: predecir nivel de emision (ALTO/BAJO) por sector, gas, anio."""
    print("\n" + "=" * 50)
    print("  TECNICA 3: Arbol de Decision")
    print("=" * 50)

    # Crear features: anio, gas_codigo, sector_id
    # Target: emision ALTA (> mediana) o BAJA
    df_annual = df.groupby(['fuente_datos_id', 'gas_id', 'sector_id', 'anio'])['tco2e'].sum().reset_index()

    if len(df_annual) < 10:
        print("  [!] Muy pocos datos para arbol de decision.")
        return 0

    median_emission = df_annual['tco2e'].median()
    df_annual['nivel_emision'] = df_annual['tco2e'].apply(
        lambda x: 'ALTO' if x > median_emission else 'BAJO')

    # Features: gas_id, sector_id (encoded), anio
    X = df_annual[['gas_id', 'sector_id', 'anio']].values
    le = LabelEncoder()
    y = le.fit_transform(df_annual['nivel_emision'])

    # Entrenar arbol
    tree = DecisionTreeClassifier(max_depth=4, min_samples_leaf=10, random_state=42)
    tree.fit(X, y)
    accuracy = tree.score(X, y)

    print(f"  Muestras: {len(df_annual)} (median={median_emission:.1f} tCO2e)")
    print(f"  Accuracy: {accuracy:.2f}")
    print(f"  Reglas del arbol:")

    # Extraer reglas como texto
    feature_names = ['gas_id', 'sector_id', 'anio']
    class_names = ['BAJO', 'ALTO']
    rules = export_text(tree, feature_names=feature_names)

    for line in rules.strip().split('\n'):
        print(f"    {line}")

    # Guardar features
    with cn.cursor() as cur:
        cur.execute("TRUNCATE mining_features;")
        cur.execute("SELECT gas_sk, gas_codigo FROM dim_gas;")
        gas_map = {sk: cod for sk, cod in cur.fetchall()}
        cur.execute("SELECT sector_sk, sector FROM dim_sector;")
        sector_map = {sk: sec for sk, sec in cur.fetchall()}

    batch = []
    for _, row in df_annual.iterrows():
        batch.append((int(row['sector_id']), int(row['gas_id']), int(row['anio']),
                      float(row['tco2e']), str(row['nivel_emision'])))

    from psycopg2.extras import execute_values
    with cn.cursor() as cur:
        execute_values(cur, """
            INSERT INTO mining_features (sector_id, gas_id, anio, emision_co2eq, nivel_emision)
            VALUES %s
            ON CONFLICT DO NOTHING;
        """, batch)

    # Guardar reglas
    rules_batch = [(rules, accuracy)]
    with cn.cursor() as cur:
        cur.execute("TRUNCATE mining_rules;")
        execute_values(cur, """
            INSERT INTO mining_rules (rule_text, accuracy) VALUES %s;
        """, rules_batch)

    # Importancia de features
    importances = tree.feature_importances_
    print(f"\n  Importancia de features:")
    for fn, imp in zip(feature_names, importances):
        print(f"    {fn}: {imp:.3f}")

    print(f"  [OK] Arbol de decision guardado.")
    return len(df_annual)


def main():
    print("=" * 60)
    print("  DATA MINING - Emisiones GEI")
    print("=" * 60)

    cn = get_connection()
    cn.autocommit = True

    try:
        with cn.cursor() as cur:
            create_mining_tables(cur)

        with cn.cursor() as cur:
            df = fetch_annual_series(cur)

        # Tecnica 1: Clustering
        run_clustering(df, cn)

        # Tecnica 2: Anomaly Detection
        run_anomaly_detection(df, cn)

        # Tecnica 3: Decision Tree
        run_decision_tree(df, cn)

        print(f"\n{'=' * 60}")
        print("  RESUMEN DE TABLAS GENERADAS")
        print(f"{'=' * 60}")
        with cn.cursor() as cur:
            for tbl in ['mining_clusters', 'mining_anomalies', 'mining_features', 'mining_rules']:
                cur.execute(f"SELECT COUNT(*) FROM public.{tbl};")
                n = cur.fetchone()[0]
                print(f"  {tbl:25s}: {n} filas")

        print(f"\n>> Data Mining completado. [OK]")
        print(f">> Abri Power BI y conecta a las tablas mining_* para visualizar.")

    except Exception as e:
        print(f"\n[!] Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        cn.close()


if __name__ == "__main__":
    main()
