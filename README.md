# IDN - Inventario de Gases de Efecto Invernadero de Peru

Data warehouse para el analisis de emisiones GEI en Peru, integrando 4 fuentes internacionales (Climate Trace, EDGAR, Climate Watch, FAOSTAT) con resolucion geoespacial a nivel distrital.

## Arquitectura

```
EXTERNAL/                   # Datos fuente (~40 CSVs)
DW_GEI_FINAL/
  pentaho/
    load/                   # Jobs orquestadores (.kjb)
      extract_job.kjb       # Extraccion: CSV -> staging
      dimensiones_job.kjb   # Dimensiones: 7 dim_*.ktr
      facts_job.kjb         # Hechos + predicciones
    transformations/
      extract/              # 5 KTRs de extraccion
      dimensions/           # 7 KTRs de dimension
      facts/                # 2 KTRs de hechos (Shell->Python)
  python/
    config.py               # Conexion BD, constantes GWP
    fact_emisiones.py       # Construye fact_emisiones (7 dims, 4 fuentes)
    fact_actividad_emisora.py # Construye fact_actividad_emisora (solo CT)
    predict_arima.py        # Forecast ARIMA + regresion lineal (2025-2034)
    data_mining.py          # K-Means, Isolation Forest, Decision Tree
    etl_orquestador.py      # Orquestador con auditoria FULL/INCREMENTAL
    validate.py             # Validacion fact_emisiones
    validate_actividad.py   # Validacion fact_actividad_emisora
```

## Modelo de datos (Star Schema)

**7 dimensiones:** dim_tiempo, dim_ubigeo, dim_gas, dim_sector, dim_fuente_datos, dim_tipo_emision, dim_unidad

**2 tablas de hechos:**

| Tabla | Medidas | Fuentes | Granularidad |
|---|---|---|---|
| `fact_emisiones` | cantidad_emisiones, cantidad_emisiones_co2eq | CT, EDGAR, CW, FAOSTAT | Mensual/Anual x Distrito/Nacional |
| `fact_actividad_emisora` | cantidad_actividad, cantidad_capacidad, factor_emision | CT | Mensual/Anual x Distrito |

**Tablas de analytics:**

| Tabla | Contenido |
|---|---|
| `fact_predicciones` | Forecast 2025-2034 (ARIMA + OLS), con intervalo de confianza 95% |
| `mining_clusters` | K-Means: 3 perfiles de emision por sector/gas |
| `mining_anomalies` | Isolation Forest: anos anomalos por sector/gas |
| `mining_features` | Features del arbol de decision (nivel ALTO/BAJO) |
| `etl_audit_log` | Auditoria: filas, duracion, estado por paso ETL |

## Fuentes de datos

| Fuente | Periodo | Granularidad | Gases |
|---|---|---|---|
| **Climate Trace** | 2021-2024 | Mensual, geolocalizado | CO2, CH4, N2O |
| **EDGAR** | 1970-2022 | Mensual, nacional | CO2, CH4, N2O |
| **Climate Watch** | 1990-2023 | Anual, nacional | CO2, CH4, N2O (CO2e) |
| **FAOSTAT** | 2000-2023 | Anual, nacional | CO2, CH4, N2O |

## Requisitos

- Python 3.11+
- PostgreSQL 18
- Pentaho Data Integration 9+
- Power BI Desktop

### Dependencias Python

```bash
pip install -r requirements.txt
```

### Base de datos

```sql
CREATE DATABASE idn_gei;
```

Editar `DW_GEI_FINAL/python/config.py` con las credenciales de PostgreSQL.

## Ejecucion

### Pipeline completo

```bash
cd DW_GEI_FINAL/python

# Carga FULL (trunca + reconstruye)
python etl_orquestador.py FULL

# Carga INCREMENTAL (inyecta delta demo + re-ejecuta facts)
python etl_orquestador.py INCREMENTAL
```

### Validacion

```bash
python validate.py
python validate_actividad.py
```

### Predicciones y mineria

```bash
python predict_arima.py      # Forecast 2025-2034
python data_mining.py        # Clustering, anomalias, arbol
```

### Via Pentaho (Spoon)

```
extract_job.kjb -> dimensiones_job.kjb -> facts_job.kjb
```

Cada KTR de dimension incluye un step `Write to log` que muestra los valores procesados en la consola.

## Calculo de CO2 equivalente

- **Climate Trace:** valor en toneladas x GWP100
- **EDGAR:** Gg x 1000 x GWP100
- **Climate Watch:** ya viene en CO2e (MtCO2e x 1,000,000)
- **FAOSTAT:** kt x 1000 x GWP100

GWP100 (IPCC AR5): CO2=1, CH4=28, N2O=265

## Resolucion geoespacial

Climate Trace proporciona `geometry_ref` a nivel provincial. El pipeline resuelve a nivel distrital buscando el centroide mas cercano dentro de la provincia correspondiente en `dim_ubigeo` (1,816 distritos del Peru). EDGAR, CW y FAOSTAT se asignan a nivel nacional.
