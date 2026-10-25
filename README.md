# vb — recolector y detector de value betting (fase de papel)

Vigila los partidos de fútbol de las competiciones configuradas, compara las cuotas de las casas
españolas con el precio justo de Pinnacle (vía OddsPapi), avisa por Telegram cuando hay valor,
registra cada señal como apuesta en papel y calcula su CLV contra el cierre de Pinnacle.

Diseño: `diseno_sistema_v1_2026-10-25.md` (proyecto Value Betting). Métrica: CLV, no beneficio.

## Qué hace, en orden

1. **Partidos**: cada 12 h descarga los partidos de los próximos 8 días (`/fixtures`, 1 consulta por competición).
2. **Barrido**: cuando algún partido está a menos de 100 min del inicio, cada 10 min pide las cuotas de
   todas las casas (`/odds-by-tournaments`, 1 consulta por casa = 6 por barrido). Guarda cada cuota con
   su marca de tiempo (`snapshots`). El intervalo se alarga solo si el consumo proyectado supera la cuota.
3. **Detector**: precio justo de Pinnacle (Shin en 1X2, multiplicativo en 2 resultados), filtros de
   referencia fresca (≤ 210 s) y estable, EV entre +2,5/3 % y +12 %, cuota entre 1,30 y 3,50/4,50,
   misma línea. Cada señal → fila en `signals` + apuesta en papel en `bets` (1 unidad) + aviso Telegram.
4. **Comprobación**: en el barrido siguiente mira si la cuota de la señal seguía disponible (`still_available`).
5. **Cierre**: 15 min tras el inicio descarga el histórico de Pinnacle (`/historical-odds`, **gratis**),
   toma el último precio antes del inicio, le quita el margen y calcula el CLV de cada señal.
6. **Liquidación**: 3 h después, para partidos con señales, pide el marcador (`/scores`, 1 consulta) y
   anota resultado y beneficio en unidades. El beneficio se mira, no decide nada.
7. **Latido**: cada 6 h un mensaje a Telegram con cuota usada y próximo partido.

## Instalación en Windows (VPS compartido con MAREA)

Carpeta propia `C:\OTRO\vb`, con su propio entorno virtual: no toca el Python ni las tareas de MAREA.
Consumo: un proceso Python que duerme casi todo el tiempo; unos pocos MB de RAM y red solo en los barridos
(unos minutos antes de cada partido). No usa el bróker ni Claude.

1. Descomprimir el zip en cualquier carpeta (por ejemplo `C:\OTRO\vb-src`).
2. PowerShell **como administrador**:
   ```powershell
   cd C:\OTRO\vb-src\vb
   powershell -ExecutionPolicy Bypass -File deploy\install_windows.ps1
   ```
   Copia a `C:\OTRO\vb`, crea `.venv`, instala `requests`, registra la tarea **VB Collector** (arranca con el
   sistema, se reinicia sola si falla) y **VB Backup** (copia diaria de la BD a las 05:00).
3. Editar `C:\OTRO\vb\.env`: pegar `ODDSPAPI_KEY` y, si se quiere, el bot de Telegram.
4. Comprobar y arrancar:
   ```powershell
   cd C:\OTRO\vb
   .venv\Scripts\python -m vb check        # 0 consultas: plan, 6 casas en OK, cuota
   .venv\Scripts\python -m vb markets      # 1 consulta
   .venv\Scripts\python -m vb fixtures     # 1 por competición
   Start-ScheduledTask -TaskName "VB Collector"
   Get-Content data\vb.log -Wait            # log en vivo
   ```
Parar: `Stop-ScheduledTask -TaskName "VB Collector"`. Desinstalar: `Unregister-ScheduledTask -TaskName "VB Collector"`.

## Instalación en Linux (alternativa)

`deploy/vb.service` es una unidad systemd: copiar el proyecto a `/opt/vb`, crear `.venv`, `pip install requests`,
rellenar `.env`, `cp deploy/vb.service /etc/systemd/system/ && systemctl enable --now vb`.

## Comandos

| Comando | Consultas | Qué hace |
|---|---|---|
| `check` | 0 | plan, casas incluidas, cuota usada |
| `tournaments [filtro…]` | 1 | ids de competiciones con partidos próximos |
| `markets` | 1 | catálogo de mercados (ids fijos) |
| `fixtures` | 1/competición | actualiza partidos |
| `run` / `once` | ver arriba | bucle permanente / una iteración |
| `report` | 0 | CLV por casa, mercado, antelación, cuota, EV; disponibilidad |
| `staleness` | 0 | edad media de las cuotas por casa (radiografía del retardo) |
| `backtest FROM TO [max]` | 1/competición + histórico gratis | simula el detector sobre partidos pasados |
| `bt-report` / `bt-export f.csv` | 0 | informe / CSV del backtest |
| `bet ID EUR` | 0 | registra apuesta real sobre la señal (fase 5) |
| `settle FIXTURE H A` | 0 | liquida a mano un partido |

## Presupuesto de consultas (plan Normal, 5.000/mes)

Barrido = 6 consultas. Ventana = 100 min → 10 barridos a 10 min = 60 consultas por "ventana de
partidos" (los partidos que coinciden en hora comparten barrido). Con fútbol top-5 + Europa son
~15-20 ventanas por semana → 900-1.200/semana. **Por encima de la cuota**: el intervalo adaptativo
lo alarga hasta 30 min cuando hace falta. Para quedarse en 10 min hay que elegir: menos competiciones
(Liga + Champions + Europa League ≈ 8-10 ventanas/semana ≈ 2.500/mes) o pasar al plan Dev.
Recomendación de arranque: `VB_TOURNAMENTS` = LaLiga + Champions + Europa League, y medir.

## Lo que hay que verificar en los primeros días

- `marketActive=false` en bet365.es con cuotas vigentes: el detector no lo bloquea. Comprobar en la web
  de bet365 cuando salte una señal si la cuota estaba de verdad.
- Significado de los periodos de `/scores` (la heurística de `final_score` está en `vb/settle.py`).
  Si falla, liquidar a mano con `settle`.
- Frecuencia real de actualización de `pinnacle+30` en la última hora (`staleness`).
- Totales y hándicaps: solo se compara la **línea principal** de cada casa; si la principal de Pinnacle
  no coincide con la de la casa, no hay comparación. En papel basta; en real conviene guardar todas las líneas.

## Tablas

`fixtures`, `markets`, `sweeps`, `snapshots` (una fila por cuota y barrido), `signals`, `bets`,
`closing_lines`, `api_calls` (auditoría de consumo), `kv`. Backtest: `bt_fixtures`, `bt_signals`.

## Pruebas

```bash
python3 -m unittest tests/test_vb.py tests/test_smoke.py
```
Usan una respuesta real de OddsPapi (LaLiga, 25-oct-2026) recortada a mercados principales.
